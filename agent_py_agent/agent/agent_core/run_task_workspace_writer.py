
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from ..conversation.authority import (
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
)
from ..user_space.home_indexes import RunIndexRef, TaskIndexRef, register_run_ref, register_task_ref
from ..user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    FinishRunWorkspaceRequest,
    activate_run_workspace,
    ensure_run_workspace,
    finish_run_workspace,
)
from ..user_space.task_title import concise_task_title, looks_like_machine_id
from ._runtime_params import ArchiveRunParams
from .runtime.task_identity import durable_task_id


def current_run_task_workspace_root(agent, params: object | None = None) -> Path | None:
    for text in _task_workspace_root_candidates(agent, params):
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            continue
    return None


def current_run_task_work_dir(agent, params: object | None = None) -> Path | None:
    for text in _task_workspace_work_dir_candidates(agent, params):
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            continue
    root = current_run_task_workspace_root(agent, params)
    return root / "work" if root is not None else None


# LLM: Archive a conversation turn under its task only after a task-promoting tool activated that
# task in this turn; a 会话运行时 sticky cwd alone must not turn plain chat into task execution.
# 函数用途: 需要时保存本轮任务目录和索引；纯聊天即使继承工作目录也不会写成任务运行。
def write_run_task_workspace_if_needed(agent, params: ArchiveRunParams) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return ""
    attrs = getattr(params, "task_attributes", None)
    if (
        isinstance(attrs, dict)
        and str(getattr(params, "source", "") or "").strip().lower() == "gateway"
        and str(attrs.get("conversation_thread_id") or "").strip()
        and attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is not True
    ):
        return ""
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is None and _unpromoted_conversation_turn(params):
        return ""
    if existing is not None:
        existing.root.mkdir(parents=True, exist_ok=True)
        existing.output_dir.mkdir(parents=True, exist_ok=True)
        existing.work_dir.mkdir(parents=True, exist_ok=True)
        register_saved_run_task_ref(agent, _SavedWorkspaceRef(existing.root, existing.work_dir), _root_task_params(params))
        return str(existing.root)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    result = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=_preferred_task_name(agent, params, params.user_prompt),
            user_prompt=params.user_prompt,
            request_id=params.run_request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
            source=params.source,
        )
    )
    register_saved_run_task_ref(agent, result, params)
    return str(result.root)


# LLM: 顶层 run 返回时只用 AgentRunResult 的 typed runtime_status/reason 结束工作区；
# conversation task 继续以 conversation store 为唯一生命周期权威，子代理走自己的 state。
# 函数用途: 把 standalone CLI/本地 run 的当前工作区从 RUNNING 投影到真实终态并刷新索引。
def finish_run_task_workspace_if_needed(agent, params: object, result: object) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {
        "task_local",
        "control_plane",
    }:
        return ""
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    if any(
        str(attrs.get(key) or "").strip()
        for key in ("conversation_thread_id", "conversation_task_id")
    ):
        return ""
    existing = _existing_workspace_paths(attrs)
    if existing is None or not _workspace_is_owner_scoped(agent, existing.root):
        return ""
    finish_request = _run_workspace_finish_request(params, result, existing.root)
    if finish_request is None:
        return ""
    try:
        finished = finish_run_workspace(finish_request)
        if finished is None:
            return ""
        _register_finished_run_task_ref(agent, params, finished, finish_request)
        return str(finished.root)
    except (OSError, RuntimeError, ValueError):
        logging.getLogger(__name__).warning(
            "run workspace terminal projection failed(task=%s, run=%s)",
            finish_request.task_id,
            finish_request.run_id,
            exc_info=True,
        )
        return ""


