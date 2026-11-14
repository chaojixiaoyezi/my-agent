"""验证副作用工具在执行前校验白名单错误码下的 effect_outcome 归一与终态重放契约。"""
from __future__ import annotations

import gc
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
from agent_py_agent.agent.contracts.idempotency import operation_idempotency_key
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    IdempotencyPolicy,
    ToolHandlerOutcome,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.tooling.tool_operation_coordinator import (
    _PRE_HANDLER_DETERMINISTIC_CODES,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)

_OWNER_ID = "owner-r1"
_WHITELISTED_CODES = tuple(sorted(_PRE_HANDLER_DETERMINISTIC_CODES))


def _fd_count() -> int:
    """统计当前进程打开的文件描述符数量，用于验证执行前后零泄漏。"""
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


class _PreHandlerProbeTool(BaseTool):
    """返回指定错误码且不显式填写 effect_outcome 的副作用探针工具。"""

    def __init__(self, error_code: str, *, name: str = "probe_mutating_validation") -> None:
        self.calls = 0
        self.error_code = error_code
        self.model_spec = make_test_model_spec(
            name,
            description="Deterministic validation probe tool.",
            input_schema={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "Test input item."},
                },
                "additionalProperties": False,
            },
        )
        self.runtime_policy = replace(
            make_test_runtime_policy("mutating"),
            idempotency_policy=IdempotencyPolicy(scope="operation"),
        )

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        _ = params
        self.calls += 1
        return ToolHandlerOutcome(
            self.model_spec.name,
            False,
            f"deterministic validation failure: {self.error_code}",
            error_code=self.error_code,
        )


@contextmanager
def _managed_registry_and_store(
    tmp_path: Path,
    tool: BaseTool,
    *,
    db_name: str = "ops.db",
) -> Iterator[tuple[ToolRegistry, LocalStore]]:
    """通过 ExitStack 托管 LocalStore 与 ToolRegistry 生命周期，确保句柄完整回收。"""
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
                max_chars=8_000,
                max_entries=100,
                max_matches=50,
                web_max_chars=8_000,
                http_timeout=5,
                catalog_limit=20,
                retrieval_limit=5,
                vector_search_enabled=False,
                operation_store=store,
                operation_store_required=True,
                operation_owner_id=_OWNER_ID,
            )
        )
        stack.callback(registry.close_mcp_clients)
        registry.register(tool)
        yield registry, store


def _execute_real_call(
    registry: ToolRegistry,
    *,
    tool_name: str,
    arguments: dict[str, object],
    run_id: str,
    call_id: str,
    attempt_id: str,
    operation_id: str,
    idempotency_key: str,
) -> ToolResult:
    """走真实 ToolRegistry.execute_tool -> ToolExecutor -> Coordinator -> LocalStore 链路。"""
    snapshot = registry.runtime_snapshot(run_id=run_id)
    call = canonical_test_call(
        snapshot,
        tool_name,
        arguments,
        call_id=call_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    return registry.execute_tool(
        call,
        write_boundary=None,
        runtime_snapshot=snapshot,
        trusted_run_context={
            "run_scope": {
                "request_id": f"req-{run_id}",
                "task_id": run_id,
                "owner_type": "user",
                "owner_id": _OWNER_ID,
            }
        },
    ).result


@pytest.mark.parametrize("error_code", _WHITELISTED_CODES)
def test_a_whitelisted_pre_handler_codes_derive_not_started(
    tmp_path: Path,
    error_code: str,
) -> None:
    """a+e: 白名单内全部执行前确定性错误码均落盘 failed 并归一为 not_started，且 fd_delta == 0。"""
    probe = _PreHandlerProbeTool(error_code)
    run_id = f"run-a-{error_code.lower()}"
    attempt_id = f"attempt-a-{error_code.lower()}"
    operation_id = f"tool_call:{attempt_id}:call-1"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path, probe) as (registry, store):
        result = _execute_real_call(
            registry,
            tool_name=probe.model_spec.name,
            arguments={"item": "alpha"},
            run_id=run_id,
            call_id="call-1",
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
    assert probe.calls == 1
    assert stored is not None
    assert stored.status == "failed"
    assert result.status == "failed"
    assert result.effect_outcome == "not_started"
    assert result.operation is not None
    assert result.operation.status == "failed"
    assert result.operation.effect_outcome == "not_started"


@pytest.mark.parametrize("error_code", _WHITELISTED_CODES)
def test_b_whitelisted_pre_handler_replay_preserves_not_started_and_single_call(
    tmp_path: Path,
    error_code: str,
) -> None:
    """b+e: 同一 operation_id 与 idempotency_key 重放返回终态结果，effect_outcome 仍为 not_started 且 handler 仅执行 1 次。"""
    probe = _PreHandlerProbeTool(error_code)
    run_id = f"run-b-{error_code.lower()}"
    attempt_id = f"attempt-b-{error_code.lower()}"
    operation_id = f"tool_call:{attempt_id}:call-replay"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path, probe) as (registry, store):
        first = _execute_real_call(
            registry,
            tool_name=probe.model_spec.name,
            arguments={"item": "beta"},
            run_id=run_id,
            call_id="call-replay-1",
            attempt_id=attempt_id,
            operation_id=operation_id,
            idempotency_key=idem_key,
        )
        replay = _execute_real_call(
            registry,
            tool_name=probe.model_spec.name,
            arguments={"item": "beta"},
            run_id=run_id,
            call_id="call-replay-2",
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
    assert probe.calls == 1
    assert stored is not None and stored.status == "failed"
    assert replay.operation is not None and replay.operation.replayed is True
    assert replay.status == "failed"
    assert replay.effect_outcome == "not_started"
    assert first.effect_outcome == "not_started"


def test_c_negative_control_non_whitelisted_failure_stays_unknown(tmp_path: Path) -> None:
    """c+e: 非白名单错误码 TOOL_EXECUTION_FAILED 无显式副作用结论时仍保持 unknown，且 fd_delta == 0。"""
    probe = _PreHandlerProbeTool("TOOL_EXECUTION_FAILED")
    run_id = "run-c-negative"
    attempt_id = "attempt-c-negative"
    operation_id = f"tool_call:{attempt_id}:call-neg"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path, probe) as (registry, store):
        result = _execute_real_call(
            registry,
            tool_name=probe.model_spec.name,
            arguments={"item": "gamma"},
            run_id=run_id,
            call_id="call-neg",
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
    assert probe.calls == 1
    assert stored is not None and stored.status == "unknown"
    assert result.effect_outcome == "unknown"


def test_d_update_persona_missing_content_derives_not_started(tmp_path: Path) -> None:
    """d+e: UpdatePersonaTool 缺少 content 返回 TOOL_INVALID_ARGUMENTS 时归一为 not_started，且 fd_delta == 0。"""
    persona_tool = UpdatePersonaTool(SimpleNamespace(_current_run_params=None))
    run_id = "run-d-persona"
    attempt_id = "attempt-d-persona"
    operation_id = f"tool_call:{attempt_id}:call-persona"
    idem_key = operation_idempotency_key(attempt_id, operation_id)

    gc.collect()
    fd_before = _fd_count()
    with _managed_registry_and_store(tmp_path, persona_tool) as (registry, store):
        result = _execute_real_call(
            registry,
            tool_name="update_persona",
            arguments={"action": "add", "target": "user"},
            run_id=run_id,
            call_id="call-persona",
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
    assert stored is not None and stored.status == "failed"
    assert result.status == "failed"
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"
