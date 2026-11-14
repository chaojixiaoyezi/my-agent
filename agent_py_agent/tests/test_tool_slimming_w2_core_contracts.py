"""W2 core tool contracts: partial-patch evidence, pre-effect refusals, nested schemas, and sibling hints."""
from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    _TASK_PROGRESS_SHARED_ENTRY_DESCRIPTIONS,
    build_create_subagents_model_spec,
)
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    TOOL_OPERATION_FAILED,
    TOOL_OPERATION_UNKNOWN,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.agent.tooling import (
    _filesystem_edit as fs_edit_module,
)
from agent_py_agent.agent.tooling import (
    _filesystem_patch as fs_patch_module,
)
from agent_py_agent.agent.tooling import (
    _filesystem_write as fs_write_module,
)
from agent_py_agent.agent.tooling import (
    process_sessions as process_sessions_module,
)
from agent_py_agent.agent.tooling._filesystem_write import (
    _atomic_write_bytes as real_atomic_write_bytes,
)
from agent_py_agent.agent.tooling.content_transport_policy import InlineContentPolicyDecision
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanupError
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_registry_test_call,
)


def _make_registry(
    workspace: Path,
    *,
    store: LocalStore,
    protected_persona_root: Path | None = None,
    max_inline_content_chars: int = 0,
    owner_scope_root: str = "",
) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=10_000,
            max_entries=100,
            max_matches=100,
            web_max_chars=10_000,
            http_timeout=1,
            catalog_limit=100,
            retrieval_limit=10,
            vector_search_enabled=False,
            owner_scope_root=owner_scope_root,
            operation_store=store,
            operation_store_required=True,
            operation_owner_id="owner-w2",
            protected_persona_root=str(protected_persona_root) if protected_persona_root else "",
            tool_write_inline_max_chars=max_inline_content_chars or 20_000,
        )
    )


