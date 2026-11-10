"""ORCH-02：直属下级未决能力申请的只读收件箱。

沿用 ORCH-01 的真实 SimpleAgent(echo)、SubAgentManager/runtime.db、ConversationStore 与定时派发链：
申请写在真实 SubAgentTask.capability_requests，授权/拒绝走真实 resolve_capability_requests，取消走真实 cancel_subagents。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_py_agent.agent.agent_core.orchestration.pending_inbox import (
    MAX_PAGE_ITEMS_COUNT,
    pending_inbox_page,
)
from agent_py_agent.agent.agent_core.orchestration.tools.cancel import CancelSubagentsTool
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.pending_requests import (
    ListPendingRequestsTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
from agent_py_agent.agent.subagents.role_templates import DIRECT_CHILD_CONTROL_TOOLS
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import _leaf_write_tools
from agent_py_agent.tests import test_orch01_management_continuity as orch01
from agent_py_agent.tests.test_orch01_management_continuity import (
    _agent,
    _bound,
    _foreground,
    _open_request,
    _start_scheduled_run,
    _thread,
)


# 与 ORCH-01 同一真实夹具：线程 long-work、父任务 task-T、直属子代理和指向 T 的定时作业。
@pytest.fixture
def case(tmp_path) -> orch01.Case:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = orch01.AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_subagents=True)
    agent = _agent(config, workspace)
    thread = _thread(agent, "long-work")
    parent = agent.conversation_store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-T", "goal": "长期工作", "status": "active"}
    ).task_id
    child = agent.subagents.create_run(goal="子任务", thought="", plan=["做"], parent_id=parent, root_id=parent, depth=1)
    job = agent.scheduler_repository.create_job(orch01.SchedulerJobCreateRequest(
        name="定时检查", prompt="检查直属下级进展", thread_id=thread.thread_id, source_task_id=parent,
        schedule={"kind": "every", "every_seconds": 600, "timezone": "UTC", "anchor_at": 1000},
        creator={"local": True}, now=1000,
    ))[0]
    return orch01.Case(agent, config, workspace, thread, parent, child.id, job)


def _as_parent(case) -> None:
    _foreground(case.agent, case.thread, case.parent_task_id)


def _list(agent, **params) -> tuple[object, dict]:
    result = ListPendingRequestsTool(agent).execute(params)
    return result, json.loads(result.output)


def _add_requests(agent, run_id: str, ids: list[str], *, status: str = "OPEN") -> None:
    task = agent.subagents.load(run_id)
    task.capability_requests = [*task.capability_requests, *[CapabilityRequest(
        id=request_id, from_run_id=run_id, problem="缺权限", needed_capability="x", status=status,
        requested_tools=["run_command"], created_at=time.time()) for request_id in ids]]
    agent.subagents.save(task)


def _new_child(case, goal: str = "子任务") -> str:
    return case.agent.subagents.create_run(goal=goal, thought="", plan=["做"], parent_id=case.parent_task_id,
                                           root_id=case.parent_task_id, depth=1).id


def _config(monkeypatch, **values) -> None:
    from agent_py_agent.agent.capability import runtime_config_reload

    monkeypatch.setattr(runtime_config_reload, "capability_config_for_agent", lambda _agent: CapabilityConfig(**values))


def _walk(agent, limit: int, **params) -> tuple[list[str], int]:
    seen, cursor, pages = [], "", 0
    while True:
        result, payload = _list(agent, limit=limit, cursor=cursor, **params)
        assert result.ok, result.output
        assert len(payload["items"]) <= limit and payload["coverage"]["children_scanned"] <= limit
        seen.extend(item["item_id"] for item in payload["items"])
        pages += 1
        # 页数有界：游标不前进时这里直接失败，而不是无限翻页。
        assert pages <= 100, "cursor did not advance"
        cursor = payload["next_cursor"]
        if not cursor:
            assert payload["coverage"]["state"] == "complete"
            return seen, pages
        assert payload["coverage"]["state"] == "partial" and payload["coverage"]["truncated"]


def _child_file_state(case, run_id: str) -> bytes:
    return (case.agent.subagents.persistence.workspace / run_id / "task.json").read_bytes()


def test_parent_sees_open_request_with_handler_scope_and_wait_and_nothing_changes(case):
    _open_request(case, "req-1")
    _as_parent(case)
    before = _child_file_state(case, case.child_id)
    wakes = len(case.agent.conversation_store.wakes.pending(limit=1000))
    result, payload = _list(case.agent)

    assert result.ok and payload["schema_version"] == "pending_inbox.v1" and payload["effect"] == "read_only"
    [item] = payload["items"]
    assert item["item_id"] == f"cap:{case.child_id}:req-1" and item["request_state"] == "OPEN"
    assert item["handling_parent"] == {"run_id": case.parent_task_id, "principal_kind": "self",
                                       "resolver_tool": "resolve_capability_requests"}
    assert item["scope"]["requested_tools"] == ["run_command"]
    assert item["wait"]["waiting"] is True and item["wait"]["age_seconds"] >= 0
    assert item["decision"]["state"] == "none" and item["execution"] == {"state": "not_applicable"}
    assert payload["coverage"]["state"] == "complete" and payload["next_cursor"] is None
    assert payload["coverage"]["excluded_item_kinds"] == ["tool_approval"]
    assert _child_file_state(case, case.child_id) == before
    assert len(case.agent.conversation_store.wakes.pending(limit=1000)) == wakes
    assert case.agent.conversation_store.guidance.pending("agent_run", case.child_id) == []


def test_scheduled_followup_sees_the_parents_children_and_unrelated_actors_see_nothing(case, tmp_path):
    _open_request(case, "req-1")
    _start_scheduled_run(case)
    with _bound(case):
        _, scheduled = _list(case.agent)
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    _, unrelated = _list(case.agent)
    other = _agent(type(case.config)(model_backend="echo", my_agent_home=str(tmp_path / "other-home"),
                                     enable_subagents=True), case.workspace)
    _foreground(other, _thread(other, "long-work"), case.parent_task_id)
    _, foreign = _list(other)

    assert [item["handling_parent"]["principal_kind"] for item in scheduled["items"]] == ["scheduled_continuation"]
    assert scheduled["items"][0]["handling_parent"]["run_id"] == case.parent_task_id
    assert unrelated["items"] == [] and foreign["items"] == []
    # 看见不等于可裁决：无关线程照样被原授权门拒绝。
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    denied = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "decision": "grant", "reason": "越权"})
    assert denied.ok is False


def test_decisions_come_only_from_recorded_grant_and_deny_evidence(case):
    _open_request(case, "req-grant", directory_only=True)
    _open_request(case, "req-deny")
    _as_parent(case)
    grant = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "request_id": "req-grant", "decision": "grant", "reason": "允许"})
    deny = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "request_id": "req-deny", "decision": "deny", "reason": "不允许"})
    assert grant.ok and deny.ok, (grant.output, deny.output)
    _, pending_only = _list(case.agent)
    _, payload = _list(case.agent, include_resolved=True)
    items = {item["request_id"]: item for item in payload["items"]}
    grant_record = case.agent.subagents.load(case.child_id).capability_grants[0]

    assert pending_only["items"] == []
    assert items["req-grant"]["decision"]["state"] == "granted"
    assert items["req-grant"]["decision"]["grant_id"] == grant_record.id
    assert items["req-grant"]["decision"]["decided_at"] == grant_record.created_at
    assert items["req-grant"]["decision"]["auto_granted"] is False
    assert items["req-deny"]["decision"] == {"state": "denied", "evidence": "capability_resolution_continuation",
                                             "decided_at": None}
    # 这里只看到排队记录，没有执行轮回指它，所以不能写成已续跑。
    assert items["req-deny"]["execution"]["state"] == "queued"
    assert items["req-grant"]["execution"]["state"] == "not_recorded"


def test_closed_by_cancel_is_not_reported_as_a_parent_denial(case):
    _open_request(case, "req-1")
    _as_parent(case)
    cancelled = CancelSubagentsTool(case.agent).execute({"run_id": case.child_id, "reason": "用户停止"})
    assert cancelled.ok, cancelled.output
    _, payload = _list(case.agent, include_resolved=True)
    [item] = payload["items"]

    assert item["request_state"] == "CLOSED"
    assert item["decision"] == {"state": "closed_unattributed", "evidence": "none", "decided_at": None}
    assert item["execution"] == {"state": "not_recorded"}


def test_gap_invalid_and_granted_without_record_are_reported_honestly(case):
    _add_requests(case.agent, case.child_id, ["req-gap"], status="GAP")
    _add_requests(case.agent, case.child_id, ["req-old"], status="PENDING_LEGACY")
    _add_requests(case.agent, case.child_id, ["req-bare"], status="GRANTED")
    _as_parent(case)
    _, pending_only = _list(case.agent)
    _, payload = _list(case.agent, include_resolved=True)
    items = {item["request_id"]: item for item in payload["items"]}

    assert [item["request_id"] for item in pending_only["items"]] == ["req-old"]
    assert items["req-old"]["request_state"] == "INVALID" and items["req-old"]["wait"]["waiting"] is True
    assert items["req-gap"]["decision"]["state"] == "gap" and items["req-gap"]["wait"]["waiting"] is False
    assert items["req-bare"]["decision"] == {"state": "granted_unrecorded", "evidence": "none", "decided_at": None}
    assert payload["page_counts"] == {"gap": 1, "granted": 1, "invalid": 1}


def test_a_later_unrelated_attempt_is_unknown_not_resumed(case):
    _open_request(case, "req-deny")
    _as_parent(case)
    deny = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "request_id": "req-deny", "decision": "deny", "reason": "不允许"})
    assert deny.ok, deny.output
    time.sleep(0.01)
    # 排队之后另起一轮与本次裁决无关的执行；它只是时间更晚，没有回指续跑记录。
    repo = case.agent.subagents.runtime_db
    agent_run_id = repo.agent_run_for_run_id(case.child_id)["agent_run_id"]
    attempt = repo.create_attempt(agent_run_id)
    _, payload = _list(case.agent, include_resolved=True)
    execution = payload["items"][0]["execution"]

    assert execution["state"] == "unknown" and execution["reason"] == "NO_CAUSAL_LINK"
    assert execution["attempt_id"] == attempt["attempt_id"]
    assert all(item["execution"]["state"] != "resumed" for item in payload["items"])


def test_unreadable_runtime_and_child_records_make_coverage_partial(case, monkeypatch):
    _open_request(case, "req-1")
    _as_parent(case)
    deny = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "request_id": "req-1", "decision": "deny", "reason": "不允许"})
    assert deny.ok, deny.output
    broken = _new_child(case, "坏记录")
    _add_requests(case.agent, broken, ["req-x"])
    task_file = case.agent.subagents.persistence.workspace / broken / "task.json"
    task_file.write_text("{not json", encoding="utf-8")

    def unreadable(_run_id):
        raise OSError("runtime.db unreadable")

    monkeypatch.setattr(case.agent.subagents.runtime_db, "current_attempt", unreadable)
    _, payload = _list(case.agent, include_resolved=True)
    coverage = payload["coverage"]

    assert coverage["state"] == "partial"
    assert [error["run_id"] for error in coverage["child_load_errors"]] == [broken]
    assert coverage["runtime_reads_failed"] == 1
    assert payload["items"][0]["execution"]["reason"] == "RUNTIME_UNREADABLE"


def test_unreadable_run_index_is_partial_not_empty(case, monkeypatch):
    _open_request(case, "req-1")
    _as_parent(case)
    from agent_py_agent.agent.subagents.services.persistence.run_index import (
        SubagentRunIndexUnavailable,
    )

    def incomplete(_db):
        raise SubagentRunIndexUnavailable("subagent run index coverage is partial")

    monkeypatch.setattr(case.agent.subagents.persistence.run_index, "_require_complete", incomplete)
    _, payload = _list(case.agent)

    assert payload["items"] == [] and payload["coverage"]["state"] == "partial"
    assert payload["coverage"]["index_errors"] == [
        {"parent_run_id": case.parent_task_id, "error_type": "SubagentRunIndexUnavailable"}]


def test_pagination_walks_every_child_and_every_request_exactly_once(case):
    children = [case.child_id, *[_new_child(case, f"子{index}") for index in range(5)]]
    quiet = _new_child(case, "没有申请")
    for index, run_id in enumerate(children):
        _add_requests(case.agent, run_id, [f"req-{index}"])
    _add_requests(case.agent, children[2], [f"req-many-{n}" for n in range(5)])
    _as_parent(case)
    expected = sorted(f"cap:{run_id}:{request.id}" for run_id in children
                      for request in case.agent.subagents.load(run_id).capability_requests)

    for limit in (1, 2, 3, 50):
        seen, pages = _walk(case.agent, limit)
        assert sorted(seen) == expected and len(seen) == len(set(seen)), limit
        if limit == 1:
            assert pages > len(expected)
    assert all(not item.endswith(quiet) for item in expected)


def test_cursor_is_bound_to_owner_and_principals_and_bad_arguments_fail_closed(case):
    for index in range(3):
        _add_requests(case.agent, case.child_id, [f"req-{index}"])
    _as_parent(case)
    _, first = _list(case.agent, limit=1)
    cursor = first["next_cursor"]
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    mismatch, mismatch_payload = _list(case.agent, cursor=cursor)
    _as_parent(case)
    malformed, _ = _list(case.agent, cursor="not-a-cursor")
    too_big, _ = _list(case.agent, limit=MAX_PAGE_ITEMS_COUNT + 1)
    wrong_type, _ = _list(case.agent, include_resolved="yes")

    assert cursor and mismatch.ok is False and mismatch.error_code == "TOOL_PERMISSION_DENIED"
    assert mismatch_payload == {"error": {"reason": "CURSOR_SCOPE_MISMATCH",
                                          "message": mismatch_payload["error"]["message"]}}
    assert mismatch.effect_outcome == "not_started"
    for result in (malformed, too_big, wrong_type):
        assert result.ok is False and result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_page_limit_defaults_follow_config_and_zero_means_hard_ceiling(case, monkeypatch):
    for index in range(3):
        _add_requests(case.agent, case.child_id, [f"req-{index}"])
    _as_parent(case)
    _config(monkeypatch, orchestration_pending_inbox_page_limit=2)
    _, configured = _list(case.agent)
    _config(monkeypatch, orchestration_pending_inbox_page_limit=0)
    _, unlimited = _list(case.agent)

    assert len(configured["items"]) == 2 and configured["coverage"]["page_limit"] == 2
    assert len(unlimited["items"]) == 3 and unlimited["coverage"]["page_limit"] == MAX_PAGE_ITEMS_COUNT


def test_switch_controls_availability_and_leaf_roles_never_get_the_tool(case, monkeypatch):
    tool = case.agent.tools.tools["list_pending_requests"]
    assert tool.availability().available is True
    _config(monkeypatch, orchestration_pending_inbox_enabled=False)
    disabled = tool.availability()
    visible = {spec.name for spec in case.agent.tools.model_visible_specs(loaded_tool_names={"list_pending_requests"})}

    assert disabled.available is False and "list_pending_requests" not in visible
    assert "list_pending_requests" in DIRECT_CHILD_CONTROL_TOOLS
    assert "list_pending_requests" not in _leaf_write_tools(
        ["read_file", "list_pending_requests"], parent_tools=["read_file", "list_pending_requests"])


def test_concurrent_grants_settle_once_and_the_inbox_shows_the_single_grant(case):
    _open_request(case, "req-1", directory_only=True)
    _as_parent(case)
    barrier = threading.Barrier(2)
    results = []

    def grant() -> None:
        barrier.wait()
        results.append(ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "request_id": "req-1", "decision": "grant", "reason": "允许"}))

    threads = [threading.Thread(target=grant) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    grants = case.agent.subagents.load(case.child_id).capability_grants
    _, payload = _list(case.agent, include_resolved=True)

    assert len(results) == 2 and len(grants) == 1
    assert payload["items"][0]["decision"]["grant_id"] == grants[0].id


@pytest.mark.parametrize("limit", [0])
def test_zero_limit_argument_uses_configured_default(case, limit):
    _open_request(case, "req-1")
    _as_parent(case)
    result, payload = _list(case.agent, limit=limit)

    assert result.ok and payload["coverage"]["page_limit"] == 50 and len(payload["items"]) == 1
