from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS, error_contract
from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS
from agent_py_agent.agent.subagents.context_bundle_contracts import (
    allowed_write_roots,
    output_contract,
)
from agent_py_agent.agent.subagents.model_task import SubAgentTask
from agent_py_agent.agent.subagents.services.output_alignment import (
    OUTPUT_TARGET_LOCKED_CODE,
    OUTPUT_TARGET_NOT_WRITABLE_CODE,
    anchored_output_refs,
)
from agent_py_agent.agent.subagents.services.runner_context_service import (
    SubAgentRunnerContextService,
    _model_allowed_write_roots,
)
from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.tooling.write_boundary import (
    validate_write_boundary,
    write_boundary_error_code,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROOT = _REPO_ROOT / "agent_py_agent" / "agent"
_SCAN_DIRS = (
    "business_spaces",
    "gateway_parts",
    "conversation",
    "workflows",
    "user_space",
    "plugin_host",
    "assets",
)
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
_NON_ERROR_STATUSES = frozenset(
    {
        "UNKNOWN_ERROR",
        "GATEWAY_READY",
        "GATEWAY_STARTING",
        "TOOL_CALL",
        "TAKEN_OVER",
        "CHILD_RUNNING",
        "PENDING_INPUT",
        "CANONICAL_PAGE",
        "CONTENT_TEXT",
        "PLAIN_TEXT",
        "REASON_TEXT",
        "MARKDOWN_TEXT",
        "WORKFLOW_RUNS",
        "WORKFLOW_STEP_ATTEMPTS",
        "AGENT_TASKS",
    }
)


def _extract_domain_error_codes() -> set[str]:
    found: set[str] = set()
    files = [
        *(p for d in _SCAN_DIRS for p in sorted((_AGENT_ROOT / d).rglob("*.py"))),
        _AGENT_ROOT / "runtime_errors.py",
    ]
    for path in files:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in ("code", "error_code", "reason_code", "access_code", "error"):
                        for sub in ast.walk(kw.value):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                val = sub.value
                                if (
                                    _ERROR_CODE_RE.match(val)
                                    and "_" in val
                                    and not val.endswith("_")
                                    and val not in _NON_ERROR_STATUSES
                                ):
                                    found.add(val)
                fn_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                )
                if any(word in fn_name for word in ("Error", "Refusal", "error", "fail", "deny", "reject")):
                    for arg in node.args[:3]:
                        for sub in ast.walk(arg):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                val = sub.value
                                if (
                                    _ERROR_CODE_RE.match(val)
                                    and "_" in val
                                    and not val.endswith("_")
                                    and val not in _NON_ERROR_STATUSES
                                ):
                                    found.add(val)
            if isinstance(node, ast.Dict):
                for k_node, v_node in zip(node.keys, node.values):
                    if isinstance(k_node, ast.Constant) and k_node.value in (
                        "code",
                        "error_code",
                        "error",
                        "reason_code",
                    ):
                        for sub in ast.walk(v_node):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                val = sub.value
                                if (
                                    _ERROR_CODE_RE.match(val)
                                    and "_" in val
                                    and not val.endswith("_")
                                    and val not in _NON_ERROR_STATUSES
                                ):
                                    found.add(val)
            if isinstance(node, ast.Assign):
                targets_txt = " ".join(ast.unparse(t) for t in node.targets)
                is_error_target = any(
                    isinstance(t, ast.Name) and t.id in ("code", "error_code", "reason_code", "err_code")
                    for t in node.targets
                ) or any(
                    word in targets_txt.upper()
                    for word in ("ERROR", "CODE", "FAIL", "DENY", "STATUS", "MAP", "REASON")
                )
                if is_error_target:
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            val = sub.value
                            if (
                                _ERROR_CODE_RE.match(val)
                                and "_" in val
                                and not val.endswith("_")
                                and val not in _NON_ERROR_STATUSES
                            ):
                                found.add(val)
    return found


