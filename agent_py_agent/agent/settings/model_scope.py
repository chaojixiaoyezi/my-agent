# LLM: 原工作片与宿主显式候选共用依赖冻结；只绑定 ContextVar，不热改 Agent/存储，权限仍由 owner_access 唯一裁决。
# 模块用途: 绑定模型、提示和工具权限视图；存储、会话、MCP 连接仍使用原权威，离开时恢复。

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import asdict, dataclass, field, replace

from .model_profiles import selected_model_config

_BINDINGS: ContextVar[dict | None] = ContextVar("model_profile_bindings", default=None)
_CACHE_LOCK = threading.Lock()
_LIFETIMES: ContextVar[tuple] = ContextVar("model_dependency_lifetimes", default=())


# LLM: 这是进程内显示投影，不存密钥或路由权威；真实请求仍只消费 ContextVar 中的不可变工作片。
# 函数用途: /status 在用户提前切模型时仍显示进行中工作片实际使用的模型。
def active_thread_model_name(agent: object, thread_id: str) -> str:
    with _CACHE_LOCK:
        rows = getattr(agent, "_model_profile_live_names", {}).get(thread_id, {})
        return next(reversed(rows.values()), "") if rows else ""


# LLM: 未进入作用域时保持原 instance 属性语义；作用域只覆盖工作片依赖，不替换存储和执行身份。
# 类用途: 让同一用户不同会话切模型、权限后安全并行，不互相覆盖配置、后端、提示或工具视图。
class ModelScopedAttribute:
    # LLM: 属性名由宿主类声明，不接收用户内容。
    # 函数用途: 绑定一个可被执行作用域临时覆盖的依赖。
    def __init__(self, name: str) -> None:
        self.name = name

    # LLM: 对象身份必须精确匹配，避免 id 重用；类级访问仍返回 descriptor。
    # 函数用途: 读取当前执行快照，没有快照时读取初始化依赖。
    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        row = (_BINDINGS.get() or {}).get(id(instance))
        if row is not None and row[0] is instance and self.name in row[1]:
            return row[1][self.name]
        try:
            return instance.__dict__[self.name]
        except KeyError as exc:
            raise AttributeError(self.name) from exc

    # LLM: 正常初始化/测试注入沿用底层默认依赖；菜单绝不通过 setattr 热改它们。
    # 函数用途: 保存 Agent 的部署默认依赖。
    def __set__(self, instance, value) -> None:
        instance.__dict__[self.name] = value


# LLM: 原部署配置是新档案未声明字段的唯一默认值；不能把当前作用域模型的覆盖当部署默认，读取不修改绑定。
# 函数用途: 在模型切换或候选准备时读取 Agent 初始化的配置，防止旧模型参数串到新模型。
def deployment_model_config(agent: object):
    if isinstance(getattr(type(agent), "config", None), ModelScopedAttribute):
        return agent.__dict__["config"]
    return agent.config


# LLM: 缓存键含 OAuth 引用代次而非 token 与思考控制方式；刷新不换模型，退出/重登录不能复用原身份。
# 函数用途: 切换模型、采样或思考控制配置时创建对应后端，相同快照复用连接与探针缓存。
def _profile_backend(agent: object, config: object):
    from ..backends import get_backend

    values = [getattr(config, key) for key in (
        "model_backend", "model_name", "api_base", "api_key", "model_context_window_tokens", "max_tokens",
        "model_custom_headers", "model_session_header", "model_auth_ref",
        "temperature", "top_p", "stream_enabled", "anthropic_prompt_cache_enabled", "request_timeout",
        "model_reasoning_control", "model_reasoning_levels", "model_structured_output", "model_reasoning_update_items",
    )]
    key = hashlib.sha256(json.dumps(values).encode()).hexdigest()
    with _CACHE_LOCK:
        cache = getattr(agent, "_model_profile_backends", None)
        if cache is None:
            cache = {}
            agent._model_profile_backends = cache
        if key not in cache:
            if len(cache) >= 16:
                cache.pop(next(iter(cache)))
            cache[key] = get_backend(config.model_backend, config)
        return cache[key]


