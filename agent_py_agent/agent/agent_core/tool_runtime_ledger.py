# LLM: 本模块把已验证运行账本、scope、rate/guardrail 与临时审批 binding 合并进每次 ToolExecutor 请求；不得从模型正文恢复控制字段。
# 模块用途: 维护工具运行门的持久证据，并生成当前调用使用的权威 write boundary。

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.protocol_status import TOOL_STATUS_DONE, TOOL_STATUS_FAILED
from ..conversation.authority import CONVERSATION_TRANSIENT_WORKSPACE_ATTR
from ..local_storage import RuntimeGateLedgerRecord
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ..tooling.runtime_contracts import tool_arguments_hash
from .tool_guard.call_guardrail import tool_guardrail_policy, tool_guardrail_records


def persist_tool_runtime_ledger(agent: object, archive_record: dict[str, object]) -> None:
    store = getattr(agent, "local_store", None)
    if not hasattr(store, "record_runtime_gate_ledger"):
        return
    record = runtime_gate_ledger_record_from_archive(archive_record)
    if record is None:
        return
    _best_effort_control_plane_write(lambda: store.record_runtime_gate_ledger(record))
    # A.3：工具完成事件写入权威 runtime_events（agent_events 可从中重建），
    # 与 legacy 台账并行 —— 权威事件流是审计/重建的单一事实源。
    _append_runtime_event(agent, archive_record)


