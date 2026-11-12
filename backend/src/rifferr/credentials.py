"""Resolve secret references at the point of use (PROFILE-4, -5, -11).

Errors name the reference, never the value.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from rifferr.core.profile import Secret, SecretRef

DOCKER_SECRETS_DIR = Path("/run/secrets")
KeyringGet = Callable[[str, str], str | None]


class SecretNotAvailableError(LookupError):
    """A referenced secret can't be read. The message names the reference only."""


def _keyring_get(service: str, name: str) -> str | None:
    import keyring  # imported lazily: probing keyring backends is slow

    return keyring.get_password(service, name)


class SecretResolver:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        keyring_get: KeyringGet | None = None,
        docker_secrets_dir: Path = DOCKER_SECRETS_DIR,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._keyring_get = keyring_get or _keyring_get
        self._docker_dir = docker_secrets_dir

    def resolve(self, ref: SecretRef) -> Secret:
        if ref.kind == "env":
            value = self._env.get(ref.target)
            if value is None:
                raise SecretNotAvailableError(f"{ref}: environment variable is not set")
            return Secret(value)
        if ref.kind == "keyring":
            service, _, name = ref.target.partition("/")
            value = self._keyring_get(service, name)
            if value is None:
                raise SecretNotAvailableError(f"{ref}: no such entry in the keyring")
            return Secret(value)
        return Secret(self._read_file(ref))

    def _read_file(self, ref: SecretRef) -> str:
        path = Path(ref.target)
        try:
            st = path.stat()
        except OSError as exc:
            raise SecretNotAvailableError(f"{ref}: can't read file ({exc.strerror})") from None
        if not stat.S_ISREG(st.st_mode):
            raise SecretNotAvailableError(f"{ref}: not a regular file")
        if st.st_mode & stat.S_IROTH and not path.resolve().is_relative_to(self._docker_dir):
            raise SecretNotAvailableError(
                f"{ref}: file is readable by other users; run chmod o-r on it"
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SecretNotAvailableError(
                f"{ref}: can't read file ({type(exc).__name__})"
            ) from None
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith("\n"):
            return text[:-1]
        return text


class ProfileSecrets:
    """A profile's named secrets for a run (``engine.SecretAccess``), resolved on demand."""

    def __init__(self, secrets: Mapping[str, SecretRef], resolver: SecretResolver) -> None:
        self._refs = dict(secrets)
        self._resolver = resolver

    def names(self) -> list[str]:
        return sorted(self._refs)

    def resolve(self, name: str) -> Secret:
        ref = self._refs.get(name)
        if ref is None:
            known = ", ".join(self.names()) or "none"
            raise SecretNotAvailableError(f"no secret named '{name}' (known: {known})")
        return self._resolver.resolve(ref)

    def env_refs(self) -> Mapping[str, str]:
        return {name: r.target for name, r in self._refs.items() if r.kind == "env"}
