"""R4 子项②钉子测试：主代理对未决 capability_request 的显式 grant/deny 回路。

复刻 R4 GoAttack 根因之二（docs/audits/R4-goattack-20260611.md）：子代理提交
capability_request 后主代理无处理手段，请求一直 OPEN、子代理永久阻塞。
resolve_capability_requests 必须：grant 落 path_scope+写工具并即时生效到写边界、
deny 走协议终态 CLOSED 且原因可审计、越界目录结构化拒绝、两种处理都唤醒子代理。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
)
from agent_py_agent.tests._tool_runtime_harness import execute_approved_registry_test_call


def _agent_and_blocked_task(td: str):
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
        Path(td),
    )
    task = agent.subagents.create_run(
        goal="写后端基础设施文件",
        thought="目标目录被锁,需要父级解锁",
        plan=["申请权限", "写文件"],
        allowed_tools=["write_file"],
    )
    request = agent.subagents.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="required_file_refs 目标路径不在 allowed_write_roots,无法写入",
            needed_capability="unlock_output_directory",
            capability_type="filesystem",
            path_scope=[str(Path(task.task_workspace_dir) / "output" / "goattack-python")],
        ),
    )
    return agent, task, request


def _tool(agent: SimpleAgent):
    from agent_py_agent.agent.core import ResolveCapabilityRequestsTool

    return ResolveCapabilityRequestsTool(agent)


# LLM: 只在 tmp_path 的真实 canonical/ConversationStore 上冻结旧结果和授权的交错；不启动模型或宿主进程。
# 函数用途: 复现旧 runner 已按 OPEN 构造 BLOCKED、父级在旧结果保存前授权的边界。
def _capability_closeout_race(tmp_path, *, grant=True, real_attempt=False):
    agent, task, request = _agent_and_blocked_task(str(tmp_path))
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "race-owner", "channel": "local",
        "channel_conversation_id": "race-thread", "channel_user_id": "race-owner",
    })
    parent_id = "parent-capability-race"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": parent_id, "status": "active"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task.id, "status": "active"})
    current = (agent.subagents.lifecycle.prepare_runner_attempt(task.id)
               if real_attempt else agent.subagents.load(task.id))
    attempt_id = current.runner_active_attempt_id if real_attempt else "attempt-original"
    current.status = "RUNNING"
    current.runner_active_attempt_id = attempt_id
    current.runner_attempts = 1
    current.attributes.update({
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_id,
        "runner_session": {
            "session_id": "session-original", "attempt_id": attempt_id,
            "status": "running", "heartbeat_at": time.time(), "ended_at": 0.0,
        },
    })
    agent.subagents.save(current)
    old_result_task = agent.subagents.load(task.id)
    old_result_task.status = "BLOCKED"
    old_result_task.failure_type = "capability_request"
    old_result_task.runner_active_attempt_id = ""
    old_result_task.turn_end_reason = "blocked"
    result = SimpleNamespace(
        run_id=task.id, status="BLOCKED", dry_run=False, turn_end_reason="blocked",
    )
    if real_attempt:
        repo = agent.subagents.runtime_db
        assert repo is not None
        run = repo.agent_run_for_run_id(task.id)
        repo.settle_agent_run(agent_run_id=run["agent_run_id"], attempt_id=attempt_id, status="done")
    if grant:
        granted = _tool(agent).execute({
            "run_id": task.id, "request_id": request.id, "decision": "grant", "reason": "同一任务授权",
        })
        assert granted.ok, granted.output
        assert json.loads(granted.output)["continuation"]["status"] == "already_running"
    agent.subagents.save(old_result_task)
    assert agent.subagents.load(task.id).status == ("PENDING" if grant else "BLOCKED")
    return agent, old_result_task, result


def test_capability_closeout_race_notifier_projects_current_run(tmp_path):
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path)
    store = agent.conversation_store
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert store.tasks.load(old_task.id).status == "active"
    assert result.status == "BLOCKED"  # 原工作片历史不能被重新解释成成功或下一工作片。
    wakes = [w for w in store.wakes.pending(limit=0) if w.reason == "subagent_runner_finished"]
    assert len(wakes) == 1 and wakes[0].metadata["status"] == "BLOCKED"
    store.wakes.mark_handled(wakes[0].wake_signal_id)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert not [w for w in store.wakes.pending(limit=0) if w.reason == "subagent_runner_finished"]
    assert store.tasks.load(old_task.id).status == "active"


def test_capability_closeout_race_worker_reads_canonical_after_session(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
    from agent_py_agent.agent.agent_core.runner.worker import _continue_pending_run_after_session

    agent, old_task, result = _capability_closeout_race(tmp_path)
    current = agent.subagents.load(old_task.id)
    session = {**current.attributes["runner_session"], "status": "completed", "ended_at": time.time()}
    assert agent.subagents.save_runner_session(old_task.id, session, now=time.time())
    started = []

    def start_next_slice(_worker, run_id):
        started.append(run_id)
        current = agent.subagents.load(run_id)
        current.runner_active_attempt_id = "attempt-next"
        current.attributes["runner_session"] = {**session, "attempt_id": "attempt-next", "status": "running"}
        agent.subagents.save(current)
        return {"started": 1, "run_ids": [run_id]}

    monkeypatch.setattr(capability_auto_sweep, "auto_start_orphan_run", start_next_slice)
    _continue_pending_run_after_session(agent, old_task.id, attempt_id="attempt-original")
    assert started == [old_task.id]
    _continue_pending_run_after_session(agent, old_task.id, attempt_id="attempt-original")
    assert started == [old_task.id]


@pytest.mark.parametrize("transition", ["completed", "cancelled", "new_attempt"])
def test_capability_closeout_race_wake_write_preserves_finished_session(tmp_path, transition):
    from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
        _record_resolution_wake,
        _ResolveContext,
    )

    agent, old_task, _result = _capability_closeout_race(tmp_path)
    stale = agent.subagents.load(old_task.id)
    finished = {**stale.attributes["runner_session"], "status": "completed", "ended_at": time.time()}
    assert agent.subagents.save_runner_session(old_task.id, finished, now=time.time())
    if transition != "completed":
        current = agent.subagents.load(old_task.id)
        current.status = "CANCELLED" if transition == "cancelled" else "RUNNING"
        current.runner_active_attempt_id = "attempt-next" if transition == "new_attempt" else ""
        current.attributes["runner_session"] = {
            **finished, "status": "cancelled" if transition == "cancelled" else "running",
            "attempt_id": current.runner_active_attempt_id or "attempt-original",
        }
        agent.subagents.save(current)
        finished = agent.subagents.load(old_task.id).attributes["runner_session"]
    _record_resolution_wake(
        agent, _ResolveContext(stale, "grant", {}, "已授权"), status="raised", wake_signal_id="wake-grant",
    )
    current = agent.subagents.load(old_task.id)
    assert current.attributes["runner_session"] == finished
    assert current.attributes["capability_resolution_wake"]["wake_signal_id"] == "wake-grant"
    assert current.status == {"completed": "PENDING", "cancelled": "CANCELLED", "new_attempt": "RUNNING"}[transition]


@pytest.mark.parametrize("current_status", ["CANCELLED", "ABANDONED", "TAKEN_OVER", "RUNNING", "PENDING"])
def test_capability_closeout_race_old_attempt_cannot_change_new_control_or_launch(tmp_path, monkeypatch, current_status):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
    from agent_py_agent.agent.agent_core.runner.worker import _continue_pending_run_after_session
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path)
    current = agent.subagents.load(old_task.id)
    current.status = current_status
    current.attributes["runner_session"] = {
        **current.attributes["runner_session"], "attempt_id": "attempt-newer",
        "status": "completed" if current_status == "PENDING" else "running",
    }
    current.runner_active_attempt_id = "attempt-newer" if current_status == "RUNNING" else ""
    agent.subagents.save(current)
    expected_link = "active" if current_status in {"RUNNING", "PENDING"} else current_status.lower()
    store = agent.conversation_store
    store.tasks.update_status({"task_id": old_task.id, "status": expected_link})
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert store.tasks.load(old_task.id).status == expected_link
    monkeypatch.setattr(capability_auto_sweep, "auto_start_orphan_run", lambda *_args: pytest.fail("旧执行轮不能派工"))
    _continue_pending_run_after_session(agent, old_task.id, attempt_id="attempt-original")
    assert agent.subagents.load(old_task.id).status == current_status


def test_capability_closeout_race_ungranted_blocked_stays_waiting(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
    from agent_py_agent.agent.agent_core.runner.worker import _continue_pending_run_after_session

    agent, task, _request = _agent_and_blocked_task(str(tmp_path))
    current = agent.subagents.load(task.id)
    current.status = "BLOCKED"
    agent.subagents.save(current)
    monkeypatch.setattr(capability_auto_sweep, "auto_start_orphan_run", lambda *_args: pytest.fail("OPEN 请求不能派工"))
    _continue_pending_run_after_session(agent, task.id, attempt_id="attempt-old")
    assert agent.subagents.load(task.id).capability_requests[0].status == "OPEN"


def test_capability_closeout_race_stop_during_projection_cas_is_preserved(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path)
    store = agent.conversation_store
    update = store.tasks.update_status

    def stop_before_reopen(request):
        if request.get("expected_status") == "blocked" and request["status"] == "active":
            update({"task_id": old_task.id, "status": "cancelled"})
        return update(request)

    monkeypatch.setattr(store.tasks, "update_status", stop_before_reopen)
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert store.tasks.load(old_task.id).status == "cancelled"


def test_capability_closeout_race_grant_between_projection_read_and_write(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.conversation_lifecycle_gate import (
        conversation_lifecycle_decisions,
    )
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path, grant=False)
    store = agent.conversation_store
    update = store.tasks.update_status
    observed = []

    def grant_before_blocked_projection(request):
        if request["status"] != "BLOCKED":
            return update(request)
        granted = _tool(agent).execute({"run_id": old_task.id, "decision": "grant", "reason": "收口期间授权"})
        assert granted.ok, granted.output
        link = update(request)
        current = agent.subagents.load(old_task.id)
        decision = conversation_lifecycle_decisions(agent, [current])[old_task.id]
        observed.append((decision.allowed, decision.should_cancel, current.status))
        assert old_task.id in store.threads.load(link.thread_id).active_task_ids
        return link

    monkeypatch.setattr(store.tasks, "update_status", grant_before_blocked_projection)
    monkeypatch.setattr(store.tasks, "_disable_task_progress_policies", lambda *_args, **_kwargs: pytest.fail("BLOCKED 不能关闭进度策略"))
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert observed == [(False, False, "PENDING")]
    assert store.tasks.load(old_task.id).status == "active"


def test_capability_closeout_race_ungranted_notifier_keeps_blocked(tmp_path):
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path, grant=False)
    store = agent.conversation_store
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id="attempt-original") == "delivered"
    assert agent.subagents.load(old_task.id).status == "BLOCKED"
    assert store.tasks.load(old_task.id).status == "BLOCKED"


@pytest.mark.parametrize("parent_status", ["active", "interrupted", "cancelled"])
def test_capability_closeout_race_real_dispatch_reopens_done_run_once(tmp_path, monkeypatch, parent_status):
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.agent_core.runner.worker import _continue_pending_run_after_session
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier

    agent, old_task, result = _capability_closeout_race(tmp_path, real_attempt=True)
    manager = agent.subagents
    repo = manager.runtime_db
    original = manager.load(old_task.id)
    old_attempt_id = original.attributes["runner_session"]["attempt_id"]
    before = repo.agent_run_for_run_id(old_task.id)
    assert before["status"] == "done"
    assert repo.get_attempt(old_attempt_id)["status"] == "done"
    assert len(repo.attempts_for_run(before["agent_run_id"])) == 1
    store = agent.conversation_store
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, manager.load, manager.save)
    assert notifier.notify_result(old_task, result, {}, attempt_id=old_attempt_id) == "delivered"
    session = {**original.attributes["runner_session"], "status": "completed", "ended_at": time.time()}
    assert manager.save_runner_session(old_task.id, session, now=time.time())
    for wake in store.wakes.pending(limit=0):
        store.wakes.mark_handled(wake.wake_signal_id)
    store.tasks.update_status({"task_id": original.attributes["conversation_task_id"], "status": parent_status})
    started = []

    # LLM: 只替换真实线程/模型入口；保留原 auto-start、creation guard、RuntimeDB 排队与准确 attempt 激活。
    # 函数用途: 消费调度器实际登记的新 attempt，证明同 run 接续不依赖把旧 done 账改成 created。
    def fake_worker(_agent, request, _mark_errors):
        next_id = request.params.expected_attempt_ids[old_task.id]
        assert next_id != old_attempt_id and repo.get_attempt(next_id)["status"] == "pending"
        prepared = manager.lifecycle.prepare_runner_attempt(old_task.id, expected_attempt_id=next_id)
        started.append(prepared.runner_active_attempt_id)
        return {"status": "started", "run_ids": request.run_ids}

    monkeypatch.setattr(dispatch, "_use_inprocess_autostart", lambda _agent: True)
    monkeypatch.setattr(dispatch, "_start_inprocess_dispatch", fake_worker)
    _continue_pending_run_after_session(agent, old_task.id, attempt_id=old_attempt_id)
    if parent_status != "active":
        assert not started
        assert len(repo.attempts_for_run(before["agent_run_id"])) == 1
        assert repo.agent_run_for_run_id(old_task.id)["status"] == "done"
        assert manager.load(old_task.id).status == "PENDING"
        return
    assert len(started) == 1
    after = repo.agent_run_for_run_id(old_task.id)
    assert after["agent_run_id"] == before["agent_run_id"]
    assert after["current_attempt_id"] == started[0]
    assert after["current_attempt_generation"] == before["current_attempt_generation"] + 1
    attempts = repo.attempts_for_run(before["agent_run_id"])
    assert len(attempts) == 2 and {row["status"] for row in attempts} == {"done", "running"}
    assert _tool(agent).execute({"run_id": old_task.id, "decision": "grant", "reason": "重复裁决"}).ok
    assert notifier.notify_result(old_task, result, {}, attempt_id=old_attempt_id) == "delivered"
    _continue_pending_run_after_session(agent, old_task.id, attempt_id=old_attempt_id)
    assert len(repo.attempts_for_run(before["agent_run_id"])) == 2
    assert store.tasks.load(old_task.id).status == "active"
    assert not store.wakes.pending(limit=0)


def test_parent_resolution_is_mutating_within_existing_authority() -> None:
    """Parent grant/deny cannot trigger a duplicate end-user dangerous approval."""
    from agent_py_agent.agent.core import ResolveCapabilityRequestsTool
    from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy

    policy = ResolveCapabilityRequestsTool.runtime_policy
    assert tool_effect_for_runtime_policy(policy, {"decision": "grant"}) == "mutating"
    assert tool_effect_for_runtime_policy(policy, {"decision": "deny"}) == "mutating"


def test_grant_resolves_request_and_extends_write_boundary():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        result = _tool(agent).execute(
            {"run_id": task.id, "decision": "grant", "reason": "解锁产物目录"}
        )
        payload = json.loads(result.output)
        assert result.ok and payload["ok"]
        assert payload["resolved"][0]["status"] == "GRANTED"

        reloaded = agent.subagents.load(task.id)
        assert reloaded.status == "PENDING"
        assert reloaded.capability_requests[0].status == "GRANTED"
        assert reloaded.capability_grants, "grant 必须落进任务"
        # grant 即时生效:runner 写边界包含解锁目录(filesystem grant 自动并写工具)
        boundary = agent.subagents.runner_context._build_write_boundary(reloaded)
        granted_dir = str(Path(task.task_workspace_dir) / "output" / "goattack-python")
        assert granted_dir in boundary["allowed_write_roots"]
        assert boundary["capability_write_roots"] == [granted_dir]
        # 唤醒账本落盘
        assert reloaded.attributes["capability_resolution_wake"]["decision"] == "grant"


def test_grant_uses_root_runtime_snapshot_as_tool_authority():
    """A root parent may grant an exact registered tool and the same run receives it."""
    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="读取文件",
            thought="需要父级工具授权",
            plan=["申请", "读取"],
            allowed_tools=["capability_request"],
        )
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="当前快照没有 read_file",
                needed_capability="read_file",
                capability_type="tool",
                requested_tools=["read_file"],
            ),
        )

        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "request_id": request.id,
                "decision": "grant",
                "reason": "read_file 在父级当前运行时快照内",
            }
        )
        payload = json.loads(result.output)
        reloaded = agent.subagents.load(task.id)

        assert result.ok and payload["ok"]
        assert payload["resolved"][0]["parent_tool_authority"][
            "all_requested_tools_grantable"
        ] is True
        assert reloaded.capability_requests[0].status == "GRANTED"
        assert reloaded.capability_grants[0].tools == ["read_file"]
        assert reloaded.status == "PENDING"


def test_grant_prefers_exact_parent_creation_snapshot_over_child_process_registry():
    """A root child grant reads the creating turn snapshot, not its worker process tool pool."""
    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="读取文件",
            thought="请求父级已有能力",
            plan=["申请", "继续"],
            parent_id="root-turn-exact",
            root_id="root-turn-exact",
            allowed_tools=["capability_request"],
            attributes={
                "direct_parent_tool_authority": {
                    "schema_version": "direct_parent_tool_authority.v1",
                    "parent_run_id": "root-turn-exact",
                    "available_tool_names": ["read_file"],
                    "snapshot_hash": "sha256:exact",
                    "owner_type": "main_agent",
                }
            },
        )
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="当前 child 没有 read_file",
                needed_capability="read_file",
                capability_type="tool",
                requested_tools=["read_file"],
            ),
        )

        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "request_id": request.id,
                "decision": "grant",
                "reason": "创建该 child 的父回合快照含 read_file",
            }
        )
        payload = json.loads(result.output)

        assert result.ok
        authority = payload["resolved"][0]["parent_tool_authority"]
        assert authority["authority_source"] == "parent_creation_runtime_snapshot"
        assert authority["snapshot_error_code"] == ""


def test_grant_rejects_tool_outside_parent_runtime_snapshot():
    """Model prose cannot grant a tool absent from the direct parent's runtime snapshot."""
    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="调用不存在工具",
            thought="测试父级上限",
            plan=["申请"],
            allowed_tools=["capability_request"],
        )
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="需要一个未注册工具",
                needed_capability="not_registered_tool",
                capability_type="tool",
                requested_tools=["not_registered_tool"],
            ),
        )

        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "request_id": request.id,
                "decision": "grant",
                "reason": "模拟模型错误批准",
            }
        )
        payload = json.loads(result.output)
        reloaded = agent.subagents.load(task.id)

        assert result.ok is False and payload["ok"] is False
        assert payload["errors"][0]["unavailable_tools"] == ["not_registered_tool"]
        assert reloaded.capability_requests[0].status == "OPEN"
        assert reloaded.capability_grants == []


