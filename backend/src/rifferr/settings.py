"""Profile settings edited from the UI (F-SETTINGS).

Each section owns one top-level key of ``profile.yaml`` and is validated with the same model
the profile loader uses (SETTINGS-2). Writing merges the new values into the file's own YAML
nodes, so comments, key order and every other key survive (SETTINGS-3), and goes through a
temporary file renamed into place. Secrets never pass through here as values: keys go to
the keyring (``KeyringStore``), the file only names them (SETTINGS-6).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from rifferr.core.ai import AISettings
from rifferr.core.profile import Profile, ProfileLoadError, parse_profile
from rifferr.profile_store import (
    PROFILE_FILE,
    ProfileChanged,
    ProfileError,
    _timezone_exists,  # pyright: ignore[reportPrivateUsage]
    atomic_write,
    profile_dir,
)

KEYRING_SERVICE = "rifferr"


class SettingsSection(Protocol):
    """One part of the Settings page (SETTINGS-1). ``key`` is its top-level key in
    ``profile.yaml``; ``model`` is the loader's model for it."""

    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def key(self) -> str: ...

    @property
    def model(self) -> type[BaseModel]: ...

    def read(self, profile: Profile) -> BaseModel: ...


@dataclass(frozen=True)
class AISection:
    """The AI section (SETTINGS-5): ``ai`` in ``profile.yaml`` (AI-CORE-5)."""

    id: str = "ai"
    title: str = "AI"
    key: str = "ai"
    model: type[BaseModel] = AISettings

    def read(self, profile: Profile) -> BaseModel:
        return profile.ai


SECTIONS: dict[str, SettingsSection] = {s.id: s for s in [AISection()]}


def section_hashes(profile: Profile) -> dict[str, str]:
    """A hash of each section's loaded values, recorded with every run job so the run
    log can say when settings changed between its jobs (SETTINGS-9). Built from the
    values, not the file text, so a comment or reordering is not a change. Sections hold
    secret names only, never keys (SETTINGS-6)."""
    return {
        id: hashlib.sha256(section.read(profile).model_dump_json().encode()).hexdigest()[:16]
        for id, section in SECTIONS.items()
    }


# -- writing --------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A dumped model value as YAML scalars: Decimals as numbers, Paths as text."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _named(entries: Any) -> bool:
    return isinstance(entries, list) and all(
        isinstance(e, Mapping) and isinstance(e.get("name"), str)  # pyright: ignore[reportUnknownMemberType]
        for e in entries  # pyright: ignore[reportUnknownVariableType]
    )


_ABSENT: Any = object()


def merge(node: Any, full: Any, sparse: Any = _ABSENT) -> Any:
    """``full`` written into the existing YAML ``node``. ``sparse`` is the same value
    without its defaults. A map key is written when it isn't a default, or when the file
    already has it (so a hand-written default and its comment stay), and is removed when
    the value no longer has it or it became empty (``None``). Lists of named maps are
    matched by ``name`` so each entry keeps its comments; anything else is replaced."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if sparse is _ABSENT:
        sparse = full
    if isinstance(full, Mapping):
        target: Any = node if isinstance(node, CommentedMap) else CommentedMap()
        sp: Mapping[str, Any] = sparse if isinstance(sparse, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]
        keep = [
            k
            for k in full  # pyright: ignore[reportUnknownVariableType]
            if k in sp or (k in target and full[k] is not None)
        ]
        for k in [k for k in target if k not in keep]:
            del target[k]
        for k in keep:
            target[k] = merge(target.get(k), full[k], sp.get(k, {}))
        return target
    if isinstance(full, list):
        sp_list: list[Any] = sparse if isinstance(sparse, list) else []  # pyright: ignore[reportUnknownVariableType]
        seq = CommentedSeq()
        if _named(full) and isinstance(node, CommentedSeq) and _named(list(node)):
            by_name = {e["name"]: e for e in node}
            sp_by_name = {e["name"]: e for e in sp_list if isinstance(e, Mapping)}  # pyright: ignore[reportUnknownMemberType]
            for entry in full:  # pyright: ignore[reportUnknownVariableType]
                name = entry["name"]
                seq.append(merge(by_name.get(name), entry, sp_by_name.get(name, entry)))
            return seq
        for i, v in enumerate(full):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            seq.append(merge(None, v, sp_list[i] if i < len(sp_list) else v))
        return seq
    return full


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _edit(
    root: Path,
    name: str,
    change: Callable[[Any], None],
    expected_hash: str | None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Load ``profile.yaml``, apply ``change`` to its YAML map, check the result loads as
    a profile, and write it atomically. Raises ``ProfileChanged`` when the file's hash
    isn't ``expected_hash`` (when given) and ``ProfileLoadError`` when the result is
    invalid. Returns the new hash."""
    from ruamel.yaml.comments import CommentedMap

    from rifferr.core.workflow.yamldoc import YamlSyntaxError, parse

    path = profile_dir(root, name) / PROFILE_FILE
    if not path.is_file():
        raise ProfileError(f"no profile '{name}' (expected {path})")
    raw = path.read_bytes()
    if expected_hash is not None and file_hash(raw) != expected_hash:
        raise ProfileChanged(name)
    try:
        doc = parse(raw.decode("utf-8"), str(path))
    except YamlSyntaxError as exc:
        raise ProfileLoadError([exc.error]) from None
    if not isinstance(doc.data, CommentedMap):
        doc.data = CommentedMap()
    change(doc.data)
    doc.touch()
    text = doc.dump()
    parse_profile(
        text,
        str(path),
        name=name,
        root=path.parent,
        env=os.environ if env is None else env,
        timezone_exists=_timezone_exists,
    )
    out = text.encode("utf-8")
    atomic_write(path, out)
    return file_hash(out)


