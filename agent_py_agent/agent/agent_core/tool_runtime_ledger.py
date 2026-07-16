
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.gates.tool_effects import args_hash_for_call
from ..contracts.protocol_status import TOOL_STATUS_DONE, TOOL_STATUS_FAILED
from ..contracts.tool_protocol_v2 import normalize_tool_call
from ..local_storage import RuntimeGateLedgerRecord
from ..local_storage.control_plane_models import AgentEventInput
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ..user_space.network_grants import active_private_hosts
from .tool_guard.call_guardrail import tool_guardrail_policy, tool_guardrail_records


def persist_tool_runtime_ledger(agent: object, archive_record: dict[str, object]) -> None:
    store = getattr(agent, "local_store", None)
    _best_effort_control_plane_write(lambda: _record_tool_agent_event(store, archive_record))
    if not hasattr(store, "record_runtime_gate_ledger"):
        return
    record = runtime_gate_ledger_record_from_archive(archive_record)
    if record is None:
        return
    _best_effort_control_plane_write(lambda: store.record_runtime_gate_ledger(record))


def _best_effort_control_plane_write(write_fn) -> None:
    """尽力而为的控制面(遥测/审计台账)写入:绝不能因写台账失败而崩掉真正的任务。sqlite 的任何
    OperationalError(不止 'database is locked',还有磁盘满的 'disk I/O error'、只读、损坏等)和 OSError
    都吞掉——真机 dogfooding:大数据任务塞满磁盘时 'disk I/O error' 原会 re-raise 把整个任务崩掉(台账是
    非关键观测数据,丢一条可以、崩任务不行;逻辑 bug 如 AttributeError 非 sqlite3.Error/OSError 仍会 surface)。"""
    try:
        write_fn()
    except (sqlite3.Error, OSError):
        return


def _record_tool_agent_event(store: object, archive_record: dict[str, object]) -> None:
    if not hasattr(store, "record_agent_event"):
        return
    run_id = _text(archive_record.get("run_id"))
    if not run_id:
        return
    scope = _dict_value(archive_record.get("run_scope"))
    root_task_id = (
        _text(scope.get("root_task_id"))
        or _text(archive_record.get("root_task_id"))
        or _text(archive_record.get("task_id"))
        or run_id
    )
    event = AgentEventInput(
        root_task_id=root_task_id,
        run_id=run_id,
        parent_run_id=_text(scope.get("parent_run_id")) or _text(archive_record.get("parent_run_id")),
        event_type="tool_call_finished",
        payload=_tool_event_payload(archive_record, scope),
    )
    store.record_agent_event(event)


def _tool_event_payload(
    archive_record: dict[str, object],
    scope: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool": _text(archive_record.get("tool")),
        "ok": bool(archive_record.get("ok")),
        "call_id": _text(archive_record.get("call_id")),
        "operation_id": _operation_id(archive_record),
        "scope": dict(scope),
    }
    for key in ("error_code", "error_category", "recommended_action", "result_ref"):
        value = _text(archive_record.get(key))
        if value:
            payload[key] = value
    refs = archive_record.get("tool_result_refs")
    if isinstance(refs, list):
        payload["tool_result_refs"] = [item for item in refs if isinstance(item, dict)]
    return payload


def write_boundary_with_runtime_ledger(agent: object, params: object) -> dict[str, object] | None:
    boundary = getattr(params, "write_boundary", None)
    merged = dict(boundary) if isinstance(boundary, dict) else {}
    _attach_task_workspace_roots(merged, params)
    _attach_remote_owner_task_write_scope(merged, agent)
    _attach_active_child_output_locks(merged, agent, params)
    _attach_owner_network_grants(merged, agent)
    guardrail_rows = tool_guardrail_records(agent)
    if guardrail_rows:
        merged["tool_guardrail_records"] = _merged_tool_guardrail_rows(
            merged.get("tool_guardrail_records"),
            guardrail_rows,
        )
    guardrail_policy = tool_guardrail_policy(params)
    if guardrail_policy:
        merged["tool_guardrail_policy"] = guardrail_policy
    store = getattr(agent, "local_store", None)
    if not hasattr(store, "runtime_idempotency_ledger"):
        return merged or boundary
    run_id = _text(getattr(params, "run_id", ""))
    if not run_id:
        return merged or boundary
    persisted = store.runtime_idempotency_ledger(run_id=run_id)
    if persisted:
        merged["idempotency_ledger"] = _merged_idempotency_rows(merged.get("idempotency_ledger"), persisted)
    rate_rows = _runtime_tool_rate_limit_rows(store, run_id)
    if rate_rows:
        merged["tool_rate_limit_records"] = _merged_rate_limit_rows(merged.get("tool_rate_limit_records"), rate_rows)
    return merged or boundary