def _append_runtime_event(agent: object, archive_record: dict[str, object]) -> None:
    """把「工具调用完成」追进权威 runtime_events（A.3/A.8 追到 attempt）。

    agent.subagents.runtime_db 是 owner 权威库（R1 接线）；无权威库
    （无 home 上下文/纯测试）时静默跳过，legacy 台账不受影响。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not hasattr(repo, "append_event"):
        return
    run_id = _text(archive_record.get("run_id"))
    operation_id = _text(archive_record.get("operation_id"))
    if not run_id or not operation_id:
        return
    try:
        agent_run_row = repo.agent_run_for_run_id(run_id)
    except (sqlite3.Error, OSError):
        return
    if agent_run_row is None:
        return
    runtime_gate = _dict_value(archive_record.get("runtime_gate"))
    payload = {
        "operation_id": operation_id,
        "tool": _text(archive_record.get("tool")),
        "ok": bool(archive_record.get("ok")),
        "error_code": _text(archive_record.get("error_code")),
        "status": _ledger_status(archive_record, runtime_gate),
        "idempotency_key": _text(archive_record.get("idempotency_key")),
    }
    _best_effort_control_plane_write(
        lambda: repo.append_event(
            event_type="tool_completed",
            attempt_id=_text(archive_record.get("attempt_id")),
            agent_run_id=str(agent_run_row["agent_run_id"]),
            task_run_id=str(agent_run_row["task_run_id"] or ""),
            payload=payload,
        )
    )


def _best_effort_control_plane_write(write_fn) -> None:
    """尽力而为的控制面(遥测/审计台账)写入:绝不能因写台账失败而崩掉真正的任务。sqlite 的任何
    OperationalError(不止 'database is locked',还有磁盘满的 'disk I/O error'、只读、损坏等)和 OSError
    都吞掉——真机 dogfooding:大数据任务塞满磁盘时 'disk I/O error' 原会 re-raise 把整个任务崩掉(台账是
    非关键观测数据,丢一条可以、崩任务不行;逻辑 bug 如 AttributeError 非 sqlite3.Error/OSError 仍会 surface)。"""
    try:
        write_fn()
    except (sqlite3.Error, OSError):
        return


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 合并顺序必须保留调用方已有 boundary，再追加宿主账本和当前 run 的精确审批；任何临时批准都不能覆盖原安全范围。
# 函数用途: 生成一次工具执行使用的完整结构化边界。
def write_boundary_with_runtime_ledger(agent: object, params: object) -> dict[str, object] | None:
    boundary = getattr(params, "write_boundary", None)
    merged = dict(boundary) if isinstance(boundary, dict) else {}
    _attach_runtime_approved_actions(merged, params)
    _attach_task_workspace_roots(merged, params)
    _attach_main_conversation_execution_cwd(merged, agent, params)
    # 配置级额外写根（additional_write_roots）：测试共享工作区等场景扩展沙箱写域。
    extra_roots = _string_list(getattr(getattr(agent, "config", None), "additional_write_roots", ()))
    if extra_roots:
        existing = _string_list(merged.get("allowed_write_roots"))
        merged["allowed_write_roots"] = list(dict.fromkeys([*existing, *extra_roots]))
    _attach_remote_owner_task_write_scope(merged, agent, params)
    _attach_transient_named_work_write_scope(merged, agent, params)
    _attach_active_child_output_locks(merged, agent, params)
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
    run_id = _text(getattr(params, "run_id", ""))
    if not run_id:
        return merged or boundary
    rate_rows = _runtime_tool_rate_limit_rows(store, run_id)
    if rate_rows:
        merged["tool_rate_limit_records"] = _merged_rate_limit_rows(
            merged.get("tool_rate_limit_records"), rate_rows
        )
    return merged or boundary


# LLM: runtime_approved_actions 只接受当前 ToolLoopExecuteParams 中已校验 dict，并与调用方批准列表按完整字段去重。
# 函数用途: 将本 run 已确认的一次性工具批准加入 ActionPolicy 输入。
def _attach_runtime_approved_actions(
    boundary: dict[str, object],
    params: object,
) -> None:
    runtime_items = getattr(params, "runtime_approved_actions", None)
    if not isinstance(runtime_items, list) or not runtime_items:
        return
    existing = boundary.get("approved_actions")
    combined = [dict(item) for item in existing if isinstance(item, Mapping)] if isinstance(existing, (list, tuple)) else []
    seen = {_approval_binding_key(item) for item in combined}
    for item in runtime_items:
        if not isinstance(item, Mapping):
            continue
        normalized = {str(key): str(value or "").strip() for key, value in item.items()}
        key = _approval_binding_key(normalized)
        if not all(key) or key in seen:
            continue
        combined.append(normalized)
        seen.add(key)
    if combined:
        boundary["approved_actions"] = combined


# LLM: 去重 key 覆盖批准身份全部安全字段；仅 tool/idempotency 相同不能合并不同参数或 operation。
# 函数用途: 返回批准记录的精确身份元组。
def _approval_binding_key(value: Mapping[object, object]) -> tuple[str, ...]:
    return tuple(
        _text(value.get(key))
        for key in (
            "approval_id",
            "tool_name",
            "run_id",
            "operation_id",
            "idempotency_key",
            "args_hash",
            "status",
        )
    )


def _attach_transient_named_work_write_scope(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    """Keep an exact named-work prepare turn inside its durable work/output dirs.

    A local/admin CLI normally has a broad workspace.  That must not turn an
    Audit prepare turn into permission to scatter source profiles beside the
    named Audit and later fail evidence publication.  The scope comes only
    from the ingress-created transient-workspace marker and canonical owner
    paths; no prompt text or Audit name participates in the decision.
    """

    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict) or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is not True:
        return
    owner_home_text = _text(getattr(getattr(agent, "home_paths", None), "owner_home_dir", ""))
    task_root = _resolved_path(boundary.get("task_root"))
    work_dir = _resolved_path(boundary.get("task_work_dir"))
    output_dir = _resolved_path(boundary.get("task_output_dir"))
    owner_home = _resolved_path(owner_home_text)
    if (
        owner_home is None
        or task_root is None
        or work_dir != (task_root / "work").resolve(strict=False)
        or output_dir != (task_root / "output").resolve(strict=False)
        or task_root.parent != (owner_home / "audits").resolve(strict=False)
    ):
        boundary["allowed_write_roots"] = []
        return
    boundary["allowed_write_roots"] = [str(work_dir), str(output_dir)]


def _attach_task_workspace_roots(boundary: dict[str, object], params: object) -> None:
    workspace = _run_workspace(params)
    if not workspace:
        return
    for source_key, target_key in (
        ("task_root", "task_root"),
        ("output_dir", "task_output_dir"),
        ("work_dir", "task_work_dir"),
    ):
        text = _text(workspace.get(source_key))
        if text:
            boundary[target_key] = text
    requested_output = _text(workspace.get("user_requested_output_dir"))
    if requested_output and not _text(boundary.get("user_requested_output_dir")):
        boundary["user_requested_output_dir"] = requested_output
    # WRITE-02(2026-08-15 真机): CLI run(local) 的 write_boundary 初始无 allowed_write_roots,
    # 导致沙箱 write_roots 为空——任务目录不可写、tmp 根回退项目目录(SANDBOX-01 复验受阻)。
    # 主链任务(run_workspace 存在)且无既有写根时, 注入任务 work/output 为沙箱写根;
    # task_local/control_plane 保持排除(子代理授权不得反向放大, 与 _main_task_write_roots 同规)。
    scope = str(getattr(params, "context_scope", "") or "").strip().lower()
    if scope in {"task_local", "control_plane"}:
        return
    existing = _string_list(boundary.get("allowed_write_roots"))
    if not existing:
        work_dir = _text(boundary.get("task_work_dir"))
        output_dir = _text(boundary.get("task_output_dir"))
        if work_dir and output_dir:
            boundary["allowed_write_roots"] = [work_dir, output_dir]


# LLM: A local/admin conversation keeps one 会话运行时 project cwd before and
# after task promotion. Hidden task work/output roots are additional runtime
# storage, not a replacement permission boundary. Remote owner task walls and
# transient Audit scopes run later and may deliberately narrow this grant.
# 函数用途: 让本地主会话进入后台任务后仍能在启动时的项目目录继续读写，避免模型被迫绕到额外测试目录。
def _attach_main_conversation_execution_cwd(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    attrs = getattr(params, "task_attributes", None)
    if (
        not _is_main_conversation_task_run(params)
        or str(getattr(params, "source", "") or "").strip().lower() == "cli_run"
        or not isinstance(attrs, dict)
        or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True
    ):
        return
    tools = getattr(agent, "tools", None)
    project_cwd = _resolved_path(getattr(tools, "workspace_root", None))
    if project_cwd is None:
        project_cwd = _resolved_path(
            getattr(agent, "effective_workspace_root", getattr(agent, "root", None))
        )
    if project_cwd is None:
        return
    cwd_text = str(project_cwd)
    boundary["execution_cwd"] = cwd_text
    provider = _text(getattr(getattr(agent, "config", None), "my_agent_owner_provider", "")).lower()
    owner_scope = _text(getattr(tools, "owner_scope_root", ""))
    if provider not in ("", "local") and owner_scope:
        return
    roots = _string_list(boundary.get("allowed_write_roots"))
    if cwd_text not in roots:
        boundary["allowed_write_roots"] = [*roots, cwd_text]


def _attach_remote_owner_task_write_scope(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
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

    if _is_main_conversation_task_run(params):
        # A foreground turn can start as ordinary chat and promote/select a task
        # after the tool loop is already running.  The live structured
        # run_workspace is then authoritative, just as 会话运行时 rebuilds tools from
        # the current TurnContext cwd/workspace_roots and 通道运行时 prepares each
        # attempt from effectiveCwd/effectiveWorkspace.  Reusing the request's
        # bootstrap roots here would leave the main agent bound to its old cwd.
        # task_local/control_plane runs are deliberately excluded below so a
        # child agent's narrower grant is never widened to the parent task root.
        boundary["allowed_write_roots"] = _main_task_write_roots(
            boundary,
            task_root=task_root,
            owner_root=owner_root,
        )
        return

    if _is_exact_subagent_workspace_rebase(params, task_root):
        boundary["allowed_write_roots"] = [str(task_root)]
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


def _main_task_write_roots(
    boundary: dict[str, object],
    *,
    task_root: Path,
    owner_root: Path,
) -> list[str]:
    """Return the host-authored writable areas for a provider task turn.

    The task root is the stable cwd/read container.  Product files belong in
    ``output`` and transient work belongs in ``work``; granting the whole root
    lets models create parallel ad-hoc project directories beside those two
    canonical locations.  Keep an explicit requested output directory only
    when the structured workspace contract places it inside the same owner.
    """

    roots: list[str] = []
    for key, expected in (
        ("task_output_dir", task_root / "output"),
        ("task_work_dir", task_root / "work"),
    ):
        raw = _text(boundary.get(key))
        candidate = _resolved_path(raw) if raw else None
        if candidate != expected.resolve(strict=False):
            continue
        text = str(candidate)
        if text not in roots:
            roots.append(text)

    requested_text = _text(boundary.get("user_requested_output_dir"))
    requested = _resolved_path(requested_text) if requested_text else None
    owner_tasks = (owner_root / "tasks").resolve(strict=False)
    if (
        requested is not None
        and requested != owner_root
        and _is_relative_to(requested, owner_root)
        and not _is_relative_to(requested, owner_tasks)
    ):
        text = str(requested)
        if text not in roots:
            roots.append(text)
    return roots


def _is_main_conversation_task_run(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return False
    context_scope = _text(getattr(params, "context_scope", "default")).lower() or "default"
    return bool(
        context_scope == "default"
        and _text(attrs.get("conversation_thread_id"))
        and _text(attrs.get("conversation_task_id"))
    )


def _is_exact_subagent_workspace_rebase(params: object, task_root: Path) -> bool:
    """Trust only the host-authored exact-task rebase marker for a child cwd."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return False
    marker = attrs.get("conversation_subagent_workspace_rebase")
    if not isinstance(marker, dict):
        return False
    context_scope = _text(getattr(params, "context_scope", "default")).lower()
    marked_root = _resolved_path(marker.get("task_root"))
    return bool(
        context_scope == "task_local"
        and _text(attrs.get("conversation_thread_id"))
        and _text(attrs.get("conversation_task_id"))
        and _text(marker.get("task_id"))
        and marked_root == task_root
    )


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


