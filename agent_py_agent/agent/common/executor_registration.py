# LLM: 工具层只依赖这个无磁盘上下文接口；启动由内部组合根注入，C/W角色事实只拒scope丢失的裸Popen，不授信；同步C/W登记与零模式合同。
# 模块用途: 让已获准入口在内部多进程模式走登记门，默认路径无额外文件或进程操作，C回调丢上下文也明确拒绝。
from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path


# LLM: 文本管道只保留原解码和缓冲声明；None/None是原locale/strict，不成为任意kwargs或命令授权通道。
# 类用途: 把既有文本标准流语义传给可信启动设施。
@dataclass(frozen=True)
class ExecutorTextIO:
    encoding: str | None = "utf-8"
    errors: str | None = "replace"
    bufsize: int = 1

    # LLM: 参数名白名单固定，不允许调用方夹带 shell、FD 或启动器选项。
    # 函数用途: 生成文本标准流所需的原 Popen 参数。
    def popen_options(self) -> dict:
        return {"text": True, "encoding": self.encoding, "errors": self.errors, "bufsize": self.bufsize}


# LLM: 只保存原入口已裁决的参数，不负责授权、沙箱选择或推测标准流。
# 类用途: 将内部执行需要保留的 cwd、环境、标准流和 shell 语义作为一个启动参数传递。
@dataclass(frozen=True)
class ExecutorProcessOptions:
    cwd: Path | str | None = None
    env: dict | None = None
    stdio: tuple = (None, None, None)
    shell: bool = False
    text: ExecutorTextIO | None = None
    delegate: str | None = None


# LLM: 失败没有 fallback/retry 权限；正文仅显示启动设施生成的安全结构化事实，不能回显 argv 或环境。
# 类用途: 向原入口交回登记失败与独立清理事实，禁止把失败自动当成命令可重跑。
class ExecutorRegistrationError(RuntimeError):
    # LLM: 错误保留 unknown，不把登记或清理未确认伪装成未产生副作用。
    # 函数用途: 冻结安全错误字段，并明确自动重放不被允许。
    def __init__(self, error: dict):
        self.error = {**error, "automatic_replay": False}
        super().__init__(json.dumps(self.error, sort_keys=True))


# LLM: fork 复制的 ContextVar 不具备原组合根权限；真实进程身份只用于拒绝，不作为账本登记身份。
# 类用途: 保存当前宿主注入的启动函数及创建该上下文的进程号。
@dataclass(frozen=True)
class _ExecutorScope:
    launch: Callable
    creator_pid: int
    tool_call_id: str | None = None


_SCOPE: ContextVar[_ExecutorScope | None] = ContextVar("executor_registration_scope", default=None)
# LLM: 默认进程尚未导入 worker 模式时也保持零查询、零 I/O，不能依赖环境猜测开关。
# 函数用途: 返回没有内部 worker 的初始内存状态。
def _default_worker_partition():
    return None


_worker_partition: Callable = _default_worker_partition
_coordinator_required: Callable = _default_worker_partition


# LLM: 仅由 worker 模式模块安装纯只读查询；不从环境、请求参数或用户配置读取开关。
# 函数用途: 让已建立的真实 worker 即使遗漏请求上下文也不能绕回默认 Popen。
def bind_worker_requirement(provider: Callable) -> None:
    global _worker_partition
    _worker_partition = provider


# LLM: 仅原C模块安装纯内存角色查询；该信号只禁止裸执行，不能恢复丢失scope或替代原T/P3/P5。
# 函数用途: 让实际正count C在上下文丢失时仍要求执行登记，不冒充worker分区。
def bind_coordinator_requirement(provider: Callable) -> None:
    global _coordinator_required
    _coordinator_required = provider