def _attach_task_workspace_roots(boundary: dict[str, object], params: object) -> None:
    workspace = _run_workspace(params)
    if not workspace:
        return
    for source_key, target_key in (
        ("task_root", "task_root"),
        ("output_dir", "task_output_dir"),
        ("work_dir", "task_work_dir"),
        ("user_requested_output_dir", "user_requested_output_dir"),
    ):
        text = _text(workspace.get(source_key))
        if text and not _text(boundary.get(target_key)):
            boundary[target_key] = text


def _attach_remote_owner_task_write_scope(boundary: dict[str, object], agent: object) -> None:
    """把远程普通 owner 的通用写工具收窄到当前任务，local/admin 保持原有权限。"""

    # LLM: provider、owner_scope_root、task_root 都是运行时结构化事实；不能从用户自然语言
    # 猜“是不是续做旧任务”。同一 owner 可以读取旧任务作参考，但普通文件工具和 shell 只写
    # 当前已选任务。admin bypass 会把 owner_scope_root 置空，因此显式高权限豁免不受影响。
    # 人类: 飞书同一用户的多个任务共用 owner home，owner 墙只能防串用户，不能防串任务；
    # 这里补第二层任务墙，避免新任务拿绝对路径回写兄弟任务目录。
    provider = _text(getattr(getattr(agent, "config", None), "my_agent_owner_provider", "")).lower()
    owner_scope = _text(getattr(getattr(agent, "tools", None), "owner_scope_root", ""))
    task_root_text = _text(boundary.get("task_root"))
    if provider in ("", "local") or not owner_scope or not task_root_text:
        return

    owner_root = _resolved_path(owner_scope)
    task_root = _resolved_path(task_root_text)
    if owner_root is None or task_root is None or not _is_strict_task_root(task_root, owner_root):
        # 显式空白名单与“键缺失”不同：write_boundary 会 fail-closed；shell 则把 owner home
        # 只读挂载。这样畸形/伪造 task_root 不会悄悄退回“整个 owner home 可写”。
        boundary["allowed_write_roots"] = []
        return

    existing_roots = _string_list(boundary.get("allowed_write_roots"))
    if not existing_roots:
        boundary["allowed_write_roots"] = [str(task_root)]
        return

    # 已有的结构化授权通常来自子代理分工，可能比 task_root 更窄；保留其窄权限，但过滤掉
    # 兄弟任务或 owner 其他目录，绝不因追加 task_root 而把子代理权限反向放大。
    scoped: list[str] = []
    for raw in existing_roots:
        candidate = _resolved_path(raw)
        if candidate is not None and _is_relative_to(candidate, task_root):
            text = str(candidate)
            if text not in scoped:
                scoped.append(text)
    boundary["allowed_write_roots"] = scoped