# LLM: 一个 standalone 终态只能构造一份 typed request，写 canonical state 和刷新索引都复用它。
# 函数用途: 从本次运行参数和真实结果整理出工作区收尾请求；仍在等待时返回 None。
def _run_workspace_finish_request(
    params: object,
    result: object,
    root: Path,
) -> FinishRunWorkspaceRequest | None:
    status = _terminal_workspace_status(params, result)
    if not status:
        return None
    workspace_task_id = str(durable_task_id(params) or "").strip()
    task_id = str(
        workspace_task_id
        or getattr(params, "run_id", "")
        or getattr(params, "request_id", "")
    ).strip()
    return FinishRunWorkspaceRequest(
        root=root,
        request_id=str(getattr(params, "request_id", "") or "").strip(),
        run_id=str(
            getattr(params, "run_id", "")
            or getattr(params, "request_id", "")
            or task_id
        ).strip(),
        task_id=workspace_task_id,
        status=status,
        verification_status="UNVERIFIED",
        runtime_status=str(getattr(result, "runtime_status", "") or ""),
        runtime_reason=str(getattr(result, "runtime_reason", "") or ""),
        runtime_source=str(getattr(result, "runtime_source", "") or ""),
    )


# LLM: 仅将 typed runtime result 映射为 workspace 投影；wait/background 和显式 goal 非成功结果保持活跃。
# 函数用途: 决定 standalone 顶层返回后应该记录 DONE、CANCELLED、BLOCKED 还是 FAILED。
def _terminal_workspace_status(params: object, result: object) -> str:
    runtime_status = str(getattr(result, "runtime_status", "ok") or "ok").strip().lower()
    runtime_reason = str(getattr(result, "runtime_reason", "") or "").strip().lower()
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    if runtime_reason in {"background_dispatch", "wait"}:
        return ""
    if str(attrs.get("thread_goal_id") or "").strip() and runtime_status != "ok":
        return ""
    if runtime_status == "ok":
        return "DONE"
    if runtime_status == "cancelled":
        return "CANCELLED"
    if runtime_status in {"blocked", "context_overflow", "unfinished"}:
        return "BLOCKED"
    return "FAILED"


# LLM: 终态写入只允许 owner tasks 真实子目录，路径链上出现 symlink 时必须 fail-closed。
# 函数用途: 确认待收尾目录属于当前用户，防止误改共享目录或其他用户目录。
def _workspace_is_owner_scoped(agent: object, root: Path) -> bool:
    home_paths = getattr(agent, "home_paths", None)
    owner_tasks = getattr(home_paths, "owner_tasks_dir", None)
    if owner_tasks is None:
        return False
    try:
        unresolved_root = Path(root).expanduser()
        resolved_root = unresolved_root.resolve(strict=False)
        resolved_root.relative_to(Path(owner_tasks).expanduser().resolve(strict=False))
        state_path = resolved_root / "work" / "state.json"
        return not any(
            path.is_symlink()
            for path in (unresolved_root, unresolved_root / "work", state_path)
        )
    except (OSError, RuntimeError, ValueError):
        return False


# LLM: task/run index 只是 canonical state 的检索投影；必须使用已经写成功的终态和工作区路径。
# 函数用途: 工作区收尾成功后刷新当前用户的任务索引和运行索引。
def _register_finished_run_task_ref(
    agent: object,
    params: object,
    result: object,
    request: FinishRunWorkspaceRequest,
) -> None:
    home_paths = getattr(agent, "home_paths", None)
    task_id = str(request.task_id or request.run_id or request.request_id or "").strip()
    run_id = str(request.run_id or request.request_id or task_id).strip()
    if home_paths is None or not task_id or not run_id:
        return
    index_status = str(request.status or "").lower()
    register_task_ref(
        home_paths,
        TaskIndexRef(
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            task_id=task_id,
            task_path=result.root,
            status=index_status,
            title=_authoritative_task_title(agent, params, task_id),
        ),
    )
    register_run_ref(
        home_paths,
        RunIndexRef(
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            run_id=run_id,
            task_id=task_id,
            run_path=result.work_dir,
            status=index_status,
        ),
    )