def test_mcp_grant_enters_next_runner_tool_snapshot():
    """requested_mcp_tools must affect the next immutable runner snapshot, not only audit JSON."""
    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="调用 MCP 工具",
            thought="测试 MCP grant 生效",
            plan=["申请", "继续"],
            allowed_tools=["capability_request"],
        )
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="当前角色快照没有精确工具",
                needed_capability="read_file",
                capability_type="mcp",
                requested_mcp_tools=["read_file"],
            ),
        )

        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "request_id": request.id,
                "decision": "grant",
                "reason": "精确工具在父级快照内",
            }
        )
        reloaded = agent.subagents.load(task.id)
        _skills, tools, grants = agent.subagents.runner_context._extract_granted_caps(reloaded)

        assert result.ok
        assert reloaded.capability_grants[0].mcp_tools == ["read_file"]
        assert "read_file" in tools
        assert grants[0]["mcp_tools"] == ["read_file"]


def test_nested_parent_can_regrant_only_its_effective_mcp_tool():
    """A prior MCP grant is durable parent authority for the exact grandchild tool."""
    from agent_py_agent.agent.agent_core.runner.context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        parent = agent.subagents.create_run(
            goal="父代理",
            thought="先获得精确工具再派孙代理",
            plan=["申请", "分派"],
            allowed_tools=["capability_request", "resolve_capability_requests"],
        )
        parent_request = agent.subagents.lifecycle.record_capability_request(
            parent.id,
            RecordCapabilityRequestParams(
                problem="父代理需要 read_file",
                needed_capability="read_file",
                capability_type="mcp",
                requested_mcp_tools=["read_file"],
            ),
        )
        agent.subagents.lifecycle.record_capability_grant(
            parent.id,
            RecordCapabilityGrantParams(
                request_id=parent_request.id,
                grant_type="mcp",
                mcp_tools=["read_file"],
                reason="根代理授予精确工具",
            ),
        )
        child = agent.subagents.create_run(
            goal="孙代理读取文件",
            thought="需要直属父级授权",
            plan=["申请", "读取"],
            parent_id=parent.id,
            root_id=parent.root_id,
            allowed_tools=["capability_request"],
        )
        child_request = agent.subagents.lifecycle.record_capability_request(
            child.id,
            RecordCapabilityRequestParams(
                problem="孙代理需要 read_file",
                needed_capability="read_file",
                capability_type="mcp",
                requested_mcp_tools=["read_file"],
            ),
        )

        previous = set_current_subagent_context(agent, run_id=parent.id)
        try:
            result = _tool(agent).execute(
                {
                    "run_id": child.id,
                    "request_id": child_request.id,
                    "decision": "grant",
                    "reason": "工具属于直属父级有效权限",
                }
            )
        finally:
            restore_current_subagent_context(agent, previous)

        payload = json.loads(result.output)
        reloaded_parent = agent.subagents.load(parent.id)
        reloaded_child = agent.subagents.load(child.id)
        assert result.ok and payload["ok"]
        assert "read_file" in reloaded_parent.allowed_tools
        assert payload["resolved"][0]["parent_tool_authority"]["authority_source"] == (
            "parent_run_execution_context"
        )
        assert reloaded_child.capability_grants[0].mcp_tools == ["read_file"]
        assert "read_file" in reloaded_child.allowed_tools


