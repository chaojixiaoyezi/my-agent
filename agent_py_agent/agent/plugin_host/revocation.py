# LLM: 撤销使用原工厂和唯一账号revision，不新增可反向恢复的权限账本。
# 模块用途: 为工具、脚本、工作流步骤提供同一逐步撤销检查。
from __future__ import annotations

from .errors import HostAPIError
from .execution_policy import require_asset


# LLM: 保存的上下文必须是本工厂签发的原活对象，复制字段不构成执行许可。
# 类用途: 在长工作每步及短提交点检查原资格。
class UserRevocationFence:
    # LLM: 不读取或缓存可变账号状态，首次及每次调用由工厂核验。
    # 函数用途: 绑定准确宿主上下文。
    def __init__(self, factory, context):
        self.factory = factory
        self.context = context

    # LLM: 不把旧enabled缓存当成当前许可。
    # 函数用途: 检查激活、grant和用户修订。
    def check(self):
        return self.factory.check(self.context)


# LLM: 资产读取来自宿主原注册表，不能仅复用入队时的版本断言。
# 类用途: 为脚本和工作流每一步提供账号、安装与内容三重栅栏。
class AssetStepFence:
    # LLM: 原来源和资产绑定不能由步骤参数覆盖。
    # 函数用途: 保存步骤运行前要重验的准确资产。
    def __init__(self, factory, context, asset, read_asset):
        self.factory, self.context = factory, context
        self.asset, self.read_asset = asset, read_asset

    # LLM: 每次调用都读当前来源和注册内容，不缓存授权成功结论。
    # 函数用途: 在每个资产步骤前执行撤权检查。
    def check(self):
        self.factory.check(self.context)
        require_asset(self.context.execution_policy, self.asset)
        if self.read_asset is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        current = self.read_asset(self.context, self.asset)
        if current != self.asset:
            raise HostAPIError("ASSET_CHANGED", 409)
        self.factory.check(self.context)
        return self.asset
