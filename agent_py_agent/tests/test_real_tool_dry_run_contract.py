from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling.models import ToolExecutionResult
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=200,
        )
    )


def test_real_tool_dry_run_accepts_actual_read_only_and_dry_run_wrapper_results(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_tool_dry_run_contract import (
        validate_real_tool_dry_run_probes,
    )

    (tmp_path / "input.txt").write_text("hello from real read_file wrapper\n", encoding="utf-8")
    registry = _registry(tmp_path)
    read_result = registry.execute_call(
        {"tool": "read_file", "path": "input.txt"},
        allowed_tools=["read_file"],
    )
    dry_run_result = registry.execute_call(
        {"tool": "controlled_exec", "command": "pwd", "apply": False, "cwd": "."},
        allowed_tools=["controlled_exec"],
        write_boundary={
            "controlled_exec_grants": [
                _controlled_exec_grant(tmp_path),
            ]
        },
    )

    assert read_result.ok is True
    assert dry_run_result.ok is True
    result = validate_real_tool_dry_run_probes((_read_probe(read_result), _controlled_exec_probe(dry_run_result)))

    assert result.ok is True
    assert result.error_codes == ()


def _controlled_exec_grant(root: Path) -> dict[str, object]:
    return {
        "grant_id": "grant-real-dry-run",
        "request_id": "req-real-dry-run",
        "run_id": "run-real-dry-run",
        "command_allowlist": ["pwd"],
        "path_scope": [str(root)],
        "network_scope": [],
        "output_budget": {"stdout_bytes": 200, "stderr_bytes": 200},
        "risk_level": "low",
    }


def _read_probe(read_result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "read-file-success",
        "operation_id": "op-read-file-success",
        "tool": read_result.tool,
        "effect": "read_only",
        "mode": "read_only",
        "outcome": "success",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/read_file/result",
        "executed_actions": [],
        "result": {
            "ok": read_result.ok,
            "payload": {"output": read_result.output, "truncated": False},
        },
    }


def _controlled_exec_probe(dry_run_result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "controlled-exec-dry-run",
        "operation_id": "op-controlled-exec-dry-run",
        "tool": dry_run_result.tool,
        "effect": "dangerous",
        "mode": "dry_run",
        "outcome": "dry_run_success",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/controlled_exec/result",
        "idempotency_key": "idem-controlled-exec-pwd",
        "args_hash": "sha256:controlled-exec-pwd",
        "executed_actions": [],
        "result": {
            "ok": dry_run_result.ok,
            "payload": json.loads(dry_run_result.output),
        },
    }


def test_real_tool_dry_run_rejects_unwrapped_or_real_side_effect_results() -> None:
    from agent_py_agent.agent.contracts.real_tool_dry_run_contract import (
        validate_real_tool_dry_run_probes,
    )

    result = validate_real_tool_dry_run_probes(
        (
            {
                "probe_id": "direct-sdk-read",
                "operation_id": "op-direct-sdk-read",
                "tool": "read_file",
                "effect": "read_only",
                "mode": "read_only",
                "outcome": "success",
                "tool_executor_ref": "direct_sdk",
                "result_schema_ref": "schema://tools/read_file/result",
                "executed_actions": ["external_read"],
                "result": {"ok": True, "payload": {"output": "inline"}},
            },
            {
                "probe_id": "controlled-exec-real-run",
                "operation_id": "op-controlled-exec-real-run",
                "tool": "controlled_exec",
                "effect": "dangerous",
                "mode": "dry_run",
                "outcome": "dry_run_success",
                "tool_executor_ref": "tool_registry.execute_call",
                "result_schema_ref": "schema://tools/controlled_exec/result",
                "idempotency_key": "",
                "args_hash": "",
                "executed_actions": ["execute_shell"],
                "result": {
                    "ok": True,
                    "payload": {"mode": "execute", "allowed": True, "action": "execute_shell"},
                },
            },
        )
    )

    assert result.error_codes == (
        "REAL_TOOL_EXECUTOR_REF_UNTRUSTED",
        "REAL_TOOL_READ_ONLY_HAS_SIDE_EFFECTS",
        "REAL_TOOL_DRY_RUN_MODE_MISMATCH",
        "REAL_TOOL_SIDE_EFFECT_EXECUTED",
        "REAL_TOOL_IDEMPOTENCY_KEY_MISSING",
        "REAL_TOOL_ARGS_HASH_MISSING",
    )