def test_deny_closes_request_with_auditable_reason_and_wakes():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        result = _tool(agent).execute(
            {"run_id": task.id, "decision": "deny", "reason": "写自己的 output 目录即可"}
        )
        payload = json.loads(result.output)
        assert result.ok and payload["ok"]
        assert payload["resolved"][0]["status"] == "CLOSED"

        reloaded = agent.subagents.load(task.id)
        assert reloaded.status == "PENDING"
        assert reloaded.capability_requests[0].status == "CLOSED"
        assert reloaded.capability_requests[0].constraints["denial_reason"] == "写自己的 output 目录即可"
        assert reloaded.attributes["capability_resolution_wake"]["decision"] == "deny"


def test_resolution_requeues_legacy_done_child_with_open_request():
    """OPEN 请求是结构化阻塞事实，能纠正旧版误写的 DONE。"""
    with tempfile.TemporaryDirectory() as td:
        agent, task, _request = _agent_and_blocked_task(td)
        task = agent.subagents.load(task.id)
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        task.failure_type = "capability_request"
        agent.subagents.save(task)

        result = _tool(agent).execute(
            {"run_id": task.id, "decision": "grant", "reason": "继续同一任务"}
        )
        payload = json.loads(result.output)
        reloaded = agent.subagents.load(task.id)

        assert result.ok is True
        assert payload["continuation"]["previous_status"] == "DONE"
        assert payload["continuation"]["next_status"] == "PENDING"
        assert reloaded.status == "PENDING"
        assert reloaded.verification_status == "UNVERIFIED"
        assert reloaded.failure_type == ""


