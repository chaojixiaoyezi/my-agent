"""LLM: extension plugin API for optional capabilities.

给人看的解释：
扩展模块通过这里声明注册入口，核心框架不直接依赖扩展内部实现。
"""

from .plugin import ExtensionPlugin, ExtensionRegistry

__all__ = ["ExtensionPlugin", "ExtensionRegistry"]