# LLM: 载体属于一个精确 Agent，仅保存本进程依赖；不是持久配置、授权结果或模型选择权威，禁止序列化。
# 类用途: 把原 config/backend/prompts/tools 视图放在一起，让候选验证和实际执行绑定同一批对象。
@dataclass(frozen=True)
class ModelScopeDependencies:
    agent: object = field(repr=False)
    values: tuple[tuple[str, object], ...] = field(repr=False)
    selection: ModelSelectionSnapshot | None = None

    # LLM: 配置来自原 permission_config，不重新解析 profile 或读取变化中的 owner 设置。
    # 函数用途: 读取这批依赖已冻结的配置对象。
    @property
    def config(self):
        return dict(self.values)["config"]


# LLM: 这是请求依赖的只读来源快照，选择权威仍是 canonical thread；时间表示本地绑定，不证明 HTTP 送达。
# 类用途: 关联实际依赖与当时采用的模型选择版本，不保存连接地址或凭据。
@dataclass(frozen=True)
class ModelSelectionSnapshot:
    thread_id: str
    profile_id: str
    resolved_profile_id: str
    revision: int
    explicit_revision: int
    selected_at: float | None
    adopted_at: float
    selection_thread_id: str = ""
    selection_source: str = "thread"
    scheduler_run_id: str = ""


# LLM: 仅返回当前 ContextVar 的来源；无 descriptor/作用域时保持未知，不能读取 owner 默认冒充当前请求。
# 函数用途: 为安全点和模型调用账提供同一次依赖的选择版本。
def current_model_selection(agent: object) -> ModelSelectionSnapshot | None:
    row = (_BINDINGS.get() or {}).get(id(agent))
    return row[2] if row is not None and row[0] is agent else None


# LLM: thread 必须已由原 owner 校验；不重新选模或写状态，时间只供观察。
# 函数用途: 从已读取的会话选择建立请求采用快照。
def model_selection_snapshot(thread: object, config: object) -> ModelSelectionSnapshot:
    source = (getattr(config, "config_sources", None) or {}).get("model_name", {})
    return ModelSelectionSnapshot(
        thread.thread_id, thread.model_profile_id, str(source.get("profile_id") or "default"), thread.model_selection_revision,
        thread.model_selection_last_explicit_revision, thread.model_selection_selected_at, time.time(),
    )


# LLM: 原模型调用账可保存这个白名单；自动候选未带快照时不把旧 profile 的版本归到新模型。
# 函数用途: 给实际调用输出无正文、地址和密钥的模型选择来源。
def model_selection_metadata(agent: object) -> dict:
    selection = current_model_selection(agent)
    config = getattr(agent, "config", None)
    source = (getattr(config, "config_sources", None) or {}).get("model_name", {})
    if selection is None or selection.resolved_profile_id != str(source.get("profile_id") or "default"):
        return {}
    return asdict(selection)


# LLM: 使用原后端缓存和权限视图构造依赖；可以读宿主设置并创建连接对象，但不探针、不发网络、不准备工具或改变 Agent。
# 函数用途: 为当前模型或一个明确候选准备同源依赖；不支持 descriptor 的适配器保持未知。
def prepare_model_dependencies(agent: object, model_config: object, *, inherited: bool = False) -> ModelScopeDependencies | None:
    if not isinstance(getattr(type(agent), "config", None), ModelScopedAttribute):
        return None
    from ..user_space.approval_mode import permission_config

    config = permission_config(model_config, agent.home_paths, inherited=inherited)
    prompts = copy(agent.prompts)
    prompts.config = config
    values = {
        "config": config,
        "backend": agent.backend if model_config is agent.config else _profile_backend(agent, config),
        "prompts": prompts,
    }
    if isinstance(getattr(type(agent), "tools", None), ModelScopedAttribute):
        from ..user_space.owner_access import resolve_owner_scope_and_access

        owner_root, access = resolve_owner_scope_and_access(agent.home_paths, config, getattr(agent, "owner_policy", None))
        values["tools"] = agent.tools.with_access_policy(
            access_mode=access, path_access_mode="full" if not owner_root else config.path_access_mode,
            owner_scope_root=owner_root,
        )
    return ModelScopeDependencies(agent, tuple(values.items()))


