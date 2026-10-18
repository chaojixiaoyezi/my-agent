# LLM: 内部组合根显式冻结 C/W 分区和请求绑定；四类工具只拿无权限推断的共用接口，不能解析用户测试标志。
# 模块用途: 将已经完成授权的真实入口接到 P1a 唯一账本与两阶段放行协议，不承担请求调度或 P2 回收裁决。
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from ..common.executor_registration import (
    ExecutorProcessOptions,
    ExecutorRegistrationError,
    executor_scope,
    reject_executor_platform,
)
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_executor_start import ExecutorCommand, ExecutorIO, start_executor


# LLM: 绑定按 P0 v1 白名单复制，忽略外部可变字典之后的修改；每次启动使用新预留，不把命令正文写盘。
# 函数用途: 由可信请求字段和分区生成完整 reserved 记录。
def _reservation(ledger, binding: dict, kind: str, reservation_id: str) -> dict:
    return {"schema_version": "multiprocess_executor_reservation.v1", "reservation_id": reservation_id,
            "kind": kind, "c_run_id": ledger.partition.c_run_id,
            **{key: binding[key] for key in ("owner_sha", "request_id", "execution_attempt_id", "lease_epoch", "tool_call_id")},
            "state": "reserved", "process_identity": None, "pgid": None, "sid": None}


# LLM: Popen shell=True 在 POSIX 相当于 /bin/sh -c 加原参数；文本选项只保留原入口标准流语义，不扩大 FD 白名单。
# 函数用途: 把原入口的受控执行参数转换成垫片 exec 的 argv 与标准流。
def _command(argv, options: ExecutorProcessOptions) -> ExecutorCommand:
    args = (argv,) if isinstance(argv, str) else tuple(argv)
    args = ("/bin/sh", "-c", *args) if options.shell else args
    return ExecutorCommand(args, options.cwd, options.env, ExecutorIO(options.stdio, text=options.text))


# LLM: 原ledger的定位冻结；原C活门另由绑定来源保留，观察者只能由可信宿主设施显式注入。
# 类用途: 为一次受信请求保存固定绑定和保留原来源的先登记后放行方法。
@dataclass(frozen=True)
class _RegistrationRoot:
    ledger: ExecutorLedger
    reservation: dict
    observer: object

    # LLM: 只传原沙箱批准的命令；成功记录仍 registered，进程退出也不能越过 P2 写 released。
    # 函数用途: 启动本次命令或抛出明确失败，不重试、不自动回放、不建立第二套权威账本。
    def launch(self, kind: str, argv, options: ExecutorProcessOptions):
        if os.name != "posix":
            reject_executor_platform()
        reservation = {**self.reservation, "reservation_id": f"exec-{uuid.uuid4().hex}", "kind": kind}
        if options.delegate is not None:
            return self._launch_delegate(reservation, argv, options)
        result = start_executor(self.ledger, reservation, _command(argv, options), self.observer)
        if result is None or not result.ok:
            raise ExecutorRegistrationError(result.error if result is not None else {"error_code": "EXECUTOR_CONTEXT_REQUIRED"})
        return result.process

    # LLM: 只接受工具设施明确申请的固定 host 委托；其它入口不得用委托替代原命令或恢复缺失上下文。
    # 函数用途: 为后台宿主启动同一账本的私有归属交接。
    def _launch_delegate(self, reservation: dict, argv, options: ExecutorProcessOptions):
        from .multiprocess_executor_delegation import launch_delegate

        if options.delegate != "background_host" or reservation["kind"] != "background_host" or options.shell:
            raise ExecutorRegistrationError({"error_code": "EXECUTOR_CONTEXT_CONFLICT", "stage": "delegate"})
        return launch_delegate(self.ledger, reservation, _command(argv, options), self.observer)


# LLM: 默认 count=0 在校验/随机数/进程观察前返回；正数冻结定位但保留可信原C提交方法，不能重建普通ledger抹门。
# 函数用途: 供内部请求组合根安装登记门，保留原来源并在退出后精确恢复请求上下文。
@contextmanager
def executor_registration_scope(ledger: ExecutorLedger, binding: dict, observer=None):
    if type(ledger.partition.count) is int and ledger.partition.count == 0:
        with executor_scope(None):
            yield
        return
    from .multiprocess_executor_scope_source import freeze_executor_source

    frozen = freeze_executor_source(ledger)
    record = frozen._bound_record(_reservation(frozen, binding, "binding_check", "binding-check"))
    root = _RegistrationRoot(frozen, record, observer)
    with executor_scope(root.launch, frozen.partition):
        yield
