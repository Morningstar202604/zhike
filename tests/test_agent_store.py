import pytest

from core import store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "agent.db"))
    store._SCHEMA_READY.clear()
    store.init_db()


def test_agent_store_run_and_step_roundtrip():
    from core import agent_store

    run = agent_store.agent_run_create(
        "run-1", "session-1", "admin", "做一件事", 2, {"max_steps": 3}
    )
    assert run["status"] == "running"
    assert run["budget_json"] == {"max_steps": 3}

    step_id = agent_store.agent_step_add(
        "run-1", 1, "先思考", "calc", {"expression": "1+1"}, "2", "done", 12
    )
    assert step_id > 0
    agent_store.agent_run_update("run-1", status="done", usage_json={"steps": 1}, result={"answer": "2"})

    run = agent_store.agent_run_get("run-1")
    steps = agent_store.agent_steps_list("run-1")
    assert run["status"] == "done"
    assert run["usage_json"] == {"steps": 1}
    assert run["result"] == {"answer": "2"}
    assert steps[0]["params"] == {"expression": "1+1"}
    assert steps[0]["observation"] == "2"


def test_agent_store_bounds_and_filtered_listing():
    from core import agent_store

    agent_store.agent_run_create("run-a", None, "admin", "a", 1, {})
    agent_store.agent_run_create("run-b", None, "admin", "b", 1, {})
    agent_store.agent_run_update("run-a", status="done")
    agent_store.agent_step_add("run-b", 2, "t" * 5000, "x", None, "o" * 9000, "failed")

    steps = agent_store.agent_steps_list("run-b")
    runs = agent_store.agent_runs_list(status="done", limit=10)
    assert len(runs) == 1 and runs[0]["id"] == "run-a"
    assert len(steps[0]["thought"]) == 4000
    assert len(steps[0]["observation"]) == 8000


def test_store_agent_api_remains_compatible():
    from core import agent_store

    agent_store.agent_run_create("run-compat", None, "admin", "compat", 1, {})
    assert store.agent_run_get("run-compat") == agent_store.agent_run_get("run-compat")
    assert store.agent_runs_list() == agent_store.agent_runs_list()
