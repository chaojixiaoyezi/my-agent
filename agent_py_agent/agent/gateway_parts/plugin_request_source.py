# LLM: A0 DTO不是授权；只消费A的真实来源复验和C的执行上下文，不回退main。
# 模块用途: 为B2输入适配提供真实来源与执行上下文闸门。
from dataclasses import dataclass

from ..plugin_host.errors import HostAPIError


# LLM: 仅宿主组合根注入原路径和执行/控制接缝；空端口必须关闭而非借main运行。
# 类用途: 为B2绑定原网关资源，不接受公开JSON或任意管理员agent。
@dataclass(frozen=True)
class PluginRequestPorts:
    paths_for: object
    prepare_request: object = None
    cancel_request: object = None


# LLM: 缺A不得继续业务IO或把手造DTO当来源。
# 函数用途: 验证当前插件调用仍属于真实入站来源。
def require_plugin_source(authorized):
    try:
        from ..plugin_host.authorization import revalidate_call
    except ModuleNotFoundError as error:
        if error.name != "agent_py_agent.agent.plugin_host.authorization":
            raise
        raise HostAPIError("HOST_NOT_READY", 503) from error
    revalidate_call(authorized)
    return authorized
