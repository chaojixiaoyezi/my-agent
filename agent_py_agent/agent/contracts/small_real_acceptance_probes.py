
from __future__ import annotations

import json
from pathlib import Path

from ..tooling.models import ToolExecutionResult
from ..tooling.registry import ToolRegistry, ToolRegistryParams


def small_real_registry(root: Path) -> ToolRegistry:
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


def read_file_probe(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "read-file-success",
        "operation_id": "op-read-file-success",
        "tool": result.tool,
        "effect": "read_only",
        "mode": "read_only",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/read_file/result",
        "executed_actions": [],
        "result": {"ok": result.ok, "payload": {"output": result.output}},
    }


def controlled_exec_probe(result: ToolExecutionResult) -> dict[str, object]:
    return {
        "probe_id": "controlled-exec-dry-run",
        "operation_id": "op-controlled-exec-dry-run",
        "tool": result.tool,
        "effect": "dangerous",
        "mode": "dry_run",
        "tool_executor_ref": "tool_registry.execute_call",
        "result_schema_ref": "schema://tools/controlled_exec/result",
        "idempotency_key": "idem-controlled-exec-pwd",
        "args_hash": "sha256:controlled-exec-pwd",
        "executed_actions": [],
        "result": {"ok": result.ok, "payload": json_payload(result.output)},
    }


def shadow_runtime_facts(
    probe_refs: list[dict[str, object]],
    comparison_ref: str,
) -> dict[str, object]:
    return {
        "run_id": "shadow-run-small-real",
        "stage": "shadow_mode",
        "mode": "shadow",
        "real_tool_probe_refs": list(probe_refs),
        "comparison_artifacts": [{"artifact_ref": f"artifact://{comparison_ref}", "exists": True}],
        "shadow_facts": _shadow_facts(comparison_ref),
        "executed_actions": [],
    }


def controlled_exec_grant(root: Path) -> dict[str, object]:
    return {
        "grant_id": "grant-small-real",
        "request_id": "req-small-real",
        "run_id": "run-small-real",
        "command_allowlist": ["pwd"],
        "path_scope": [str(root)],
        "network_scope": [],
        "output_budget": {"stdout_bytes": 200, "stderr_bytes": 200},
        "risk_level": "low",
    }


def _shadow_facts(comparison_ref: str) -> dict[str, object]:
    return {
        "mode": "shadow",
        "risk": {"score": 50},
        "evidence_refs": [{"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": "tool://read-file-success"}],
        "recommended_actions": [_recommended_action(comparison_ref)],
        "dry_run_results": [{"action_id": "ACT-1", "mode": "dry_run", "ok": True, "result_ref": "artifact://shadow/dry-run.json"}],
        "executed_actions": [],
        "human_review": {
            "review_id": "HR-1",
            "review_ref": f"artifact://{comparison_ref}",
            "decision": "agree",
            "agreement": True,
        },
    }


def _recommended_action(comparison_ref: str) -> dict[str, object]:
    return {
        "action_id": "ACT-1",
        "effect": "dangerous",
        "mode": "dry_run",
        "operator_review_ref": f"artifact://{comparison_ref}",
        "approval_draft_ref": "artifact://shadow/approval.json",
    }


def json_payload(value: str) -> dict[str, object]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {"mode": "", "raw_parse_failed": True}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


__all__ = [
    "controlled_exec_grant",
    "controlled_exec_probe",
    "read_file_probe",
    "shadow_runtime_facts",
    "small_real_registry",
]