def register_saved_run_task_ref(agent, result, params) -> None:
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return
    task_id = params.task_id or params.run_id or params.run_request_id
    if not task_id:
        return
    try:
        register_task_ref(
            home_paths,
            TaskIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                task_id=task_id,
                task_path=result.root,
                status="active",
                title=_authoritative_task_title(agent, params, task_id),
            ),
        )
        register_run_ref(
            home_paths,
            RunIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                run_id=str(params.run_id or params.run_request_id or task_id),
                task_id=str(task_id),
                run_path=result.work_dir,
                status="active",
            ),
        )
        _sync_conversation_task_workspace(agent, params, task_id, result.root)
    except OSError:
        return


def _sync_conversation_task_workspace(agent, run_params, task_id: str, task_root: Path) -> None:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "bind_task", None)):
        return
    attrs = getattr(run_params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    # Ingress has already bound transient named-work identity, path and lifecycle.
    # Rebinding it from a generic run archive would reinterpret this turn's user
    # prompt as durable configuration.  An Audit prepare draft remains pending
    # until publish_audit_update commits it.
    if attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True:
        return
    # 同一 thread 只有一份历史；只有结构化任务晋升已经给出 task_id 时才建立
    # thread↔task/workspace 运行关系。
    if str(attrs.get("conversation_task_id") or "").strip() != str(task_id):
        return
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id:
        try:
            thread = store.thread_for_task(str(task_id))
        except Exception:
            # 绑定线索失败只降级(任务工作区不依赖会话线索),但要留观测——
            # 静默丢元数据会让跨通道接续悄悄断链(体检实锤)。
            logging.getLogger(__name__).debug("thread_for_task lookup failed", exc_info=True)
            thread = None
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        return
    existing = _conversation_task_link(store, thread_id, str(task_id))
    goal = str(getattr(existing, "goal", "") or getattr(run_params, "user_prompt", "") or task_id)
    existing_path = str(getattr(existing, "task_path", "") or "").strip()
    task_path = existing_path if existing_path and Path(existing_path).exists() else str(task_root)
    status = str(getattr(existing, "status", "") or "active")
    if (
        existing is not None
        and goal == str(getattr(existing, "goal", "") or "")
        and task_path == existing_path
        and status == str(getattr(existing, "status", "") or "")
    ):
        return
    try:
        store.bind_task(
            {
                "thread_id": thread_id,
                "task_id": str(task_id),
                "goal": goal,
                "status": status,
                "task_path": task_path,
                "now": float(getattr(existing, "created_at", 0.0) or 0.0) or None,
            }
        )
    except OSError:
        return


def _conversation_task_link(store: object, thread_id: str, task_id: str):
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next(
        (item for item in links if str(getattr(item, "task_id", "") or "") == task_id),
        None,
    )


# LLM: 标题优先显式结构化值和 goal，普通运行回退 root prompt，不能用 compact 续接提示覆盖原任务名。
# 函数用途: 为任务索引选择稳定、可读的标题。
def _authoritative_task_title(agent: object, params: object, task_id: str) -> str:
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    for key in ("task_title", "task_name"):
        value = str(attrs.get(key) or "").strip()
        if value:
            return value[:160]
    store = getattr(agent, "conversation_store", None)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if store is not None and thread_id:
        link = _conversation_task_link(store, thread_id, task_id)
        goal = str(getattr(link, "goal", "") or "").strip()
        if goal:
            return goal[:160]
    return str(
        getattr(params, "root_user_prompt", "")
        or getattr(params, "user_prompt", "")
        or task_id
    )[:160]


def attach_run_task_workspace_context(agent, params, user_prompt: str):
    if not _should_create_workspace(agent, params):
        return params
    result = _ensure_workspace_for_run(agent, params, user_prompt)
    primary_workspace_root = _primary_workspace_root(agent)
    injection = _workspace_prompt_section(
        result,
        primary_workspace_root=primary_workspace_root,
        # R7c 实锤(单次 run"请示退出"形态):cli_run 是一次性非交互运行,中途请示
        # 无人应答——这个环境事实必须告知模型;gateway 有 guidance 补发渠道、chat
        # 可多轮,不注入。
        single_shot=str(getattr(params, "source", "") or "").strip() == "cli_run",
    )
    next_inject = _append_once(list(getattr(params, "inject", None) or []), injection)
    next_attrs = _task_attributes_with_workspace(getattr(params, "task_attributes", None), result)
    next_contract = _delivery_contract_with_workspace(
        getattr(params, "delivery_contract", None),
        result,
        primary_workspace_root=primary_workspace_root,
    )
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs, delivery_contract=next_contract)