# LLM: 显式宿主作用域允许候选暂时覆盖原工作片；必须是同一 Agent 的准备对象，ContextVar token 按嵌套次序恢复，不修改共享默认。
# 函数用途: 为验证或已提交执行绑定一批依赖；退出及异常均恢复原模型、提示和工具视图。
@contextmanager
def model_dependencies_scope(agent: object, dependencies: ModelScopeDependencies, *, thread_id: str = "", active: bool = False):
    if not isinstance(dependencies, ModelScopeDependencies) or dependencies.agent is not agent:
        raise ValueError("模型依赖不能绑定其他 Agent。")
    selection = dependencies.selection or current_model_selection(agent)
    token = _BINDINGS.set({**(_BINDINGS.get() or {}), id(agent): (agent, dict(dependencies.values), selection)})
    display_key = object()
    if thread_id and active:
        with _CACHE_LOCK:
            names = getattr(agent, "_model_profile_live_names", None)
            if names is None:
                names = agent._model_profile_live_names = {}
            names.setdefault(thread_id, {})[display_key] = str(dependencies.config.model_name)
            projections = getattr(agent, "_model_profile_live_selections", None)
            if projections is None:
                projections = agent._model_profile_live_selections = {}
            facts = model_selection_metadata(agent)
            source = (getattr(dependencies.config, "config_sources", None) or {}).get("model_name", {})
            projections.setdefault(thread_id, {})[display_key] = {
                "model_name": dependencies.config.model_name,
                "backend": dependencies.config.model_backend,
                "profile_id": facts.get("profile_id", str(source.get("profile_id") or "default")),
                "selection_revision": facts.get("revision"),
                "explicit_revision": selection.explicit_revision if selection else None,
                "adopted_at": facts.get("adopted_at"),
                "selection_source": facts.get("selection_source"),
                "selection_thread_id": facts.get("selection_thread_id") or thread_id,
                "scheduler_run_id": facts.get("scheduler_run_id"),
            }
    try:
        yield
    finally:
        if thread_id and active:
            with _CACHE_LOCK:
                names = agent._model_profile_live_names
                names.get(thread_id, {}).pop(display_key, None)
                if not names.get(thread_id):
                    names.pop(thread_id, None)
                projections = agent._model_profile_live_selections
                projections.get(thread_id, {}).pop(display_key, None)
                if not projections.get(thread_id):
                    projections.pop(thread_id, None)
        _BINDINGS.reset(token)


# LLM: 仅原执行线程可延长绑定；复制到模型 I/O 线程的上下文只读，不能把 ContextVar token 留给另一线程退出。
# 类用途: 让本片采用后的依赖持续到原 finalization，按后进先出清理，不修改共享 Agent。
@dataclass
class _ModelDependencyLifetime:
    agent: object
    thread_id: int
    stack: ExitStack


# LLM: 放在 selected_model_scope 内、真实执行/收口外；不选模型、不读取目录、不写持久状态，异常也按嵌套次序恢复。
# 函数用途: 为同一执行片中经宿主验证的模型依赖保留一个清理边界。
@contextmanager
def model_dependency_lifetime(agent: object):
    with ExitStack() as stack:
        lifetime = _ModelDependencyLifetime(agent, threading.get_ident(), stack)
        token = _LIFETIMES.set((*_LIFETIMES.get(), lifetime))
        try:
            yield
        finally:
            try:
                stack.close()
            finally:
                _LIFETIMES.reset(token)


# LLM: 只有当前线程的原执行范围可以接纳依赖；这只证明绑定位置可用，不证明候选授权、首请求资格或 CAS 成功。
# 函数用途: 在持久采用前检查本片能否继续使用同一批冻结依赖。
def model_dependency_lifetime_active(agent: object) -> bool:
    return any(row.agent is agent and row.thread_id == threading.get_ident() for row in reversed(_LIFETIMES.get()))