def _resolved_path(raw: object) -> Path | None:
    try:
        return Path(str(raw)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _is_strict_task_root(task_root: Path, owner_root: Path) -> bool:
    tasks_root = (owner_root / "tasks").resolve(strict=False)
    return task_root != tasks_root and _is_relative_to(task_root, tasks_root)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _attach_owner_network_grants(boundary: dict[str, object], agent: object) -> None:
    """把 owner 授权过的内网主机灌进 allowed_private_hosts(N1 接线:授权存储 → 出站闸)。
    这里是主/子代理每次工具调用共用的 boundary chokepoint,一处灌入,network_safety 出站闸与
    path_url_command 预检闸同时放行。必须逐次新鲜读盘:授权发生在 run 中途(authorize_network_host),
    init 期缓存会漏掉同轮生效;grant 目录只有几个小 JSON,读一次微秒级。与既有值取并集不覆盖。"""
    owner_home = _text(getattr(getattr(agent, "home_paths", None), "owner_home_dir", ""))
    if not owner_home:
        return
    hosts = active_private_hosts(Path(owner_home))
    if not hosts:
        return
    existing = _string_list(boundary.get("allowed_private_hosts"))
    boundary["allowed_private_hosts"] = [*existing, *(host for host in hosts if host not in existing)]


def _attach_active_child_output_locks(boundary: dict[str, object], agent: object, params: object) -> None:
    locks = _active_child_output_refs(agent, params)
    if not locks:
        return
    existing = _string_list(boundary.get("locked_files"))
    boundary["locked_files"] = [*existing, *(item for item in locks if item not in existing)]


def _active_child_output_refs(agent: object, params: object) -> list[str]:
    subagents = getattr(agent, "subagents", None)
    if not hasattr(subagents, "list_runs"):
        return []
    try:
        tasks = subagents.list_runs()
    except (OSError, RuntimeError, ValueError):
        return []
    current_ids = _current_task_ids(params)
    refs: list[str] = []
    for task in tasks:
        _append_active_child_output_refs(refs, task, current_ids)
    return refs


def _append_active_child_output_refs(refs: list[str], task: object, current_ids: set[str]) -> None:
    if not _is_active_child_for_current_run(task, current_ids):
        return
    for ref in _declared_output_refs(task):
        if ref not in refs:
            refs.append(ref)


def _current_task_ids(params: object) -> set[str]:
    values = {
        _text(getattr(params, "run_id", "")),
        _text(getattr(params, "task_id", "")),
    }
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        values.add(_text(attrs.get("conversation_task_id")))
    return {value for value in values if value}


def _is_active_child_for_current_run(task: object, current_ids: set[str]) -> bool:
    if not current_ids:
        return False
    # 正主不锁自己:子代理跑轮的 current_ids 含它自己的 run_id,而它声明的 output_files
    # 正是派工点名要它写的活——把自己的申报单也塞进 locked_files 会把它锁在门外
    # (真机实锤:子代理被"output/inventory.py 在 locked_files 中"拦死,capability 已
    # GRANTED 也无济于事,3/4 子代理被迫由主代理接管代写)。锁的正当用途是拦
    # 【别人】(兄弟/主代理中途)乱写在建产物,单向外溢保护,不拦正主。
    if _text(getattr(task, "id", "")) in current_ids:
        return False
    if task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES):
        return False
    return _text(getattr(task, "parent_id", "")) in current_ids or _text(getattr(task, "root_id", "")) in current_ids


def _declared_output_refs(task: object) -> list[str]:
    attrs = getattr(task, "attributes", None)
    refs: list[str] = []
    if isinstance(attrs, dict):
        for key in ("output_files", "output_refs", "artifact_refs"):
            refs.extend(_string_list(attrs.get(key)))
    for key in ("output_files", "output_refs", "artifact_refs"):
        refs.extend(_string_list(getattr(task, key, None)))
    return list(dict.fromkeys(ref for ref in refs if ref))


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list | tuple | set):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _run_workspace(params: object) -> dict[str, object]:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get("run_workspace"), dict):
        return dict(attrs["run_workspace"])
    contract = getattr(params, "delivery_contract", None)
    if isinstance(contract, dict) and isinstance(contract.get("task_workspace"), dict):
        return dict(contract["task_workspace"])
    return {}


def runtime_gate_ledger_record_from_archive(archive_record: dict[str, object]) -> RuntimeGateLedgerRecord | None:
    runtime_gate = archive_record.get("runtime_gate")
    if not isinstance(runtime_gate, dict):
        return None
    run_id = _text(archive_record.get("run_id"))
    operation_id = _operation_id(archive_record)
    if not run_id or not operation_id:
        return None
    return RuntimeGateLedgerRecord(
        run_id=run_id,
        task_id=_text(archive_record.get("task_id")),
        operation_id=operation_id,
        tool=_text(archive_record.get("tool")),
        parameters=_dict_value(archive_record.get("parameters")),
        runtime_gate=runtime_gate,
        idempotency_key=_idempotency_key(archive_record, runtime_gate),
        args_hash=_args_hash(archive_record),
        approval_id=_approval_id(archive_record, runtime_gate),
        result_ref=_result_ref(archive_record),
        status=_ledger_status(archive_record, runtime_gate),
    )


