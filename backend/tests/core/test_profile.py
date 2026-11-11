from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rifferr.core.profile import (
    SECRET_REF_HELP,
    ProfileError,
    ProfileLoadError,
    Redactor,
    Secret,
    SecretRef,
    env_var_name,
    merged_vars,
    parse_profile,
    resolve_vars,
    validate_profile_name,
)
from rifferr.core.workflow import VarDecl

ROOT = Path("/cfg/profiles/main")

FULL = """\
backend: beets
beets:
  beetsdir: /home/u/.config/beets
vars:
  inbox: /srv/downloads/music
  library: /srv/music
secrets:
  discogs_token: keyring:rifferr/discogs
  anthropic_api_key: env:ANTHROPIC_API_KEY
  lastfm: file:/run/secrets/lastfm
timezone: Europe/Amsterdam
"""


def load(text: str) -> object:
    return parse_profile(textwrap.dedent(text), "profile.yaml", name="main", root=ROOT, env={})


def errors(text: str) -> list[str]:
    with pytest.raises(ProfileLoadError) as exc:
        load(text)
    return [str(e) for e in exc.value.errors]


def test_PROFILE_2_full_profile_parses() -> None:
    p = parse_profile(FULL, "profile.yaml", name="main", root=ROOT, env={})
    assert p.name == "main"
    assert p.root == ROOT
    assert p.backend == "beets"
    assert p.beets.beetsdir == "/home/u/.config/beets"
    assert p.vars == {"inbox": "/srv/downloads/music", "library": "/srv/music"}
    assert p.secrets["discogs_token"] == SecretRef(kind="keyring", target="rifferr/discogs")
    assert p.secrets["anthropic_api_key"] == SecretRef(kind="env", target="ANTHROPIC_API_KEY")
    assert p.secrets["lastfm"] == SecretRef(kind="file", target="/run/secrets/lastfm")
    assert p.timezone == "Europe/Amsterdam"


def test_PROFILE_2_empty_profile_has_defaults() -> None:
    p = parse_profile("{}\n", "profile.yaml", name="main", root=ROOT, env={})
    assert (p.backend, p.beets.beetsdir, p.vars, p.secrets, p.timezone) == (
        "beets",
        None,
        {},
        {},
        None,
    )


def test_PROFILE_2_unknown_keys_are_located_errors() -> None:
    assert errors("vars: {}\nvarz: {}\n") == ["profile.yaml:2:1: varz: unknown key"]


def test_PROFILE_2_var_values_must_be_strings() -> None:
    errs = errors("vars:\n  port: 8080\n")
    assert len(errs) == 1
    assert errs[0].startswith("profile.yaml:2:9: vars.port:")


@pytest.mark.parametrize("bad", ["Main", "-x", "a b", "a/b", "", "x" * 65, "ü"])
def test_PROFILE_3_invalid_profile_names(bad: str) -> None:
    with pytest.raises(ProfileError):
        validate_profile_name(bad)


@pytest.mark.parametrize("good", ["main", "test-2", "a", "0", "x_y", "x" * 64])
def test_PROFILE_3_valid_profile_names(good: str) -> None:
    validate_profile_name(good)


@pytest.mark.parametrize(
    ("ref", "kind", "target"),
    [
        ("keyring:rifferr/discogs", "keyring", "rifferr/discogs"),
        ("env:DISCOGS_TOKEN", "env", "DISCOGS_TOKEN"),
        ("file:/run/secrets/x", "file", "/run/secrets/x"),
    ],
)
def test_PROFILE_4_secret_reference_kinds(ref: str, kind: str, target: str) -> None:
    assert SecretRef.parse(ref) == SecretRef(kind=kind, target=target)  # type: ignore[arg-type]
    assert str(SecretRef.parse(ref)) == ref


@pytest.mark.parametrize(
    "bad",
    [
        "abc123secretvalue",
        "keyring:",
        "keyring:svc",
        "keyring:/name",
        "env:",
        "env:1BAD",
        "file:relative/path",
        "vault:x",
    ],
)
def test_PROFILE_4_literal_or_malformed_secret_is_an_error_without_echoing_it(bad: str) -> None:
    errs = errors(f"secrets:\n  token: '{bad}'\n")
    assert errs == [f"profile.yaml:2:10: secrets.token: {SECRET_REF_HELP}"]


