"""ORCH-02：直属下级未决能力申请的只读收件箱。

沿用 ORCH-01 的真实 SimpleAgent(echo)、SubAgentManager/runtime.db、ConversationStore 与定时派发链：
申请写在真实 SubAgentTask.capability_requests，授权/拒绝走真实 resolve_capability_requests，取消走真实 cancel_subagents。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration.pending_inbox import (
    MAX_EXAMINED_REQUESTS_COUNT,
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
        assert payload["coverage"]["requests_examined"] <= MAX_EXAMINED_REQUESTS_COUNT
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
    # 机器层只读顶层 error_code；reason 只在结构化 details 里给诊断展示。
    assert mismatch.result_envelope["details"]["error"]["reason"] == "CURSOR_SCOPE_MISMATCH"
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


# ---------- B1 开关生命周期（PRO-MSG-016）：取代 4bd54f9cf 的“启动时不注册”观察，原候选与探针留作历史 ----------
# 工具常驻注册是实现细节；公开合同是：关着时不出现在任何模型可见面（快照、规格、目录、搜索），任何调用都不执行，
# 同一个 agent 上 true→false / false→true 下一轮就生效，按 owner 隔离，配置读不出或非布尔按关闭。


def _write_switch(agent, value: str) -> None:
    path = Path(agent.capability_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"orchestration_pending_inbox_enabled: {value}\n", encoding="utf-8")
    # 同一秒内重写时也要换掉 stat 身份，确保读口看到的是新文件。
    stamp = time.time() + (0.0 if value == "true" else 1.0)
    os.utime(path, (stamp, stamp))


def _surface(agent) -> dict[str, bool]:
    name = "list_pending_requests"
    snapshot = agent.tools.runtime_snapshot()
    searched = agent.tools.search_deferred_specs(name, runtime_snapshot=snapshot)
    return {
        "snapshot": name in set(snapshot.available_tool_names),
        "specs": name in {spec.name for spec in agent.tools.model_visible_specs()},
        "catalog": name in agent.tools.render_catalog_section(),
        "search": name in {spec.name for spec in searched},
    }


# LLM: PRO-MSG-021：真实保存“关之前”的运行时快照和调用，之后才关开关，再拿旧快照走 registry.execute_tool；
#   这才是过期快照证明（harness 每次新建快照，不能当这个证明）。
# 函数用途: 在开关开着时保存快照与规范调用。
def _saved_call(case):
    from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

    run_id = f"gwreq-inbox-{time.time_ns()}"
    attempt = case.agent.subagents.runtime_db.record_run_creation(
        owner_id=case.agent.home_paths.owner_id, goal="inbox", conversation_task_id=case.parent_task_id,
        thread_id=case.thread.thread_id, run_id=run_id, role="assistant")["attempt_id"]
    snapshot = case.agent.tools.runtime_snapshot(run_id=run_id)
    assert "list_pending_requests" in snapshot.available_tool_names
    return snapshot, canonical_test_call(snapshot, "list_pending_requests", {}, attempt_id=attempt)


def _child_facts(case) -> tuple:
    task = case.agent.subagents.load(case.child_id)
    wakes = case.agent.conversation_store.wakes.pending(limit=1000)
    return task.status, [(r.id, r.status) for r in task.capability_requests], len(wakes)


def test_b1_switch_toggles_live_on_the_same_agent_both_directions(case):
    _open_request(case, "req-1")
    _as_parent(case)
    on = _surface(case.agent)
    saved_snapshot, saved_call = _saved_call(case)
    _write_switch(case.agent, "false")
    off = _surface(case.agent)
    facts_before = _child_facts(case)
    direct, direct_payload = _list(case.agent)
    stale = case.agent.tools.execute_tool(saved_call, write_boundary=None, runtime_snapshot=saved_snapshot).result
    facts_after = _child_facts(case)
    _write_switch(case.agent, "true")
    again = _surface(case.agent)
    result, payload = _list(case.agent)

    # 工具不延迟（默认延迟合同），目录与延迟搜索本就只列延迟工具，开着时只核快照与规格。
    assert on["snapshot"] is True and on["specs"] is True
    assert off == {"snapshot": False, "specs": False, "catalog": False, "search": False}
    assert direct.ok is False and direct.effect_outcome == "not_started"
    assert direct_payload["error"]["reason"] == "PENDING_INBOX_DISABLED" and "items" not in direct_payload
    assert stale.ok is False and stale.effect_outcome == "not_started" and "req-1" not in stale.output
    assert facts_after == facts_before
    assert again == on and result.ok is True and [item["request_id"] for item in payload["items"]] == ["req-1"]


def test_b1_disabled_before_construction_is_absent_from_every_model_surface(case):
    _write_switch(case.agent, "false")
    fresh = _agent(case.config, case.workspace)

    assert _surface(fresh) == {"snapshot": False, "specs": False, "catalog": False, "search": False}
    _write_switch(fresh, "true")
    assert _surface(fresh)["specs"] is True


def test_b1_switch_is_isolated_per_owner(case, tmp_path):
    other_workspace = tmp_path / "other-ws"
    other_workspace.mkdir()
    other = _agent(type(case.config)(model_backend="echo", my_agent_home=str(tmp_path / "other-home"),
                                     enable_subagents=True), other_workspace)
    _write_switch(case.agent, "false")

    assert _surface(case.agent)["specs"] is False
    assert _surface(other)["specs"] is True


# LLM: “读不出”用真正读不了的路径（同名目录）构造；共享解析器对无法识别的行按缺省处理，那不算读不出，记在 HANDOFF。
# 函数用途: 非布尔值与配置读不出都按关闭处理，模型看不到、直接调用也不执行。
@pytest.mark.parametrize("broken", ["non_boolean", "unreadable"])
def test_b1_unreadable_or_non_boolean_config_fails_closed(case, broken):
    path = Path(case.agent.capability_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _surface(case.agent)
    if broken == "non_boolean":
        path.write_text("orchestration_pending_inbox_enabled: maybe\n", encoding="utf-8")
        stamp = time.time() + 2
        os.utime(path, (stamp, stamp))
    else:
        path.unlink(missing_ok=True)
        path.mkdir()
    result, _ = _list(case.agent)

    assert _surface(case.agent)["specs"] is False and result.ok is False
    assert result.effect_outcome == "not_started"


# LLM: 宿主显式内存注入的 CapabilityConfig 不经过文件严格解析；字符串 "false" 是真值，读口必须严格认 True。
# 函数用途: 内存注入的非布尔开关值也按关闭处理。
def test_b1_injected_non_boolean_value_is_not_treated_as_enabled(case):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.config import CapabilityConfig

    case.agent._capability_config_runtime_snapshot = SimpleNamespace(
        config=CapabilityConfig(orchestration_pending_inbox_enabled="false"))
    result, _ = _list(case.agent)

    assert _surface(case.agent)["specs"] is False and result.ok is False


def _write_raw(agent, text: str, bump: float) -> None:
    path = Path(agent.capability_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() + bump
    os.utime(path, (stamp, stamp))


# LLM: B1-R1（PRO-MSG-020/021）：选中的用户文件里有坏行（整份乱码、合法行夹坏行、引号不闭合）时，解析器跳过坏行
#   并给出结构诊断；收件箱必须按关闭处理，修好文件后同一实例恢复。
# 函数用途: 坏掉的选中配置不能把收件箱开成默认开启。
@pytest.mark.parametrize("text", [
    "{{{ not yaml\n",
    "orchestration_pending_inbox_enabled: true\n{{{ junk\n",
    'orchestration_pending_inbox_enabled: "true\n',
])
def test_b1_r1_malformed_selected_config_fails_closed_and_recovers(case, text):
    _open_request(case, "req-1")
    _as_parent(case)
    _write_switch(case.agent, "true")
    assert _surface(case.agent)["specs"] is True
    _write_raw(case.agent, text, 3)
    broken_surface = _surface(case.agent)
    broken, _ = _list(case.agent)
    _write_raw(case.agent, "orchestration_pending_inbox_enabled: true\n", 5)
    repaired, payload = _list(case.agent)

    assert broken_surface == {"snapshot": False, "specs": False, "catalog": False, "search": False}
    assert broken.ok is False and broken.effect_outcome == "not_started"
    assert repaired.ok is True and [item["request_id"] for item in payload["items"]] == ["req-1"]


# LLM: 合法的空文件、只有注释、只写别的键都是合法的部分配置，沿随包默认开启（PRO-MSG-020 的合法默认规则）。
# 函数用途: 合法部分配置保留随包默认。
@pytest.mark.parametrize("text", ["", "# 只有注释\n", "session_pair_hourly_limit: 7\n"])
def test_b1_valid_partial_config_keeps_the_shipped_default(case, text):
    _as_parent(case)
    _write_raw(case.agent, text, 3)

    assert _surface(case.agent)["specs"] is True and _list(case.agent)[0].ok is True


# LLM: B1-R2：没有用户文件、随包模板也读不到时，原入口会回一个默认对象；它不算来源，收件箱关闭。随包模板真读到时
#   缺用户文件是正常情况（正向对照）。只重定向随包路径，解析器、加载器和开关读口都是真实实现。
# 函数用途: 没有任何成功读取的来源时收件箱关闭。
def test_b1_r2_no_successfully_read_source_fails_closed(case, monkeypatch, tmp_path):
    from agent_py_agent.agent.capability import runtime_config_reload

    Path(case.agent.capability_config_path).unlink(missing_ok=True)
    fresh = _agent(case.config, case.workspace)
    assert not Path(fresh.capability_config_path).exists()
    with_template = _surface(fresh)["specs"]
    monkeypatch.setattr(runtime_config_reload, "bundled_capability_config_path", lambda: tmp_path / "missing.yaml")
    without_any_source = _surface(fresh)
    direct, _ = _list(fresh)

    assert with_template is True
    assert without_any_source == {"snapshot": False, "specs": False, "catalog": False, "search": False}
    assert direct.ok is False and direct.effect_outcome == "not_started"


# LLM: B1-R3：关着时真实 tool_search 的 query/names/catalog 三种入口（完整输出与 live_prompt_output）都不能再带出
#   它的名字、类别或原因；只排除对入参的原样回显（query/names/not_found）。开着时 names 能查到（正向对照），
#   其它工具的不可用诊断不受影响由原有 registry 测试守住。
# 函数用途: 关闭的收件箱不出现在模型可见的发现元数据里。
def test_b1_r3_disabled_inbox_leaves_no_discovery_metadata(case):
    _as_parent(case)
    search = case.agent.tools.tools["tool_search"]
    echo = {"query", "names", "not_found"}

    def leaked(params) -> bool:
        result = search.execute(params)
        payload = json.loads(result.output)
        live = (result.result_envelope or {}).get("tool_output_policy", {}).get("live_prompt_output")
        live = json.loads(live) if isinstance(live, str) else (live or {})
        rest = [{k: v for k, v in part.items() if k not in echo} for part in (payload, live) if isinstance(part, dict)]
        return "list_pending_requests" in json.dumps(rest, ensure_ascii=False)

    _write_switch(case.agent, "true")
    enabled = {"names": leaked({"names": ["list_pending_requests"]}), "catalog": leaked({"action": "catalog"})}
    _write_switch(case.agent, "false")
    disabled = {"query": leaked({"query": "list_pending_requests"}), "names": leaked({"names": ["list_pending_requests"]}),
                "catalog": leaked({"action": "catalog"})}

    assert enabled == {"names": True, "catalog": True}
    assert disabled == {"query": False, "names": False, "catalog": False}


def test_leaf_roles_never_get_the_tool():
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


def _recode(cursor: str, at: object) -> str:
    data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    data["at"] = at
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def test_typed_cursor_positions_are_rejected_without_coercion(case):
    for index in range(3):
        _add_requests(case.agent, case.child_id, [f"req-{index}"])
    _as_parent(case)
    _, first = _list(case.agent, limit=1)
    cursor = first["next_cursor"]
    original_at = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["at"]
    bad_positions = ["abc", {"a": 1, "b": 2, "c": 3}, [1, 2, 3], ["task-other", case.child_id, "req-0"],
                     [case.parent_task_id, "", "req-0"], [case.parent_task_id, case.child_id], ["", "", ""],
                     [case.parent_task_id, case.child_id, 123], [case.parent_task_id, 7, ""]]
    for at in bad_positions:
        result, payload = _list(case.agent, cursor=_recode(cursor, at))
        assert result.ok is False and result.error_code == "TOOL_INVALID_ARGUMENTS", (at, payload)
        assert result.effect_outcome == "not_started"
    ok, _ = _list(case.agent, cursor=_recode(cursor, original_at))
    assert ok.ok is True


def test_long_resolved_prefix_is_charged_to_a_finite_request_budget(case):
    _add_requests(case.agent, case.child_id, [f"req-closed-{n:03d}" for n in range(400)], status="CLOSED")
    _add_requests(case.agent, case.child_id, ["req-open"])
    _as_parent(case)
    pages, found, examined = 0, [], []
    cursor = ""
    while True:
        result, payload = _list(case.agent, limit=1, cursor=cursor)
        assert result.ok, result.output
        pages += 1
        examined.append(payload["coverage"]["requests_examined"])
        found.extend(item["request_id"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            break
        assert pages <= 10, "cursor did not advance"

    # 每页检查数有界、游标按“最后检查过”的申请前进，401 条状态各检查一次就找到唯一的待处理申请。
    assert found == ["req-open"]
    assert max(examined) <= MAX_EXAMINED_REQUESTS_COUNT and sum(examined) == 401
    # 第 4 页为空：子代理读取预算用完时无法确定后面还有没有孩子，只能交回游标再读一次。
    assert pages <= 4


def test_cursor_whose_request_vanished_restarts_the_child_and_reports_partial(case):
    for index in range(3):
        _add_requests(case.agent, case.child_id, [f"req-{index}"])
    _as_parent(case)
    _, first = _list(case.agent, limit=1)
    # 规范保存不会删掉已有申请，这里用指向不存在申请的游标模拟断点已消失。
    vanished = _recode(first["next_cursor"], [case.parent_task_id, case.child_id, "req-gone"])
    _, second = _list(case.agent, limit=5, cursor=vanished)

    assert [item["request_id"] for item in second["items"]] == ["req-0", "req-1", "req-2"]
    assert second["coverage"]["cursor_request_missing"] == [case.child_id]
    assert second["coverage"]["state"] == "partial"
