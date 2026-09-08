# LLM: 本模块只在工作片入口冻结用户选择的模型；共享 Agent 的其它线程和正在运行的子代理不得被热改。
# 模块用途: 把模型配置、后端和提示构造器一起绑定到当前执行上下文，离开时原样恢复。

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


# LLM: 未进入作用域时保持原 instance 属性语义；作用域仅覆盖三个模型依赖，不替换存储、工具和执行身份。
# 类用途: 让同一用户不同会话在切模型前后安全并行，不互相覆盖 config/backend/prompts。
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
        if row is not None and row[0] is instance:
            return row[1][self.name]
        try:
            return instance.__dict__[self.name]
        except KeyError as exc:
            raise AttributeError(self.name) from exc

    # LLM: 正常初始化/测试注入沿用底层默认依赖；菜单绝不通过 setattr 热改它们。
    # 函数用途: 保存 Agent 的部署默认依赖。
    def __set__(self, instance, value) -> None:
        instance.__dict__[self.name] = value


# LLM: 后端缓存只按完整模型配置快照摘要命中；有界、同 owner，并保留 backend 的能力探针缓存。
# 函数用途: 切换模型时按需创建后端，再次使用相同配置时不重复初始化或探测。
def _profile_backend(agent: object, config: object):
    from ..backends import get_backend

    values = [getattr(config, key) for key in (
        "model_backend", "model_name", "api_base", "api_key", "model_context_window_tokens", "max_tokens",
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


# LLM: 只绑定真正支持 descriptor 的 Agent；嵌套调用复用同一快照，异常/取消也必须清理，不读取模型文字。
# 函数用途: 在新主工作片开始时采用用户配置；已有执行和子代理继承的配置不被中途替换。
@contextmanager
def selected_model_scope(agent: object, *, inherited: bool = False):
    if inherited or not isinstance(getattr(type(agent), "config", None), ModelScopedAttribute) or id(agent) in (_BINDINGS.get() or {}):
        yield
        return
    config = selected_model_config(agent)
    if config is agent.config:
        yield
        return
    prompts = copy(agent.prompts)
    prompts.config = config
    token = _BINDINGS.set({**(_BINDINGS.get() or {}), id(agent): (agent, {
        "config": config, "backend": _profile_backend(agent, config), "prompts": prompts,
    })})
    try:
        yield
    finally:
        _BINDINGS.reset(token)