def test_grant_rejects_out_of_workspace_roots_structurally():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "decision": "grant",
                "reason": "测试越界",
                "write_roots": ["/etc/cron.d"],
            }
        )
        payload = json.loads(result.output)
        assert not payload["ok"]
        assert payload["errors"][0]["rejected_write_roots"] == ["/etc/cron.d"]
        reloaded = agent.subagents.load(task.id)
        # 越界授权不落 grant、请求保持未决
        assert reloaded.capability_requests[0].status == "OPEN"
        assert not reloaded.capability_grants


def test_grant_cannot_use_broad_agent_workspace_to_cross_owner_wall():
    """直属父级的显式 grant 也不能把 scoped owner 扩到宿主 workspace。"""
    with tempfile.TemporaryDirectory() as td:
        host_root = Path(td)
        owner_root = host_root / "owners" / "alice"
        owner_root.mkdir(parents=True)
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            owner_root,
        )
        agent.workspace_root = host_root
        agent.subagents.owner_scope_root = str(owner_root)
        task = agent.subagents.create_run(
            goal="读取宿主全局目录",
            thought="申请扩大目录范围",
            plan=["申请权限"],
        )
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="需要读取 owner 外的宿主目录",
                needed_capability="host_workspace_access",
                capability_type="filesystem",
                path_scope=[str(host_root)],
            ),
        )

        result = _tool(agent).execute(
            {"run_id": task.id, "decision": "grant", "reason": "模拟错误的父级批准"}
        )
        payload = json.loads(result.output)
        reloaded = agent.subagents.load(task.id)

        assert result.ok is False and payload["ok"] is False
        assert payload["errors"][0]["rejected_write_roots"] == [str(host_root)]
        assert next(item for item in reloaded.capability_requests if item.id == request.id).status == "OPEN"
        assert reloaded.capability_grants == []


