"""API-18 script files and the TAGSCRIPT-20/-21 preview endpoint."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import Api

EP = """\
# release types
actions:
  - name: ep-suffix
    when: 'albumtype:ep'
    set: { album: '$album (EP)' }
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def test_API_18_save_list_and_read(api: Api) -> None:
    r = await api.client.get("/api/profiles/main/scripts")
    assert (r.status_code, r.json()) == (200, [])
    r = await api.client.put("/api/profiles/main/scripts/ep", json={"text": EP})
    assert r.status_code == 200, r.text
    assert r.json()["sha256"] == sha(EP)
    assert (profile_dir(config_root(), "main") / "scripts" / "ep.yaml").read_text() == EP
    await api.client.put("/api/profiles/main/scripts/bad", json={"text": "actions:\n  - x: 1\n"})
    listed = {s["name"]: s for s in (await api.client.get("/api/profiles/main/scripts")).json()}
    assert listed["ep"]["valid"] is True
    assert listed["bad"]["valid"] is False
    assert listed["bad"]["errors"][0]["line"] == 2
    d = (await api.client.get("/api/profiles/main/scripts/ep")).json()
    assert d["text"] == EP  # comments survive: written as typed


async def test_API_18_hash_mismatch(api: Api) -> None:
    await api.client.put("/api/profiles/main/scripts/ep", json={"text": EP})
    loaded = sha(EP)
    (profile_dir(config_root(), "main") / "scripts" / "ep.yaml").write_text(EP + "# edited\n")
    r = await api.client.put(
        "/api/profiles/main/scripts/ep", json={"text": EP + "# mine\n", "sha256": loaded}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "hash_mismatch")
    current = sha(EP + "# edited\n")
    r = await api.client.put(
        "/api/profiles/main/scripts/ep", json={"text": EP + "# mine\n", "sha256": current}
    )
    assert r.status_code == 200


@pytest.mark.parametrize("name", ["..x", ".hidden", "a\\b"])
async def test_API_18_names_stay_inside_scripts(api: Api, name: str) -> None:
    r = await api.client.put(f"/api/profiles/main/scripts/{name}", json={"text": EP})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_script_name")


async def test_API_18_missing_and_ambiguous(api: Api) -> None:
    r = await api.client.get("/api/profiles/main/scripts/nope")
    assert r.status_code == 404
    folder = profile_dir(config_root(), "main") / "scripts"
    folder.mkdir(exist_ok=True)
    (folder / "dup.yaml").write_text(EP)
    (folder / "dup.yml").write_text(EP)
    r = await api.client.get("/api/profiles/main/scripts/dup")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "ambiguous_script_name")
    r = await api.client.get("/api/profiles/nobody/scripts")
    assert r.status_code == 404


async def test_API_18_saving_needs_json(api: Api) -> None:
    r = await api.client.put(
        "/api/profiles/main/scripts/ep", content=EP, headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415


async def test_TAGSCRIPT_20_preview_endpoint(api: Api) -> None:
    r = await api.client.post(
        "/api/profiles/main/tagscript/preview", json={"script_text": EP, "limit": 2}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["albums"] == 2
    assert body["truncated"] is True
    changed = [t for t in body["targets"] if t["fields"]]
    assert changed[0]["fields"][0]["after"] == "Album 0 (EP)"
    assert body["idempotence"][0]["action"] == "ep-suffix"


async def test_TAGSCRIPT_20_preview_of_a_script_file(api: Api) -> None:
    await api.client.put("/api/profiles/main/scripts/ep", json={"text": EP})
    r = await api.client.post(
        "/api/profiles/main/tagscript/preview", json={"script": "ep.yaml", "query": "album:0"}
    )
    assert r.json()["albums"] == 1
    r = await api.client.post("/api/profiles/main/tagscript/preview", json={"script": "no.yaml"})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "script_not_found")


async def test_TAGSCRIPT_20_preview_validation_errors(api: Api) -> None:
    r = await api.client.post(
        "/api/profiles/main/tagscript/preview", json={"script_text": "actions:\n  - {}\n"}
    )
    assert r.status_code == 200
    (error,) = r.json()["validation_errors"]
    assert (error["line"], error["column"]) == (2, 5)
    r = await api.client.post("/api/profiles/main/tagscript/preview", json={"limit": 501})
    assert r.status_code == 400


async def test_TAGSCRIPT_21_preview_answers_while_a_run_is_active(api: Api) -> None:
    from tests.api.conftest import SLOW

    api.add_workflow("slow", SLOW)
    run_id = await api.start("slow")
    r = await api.client.post("/api/profiles/main/tagscript/preview", json={"script_text": EP})
    assert r.status_code == 200
    state = (await api.client.get(f"/api/runs/{run_id}")).json()["state"]
    assert state in ("queued", "running")
    await api.client.post(f"/api/runs/{run_id}/cancel")


async def test_TAGSCRIPT_21_timeout_and_superseded(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rifferr.api.routes.scripts as routes

    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "0.3")
    monkeypatch.setattr(routes, "TIMEOUT", 0.5)
    r = await api.client.post("/api/profiles/main/tagscript/preview", json={"script_text": EP})
    assert (r.status_code, r.json()["error"]["code"]) == (504, "preview_timeout")
    monkeypatch.setattr(routes, "TIMEOUT", 30)
    url = "/api/profiles/main/tagscript/preview"
    first = asyncio.create_task(api.client.post(url, json={"script_text": EP, "client": "t"}))
    await asyncio.sleep(0.3)
    second = await api.client.post(
        url, json={"script_text": "actions:\n  - delete: [x]\n", "client": "t"}
    )
    assert second.status_code == 200
    old = await first
    assert (old.status_code, old.json()["error"]["code"]) == (409, "superseded")


async def test_READ_ORDER_1_3_preview_superseded_by_seq_not_arrival(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "0.3")
    url = "/api/profiles/main/tagscript/preview"
    newest = asyncio.create_task(
        api.client.post(url, json={"script_text": EP, "client": "t", "seq": 2})
    )
    await asyncio.sleep(0.3)
    late = await api.client.post(
        url, json={"script_text": "actions:\n  - delete: [x]\n", "client": "t", "seq": 1}
    )
    assert (late.status_code, late.json()["error"]["code"]) == (409, "superseded")
    assert not newest.done()
    assert (await newest).status_code == 200
    r = await api.client.post(url, json={"script_text": EP, "seq": -1})
    assert r.status_code == 400
