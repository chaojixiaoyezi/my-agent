"""R4 子项②钉子测试：主代理对未决 capability_request 的显式 grant/deny 回路。

复刻 R4 GoAttack 根因之二（docs/audits/R4-goattack-20260611.md）：子代理提交
capability_request 后主代理无处理手段，请求一直 OPEN、子代理永久阻塞。
resolve_capability_requests 必须：grant 落 path_scope+写工具并即时生效到写边界、
deny 走协议终态 CLOSED 且原因可审计、越界目录结构化拒绝、两种处理都唤醒子代理。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
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
        assert reloaded.capability_requests[0].status == "GRANTED"
        assert reloaded.capability_grants, "grant 必须落进任务"
        # grant 即时生效:runner 写边界包含解锁目录(filesystem grant 自动并写工具)
        boundary = agent.subagents.runner_context._build_write_boundary(reloaded)
        granted_dir = str(Path(task.task_workspace_dir) / "output" / "goattack-python")
        assert granted_dir in boundary["allowed_write_roots"]
        # 唤醒账本落盘
        assert reloaded.attributes["capability_resolution_wake"]["decision"] == "grant"


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
        assert reloaded.capability_requests[0].status == "CLOSED"
        assert reloaded.capability_requests[0].constraints["denial_reason"] == "写自己的 output 目录即可"
        assert reloaded.attributes["capability_resolution_wake"]["decision"] == "deny"


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
        thread = store.thread_for_task(task.id)
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
        thread_now = store.thread_for_task(task.id)
        if thread_now is not None:
            assert notify_error is None, f"有 thread 时通知不应失败: {notify_error}"
