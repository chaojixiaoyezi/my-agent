# LLM: 只供宿主注入活许可/管理file commit，不从模型参数构造，不缓存许可或持正文；callback只运输原source短提交。
# 模块用途: 用一个不可变依赖集合绑定开关、URL许可、严格传输和活path policy。
from collections.abc import Callable
from dataclasses import dataclass


# LLM: 默认本地启用、网络关闭且无提交回调；回调不能签发授权，普通读取不创建文件或新的许可。
# 类用途: 收拢宿主依赖而不是扩大配置或授权范围。
@dataclass(frozen=True)
class AgentsSourceOptions:
    enabled: Callable = lambda: True
    network_enabled: Callable = lambda: False
    url_allowed: Callable = lambda url: False
    fetcher: Callable | None = None
    url_validator: Callable | None = None
    path_policy_provider: Callable | None = None
    file_commit: Callable | None = None