def test_missing_params_and_no_pending_requests_error_clearly():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        tool = _tool(agent)
        assert not tool.execute({"run_id": task.id, "decision": "grant"}).ok  # 缺 reason
        assert not tool.execute({"run_id": task.id, "decision": "ignore", "reason": "x"}).ok
        missing = tool.execute({"run_id": "subagent-missing", "decision": "grant", "reason": "x"})
        # run_id 打错 → 报错并给真实子代理名册(带未决申请标注)供自纠
        assert not missing.ok and task.id in missing.output and "未决申请" in missing.output
        # 处理完后再次调用 → 幂等 no-op 成功(不再制造业务失败去喂工具熔断器),
        # 带子代理状态实情 + 申请账目 + 下一步指引(真机 0/22 编队拖死链的钉子)
        assert tool.execute({"run_id": task.id, "decision": "deny", "reason": "拒绝"}).ok
        again = tool.execute({"run_id": task.id, "decision": "deny", "reason": "再次"})
        payload = json.loads(again.output)
        assert again.ok and payload["ok"] and payload["status"] == "no_pending_requests"
        assert payload["capability_request_status_counts"].get("CLOSED") == 1
        assert "cancel_subagents" in payload["note"]


def test_request_id_mismatch_lists_actual_pending_ids():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        wrong = _tool(agent).execute(
            {"run_id": task.id, "decision": "grant", "reason": "x", "request_id": "capreq-nope"}
        )
        # 有未决申请但 request_id 对不上 → 报错并列出真实 request_id 供自纠(不是笼统失败)
        assert not wrong.ok and request.id in wrong.output