def save_section(
    root: Path,
    name: str,
    section: SettingsSection,
    value: BaseModel,
    expected_hash: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Write ``value`` as the section's key in ``profile.yaml`` (SETTINGS-3). A default
    value is written only where the file already has that setting, so hand-written lines
    and their comments stay; a section with nothing left is removed. Returns the new
    hash."""
    full = _plain(value.model_dump(mode="python"))
    sparse = _plain(value.model_dump(mode="python", exclude_defaults=True))

    def change(data: Any) -> None:
        merged = merge(data.get(section.key), full, sparse)
        if merged:
            data[section.key] = merged
        elif section.key in data:
            del data[section.key]

    return _edit(root, name, change, expected_hash, env)


# -- provider keys (SETTINGS-6) ---------------------------------------------------------


class KeyringStore(Protocol):
    """The OS keyring, injectable for tests."""

    def available(self) -> bool: ...

    def get(self, service: str, name: str) -> str | None: ...

    def set(self, service: str, name: str, value: str) -> None: ...

    def delete(self, service: str, name: str) -> None: ...


class SystemKeyring:
    """The ``keyring`` package's default backend. Unavailable when it has none (e.g.
    Docker), so the page offers an environment variable instead."""

    def available(self) -> bool:
        import keyring
        from keyring.backends import chainer, fail

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return False
        return not (isinstance(kr, chainer.ChainerBackend) and not kr.backends)

    def get(self, service: str, name: str) -> str | None:
        import keyring

        return keyring.get_password(service, name)

    def set(self, service: str, name: str, value: str) -> None:
        import keyring

        keyring.set_password(service, name, value)

    def delete(self, service: str, name: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):  # already gone
            keyring.delete_password(service, name)


@dataclass(frozen=True)
class KeyPlan:
    """Where a provider's key goes: the profile's secret name, the keyring entry, and the
    environment variable to use without a keyring."""

    secret: str
    keyring_name: str
    env_var: str

    @property
    def keyring_ref(self) -> str:
        return f"keyring:{KEYRING_SERVICE}/{self.keyring_name}"

    @property
    def env_line(self) -> str:
        return f"secrets: {{{self.secret}: env:{self.env_var}}}"


def key_plan(profile: str, provider: str) -> KeyPlan:
    """The names the page proposes for ``provider``'s key (SETTINGS-6)."""
    env = re.sub(r"[^A-Z0-9]+", "_", f"RIFFERR_{profile}_{provider}_KEY".upper())
    return KeyPlan(secret=f"ai-{provider}", keyring_name=f"{profile}/{provider}", env_var=env)


def _provider_entry(data: Any, provider: str) -> Any:
    ai = data.get("ai")
    providers = ai.get("providers") if isinstance(ai, Mapping) else None
    for entry in providers or []:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(entry, Mapping) and entry.get("name") == provider:  # pyright: ignore[reportUnknownMemberType]
            return entry
    raise KeyError(provider)


def link_key(root: Path, profile: str, provider: str, plan: KeyPlan) -> str:
    """Name the keyring entry under ``secrets`` and point the provider's ``api_key`` at
    it. Raises ``KeyError`` for an unknown provider. Returns the new hash."""
    from ruamel.yaml.comments import CommentedMap

    def change(data: Any) -> None:
        entry = _provider_entry(data, provider)
        secrets = data.get("secrets")
        if not isinstance(secrets, CommentedMap):
            secrets = CommentedMap()
            data["secrets"] = secrets
        secrets[plan.secret] = plan.keyring_ref
        entry["api_key"] = plan.secret

    return _edit(root, profile, change, None)


def unlink_key(root: Path, profile: str, provider: str, secret: str) -> str:
    """Remove the provider's ``api_key`` and the ``secrets`` entry it named, unless
    another provider or setting still uses that name. Returns the new hash."""

    def change(data: Any) -> None:
        entry = _provider_entry(data, provider)
        if entry.get("api_key") == secret:
            del entry["api_key"]
        ai = data.get("ai")
        others = [
            p.get("api_key")
            for p in (ai.get("providers") or [] if isinstance(ai, Mapping) else [])  # pyright: ignore[reportUnknownMemberType]
            if isinstance(p, Mapping)
        ]
        secrets = data.get("secrets")
        if isinstance(secrets, Mapping) and secret in secrets and secret not in others:
            del secrets[secret]  # pyright: ignore[reportIndexIssue]
            if not secrets:
                del data["secrets"]

    return _edit(root, profile, change, None)