def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    if _unpromoted_conversation_turn(params):
        # 普通聊天是 RequestRun，不因“可能以后会做事”预建 RUNNING task。真正调用
        # promotes_task 工具/create_subagents/task_progress 时由任务晋升点懒建。
        agent._current_run_task_workspace = ""
        return False
    return bool(getattr(agent, "home_paths", None) is not None)


def _unpromoted_conversation_turn(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    return bool(
        isinstance(attrs, dict)
        and str(attrs.get("conversation_thread_id") or "").strip()
        and not str(attrs.get("conversation_task_id") or "").strip()
    )


def materialize_promoted_task_workspace(agent: object, params: object, goal: str = "") -> Path | None:
    """TaskRun 晋升后的唯一懒建入口；同步 attrs、delivery contract 与当前工具边界。"""
    if getattr(agent, "home_paths", None) is None:
        return None
    result = _ensure_workspace_for_run(agent, params, goal or str(getattr(params, "root_user_prompt", "") or ""))
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    attrs.update(_task_attributes_with_workspace(attrs, result))
    contract = _delivery_contract_with_workspace(
        getattr(params, "delivery_contract", None),
        result,
        primary_workspace_root=_primary_workspace_root(agent),
    )
    if contract is not None:
        params.delivery_contract = contract
    agent._current_run_task_workspace = str(result.root)
    return result.root


def _ensure_workspace_for_run(agent, params, user_prompt: str):
    request = _run_workspace_request(agent, params, user_prompt)
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is not None:
        return activate_run_workspace(existing.root, request)
    return ensure_run_workspace(request)


def _run_workspace_request(agent, params, user_prompt: str) -> EnsureRunWorkspaceRequest:
    home_paths = agent.home_paths
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    return EnsureRunWorkspaceRequest(
        home=target_home,
        template=str(getattr(agent.config, "workspace_task_path_template", "")),
        task_name=_preferred_task_name(agent, params, user_prompt),
        user_prompt=user_prompt,
        request_id=str(getattr(params, "request_id", "") or ""),
        run_id=str(getattr(params, "run_id", "") or ""),
        task_id=durable_task_id(params),
        owner_id=str(getattr(home_paths, "owner_id", "") or ""),
        owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
        source=str(getattr(params, "source", "") or "run"),
    )


# LLM: 通道运行时 风格的一次性 LLM slug 是显式开关能力，不得成为普通 IM 首响的隐藏必经调用。
#   结构化 task_title 最优先；LLM 失败、空答或格式坏时必须回退确定性中文标题。
# 人类: 可读目录名有稳定主链，也允许管理员按需开启模型取名。
def _preferred_task_name(agent, params: object, user_prompt: str) -> str:
    """选择结构化标题、可选模型短标题或确定性用户请求标题。"""
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    for key in ("task_title", "task_name"):
        value = str(attrs.get(key) or "").strip()
        if value and not looks_like_machine_id(value):
            return concise_task_title(value)
    legacy_task_id = str(getattr(params, "task_id", "") or "").strip()
    if legacy_task_id and not looks_like_machine_id(legacy_task_id):
        return concise_task_title(legacy_task_id)
    if bool(getattr(agent.config, "workspace_task_llm_title_enabled", False)):
        generated = _llm_task_title(agent, user_prompt)
        if generated:
            return generated
    return concise_task_title(user_prompt)


def _llm_task_title(agent, user_prompt: str) -> str:
    prompt_limit = max(200, int(getattr(agent.config, "workspace_task_llm_title_input_chars", 2000) or 2000))
    source = str(user_prompt or "").strip()[:prompt_limit]
    if not source:
        return ""
    prompt = (
        "为下面任务生成一个可读的短目录标题。只返回 JSON 对象，格式为 "
        '{"title":"..."}。标题用原任务语言，1到5个词，不含日期、编号、解释。\n\n任务：\n'
        + source
    )
    try:
        response = agent.backend.generate_json(prompt, max_tokens=64)
        payload = json.loads(str(getattr(response, "text", "") or ""))
    # 这是非关键的目录标题增强：第三方 backend 也可能抛出自定义异常，任何失败都必须
    # 回到确定性标题，绝不能让取名旁路中断用户任务。
    except Exception:  # noqa: BLE001
        return ""
    title = str(payload.get("title") or "").strip() if isinstance(payload, dict) else ""
    if not title or looks_like_machine_id(title):
        return ""
    return concise_task_title(title)


@dataclass(frozen=True)
class _ExistingWorkspacePaths:
    root: Path
    output_dir: Path
    work_dir: Path


@dataclass(frozen=True)
class _SavedWorkspaceRef:
    root: Path
    work_dir: Path


def _root_task_params(params: ArchiveRunParams) -> ArchiveRunParams:
    root_task_id = _conversation_task_id(getattr(params, "task_attributes", None))
    if not root_task_id:
        return params
    return replace(params, task_id=root_task_id)


def _conversation_task_id(attrs: object) -> str:
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def _existing_workspace_paths(attrs: object) -> _ExistingWorkspacePaths | None:
    if not isinstance(attrs, dict):
        return None
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    root_text = str(workspace.get("task_root") or "").strip()
    if not root_text:
        return None
    root = Path(root_text)
    output_dir = Path(str(workspace.get("output_dir") or root / "output"))
    work_dir = Path(str(workspace.get("work_dir") or root / "work"))
    return _ExistingWorkspacePaths(root=root, output_dir=output_dir, work_dir=work_dir)


def _task_workspace_root_candidates(agent, params: object | None) -> list[str]:
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    subagent_root = _subagent_run_workspace(agent, params)
    internal_root = _internal_agent_run_workspace(params, attrs)
    return [
        subagent_root,
        internal_root,
        _workspace_root_from_mapping(contract, "task_workspace"),
        _workspace_root_from_mapping(attrs, "run_workspace"),
        str(getattr(agent, "_current_run_task_workspace", "") or "").strip(),
    ]


def _task_workspace_work_dir_candidates(agent, params: object | None) -> list[str]:
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    subagent_root = _subagent_run_workspace(agent, params)
    internal_root = _internal_agent_run_workspace(params, attrs)
    return [
        subagent_root,
        internal_root,
        _workspace_work_dir_from_mapping(contract, "task_workspace"),
        _workspace_work_dir_from_mapping(attrs, "run_workspace"),
        str(Path(getattr(agent, "_current_run_task_workspace", "") or "") / "work")
        if str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
        else "",
    ]


def _internal_agent_run_workspace(params: object | None, attrs: object) -> str:
    if str(getattr(params, "context_scope", "") or "").strip().lower() not in {"task_local", "control_plane"}:
        return ""
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("agent_run_workspace_dir") or "").strip()


