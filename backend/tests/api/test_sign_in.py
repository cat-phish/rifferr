"""Sessions that survive a restart, and password sign-in (F-SECURITY SECURITY-15 to -21)."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from rifferr.api.app import DbSessionStore
from rifferr.api.security import (
    DAY,
    SecurityConfig,
    Sessions,
    SignInThrottle,
    check_bind,
    secure,
    user_agent_label,
)
from rifferr.appdb import AppDB
from rifferr.passwords import PasswordCheck, PasswordError, read_login, set_password

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
COOKIE = f"rifferr_token_{PORT}"
TOKEN = "local-token-" + "x" * 32
PASSWORD = "correct horse battery"
SAME = {"origin": BASE, "content-type": "application/json"}


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def client(
    config: SecurityConfig, base: str = BASE, address: str = "127.0.0.1"
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=secure(app, config), client=(address, 5000))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url=base)


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    set_password(path, PASSWORD)
    return path


def config(auth_file: Path | None = None, **kw: Any) -> SecurityConfig:
    return SecurityConfig(
        port=PORT,
        token=TOKEN,
        passwords=PasswordCheck(auth_file) if auth_file else None,
        **kw,
    )


async def sign_in(c: httpx.AsyncClient, user: str = "admin", password: str = PASSWORD, **h: str):
    return await c.post(
        "/auth/sign-in", json={"username": user, "password": password}, headers={**SAME, **h}
    )


# -- SECURITY-15: sessions survive restarts --------------------------------------------


async def test_SECURITY_15_sessions_survive_a_restart(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    first = Sessions(store=DbSessionStore(db))
    code = first.sign_in_code()
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=first)) as c:
        r = await c.get(f"/auth/exchange?token={code}")
        sid = c.cookies[COOKIE]
    assert "Max-Age=2592000" in r.headers["set-cookie"]  # 30 days
    stored = db._conn.execute("SELECT id_sha256, started_by FROM sessions").fetchall()  # pyright: ignore[reportPrivateUsage]
    assert [tuple(row) for row in stored] == [(Sessions.key(sid), "code")]
    assert sid not in json.dumps([tuple(row) for row in stored])
    db.close()
    # "restart": a new database connection and new in-memory state
    db = AppDB(tmp_path / "app.db")
    second = Sessions(store=DbSessionStore(db))
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=second)) as c:
        c.cookies.set(COOKIE, sid)
        assert (await c.get("/api/runs")).status_code == 200
    db.close()


def test_SECURITY_15_idle_and_maximum_lifetimes() -> None:
    clock = Clock()
    sessions = Sessions(wall=clock, idle=10 * DAY, max_age=25 * DAY)
    sid = sessions.new_session()
    clock.now += 9 * DAY
    row, touched = sessions.check(sid)  # use extends the idle limit
    assert row is not None
    assert touched
    clock.now += 9 * DAY  # 18 days in, 9 since last use
    assert sessions.lookup(sid) is not None
    clock.now += 9 * DAY  # 27 days in: past the maximum though used 9 days ago
    assert sessions.lookup(sid) is None
    other = sessions.new_session()
    clock.now += 10 * DAY + 1  # unused past the idle limit
    assert sessions.lookup(other) is None


def test_SECURITY_15_use_is_recorded_at_most_hourly() -> None:
    clock = Clock()
    sessions = Sessions(wall=clock)
    sid = sessions.new_session()
    clock.now += 60
    assert sessions.check(sid)[1] is False
    clock.now += 3600
    assert sessions.check(sid)[1] is True


async def test_SECURITY_15_cookie_max_age_follows_the_session() -> None:
    clock = Clock()
    sessions = Sessions(wall=clock, idle=2 * DAY)
    sid = sessions.new_session()
    clock.now += DAY
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=sessions)) as c:
        c.cookies.set(COOKIE, sid)
        r = await c.get("/api/runs")
    assert r.status_code == 200
    assert f"{COOKIE}={sid}" in r.headers["set-cookie"]
    assert "Max-Age=172800" in r.headers["set-cookie"]


async def test_SECURITY_15_session_id_is_not_a_bearer_token() -> None:
    sessions = Sessions()
    sid = sessions.new_session()
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=sessions)) as c:
        r = await c.get("/api/runs", headers={"authorization": f"Bearer {sid}"})
    assert r.status_code == 401


def test_SECURITY_15_user_agent_label() -> None:
    ua = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
    assert user_agent_label(ua) == "Firefox on Linux"
    assert user_agent_label(None) == "unknown"


# -- SECURITY-16: signing out ----------------------------------------------------------


async def test_SECURITY_16_sign_out_and_sign_out_everywhere() -> None:
    sessions = Sessions()
    a, b = sessions.new_session(), sessions.new_session()
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=sessions)) as c:
        c.cookies.set(COOKIE, a)
        r = await c.post("/auth/sign-out", json={}, headers=SAME)
        assert r.status_code == 204
        assert "Max-Age=0" in r.headers["set-cookie"]
        assert sessions.lookup(a) is None
        assert sessions.lookup(b) is not None
        c.cookies.clear()
        r = await c.post("/auth/sign-out-all", json={}, headers=SAME)
        assert r.status_code == 401  # needs a session or the token
        c.cookies.set(COOKIE, b)
        c2 = sessions.new_session()
        r = await c.post("/auth/sign-out-all", json={}, headers=SAME)
        assert r.status_code == 204
        assert sessions.lookup(b) is None
        assert sessions.lookup(c2) is None


async def test_SECURITY_16_sign_out_keeps_the_origin_and_json_checks() -> None:
    sessions = Sessions()
    sid = sessions.new_session()
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=sessions)) as c:
        c.cookies.set(COOKIE, sid)
        r = await c.post(
            "/auth/sign-out-all",
            json={},
            headers={"origin": "https://evil.example", "content-type": "application/json"},
        )
        assert r.status_code == 403
        r = await c.post("/auth/sign-out-all", content=b"x", headers={"origin": BASE})
        assert r.status_code == 415
    assert sessions.lookup(sid) is not None


# -- SECURITY-17: the stored login -----------------------------------------------------


def test_SECURITY_17_only_an_argon2id_hash_is_stored(auth_file: Path) -> None:
    text = auth_file.read_text()
    assert PASSWORD not in text
    login = read_login(auth_file)
    assert login is not None
    assert login.user == "admin"
    assert login.hash.startswith("$argon2id$")
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_SECURITY_17_short_passwords_are_refused(tmp_path: Path) -> None:
    with pytest.raises(PasswordError, match="at least 12") as err:
        set_password(tmp_path / "auth.json", "short-pass")
    assert "short-pass" not in str(err.value)
    assert not (tmp_path / "auth.json").exists()


def test_SECURITY_17_the_server_reads_a_changed_file(auth_file: Path) -> None:
    check = PasswordCheck(auth_file)
    assert check.verify("admin", PASSWORD)
    set_password(auth_file, "a different long password", user="me")
    assert not check.verify("admin", PASSWORD)
    assert check.verify("me", "a different long password")
    auth_file.unlink()
    assert not check.enabled()


def _cli(tmp_path: Path, *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "RIFFERR_CONFIG_DIR": str(tmp_path / "config" / "rifferr"),
        "RIFFERR_DATA_DIR": str(tmp_path / "data" / "rifferr"),
    }
    return subprocess.run(
        [sys.executable, "-m", "rifferr.cli", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_SECURITY_17_passwd_cli_sets_the_login_and_ends_sessions(tmp_path: Path) -> None:
    from rifferr.profile_store import app_db_path, config_root

    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "RIFFERR_CONFIG_DIR": str(tmp_path / "config" / "rifferr"),
        "RIFFERR_DATA_DIR": str(tmp_path / "data" / "rifferr"),
    }
    db_path = app_db_path(env)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = AppDB(db_path)
    sessions = Sessions(store=DbSessionStore(db))
    sid = sessions.new_session()
    r = _cli(tmp_path, "passwd", "--stdin", stdin=PASSWORD + "\n")
    assert r.returncode == 0, r.stderr
    assert PASSWORD not in r.stdout + r.stderr
    assert "1 session(s) ended" in r.stdout
    assert sessions.lookup(sid) is None
    login = read_login(config_root(env) / "auth.json")
    assert login is not None
    assert login.user == "admin"
    r = _cli(tmp_path, "passwd", "--stdin", stdin="short\n")
    assert r.returncode == 2
    assert "at least 12" in r.stderr
    sessions.new_session()
    r = _cli(tmp_path, "sign-out", "--all")
    assert (r.returncode, r.stdout.strip()) == (0, "1 session(s) ended")
    r = _cli(tmp_path, "passwd", "--remove")
    assert r.returncode == 0
    assert read_login(config_root(env) / "auth.json") is None
    db.close()


def test_SECURITY_17_passwd_without_a_terminal_needs_stdin(tmp_path: Path) -> None:
    r = _cli(tmp_path, "passwd")
    assert r.returncode == 2
    assert "--stdin" in r.stderr


# -- SECURITY-18: sign-in ----------------------------------------------------------------


async def test_SECURITY_18_sign_in_starts_a_password_session(auth_file: Path) -> None:
    cfg = config(auth_file)
    async with client(cfg) as c:
        assert (await c.get("/auth/status")).json() == {"signed_in": False, "password_login": True}
        r = await sign_in(c)
        assert r.status_code == 204, r.text
        assert "HttpOnly" in r.headers["set-cookie"]
        assert "SameSite=Strict" in r.headers["set-cookie"]
        assert (await c.get("/api/runs")).status_code == 200
        assert (await c.get("/auth/status")).json()["signed_in"] is True
    row = cfg.sessions.lookup(c.cookies[COOKIE])
    assert row is not None
    assert row.started_by == "password"


async def test_SECURITY_18_same_answer_for_a_wrong_name_or_password(
    auth_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with client(config(auth_file)) as c:
        wrong_password = await sign_in(c, password="not the password at all")
        wrong_name = await sign_in(c, user="root")
    assert wrong_password.status_code == wrong_name.status_code == 401
    assert wrong_password.json() == wrong_name.json()
    assert wrong_password.json()["error"]["code"] == "bad_credentials"
    assert "not the password" not in caplog.text
    assert "root" not in caplog.text
    assert "127.0.0.1" in caplog.text


async def test_SECURITY_18_without_a_login(tmp_path: Path) -> None:
    async with client(config(tmp_path / "auth.json")) as c:
        r = await sign_in(c)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "no_password")
        assert (await c.get("/auth/status")).json()["password_login"] is False


async def test_SECURITY_18_bad_bodies(auth_file: Path) -> None:
    async with client(config(auth_file)) as c:
        r = await c.post("/auth/sign-in", json={"username": "admin"}, headers=SAME)
        assert r.status_code == 400
        r = await c.post("/auth/sign-in", content=b"x" * 5000, headers=SAME)
        assert r.status_code == 400
        r = await c.post(
            "/auth/sign-in",
            json={"username": "admin", "password": PASSWORD},
            headers={"origin": "https://evil.example", "content-type": "application/json"},
        )
        assert r.status_code == 403


async def test_SECURITY_18_signed_out_page_holds_the_form(auth_file: Path) -> None:
    async with client(config(auth_file)) as c:
        page = await c.get("/", headers={"accept": "text/html"})
        assert page.status_code == 401
        assert 'id="sign-in"' in page.text
        assert '<script src="/auth/sign-in.js">' in page.text
        script = await c.get("/auth/sign-in.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
    async with client(config()) as c:
        page = await c.get("/", headers={"accept": "text/html"})
        assert 'id="sign-in"' not in page.text


def test_SECURITY_18_outdated_hashes_are_rehashed(tmp_path: Path) -> None:
    from argon2 import PasswordHasher

    path = tmp_path / "auth.json"
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    path.write_text(json.dumps({"user": "admin", "hash": weak}))
    assert PasswordCheck(path).verify("admin", PASSWORD)
    login = read_login(path)
    assert login is not None
    assert login.hash != weak
    assert PasswordCheck(path).verify("admin", PASSWORD)


# -- SECURITY-19: throttling -------------------------------------------------------------


def test_SECURITY_19_per_address_and_global_limits() -> None:
    clock = Clock()
    t = SignInThrottle(clock=clock)
    for _ in range(4):
        t.failed("10.0.0.1")
    assert t.retry_after("10.0.0.1") is None
    t.failed("10.0.0.1")
    wait = t.retry_after("10.0.0.1")
    assert wait is not None
    assert wait == pytest.approx(900)
    assert t.retry_after("10.0.0.2") is None
    clock.now += 901
    assert t.retry_after("10.0.0.1") is None
    for i in range(20):
        t.failed(f"10.1.0.{i % 4}")  # 4 addresses, 5 each: also the global limit
    assert t.retry_after("10.9.9.9") is not None
    clock.now += 901
    assert t.retry_after("10.9.9.9") is None


async def test_SECURITY_19_blocked_address_gets_429_and_codes_still_work(
    auth_file: Path,
) -> None:
    cfg = config(auth_file)
    async with client(cfg) as c:
        for _ in range(5):
            assert (await sign_in(c, password="wrong password here")).status_code == 401
        r = await sign_in(c)  # even the right password waits
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "too_many_attempts"
        assert int(r.headers["retry-after"]) > 0
        code = cfg.sessions.sign_in_code()
        assert (await c.get(f"/auth/exchange?token={code}")).status_code == 303


# -- SECURITY-20: https, and binding ---------------------------------------------------


async def test_SECURITY_20_plain_http_from_the_network_is_refused(auth_file: Path) -> None:
    proxy = "rifferr.home.lan"
    cfg = config(auth_file, extra_hosts=(proxy,))
    async with client(cfg, base=f"http://{proxy}", address="192.168.1.20") as c:
        r = await sign_in(c, origin=f"http://{proxy}")
        assert (r.status_code, r.json()["error"]["code"]) == (403, "https_required")
        r = await sign_in(c, origin=f"https://{proxy}")  # through an https proxy
        assert r.status_code == 204
        assert "Secure" in r.headers["set-cookie"]
    async with client(cfg, address="192.168.1.20") as c:  # loopback name, foreign client
        r = await sign_in(c)
        assert r.status_code == 403
    async with client(cfg, base=f"https://{proxy}", address="192.168.1.20") as c:
        r = await sign_in(c, origin=f"https://{proxy}")  # served with TLS
        assert r.status_code == 204


def test_SECURITY_20_a_login_lifts_the_bind_refusal() -> None:
    check_bind("0.0.0.0", real_auth=True)


def test_SECURITY_20_serve_refuses_no_auth_beyond_loopback(tmp_path: Path) -> None:
    r = _cli(tmp_path, "serve", "--host", "0.0.0.0", "--no-auth", "--no-browser")
    assert r.returncode == 2


# -- SECURITY-21: one-time codes stay --------------------------------------------------


async def test_SECURITY_21_codes_work_with_a_login(auth_file: Path) -> None:
    cfg = config(auth_file)
    async with client(cfg) as c:
        r = await c.get(f"/auth/exchange?token={cfg.sessions.sign_in_code()}")
        assert r.status_code == 303
        row = cfg.sessions.lookup(c.cookies[COOKIE])
        assert row is not None
        assert row.started_by == "code"
