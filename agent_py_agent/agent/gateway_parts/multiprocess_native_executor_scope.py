# LLM: 原P5绑定只提供定位；真实登记、P3准入、原T与C短门仍由原native执行session裁决。
# 模块用途: 把正式W认领请求及规范工具调用接到同一个持久资源客户端，不启动W进程、不重试。
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace

from ..common.executor_registration import (
    ExecutorRegistrationError,
    current_executor_tool_call,
    executor_scope,
)
from .multiprocess_bootstrap import require_worker_bootstrap
from .multiprocess_dispatch_binding import ExecutionBinding
from .multiprocess_native_executor_client import NativeExecutorClient


# LLM: 请求身份来自原claim，客户端来自W启动组合根；冻结对象只运输定位，不成为新权限源。
# 类用途: 保存一次原认领的客户端引用和创建进程，工具调用编号在实际执行边界读取。
@dataclass(frozen=True)
class _ClaimExecutor:
    binding: ExecutionBinding
    client: NativeExecutorClient | None
    creator_pid: int

    # LLM: fork/缺客户端/父C错配在传输前拒绝；bind_request共享原transport序号，绝不重造endpoint或P1。
    # 函数用途: 把已获准启动交给原C资源设施，保持实际request/attempt/tool调用关联。
    def launch(self, kind, argv, options):
        if self.creator_pid != os.getpid():
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_CONTEXT_REQUIRED', 'stage': 'binding'})
        bootstrap = self.binding.snapshot.bootstrap
        require_worker_bootstrap(bootstrap, bootstrap.launch)
        if type(self.client) is not NativeExecutorClient:
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_NATIVE_CLIENT_REQUIRED', 'stage': 'binding'})
        self.client._require_original()
        if self.client._parent[0] != bootstrap.launch.parent_pid:
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_NATIVE_PARENT_MISMATCH', 'stage': 'binding'})
        call_id = current_executor_tool_call()
        if call_id is None:
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_TOOL_CALL_REQUIRED', 'stage': 'binding'})
        claim = self.binding.claim
        argv, options = _native_command(argv, options)
        return self.client.bind_request(claim.request_id, claim.execution_attempt_id, call_id).launch(kind, argv, options)


# LLM: 只归一原已获准POSIX shell=True调用，不拆词或解析正文；cwd/env/stdio/text/delegate原样保留。
# 函数用途: 用原/bin/sh -c及其位置参数表示已有shell语义，资源协议仍只运输argv。
def _native_command(argv, options):
    if not options.shell:
        return argv, options
    if os.name != 'posix':
        raise ExecutorRegistrationError({'error_code': 'EXECUTOR_PLATFORM_UNSUPPORTED', 'stage': 'binding'})
    args = [argv] if isinstance(argv, str) else list(argv)
    return ['/bin/sh', '-c', *args], replace(options, shell=False)


# LLM: 只在原P5已核claim与Agent归属之后安装；默认单进程不调用，结束精确还原ContextVar。
# 函数用途: 将已认领请求接入工具登记边界，缺资源客户端时也禁止回落W Popen。
@contextmanager
def native_request_executor_scope(binding, client):
    if type(binding) is not ExecutionBinding:
        raise ExecutorRegistrationError({'error_code': 'EXECUTOR_CONTEXT_REQUIRED', 'stage': 'binding'})
    bootstrap = binding.snapshot.bootstrap
    require_worker_bootstrap(bootstrap, bootstrap.launch)
    launcher = _ClaimExecutor(binding, client, os.getpid())
    with executor_scope(launcher.launch, bootstrap.launch.partition):
        yield
