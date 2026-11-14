"""R2 行为回归：受控命令规划阶段被拒时保持 WRITE_FORBIDDEN 与 not_started 契约，不产生 unknown 收口。"""

from __future__ import annotations

import gc
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _mark_unknown_outcome_halt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.contracts.idempotency import operation_idempotency_key
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

_OWNER_ID = "owner-r2-controlled-exec"


def _fd_count() -> int:
    """统计当前进程打开的文件描述符数量，用于验证执行前后句柄零泄漏。"""
    if os.name == "nt":
        import msvcrt

        count = 0
        for descriptor in range(2048):
            try:
                msvcrt.get_osfhandle(descriptor)
                count += 1
            except OSError:
                pass
        return count
    fd_dir = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    return len(os.listdir(fd_dir))


@contextmanager
def _managed_registry_and_store(
    tmp_path: Path,
    *,
    db_name: str = "controlled_exec_ops.db",
) -> Iterator[tuple[Path, ToolRegistry, LocalStore]]:
    """通过 ExitStack 托管持久化 LocalStore 与真实 ToolRegistry，确保退出后连接零泄漏。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(sqlite_connection_scope(enabled=True))
        store = LocalStore(tmp_path / db_name, enable_fts=False)
        close_store = getattr(store, "close", None)
        if callable(close_store):
            stack.callback(close_store)
        registry = ToolRegistry(
            ToolRegistryParams(
                workspace_root=workspace,
                max_chars=12_000,
                max_entries=100,
                max_matches=50,
                web_max_chars=12_000,
                http_timeout=5,
                catalog_limit=20,
                retrieval_limit=5,
                vector_search_enabled=False,
                shell_tool_timeout=10,
                operation_store=store,
                operation_store_required=True,
                operation_owner_id=_OWNER_ID,
            )
        )
        stack.callback(registry.close_mcp_clients)
        yield workspace, registry, store


def _pwd_only_boundary(workspace: Path, *, run_id: str = "run-r2") -> dict[str, object]:
    """构造仅允许 pwd 命令与当前 workspace 路径范围的父级受控执行授权边界。"""
    return {
        "task_dir": str(workspace / "task"),
        "controlled_exec_grants": [
            {
                "grant_id": "grant-shell-1",
                "request_id": "req-shell-1",
                "run_id": run_id,
                "command_allowlist": ["pwd"],
                "path_scope": [str(workspace)],
                "network_scope": [],
                "output_budget": {"timeout_seconds": 5, "stdout_bytes": 1024},
                "constraints": {"delete_policy": "trash_only"},
            }
        ],
    }


def _execute_controlled_call(
    registry: ToolRegistry,
    *,
    arguments: dict[str, object],
    boundary: dict[str, object],
    run_id: str,
    call_id: str,
    attempt_id: str,
    operation_id: str,
    idempotency_key: str,
) -> tuple[ToolCall, ToolExecution]:
    """经真实 ToolRegistry -> ToolExecutor -> Coordinator -> LocalStore 执行一次受控命令调用。"""
    snapshot = registry.runtime_snapshot(allowed_tools=["controlled_exec"], run_id=run_id)
    call = canonical_test_call(
        snapshot,
        "controlled_exec",
        arguments,
        call_id=call_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    execution = registry.execute_tool(
        call,
        write_boundary=boundary,
        runtime_snapshot=snapshot,
        trusted_run_context={
            "run_scope": {
                "request_id": f"req-{run_id}",
                "task_id": run_id,
                "owner_type": "user",
                "owner_id": _OWNER_ID,
            }
        },
    )
    return call, execution


def _make_loop_params(run_id: str, attempt_id: str) -> ToolLoopExecuteParams:
    """构造真实 ToolLoopExecuteParams 以验证当轮是否触发 unknown_outcome_halt。"""
    return ToolLoopExecuteParams(
        user_prompt="check controlled command",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=["controlled_exec"],
        write_boundary=None,
        task_attributes=None,
        request_id=f"req-{run_id}",
        run_id=run_id,
        task_id=run_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        attempt_id=attempt_id,
    )


def _assert_no_unknown_halt(call: ToolCall, result: ToolResult, *, run_id: str, attempt_id: str) -> None:
    """确认结果不会在真实回合服务中标记 unknown_outcome_halt 或注入停止提示。"""
    loop_params = _make_loop_params(run_id, attempt_id)
    record = ToolCallRecordParams(
        params=loop_params,
        tool_rounds=1,
        idx=0,
        call=call,
        result=result,
    )
    _mark_unknown_outcome_halt(None, record)
    assert getattr(loop_params, "unknown_outcome_halt", None) is None
    assert loop_params.tool_context == []


def _assert_blocked_plan_contract(
    result: ToolResult,
    *,
    expected_failure_stage: str = "validation",
) -> None:
    """核验被拒受控执行计划的错误码、未启动事实、保留的阻断原因与可执行下一步指引。"""
    assert result.ok is False
    assert result.status == "failed"
    assert result.error_code == "WRITE_FORBIDDEN"
    assert result.effect_outcome == "not_started"
    assert result.failure_stage == expected_failure_stage

    plan_data = json.loads(result.output)
    assert plan_data["mode"] == "dry_run"
    assert plan_data["grant_id"] == "grant-shell-1"
    assert plan_data["allowed"] is False
    assert plan_data["action"] == "blocked"
    assert plan_data["reason"] == "command_not_granted:ls"
    assert "command_not_granted:ls" in plan_data["blockers"]
    assert plan_data["shell_decision"]["would_execute"] is False

    next_action = str(result.metadata.get("next_action") or "").strip()
    assert next_action
    assert "command" in next_action and "cwd" in next_action
    assert "capability_request" in next_action


def test_omitted_apply_blocked_plan_persists_failed_not_started_and_replays_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """省略 apply 时被拒计划应落盘 failed + not_started，同操作重放保持 not_started 且不停止工具轮。"""
    dispatch_calls = 0

    def _forbid_shell_dispatch(*_args: object, **_kwargs: object) -> object:
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise AssertionError("execute_shell_command must not run for blocked controlled_exec plan")

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.controlled_exec.execute_shell_command",
        _forbid_shell_dispatch,
    )

    run_id = "run-r2-omitted"
    attempt_id = "attempt-r2-omitted"
    operation_id = f"tool_call:{attempt_id}:call-ls-omitted"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path) as (workspace, registry, store):
        boundary = _pwd_only_boundary(workspace, run_id=run_id)
        arguments: dict[str, object] = {
            "grant_id": "grant-shell-1",
            "command": "ls",
            "cwd": str(workspace),
        }
        first_call, first_exec = _execute_controlled_call(
            registry,
            arguments=arguments,
            boundary=boundary,
            run_id=run_id,
            call_id="call-ls-omitted-1",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        replay_call, replay_exec = _execute_controlled_call(
            registry,
            arguments=arguments,
            boundary=boundary,
            run_id=run_id,
            call_id="call-ls-omitted-2",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        stored = store.get_tool_operation(
            owner_id=_OWNER_ID,
            run_id=run_id,
            operation_id=operation_id,
        )
    gc.collect()
    fd_delta = _fd_count() - fd_before

    assert fd_delta == 0
    assert dispatch_calls == 0

    _assert_blocked_plan_contract(first_exec.result)
    assert first_exec.result.operation is not None
    assert first_exec.result.operation.status == "failed"
    assert first_exec.result.operation.effect_outcome == "not_started"
    assert first_exec.result.operation.replayed is False

    assert stored is not None
    assert stored.status == "failed"

    _assert_blocked_plan_contract(replay_exec.result, expected_failure_stage="effect_reconciliation")
    assert replay_exec.result.operation is not None
    assert replay_exec.result.operation.status == "failed"
    assert replay_exec.result.operation.effect_outcome == "not_started"
    assert replay_exec.result.operation.replayed is True

    _assert_no_unknown_halt(first_call, first_exec.result, run_id=run_id, attempt_id=attempt_id)
    _assert_no_unknown_halt(replay_call, replay_exec.result, run_id=run_id, attempt_id=attempt_id)


def test_explicit_false_apply_blocked_plan_returns_write_forbidden_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 apply=False 被拒计划应返回 WRITE_FORBIDDEN + not_started，零进程派发且不产生操作占位。"""
    dispatch_calls = 0

    def _forbid_shell_dispatch(*_args: object, **_kwargs: object) -> object:
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise AssertionError("execute_shell_command must not run for dry-run blocked plan")

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.controlled_exec.execute_shell_command",
        _forbid_shell_dispatch,
    )

    run_id = "run-r2-false"
    attempt_id = "attempt-r2-false"
    operation_id = f"tool_call:{attempt_id}:call-ls-false"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path) as (workspace, registry, store):
        boundary = _pwd_only_boundary(workspace, run_id=run_id)
        call, execution = _execute_controlled_call(
            registry,
            arguments={
                "grant_id": "grant-shell-1",
                "command": "ls",
                "cwd": str(workspace),
                "apply": False,
            },
            boundary=boundary,
            run_id=run_id,
            call_id="call-ls-false",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        stored = store.get_tool_operation(
            owner_id=_OWNER_ID,
            run_id=run_id,
            operation_id=operation_id,
        )
    gc.collect()
    fd_delta = _fd_count() - fd_before

    assert fd_delta == 0
    assert dispatch_calls == 0
    assert stored is None
    _assert_blocked_plan_contract(execution.result)
    _assert_no_unknown_halt(call, execution.result, run_id=run_id, attempt_id=attempt_id)


def test_approved_true_apply_blocked_plan_persists_failed_not_started_and_replays_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """携带精确审批绑定的 apply=True 被拒计划必须落盘 failed + not_started，重放不派发命令且不锁死当轮。"""
    dispatch_calls = 0

    def _forbid_shell_dispatch(*_args: object, **_kwargs: object) -> object:
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise AssertionError("execute_shell_command must not run when plan_controlled_exec blocks ls")

    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.controlled_exec.execute_shell_command",
        _forbid_shell_dispatch,
    )

    run_id = "run-r2-approved"
    attempt_id = "attempt-r2-approved"
    operation_id = f"tool_call:{attempt_id}:call-ls-approved"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path) as (workspace, registry, store):
        boundary = _pwd_only_boundary(workspace, run_id=run_id)
        arguments: dict[str, object] = {
            "grant_id": "grant-shell-1",
            "command": "ls",
            "cwd": str(workspace),
            "apply": True,
        }
        _, ask_exec = _execute_controlled_call(
            registry,
            arguments=arguments,
            boundary=boundary,
            run_id=run_id,
            call_id="call-ls-approved",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        assert ask_exec.result.status == "approval_required"
        assert ask_exec.decision.approval_request
        assert store.get_tool_operation(owner_id=_OWNER_ID, run_id=run_id, operation_id=operation_id) is None

        approved_boundary = {
            **boundary,
            "approved_actions": [
                {
                    **dict(ask_exec.decision.approval_request),
                    "approval_id": "approval-ls-approved",
                    "status": "APPROVED",
                }
            ],
        }
        approved_call, approved_exec = _execute_controlled_call(
            registry,
            arguments=arguments,
            boundary=approved_boundary,
            run_id=run_id,
            call_id="call-ls-approved",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        replay_call, replay_exec = _execute_controlled_call(
            registry,
            arguments=arguments,
            boundary=approved_boundary,
            run_id=run_id,
            call_id="call-ls-approved",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        stored = store.get_tool_operation(
            owner_id=_OWNER_ID,
            run_id=run_id,
            operation_id=operation_id,
        )
    gc.collect()
    fd_delta = _fd_count() - fd_before

    assert fd_delta == 0
    assert dispatch_calls == 0

    _assert_blocked_plan_contract(approved_exec.result)
    assert approved_exec.result.operation is not None
    assert approved_exec.result.operation.status == "failed"
    assert approved_exec.result.operation.effect_outcome == "not_started"
    assert approved_exec.result.operation.replayed is False

    assert stored is not None
    assert stored.status == "failed"

    _assert_blocked_plan_contract(replay_exec.result, expected_failure_stage="effect_reconciliation")
    assert replay_exec.result.operation is not None
    assert replay_exec.result.operation.status == "failed"
    assert replay_exec.result.operation.effect_outcome == "not_started"
    assert replay_exec.result.operation.replayed is True

    _assert_no_unknown_halt(approved_call, approved_exec.result, run_id=run_id, attempt_id=attempt_id)
    _assert_no_unknown_halt(replay_call, replay_exec.result, run_id=run_id, attempt_id=attempt_id)


def test_allowed_dry_run_and_approved_execution_succeed_after_blocked_refusal(
    tmp_path: Path,
) -> None:
    """被拒计划之后，同一运行下已授权的 pwd 干跑计划与批准后的真实执行均正常成功。"""
    run_id = "run-r2-followup"
    attempt_id = "attempt-r2-followup"

    with _managed_registry_and_store(tmp_path) as (workspace, registry, store):
        boundary = _pwd_only_boundary(workspace, run_id=run_id)

        blocked_op = f"tool_call:{attempt_id}:call-1"
        _, blocked_exec = _execute_controlled_call(
            registry,
            arguments={"grant_id": "grant-shell-1", "command": "ls", "cwd": str(workspace)},
            boundary=boundary,
            run_id=run_id,
            call_id="call-1",
            attempt_id=attempt_id,
            operation_id=blocked_op,
            idempotency_key=operation_idempotency_key(attempt_id, blocked_op),
        )
        _assert_blocked_plan_contract(blocked_exec.result)

        dry_op = f"tool_call:{attempt_id}:call-2"
        _, dry_exec = _execute_controlled_call(
            registry,
            arguments={"grant_id": "grant-shell-1", "command": "pwd", "cwd": str(workspace), "apply": False},
            boundary=boundary,
            run_id=run_id,
            call_id="call-2",
            attempt_id=attempt_id,
            operation_id=dry_op,
            idempotency_key=operation_idempotency_key(attempt_id, dry_op),
        )
        assert dry_exec.result.ok is True
        dry_data = json.loads(dry_exec.result.output)
        assert dry_data["mode"] == "dry_run"
        assert dry_data["allowed"] is True
        assert dry_data["action"] == "execute_shell"

        exec_op = f"tool_call:{attempt_id}:call-3"
        exec_args: dict[str, object] = {
            "grant_id": "grant-shell-1",
            "command": "pwd",
            "cwd": str(workspace),
            "apply": True,
        }
        _, ask_pwd = _execute_controlled_call(
            registry,
            arguments=exec_args,
            boundary=boundary,
            run_id=run_id,
            call_id="call-3",
            attempt_id=attempt_id,
            operation_id=exec_op,
            idempotency_key=operation_idempotency_key(attempt_id, exec_op),
        )
        assert ask_pwd.result.status == "approval_required"
        approved_pwd_boundary = {
            **boundary,
            "approved_actions": [
                {
                    **dict(ask_pwd.decision.approval_request),
                    "approval_id": "approval-pwd-3",
                    "status": "APPROVED",
                }
            ],
        }
        _, ran_pwd = _execute_controlled_call(
            registry,
            arguments=exec_args,
            boundary=approved_pwd_boundary,
            run_id=run_id,
            call_id="call-3",
            attempt_id=attempt_id,
            operation_id=exec_op,
            idempotency_key=operation_idempotency_key(attempt_id, exec_op),
        )
        assert ran_pwd.result.ok is True
        ran_data = json.loads(ran_pwd.result.output)
        assert ran_data["mode"] == "execute"
        assert ran_data["execution"]["executed"] is True
        assert ran_data["execution"]["exit_code"] == 0
        stored_pwd = store.get_tool_operation(
            owner_id=_OWNER_ID,
            run_id=run_id,
            operation_id=exec_op,
        )
        assert stored_pwd is not None and stored_pwd.status == "succeeded"