# LLM: 调用者必须完成原线程 CAS；此入口只绑定准备对象，不写选择、不探针，失败不能伪称候选已经执行。
# 函数用途: 把已采用的 config/backend/prompt/tools 及端点占用保持到当前片收尾，退出后恢复默认。
def activate_model_dependencies(agent: object, dependencies: ModelScopeDependencies, *, thread_id: str = "") -> None:
    from ..backends.request_scope import foreground_model_scope

    lifetime = next((row for row in reversed(_LIFETIMES.get())
                     if row.agent is agent and row.thread_id == threading.get_ident()), None)
    if lifetime is None:
        raise RuntimeError("当前执行片没有可绑定的模型依赖范围。")
    lifetime.stack.enter_context(model_dependencies_scope(agent, dependencies, thread_id=thread_id, active=True))
    lifetime.stack.enter_context(foreground_model_scope(agent.backend))


# LLM: 最外层按 canonical thread 建立初始依赖；后续显式选择由原循环请求安全点采用，子代理继承仍独立冻结。
# 函数用途: 为本次执行绑定初始模型及选择版本，退出恢复宿主依赖，不新建 MCP。
@contextmanager
def selected_model_scope(agent: object, *, inherited: bool = False, thread_id: str = "", active: bool = True):
    if not isinstance(getattr(type(agent), "config", None), ModelScopedAttribute) or id(agent) in (_BINDINGS.get() or {}):
        yield
        return
    from .thread_model_selection import _require_owner, thread_model_profile_id

    selection = None
    if thread_id and not inherited:
        thread_model_profile_id(agent, thread_id)
        thread = agent.conversation_store.threads.load(thread_id)
        if thread is None:
            from .model_profiles import ModelProfileError

            raise ModelProfileError("当前模型选择所属会话已不存在。")
        _require_owner(agent, thread)
        model_config = selected_model_config(agent, profile_id=thread.model_profile_id)
        selection = model_selection_snapshot(thread, model_config)
    else:
        model_config = agent.config if inherited else selected_model_config(agent)
    dependencies = prepare_model_dependencies(agent, model_config, inherited=inherited)
    assert dependencies is not None
    dependencies = replace(dependencies, selection=selection)
    with model_dependencies_scope(agent, dependencies, thread_id=thread_id, active=active):
        yield


# LLM: 状态只投影 canonical 选择与活动依赖；定时继承读原会话，固定策略不等待会话选模，实际请求版本仍在调用账。
# 函数用途: 展示执行来源、绑定模型与待采用版本；跨线程读取不改配置，bound 不声称供应商已收到。
def thread_model_execution_status(agent: object, thread_id: str) -> dict:
    from ..conversation.agent_activity import main_activity_metadata
    from .model_selection_status import latest_thread_model_call
    from .thread_model_selection import _require_owner

    thread = agent.conversation_store.threads.load(thread_id)
    if thread is None:
        return {"state": "unavailable", "reason_code": "THREAD_NOT_FOUND"}
    _require_owner(agent, thread)
    with _CACHE_LOCK:
        rows = getattr(agent, "_model_profile_live_selections", {}).get(thread_id, {})
        bound = dict(next(reversed(rows.values()))) if rows else None
    selected = {"profile_id": thread.model_profile_id, "revision": thread.model_selection_revision,
                "source": thread.model_selection_source, "selected_at": thread.model_selection_selected_at}
    pending = None
    if bound is not None and bound.get("selection_source") == "scheduler_fixed":
        selected = {"profile_id": bound["profile_id"], "revision": bound["selection_revision"],
                    "source": "scheduler_fixed", "selected_at": None}
        pending = False
    elif bound is not None and bound["explicit_revision"] is not None:
        source_id = bound.get("selection_thread_id") or thread_id
        source = thread if source_id == thread_id else agent.conversation_store.threads.load(source_id)
        if source is not None:
            _require_owner(agent, source)
            selected = {"profile_id": source.model_profile_id, "revision": source.model_selection_revision,
                        "source": source.model_selection_source, "selected_at": source.model_selection_selected_at}
            pending = source.model_selection_last_explicit_revision > bound["explicit_revision"]
    last_request = latest_thread_model_call(agent, thread_id)
    activity = main_activity_metadata(agent, thread_id, task_id=str((last_request or {}).get("task_id") or ""),
                                     bound_at=float((bound or {}).get("adopted_at") or 0)) if bound else {}
    return {
        "thread_id": thread_id,
        "selected": selected,
        "bound": bound,
        "pending": pending,
        "pending_reason": "next_request_boundary" if pending else "",
        "activity": activity,
        "last_request": last_request,
    }
