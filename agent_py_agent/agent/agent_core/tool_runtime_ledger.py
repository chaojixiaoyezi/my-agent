# LLM: 本模块把已验证运行账本、scope、rate/guardrail 与临时审批 binding 合并进每次 ToolExecutor 请求；不得从模型正文恢复控制字段。
# 模块用途: 维护工具运行门的持久证据，并生成当前调用使用的权威 write boundary。

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.protocol_status import TOOL_STATUS_DONE, TOOL_STATUS_FAILED
from ..conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
)
from ..local_storage import RuntimeGateLedgerRecord
from ..path_access_policy import inheritable_declared_work_roots
from ..plugin_observation import observation_event_payload_from_envelope
from ..tooling.runtime_contracts import tool_arguments_hash
from .run_task_workspace_writer import current_run_tool_output_archive_root
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
    # 插件观察候选的查找投影随同一事件落库：新鲜度按事件 seq 判定，归档信封仍是候选内容的唯一权威
    observation = observation_event_payload_from_envelope(
        _dict_value(archive_record.get("tool_result_envelope")).get("observation"),
        task_id=_text(archive_record.get("task_id")), operation_id=operation_id,
    )
    if observation is not None:
        payload["observation"] = observation
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
    _attach_canonical_owner_home_root(merged, agent)
    _attach_effective_owner_scope(merged, agent, params)
    _attach_runtime_approved_actions(merged, params)
    _attach_task_workspace_roots(merged, params)
    _attach_tool_output_read_root(merged, agent, params)
    _attach_main_conversation_execution_cwd(merged, agent, params)
    # 配置级额外写根（additional_write_roots）：测试共享工作区等场景扩展沙箱写域。
    extra_roots = _string_list(getattr(getattr(agent, "config", None), "additional_write_roots", ()))
    if extra_roots:
        existing = _string_list(merged.get("allowed_write_roots"))
        merged["allowed_write_roots"] = list(dict.fromkeys([*existing, *extra_roots]))
    _attach_owner_task_write_scope(merged, agent, params)
    _attach_transient_named_work_write_scope(merged, agent, params)
    _attach_owner_control_write_guards(merged, agent)
    _attach_granted_execution_workspace_roots(merged, agent, params)
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


# LLM: The canonical owner home is an address fact, not a permission grant. Always derive it
# from HomePaths and keep it separate from effective_owner_scope_root, which is intentionally
# absent for a local administrator using Full Access.
# 函数用途: 把当前用户家的真实路径写入工具边界，供 tasks/... 等公开地址稳定还原。
def _attach_canonical_owner_home_root(
    boundary: dict[str, object],
    agent: object,
) -> None:
    owner_home = _resolved_path(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    )
    if owner_home is not None:
        boundary["canonical_owner_home_root"] = str(owner_home)


