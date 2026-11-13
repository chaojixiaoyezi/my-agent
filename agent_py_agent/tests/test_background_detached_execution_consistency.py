"""后台身份和任务详情沿同一有限任务页核验detached执行。"""

import json

import pytest

from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
from agent_py_agent.tests.test_business_task_execution_view import (
    ClaimExecutionBinding,
    background_call,
    bind_claim_execution,
    observed_native_task,
    publish_claim_runtime_authority,
    seed_observed_runtime,
)
from agent_py_agent.tests.test_business_task_execution_view import (
    background_http as background_http,
)


def _detached(background_http, services=None, task=None):
    if services is None:
        services, task = observed_native_task(background_http)
    _runtime, run = seed_observed_runtime(services, task)
    store = services.host.conversation_store
    request_id = "observed-" + task.task_id
    store.session_tasks.bind_turn(task.task_id, turn_id=request_id)
    store.session_tasks.bind_execution(
        task.task_id, task_run_id=run["task_run_id"], target_thread_id=task.target_thread_id
    )
    store.tasks.bind(
        {
            "thread_id": task.target_thread_id,
            "task_id": task.task_id,
            "cancellation_scope": "detached",
        }
    )
    scope = detached_task_claim_scope_id(task.target_thread_id, task.task_id)
    claim = store.claims.acquire(
        {"thread_id": task.target_thread_id, "reason": "session_task", "claim_scope_id": scope}
    )
    with bind_claim_execution(
        ClaimExecutionBinding(store.claims, task.target_thread_id, claim["claim_id"], scope)
    ):
        publish_claim_runtime_authority(
            {"invocation_run_id": request_id, "run_id": request_id, **run}
        )
    return services, task, _runtime, run, scope


def test_detached_claim_is_visible_in_task_agent_and_list(background_http):
    services, task, _runtime, _run, _scope = _detached(background_http)
    status, detail = background_call(background_http, "tasks.get", {"task_id": task.task_id})
    assert status == 200 and detail["data"]["scheduling"]["state"] == "running"
    status, agent = background_call(
        background_http, "background_agents.get", {"agent_id": task.target_thread_id}
    )
    assert (
        status == 200
        and agent["data"]["current_execution"]["run_id"] == detail["data"]["task"]["current_run_id"]
    )
    assert agent["data"]["scheduling"]["state"] == agent["data"]["agent"]["work_state"] == "running"
    status, listed = background_call(background_http, "background_agents.list", {"limit": 10})
    assert status == 200
    [row] = [row for row in listed["data"]["items"] if row["agent_id"] == task.target_thread_id]
    assert row["work_state"] == "running"


@pytest.mark.parametrize("fault", ["claim", "attempt"])
def test_unreadable_or_stale_detached_execution_remains_unknown(background_http, fault):
    services, task, runtime, run, scope = _detached(background_http)
    store = services.host.conversation_store
    path = store.storage.background_claim_path(scope)
    if fault == "claim":
        value = json.loads(path.read_text())
        value["status"] = []
        path.write_text(json.dumps(value))
    else:
        runtime.create_attempt(run["agent_run_id"])
    before = path.read_bytes()
    status, result = background_call(
        background_http, "background_agents.get", {"agent_id": task.target_thread_id}
    )
    assert status == 200 and result["data"]["current_execution"] is None
    assert (
        result["data"]["agent"]["work_state"] == result["data"]["scheduling"]["state"] == "unknown"
    )
    assert path.read_bytes() == before


def test_multiple_detached_executions_are_unknown_not_arbitrarily_selected(background_http):
    services, first, _runtime, _run, _scope = _detached(background_http)
    status, created = background_call(
        background_http,
        "tasks.assign",
        {
            "agent_id": first.target_thread_id,
            "space_id": first.metadata["business_context"]["space_id"],
            "instruction": "second concurrent task",
        },
        idempotency_key="detached-second",
    )
    assert status == 200
    second = services.host.conversation_store.session_tasks.require(created["data"]["task_id"])
    _detached(background_http, services, second)
    status, result = background_call(
        background_http, "background_agents.get", {"agent_id": first.target_thread_id}
    )
    assert status == 200 and result["data"]["current_execution"] is None
    assert result["data"]["scheduling"]["state"] == "unknown"
    assert result["data"]["agent"]["work_state"] == "running"
    assert result["data"]["scheduling"]["reason_code"] == "MULTIPLE_ACTIVE_EXECUTIONS"


def test_known_current_execution_survives_more_than_one_history_page(background_http, monkeypatch):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
    from agent_py_agent.tests.test_business_task_execution_view import active_task

    _client, services, task, _runtime, _run = active_task(background_http)
    store = services.host.conversation_store
    for number in range(105):
        old = store.session_tasks.create(
            SessionTaskDraft(
                task.sender_thread_id,
                task.target_thread_id,
                "historical task",
                dedupe_key=f"history-{number}",
            ),
            now=1 + number,
        )
        store.session_tasks.advance(
            old.task_id, SessionTaskUpdate(status="accepted"), now=2 + number
        )
        store.session_tasks.advance(old.task_id, SessionTaskUpdate(status="done"), now=3 + number)

    def forbidden(*args, **kwargs):
        raise AssertionError("不能为消除unknown全扫历史或claim目录")

    monkeypatch.setattr(store.session_tasks, "list_report", forbidden)
    status, result = background_call(
        background_http, "background_agents.get", {"agent_id": task.target_thread_id}
    )
    assert status == 200, result
    assert result["data"]["current_execution"]["run_id"] == "observed-" + task.task_id
    assert (
        result["data"]["agent"]["work_state"] == result["data"]["scheduling"]["state"] == "running"
    )
    assert result["data"]["scheduling"]["coverage"] == "partial"
    assert result["data"]["scheduling"]["reason_code"] == "EXECUTION_SOURCE_INCOMPLETE"
