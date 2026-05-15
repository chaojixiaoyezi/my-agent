"""LLM: tests for dispatch capability-request follow-up reruns.

函数/模块用途: 验证 runner 写出能力申请后，同一次 dispatch 能先授权再重跑 worker。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent

from .backends import CapabilityThenAcceptedBackend, IncompleteOutputThenAcceptedBackend


# LLM: test_dispatch_routes_new_capability_request_then_reruns_worker covers the real R3 stalled flow.
# 函数用途: 同一次 dispatch 中，runner 写出 capability_request 后，应先路由授权，再重跑该 worker，而不是让上层模型空转猜下一步。
def test_dispatch_routes_new_capability_request_then_reruns_worker(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = CapabilityThenAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="需要受控 shell 能力，授权后再继续执行并报告 refs。",
            thought="先申请授权，再执行。",
            plan=["申请能力", "授权后执行", "等待验收"],
            allowed_tools=["write_file"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert backend.calls == 2
        assert [item.step for item in report.records].count("runner") == 2
        assert any(item.step == "capability_route" and item.action == "granted" for item in report.records)
        assert loaded.capability_requests[0].status == "GRANTED"
        assert loaded.capability_requests[0].requested_commands == ["pwd", "rm"]
        assert loaded.capability_grants[0].command_allowlist == ["pwd"]
        assert "controlled_exec" in loaded.allowed_tools
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"


# LLM: test_dispatch_reruns_incomplete_output_after_write_grant covers real HTML E2E stalls.
# 函数用途: 子代理产物只写半截时，父级授权继续写后，同一次 dispatch 应重跑该 run，而不是停在纸面授权。
def test_dispatch_reruns_incomplete_output_after_write_grant(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = IncompleteOutputThenAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="写一个完整的单文件 HTML。",
            thought="先写文件，必要时继续补齐。",
            plan=["写文件", "补齐", "等待验收"],
            allowed_tools=["write_file"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert backend.calls == 2
        assert [item.step for item in report.records].count("runner") == 2
        assert any(item.step == "capability_route" and item.action == "granted" for item in report.records)
        assert "授权后续跑" in backend.prompts[1]
        assert "不要从头重做任务" in backend.prompts[1]
        assert loaded.capability_requests[0].status == "GRANTED"
        assert "append_file" in loaded.allowed_tools
        assert "HTML已补齐" in loaded.result
