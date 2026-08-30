from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from agent_py_agent.agent.memory_archive import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import (
    ReadFileTool,
    filesystem_access_options,
)
from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool
from agent_py_agent.agent.tooling.executor import ToolOutputProjection
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome, ToolInvocationContext
from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
from agent_py_agent.agent.tooling.registry_invoke import (
    AuthorizedToolDispatchRequest,
    RegistryToolInvokeRequest,
    _sandbox_read_roots_for_invocation,
    _tool_params_with_runtime_boundary,
    _workspace_roots_for_invocation,
    execute_authorized_tool,
    invoke_registry_tool,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolContentBlock,
    ToolResultRef,
)
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class FlakyReadTool(BaseTool):
    model_spec = make_test_model_spec(
        "flaky_read",
        description="Fails once then succeeds.",
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        if self.calls == 1:
            return ToolHandlerOutcome("flaky_read", False, "timeout", error_code="TOOL_TIMEOUT")
        return ToolHandlerOutcome("flaky_read", True, "ok-after-retry")


class FlakyWriteTool(BaseTool):
    model_spec = make_test_model_spec(
        "flaky_write",
        description="Fails once and should not be retried without idempotency.",
    )
    runtime_policy = make_test_runtime_policy("mutating", idempotency_scope="")

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome("flaky_write", False, "timeout", error_code="TOOL_TIMEOUT")


class HugeOutputTool(BaseTool):
    model_spec = make_test_model_spec(
        "huge_output",
        description="Returns large output.",
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome("huge_output", True, "x" * 25000)


class HugeReadFileTool(BaseTool):
    model_spec = make_test_model_spec(
        "read_file",
        description="Returns large file content.",
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome("read_file", True, "文件正文" * 9000)


class CountingReadFileTool(BaseTool):
    model_spec = make_test_model_spec(
        "read_file",
        description="Records whether the handler executed.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "path"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome("read_file", True, str(params.get("path") or ""))


class PreservedOutputTool(BaseTool):
    model_spec = make_test_model_spec(
        "preserved_output",
        description="Returns large machine-readable output.",
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome(
            "preserved_output",
            True,
            '{"items":[' + ",".join('"value"' for _ in range(3000)) + "]}",
            result_envelope={"tool_output_policy": {"preserve_prompt_output": True}},
        )


class ExternalOutputTool(BaseTool):
    model_spec = make_test_model_spec(
        "external_output",
        description="Returns external data.",
    )
    runtime_policy = make_test_runtime_policy(
        "read_only",
        output_trust="external_data",
    )

    def execute(self, params):
        return ToolHandlerOutcome(
            "external_output",
            True,
            "external",
            result_envelope={
                "tool_output_policy": {
                    "trust": "runtime",
                    "redaction": "source_code",
                }
            },
        )


class DynamicExternalReadTool(BaseTool):
    model_spec = make_test_model_spec(
        "dynamic_external_read",
        description="Usually reads source code but this result came from an external archive.",
    )
    runtime_policy = make_test_runtime_policy(
        "read_only",
        output_redaction="source_code",
    )

    def execute(self, params):
        return ToolHandlerOutcome(
            "dynamic_external_read",
            True,
            "external archive",
            result_envelope={
                "tool_output_policy": {
                    "trust": "external_data",
                    "redaction": "default",
                }
            },
        )


def _execute(
    payload: dict[str, object],
    tools: dict[str, BaseTool],
    workspace: Path,
    *,
    write_boundary: dict[str, object] | None = None,
    output_archiver=None,
):
    arguments = dict(payload)
    tool_name = str(arguments.pop("tool"))
    return execute_canonical_test_call(
        workspace,
        tools=tools,
        tool_name=tool_name,
        arguments=arguments,
        write_boundary=write_boundary,
        output_archiver=output_archiver,
    ).result


def _with_test_runtime(request: RegistryToolInvokeRequest) -> RegistryToolInvokeRequest:
    """Bind lower-seam invocation tests to the same immutable runtime snapshot."""

    snapshot = runtime_snapshot_for_tools(
        request.tools,
        run_id="registry-invoke-test",
        allowed_tools=request.allowed_tools,
    )
    return replace(
        request,
        runtime_snapshot=snapshot,
        runtime=snapshot.runtime(request.tool_name),
    )


def _archive_into(root: Path, records: list[dict[str, object]]):
    def archive(call, outcome) -> ToolOutputProjection:
        record = externalize_tool_output_record(
            ExternalizeToolOutputRequest(
                root=root,
                tool=call.tool_name,
                call_id=call.call_id,
                output=outcome.output,
                ok=outcome.ok,
                error_code=outcome.error_code,
                run_id=call.run_id,
                task_id=call.run_id,
                min_chars=100,
                preview_chars=100,
                parameters={"tool": call.tool_name, **call.arguments},
            )
        )
        records.append(record)
        ref = str(
            record.get("output_path")
            or record.get("source_artifact_ref")
            or record.get("artifact_ref")
            or record.get("scoped_call_id")
        )
        refs = (
            ToolResultRef(
                kind="tool_output",
                ref=ref,
                sha256=str(record.get("output_hash") or ""),
                size_bytes=int(record.get("output_size_bytes") or 0),
                summary="complete raw tool output",
            ),
        )
        return ToolOutputProjection(
            content_blocks=(
                ToolContentBlock("text", text=str(record.get("output_preview") or "")),
                ToolContentBlock("ref", ref=ref),
            ),
            refs=refs,
            metadata={
                "archive_output_record": record,
                "raw_output_chars": len(str(outcome.output or "")),
                "raw_output_bytes": int(record.get("output_size_bytes") or 0),
                "raw_output_sha256": str(record.get("output_hash") or ""),
                "projection_truncated": record.get("output_externalized") is True,
            },
        )

    return archive


def test_registry_retries_retryable_read_only_tool_once(tmp_path: Path) -> None:
    tool = FlakyReadTool()

    result = _execute({"tool": "flaky_read"}, {"flaky_read": tool}, tmp_path)

    assert result.ok is True
    assert result.output == "ok-after-retry"
    assert tool.calls == 2
    assert result.metadata["handler_details"]["tool_resilience"]["retry_attempts"] == 1


def test_registry_does_not_retry_mutating_tool_without_idempotency(tmp_path: Path) -> None:
    tool = FlakyWriteTool()

    result = _execute({"tool": "flaky_write"}, {"flaky_write": tool}, tmp_path)

    assert result.ok is False
    assert tool.calls == 0
    assert result.error_code in {
        "TOOL_IDEMPOTENCY_KEY_MISSING",
        "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",
    }


def test_registry_keeps_large_output_and_bounds_only_model_projection(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    result = _execute(
        {"tool": "huge_output"},
        {"huge_output": HugeOutputTool()},
        tmp_path,
        output_archiver=_archive_into(tmp_path, records),
    )

    assert result.ok is True
    assert result.content_blocks[0].text.startswith("x")
    assert len(result.content_blocks[0].text) <= 160
    assert result.refs[0].size_bytes == 25000
    assert result.metadata["raw_output_chars"] == 25000
    assert records[0]["output_externalized"] is True


def test_large_projection_archives_and_reads_the_complete_execution_output(
    tmp_path: Path,
) -> None:
    records: list[dict[str, object]] = []
    result = _execute(
        {"tool": "huge_output"},
        {"huge_output": HugeOutputTool()},
        tmp_path,
        output_archiver=_archive_into(tmp_path, records),
    )
    archived = records[0]

    assert archived["output_size_bytes"] == 25000
    assert archived["output_externalized"] is True
    restored = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=tmp_path,
            artifact_ref=archived["scoped_call_id"],
            run_id="test-run",
            task_id="test-run",
            max_chars=0,
        )
    )
    assert restored["ok"] is True
    assert restored["content"] == "x" * 25000


def test_registry_does_not_create_a_second_large_output_store(tmp_path: Path) -> None:
    source_workspace = tmp_path / "source"
    source_workspace.mkdir()
    task_root = tmp_path / "home" / "tasks" / "2026-06-07" / "demo"
    task_work = task_root / "work"
    records: list[dict[str, object]] = []
    result = _execute(
        {"tool": "huge_output"},
        {"huge_output": HugeOutputTool()},
        source_workspace,
        write_boundary={"task_root": str(task_root), "task_work_dir": str(task_work)},
        output_archiver=_archive_into(task_work, records),
    )

    assert result.ok is True
    assert len(records) == 1
    assert str(records[0]["output_path"]).startswith(str(task_work))
    assert not (source_workspace / ".agent_tool_outputs").exists()
    assert not (source_workspace / "work" / "blobs" / "tool_outputs").exists()


def test_registry_preserves_read_file_output_for_context_compaction(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    result = _execute(
        {"tool": "read_file"},
        {"read_file": HugeReadFileTool()},
        tmp_path,
        output_archiver=_archive_into(tmp_path, records),
    )

    assert result.ok is True
    assert result.refs
    assert records[0]["source_output_archived"] is True
    assert records[0]["output_size_bytes"] == len(("文件正文" * 9000).encode("utf-8"))


def test_registry_preserves_large_machine_output_when_declared(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    result = _execute(
        {"tool": "preserved_output"},
        {"preserved_output": PreservedOutputTool()},
        tmp_path,
        output_archiver=_archive_into(tmp_path, records),
    )

    assert result.ok is True
    assert result.content_blocks[0].text.startswith('{"items":[')
    assert result.refs
    assert records[0]["output_externalized"] is True


def test_registry_runtime_policy_owns_output_projection_policy(tmp_path: Path) -> None:
    result = _execute(
        {"tool": "external_output"},
        {"external_output": ExternalOutputTool()},
        tmp_path,
    )

    assert result.output_trust == "external_data"
    assert result.output_redaction == "default"


def test_registry_result_may_tighten_but_not_loosen_projection_policy(
    tmp_path: Path,
) -> None:
    result = _execute(
        {"tool": "dynamic_external_read"},
        {"dynamic_external_read": DynamicExternalReadTool()},
        tmp_path,
    )

    assert result.output_trust == "external_data"
    assert result.output_redaction == "default"


def test_process_tools_receive_structured_sandbox_write_roots(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    allowed = task_root / "work"
    output = task_root / "output"
    readable = tmp_path / "shared-source"
    boundary = {
        "allowed_write_roots": [str(output), str(allowed)],
        "allowed_read_roots": [str(readable)],
        "shell_access_mode": "workspace-write",
        "task_root": str(task_root),
        "task_work_dir": str(allowed),
    }
    for tool_name in ("run_command", "terminal_session"):
        tool_params = {"action": "start"} if tool_name == "terminal_session" else {"action": "status"}
        params = _tool_params_with_runtime_boundary(
            AuthorizedToolDispatchRequest(
                tool_name=tool_name,
                tool=HugeOutputTool(),
                tool_params=tool_params,
                workspace_root=tmp_path,
                write_boundary=boundary,
                sandbox_read_roots=(readable,),
            )
        )
        assert params["__sandbox_write_roots"] == [str(allowed), str(output)]
        assert params["__sandbox_read_roots"] == [str(readable)]
        assert params["__access_mode"] == "workspace-write"
        assert "working_dir" not in params


def test_owner_scoped_process_boundary_missing_write_roots_fails_closed(
    tmp_path: Path,
) -> None:
    """畸形 owner boundary 省略写根时，Shell/PTY 都必须得到显式空写根。"""

    owner = tmp_path / "owner"
    source = owner / "readonly-source"
    source.mkdir(parents=True)
    shell = ShellTool(
        owner,
        options=ShellToolOptions(owner_scope_root=str(owner)),
    )
    boundary = {
        "execution_cwd": str(source),
        "allowed_read_roots": [str(source)],
    }
    for tool_name, tool, tool_params in (
        ("run_command", shell, {"command": "pwd"}),
        ("terminal_session", TerminalSessionTool(shell), {"action": "start", "command": "pwd"}),
    ):
        params = _tool_params_with_runtime_boundary(
            AuthorizedToolDispatchRequest(
                tool_name=tool_name,
                tool=tool,
                tool_params=tool_params,
                workspace_root=source,
                write_boundary=boundary,
                sandbox_read_roots=(source,),
            )
        )
        assert params["__sandbox_write_roots"] == []


def test_owner_scoped_process_without_boundary_keeps_workspace_default(
    tmp_path: Path,
) -> None:
    """没有任务 boundary 的 WorkspaceOnly 主会话仍使用 owner-home 默认可写语义。"""

    owner = tmp_path / "owner"
    owner.mkdir()
    shell = ShellTool(
        owner,
        options=ShellToolOptions(owner_scope_root=str(owner)),
    )
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="run_command",
            tool=shell,
            tool_params={"command": "pwd"},
            workspace_root=owner,
            write_boundary=None,
        )
    )
    assert "__sandbox_write_roots" not in params


def test_full_access_boundary_missing_write_roots_keeps_host_write_semantics(
    tmp_path: Path,
) -> None:
    """无 owner 墙的本地 Full Access 不因只读提示字段被静默降权。"""

    source = tmp_path / "external-project"
    source.mkdir()
    shell = ShellTool(source, options=ShellToolOptions(owner_scope_root=""))
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="run_command",
            tool=shell,
            tool_params={"command": "pwd"},
            workspace_root=source,
            write_boundary={
                "execution_cwd": str(source),
                "allowed_read_roots": [str(source)],
            },
            sandbox_read_roots=(source,),
        )
    )
    assert "__sandbox_write_roots" not in params


def test_process_tools_can_enter_read_root_without_promoting_it_to_write(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    readable = tmp_path / "shared-source"
    request = RegistryToolInvokeRequest(
        tool_name="run_command",
        arguments={"command": "pwd", "working_dir": str(readable)},
        tools={},
        workspace_root=task_root,
        workspace_roots=[task_root],
        allowed_tools=["run_command"],
        write_boundary={
            "task_root": str(task_root),
            "allowed_write_roots": [str(task_root)],
            "allowed_read_roots": [str(readable)],
        },
    )

    roots = _workspace_roots_for_invocation(request)
    assert readable.resolve() in roots
    assert _sandbox_read_roots_for_invocation(request) == (
        task_root.resolve(),
        readable.resolve(),
    )


def test_exact_read_scope_rejects_other_owner_file_before_handler(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "owner" / "docs" / "source.md"
    denied = tmp_path / "owner" / "private" / "user.md"
    allowed.parent.mkdir(parents=True)
    denied.parent.mkdir(parents=True)
    allowed.write_text("source", encoding="utf-8")
    denied.write_text("private", encoding="utf-8")
    tool = CountingReadFileTool()

    rejected = invoke_registry_tool(
        _with_test_runtime(RegistryToolInvokeRequest(
            tool_name="read_file",
            arguments={"path": str(denied)},
            tools={"read_file": tool},
            workspace_root=tmp_path / "owner",
            workspace_roots=[tmp_path / "owner"],
            allowed_tools=["read_file"],
            write_boundary={
                "read_scope_mode": "exact",
                "allowed_read_roots": [str(allowed)],
            },
        ))
    )
    accepted = invoke_registry_tool(
        _with_test_runtime(RegistryToolInvokeRequest(
            tool_name="read_file",
            arguments={"path": str(allowed)},
            tools={"read_file": tool},
            workspace_root=tmp_path / "owner",
            workspace_roots=[tmp_path / "owner"],
            allowed_tools=["read_file"],
            write_boundary={
                "read_scope_mode": "exact",
                "allowed_read_roots": [str(allowed)],
            },
        ))
    )

    assert rejected.ok is False
    assert rejected.error_code == "TOOL_PERMISSION_DENIED"
    assert rejected.handler_executed is False
    assert accepted.ok is True
    assert accepted.handler_executed is True
    assert tool.calls == 1


def test_read_artifact_receives_current_run_scope_from_boundary(
    tmp_path: Path,
) -> None:
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="read_artifact",
            tool=HugeOutputTool(),
            tool_params={"artifact_ref": "run-a:1-1"},
            workspace_root=tmp_path,
            write_boundary={
                "task_work_dir": str(tmp_path / "task" / "work"),
                "artifact_read_root": str(tmp_path / "task" / "work" / "agents" / "run-a"),
                "artifact_read_scope_mode": "current_run",
            },
        )
    )

    assert params["__artifact_read_root"] == str(
        tmp_path / "task" / "work" / "agents" / "run-a"
    )
    assert params["__artifact_read_scope_mode"] == "current_run"


def test_read_artifact_falls_back_to_task_work_root_for_legacy_boundary(
    tmp_path: Path,
) -> None:
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="read_artifact",
            tool=HugeOutputTool(),
            tool_params={"artifact_ref": "run-a:1-1"},
            workspace_root=tmp_path,
            write_boundary={"task_work_dir": str(tmp_path / "task" / "work")},
        )
    )

    assert params["__artifact_read_root"] == str(tmp_path / "task" / "work")


def test_deterministic_handler_failure_operation_status_failed(
    tmp_path: Path,
) -> None:
    """问题B(2026-08-14 真机实证, ④类复刻): mutating 工具 handler 显式返回的
    执行前确定性失败(TOOL_INVALID_ARGUMENTS, taxonomy category=tool/retryable)
    必须终态 FAILED 而非 UNKNOWN——修复前一律 UNKNOWN → 系统禁止自动重试 →
    模型卡死到轮限(真机: apply_patch 无效补丁卡死复刻任务,
    outcome_json=effect_outcome_unknown:TOOL_INVALID_ARGUMENTS)。
    """
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    tool = ApplyPatchTool(workspace_root=tmp_path)
    result = execute_authorized_tool(
        AuthorizedToolDispatchRequest(
            tool_name="apply_patch",
            tool=tool,
            tool_params={"patch": "*** 非补丁文本，必触发参数校验失败 ***"},
            workspace_root=tmp_path,
            write_boundary=None,
            invocation_context=ToolInvocationContext(
                runtime_snapshot=None,
                cancellation_token=None,
            ),
        )
    )
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    # execute_authorized_tool 按统一入口语义标 handler_executed=True(能走到
    # 派发=handler 被调用); 修复点=_operation_status_for_result 识别执行前
    # 确定性失败族(category=tool/path 且 retryable), 即使 True 也终态 FAILED。
    assert _operation_status_for_result(result) == "failed"


def test_wrong_status_surface_is_not_started_and_does_not_halt_turn(
    tmp_path: Path,
) -> None:
    """内部状态路径在 shell 启动前被拒绝，必须把原因交回模型而非误报未知副作用。"""
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    workspace = tmp_path / "workspace"
    state = (
        workspace
        / "tasks"
        / "2026-08-22"
        / "demo"
        / "work"
        / "agents"
        / "subagent-123"
        / "canonical_state.json"
    )
    state.parent.mkdir(parents=True)
    state.write_text('{"status":"RUNNING"}', encoding="utf-8")
    result = execute_authorized_tool(
        AuthorizedToolDispatchRequest(
            tool_name="run_command",
            tool=ShellTool(
                workspace,
                options=ShellToolOptions(default_timeout=5),
            ),
            tool_params={"command": f"cat {state}"},
            workspace_root=workspace,
            write_boundary=None,
            invocation_context=ToolInvocationContext(
                runtime_snapshot=None,
                cancellation_token=None,
            ),
        )
    )

    assert result.error_code == "WRONG_STATUS_SURFACE"
    assert result.handler_executed is True
    assert result.effect_outcome == "not_started"
    assert _operation_status_for_result(result) == "failed"


def test_owner_quota_unavailable_deterministic_failure_failed() -> None:
    """问题B同族(2026-08-14 真机, bs4 复刻): write_file 因 owner 配额无法
    可靠读取 fail-closed(OWNER_QUOTA_UNAVAILABLE, 副作用零发生)必须终态 FAILED
    而非 UNKNOWN——修复前归 UNKNOWN 禁止自动重试, bs4 最后一笔 write_file
    (fix5.py)因此卡死到轮限(真机 outcome_json=effect_outcome_unknown:
    OWNER_QUOTA_UNAVAILABLE)。归 FAILED 后模型可如实报告/换策略, 不再哑卡。
    """
    from agent_py_agent.agent.tooling._filesystem_read import owner_quota_error_result
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    outcome = owner_quota_error_result(
        "write_file", RuntimeError("owner disk usage scan failed")
    )
    assert outcome.error_code == "OWNER_QUOTA_UNAVAILABLE"
    # 与 TOOL_INVALID_ARGUMENTS 同族: 执行前确定性失败(retryable)即使
    # execute_authorized_tool 标了 handler_executed=True 也终态 FAILED。
    assert _operation_status_for_result(outcome) == "failed"


def test_path_outside_workspace_deterministic_failure_failed() -> None:
    """EXEC-03(2026-08-15 compact 场景真机): PATH_OUTSIDE_WORKSPACE 是校验
    阶段确定性拒绝(零副作用), 但 taxonomy retryable=False——旧判定叠加
    retryable 闸使其永不命中白名单 → 归 UNKNOWN → unknown 收口闸 → 模型无法
    改路径重试, 任务死(真机: python heredoc working_dir 越界, RC=2)。
    终态 FAILED 不授予自动重试(同参重放仍回 FAILED), 只解除收口让模型
    读 recovery_hint 改参后以新调用继续——正是 retryable=False 的语义。
    """
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    outcome = ToolHandlerOutcome(
        "run_command",
        False,
        "COMMAND_ACCESS_DENIED: 多用户 owner 只能在当前任务工作区或结构化授权目录执行命令。",
        error_code="PATH_OUTSIDE_WORKSPACE",
        handler_executed=True,
    )
    assert _operation_status_for_result(outcome) == "failed"


def test_write_forbidden_deterministic_failure_failed() -> None:
    """EXEC-03 同族: WRITE_FORBIDDEN(permission/retryable=False)也是校验阶段
    确定性拒绝——旧 retryable 闸使其归 UNKNOWN 收口; 白名单是唯一权威。
    """
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    outcome = ToolHandlerOutcome(
        "write_file",
        False,
        "write forbidden by policy",
        error_code="WRITE_FORBIDDEN",
        handler_executed=True,
    )
    assert _operation_status_for_result(outcome) == "failed"


def test_non_whitelisted_mutating_failure_stays_unknown() -> None:
    """安全回归: 白名单外且 handler 已执行过的失败(如 TOOL_ERROR)必须保持
    UNKNOWN——不能因为放开 retryable 闸而放大自动重做风险。"""
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _operation_status_for_result,
    )

    outcome = ToolHandlerOutcome(
        "write_file",
        False,
        "boom",
        error_code="TOOL_ERROR",
        handler_executed=True,
    )
    assert _operation_status_for_result(outcome) == "unknown"


