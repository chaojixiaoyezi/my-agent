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


def _agent_and_done_task_with_outputs(td: str, output_files: list[str]):
    # 干净的 DONE 子代理（无未决 capability_request），声明产物但未交付
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
        Path(td),
    )
    task = agent.subagents.create_run(
        goal="交付后端文件", thought="t", plan=["写文件"], allowed_tools=["write_file"],
    )
    task = agent.subagents.load(task.id)
    task.status = "DONE"
    task.attributes = {**task.attributes, "output_files": list(output_files)}
    agent.subagents.save(task)

    class _Closeout:
        class params:
            run_id = "main-run"
            task_attributes = {"run_workspace": {"task_root": str(Path(task.task_workspace_dir))}}

        agent = None

    return agent, task, _Closeout


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
        assert not tool.execute({"run_id": "subagent-missing", "decision": "grant", "reason": "x"}).ok
        # 处理完后再次调用 → 没有未决请求
        assert tool.execute({"run_id": task.id, "decision": "deny", "reason": "拒绝"}).ok
        again = tool.execute({"run_id": task.id, "decision": "deny", "reason": "再次"})
        assert not again.ok and "没有匹配的未决" in again.output


def test_resolution_unblocks_closeout_aggregation_gate():
    # 端到端:OPEN 请求拦 closeout → 处理后放行(与 SUBAGENTS_CAPABILITY_REQUESTS_OPEN 闭环)
    from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
        evaluate_subagent_aggregation_gate,
    )

    with tempfile.TemporaryDirectory() as td:
        agent, task, request = _agent_and_blocked_task(td)
        task = agent.subagents.load(task.id)
        task.status = "DONE"
        agent.subagents.save(task)

        class _Closeout:
            class params:
                run_id = "main-run"
                task_attributes = {
                    "run_workspace": {"task_root": str(Path(task.task_workspace_dir))}
                }

            agent = None

        decision_before = evaluate_subagent_aggregation_gate(_Closeout())
        assert decision_before.allowed is False

        _tool(agent).execute({"run_id": task.id, "decision": "grant", "reason": "解锁"})
        decision_after = evaluate_subagent_aggregation_gate(_Closeout())
        assert decision_after.allowed is True


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


def test_accept_output_gaps_records_exemption_and_unblocks_gate():
    # R4 子项③豁免出口：声明产物缺失拦 closeout → accept_output_gaps 登记豁免 → 放行
    from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
        evaluate_subagent_aggregation_gate,
    )

    with tempfile.TemporaryDirectory() as td:
        agent, task, closeout = _agent_and_done_task_with_outputs(td, ["goattack-python/missing.py"])

        # 缺失声明产物 → gate 拦
        before = evaluate_subagent_aggregation_gate(closeout())
        assert before.allowed is False
        assert any(f.code == "SUBAGENTS_DECLARED_OUTPUTS_MISSING" for f in before.findings)

        # 豁免具体声明
        result = _tool(agent).execute(
            {
                "run_id": task.id,
                "decision": "accept_output_gaps",
                "reason": "上游依赖未就绪，本轮先交可完成部分",
                "exempt_refs": ["goattack-python/missing.py"],
            }
        )
        payload = json.loads(result.output)
        assert result.ok and payload["exempted"][0]["ref"] == "goattack-python/missing.py"
        reloaded = agent.subagents.load(task.id)
        assert reloaded.attributes["output_delivery_exemptions"][0]["reason"].startswith("上游依赖")

        # 豁免后放行
        assert evaluate_subagent_aggregation_gate(closeout()).allowed is True


def test_accept_output_gaps_wildcard_exempts_report_only_task():
    # 纯汇报任务：缺省 exempt_refs → 通配 "*" 整体豁免
    from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
        evaluate_subagent_aggregation_gate,
    )

    with tempfile.TemporaryDirectory() as td:
        agent, task, closeout = _agent_and_done_task_with_outputs(td, ["a.md", "b.md", "c.md"])

        result = _tool(agent).execute(
            {"run_id": task.id, "decision": "accept_output_gaps", "reason": "纯汇报任务，结论在最终报告"}
        )
        payload = json.loads(result.output)
        assert payload["exempted"][0]["ref"] == "*"

        assert evaluate_subagent_aggregation_gate(closeout()).allowed is True
        # 幂等：重复豁免不重复登记
        _tool(agent).execute({"run_id": task.id, "decision": "accept_output_gaps", "reason": "再次"})
        reloaded = agent.subagents.load(task.id)
        assert len([r for r in reloaded.attributes["output_delivery_exemptions"] if r["ref"] == "*"]) == 1
