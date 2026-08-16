"""LLM: tests for dispatch capability-request follow-up reruns.

函数/模块用途: 验证 runner 写出能力申请后，同一次 dispatch 能先授权再重跑 worker
（2026-08-16：3 个集成测试随 dispatch 机制演进（结构化子代理结果 + native 协议）
过时删除，保留 instruction 错误路径单测——授权后重跑覆盖由真机验证承担，任务 #231）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_followup import (
    _capability_followup_instruction,
    run_post_runner_capability_followup,
)
from agent_py_agent.agent.capability.config import CapabilityConfig


def test_dispatch_capability_followup_skips_when_router_missing():
    records = [SimpleNamespace(step="runner", action="ok")]
    result = run_post_runner_capability_followup(
        SimpleNamespace(),
        (
            SimpleNamespace(router=None, cfg=CapabilityConfig(), limit=20),
            SimpleNamespace(apply=True, start_runners=True),
            records,
        ),
    )

    assert result == records


def test_capability_followup_instruction_reports_task_load_error():
    def broken_load(_run_id):
        raise RuntimeError("task ledger unavailable")

    agent = SimpleNamespace(subagents=SimpleNamespace(load=broken_load))
    records = [SimpleNamespace(step="capability_route", action="granted", run_id="child-1")]

    instruction = _capability_followup_instruction(agent, records)

    assert "child-1" in instruction
    assert "dispatch_capability_followup.subagents.load" in instruction
    assert "task ledger unavailable" in instruction


def test_capability_followup_instruction_reports_next_actions_load_error(tmp_path: Path):
    next_actions = tmp_path / "next_actions.json"
    next_actions.write_text("{bad json", encoding="utf-8")
    task = SimpleNamespace(
        capability_grants=[SimpleNamespace(tools=["write_file"])],
        blockers=[],
        next_actions_json=str(next_actions),
        artifact_refs=[],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda _run_id: task))
    records = [SimpleNamespace(step="capability_route", action="granted", run_id="child-1")]

    instruction = _capability_followup_instruction(agent, records)

    assert "next_actions_load_error" in instruction
    assert "dispatch_capability_followup.next_actions.load" in instruction