def test_explicit_process_working_dir_overrides_selected_task_root(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    explicit = task_root / "output" / "project"
    params = _tool_params_with_runtime_boundary(
        AuthorizedToolDispatchRequest(
            tool_name="run_command",
            tool=HugeOutputTool(),
            tool_params={"command": "pwd", "working_dir": str(explicit)},
            workspace_root=tmp_path,
            write_boundary={
                "task_root": str(task_root),
                "allowed_write_roots": [str(task_root)],
            },
        )
    )

    assert params["working_dir"] == str(explicit)


def _task_scoped_apply_patch_request(
    *,
    owner_root: Path,
    task_root: Path,
    patch: str,
) -> RegistryToolInvokeRequest:
    tool = ApplyPatchTool(
        owner_root,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )
    return _with_test_runtime(RegistryToolInvokeRequest(
        tool_name="apply_patch",
        arguments={"patch": patch},
        tools={"apply_patch": tool},
        workspace_root=owner_root,
        workspace_roots=[owner_root],
        allowed_tools=["apply_patch"],
        write_boundary={
            "task_root": str(task_root),
            "task_dir": str(task_root),
            "task_output_dir": str(task_root / "output"),
            "task_work_dir": str(task_root / "work"),
            "allowed_write_roots": [
                str(task_root / "output"),
                str(task_root / "work"),
            ],
        },
    ))


def test_task_scoped_apply_patch_resolves_work_delete_to_current_task(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = task_root / "work" / "delete-smoke.txt"
    target.parent.mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("remove me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch=(
                "*** Begin Patch\n"
                "*** Delete File: work/delete-smoke.txt\n"
                "*** End Patch\n"
            ),
        )
    )

    assert result.ok is True
    assert result.handler_executed is True
    assert not target.exists()


