# LLM: 模式事实由最早CLI组合根安装的纯内存provider提供；只拒受限执行器直接写全局事实，不授C/管理员权限，不依据路径/正文/目录名猜资源。
# 模块用途: 为真正全局写入口提供同一个worker硬保护，默认单进程无I/O，owner私有写和受控索引追加不调用本门。
from collections.abc import Callable


# LLM: 默认查询没有初始化或I/O；worker模式模块导入后由原冻结事实替换，不从环境恢复模式。
# 函数用途: 返回尚无受限执行器的默认状态。
def _unrestricted_writer():
    return None


_restricted_writer: Callable = _unrestricted_writer
_restricted_executor = False


# LLM: 错误只带资源合同名，无正文、路径或凭据，拒绝不能被当作成功或重放授权。
# 类用途: 表示worker企图直接修改属于协调者的全局事实。
class GlobalWriteInWorkerError(RuntimeError):
    error_code = 'GLOBAL_WRITE_IN_WORKER'

    # LLM: 资源名来自产品调用点的明确事实归属，不是worker报文自证权限。
    # 函数用途: 保存结构化拒绝原因，供原错误管线和验收读取。
    def __init__(self, resource):
        self.resource = resource
        super().__init__(self.error_code)


# LLM: 宿主设施不是W，不借W身份；独立错误仍只表示受限actor禁止全局写，不授权owner私有写。
# 类用途: 报告托管执行器企图直接修改协调者事实。
class GlobalWriteInExecutorError(GlobalWriteInWorkerError):
    error_code = 'GLOBAL_WRITE_IN_EXECUTOR'


# LLM: 仅原worker模式组合根注册查询；provider输出只拒绝，不能为任何actor签发写许可。
# 函数用途: 接入原冻结worker事实，不添加第二角色/身份账。
def bind_global_write_restriction(provider: Callable):
    global _restricted_writer
    _restricted_writer = provider


# LLM: 原native host完成受信bootstrap后调用，纯内存不可逆拒绝标记不含分区/角色字段，不假扮W，不授任何新权限。
# 函数用途: 让托管宿主及其继承子进程禁止通过产品API直接写全局事实。
def restrict_executor_global_writes():
    global _restricted_executor
    _restricted_executor = True


# LLM: 默认路径只做内存查询；caller仍须守原认证/审批/资源锁/原T/C生命周期，门通过不等于管理员授权。
# 函数用途: 在任何全局写副作用前拒绝worker，允许原C与默认单进程继续原权限检查。
def require_global_writer(resource):
    if _restricted_writer() is not None:
        raise GlobalWriteInWorkerError(resource)
    if _restricted_executor:
        raise GlobalWriteInExecutorError(resource)


# LLM: 只读原native host不可逆拒事实，不授权C/W或owner私有业务；调用方仍必须核原request/P3/P5，默认无I/O。
# 函数用途: 防止受限宿主遗漏当前请求上下文时回落到默认单进程业务路。
def restricted_executor_global_writes():
    return _restricted_executor