def _subagent_run_workspace(agent, params: object | None) -> str:
    run_id = str(getattr(params, "run_id", "") or "").strip()
    if not run_id:
        return ""
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return ""
    try:
        task = manager.load(run_id)
    except Exception:
        return ""
    return str(getattr(task, "agent_run_workspace_dir", "") or "").strip()


def _workspace_root_from_mapping(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    workspace = value.get(key)
    if not isinstance(workspace, dict):
        return ""
    root = str(workspace.get("task_root") or "").strip()
    if root:
        return root
    for field in ("output_dir", "work_dir"):
        text = str(workspace.get(field) or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except OSError:
            continue
        if field == "output_dir":
            return str(path.parent)
        if field == "work_dir":
            return str(path.parent)
    return ""


def _workspace_work_dir_from_mapping(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    workspace = value.get(key)
    if not isinstance(workspace, dict):
        return ""
    work_dir = str(workspace.get("work_dir") or "").strip()
    if work_dir:
        return work_dir
    root = str(workspace.get("task_root") or "").strip()
    return str(Path(root) / "work") if root else ""


# LLM: run 任务工作区的软运行状态提示(纯自然语言软约束,不做机器决策)。
#   single_shot=True(cli_run)时附加"单次运行无人应答"环境事实——R7c 实锤:
#   模型把单次 run 当可交互会话,零产物即"请指示如何继续"退出;这不是模型的错,
#   是运行时没把交互形态告诉它。gateway(有 guidance 渠道)/chat(多轮)不附加。
# 函数用途: 告诉模型本轮的工作区位置、产物去向,以及"这次没人会中途回复你"。
# LLM: 任务工作区注入段(稳而不管减负,2026-06-12,PLAN-stability-not-control):
#   从 18 行教学文案瘦身为"事实 + 两句定位"——对照组实证(长期助手 三层提示)
#   表明每轮注入的指挥性内容会挤占模型的任务注意力(R9 取证:单篇产出缩水
#   4 倍)。被砍的目录使用知识(输入目录≠交付目录、旧报告只当线索、收口整理等)
#   收编进 lessons/workspace.md,按需召回而非每轮灌输。改动时同步检查
#   home_memory_seeds 的 workspace lesson 与 tests/test_run_task_workspace_writer。
# 函数用途: 告诉模型本轮任务的目录事实(在哪读、往哪交),一眼看完不啰嗦。
def _workspace_prompt_section(paths, *, primary_workspace_root: Path, single_shot: bool = False) -> str:
    single_shot_line = (
        ["- 单次运行：提问或请示不会有任何回复；自行决策推进到底，结束前把交付物（或不可行说明）写进 output_dir。"]
        if single_shot
        else []
    )
    return "\n".join(
        [
            "# Current Task Workspace",
            f"- relative_input_root: {primary_workspace_root}（用户给的相对路径从这里读）",
            f"- task_root: {paths.root}",
            f"- output_dir: {paths.output_dir}（最终交付物写这里；用户让你读的输入目录不是交付目录）",
            f"- work_dir: {paths.work_dir}（草稿与过程文件）",
            *single_shot_line,
            *_relay_legacy_lines(paths),
        ]
    )


# LLM: 接力遗产清单(R11a 接力倒退实锤:上轮 5 个子代理产出 345KB 深度内容
#   躺在 work/agents/*/runner_response.md 与 draft 文件里,接力轮模型看不到这笔
#   资产,重新派工又没干完——交付区反而被新一层浅文件覆盖)。任务目录复用
#   (timeline 多于一行=接力轮)时,把上轮遗留的子代理产出档案按"份数+大小+
#   路径"投影成结构化事实。纯事实零指令:用不用、怎么用由模型决定。
# 函数用途: 接力时告诉模型"上一轮帮手已经写了多少东西、在哪",别当无事发生。
def _relay_legacy_lines(paths) -> list[str]:
    try:
        timeline = paths.work_dir / "timeline.jsonl"
        if not timeline.is_file() or len(timeline.read_text(encoding="utf-8").splitlines()) < 2:
            return []
        responses = sorted((paths.work_dir / "agents").glob("*/runner_response.md"))
        sized = [(p, p.stat().st_size) for p in responses if p.is_file()]
        total_kb = sum(size for _, size in sized) // 1024
        if not sized or total_kb <= 0:
            return []
    except OSError:
        return []
    listing = "; ".join(f"{p.parent.name}:{size // 1024}KB" for p, size in sized[:8])
    return [
        f"- relay_legacy_outputs: 上一轮遗留 {len(sized)} 份子代理产出档案共 {total_kb}KB"
        f"（work/agents/<run_id>/runner_response.md，明细: {listing}）",
    ]


def _append_once(items: list[str], injection: str) -> list[str]:
    marker = "# Current Task Workspace"
    return items if any(marker in str(item) for item in items) else [*items, injection]


def _task_attributes_with_workspace(attrs: object, paths) -> dict:
    result = dict(attrs) if isinstance(attrs, dict) else {}
    result["run_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    return result


def _primary_workspace_root(agent) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve(strict=False)


def _delivery_contract_with_workspace(contract: object, paths, *, primary_workspace_root: Path | None = None):
    if not isinstance(contract, dict):
        return contract
    result = dict(contract)
    task_workspace = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    if primary_workspace_root is not None:
        task_workspace["source_workspace_root"] = str(primary_workspace_root)
        task_workspace["relative_input_root"] = str(primary_workspace_root)
    artifacts = result.get("artifacts")
    user_requested_output_dir = _user_requested_output_dir(artifacts, paths, primary_workspace_root)
    if user_requested_output_dir:
        task_workspace["user_requested_output_dir"] = user_requested_output_dir
    result["task_workspace"] = task_workspace
    if isinstance(artifacts, list):
        result["artifacts"] = [
            _artifact_with_default_output_root(
                item,
                paths,
                primary_workspace_root=primary_workspace_root,
            )
            for item in artifacts
        ]
    return result


def _user_requested_output_dir(artifacts: object, paths, primary_workspace_root: Path | None) -> str:
    if not isinstance(artifacts, list):
        return ""
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        if directory := _user_requested_dir_from_artifact_path(item, paths, primary_workspace_root):
            return directory
        if directory := _user_requested_dir_from_roots(item, paths, primary_workspace_root):
            return directory
    return ""


def _user_requested_dir_from_artifact_path(artifact: dict, paths, primary_workspace_root: Path | None = None) -> str:
    text = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not text or not _is_absolute_or_home_path(text):
        return _relative_user_requested_dir(text, primary_workspace_root)
    try:
        path = Path(text).expanduser()
    except OSError:
        return ""
    if _same_or_inside(path, Path(paths.output_dir)):
        return ""
    return _output_dir_for_path_text(text)


def _user_requested_dir_from_roots(artifact: dict, paths, primary_workspace_root: Path | None = None) -> str:
    roots = artifact.get("allowed_output_roots")
    if not isinstance(roots, list):
        return ""
    for value in roots:
        directory = _user_requested_dir_from_root_text(str(value or "").strip(), paths, primary_workspace_root)
        if directory:
            return directory
    return ""


def _user_requested_dir_from_root_text(text: str, paths, primary_workspace_root: Path | None) -> str:
    if not text:
        return ""
    if not _is_absolute_or_home_path(text):
        return _relative_user_requested_dir(text, primary_workspace_root)
    try:
        path = Path(text).expanduser()
    except OSError:
        return ""
    return "" if _same_or_inside(path, Path(paths.output_dir)) else _output_dir_for_path_text(text)


def _relative_user_requested_dir(text: str, primary_workspace_root: Path | None) -> str:
    relative = _safe_non_output_relative_path(text)
    if relative is None or primary_workspace_root is None:
        return ""
    target = relative.parent if relative.suffix else relative
    return str((primary_workspace_root / target).resolve(strict=False))


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _artifact_with_default_output_root(item: object, paths, *, primary_workspace_root: Path | None = None) -> object:
    if not isinstance(item, dict):
        return item
    artifact = dict(item)
    artifact = _artifact_with_task_output_paths(
        artifact,
        paths,
        primary_workspace_root=primary_workspace_root,
    )
    if _artifact_declares_output_target(artifact):
        return artifact
    artifact["allowed_output_roots"] = [str(paths.output_dir)]
    return artifact


def _artifact_with_task_output_paths(
    artifact: dict,
    paths,
    *,
    primary_workspace_root: Path | None = None,
) -> dict:
    for key in ("preferred_path", "path"):
        rewritten = _rewrite_user_requested_relative_path(artifact.get(key), primary_workspace_root)
        if not rewritten:
            rewritten = _rewrite_task_output_path(artifact.get(key), paths)
        if rewritten:
            artifact[key] = rewritten
    roots = artifact.get("allowed_output_roots")
    if isinstance(roots, list):
        artifact["allowed_output_roots"] = [
            _rewrite_user_requested_relative_root(value, primary_workspace_root)
            or _rewrite_task_output_root(value, paths)
            or value
            for value in roots
        ]
    if not _has_allowed_output_roots(artifact):
        explicit_root = _explicit_output_root_from_artifact_path(artifact, paths, primary_workspace_root)
        if explicit_root:
            artifact["allowed_output_roots"] = [explicit_root]
    return artifact


def _rewrite_user_requested_relative_path(value: object, primary_workspace_root: Path | None) -> str:
    relative = _safe_non_output_relative_path(str(value or "").strip())
    if relative is None or primary_workspace_root is None:
        return ""
    return str((primary_workspace_root / relative).resolve(strict=False))


def _rewrite_user_requested_relative_root(value: object, primary_workspace_root: Path | None) -> str:
    relative = _safe_non_output_relative_path(str(value or "").strip())
    if relative is None or primary_workspace_root is None:
        return ""
    return str((primary_workspace_root / relative).resolve(strict=False))


def _rewrite_task_output_path(value: object, paths) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _is_absolute_or_home_path(text):
        return text
    suffix = _relative_output_suffix(text)
    if suffix is None:
        return text
    return str((paths.output_dir / suffix).resolve(strict=False)) if suffix else str(paths.output_dir)


def _rewrite_task_output_root(value: object, paths) -> str:
    text = str(value or "").strip()
    if not text or _is_absolute_or_home_path(text):
        return text
    suffix = _relative_output_suffix(text)
    if suffix is None:
        return text
    return str((paths.output_dir / suffix).resolve(strict=False)) if suffix else str(paths.output_dir)


def _relative_output_suffix(text: str) -> Path | None:
    normalized = text.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == "output":
        return Path()
    if normalized.startswith("output/"):
        return Path(normalized[len("output/") :])
    return None


def _safe_non_output_relative_path(text: str) -> Path | None:
    if not text or _is_absolute_or_home_path(text):
        return None
    if _relative_output_suffix(text) is not None:
        return None
    normalized = text.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized == "." or normalized.startswith("../") or normalized == "..":
        return None
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _is_absolute_or_home_path(text: str) -> bool:
    return text.startswith("/") or text.startswith("~") or _is_windows_absolute_path(text)


def _has_allowed_output_roots(artifact: dict) -> bool:
    roots = artifact.get("allowed_output_roots")
    return isinstance(roots, list) and any(str(item or "").strip() for item in roots)


def _explicit_output_root_from_artifact_path(
    artifact: dict,
    paths,
    primary_workspace_root: Path | None = None,
) -> str:
    text = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not text:
        return ""
    if directory := _relative_user_requested_dir(text, primary_workspace_root):
        return directory
    if not _is_absolute_or_home_path(text):
        return ""
    try:
        path = Path(text).expanduser()
    except OSError:
        return ""
    if _same_or_inside(path, Path(paths.output_dir)):
        return ""
    return _output_dir_for_path_text(text)


def _output_dir_for_path_text(text: str) -> str:
    pure = _pure_path(text)
    target = pure.parent if pure.suffix else pure
    result = str(target)
    return result if result != "." else "."


def _pure_path(text: str):
    return PureWindowsPath(text) if _is_windows_path(text) else Path(text).expanduser()


def _is_windows_path(text: str) -> bool:
    return _is_windows_absolute_path(text) or "\\" in text


def _is_windows_absolute_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text))


def _artifact_declares_output_target(artifact: dict) -> bool:
    if str(artifact.get("preferred_path") or artifact.get("path") or "").strip():
        return True
    for key in ("allowed_output_roots", "search_roots", "artifact_roots"):
        value = artifact.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


__all__ = [
    "attach_run_task_workspace_context",
    "current_run_task_work_dir",
    "current_run_task_workspace_root",
    "materialize_promoted_task_workspace",
    "write_run_task_workspace_if_needed",
]