def test_parent_cannot_resolve_grandchild_capability_request():
    from agent_py_agent.agent.agent_core.runner.context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        parent = agent.subagents.create_run(goal="父", thought="", plan=["分工"])
        child = agent.subagents.create_run(
            goal="子",
            thought="",
            plan=["继续分工"],
            parent_id=parent.id,
            root_id=parent.id,
        )
        grandchild = agent.subagents.create_run(
            goal="孙",
            thought="",
            plan=["申请权限"],
            parent_id=child.id,
            root_id=parent.id,
        )
        request = agent.subagents.lifecycle.record_capability_request(
            grandchild.id,
            RecordCapabilityRequestParams(
                problem="目标目录不可写",
                needed_capability="unlock_output_directory",
                capability_type="filesystem",
                path_scope=[str(Path(grandchild.task_workspace_dir) / "output")],
            ),
        )
        previous = set_current_subagent_context(agent, run_id=parent.id)
        try:
            result = _tool(agent).execute(
                {
                    "run_id": grandchild.id,
                    "decision": "deny",
                    "reason": "越级裁决",
                }
            )
        finally:
            restore_current_subagent_context(agent, previous)

        assert result.ok is False
        reloaded = agent.subagents.load(grandchild.id)
        assert next(item for item in reloaded.capability_requests if item.id == request.id).status == "OPEN"


