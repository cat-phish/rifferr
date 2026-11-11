from __future__ import annotations

import os
from pathlib import Path

import pytest

from rifferr.core.profile import SecretRef
from rifferr.credentials import SecretNotAvailableError, SecretResolver


def resolver(**kw: object) -> SecretResolver:
    return SecretResolver(**kw)  # type: ignore[arg-type]


def test_PROFILE_4_env_secret() -> None:
    r = resolver(env={"TOKEN": "abc"})
    assert r.resolve(SecretRef.parse("env:TOKEN")).reveal() == "abc"
    with pytest.raises(SecretNotAvailableError, match="TOKEN"):
        resolver(env={}).resolve(SecretRef.parse("env:TOKEN"))


def test_PROFILE_4_keyring_secret_uses_injected_lookup() -> None:
    calls: list[tuple[str, str]] = []

    def lookup(service: str, name: str) -> str | None:
        calls.append((service, name))
        return "from-keyring" if name == "discogs" else None

    r = resolver(keyring_get=lookup)
    assert r.resolve(SecretRef.parse("keyring:rifferr/discogs")).reveal() == "from-keyring"
    assert calls == [("rifferr", "discogs")]
    with pytest.raises(SecretNotAvailableError, match="rifferr/other"):
        r.resolve(SecretRef.parse("keyring:rifferr/other"))


def test_PROFILE_5_errors_never_contain_values(tmp_path: Path) -> None:
    f = tmp_path / "s"
    f.write_text("sekrit-value\n")
    os.chmod(f, 0o644)
    with pytest.raises(SecretNotAvailableError) as exc:
        resolver().resolve(SecretRef.parse(f"file:{f}"))
    assert "sekrit" not in str(exc.value)


def test_PROFILE_11_file_secret_strips_one_trailing_newline(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("abc\n\n")
    os.chmod(f, 0o600)
    assert resolver().resolve(SecretRef.parse(f"file:{f}")).reveal() == "abc\n"
    f.write_text("abc\r\n")
    assert resolver().resolve(SecretRef.parse(f"file:{f}")).reveal() == "abc"


def test_PROFILE_11_file_secret_readable_by_others_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("abc")
    os.chmod(f, 0o604)
    with pytest.raises(SecretNotAvailableError, match="readable by other users"):
        resolver().resolve(SecretRef.parse(f"file:{f}"))


def test_PROFILE_11_docker_secrets_dir_is_exempt_from_mode_check(tmp_path: Path) -> None:
    run_secrets = tmp_path / "run" / "secrets"
    run_secrets.mkdir(parents=True)
    f = run_secrets / "token"
    f.write_text("abc")
    os.chmod(f, 0o644)
    r = resolver(docker_secrets_dir=run_secrets)
    assert r.resolve(SecretRef.parse(f"file:{f}")).reveal() == "abc"


@pytest.mark.parametrize("kind", ["missing", "dir"])
def test_PROFILE_11_file_secret_must_be_regular_file(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "x"
    if kind == "dir":
        target.mkdir()
    with pytest.raises(SecretNotAvailableError):
        resolver().resolve(SecretRef.parse(f"file:{target}"))


def test_PROFILE_11_file_secret_is_read_at_use_not_on_construction(tmp_path: Path) -> None:
    f = tmp_path / "token"
    r = resolver()
    ref = SecretRef.parse(f"file:{f}")
    f.write_text("later")
    os.chmod(f, 0o600)
    assert r.resolve(ref).reveal() == "later"
