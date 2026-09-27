"""父代理能看到子代理工具失败的结构化摘要（2026-09-27 真实缺口 A）。

真机：4 个只读子代理读 owner home 外的工作树，list_files/read_file/search_text 全在授权阶段被
PATH_OWNER_SCOPE_BLOCKED 拦下；父代理的 list_agents 只看到 current_tool 和"最近成功调用工具: search_text"，
分不清"被拦下"和"在慢慢想"。这里钉住三件事：
1. 连续失败段只按 (error_code, failure_stage) 结构化字段计算，成功或不同原因打断，重复门自身拒绝透明；
2. 父代理 list_agents 的每个子代理节点带 recent_tool_failure，数据来自 owner 权威 runtime_events；
3. last_progress_summary 在最近一次调用失败时如实说失败，不再报更早的成功。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.agent_tree.model_view import agent_tree_model_preview
from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.session_progress import (
    record_runtime_subagent_tool_progress,
)
from agent_py_agent.agent.subagents.tool_failure_ledger import (
    ToolCallFact,
    tool_failure_progress_summary,
    tool_failure_streak,
)

_BLOCKED = "PATH_OWNER_SCOPE_BLOCKED"


def _fail(tool: str, code: str = _BLOCKED, stage: str = "authorization", at: float = 0.0) -> ToolCallFact:
    return ToolCallFact(tool=tool, ok=False, error_code=code, failure_stage=stage, at=at)


def _ok(tool: str) -> ToolCallFact:
    return ToolCallFact(tool=tool, ok=True)


# ---------------------------------------------------------------------------
# 1. 连续失败段的纯函数语义
# ---------------------------------------------------------------------------


def test_streak_counts_trailing_same_code_across_tools():
    facts = [_ok("search_text"), _fail("read_file"), _fail("list_files"), _fail("read_file", at=12.5)]
    streak = tool_failure_streak(facts)
    assert streak["tool"] == "read_file"
    assert streak["tools"] == ["read_file", "list_files"]
    assert streak["error_code"] == _BLOCKED
    assert streak["failure_stage"] == "authorization"
    assert streak["consecutive_failures"] == 3
    assert streak["ongoing"] is True
    assert streak["last_failed_at"] == 12.5
    assert "count_is_lower_bound" not in streak


def test_later_success_keeps_last_failure_but_marks_it_not_ongoing():
    streak = tool_failure_streak([_fail("read_file"), _fail("read_file"), _ok("search_text")])
    assert streak["consecutive_failures"] == 2
    assert streak["ongoing"] is False


def test_different_code_or_stage_breaks_the_streak():
    by_code = tool_failure_streak([_fail("read_file", code="TOOL_TIMEOUT"), _fail("read_file"), _fail("read_file")])
    assert (by_code["error_code"], by_code["consecutive_failures"]) == (_BLOCKED, 2)
    by_stage = tool_failure_streak([_fail("read_file", stage="execution"), _fail("read_file")])
    assert (by_stage["failure_stage"], by_stage["consecutive_failures"]) == ("authorization", 1)


def test_guardrail_self_blocks_neither_count_nor_break():
    guard = ToolCallFact(tool="read_file", ok=False, error_code="TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED")
    streak = tool_failure_streak([_fail("read_file"), guard, _fail("read_file"), guard])
    assert streak["consecutive_failures"] == 2
    assert streak["error_code"] == _BLOCKED
    assert streak["ongoing"] is True


def test_no_failure_means_no_summary_and_full_window_is_a_lower_bound():
    assert tool_failure_streak([_ok("read_file"), _ok("list_files")]) == {}
    assert tool_failure_streak([]) == {}
    capped = tool_failure_streak([_fail("read_file")] * 3, window_full=True)
    assert capped["count_is_lower_bound"] is True
    broken = tool_failure_streak([_ok("x"), *[_fail("read_file")] * 3], window_full=True)
    assert "count_is_lower_bound" not in broken


def test_progress_summary_is_built_only_from_structured_fields():
    assert tool_failure_progress_summary(
        {"tool": "read_file", "error_code": _BLOCKED, "consecutive_failures": 4}
    ) == "最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED，连续 4 次）"
    assert tool_failure_progress_summary(
        {"tool": "read_file", "error_code": _BLOCKED}
    ) == "最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED）"
    assert tool_failure_progress_summary({"tool": "run_command"}) == "最近一次工具调用失败：run_command"
    assert tool_failure_progress_summary(
        {"tool": "read_file", "error_code": _BLOCKED, "consecutive_failures": 200, "count_is_lower_bound": True}
    ) == "最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED，连续至少 200 次）"


# ---------------------------------------------------------------------------
# 2/3. 真实 owner 权威库 + 产品写入口 → 父代理 list_agents
# ---------------------------------------------------------------------------


def _managed_child(tmp_path):
    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    owner_home.mkdir(parents=True)
    manager = SubAgentManager(tmp_path / "subagents", owner_home_dir=str(owner_home))
    assert manager.runtime_db is not None
    child = manager.create_run(
        goal="读取 owner home 外的工作树",
        thought="只读调研",
        plan=["list_files", "read_file"],
        role="researcher",
        parent_id="task-root",
        root_id="task-root",
    )
    manager.runtime_db.record_run_creation(
        owner_id="owner-a", goal=child.goal, conversation_task_id="task-root",
        thread_id="thread-1", run_id=child.id, role="researcher",
    )
    return manager, child


def _denied(tool: str) -> dict:
    return {"tool": tool, "ok": False, "error_code": _BLOCKED, "failure_stage": "authorization"}


def _succeeded(tool: str) -> dict:
    return {"tool": tool, "ok": True, "error_code": ""}


# 函数用途: 按真实顺序模拟子代理一次工具调用——先经产品入口写权威 tool_completed 事件，再写子代理进度状态。
def _child_tool_call(manager, child, index: int, outcome: dict):
    archive = {
        "run_id": child.id,
        "task_id": "task-root",
        "attempt_id": "attempt-1",
        "operation_id": f"op-{index}",
        "handler_executed": outcome["ok"],
        "idempotency_key": f"key-{index}",
        **outcome,
    }
    persist_tool_runtime_ledger(SimpleNamespace(subagents=manager, local_store=None), archive)
    result = SimpleNamespace(
        tool_name=outcome["tool"], ok=outcome["ok"], error_code=outcome["error_code"],
        output="ok" if outcome["ok"] else "denied",
    )
    record_runtime_subagent_tool_progress(
        SimpleNamespace(subagents=manager),
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id=child.id),
            result=result,
            payload={"path": f"/outside/file-{index}.md"},
            tool_rounds=index,
            idx=1,
        ),
    )


def _child_node(manager, child):
    payload = json.loads(ListAgentsTool(SimpleNamespace(subagents=manager)).execute({}).output)
    return next(node for node in payload["nodes"] if node["run_id"] == child.id), payload


def test_tool_completed_event_carries_failure_stage_and_handler_fact(tmp_path):
    manager, child = _managed_child(tmp_path)
    _child_tool_call(manager, child, 1, _denied("read_file"))
    agent_run = manager.runtime_db.agent_run_for_run_id(child.id)
    events = manager.runtime_db.events_for_agent_run(str(agent_run["agent_run_id"]), event_type="tool_completed")
    payload = events[-1]["payload"]
    assert payload["failure_stage"] == "authorization"
    assert payload["handler_executed"] is False
    assert payload["error_code"] == _BLOCKED


def test_parent_list_agents_shows_child_authorization_failure_streak(tmp_path):
    manager, child = _managed_child(tmp_path)
    _child_tool_call(manager, child, 1, _succeeded("search_text"))
    for index, tool in enumerate(("list_files", "read_file", "search_text", "read_file"), start=2):
        _child_tool_call(manager, child, index, _denied(tool))

    node, payload = _child_node(manager, child)

    failure = node["recent_tool_failure"]
    assert failure["tool"] == "read_file"
    assert failure["error_code"] == _BLOCKED
    assert failure["failure_stage"] == "authorization"
    assert failure["consecutive_failures"] == 4
    assert failure["ongoing"] is True
    assert failure["last_failed_at"] > 0
    assert failure["tools"] == ["list_files", "read_file", "search_text"]
    # 大树预览（模型实际看到的有界 JSON）同样保留这份结构化摘要。
    preview_nodes = json.loads(agent_tree_model_preview(payload))["nodes"]
    assert next(row for row in preview_nodes if row["run_id"] == child.id)["recent_tool_failure"] == failure


def test_last_progress_summary_reports_latest_failure_not_earlier_success(tmp_path):
    manager, child = _managed_child(tmp_path)
    _child_tool_call(manager, child, 1, _succeeded("search_text"))
    success_at = manager.load(child.id).last_progress_at
    assert manager.load(child.id).last_progress_summary == "最近成功调用工具: search_text"
    for index in range(2, 5):
        _child_tool_call(manager, child, index, _denied("read_file"))

    stored = manager.load(child.id)
    assert stored.last_progress_summary == "最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED，连续 3 次）"
    assert stored.last_progress_at == success_at, "失败不是进展，不能刷新 last_progress_at"
    node, _payload = _child_node(manager, child)
    assert node["last_progress_summary"] == stored.last_progress_summary

    _child_tool_call(manager, child, 5, _succeeded("list_files"))
    recovered = manager.load(child.id)
    assert recovered.last_progress_summary == "最近成功调用工具: list_files"
    assert _child_node(manager, child)[0]["recent_tool_failure"]["ongoing"] is False


def test_no_authority_db_or_no_failure_keeps_node_without_failure_summary(tmp_path):
    managed, child = _managed_child(tmp_path)
    _child_tool_call(managed, child, 1, _succeeded("read_file"))
    assert "recent_tool_failure" not in _child_node(managed, child)[0]

    unmanaged = SubAgentManager(tmp_path / "local-subagents")
    assert unmanaged.runtime_db is None
    local_child = unmanaged.create_run(goal="本地非托管", thought="", plan=[], parent_id="task-root", root_id="task-root")
    record_runtime_subagent_tool_progress(
        SimpleNamespace(subagents=unmanaged),
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id=local_child.id),
            result=SimpleNamespace(tool_name="read_file", ok=False, error_code=_BLOCKED, output="denied"),
            payload={"path": "/outside/file.md"},
            tool_rounds=1,
            idx=1,
        ),
    )
    node = _child_node(unmanaged, local_child)[0]
    assert "recent_tool_failure" not in node
    # 没有权威次数时只写工具和错误码，不猜次数，也不再报成功。
    assert node["last_progress_summary"] == "最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED）"