def _attach_active_child_output_locks(
    boundary: dict[str, object], agent: object, params: object
) -> None:
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
    current_lineage = _current_lineage_ids(tasks, current_ids)
    refs: list[str] = []
    for task in tasks:
        _append_active_child_output_refs(refs, task, current_ids, current_lineage)
    return refs


def _append_active_child_output_refs(
    refs: list[str],
    task: object,
    current_ids: set[str],
    current_lineage: set[str],
) -> None:
    if not _is_active_child_for_current_run(task, current_ids, current_lineage):
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


def _is_active_child_for_current_run(
    task: object,
    current_ids: set[str],
    current_lineage: set[str],
) -> bool:
    if not current_ids:
        return False
    # 正主不锁自己:子代理跑轮的 current_ids 含它自己的 run_id,而它声明的 output_files
    # 正是派工点名要它写的活——把自己的申报单也塞进 locked_files 会把它锁在门外
    # (真机实锤:子代理被"output/inventory.py 在 locked_files 中"拦死,capability 已
    # GRANTED 也无济于事,3/4 子代理被迫由主代理接管代写)。锁的正当用途是拦
    # 【别人】(兄弟/主代理中途)乱写在建产物,单向外溢保护,不拦正主。
    if _text(getattr(task, "id", "")) in current_lineage:
        return False
    if task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES):
        return False
    return (
        _text(getattr(task, "parent_id", "")) in current_ids
        or _text(getattr(task, "root_id", "")) in current_ids
    )


