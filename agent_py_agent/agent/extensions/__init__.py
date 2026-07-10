
"""extension plugin API for optional capabilities.

扩展模块通过这里声明注册入口，核心框架不直接依赖扩展内部实现。
"""

from .plugin import (
    EXTENSION_ENTRYPOINT_GROUP,
    ExtensionActivationError,
    ExtensionLoadError,
    ExtensionPlugin,
    ExtensionRegistry,
    load_extension_registry,
)

__all__ = [
    "EXTENSION_ENTRYPOINT_GROUP",
    "ExtensionActivationError",
    "ExtensionLoadError",
    "ExtensionPlugin",
    "ExtensionRegistry",
    "load_extension_registry",
]
