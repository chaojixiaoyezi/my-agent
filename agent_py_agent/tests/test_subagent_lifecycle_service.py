from __future__ import annotations

"""LLM: verifies lifecycle mutations route through SubAgentLifecycleService.

给人看的解释：
这个测试确保能力请求、授权、证据和状态更新 service 化后仍保持旧 API 行为。
"""

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGapParams,
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
    assert "search_text" in loaded.allowed_tools
    assert "read_file" in loaded.allowed_tools
    assert "write_file" in loaded.allowed_tools
    assert loaded.capability_requests[0].id == request.id


def test_subagent_lifecycle_service_loads_current_capability_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="capability fields", thought="load current fields", plan=["record"])
    request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="need shell",
            needed_capability="shell",
            requested_commands=["pwd"],
        ),
    )

    loaded = manager.load(task.id)

    assert loaded.capability_requests[0].id == request.id
    assert loaded.capability_requests[0].requested_commands == ["pwd"]


def test_subagent_lifecycle_service_dedupes_equivalent_capability_requests(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="dedupe capability requests", thought="avoid repeats", plan=["record"])
    params = RecordCapabilityRequestParams(
        problem="need controlled shell",
        needed_capability="controlled_exec",
        capability_type="shell",
        requested_tools=["controlled_exec"],
        requested_commands=["python3 -c \"print(1)\"", "rm stale.txt"],
        path_scope=[str(tmp_path)],
        output_budget={"stdout_bytes": 4096},
        risk_level="low",
    )

    first = manager.record_capability_request(task.id, params)
    second = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="same request in different words",
            needed_capability="controlled_exec",
            capability_type="shell",
            requested_tools=["controlled_exec"],
            requested_commands=["rm", "python3"],
            path_scope=[str(tmp_path)],
            output_budget={"stdout_bytes": 4096},
            risk_level="low",
        ),
    )

    loaded = manager.load(task.id)
    assert second.id == first.id
    assert len(loaded.capability_requests) == 1


def test_subagent_lifecycle_service_blocks_done_without_evidence(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="service lifecycle", thought="keep API", plan=["record"])

    with pytest.raises(ValueError):
        manager.set_status(task.id, "DONE", require_evidence=True)


def test_subagent_lifecycle_service_records_memory_route_load_error(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents.services import lifecycle

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="需要额外能力", thought="record gap", plan=["record"])
    index = manager.workspace_root / "memory" / "routing" / "INDEX.md"
    index.parent.mkdir(parents=True)
    index.write_text("# routes\n", encoding="utf-8")

    def broken_load_routes(_path):
        raise OSError("memory route index unreadable")

    monkeypatch.setattr(lifecycle, "load_routes", broken_load_routes)

    gap = manager.record_capability_gap(
        task.id,
        RecordCapabilityGapParams(
            missing_capability="search",
            why_failed="route lookup failed",
        ),
    )

    loaded = manager.load(task.id)
    assert loaded.capability_gaps[0].id == gap.id
    assert gap.memory_routes[0]["route_id"] == "_memory_route_load_error"
    assert gap.memory_routes[0]["context"] == "capability_gap.memory_routes"
    assert "memory route index unreadable" in gap.memory_routes[0]["message"]


def test_create_run_rebinds_stale_self_output_subagent_path(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    stale_id = "subagent-0000000000-stale"
    task = manager.create_run(
        goal="请按结构化 output_refs 写数据收集结果。",
        thought="输出路径由 attributes.output_refs 决定。",
        plan=["读取输入 refs", "生成摘要 refs"],
        acceptance_checks=["必须保存到结构化产物 refs。"],
        attributes={
            "input_refs": [f"data/subagents/{stale_id}/input.md"],
            "output_refs": [f"data/subagents/{stale_id}/data_collection.md"],
            "output_files": [f"data/subagents/{stale_id}/summary.md"],
        },
    )
    loaded = manager.load(task.id)

    assert loaded.attributes["input_refs"] == [f"data/subagents/{stale_id}/input.md"]
    assert loaded.attributes["output_refs"] == [f"data/subagents/{loaded.id}/data_collection.md"]
    assert loaded.attributes["output_files"] == [f"data/subagents/{loaded.id}/summary.md"]
    assert loaded.attributes["output_ref_rebindings"][0]["from"].startswith("data/subagents/")