# LLM: Main runs and subagents need a stable read root matching the first archive write.
# Preserve an explicit child-run root; otherwise bind the host-cached owner-scoped run root.
# Missing canonical agent.root must fail closed by omitting this optional read authority, not by
# guessing from cwd or crashing unrelated read-only/tool-policy calls.
# 函数用途: 有明确用户根目录时让 read_artifact 读本轮大输出；缺根时不猜目录也不扩大权限。
def _attach_tool_output_read_root(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    if _text(boundary.get("artifact_read_root")):
        return
    if not _text(getattr(agent, "root", "")):
        return
    boundary["artifact_read_root"] = str(
        current_run_tool_output_archive_root(agent, params)
    )


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


# LLM: A Full Access parent still creates WorkspaceOnly children. This per-run owner wall is a
# host fact consumed by ToolExecutor and request-local handlers; it cannot come from model args.
# 函数用途: 把当前主/子代理真正生效的 owner 边界写进本轮不可变工具权限快照。
def _attach_effective_owner_scope(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    owner_scope = _effective_owner_scope(agent, params)
    if owner_scope:
        boundary["effective_owner_scope_root"] = owner_scope


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


# LLM: 会话 cwd 与运行归档分离；始终使用宿主验证的 cwd，默认用户 home。权限根不随任务晋升收窄。
# 函数用途: 为主会话提供真实执行位置和完整家目录读范围，防止继承 Gateway 的启动目录。
def _attach_main_conversation_execution_cwd(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    context_scope = _text(getattr(params, "context_scope", "default")).lower() or "default"
    if context_scope in {"task_local", "control_plane"}:
        # 子代理 cwd 已由 runner_context 的结构化项目写合同给出。这里若再用内部
        # run_workspace.task_root 覆盖，就会把用户项目相对路径错误写进状态目录。
        return
    attrs = getattr(params, "task_attributes", None)
    has_typed_client_cwd = bool(
        isinstance(attrs, dict)
        and _text(attrs.get("conversation_thread_id"))
        and _text(attrs.get(CONVERSATION_EXECUTION_CWD_ATTR))
    )
    if (
        not (_is_main_conversation_task_run(params) or has_typed_client_cwd)
        or str(getattr(params, "source", "") or "").strip().lower() == "cli_run"
        or not isinstance(attrs, dict)
        or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True
    ):
        return
    tools = getattr(agent, "tools", None)
    project_cwd = _resolved_path(attrs.get(CONVERSATION_EXECUTION_CWD_ATTR))
    if project_cwd is None:
        project_cwd = _resolved_path(boundary.get("canonical_owner_home_root"))
    if project_cwd is None:
        project_cwd = _resolved_path(getattr(tools, "workspace_root", None))
    if project_cwd is None:
        project_cwd = _resolved_path(
            getattr(agent, "effective_workspace_root", getattr(agent, "root", None))
        )
    if project_cwd is None:
        return
    owner_scope = _resolved_path(getattr(tools, "owner_scope_root", None))
    if owner_scope is not None and not _is_relative_to(project_cwd, owner_scope):
        # 升级前落盘的旧 thread 可能仍带 Gateway daemon cwd。WorkspaceOnly 下只能
        # 回到当前 owner home；不能让一条历史属性重新授予 /root 或源码树权限。
        tool_workspace = _resolved_path(getattr(tools, "workspace_root", None))
        project_cwd = (
            tool_workspace
            if tool_workspace is not None and _is_relative_to(tool_workspace, owner_scope)
            else owner_scope
        )
    cwd_text = str(project_cwd)
    boundary["execution_cwd"] = cwd_text
    requested_roots = [
        *_string_list(attrs.get(CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR)),
        _text(boundary.get("canonical_owner_home_root")),
    ]
    execution_roots: list[str] = []
    for value in [cwd_text, *requested_roots]:
        root = _resolved_path(value)
        if root is None:
            continue
        if owner_scope is not None and not _is_relative_to(root, owner_scope):
            continue
        if str(root) not in execution_roots:
            execution_roots.append(str(root))
    boundary["execution_workspace_roots"] = execution_roots
    if owner_scope is not None:
        return
    roots = _string_list(boundary.get("allowed_write_roots"))
    boundary["allowed_write_roots"] = list(dict.fromkeys([*roots, *execution_roots]))


# LLM: 子代理的产品写根在创建时就由宿主写进 allowed_write_roots；工具执行根必须从同一权威派生，
#   否则路径门只认 owner home / registry 根，会把已经授权的墙外工作目录判成
#   PATH_OWNER_SCOPE_BLOCKED（2026-09-11 真机：4 个子代理各写一个 core_*.go，收工时 6 个文件
#   一个都不存在，子代理自己提交的能力申请里写明"allowed_write_roots 含目标目录但仍被拦"）。
#   只投影"已经授权、且位于 owner 墙外"的根；系统根目录、宿主控制面、其它 owner 的家一律不投影。
#   这一层只补执行根，不放宽 allowed_write_roots，也不引入任何新的写权限。
# 函数用途: 让 owner 墙外的已授权工作目录真正成为本次调用的工作根与相对路径起点。
def _attach_granted_execution_workspace_roots(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    owner_home = _resolved_path(boundary.get("canonical_owner_home_root")) or _resolved_path(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    )
    granted = _granted_external_roots(boundary, owner_home)
    if not granted:
        return
    existing = _string_list(boundary.get("execution_workspace_roots"))
    boundary["execution_workspace_roots"] = list(dict.fromkeys([*existing, *granted]))
    attrs = getattr(params, "task_attributes", None)
    declared_cwd = _resolved_path(
        attrs.get(CONVERSATION_EXECUTION_CWD_ATTR) if isinstance(attrs, dict) else ""
    )
    if declared_cwd is not None and any(
        _is_relative_to(declared_cwd, Path(root)) for root in granted
    ):
        # 父代理已经把"在这里干活"作为结构化事实下发；只有它落在已授权根内才当工作目录。
        boundary["execution_cwd"] = str(declared_cwd)


# LLM: "墙外已授权根"只从当前写边界的结构化字段派生；系统根目录、宿主控制面、其它 owner 的家
#   都由 inheritable_declared_work_roots 过滤掉。owner_home 为空（无 owner 墙）时返回空，
#   因为此时路径门本来就不按 owner 墙裁决，不需要投影。
# 函数用途: 返回当前写边界里位于 owner 墙外、且允许继承的工作根。
def _granted_external_roots(
    boundary: dict[str, object],
    owner_home: Path | None,
) -> list[str]:
    if owner_home is None:
        return []
    granted = inheritable_declared_work_roots(
        boundary.get("allowed_write_roots"),
        owner_home=owner_home,
    )
    return [root for root in granted if not _is_relative_to(Path(root), owner_home)]


# LLM: 普通 main/child 的文件墙是同一 owner home；Audit 精确授权和 control_plane 不扩大。
# 函数用途: 放开自己家里的普通文件操作，继续保护其他用户与宿主权威文件；不看 tasks 命名或模型文字。
def _attach_owner_task_write_scope(
    boundary: dict[str, object],
    agent: object,
    params: object,
) -> None:
    """Apply owner-home scope without turning business folders into permission walls."""

    owner_scope = _effective_owner_scope(agent, params)
    if not owner_scope:
        scope = _text(getattr(params, "context_scope", "default")).lower() or "default"
        if scope not in {"task_local", "control_plane"} and _is_local_full_access(agent):
            # Full Access is intentionally not a workspace allowlist. The path policy and
            # catastrophic-command guard remain, while model arguments may name an external path.
            boundary.pop("allowed_write_roots", None)
        return

    owner_root = _resolved_path(owner_scope)
    if owner_root is None:
        boundary["allowed_write_roots"] = []
        return

    scope = _text(getattr(params, "context_scope", "default")).lower() or "default"
    if scope != "control_plane" and boundary.get("read_scope_mode") != "exact":
        # 用户自己的 owner home 是 WorkspaceOnly 的完整产品工作区。任务目录、旧项目和
        # 用户直接放在 home 下的文件都可写；框架控制面由 forbidden_write_roots 另行保护。
        # 但宿主创建时下发的"墙外显式工作目录"是结构化授权，不能被 owner home 覆盖：
        # 覆盖会让子代理在用户声明的项目目录里拿 WRITE_FORBIDDEN（2026-09-11 真机复测：
        # 创建时 allowed_write_roots 含目标目录，运行时只剩 owner home，写门直接拒绝）。
        boundary["allowed_write_roots"] = list(
            dict.fromkeys([str(owner_root), *_granted_external_roots(boundary, owner_root)])
        )
        boundary["execution_workspace_roots"] = list(boundary["allowed_write_roots"])
        return

    task_root_text = _text(boundary.get("task_root"))
    if not task_root_text:
        boundary["allowed_write_roots"] = []
        return
    task_root = _resolved_path(task_root_text)
    if owner_root is None or task_root is None or not _is_strict_task_root(
        task_root,
        owner_root,
        agent,
    ):
        # 显式空白名单与“键缺失”不同：write_boundary 会 fail-closed；shell 则把 owner home
        # 只读挂载。这样畸形/伪造 task_root 不会悄悄退回“整个 owner home 可写”。
        boundary["allowed_write_roots"] = []
        return


    existing_roots = _string_list(boundary.get("allowed_write_roots"))
    if not existing_roots:
        boundary["allowed_write_roots"] = [str(task_root)]
        return

    product_roots = [
        candidate
        for raw in _string_list(boundary.get("product_write_roots"))
        if (candidate := _resolved_path(raw)) is not None
        and _is_relative_to(candidate, owner_root)
    ]
    capability_roots = [
        candidate
        for raw in _string_list(boundary.get("capability_write_roots"))
        if (candidate := _resolved_path(raw)) is not None
        and _is_relative_to(candidate, owner_root)
    ]
    # 已有的结构化授权可能是内部 task 子树，也可能是父代理明确分配的用户项目根。
    # 后者必须同时出现在 product_write_roots 或 capability_write_roots 且位于 owner
    # home 内；这样既不吞掉正式能力授权，也不会把一个畸形 allowed_write_roots 单独
    # 当成授权来源。
    scoped: list[str] = []
    for raw in existing_roots:
        candidate = _resolved_path(raw)
        if candidate is not None and (
            _is_relative_to(candidate, task_root)
            or any(_is_relative_to(candidate, root) for root in product_roots)
            or any(_is_relative_to(candidate, root) for root in capability_roots)
        ):
            text = str(candidate)
            if text not in scoped:
                scoped.append(text)
    boundary["allowed_write_roots"] = scoped


# LLM: Owner home contains both user files and host-authoritative policy/control files. The main
# WorkspaceOnly root may be writable, but these exact existing control paths stay read-only and
# are propagated to both filesystem tools and process sandboxes through one boundary field.
# 函数用途: 保护当前 owner 的权限、配额和运行账本，防止模型靠改框架元数据给自己提权。
def _attach_owner_control_write_guards(
    boundary: dict[str, object],
    agent: object,
) -> None:
    owner_scope = _text(boundary.get("effective_owner_scope_root"))
    if not owner_scope:
        return
    home = getattr(agent, "home_paths", None)
    protected: list[str] = []
    for name in (
        "owner_permissions_json",
        "owner_quota_json",
        "owner_retention_json",
        "owner_memory_policy_json",
        "owner_skill_policy_json",
        "owner_tool_policy_json",
        "owner_runs_dir",
        "owner_agents_dir",
        "owner_compact_dir",
        "owner_data_dir",
        "owner_logs_dir",
        "owner_capability_requests_dir",
        "owner_temporary_grants_dir",
        "owner_audit_log_jsonl",
    ):
        path = _resolved_path(getattr(home, name, None))
        if path is not None and str(path) not in protected:
            protected.append(str(path))
    if protected:
        existing = _string_list(boundary.get("forbidden_write_roots"))
        boundary["forbidden_write_roots"] = list(dict.fromkeys([*existing, *protected]))


# LLM: The registry's owner wall describes the main agent. Task-local descendants additionally
# use SubAgentManager.owner_scope_root so an administrator's Full Access never leaks downward.
# 函数用途: 返回当前工具调用真正生效的 owner home 边界，主代理可为空，子代理始终恢复自己的家目录墙。
def _effective_owner_scope(agent: object, params: object) -> str:
    scope = _text(getattr(params, "context_scope", "default")).lower() or "default"
    if scope in {"task_local", "control_plane"}:
        child_scope = _text(getattr(getattr(agent, "subagents", None), "owner_scope_root", ""))
        if child_scope:
            return child_scope
    return _text(getattr(getattr(agent, "tools", None), "owner_scope_root", ""))


# LLM: Full Access is a host configuration of the local/main identity, not the absence of a path
# by itself and never a claim parsed from chat text.
# 函数用途: 判断本轮主代理是否拥有真正的本机全盘权限，供文件与命令写边界保持一致。
def _is_local_full_access(agent: object) -> bool:
    home = getattr(agent, "home_paths", None)
    provider = _text(getattr(home, "owner_provider", "")).lower()
    owner_kind = _text(getattr(home, "owner_kind", "")).lower()
    access = (
        _text(getattr(getattr(agent, "config", None), "access_mode", "workspace-write"))
        .lower()
        .replace("_", "-")
    )
    return provider in {"", "local"} and owner_kind in {"", "main"} and access == "full-access"


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


# LLM: 缺失路径必须保持“没有事实”，不能经 str(None) 变成 cwd/None；所有
# workspace/owner/task 边界调用方都依赖这个 fail-closed 归一化。
# 函数用途: 把非空结构化路径解析为绝对路径，空值直接返回没有路径。
def _resolved_path(raw: object) -> Path | None:
    value = _text(raw)
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


# LLM: A child task root is valid only below one of the host-owned task namespaces: the public
# owner tasks tree or this manager's canonical subagent workspace. An arbitrary owner subfolder
# cannot become task authority merely because it is inside the owner wall.
# 函数用途: 核对子代理内部任务目录是否来自框架的标准任务命名空间。
def _is_strict_task_root(
    task_root: Path,
    owner_root: Path,
    agent: object,
) -> bool:
    task_namespaces = [(owner_root / "tasks").resolve(strict=False)]
    manager_workspace = _resolved_path(
        getattr(getattr(agent, "subagents", None), "workspace", None)
    )
    if manager_workspace is not None and _is_relative_to(manager_workspace, owner_root):
        task_namespaces.append((manager_workspace / "tasks").resolve(strict=False))
    return any(
        task_root != namespace and _is_relative_to(task_root, namespace)
        for namespace in task_namespaces
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