def test_task_scoped_apply_patch_rejects_malformed_delete_without_side_effect(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = task_root / "work" / "keep.txt"
    target.parent.mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("keep me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch="*** Delete File: work/keep.txt\n",
        )
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.handler_executed is True
    assert target.read_text(encoding="utf-8") == "keep me"


def test_task_scoped_apply_patch_rejects_work_path_escape_before_handler(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "task-a"
    target = owner_root / "outside.txt"
    (task_root / "work").mkdir(parents=True)
    (task_root / "output").mkdir()
    target.write_text("keep me", encoding="utf-8")

    result = invoke_registry_tool(
        _task_scoped_apply_patch_request(
            owner_root=owner_root,
            task_root=task_root,
            patch=(
                "*** Begin Patch\n"
                "*** Delete File: work/../../../outside.txt\n"
                "*** End Patch\n"
            ),
        )
    )

    assert result.ok is False
    assert result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False
    assert target.read_text(encoding="utf-8") == "keep me"


def _task_scoped_filesystem_request(
    *,
    owner_root: Path,
    task_root: Path,
    tool: BaseTool,
    payload: dict[str, object],
) -> RegistryToolInvokeRequest:
    return _with_test_runtime(RegistryToolInvokeRequest(
        tool_name=tool.model_spec.name,
        arguments=payload,
        tools={tool.model_spec.name: tool},
        workspace_root=owner_root,
        workspace_roots=[owner_root],
        allowed_tools=[tool.model_spec.name],
        write_boundary={
            "execution_cwd": str(task_root),
            "task_root": str(task_root),
            "task_dir": str(task_root),
            "task_output_dir": str(task_root / "output"),
            "task_work_dir": str(task_root / "work"),
            "allowed_write_roots": [
                str(task_root / "output"),
                str(task_root / "work"),
            ],
        },
    ))


def test_task_scoped_relative_file_tools_share_one_effective_cwd(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "audits" / "audit-a"
    task_root.mkdir(parents=True)
    (task_root / "output").mkdir()
    (task_root / "work").mkdir()
    (task_root / "task-only.md").write_text("TASK_SCOPE_MARKER", encoding="utf-8")
    (owner_root / "owner-only.md").write_text("OWNER_SCOPE_MARKER", encoding="utf-8")
    access = filesystem_access_options(owner_scope_root=str(owner_root))
    tools_and_payloads = (
        (ReadFileTool(owner_root, 10_000, access_options=access), {"path": "task-only.md"}),
        (ListFilesTool(owner_root, 100, access_options=access), {"path": "."}),
        (FindFilesTool(owner_root, 100, access_options=access), {"pattern": "task-only.md", "path": "."}),
        (SearchTextTool(owner_root, 100, access_options=access), {"query": "TASK_SCOPE_MARKER", "path": "."}),
    )

    for tool, payload in tools_and_payloads:
        result = invoke_registry_tool(
            _task_scoped_filesystem_request(
                owner_root=owner_root,
                task_root=task_root,
                tool=tool,
                payload=payload,
            )
        )
        assert result.ok is True, (tool.model_spec.name, result.output)
        assert "task-only.md" in result.output or "TASK_SCOPE_MARKER" in result.output
        assert "owner-only.md" not in result.output
        assert tool.workspace_root == owner_root.resolve()


def test_shared_file_tool_does_not_leak_task_cwd_between_concurrent_calls(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    first_root = owner_root / "audits" / "first"
    second_root = owner_root / "audits" / "second"
    for root, marker in ((first_root, "FIRST_MARKER"), (second_root, "SECOND_MARKER")):
        (root / "work").mkdir(parents=True)
        (root / "output").mkdir()
        (root / "marker.txt").write_text(marker, encoding="utf-8")
    tool = ReadFileTool(
        owner_root,
        10_000,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )

    def read(root: Path) -> str:
        result = invoke_registry_tool(
            _task_scoped_filesystem_request(
                owner_root=owner_root,
                task_root=root,
                tool=tool,
                payload={"path": "marker.txt"},
            )
        )
        assert result.ok is True
        return result.output

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read, first_root)
        second = pool.submit(read, second_root)
        assert "FIRST_MARKER" in first.result()
        assert "SECOND_MARKER" in second.result()
    assert tool.workspace_root == owner_root.resolve()


def test_task_scoped_file_tool_keeps_owner_wall_and_ordinary_cwd(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "audits" / "task"
    outsider = tmp_path / "other-owner" / "secret.txt"
    owner_root.mkdir()
    (task_root / "work").mkdir(parents=True)
    (task_root / "output").mkdir()
    outsider.parent.mkdir()
    outsider.write_text("OTHER_OWNER_SECRET", encoding="utf-8")
    (owner_root / "ordinary.txt").write_text("ORDINARY_MARKER", encoding="utf-8")
    tool = ReadFileTool(
        owner_root,
        10_000,
        access_options=filesystem_access_options(owner_scope_root=str(owner_root)),
    )

    denied = invoke_registry_tool(
        _task_scoped_filesystem_request(
            owner_root=owner_root,
            task_root=task_root,
            tool=tool,
            payload={"path": str(outsider)},
        )
    )
    ordinary = invoke_registry_tool(
        _with_test_runtime(RegistryToolInvokeRequest(
            tool_name="read_file",
            arguments={"path": "ordinary.txt"},
            tools={"read_file": tool},
            workspace_root=owner_root,
            workspace_roots=[owner_root],
            allowed_tools=["read_file"],
            write_boundary=None,
        ))
    )

    assert denied.ok is False
    assert "OTHER_OWNER_SECRET" not in denied.output
    assert ordinary.ok is True
    assert "ORDINARY_MARKER" in ordinary.output