def _schema_description_gaps(schema: dict[str, Any]) -> list[str]:
    gaps: list[str] = []

    def walk(
        node: Any,
        path: str,
        inherited: dict[str, Any] | None = None,
        seen: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target: Any = schema
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            walk(target, path, inherited, (*seen, ref))
        props = node.get("properties", {})
        parent = inherited or {}
        for name, child in props.items():
            effective = child
            if isinstance(child, dict) and isinstance(child.get("$ref"), str) and child["$ref"].startswith("#/"):
                target = schema
                for part in child["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                effective = {**target, **child}
            description = (
                effective.get("description")
                if isinstance(effective, dict)
                else None
            ) or parent.get(name, {}).get("description")
            if not isinstance(description, str) or not description.strip():
                gaps.append(f"{path}.{name}")
            elif description.strip() == name:
                gaps.append(f"{path}.{name}:generic_name_echo")
            walk(child, f"{path}.{name}", seen=seen)
        for branch_key in ("oneOf", "anyOf", "allOf"):
            for index, child in enumerate(node.get(branch_key, [])):
                walk(child, f"{path}/{branch_key}/{index}", {**parent, **props}, seen)
        for nested_key in ("items", "additionalProperties"):
            if isinstance(node.get(nested_key), dict):
                walk(node[nested_key], f"{path}/{nested_key}", seen=seen)

    walk(schema, "$")
    return sorted(set(gaps))


def test_partial_patch_single_io_error_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = workspace / "first.py"
    second = workspace / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("count = 10\n", encoding="utf-8")

    store = LocalStore(tmp_path / "ops.db", enable_fts=False)
    registry = _make_registry(workspace, store=store)
    write_attempts = {"second": 0}

    def fail_second_before_publish(target: Path, data: bytes, **kwargs: Any) -> Any:
        if target == second:
            write_attempts["second"] += 1
            raise OSError(errno.EIO, "simulated I/O error on second target")
        return real_atomic_write_bytes(target, data, **kwargs)

    monkeypatch.setattr(fs_patch_module, "_atomic_write_bytes", fail_second_before_publish)
    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: first.py\n"
        "-value = 1\n"
        "+value = 2\n"
        "*** Update File: second.py\n"
        "-count = 10\n"
        "+count = 20\n"
        "*** End Patch\n"
    )
    try:
        snapshot = registry.runtime_snapshot(run_id="run-partial")
        call = canonical_test_call(
            snapshot,
            "apply_patch",
            {"patch": patch_body},
            operation_id="op-partial-proven",
            idempotency_key="idem-partial-proven",
        )
        result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result
        assert result.ok is False
        assert result.error_code == "TOOL_PARTIAL_COMMIT"
        assert result.metadata.get("reported_error_code") == "TOOL_EXECUTION_FAILED"
        assert result.effect_outcome == "failed"
        assert result.retryable is False
        assert first.read_text(encoding="utf-8") == "value = 2\n"
        assert second.read_text(encoding="utf-8") == "count = 10\n"

        envelope = result.metadata["handler_details"]
        assert envelope["partial_commit"] is True
        assert envelope["files_modified"] == ["first.py"]
        assert envelope["failed_path"] == "second.py"
        assert envelope["failed_path_effect"] == "none"
        assert envelope["artifact_refs"] == [
            {"path": str(first.resolve()), "kind": "file", "status": "ready"}
        ]
        assert [ref.ref for ref in result.refs] == [str(first.resolve())]
        next_action = str(result.metadata.get("next_action") or "")
        assert "read_file" in next_action
        assert "files_modified" in next_action and "failed_path" in next_action
        assert "禁止原样重提整份补丁" in next_action

        stored_op = store.get_tool_operation(
            owner_id="owner-w2",
            run_id="run-partial",
            operation_id="op-partial-proven",
        )
        assert stored_op is not None and stored_op.status == TOOL_OPERATION_FAILED

        # Durable replay must return the recorded partial-commit receipt without re-running the handler.
        replayed = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result
        assert write_attempts["second"] == 1
        assert replayed.ok is False
        assert replayed.operation is not None and replayed.operation.replayed is True
        assert replayed.metadata.get("replayed_terminal_mutation") is True
        assert replayed.error_code == "TOOL_PARTIAL_COMMIT"
        assert replayed.metadata.get("reported_error_code") == "TOOL_EXECUTION_FAILED"
        assert replayed.effect_outcome == "failed"
        assert replayed.retryable is False
        replayed_envelope = replayed.metadata["handler_details"]
        assert replayed_envelope["files_modified"] == ["first.py"]
        assert replayed_envelope["failed_path"] == "second.py"
        assert replayed_envelope["failed_path_effect"] == "none"
        assert "read_file" in str(replayed.metadata.get("next_action") or "")

        # Unproven second-target mutation: second file changes on disk before raising OSError -> unknown.
        alpha = workspace / "alpha.py"
        beta = workspace / "beta.py"
        alpha.write_text("a = 1\n", encoding="utf-8")
        beta.write_text("b = 1\n", encoding="utf-8")

        def fail_beta_after_mutation(target: Path, data: bytes, **kwargs: Any) -> Any:
            obs = real_atomic_write_bytes(target, data, **kwargs)
            if target == beta:
                raise OSError(errno.EIO, "simulated post-write I/O error")
            return obs

        monkeypatch.setattr(fs_patch_module, "_atomic_write_bytes", fail_beta_after_mutation)
        unknown_patch = (
            "*** Begin Patch\n"
            "*** Update File: alpha.py\n"
            "-a = 1\n"
            "+a = 2\n"
            "*** Update File: beta.py\n"
            "-b = 1\n"
            "+b = 2\n"
            "*** End Patch\n"
        )
        direct_unknown = fs_patch_module.ApplyPatchTool(workspace).execute({"patch": unknown_patch})
        assert direct_unknown.ok is False
        assert direct_unknown.error_code == "TOOL_PARTIAL_COMMIT"
        assert direct_unknown.reported_error_code == "TOOL_EXECUTION_FAILED"
        assert direct_unknown.effect_outcome == "unknown"
        assert direct_unknown.result_envelope["failed_path_effect"] == "unknown"

        # Reset alpha/beta for coordinator-tracked execution of the same unproven second-target case.
        alpha.write_text("a = 1\n", encoding="utf-8")
        beta.write_text("b = 1\n", encoding="utf-8")
        call_unknown = canonical_test_call(
            snapshot,
            "apply_patch",
            {"patch": unknown_patch},
            operation_id="op-partial-unknown",
            idempotency_key="idem-partial-unknown",
        )
        res_unknown = registry.execute_tool(
            call_unknown,
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_unknown.ok is False
        assert res_unknown.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert res_unknown.metadata.get("reported_error_code") == "TOOL_EXECUTION_FAILED"
        assert res_unknown.effect_outcome == "unknown"
        assert res_unknown.retryable is False
        unknown_envelope = res_unknown.metadata["handler_details"]
        assert unknown_envelope["files_modified"] == ["alpha.py"]
        assert unknown_envelope["failed_path"] == "beta.py"
        assert unknown_envelope["failed_path_effect"] == "unknown"

        unknown_op = store.get_tool_operation(
            owner_id="owner-w2",
            run_id="run-partial",
            operation_id="op-partial-unknown",
        )
        assert unknown_op is not None and unknown_op.status == TOOL_OPERATION_UNKNOWN
    finally:
        registry.close_mcp_clients()


def test_pre_effect_refusals_do_not_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    persona_root = workspace / "persona_guard"
    persona_root.mkdir()
    (persona_root / "SOUL.md").write_text("calm tone\n", encoding="utf-8")
    editable = workspace / "module.py"
    editable.write_text("flag = False\n", encoding="utf-8")

    store = LocalStore(tmp_path / "refusals.db", enable_fts=False)
    registry = _make_registry(
        workspace,
        store=store,
        protected_persona_root=persona_root,
        max_inline_content_chars=32,
    )
    run_scope = {
        "owner_id": "owner-w2",
        "session_id": "thread-w2",
        "root_task_id": "task-w2",
        "run_id": "run-refusal",
    }
    try:
        snapshot = registry.runtime_snapshot(run_id="run-refusal")

        def run_op(tool_name: str, args: dict[str, Any], op_suffix: str) -> tuple[Any, Any]:
            op_id = f"op-{op_suffix}"
            call = canonical_test_call(
                snapshot,
                tool_name,
                args,
                operation_id=op_id,
                idempotency_key=f"idem-{op_suffix}",
            )
            res = registry.execute_tool(
                call,
                write_boundary=None,
                runtime_snapshot=snapshot,
            ).result
            row = store.get_tool_operation(
                owner_id="owner-w2",
                run_id="run-refusal",
                operation_id=op_id,
            )
            return res, row

        # 1. edit_file: pre-write read OSError, persona approval refusal, and persona rule refusal -> not_started.
        real_read_bytes = Path.read_bytes

        def fail_editable_read(self: Path) -> bytes:
            if self == editable:
                raise OSError(errno.EIO, "simulated I/O error during pre-edit read")
            return real_read_bytes(self)

        with monkeypatch.context() as mp:
            mp.setattr(Path, "read_bytes", fail_editable_read)
            res_read_err, op_read_err = run_op(
                "edit_file",
                {"path": "module.py", "old_string": "False", "new_string": "True"},
                "edit-read-io",
            )
        assert res_read_err.error_code == "TOOL_EXECUTION_FAILED"
        assert res_read_err.effect_outcome == "not_started"
        assert op_read_err is not None and op_read_err.status == TOOL_OPERATION_FAILED

        res_edit_soul, op_edit_soul = run_op(
            "edit_file",
            {"path": "persona_guard/SOUL.md", "old_string": "calm", "new_string": "warm"},
            "edit-persona-guard",
        )
        assert res_edit_soul.error_code == "PERSONA_WRITE_REQUIRES_TOOL"
        assert res_edit_soul.effect_outcome == "not_started"
        assert op_edit_soul is not None and op_edit_soul.status == TOOL_OPERATION_FAILED

        with monkeypatch.context() as mp:
            mp.setattr(fs_edit_module, "_persona_injection_write_error", lambda *_a: "persona rule refusal")
            res_edit_rule, op_edit_rule = run_op(
                "edit_file",
                {"path": "module.py", "old_string": "False", "new_string": "True"},
                "edit-persona-rule",
            )
        assert res_edit_rule.error_code == "PERSONA_INJECTION_BLOCKED"
        assert res_edit_rule.effect_outcome == "not_started"
        assert op_edit_rule is not None and op_edit_rule.status == TOOL_OPERATION_FAILED

        # Post-write OSError in edit_file must NOT be mislabeled not_started.
        with monkeypatch.context() as mp:
            mp.setattr(
                fs_edit_module,
                "_atomic_write_bytes",
                lambda *_a, **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "simulated write-stage error")),
            )
            res_edit_write_err, op_edit_write_err = run_op(
                "edit_file",
                {"path": "module.py", "old_string": "False", "new_string": "True"},
                "edit-write-io",
            )
        assert res_edit_write_err.effect_outcome == "unknown"
        assert op_edit_write_err is not None and op_edit_write_err.status == TOOL_OPERATION_UNKNOWN

        # 2. write_file: content_policy, candidate artifact validation, system ledger refusal, and persona rule refusal -> not_started.
        res_write_candidate, op_write_candidate = run_op(
            "write_file",
            {"path": "report.pdf", "content": "binary extension via text content"},
            "write-candidate-invalid",
        )
        assert res_write_candidate.error_code == "ARTIFACT_VALIDATION_FAILED"
        assert res_write_candidate.effect_outcome == "not_started"
        assert op_write_candidate is not None and op_write_candidate.status == TOOL_OPERATION_FAILED

        with monkeypatch.context() as mp:
            mp.setattr(
                fs_write_module,
                "check_inline_write_content",
                lambda _req: InlineContentPolicyDecision(False, 64, 32, "inline transport policy refusal"),
            )
            res_write_policy, op_write_policy = run_op(
                "write_file",
                {"path": "policy_blocked.txt", "content": "blocked before target creation"},
                "write-policy",
            )
        assert res_write_policy.error_code == "ARTIFACT_VALIDATION_FAILED"
        assert res_write_policy.effect_outcome == "not_started"
        assert op_write_policy is not None and op_write_policy.status == TOOL_OPERATION_FAILED

        res_write_ledger, op_write_ledger = run_op(
            "write_file",
            {"path": "memory_archive/task_progress/progress.json", "content": "{}"},
            "write-ledger",
        )
        assert res_write_ledger.error_code == "SYSTEM_LEDGER_WRITE_BLOCKED"
        assert res_write_ledger.effect_outcome == "not_started"
        assert op_write_ledger is not None and op_write_ledger.status == TOOL_OPERATION_FAILED

        with monkeypatch.context() as mp:
            mp.setattr(fs_write_module, "_persona_injection_write_error", lambda *_a: "persona rule refusal")
            res_write_rule, op_write_rule = run_op(
                "write_file",
                {"path": "short.txt", "content": "ok"},
                "write-persona-rule",
            )
        assert res_write_rule.error_code == "PERSONA_INJECTION_BLOCKED"
        assert res_write_rule.effect_outcome == "not_started"
        assert op_write_rule is not None and op_write_rule.status == TOOL_OPERATION_FAILED

        with monkeypatch.context() as mp:
            mp.setattr(
                fs_write_module,
                "_atomic_write_bytes",
                lambda *_a, **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "simulated write-stage error")),
            )
            res_write_io, op_write_io = run_op(
                "write_file",
                {"path": "short.txt", "content": "ok"},
                "write-stage-io",
            )
        assert res_write_io.effect_outcome == "unknown"
        assert op_write_io is not None and op_write_io.status == TOOL_OPERATION_UNKNOWN

        # 3. process_session: pre-signal validation/lookup failures -> not_started; post-signal cleanup -> unknown.
        proc_ws = tmp_path / "proc_ws"
        proc_ws.mkdir(mode=0o700)
        proc_registry = _make_registry(proc_ws, store=store, owner_scope_root=str(proc_ws))
        try:
            process_tool = proc_registry.tools["process_session"]
            miss_id = process_tool.execute({"action": "stop", "__run_scope": run_scope})
            assert miss_id.error_code == "TOOL_INVALID_ARGUMENTS"
            assert miss_id.effect_outcome == "not_started"

            not_found = execute_registry_test_call(
                proc_registry,
                "process_session",
                {"action": "stop", "session_id": "bg-missing"},
                trusted_run_context={"run_scope": run_scope},
                run_id="run-refusal",
                operation_id="op-proc-not-found",
                idempotency_key="idem-proc-not-found",
            )
            assert not_found.error_code == "PROCESS_NOT_FOUND"
            assert not_found.effect_outcome == "not_started"
            op_proc_nf = store.get_tool_operation(
                owner_id="owner-w2",
                run_id="run-refusal",
                operation_id="op-proc-not-found",
            )
            assert op_proc_nf is not None and op_proc_nf.status == TOOL_OPERATION_FAILED

            with monkeypatch.context() as mp:
                mp.setattr(
                    process_sessions_module.process_registry,
                    "kill",
                    lambda *_a, **_kw: {"session_id": "bg-1", "termination": {"confirmed": False}},
                )
                unconfirmed = process_tool.execute(
                    {"action": "stop", "session_id": "bg-1", "__run_scope": run_scope}
                )
            assert unconfirmed.effect_outcome == "unknown"
            assert unconfirmed.reported_error_code == "PROCESS_STOP_UNCONFIRMED"

            with monkeypatch.context() as mp:
                mp.setattr(
                    process_sessions_module.process_registry,
                    "kill",
                    lambda *_a, **_kw: (_ for _ in ()).throw(
                        ProcessSessionCleanupError(
                        OSError("simulated store write error"),
                        {"session_id": "bg-2"},
                        (),
                        True,
                    )
                    ),
                )
                cleanup_err = process_tool.execute(
                    {"action": "stop", "session_id": "bg-2", "__run_scope": run_scope}
                )
            assert cleanup_err.effect_outcome == "unknown"
        finally:
            proc_registry.close_mcp_clients()

        # 4. create_subagents & resolve_capability_requests pre-effect refusals vs post-decision persistence.
        agent_root = tmp_path / "agent_ws"
        agent_root.mkdir()
        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "agent_home"),
                enable_tools=True,
                enable_subagents=False,
            ),
            agent_root,
        )
        create_tool = agent.tools.tools["create_subagents"]
        disabled_out = create_tool.execute({"items": [{"goal": "inspect files"}]})
        assert disabled_out.error_code == "TOOL_UNAVAILABLE"
        assert disabled_out.effect_outcome == "not_started"

        agent.config.enable_subagents = True
        bad_items_out = create_tool.execute({"items": []})
        assert bad_items_out.error_code == "TOOL_INVALID_ARGUMENTS"
        assert bad_items_out.effect_outcome == "not_started"

        cap_tool = ResolveCapabilityRequestsTool(agent)
        pre_persist_missing = cap_tool.execute({"run_id": "", "decision": "grant"})
        assert pre_persist_missing.error_code == "TOOL_PARAMETER_REQUIRED"
        assert pre_persist_missing.effect_outcome == "not_started"

        pre_persist_unknown_run = cap_tool.execute(
            {"run_id": "run-nonexistent", "decision": "grant", "reason": "check"}
        )
        assert pre_persist_unknown_run.error_code == "TOOL_INVALID_ARGUMENTS"
        assert pre_persist_unknown_run.effect_outcome == "not_started"

        child_task = agent.subagents.create_run(
            goal="write module report",
            thought="request scoped directory",
            plan=["request grant"],
            allowed_tools=["write_file"],
        )
        agent.subagents.lifecycle.record_capability_request(
            child_task.id,
            RecordCapabilityRequestParams(
                problem="need output dir",
                needed_capability="unlock_output_directory",
                capability_type="filesystem",
                path_scope=[str(tmp_path / "outside_allowed_scope")],
            ),
        )
        domain_refusal = cap_tool.execute(
            {"run_id": child_task.id, "decision": "grant", "reason": "scope validation check"}
        )
        assert domain_refusal.ok is False
        assert domain_refusal.effect_outcome == "failed"

        # If post-persistence wake/bookkeeping raises unexpectedly, coordinator must treat it as unknown, not not_started.
        child_task_2 = agent.subagents.create_run(
            goal="write second module report",
            thought="request valid directory",
            plan=["request grant"],
            allowed_tools=["write_file"],
        )
        valid_dir = Path(child_task_2.task_workspace_dir) / "deliverables"
        agent.subagents.lifecycle.record_capability_request(
            child_task_2.id,
            RecordCapabilityRequestParams(
                problem="need deliverables dir",
                needed_capability="unlock_output_directory",
                capability_type="filesystem",
                path_scope=[str(valid_dir)],
            ),
        )
        registry.register(cap_tool)
        snap_with_cap = registry.runtime_snapshot(run_id="run-refusal")
        real_persist_grants = cap_tool._persist_grants

        def persist_then_fail(ctx: Any, pending_grants: Any) -> None:
            real_persist_grants(ctx, pending_grants)
            raise RuntimeError("simulated post-persistence failure")

        with monkeypatch.context() as mp:
            mp.setattr(cap_tool, "execute_scoped", lambda params, _ctx: cap_tool.execute(params))
            mp.setattr(cap_tool, "_persist_grants", persist_then_fail)
            res_post_persist = registry.execute_tool(
                canonical_test_call(
                    snap_with_cap,
                    "resolve_capability_requests",
                    {"run_id": child_task_2.id, "decision": "grant", "reason": "approve"},
                    operation_id="op-cap-post-persist",
                    idempotency_key="idem-cap-post-persist",
                ),
                write_boundary=None,
                runtime_snapshot=snap_with_cap,
            ).result
        assert res_post_persist.effect_outcome == "unknown"
        assert res_post_persist.retryable is False
    finally:
        registry.close_mcp_clients()