def test_biz006_all_domain_error_codes_registered_in_taxonomy() -> None:
    extracted = _extract_domain_error_codes()
    assert len(extracted) > 500
    missing = [code for code in sorted(extracted) if error_contract(code).code == "UNKNOWN_ERROR"]
    assert missing == [], f"Unregistered error codes fell back to UNKNOWN_ERROR: {missing[:20]}"

    required_spot_codes = (
        "MODEL_PROFILE_NOT_FOUND",
        "MODEL_PROFILE_UNAVAILABLE",
        "MODEL_PROFILE_REQUIRED",
        "OUTCOME_UNKNOWN",
        "OUTPUT_TARGET_NOT_WRITABLE",
        "NETWORK_PORT_INVALID",
        "NETWORK_SCHEME_BLOCKED",
        "PLUGIN_CLEANUP_UNCONFIRMED",
        "RUN_OUTCOME_UNKNOWN",
        "MEMORY_CONTROL_RESULT_UNKNOWN",
        "UNASSIGNED_TASK_LINEAGE_CYCLE",
    )
    for code in required_spot_codes:
        contract = error_contract(code)
        assert contract.code == code
        assert contract.category and contract.category != "unknown"
        assert contract.recommended_action in RECOVERY_ACTIONS
        assert contract.recovery_hint

    assert ERROR_CONTRACTS["OUTCOME_UNKNOWN"].retryable is False
    assert ERROR_CONTRACTS["RUN_OUTCOME_UNKNOWN"].retryable is False
    assert ERROR_CONTRACTS["MEMORY_CONTROL_RESULT_UNKNOWN"].retryable is False
    assert ERROR_CONTRACTS["WRITE_FORBIDDEN"].recommended_action == "request_permission"

    for sample_code in (
        "MODEL_PROFILE_NOT_FOUND",
        "MODEL_PROFILE_UNAVAILABLE",
        "OUTCOME_UNKNOWN",
        "OUTPUT_TARGET_NOT_WRITABLE",
        "AGENTS_WRITE_CROSS_OWNER_FORBIDDEN",
        "WORKFLOW_CANCELLED",
        "PLUGIN_CLEANUP_UNCONFIRMED",
    ):
        outcome = ToolHandlerOutcome("sample_tool", False, "failed", error_code=sample_code)
        assert outcome.error_code == sample_code
        assert outcome.recommended_action in RECOVERY_ACTIONS