def test_cancelled_child_closes_leftover_open_request():
    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        task = agent.subagents.load(task.id)
        task.status = "BLOCKED"
        agent.subagents.save(task)
        # seq 253 闭合：MANAGED authority 门要求 run 登记真实权威链。
        repo = agent.subagents.runtime_db
        record = repo.record_run_creation(
            owner_id="owner-a",
            goal="cancel gate test-run",
            conversation_task_id="task-test-run",
            thread_id="thread-test-run",
            run_id="test-run",
            role="assistant",
        )
        result = execute_approved_registry_test_call(
            agent.tools,
            "cancel_subagents",
            {"run_ids": [task.id], "reason": "救不回来,了结"},
            attempt_id=str(record["attempt_id"]),
        )
        assert result.ok
        reloaded = agent.subagents.load(task.id)
        assert reloaded.status == "CANCELLED"
        assert [r.status for r in reloaded.capability_requests] == ["CLOSED"]
        assert reloaded.capability_requests[0].constraints["denial_reason"].startswith("subagent_cancelled")
        assert reloaded.attributes["cancel_subagents"]["closed_capability_request_ids"] == [request.id]


def test_capability_request_submission_notifies_parent_thread():
    # R4 根因:子代理提交请求后主代理全程不知道。提交端必须发 requires_main_agent 观察。
    from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool
    from agent_py_agent.agent.agent_core.runner import context as runner_context

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="测试提交推送", thought="t", plan=["p"], allowed_tools=["write_file"],
        )
        store = agent.conversation_store
        thread = store.tasks.thread_for(task.id)
        if thread is None and hasattr(store, "open_thread_for_task"):
            store.open_thread_for_task(task.id)

        token = runner_context.push_subagent_run_id(agent, task.id) if hasattr(runner_context, "push_subagent_run_id") else None
        try:
            result = CapabilityRequestTool(agent).execute(
                {
                    "run_id": task.id,
                    "problem": "目标目录不可写",
                    "needed_capability": "unlock_output_directory",
                    "capability_type": "filesystem",
                    "path_scope": [str(Path(task.task_workspace_dir) / "output")],
                }
            )
        finally:
            if token is not None and hasattr(runner_context, "pop_subagent_run_id"):
                runner_context.pop_subagent_run_id(agent, token)
        assert result.ok, result.output

        reloaded = agent.subagents.load(task.id)
        assert reloaded.capability_requests, "请求必须落账"
        # 通知失败时会落结构化错误账本;成功时不应有错误记录
        notify_error = reloaded.attributes.get("capability_request_notify_error")
        thread_now = store.tasks.thread_for(task.id)
        if thread_now is not None:
            assert notify_error is None, f"有 thread 时通知不应失败: {notify_error}"