def _current_lineage_ids(tasks: list[object], current_ids: set[str]) -> set[str]:
    """Return current subagent plus ancestors so delegated outputs stay writable."""

    by_id = {task_id: task for task in tasks if (task_id := _text(getattr(task, "id", "")))}
    lineage = set(current_ids)
    pending = [task_id for task_id in current_ids if task_id in by_id]
    while pending:
        task = by_id[pending.pop()]
        parent_id = _text(getattr(task, "parent_id", ""))
        if parent_id and parent_id not in lineage:
            lineage.add(parent_id)
            if parent_id in by_id:
                pending.append(parent_id)
    return lineage


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


def runtime_gate_ledger_record_from_archive(
    archive_record: dict[str, object],
) -> RuntimeGateLedgerRecord | None:
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
    return _text(record.get("operation_id"))


def _idempotency_key(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("idempotency_key"))
    if direct:
        return direct
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("idempotency_key"))


def _approval_id(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("approval_id"))
    if direct:
        return direct
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("approval_id"))


def _args_hash(record: Mapping[str, object]) -> str:
    direct = _text(record.get("args_hash"))
    if direct:
        return direct
    parameters = _dict_value(record.get("parameters"))
    if not parameters:
        return ""
    arguments = {key: value for key, value in parameters.items() if key not in {"tool", "call_id"}}
    return tool_arguments_hash(arguments)


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
    timestamp = float(
        getattr(record, "created_at", 0.0) or getattr(record, "updated_at", 0.0) or 0.0
    )
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


def _merged_rate_limit_rows(
    existing: object, persisted: tuple[dict[str, object], ...]
) -> tuple[dict[str, object], ...]:
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