def _operation_id(record: Mapping[str, object]) -> str:
    direct = _text(record.get("operation_id"))
    if direct:
        return direct
    protocol = _dict_value(record.get("tool_protocol_v2"))
    return _text(protocol.get("operation_id"))


def _idempotency_key(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("idempotency_key"))
    if direct:
        return direct
    protocol = _dict_value(record.get("tool_protocol_v2"))
    protocol_key = _text(protocol.get("idempotency_key"))
    if protocol_key:
        return protocol_key
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("idempotency_key"))


def _approval_id(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("approval_id"))
    if direct:
        return direct
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("approval_id"))


def _args_hash(record: Mapping[str, object]) -> str:
    parameters = _dict_value(record.get("parameters"))
    if not parameters:
        return ""
    call = normalize_tool_call(parameters)
    return args_hash_for_call(call.input)


def _result_ref(record: Mapping[str, object]) -> str:
    for key in ("artifact_ref", "output_path", "source_ref", "path"):
        value = _text(record.get(key))
        if value:
            return value
    return ""


def _ledger_status(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    if runtime_gate.get("allowed") is not True:
        return "blocked"
    return "done" if record.get("ok") is True else "failed"


def _merged_idempotency_rows(existing: object, persisted: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *persisted]:
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("idempotency_key"))
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "idempotency_key": key,
            "args_hash": _text(item.get("args_hash")),
            "status": _text(item.get("status")),
            "result_ref": _text(item.get("result_ref")),
        })
    return tuple(rows)


def _runtime_tool_rate_limit_rows(store: object, run_id: str) -> tuple[dict[str, object], ...]:
    if not hasattr(store, "list_runtime_gate_ledger"):
        return ()
    records = store.list_runtime_gate_ledger(run_id=run_id, limit=500)
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        identity = _rate_limit_identity(record)
        if identity is None:
            continue
        row = by_identity.setdefault(identity, _new_rate_limit_row(identity))
        _apply_rate_limit_record(row, record)
    return tuple(by_identity.values())


def _rate_limit_identity(record: object) -> tuple[str, str] | None:
    tool = _text(getattr(record, "tool", ""))
    args_hash = _text(getattr(record, "args_hash", ""))
    return (tool, args_hash) if tool and args_hash else None


def _new_rate_limit_row(identity: tuple[str, str]) -> dict[str, object]:
    tool, args_hash = identity
    return {
        "tool_name": tool,
        "args_hash": args_hash,
        "attempt_timestamps": [],
        "consecutive_failures": 0,
        "last_failure_at": 0.0,
        "last_success_at": 0.0,
        "total_failures": 0,
    }


def _apply_rate_limit_record(row: dict[str, object], record: object) -> None:
    timestamp = float(getattr(record, "created_at", 0.0) or getattr(record, "updated_at", 0.0) or 0.0)
    if timestamp > 0:
        row["attempt_timestamps"].append(timestamp)
    status = _text(getattr(record, "status", ""))
    if status == TOOL_STATUS_FAILED:
        row["consecutive_failures"] = int(row["consecutive_failures"]) + 1
        row["total_failures"] = int(row["total_failures"]) + 1
        row["last_failure_at"] = timestamp
    if status == TOOL_STATUS_DONE:
        row["consecutive_failures"] = 0
        row["last_success_at"] = timestamp


def _merged_rate_limit_rows(existing: object, persisted: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *persisted]:
        if not isinstance(item, Mapping):
            continue
        tool = _text(item.get("tool_name") or item.get("tool"))
        args_hash = _text(item.get("args_hash"))
        if not tool or not args_hash or (tool, args_hash) in seen:
            continue
        seen.add((tool, args_hash))
        rows.append(dict(item))
    return tuple(rows)


def _merged_tool_guardrail_rows(
    existing: object,
    runtime_rows: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *runtime_rows]:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return tuple(rows[-256:])


def _dict_value(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}

__all__ = [
    "persist_tool_runtime_ledger",
    "runtime_gate_ledger_record_from_archive",
    "write_boundary_with_runtime_ledger",
]
