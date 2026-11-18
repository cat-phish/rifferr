"""S-SUBWORKFLOW through the API: stored called files, trust per file, safety, run hashes."""

from __future__ import annotations

from rifferr.trust import workflow_hash
from tests.api.conftest import Api, wf

CALLED = wf(
    "{id: say, uses: test.echo, with: {message: called}}",
    "{id: fetch, uses: shell, with: {run: [curl, 'https://example.invalid']}}",
)
CALLER = wf(
    "{id: first, uses: test.echo, with: {message: first}}",
    "{id: sub, uses: workflow, with: {workflow: called.yaml}}",
)
Q = {"profile": "main"}


def setup(api: Api) -> None:
    api.add_workflow("caller", CALLER)
    api.add_workflow("called", CALLED)


async def test_SUBWORKFLOW_13_a_run_is_trusted_only_with_every_called_file(api: Api) -> None:
    setup(api)
    api.state.db.trust(workflow_hash(CALLER), "caller.yaml", "test")
    detail = (await api.client.get("/api/workflows/caller", params=Q)).json()
    assert detail["trusted"] is False
    assert [(c["path"], c["trusted"], c["yaml"]) for c in detail["called"]] == [
        ("called.yaml", False, CALLED)
    ]
    run_id = await api.start("caller")
    assert (await api.client.get(f"/api/runs/{run_id}")).json()["trusted"] is False
    await api.wait_finished(run_id)


async def test_SUBWORKFLOW_13_trust_lists_called_files_by_their_own_hash(api: Api) -> None:
    setup(api)
    sha, called_sha = workflow_hash(CALLER), workflow_hash(CALLED)
    url = "/api/workflows/caller/trust"
    r = await api.client.post(url, params=Q, json={"sha256": sha})
    assert r.status_code == 409
    assert "called workflow 'called.yaml'" in r.json()["error"]["message"]
    assert not api.state.db.is_trusted(sha)
    wrong = {"sha256": sha, "called": {"called.yaml": "0" * 64}}
    assert (await api.client.post(url, params=Q, json=wrong)).status_code == 409
    body = {"sha256": sha, "called": {"called.yaml": called_sha}}
    r = await api.client.post(url, params=Q, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["called"] == {"called.yaml": called_sha}
    assert api.state.db.is_trusted(sha)
    assert api.state.db.is_trusted(called_sha)
    # Trusting the caller trusted only what was listed: the called file on its own.
    assert (await api.client.get("/api/workflows/caller", params=Q)).json()["trusted"] is True
    run_id = await api.start("caller")
    assert (await api.wait_finished(run_id))["trusted"] is True


async def test_SUBWORKFLOW_14_safety_report_labels_called_findings(api: Api) -> None:
    setup(api)
    r = await api.client.post("/api/workflows/caller/validate", params=Q, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True, body
    called = [f for f in body["safety"] if f["workflow"] == "called.yaml"]
    assert called
    assert {f["step_id"] for f in called} == {"sub/fetch"}
    assert any(f["kind"] == "network_tool" for f in called)


async def test_SUBWORKFLOW_15_run_records_called_file_hashes(api: Api) -> None:
    setup(api)
    run_id = await api.start("caller")
    body = await api.wait_finished(run_id)
    assert body["called_hashes"] == {"called.yaml": workflow_hash(CALLED)}
    assert body["workflow_hash"] == workflow_hash(CALLER)
    steps = {s["id"]: s for s in body["steps"]}
    assert list(steps) == ["first", "sub/say", "sub/fetch"]
    assert (steps["sub/say"]["call_path"], steps["sub/say"]["workflow"]) == (
        ["sub"],
        "called.yaml",
    )


async def test_SUBWORKFLOW_12_the_job_stores_called_texts(api: Api) -> None:
    setup(api)
    run_id = await api.start("caller")
    job = api.state.db.job_for_run(run_id)
    assert job is not None
    assert job.payload["called"] == [
        {"path": "called.yaml", "source": CALLED, "sha256": workflow_hash(CALLED)}
    ]
    await api.wait_finished(run_id)


async def test_SUBWORKFLOW_1_run_start_refuses_a_missing_called_file(api: Api) -> None:
    api.add_workflow("caller", CALLER)
    r = await api.client.post("/api/runs", json={"profile": "main", "workflow": "caller"})
    assert r.status_code == 422
    assert "not found in the profile's workflows/ folder" in r.text
