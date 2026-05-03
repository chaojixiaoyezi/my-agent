from __future__ import annotations

"""LLM: verifies lifecycle mutations route through SubAgentLifecycleService.

给人看的解释：
这个测试确保能力请求、授权、证据和状态更新 service 化后仍保持旧 API 行为。
"""

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_lifecycle_service_records_capabilities_and_status(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="service lifecycle", thought="keep API", plan=["record"])

    request = manager.record_capability_request(
        task.id,
        problem="need search",
        needed_capability="search",
    )
    grant = manager.record_capability_grant(
        task.id,
        request_id=request.id,
        skills=["memory-route"],
        tools=["search_text"],
    )
    evidence = manager.record_evidence(task.id, kind="test", summary="passed")
    updated = manager.set_status(task.id, "DONE", require_evidence=True)

    loaded = manager.load(task.id)
    assert manager.lifecycle is not None
    assert grant.skills == ["memory-route"]
    assert evidence.ok is True
    assert updated.status == "DONE"
    assert loaded.allowed_tools == ["search_text"]
    assert loaded.capability_requests[0].id == request.id


def test_subagent_lifecycle_service_blocks_done_without_evidence(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="service lifecycle", thought="keep API", plan=["record"])

    with pytest.raises(ValueError):
        manager.set_status(task.id, "DONE", require_evidence=True)
