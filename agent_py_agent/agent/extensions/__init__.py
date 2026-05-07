# LLM: Extension module; keep plugin registration hooks stable.
# 模块用途: 定义插件扩展点，让外部能力注册工具、命令、工作流或记忆源。

"""extension plugin API for optional capabilities.

给人看的解释：
扩展模块通过这里声明注册入口，核心框架不直接依赖扩展内部实现。
"""

from .plugin import ExtensionPlugin, ExtensionRegistry

__all__ = ["ExtensionPlugin", "ExtensionRegistry"]

