"""Writing a settings section into profile.yaml (F-SETTINGS SETTINGS-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rifferr.core.ai import AISettings
from rifferr.core.profile import ProfileLoadError
from rifferr.profile_store import ProfileChanged, create_profile
from rifferr.settings import SECTIONS, file_hash, key_plan, link_key, save_section, unlink_key

AI = SECTIONS["ai"]
START = """\
# my profile
backend: beets
vars: {inbox: /srv/inbox}  # where imports come from
ai:
  # tuned for the local box
  concurrency: 1
  timeout: 60s  # NAS is slow
  # providers below
  providers:
    - name: local  # ollama on the NAS
      type: openai-compatible
      model: qwen3
      base_url: http://127.0.0.1:11434/v1
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    (tmp_path / "beets").mkdir()
    create_profile(root, "main", beetsdir=tmp_path / "beets")
    (root / "profiles" / "main" / "profile.yaml").write_text(START, encoding="utf-8")
    return root


def path(root: Path) -> Path:
    return root / "profiles" / "main" / "profile.yaml"


def h(root: Path) -> str:
    return file_hash(path(root).read_bytes())


def test_SETTINGS_3_writes_only_its_key_and_keeps_comments(root: Path) -> None:
    value = AISettings.model_validate(
        {
            "concurrency": 3,
            "providers": [
                {"name": "claude", "type": "anthropic", "model": "claude-haiku-5-5"},
                {
                    "name": "local",
                    "type": "openai-compatible",
                    "model": "qwen3:8b",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "settings": {"price_per_mtok": {"input": 0, "output": 0.5}},
                },
            ],
            "suggest": {"mode": "always"},
        }
    )
    new_hash = save_section(root, "main", AI, value, h(root), env={})
    text = path(root).read_text(encoding="utf-8")
    assert new_hash == h(root)
    assert text.startswith(
        "# my profile\nbackend: beets\nvars: {inbox: /srv/inbox}  # where imports"
    )
    assert "# tuned for the local box" in text
    assert "- name: local  # ollama on the NAS" in text
    assert "concurrency: 3" in text
    assert "model: qwen3:8b" in text
    assert "output: 0.5" in text
    assert "cache_ttl" not in text  # defaults stay out of the file
    assert text.index("name: claude") < text.index("name: local")


def test_SETTINGS_3_a_changed_file_is_a_conflict_and_nothing_is_written(root: Path) -> None:
    before = path(root).read_text(encoding="utf-8")
    with pytest.raises(ProfileChanged):
        save_section(root, "main", AI, AISettings(), "0" * 64, env={})
    assert path(root).read_text(encoding="utf-8") == before


def test_SETTINGS_2_an_invalid_profile_is_not_written(root: Path) -> None:
    before = path(root).read_text(encoding="utf-8")
    value = AISettings.model_validate(
        {"providers": [{"name": "c", "type": "anthropic", "model": "m", "api_key": "nope"}]}
    )
    with pytest.raises(ProfileLoadError, match="must name an entry in secrets"):
        save_section(root, "main", AI, value, h(root), env={})
    assert path(root).read_text(encoding="utf-8") == before


def test_SETTINGS_3_all_defaults_add_no_section(root: Path) -> None:
    text = path(root).read_text(encoding="utf-8")
    path(root).write_text(text[: text.index("ai:")], encoding="utf-8")
    save_section(root, "main", AI, AISettings(), h(root), env={})
    assert "ai:" not in path(root).read_text(encoding="utf-8")


def test_SETTINGS_6_link_and_unlink_a_key_name(root: Path) -> None:
    plan = key_plan("main", "local")
    assert (plan.secret, plan.keyring_ref, plan.env_var) == (
        "ai-local",
        "keyring:rifferr/main/local",
        "RIFFERR_MAIN_LOCAL_KEY",
    )
    link_key(root, "main", "local", plan)
    text = path(root).read_text(encoding="utf-8")
    assert "ai-local: keyring:rifferr/main/local" in text
    assert "api_key: ai-local" in text
    unlink_key(root, "main", "local", plan.secret)
    text = path(root).read_text(encoding="utf-8")
    assert "ai-local" not in text
    assert "secrets" not in text
    with pytest.raises(KeyError):
        link_key(root, "main", "nope", plan)


def test_SETTINGS_3_hand_written_defaults_and_their_comments_stay(root: Path) -> None:
    value = AISettings.model_validate(
        {
            "concurrency": 1,
            "providers": [
                {"name": "local", "type": "openai-compatible", "model": "qwen3", "enabled": True}
            ],
        }
    )
    save_section(root, "main", AI, value, h(root), env={})
    text = path(root).read_text(encoding="utf-8")
    assert "timeout: 60s  # NAS is slow\n  # providers below" in text  # default, kept
    assert "cache_ttl" not in text  # default the file never had: not added
    assert "enabled" not in text
    assert "base_url" not in text  # cleared: removed
    assert "- name: local  # ollama on the NAS" in text
