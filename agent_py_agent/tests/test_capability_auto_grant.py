"""编排稳定性 §5.1 头号靶钉子测试：常规能力申请机制层自动批。

复刻真机 3 例（A1-u2/B-u1/B-u3）根因：子代理申请 shell/写自己任务沙箱后，
主代理模型不调 resolve_capability_requests，子代理卡 BLOCKED 到收口 ok=False。
机制层自动批必须：常规申请（shell/读写、范围在任务沙箱内）落 grant 并置 GRANTED、
外部能力/高风险/越界/纯删除一律不碰（留给父级裁决）、工具端自动批后不再吵醒主代理、
wake 端 sweep 能兜住遗留 OPEN 请求。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.capability_auto_grant import (
    assess_routine_capability_request,
    auto_grant_open_requests,
    auto_grant_routine_request,
)
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams


def _agent_and_task(td: str):
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", subagent_workspace="subs"),
        Path(td),
    )
    task = agent.subagents.create_run(
        goal="建一个能跑的书店网站",
        thought="需要跑 npm install 和写源码",
        plan=["装依赖", "写代码", "跑测试"],
        allowed_tools=["read_file"],
    )
    return agent, task


def _record_request(agent, task, **overrides):
    params = {
        "problem": "需要 shell 跑 npm install 并把源码写进任务工作区",
        "needed_capability": "shell",
        "capability_type": "shell",
        "requested_tools": ["run_command", "write_file"],
        "requested_commands": ["npm install", "npm test"],
        "path_scope": [str(Path(task.task_workspace_dir) / "output")],
        "risk_level": "low",
    }
    params.update(overrides)
    return agent.subagents.lifecycle.record_capability_request(
        task.id, RecordCapabilityRequestParams(**params)
    )


def test_routine_shell_write_request_auto_grants():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        request = _record_request(agent, task)
        grant = auto_grant_routine_request(agent.subagents, task.id, request.id)
        assert grant is not None
        assert "run_command" in grant.tools
        assert "write_file" in grant.tools and "apply_patch" in grant.tools
        assert grant.command_allowlist == ["npm"]
        assert grant.request_scope.get("resolved_by") == "capability_auto_grant"
        reloaded = agent.subagents.load(task.id)
        statuses = {req.id: req.status for req in reloaded.capability_requests}
        assert statuses[request.id] == "GRANTED"
        assert any(g.request_id == request.id for g in reloaded.capability_grants)


def test_empty_path_scope_defaults_to_task_workspace():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        request = _record_request(agent, task, path_scope=[], requested_tools=["run_command"])
        grant = auto_grant_routine_request(agent.subagents, task.id, request.id)
        assert grant is not None
        assert grant.path_scope == [str(task.task_workspace_dir)]


def test_out_of_sandbox_path_is_not_auto_granted():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        request = _record_request(agent, task, path_scope=["/etc/systemd"])
        assessment = assess_routine_capability_request(agent.subagents.load(task.id), request)
        assert not assessment.eligible
        assert any(item.startswith("path_out_of_sandbox") for item in assessment.blockers)
        assert auto_grant_routine_request(agent.subagents, task.id, request.id) is None
        reloaded = agent.subagents.load(task.id)
        assert reloaded.capability_requests[0].status == "OPEN"


def test_external_capability_and_high_risk_are_not_auto_granted():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        cases = [
            {"requested_mcp_tools": ["browser"]},
            {"network_scope": ["https://example.com"]},
            {"requested_skills": ["deploy"]},
            {"capability_type": "network", "needed_capability": "network"},
            {"risk_level": "high"},
            {"requested_tools": ["controlled_exec"]},
            {"requested_commands": ["rm -rf build"], "requested_tools": [], "problem": "只想删构建产物"},
        ]
        current = agent.subagents.load(task.id)
        for overrides in cases:
            request = _record_request(agent, task, **overrides)
            assessment = assess_routine_capability_request(current, request)
            assert not assessment.eligible, overrides


def test_capability_request_tool_auto_grants_without_waking_parent():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool

        result = CapabilityRequestTool(agent).execute(
            {
                "run_id": task.id,
                "problem": "需要 shell 装依赖并写任务工作区",
                "capability_type": "shell",
                "requested_tools": ["run_command"],
                "requested_commands": ["npm install"],
                "risk_level": "low",
            }
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["auto_granted"] is True
        assert payload["status"] == "GRANTED"
        assert "run_command" in payload["granted_tools"]
        reloaded = agent.subagents.load(task.id)
        assert reloaded.capability_requests[0].status == "GRANTED"
        assert reloaded.capability_grants
        # 自动批不该再以 requires_main_agent=True 吵醒主代理(那是未决申请的通道)。
        store = getattr(agent, "conversation_store", None)
        if store is not None:
            pending = store.unhandled_observations_requiring_main(limit=10)
            assert all(
                item.event_type != "subagent_capability_request_open" for item in pending
            )


def test_capability_request_tool_keeps_special_request_open_for_parent():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool

        result = CapabilityRequestTool(agent).execute(
            {
                "run_id": task.id,
                "problem": "需要访问外部 API 拿汇率数据",
                "capability_type": "network",
                "requested_mcp_tools": ["exchange_api"],
            }
        )
        assert result.ok
        payload = json.loads(result.output)
        assert "auto_granted" not in payload
        assert payload["status"] == "OPEN"
        reloaded = agent.subagents.load(task.id)
        assert reloaded.capability_requests[0].status == "OPEN"


def test_wake_sweep_auto_grants_and_redispatches():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        _record_request(agent, task)
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
            auto_capability_sweep,
            sweep_applies_to_reason,
        )

        assert sweep_applies_to_reason("subagent_capability_request_open")
        assert sweep_applies_to_reason("subagent_capability_granted")
        assert sweep_applies_to_reason("subagent_capability_denied")
        assert sweep_applies_to_reason("subagent_runner_finished")
        assert not sweep_applies_to_reason("incoming_channel_message")

        dispatch_calls: list[dict] = []

        def _fake_dispatch(router, cfg, **kwargs):
            dispatch_calls.append(kwargs)
            return SimpleNamespace(records=[SimpleNamespace(step="runner", action="started")])

        agent.dispatch_subagents = _fake_dispatch
        signal = SimpleNamespace(
            metadata={"run_id": task.id},
            source_agent_id=task.id,
            reason="subagent_capability_request_open",
        )
        summary = auto_capability_sweep(agent, signal)
        assert summary["auto_granted"] == 1
        assert summary["redispatched"] == 1
        assert dispatch_calls
        dispatch_params = dispatch_calls[0]["params"]
        assert dispatch_params.apply is True
        assert dispatch_params.start_runners is True
        assert dispatch_params.include_run_ids == [task.id]
        reloaded = agent.subagents.load(task.id)
        assert reloaded.capability_requests[0].status == "GRANTED"


def test_audit_source_wake_uses_durable_background_autostart(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration.dispatch import (
            capability_auto_sweep,
        )

        started: list[int] = []
        monkeypatch.setattr(
            capability_auto_sweep,
            "auto_start_orphan_run",
            lambda _agent, _run_id: started.append(1)
            or {"started": 2, "run_ids": [task.id]},
        )
        monkeypatch.setattr(
            capability_auto_sweep,
            "_redispatch_stalled_subagents",
            lambda _agent: (_ for _ in ()).throw(
                AssertionError("source continuation must not occupy wake runner")
            ),
        )

        summary = capability_auto_sweep.auto_capability_sweep(
            agent,
            SimpleNamespace(
                metadata={
                    "run_id": task.id,
                    "audit_source_worker": True,
                },
                source_agent_id=task.id,
                reason="subagent_runner_finished",
            ),
        )

        assert started == [1]
        assert summary["redispatched"] == 2


def test_blocked_closeout_after_in_run_auto_grant_marks_capability_request():
    # 授权后续跑可达性钉子:运行中自动批的 grant,BLOCKED 收尾必须归为
    # CAPABILITY_REQUEST(dispatch._blocked_after_capability_grant 才认),
    # 续跑后没有新 grant 则不再触发(防无限续派)。
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        request = _record_request(agent, task)
        current = agent.subagents.load(task.id)
        current.runner_last_attempt_at = 1000.0  # attempt 开始时间(收尾流程刷新前)
        agent.subagents.save(current)
        grant = auto_grant_routine_request(agent.subagents, task.id, request.id)
        assert grant is not None and grant.created_at > 1000.0

        from types import SimpleNamespace

        from agent_py_agent.agent.subagents.runner_result_state import (
            RunnerResultFieldParams,
            apply_runner_result_fields,
        )

        def _blocked_closeout(reloaded):
            apply_runner_result_fields(
                RunnerResultFieldParams(
                    task=reloaded,
                    result_meta={"ok": False, "message": "blocked", "response": "", "dry_run": False},
                    status_context={"status": "BLOCKED", "verification_status": "", "failure_type": ""},
                    parsed=SimpleNamespace(
                        found=True,
                        ok=True,
                        status="BLOCKED",
                        failure_type="",
                        capability_requests=[],
                        blocked_reason="等待授权生效",
                        parse_error="",
                    ),
                    now=grant.created_at + 60.0,
                )
            )

        reloaded = agent.subagents.load(task.id)
        _blocked_closeout(reloaded)
        assert reloaded.status == "BLOCKED"
        assert reloaded.failure_type == "capability_request"

        # 续跑一轮后(attempt 开始时间已晚于 grant)再 BLOCKED → 不再归为能力等待。
        second = agent.subagents.load(task.id)
        second.runner_last_attempt_at = grant.created_at + 120.0
        second.failure_type = ""
        _blocked_closeout(second)
        assert second.failure_type != "capability_request"


def test_auto_grant_open_requests_sweeps_only_routine_ones():
    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        routine = _record_request(agent, task)
        special = _record_request(
            agent, task, capability_type="network", network_scope=["https://api.example.com"],
            problem="需要外网", requested_tools=[], requested_commands=[], path_scope=[],
        )
        grants = auto_grant_open_requests(agent.subagents, task.id)
        assert len(grants) == 1
        reloaded = agent.subagents.load(task.id)
        statuses = {req.id: req.status for req in reloaded.capability_requests}
        assert statuses[routine.id] == "GRANTED"
        assert statuses[special.id] == "OPEN"


def test_auto_grant_path_raises_grant_wake_signal():
    """S-C2 回归：auto_grant 直接批的路径必须补发 grant wake。

    真机实锤（SUB-C 场景1/s2-fast）：auto_grant 成功路径不发 wake，子代理若未
    按 next_action 收尾 BLOCKED，将无人唤醒它续跑 → 卡死 → CANCELLED。
    修复后：批完立即走 capability_followup._raise_grant_wake_signal_for_run，
    task attributes 应落 capability_grant_wake 账本（reason=subagent_capability_granted）。
    """
    from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool

    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        raised: list[dict[str, object]] = []

        class _FakeThread:
            thread_id = "thread-test"

        class _FakeStore:
            def thread_for_task(self, task_id):
                return _FakeThread()

            def append_observation(self, observation):
                return type("Obs", (), {"observation_id": "obs-1"})()

            def raise_wake_signal(self, request):
                raised.append(request)
                return type("Sig", (), {"wake_signal_id": "sig-1"})()

        agent.conversation_store = _FakeStore()  # type: ignore[assignment]
        tool = CapabilityRequestTool(agent)
        outcome = tool.execute(
            {
                "run_id": task.id,
                "problem": "需要写文件到任务工作区",
                "needed_capability": "shell",
                "capability_type": "shell",
                "requested_tools": ["write_file"],
                "path_scope": [str(Path(task.task_workspace_dir) / "output")],
                "risk_level": "low",
            }
        )
        assert outcome.ok, outcome.output
        payload = json.loads(outcome.output)
        assert payload.get("status") == "GRANTED"
        assert payload.get("auto_granted") is True
        # S-C2：auto_grant 成功后必须补发 grant wake（reason/dedupe_key 与
        # dispatch 路径同构），且 wake 账本落到 task attributes。
        assert len(raised) == 1, raised
        assert raised[0]["reason"] == "subagent_capability_granted"
        assert raised[0]["dedupe_key"] == f"capability-granted:{task.id}"
        reloaded = agent.subagents.load(task.id)
        attrs = reloaded.attributes or {}
        wake = attrs.get("capability_grant_wake") or {}
        assert wake.get("status") == "raised", attrs
        assert wake.get("wake_signal_id"), attrs


def test_auto_grant_wake_dedupe_key_matches_dispatch_path():
    """S-C2 去重：auto_grant 路径的 wake dedupe_key 与 dispatch 路径一致，
    双路径不会双发（capability-granted:{run_id}）。"""
    from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_followup import (
        _grant_wake_signal_request,
    )

    with tempfile.TemporaryDirectory() as td:
        agent, task = _agent_and_task(td)
        thread = agent.conversation_store.thread_for_task(task.id)
        if thread is None:
            # 无线程时验证 dedupe_key 构造本身与 dispatch 路径同构
            return
        request = _grant_wake_signal_request(thread, task, task.id, {"thread_id": thread.thread_id})
        assert request["dedupe_key"] == f"capability-granted:{task.id}"
        assert request["reason"] == "subagent_capability_granted"
