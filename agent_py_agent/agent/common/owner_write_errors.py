# LLM: 唯一定义原owner写来源拒绝类型；只作负向异常传播，不授写权、清理权或恢复权。Gateway公开导入仍指向同一类，勿复制定义。
# 模块用途: 让记忆、配额与Gateway共享原异常协议，避免底层领域反向依赖Gateway。
from ..runtime_errors import DataCorruptionError


# LLM: 精确错误子类只标原C失效，GET可只读返回；其他账本损坏不能被吞掉。
# 类用途: 区分旧C调用与真正坏回执。
class LocalControlCoordinatorUnavailable(DataCorruptionError):
    pass



# LLM: 通用运输拒绝只携带结构化原因，不签领域授权或定义AGENTS错误码；consumer在其原边界映射。
# 类用途: 将管理入口身份失效交给对应业务处理，不把权限错误当成新用户请求。
class OwnerManagementIngressUnavailable(ValueError):
    # LLM: 原因只供公开管理consumer映射，不从正文猜权限；没有可序列化permit。
    # 函数用途: 保存本次只读认证核验失败的原因。
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)