def test_capability_request_wake_carries_parent_tool_authority_and_separate_approval_fact():
    """The parent wake exposes exact grantability without turning grant into user approval."""
    from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool
    from agent_py_agent.agent.agent_core.runner import context as runner_context

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(
            goal="测试 MCP 权限申请通知",
            thought="提交非自动授权请求",
            plan=["申请"],
            allowed_tools=["capability_request"],
        )
        store = agent.conversation_store
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": "owner-capability-test",
                "channel": "internal",
                "channel_conversation_id": "capability-test-thread",
                "channel_user_id": "owner-capability-test",
            }
        )
        store.tasks.bind(
            {
                "thread_id": thread.thread_id,
                "task_id": task.id,
                "goal": task.goal,
            }
        )
        previous = runner_context.set_current_subagent_context(agent, run_id=task.id)
        try:
            result = CapabilityRequestTool(agent).execute(
                {
                    "problem": "角色快照没有 read_file",
                    "needed_capability": "read_file",
                    "capability_type": "mcp",
                    "requested_mcp_tools": ["read_file"],
                }
            )
        finally:
            runner_context.restore_current_subagent_context(agent, previous)

        assert result.ok
        events = store.observations.unhandled_requiring_main(limit=20)
        event = next(item for item in events if item.event_type == "subagent_capability_request_open")
        authority = event.metadata["parent_tool_authority"]
        request_scope = event.metadata["capability_request"]

        assert request_scope["requested_mcp_tools"] == ["read_file"]
        assert authority["grantable_tools"] == ["read_file"]
        assert authority["all_requested_tools_grantable"] is True
        assert authority["capability_resolution_requires_user_approval"] is False
        assert authority["dangerous_tool_call_approval_is_separate"] is True
