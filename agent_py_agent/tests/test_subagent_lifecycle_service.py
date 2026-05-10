from __future__ import annotations

"""LLM: verifies lifecycle mutations route through SubAgentLifecycleService.

给人看的解释：
这个测试确保能力请求、授权、证据和状态更新 service 化后仍保持旧 API 行为。
"""

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
)


def test_subagent_lifecycle_service_records_capabilities_and_status(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="service lifecycle", thought="keep API", plan=["record"])

    request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="need search",
            needed_capability="search",
            capability_type="shell",
            requested_tools=["shell_gateway"],
            requested_commands=["pytest"],
            path_scope=["/workspace/project"],
            output_budget={"stdout_bytes": 4096},
            risk_level="low",
        ),
    )
    grant = manager.record_capability_grant(
        task.id,
        RecordCapabilityGrantParams(
            request_id=request.id,
            grant_type="shell",
            skills=["memory-route"],
            tools=["search_text"],
            command_allowlist=["pytest"],
            path_scope=["/workspace/project"],
            output_budget={"stdout_bytes": 4096},
            risk_level="low",
        ),
    )
    evidence = manager.record_evidence(task.id, RecordEvidenceParams(kind="test", summary="passed"))
    updated = manager.set_status(task.id, "DONE", require_evidence=True)

    loaded = manager.load(task.id)
    assert manager.lifecycle is not None
    assert grant.skills == ["memory-route"]
    assert request.capability_type == "shell"
    assert grant.command_allowlist == ["pytest"]
    assert loaded.capability_requests[0].output_budget["stdout_bytes"] == 4096
    assert loaded.capability_grants[0].path_scope == ["/workspace/project"]
    assert evidence.ok is True
    assert updated.status == "DONE"
    assert loaded.allowed_tools == ["search_text"]
    assert loaded.capability_requests[0].id == request.id


def test_subagent_lifecycle_service_ignores_future_capability_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="future fields", thought="load compat", plan=["record"])
    request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="need shell",
            needed_capability="shell",
            requested_commands=["pwd"],
        ),
    )
    task_file = tmp_path / task.id / "task.json"
    payload = task_file.read_text(encoding="utf-8")
    payload = payload.replace(f'"id": "{request.id}"', f'"id": "{request.id}", "future_field": "ignored"', 1)
    task_file.write_text(payload, encoding="utf-8")

    loaded = manager.load(task.id)

    assert loaded.capability_requests[0].id == request.id
    assert loaded.capability_requests[0].requested_commands == ["pwd"]


def test_subagent_lifecycle_service_blocks_done_without_evidence(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="service lifecycle", thought="keep API", plan=["record"])

    with pytest.raises(ValueError):
        manager.set_status(task.id, "DONE", require_evidence=True)