def test_pd3_output_target_not_writable_warning_and_non_widening(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    extra_roots = [tmp_path / f"extra_{idx}" for idx in range(6)]
    for root in extra_roots:
        root.mkdir()
    outside_dir = tmp_path / "parent_only_dir"
    outside_dir.mkdir()
    outside_target = outside_dir / "final_report.md"
    locked_dir = ws / "locked_zone"
    locked_dir.mkdir()
    locked_target = locked_dir / "nested.md"
    valid_target = ws / "deliverable.md"

    original_allowed = [str(root) for root in extra_roots]
    task = SubAgentTask(
        id="task-pd3-out",
        goal="produce report",
        thought="",
        plan=[],
        role="worker",
        task_workspace_dir=str(ws),
        allowed_write_roots=list(original_allowed),
        forbidden_write_roots=[str(locked_dir)],
        attributes={
            "output_files": [str(outside_target), str(valid_target), str(locked_target)],
            "output_refs": [str(outside_target)],
        },
    )

    anchoring = anchored_output_refs(task)
    not_writable = [w for w in anchoring.warnings if w.get("code") == OUTPUT_TARGET_NOT_WRITABLE_CODE]
    assert len(not_writable) == 2  # outside_target (deduped across output_files/output_refs) + locked_target

    outside_warning = next(w for w in not_writable if w["target_path"] == outside_target.resolve().as_posix())
    assert outside_warning["field"] == "output_files"
    assert outside_warning["ref"] == str(outside_target)
    assert outside_warning["side_effect"] == "none"
    assert outside_warning["recommended_action"] == "use_writable_child_workspace_or_parent_integration"
    assert len(outside_warning["writable_roots"]) == 5
    assert outside_warning["writable_roots"][0] == ws.resolve().as_posix()
    assert all(isinstance(p, str) and p.startswith("/") for p in outside_warning["writable_roots"])
    assert "不在" in outside_warning["message_zh"] and "可写范围" in outside_warning["message_zh"]

    # Non-widening guarantees: task roots and runtime write boundary MUST NOT expand.
    assert task.allowed_write_roots == original_allowed
    assert str(outside_dir) not in allowed_write_roots(task)
    assert str(outside_dir.resolve()) not in _model_allowed_write_roots(task, [], [])

    contract = output_contract(task)
    assert any(w.get("code") == OUTPUT_TARGET_NOT_WRITABLE_CODE for w in contract["write_contract_warnings"])

    manager = SimpleNamespace(
        workspace_root=ws,
        owner_scope_root="",
        _owner_workspace_dir=ws,
        _resolve_artifact_backup_root=lambda: None,
    )
    boundary = SubAgentRunnerContextService(manager)._build_write_boundary(task)
    assert str(outside_dir.resolve()) not in boundary["allowed_write_roots"]
    denial = validate_write_boundary(
        "write_file",
        {"path": str(outside_target)},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert write_boundary_error_code(denial) == "WRITE_FORBIDDEN"

    # Relative path without execution_cwd stays warning-free.
    rel_task = SubAgentTask(
        id="task-pd3-rel",
        goal="rel",
        thought="",
        plan=[],
        role="worker",
        attributes={"output_files": ["app/main.py"]},
    )
    assert anchored_output_refs(rel_task).warnings == []

    # Locked files continue emitting OUTPUT_TARGET_LOCKED cleanly alongside non-writable check.
    locked_file_task = SubAgentTask(
        id="task-pd3-locked",
        goal="locked",
        thought="",
        plan=[],
        role="worker",
        task_workspace_dir=str(ws),
        locked_files=[str(valid_target)],
        attributes={"output_files": [str(valid_target)]},
    )
    codes = {w["code"] for w in anchored_output_refs(locked_file_task).warnings}
    assert OUTPUT_TARGET_LOCKED_CODE in codes


def test_pd3_write_forbidden_guidance_across_boundary_and_write_tools(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    roots = [tmp_path / f"root_{idx}" for idx in range(7)]
    for root in roots:
        root.mkdir()
    outside = tmp_path / "outside" / "blocked.txt"
    outside.parent.mkdir()
    outside.write_text("original\n", encoding="utf-8")

    # 1. validate_write_boundary with 7 allowed roots -> caps at 5, surfaces recommended_action & details.
    boundary = {
        "allowed_write_roots": [str(r) for r in roots],
        "forbidden_write_roots": [str(ws / "forbid")],
    }
    err = validate_write_boundary(
        "write_file",
        {"path": str(outside)},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert write_boundary_error_code(err) == "WRITE_FORBIDDEN"
    assert "allowed=" in err
    assert "recommended_action=repair_tool_arguments" in err
    assert "side_effect=not_started" in err
    assert err.details["denied_path"] == outside.resolve().as_posix()
    assert err.details["allowed_roots"] == [r.resolve().as_posix() for r in roots[:5]]
    assert err.details["total_allowed_roots"] == 7
    assert err.details["side_effect"] == "not_started"
    assert err.details["recommended_action"] == "repair_tool_arguments"
    assert err.details["retryable"] is False

    # Empty allowed_write_roots -> allowed=none
    empty_err = validate_write_boundary(
        "write_file",
        {"path": str(ws / "x.txt")},
        workspace_root=ws,
        write_boundary={"allowed_write_roots": []},
    )
    assert write_boundary_error_code(empty_err) == "WRITE_FORBIDDEN"
    assert "allowed=none" in empty_err
    assert "recommended_action=repair_tool_arguments" in empty_err

    # 2. Direct WriteFileTool / EditFileTool / ApplyPatchTool WriteScopeError normalization & envelope
    access = FileSystemAccessOptions(owner_scope_root=str(tmp_path))
    tool_kwargs = dict(workspace_root=ws, workspace_roots=roots[:6], access_options=access)

    write_tool = WriteFileTool(
        workspace_root=ws,
        workspace_roots=roots[:6],
        options=WriteFileToolOptions(access_options=access),
    )
    write_outcome = write_tool.execute({"path": str(outside), "content": "new\n"})
    assert write_outcome.ok is False
    assert write_outcome.error_code == "WRITE_FORBIDDEN"
    assert write_outcome.effect_outcome == "not_started"
    assert write_outcome.retryable is False
    assert write_outcome.recommended_action == "request_permission"
    assert write_outcome.next_action == "repair_tool_arguments"
    assert "allowed=" in write_outcome.output
    assert "recommended_action=repair_tool_arguments" in write_outcome.output
    assert len(write_outcome.result_envelope["allowed_roots"]) == 5
    assert write_outcome.result_envelope["recommended_action"] == "repair_tool_arguments"
    assert write_outcome.result_envelope["error_details"]["side_effect"] == "not_started"
    assert write_outcome.result_envelope["error_details"]["recommended_action"] == "repair_tool_arguments"
    assert write_outcome.result_envelope["error_details"]["denied_path"] == outside.resolve().as_posix()

    edit_tool = EditFileTool(**tool_kwargs)
    edit_outcome = edit_tool.execute({"path": str(outside), "old_string": "original", "new_string": "changed"})
    assert edit_outcome.ok is False
    assert edit_outcome.error_code == "WRITE_FORBIDDEN"
    assert edit_outcome.effect_outcome == "not_started"
    assert edit_outcome.retryable is False
    assert edit_outcome.next_action == "repair_tool_arguments"
    assert edit_outcome.result_envelope["recommended_action"] == "repair_tool_arguments"
    assert len(edit_outcome.result_envelope["allowed_roots"]) == 5
    assert edit_outcome.result_envelope["denied_path"] == outside.resolve().as_posix()

    patch_tool = ApplyPatchTool(**tool_kwargs)
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {outside}\n"
        "-original\n"
        "+changed\n"
        "*** End Patch\n"
    )
    patch_outcome = patch_tool.execute({"patch": patch_text})
    assert patch_outcome.ok is False
    assert patch_outcome.error_code == "WRITE_FORBIDDEN"
    assert patch_outcome.effect_outcome == "not_started"
    assert patch_outcome.retryable is False
    assert patch_outcome.next_action == "repair_tool_arguments"
    assert patch_outcome.result_envelope["recommended_action"] == "repair_tool_arguments"
    assert len(patch_outcome.result_envelope["allowed_roots"]) == 5
    assert patch_outcome.result_envelope["error_details"]["denied_path"] == outside.resolve().as_posix()
    assert outside.read_text(encoding="utf-8") == "original\n"

    # ARTIFACT_REF_AS_WRITE_PATH must stay preserved (not converted to WRITE_FORBIDDEN).
    art_outcome = write_tool.execute({"path": "run-123:call_123", "content": "x"})
    assert art_outcome.error_code == "ARTIFACT_REF_AS_WRITE_PATH"

    # 3. ToolExecutor end-to-end envelope propagation when write_tool hits WriteScopeError
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=ws,
            max_chars=4000,
            max_entries=100,
            max_matches=50,
            web_max_chars=4000,
            http_timeout=10,
            catalog_limit=10,
            retrieval_limit=5,
            vector_search_enabled=False,
            workspace_roots=roots[:6],
            owner_scope_root=str(tmp_path),
            operation_store_required=False,
        )
    )
    snapshot = registry.runtime_snapshot(run_id="run-1")
    write_hash = next(spec.schema_hash for spec in registry.specs_for_snapshot(snapshot) if spec.name == "write_file")
    execution = registry.execute_tool(
        ToolCall(
            call_id="call-w3",
            tool_name="write_file",
            arguments={"path": str(outside), "content": "x"},
            source_protocol="native",
            schema_hash=write_hash,
            run_id="run-1",
            turn_id="turn-1",
            attempt_id="attempt-1",
        ),
        write_boundary=None,
        runtime_snapshot=snapshot,
    )
    assert execution.result.status != "ok"
    assert execution.result.error_code == "WRITE_FORBIDDEN"
    assert execution.result.metadata["next_action"] == "repair_tool_arguments"
    assert execution.result.metadata["error_details"]["recommended_action"] == "repair_tool_arguments"
    assert len(execution.result.metadata["error_details"]["allowed_roots"]) == 5
    assert execution.result.metadata["error_details"]["side_effect"] == "not_started"