def test_PROFILE_4_non_string_secret_is_an_error() -> None:
    assert any("secrets.token" in e for e in errors("secrets:\n  token: 12345\n"))


def test_PROFILE_5_secret_wrapper_never_shows_value() -> None:
    s = Secret("hunter2-very-secret")
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s)
    assert "hunter2" not in f"{s}"
    assert s.reveal() == "hunter2-very-secret"


def test_PROFILE_5_redactor_masks_secret_values() -> None:
    r = Redactor([Secret("tok-123456"), Secret("pw-abcdef")])
    assert r.redact("auth tok-123456 and pw-abcdef!") == "auth *** and ***!"
    assert r.redact("nothing here") == "nothing here"


def test_PROFILE_5_redactor_prefers_longest_match_and_ignores_tiny_values() -> None:
    r = Redactor([Secret("abcd"), Secret("abcdefgh"), Secret("x"), Secret("")])
    assert r.redact("abcdefgh abcd x") == "*** *** x"


def test_PROFILE_6_profile_vars_override_workflow_defaults() -> None:
    decls = {
        "inbox": VarDecl(default="/default/in"),
        "mode": VarDecl(default="quiet"),
        "lib": VarDecl(),
    }
    assert merged_vars({"inbox": "/srv/in", "extra": "1"}, decls) == {
        "inbox": "/srv/in",
        "mode": "quiet",
        "extra": "1",
    }


@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("inbox", "RIFFERR_VAR_INBOX"),
        ("music_dir", "RIFFERR_VAR_MUSIC_DIR"),
        ("a-b.c", "RIFFERR_VAR_A_B_C"),
    ],
)
def test_PROFILE_6_shell_env_var_names(name: str, env: str) -> None:
    assert env_var_name(name) == env


def test_PROFILE_7_vars_reference_vars_and_env() -> None:
    raw = {
        "root": "{{ env.HOME }}/music",
        "inbox": "{{ vars.root }}/inbox",
        "deep": "{{ vars.inbox }}/x",
    }
    assert resolve_vars(raw, {"HOME": "/home/u"}) == {
        "root": "/home/u/music",
        "inbox": "/home/u/music/inbox",
        "deep": "/home/u/music/inbox/x",
    }


def test_PROFILE_7_var_cycles_are_errors() -> None:
    with pytest.raises(ProfileError, match="cycle: a -> b -> a"):
        resolve_vars({"a": "{{ vars.b }}", "b": "{{ vars.a }}"}, {})
    with pytest.raises(ProfileError, match="cycle: a -> a"):
        resolve_vars({"a": "x{{ vars.a }}"}, {})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"a": "{{ vars.missing }}"}, "unknown variable 'missing'"),
        ({"a": "{{ env.NOPE }}"}, "environment variable 'NOPE' is not set"),
        ({"a": "{{ run.id }}"}, "only vars.<name> and env.<NAME>"),
        ({"a": "{{ bad"}, "malformed"),
    ],
)
def test_PROFILE_7_unresolvable_vars(raw: dict[str, str], message: str) -> None:
    with pytest.raises(ProfileError, match=message):
        resolve_vars(raw, {})


def test_PROFILE_7_parse_resolves_vars_with_env() -> None:
    p = parse_profile(
        "vars:\n  inbox: '{{ env.HOME }}/in'\n",
        "profile.yaml",
        name="main",
        root=ROOT,
        env={"HOME": "/h"},
    )
    assert p.vars == {"inbox": "/h/in"}


def test_PROFILE_8_profile_without_beetsdir_requires_it_for_library_access() -> None:
    p = parse_profile("{}\n", "profile.yaml", name="main", root=ROOT, env={})
    with pytest.raises(ProfileError, match=r"beets\.beetsdir"):
        p.require_beetsdir()


def test_PROFILE_8_require_beetsdir_returns_path() -> None:
    p = parse_profile("beets: {beetsdir: /b}\n", "profile.yaml", name="main", root=ROOT, env={})
    assert p.require_beetsdir() == Path("/b")
