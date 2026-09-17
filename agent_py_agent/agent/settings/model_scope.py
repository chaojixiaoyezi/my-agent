# LLM: 工作片入口冻结模型及用户显式路径权限；共享 Agent 其他线程不被热改，子代理不能继承管理员全盘权限。
# 模块用途: 绑定模型、提示和工具权限视图；存储、会话、MCP 连接仍使用原权威，离开时恢复。

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy

from .model_profiles import selected_model_config

_BINDINGS: ContextVar[dict | None] = ContextVar("model_profile_bindings", default=None)
_CACHE_LOCK = threading.Lock()


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


# LLM: 缓存键含 OAuth 引用代次而非 token；刷新不换模型，退出/重登录不能复用原身份。
# 函数用途: 切换模型或采样配置时创建对应后端，相同快照复用连接与探针缓存。
def _profile_backend(agent: object, config: object):
    from ..backends import get_backend

    values = [getattr(config, key) for key in (
        "model_backend", "model_name", "api_base", "api_key", "model_context_window_tokens", "max_tokens",
        "model_custom_headers", "model_session_header", "model_auth_ref",
        "temperature", "top_p", "stream_enabled", "anthropic_prompt_cache_enabled", "anthropic_version", "request_timeout",
        "model_temperature_explicit",
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


# LLM: 只绑定 descriptor Agent；即使部署默认也冻结绑定，防止嵌套运行重读 owner 默认；退出恢复，不新建 MCP。
# 函数用途: 工作片按 canonical thread 选模型，期间切换仅影响下一片；子代理继承模型但保持家目录边界。
@contextmanager
def selected_model_scope(agent: object, *, inherited: bool = False, thread_id: str = "", active: bool = True):
    if not isinstance(getattr(type(agent), "config", None), ModelScopedAttribute) or id(agent) in (_BINDINGS.get() or {}):
        yield
        return
    from ..user_space.approval_mode import permission_config
    from .thread_model_selection import thread_model_config

    model_config = agent.config if inherited else (
        thread_model_config(agent, thread_id) if thread_id else selected_model_config(agent)
    )
    config = permission_config(model_config, agent.home_paths, inherited=inherited)
    prompts = copy(agent.prompts)
    prompts.config = config
    values = {
        "config": config,
        "backend": agent.backend if model_config is agent.config else _profile_backend(agent, config),
        "prompts": prompts,
    }
    if isinstance(getattr(type(agent), "tools", None), ModelScopedAttribute):
        from ..core import _resolve_owner_scope_and_access

        owner_root, access = _resolve_owner_scope_and_access(agent, config)
        values["tools"] = agent.tools.with_access_policy(
            access_mode=access, path_access_mode="full" if not owner_root else config.path_access_mode,
            owner_scope_root=owner_root,
        )
    token = _BINDINGS.set({**(_BINDINGS.get() or {}), id(agent): (agent, values)})
    display_key = object()
    if thread_id and active:
        with _CACHE_LOCK:
            names = getattr(agent, "_model_profile_live_names", None)
            if names is None:
                names = agent._model_profile_live_names = {}
            names.setdefault(thread_id, {})[display_key] = str(config.model_name)
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
