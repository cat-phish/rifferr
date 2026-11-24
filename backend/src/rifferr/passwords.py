"""The single password login (F-SECURITY SECURITY-17, -18).

``<config dir>/auth.json`` holds the user name and an argon2id hash, never the password.
The server reads it again whenever the file changes, so ``rifferr passwd`` needs no
restart.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

AUTH_FILE: Final = "auth.json"
DEFAULT_USER: Final = "admin"
MIN_LENGTH: Final = 12


class PasswordError(ValueError):
    """A password or login that can't be used; the message never repeats the password."""


@dataclass(frozen=True)
class Login:
    user: str
    hash: str


def auth_path(config_dir: Path) -> Path:
    return config_dir / AUTH_FILE


def read_login(path: Path) -> Login | None:
    """The login in ``path``, or None when there's none (or the file is unusable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    user, hashed = data.get("user"), data.get("hash")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(user, str) or not isinstance(hashed, str) or not user or not hashed:
        return None
    return Login(user, hashed)


def _write(path: Path, login: Login) -> None:
    """Atomically, mode 0600 from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".auth-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"user": login.user, "hash": login.hash}, f)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_password(path: Path, password: str, user: str = DEFAULT_USER) -> Login:
    """Hash ``password`` and store it as the login. Raises ``PasswordError``."""
    user = user.strip()
    if not user or any(c.isspace() for c in user):
        raise PasswordError("the user name must be one word")
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"the password must have at least {MIN_LENGTH} characters")
    login = Login(user, PasswordHasher().hash(password))
    _write(path, login)
    return login


def remove_login(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


class PasswordCheck:
    """Checks a sign-in against the login in ``path``, read again when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._hasher = PasswordHasher()
        self._stamp: tuple[int, int] | None = None
        self._login: Login | None = None
        self._dummy: str | None = None

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def login(self) -> Login | None:
        stamp = self._stat()
        if stamp != self._stamp:
            self._stamp = stamp
            self._login = read_login(self.path) if stamp is not None else None
        return self._login

    def enabled(self) -> bool:
        return self.login() is not None

    def verify(self, user: str, password: str) -> bool:
        """Whether ``user`` and ``password`` match the login. Slow on purpose (argon2), and
        as slow for an unknown name. A match with an outdated hash is rehashed."""
        login = self.login()
        if login is None:
            return False
        name_ok = hmac.compare_digest(user.encode(), login.user.encode())
        target = login.hash if name_ok else self._dummy_hash()
        try:
            self._hasher.verify(target, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if not name_ok:
            return False
        if self._hasher.check_needs_rehash(login.hash):
            with contextlib.suppress(OSError):
                _write(self.path, Login(login.user, self._hasher.hash(password)))
        return True

    def _dummy_hash(self) -> str:
        if self._dummy is None:
            self._dummy = self._hasher.hash(secrets.token_urlsafe(16))
        return self._dummy
