# LLM: 插件共用通道包（M 线第一期 B2）；只导出连接池与异常，不导入面板或事件模块，保持单向依赖。
# 模块用途: 面板服务与事件中心共用同一套插件连接管理，设计见 docs/design/PLUGIN_EVENT_HOOKS.md。
from .pool import (
    ERROR_BACKOFF_SECONDS,
    IDLE_CLOSE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    ChannelCall,
    ChannelConnection,
    PluginChannelBackoff,
    PluginChannelError,
    PluginChannelPool,
    PluginChannelRevoked,
    PluginChannelStartFailed,
    PluginChannelTimeout,
    RetireScope,
)

__all__ = [
    "ERROR_BACKOFF_SECONDS",
    "IDLE_CLOSE_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "ChannelCall",
    "ChannelConnection",
    "PluginChannelBackoff",
    "PluginChannelError",
    "PluginChannelPool",
    "PluginChannelRevoked",
    "PluginChannelStartFailed",
    "PluginChannelTimeout",
    "RetireScope",
]