# LLM: 分支只有ContextVar、冻结W分区及原C纯内存登记；角色信号只拒默认绕行，零路径不构造账本、不查询进程或FD、不读写磁盘。
# 函数用途: 告诉原入口本次启动是否必须登记，丢失C/W上下文不能回落普通Popen。
def executor_registration_enabled() -> bool:
    partition = _worker_partition()
    return _SCOPE.get() is not None or (partition is not None and partition.count > 0) or bool(_coordinator_required())


# LLM: 只核原冻结W/C provider及不可逆host拒事实，显式Count0 launcher不是MP模式；纯内存，不授P5或恢复scope。
# 函数用途: 告诉跨层业务是否必须携带原请求执行端口。
def managed_request_execution_required() -> bool:
    from .global_write_guard import restricted_executor_global_writes

    return (_worker_partition() is not None or bool(_coordinator_required())
            or restricted_executor_global_writes())


# LLM: 嵌套作用域按 token 还原；显式 count=0 的空作用域也不能盖掉真实 worker 的进程级拒绝门。
# 函数用途: 供组合根为当前请求安装一个可信登记启动函数。
@contextmanager
def executor_scope(launch: Callable | None, partition=None):
    required = _worker_partition()
    if launch is not None and required is not None and required != partition:
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_CONFLICT", "stage": "binding"})
    token = _SCOPE.set(_ExecutorScope(launch, os.getpid()) if launch is not None else None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


# LLM: ToolExecutor在批准后的真实handler边界绑定规范call_id；纯内存上下文不授予权限、不读用户参数。
# 函数用途: 为已安装的执行上下文临时绑定当前工具调用，默认路径保持无查询和无I/O。
@contextmanager
def executor_tool_call_scope(tool_call_id: str):
    scope = _SCOPE.get()
    if scope is None:
        yield
        return
    if scope.creator_pid != os.getpid():
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_REQUIRED", "stage": "binding"})
    if not isinstance(tool_call_id, str) or not tool_call_id.strip() or len(tool_call_id) > 512:
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_TOOL_CALL_INVALID", "stage": "binding"})
    token = _SCOPE.set(replace(scope, tool_call_id=tool_call_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


# LLM: 只读当前进程上下文；fork副本在任何传输/锁之前拒绝，定位字段不替代C端原T/P3裁决。
# 函数用途: 让组合根取得真实规范工具调用编号，不从命令或工具正文猜测。
def current_executor_tool_call() -> str | None:
    scope = _SCOPE.get()
    if scope is None:
        return None
    if scope.creator_pid != os.getpid():
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_REQUIRED", "stage": "binding"})
    return scope.tool_call_id


# LLM: 只捕获原组合根已安装的真实launch引用，非新许可；fork副本拒绝，调用方仍须原request活scope和每次P1准入。
# 函数用途: 让同步原child执行线程沿用当次请求真实工具客户端，不运输旧tool_call_id或重造executor身份。
def capture_executor_launcher():
    scope = _SCOPE.get()
    if scope is None:
        return None
    if scope.creator_pid != os.getpid():
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_REQUIRED", "stage": "binding"})
    return scope.launch


# LLM: 没有受信绑定或 fork 继承的旧绑定一律拒绝；绝不执行旧 Popen，也不重试启动。
# 函数用途: 让已获准命令经过注入的先登记后放行实现，返回原等待链路所需的 Popen。
def registered_popen(kind: str, argv, options: ExecutorProcessOptions):
    scope = _SCOPE.get()
    if scope is None or scope.creator_pid != os.getpid():
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_REQUIRED", "stage": "binding"})
    return scope.launch(kind, argv, options)


# LLM: 当前 P1a 垫片使用 POSIX FD/PGID/SID 协议；内部模式 Windows 必须拒绝，而不是偷偷走原 powershell。
# 函数用途: 给不支持的平台返回明确拒绝，默认平台路径不调用此函数。
def reject_executor_platform() -> None:
    raise ExecutorRegistrationError({"error_code": "EXECUTOR_PLATFORM_UNSUPPORTED", "stage": "binding"})
