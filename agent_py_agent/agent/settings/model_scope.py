# LLM: 原工作片与宿主显式候选共用依赖冻结；只绑定 ContextVar，不热改 Agent/存储，权限仍由 owner_access 唯一裁决。
# 模块用途: 绑定模型、提示和工具权限视图；存储、会话、MCP 连接仍使用原权威，离开时恢复。

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, field

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


# LLM: 缓存键含 OAuth 引用代次而非 token 与思考控制方式；刷新不换模型，退出/重登录不能复用原身份。
# 函数用途: 切换模型、采样或思考控制配置时创建对应后端，相同快照复用连接与探针缓存。
def _profile_backend(agent: object, config: object):
    from ..backends import get_backend

    values = [getattr(config, key) for key in (
        "model_backend", "model_name", "api_base", "api_key", "model_context_window_tokens", "max_tokens",
        "model_custom_headers", "model_session_header", "model_auth_ref",
        "temperature", "top_p", "stream_enabled", "anthropic_prompt_cache_enabled", "anthropic_version", "request_timeout",
        "model_temperature_explicit", "model_reasoning_control", "model_structured_output",
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

    # LLM: 配置来自原 permission_config，不重新解析 profile 或读取变化中的 owner 设置。
    # 函数用途: 读取这批依赖已冻结的配置对象。
    @property
    def config(self):
        return dict(self.values)["config"]


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
    token = _BINDINGS.set({**(_BINDINGS.get() or {}), id(agent): (agent, dict(dependencies.values))})
    display_key = object()
    if thread_id and active:
        with _CACHE_LOCK:
            names = getattr(agent, "_model_profile_live_names", None)
            if names is None:
                names = agent._model_profile_live_names = {}
            names.setdefault(thread_id, {})[display_key] = str(dependencies.config.model_name)
    try:
        yield
    finally:
        if thread_id and active:
            with _CACHE_LOCK:
                names = agent._model_profile_live_names
                names.get(thread_id, {}).pop(display_key, None)
                if not names.get(thread_id):
                    names.pop(thread_id, None)
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


# LLM: 普通调用仍只绑定最外层 descriptor Agent；显式候选另用 model_dependencies_scope，同源权限/缓存准备不会重选既有工作片。
# 函数用途: 工作片按 canonical thread 选模型，子代理保持继承语义，退出恢复且不新建 MCP。
@contextmanager
def selected_model_scope(agent: object, *, inherited: bool = False, thread_id: str = "", active: bool = True):
    if not isinstance(getattr(type(agent), "config", None), ModelScopedAttribute) or id(agent) in (_BINDINGS.get() or {}):
        yield
        return
    from .thread_model_selection import thread_model_config

    model_config = agent.config if inherited else (
        thread_model_config(agent, thread_id) if thread_id else selected_model_config(agent)
    )
    dependencies = prepare_model_dependencies(agent, model_config, inherited=inherited)
    assert dependencies is not None
    with model_dependencies_scope(agent, dependencies, thread_id=thread_id, active=active):
        yield
