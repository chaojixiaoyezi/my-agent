from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import CapabilityGrant


def test_task_address_v1_captures_lineage_and_workspace_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.protocol import build_task_address

    manager, root, child, leaf = _make_protocol_tree(tmp_path)

    address = build_task_address(leaf, all_tasks=manager.list_runs())

    assert address.schema_version == "subagent_task_address.v1"
    assert address.run_id == leaf.id
    assert address.root_id == root.id
    assert address.parent_id == child.id
    assert address.depth == 2
    assert address.lineage == [root.id, child.id, leaf.id]
    assert address.workspace_ref.endswith(leaf.id)


def test_task_envelope_v1_contains_address_tool_write_and_acceptance(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.protocol import build_task_envelope

    manager, root, _child, leaf = _make_protocol_tree(tmp_path)
    leaf.goal = "写 index.html，必须放在 build 目录"
    leaf.allowed_tools = ["read_file", "write_file", "controlled_exec"]
    leaf.allowed_write_roots = [str(tmp_path / "build")]
    leaf.forbidden_write_roots = ["/System"]
    leaf.acceptance_checks = ["index.html 存在", "页面没有空链接"]
    leaf.capability_grants = [
        CapabilityGrant(
            id="grant-exec",
            request_id="req-exec",
            grant_to_run_id=leaf.id,
            tools=["controlled_exec"],
            path_scope=[str(tmp_path / "build")],
        )
    ]
    manager.save(leaf)

    envelope = build_task_envelope(manager.load(leaf.id), all_tasks=manager.list_runs())
    payload = envelope.to_dict()

    assert payload["schema_version"] == "subagent_task_envelope.v1"
    assert payload["address"]["lineage"] == [root.id, _child.id, leaf.id]
    assert payload["goal"] == leaf.goal
    assert payload["tool_contract"]["allowed_tools"] == ["read_file", "write_file", "controlled_exec"]
    assert payload["tool_contract"]["controlled_exec_grant_ids"] == ["grant-exec"]
    assert payload["write_contract"]["product_write_roots"] == [str(tmp_path / "build")]
    assert payload["write_contract"]["allowed_write_roots"] == [str(tmp_path / "build")]
    assert payload["write_contract"]["forbidden_write_roots"] == ["/System"]
    assert payload["acceptance"]["checks"] == ["index.html 存在", "页面没有空链接"]


def test_task_envelope_write_contract_includes_granted_filesystem_roots(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.protocol import build_task_envelope

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="写页面到 lab_outputs/site-output/index.html",
        thought="等待父级授权产物目录",
        plan=["写 HTML"],
        role="worker",
    )
    product_root = tmp_path / "lab_outputs"
    task.acceptance_checks = ["index.html 存在"]
    task.capability_grants = [
        CapabilityGrant(
            id="grant-write",
            request_id="req-write",
            grant_to_run_id=task.id,
            tools=["write_file"],
            path_scope=[str(product_root)],
        )
    ]
    manager.save(task)

    payload = build_task_envelope(manager.load(task.id), all_tasks=manager.list_runs()).to_dict()

    assert payload["write_contract"]["allowed_write_roots"] == [
        task.task_dir,
        str(product_root),
    ]
    assert payload["write_contract"]["product_write_roots"] == [str(product_root)]


def test_protocol_validation_reports_structured_errors(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.protocol import build_task_envelope, validate_task_envelope

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="", thought="", plan=[], role="worker")
    task.acceptance_checks = []
    manager.save(task)

    report = validate_task_envelope(build_task_envelope(manager.load(task.id), all_tasks=manager.list_runs()))

    assert report.ok is False
    assert [issue.code for issue in report.issues] == [
        "missing_goal",
        "missing_acceptance_checks",
    ]
    assert report.issues[0].kind == "ProtocolError"


def test_tool_preflight_reports_missing_tool_and_write_root_without_stripping_basics(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.protocol import build_task_envelope
    from agent_py_agent.agent.subagents.protocol_preflight import run_tool_preflight

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="写 build/index.html", thought="", plan=["写文件"], role="worker")
    task.allowed_tools = ["read_file", "write_file", "controlled_exec"]
    task.acceptance_checks = ["build/index.html 存在"]
    manager.save(task)

    result = run_tool_preflight(
        build_task_envelope(manager.load(task.id), all_tasks=manager.list_runs()),
        available_tools={"read_file", "write_file"},
    )

    assert result.ok is False
    assert [issue.code for issue in result.issues] == [
        "missing_allowed_tool",
        "missing_allowed_write_roots",
        "controlled_exec_grant_missing",
    ]
    assert all(issue.kind == "ToolContractError" for issue in result.issues)
    assert result.effective_tools == ["read_file", "write_file"]


def test_recovery_strategy_exports_address_and_envelope_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services.recovery.strategy import (
        SubagentRecoveryStrategyRequest,
        build_subagent_recovery_strategy,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="继续写页面", thought="", plan=["续写"], role="worker")
    task.status = "TIMEOUT"
    task.failure_type = "runner_timeout"
    task.acceptance_checks = ["index.html 存在"]
    reports = Path(task.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    checkpoint = reports / "checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    task.agent_run_checkpoint_json = str(checkpoint)
    manager.save(task)

    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=manager.load(task.id), all_tasks=manager.list_runs())
    ).to_dict()

    assert strategy["address"]["run_id"] == task.id
    assert strategy["task_envelope"]["address"]["run_id"] == task.id
    assert strategy["task_envelope"]["acceptance"]["checks"] == ["index.html 存在"]
    assert strategy["recommended_action"] == "takeover"
    assert strategy["recovery_mode"] == "takeover_from_continue_packet"


def test_recovery_strategy_does_not_close_completed_alias(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services.recovery.strategy import (
        SubagentRecoveryStrategyRequest,
        build_subagent_recovery_strategy,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="继续写页面", thought="", plan=["续写"], role="worker")
    task.status = "COMPLETED"
    manager.save(task)

    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=manager.load(task.id), all_tasks=manager.list_runs())
    ).to_dict()

    assert strategy["recovery_mode"] == "manual_review_missing_recovery_refs"
    assert strategy["recommended_action"] == "manual_review"


def _make_protocol_tree(tmp_path: Path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="", plan=["派工"], role="coordinator")
    child = manager.create_run(
        goal="child",
        thought="",
        plan=["继续派工"],
        role="coordinator",
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )
    leaf = manager.create_run(
        goal="leaf",
        thought="",
        plan=["写文件"],
        role="worker",
        parent_id=child.id,
        root_id=root.id,
        depth=2,
    )
    root.child_ids = [child.id]
    child.child_ids = [leaf.id]
    for task in [root, child, leaf]:
        manager.save(task)
    return manager, root, child, leaf
