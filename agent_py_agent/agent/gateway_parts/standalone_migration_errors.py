# LLM: 迁移拒绝只运输固定原因/动作与原ref，不把异常正文或旧JSON转换为许可。
# 模块用途: 给原公开启动入口提供可行动的单进程迁移错误。
from .multiprocess_coordinator import CoordinatorUnavailable


# LLM: 继承原启动拒绝合同，不新增状态机；ref仅定位原来源，不能授执行权。
# 类用途: 让用户知道应先在原Count0恢复、正常停止或处理具体资源。
class StandaloneMigrationRejected(CoordinatorUnavailable):
    error_code = "GATEWAY_STANDALONE_MIGRATION_REJECTED"

    # LLM: 固定code与中文动作由产品调用方提供，不拼用户正文或秘密。
    # 函数用途: 保存机器原因与可行动说明，供原CLI拒绝输出。
    def __init__(self, reason: str, action: str, ref: str = ""):
        self.reason, self.action, self.ref = reason, action, ref
        super().__init__(reason)