def test_core_nested_schemas_and_valid_examples(tmp_path: Path) -> None:
    workspace = tmp_path / "schema_ws"
    workspace.mkdir()
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "schema_home"),
            business_spaces_enabled=True,
        ),
        workspace,
    )
    pool = {name: tool.model_spec for name, tool in agent.tools.tools.items()}

    w2_schema_tools = (
        "self_map",
        "update_goal",
        "session_search",
        "request_credentials",
        "task_progress",
        "create_subagents",
        "remember",
        "update_persona",
    )
    for tool_name in w2_schema_tools:
        assert tool_name in pool, f"missing tool {tool_name}"
        assert _schema_description_gaps(pool[tool_name].input_schema) == [], tool_name

    # Also verify create_subagents when subagent_input_media_enabled=True.
    media_spec = build_create_subagents_model_spec(input_media=True)
    assert _schema_description_gaps(media_spec.input_schema) == []

    # Verify source-definition description inheritance across projections.
    tp_props = pool["task_progress"].input_schema["properties"]
    item_props = tp_props["items"]["items"]["properties"]
    target_props = tp_props["coverage"]["properties"]["targets"]["items"]["properties"]
    for shared_key, expected_desc in _TASK_PROGRESS_SHARED_ENTRY_DESCRIPTIONS.items():
        assert item_props[shared_key]["description"] == expected_desc
        assert target_props[shared_key]["description"] == expected_desc

    rem_props = pool["remember"].input_schema["properties"]
    rem_op_props = rem_props["operations"]["items"]["properties"]
    for shared_key in ("entry_id", "content", "kind", "tags", "origin", "evidence_refs", "subject_key"):
        assert rem_op_props[shared_key]["description"] == rem_props[shared_key]["description"]
    for scope_field, scope_shape in rem_props["scope"]["properties"].items():
        assert (
            rem_op_props["scope"]["properties"][scope_field]["description"]
            == scope_shape["description"]
        )

    persona_props = pool["update_persona"].input_schema["properties"]
    persona_op_props = persona_props["operations"]["items"]["properties"]
    for shared_key in ("content", "entry_id", "source_quote"):
        assert persona_op_props[shared_key]["description"] == persona_props[shared_key]["description"]

    # Verify sibling routing guidance and schema-valid examples on W2 hint targets.
    sibling_expectations = {
        "skill_feedback": ("skill_search", "remember"),
        "self_map": ("skill_search", "list_agents"),
        "manage_handoff_board": ("task_progress", "send_guidance", "manage_requirement_provenance"),
        "manage_requirement_provenance": ("task_progress", "manage_handoff_board"),
    }
    for tool_name, expected_siblings in sibling_expectations.items():
        spec = pool[tool_name]
        assert spec.hints.use_cases, f"missing use_cases on {tool_name}"
        avoid_joined = " ".join(spec.hints.avoid_when)
        for sibling in expected_siblings:
            assert sibling in avoid_joined, f"{tool_name} avoid_when missing sibling {sibling}"
        assert len(spec.hints.examples) >= 2, f"{tool_name} should provide concrete examples"
        for raw_example in spec.hints.examples:
            parsed = json.loads(raw_example)
            validation = validate_tool_input(parsed, spec.input_schema)
            assert validation.ok, f"{tool_name} invalid example {raw_example}: {validation}"

    # Verify persona confirmation and read-only gates remain intact.
    persona_tool = agent.tools.tools["update_persona"]
    assert (
        tool_effect_for_runtime_policy(
            persona_tool.runtime_policy,
            {"target": "soul", "action": "add", "content": "concise style"},
        )
        == "dangerous"
    )
    assert (
        tool_effect_for_runtime_policy(
            persona_tool.runtime_policy,
            {"target": "user", "action": "list"},
        )
        == "read_only"
    )
    agents_write = persona_tool.execute(
        {"target": "agents", "action": "add", "content": "never mutate agents via model"}
    )
    assert agents_write.ok is False
