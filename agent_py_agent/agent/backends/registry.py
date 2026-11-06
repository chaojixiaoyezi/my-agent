# LLM: 仅供可信宿主进程内显式登记，不加载插件、不执行构造器、不持久化；工厂接线仍归 factory。
# 模块用途: 保存自定义后端构造器并提供精确查找和冻结名称快照，不授予工具或网络权限。
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 设置校验层经 model_provider_schema 导入本模块；运行时导入 base 会形成 settings↔backends 环。
    from .base import BaseBackend

BackendConstructor = Callable[[Any], "BaseBackend"]
_BACKENDS: dict[str, BackendConstructor] = {}


# LLM: 只由可信宿主启动/装配调用；同名显式登记覆盖旧值，输入无效时不得改表，不调用构造器。
# 函数用途: 把精确协议名绑定到可调用的后端构造器。
def register_backend(name: str, constructor: BackendConstructor) -> None:
    if (not isinstance(name, str) or not name or name != name.strip()
            or any(char.isspace() or not char.isprintable() for char in name)):
        raise ValueError("backend name must be nonempty and contain no whitespace or control characters")
    if not callable(constructor):
        raise TypeError("backend constructor must be callable")
    _BACKENDS[name] = constructor


# LLM: 查询不规范化名称或触发构造，未登记返回 None；是否构造及内置优先级仍由原工厂决定。
# 函数用途: 按精确协议名称获取已登记的构造器。
def resolve_registered_backend(name: str) -> BackendConstructor | None:
    return _BACKENDS.get(name)


# LLM: 每次从当前表生成冻结副本；调用方不能借返回值修改注册状态或自动登记未知协议。
# 函数用途: 为模型校验返回当前已登记协议的只读名称快照。
def registered_backend_names() -> frozenset[str]:
    return frozenset(_BACKENDS)
