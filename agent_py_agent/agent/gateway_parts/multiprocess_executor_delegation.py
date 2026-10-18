# LLM: 只把已登记可信 host 的冻结归属经私有 FD 交出；不从用户环境/请求恢复权限，不新建第二份账本。
# 模块用途: 在独立后台宿主内恢复同一登记门，宿主命令仍须另行登记。
from __future__ import annotations

import importlib.util
import json
import os
import select
import stat
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from ..common.executor_registration import ExecutorRegistrationError, bind_worker_requirement
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_executor_process import WaitingExecutor, _write_context, abort_waiting
from .multiprocess_executor_start import _registration, start_executor
from .multiprocess_route import WorkerPartition

_HOST_MODULE = "agent_py_agent.agent.tooling.background_process_host"
# 私有归属只含固定记录，16KiB 上限避免交接变成无界正文运输。
_HANDOFF_MAX_BYTES = 16 * 1024
# 私有管道总共只等5秒；半帧和失联必须拒绝，不能无限阻塞独立host。
_HANDOFF_SECONDS = 5.0


# LLM: 固定模块origin兼容同包.py/.pyc，-I隔离且不接受任意模块；不能让命令PYTHONPATH/sitecustomize先于归属检查，同步核对发行闭包测试。
# 函数用途: 为私有交接 FD 生成唯一可信加载命令。
def bootstrap_argv(descriptor: int) -> tuple:
    return (sys.executable, "-I", str(Path(importlib.util.find_spec(
        "agent_py_agent.agent.gateway_parts.multiprocess_executor_bootstrap").origin)), str(descriptor))


# LLM: 仅允许原后台 host 主入口及三个原固定参数，不提供任意模块/代码加载委托。
# 函数用途: 拒绝非 host 命令或夹带参数的交接。
def _check_host_argv(argv) -> None:
    if (type(argv) not in {list, tuple} or len(argv) != 10
            or tuple(argv[:4]) != (sys.executable, "-u", "-m", _HOST_MODULE)
            or tuple(argv[4::2]) != ("--store", "--session", "--spec")
            or any(type(arg) is not str or not arg or "\x00" in arg for arg in argv)):
        raise ValueError("invalid_executor_host_command")


# LLM: FD 只在内部正数门中创建；registered/R 成功后才交冻结帧，收尾未知也没有自动重放许可。
# 函数用途: 登记 host 并把原归属送入该真实进程。
def launch_delegate(ledger, record: dict, command, observer):
    _check_host_argv(command.argv)
    result = None
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, "rb", buffering=0) as source, os.fdopen(writer, "wb", buffering=0) as target:
            delegated = replace(command, argv=bootstrap_argv(source.fileno()),
                                io=replace(command.io, pass_fds=(source.fileno(),)))
            result = start_executor(ledger, record, delegated, observer)
            _send_handoff(ledger, result, command, target.fileno())
        return result.process
    except BaseException as exc:
        _failed_delegate(ledger, result, exc)
        raise


# LLM: 只有 start 的完整登记读回和 R 成功才发帧；解码内容有界且不落盘，失败不能改为直接 host。
# 函数用途: 发送已登记 host 的原归属。
def _send_handoff(ledger, result, command, descriptor: int) -> None:
    if result is None or not result.ok:
        raise ExecutorRegistrationError(result.error if result else {"error_code": "EXECUTOR_CONTEXT_REQUIRED"})
    payload = {"version": 1, "gateway_root": str(ledger.root), "partition": asdict(ledger.partition),
               "registration": result.reservation, "argv": command.argv}
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(content) > _HANDOFF_MAX_BYTES:
        raise ValueError("executor_handoff_too_large")
    _write_context(descriptor, content)


# LLM: 已放行 host 也可能创建后代，直接子进程回收不能变成整树死亡证明；原记录保留完整身份供 P2。
# 函数用途: 独立清理交接失败，并用安全字段交回 unknown 和禁止重放。
def _failed_delegate(ledger, result, failure: BaseException) -> None:
    if result is None or not result.ok:
        return
    cleanup = abort_waiting(WaitingExecutor(result.process, -1, -1))
    abandonment = "unknown"
    try:
        ledger.abandon(result.reservation)
        abandonment = "confirmed"
    except BaseException:
        pass
    if isinstance(failure, Exception):
        raise ExecutorRegistrationError({"error_code": "EXECUTOR_RESERVATION_FAILED", "stage": "handoff",
                                         "cause": type(failure).__name__, "cleanup": cleanup,
                                         "abandonment": abandonment}) from failure


# LLM: 同一总5秒预算读足每片，EOF/半帧/尾随内容拒绝；诊断不含交接正文。
# 函数用途: 从匿名管道接收有界字节。
def _read_exact(descriptor: int, length: int, deadline: float) -> bytes:
    result = b""
    while len(result) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise TimeoutError("executor_handoff_timeout")
        fragment = os.read(descriptor, length - len(result))
        if not fragment:
            raise ValueError("executor_handoff_incomplete")
        result += fragment
    return result


# LLM: 重复键不能被 JSON 后项覆盖后冒用绑定；所有嵌套对象也经过此检查。
# 函数用途: 读取不允许重复字段的交接对象。
def _unique_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_executor_handoff_key")
    return result


# LLM: FD 必须为私有管道，先完整读取并关闭再解释权限；环境和普通文件不能充当交接来源。
# 函数用途: 解码显式版本帧，拒绝旧版、未知字段及不完整运输。
def receive_context(descriptor: int) -> dict:
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        if not stat.S_ISFIFO(os.fstat(source.fileno()).st_mode):
            raise ValueError("executor_handoff_pipe_required")
        deadline = time.monotonic() + _HANDOFF_SECONDS
        length = int.from_bytes(_read_exact(source.fileno(), 4, deadline), "big")
        if not 0 < length <= _HANDOFF_MAX_BYTES:
            raise ValueError("invalid_executor_handoff_length")
        content = _read_exact(source.fileno(), length, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([source.fileno()], [], [], remaining)[0] or os.read(source.fileno(), 1):
            raise ValueError("invalid_executor_handoff_end")
    payload = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_pairs)
    if (type(payload) is not dict or set(payload) != {"version", "gateway_root", "partition", "registration", "argv"}
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("invalid_executor_handoff_schema")
    return payload


# LLM: 载荷本身不是许可；唯一原记录和本进程真实内核身份必须逐字段一致，旧/未登记/错分区一律拒绝。
# 函数用途: 确认受信 host 的冻结账本绑定。
def validate_context(payload: dict):
    _check_host_argv(payload["argv"])
    partition = payload["partition"]
    if type(partition) is not dict or set(partition) != {"index", "count", "c_run_id"}:
        raise ValueError("invalid_executor_handoff_partition")
    ledger = ExecutorLedger(Path(payload["gateway_root"]), WorkerPartition(**partition))
    record = ledger._bound_record(payload["registration"])
    if record["state"] != "registered" or record["kind"] != "background_host":
        raise ValueError("executor_handoff_unregistered")
    if ledger.read(record["reservation_id"]) != record:
        raise ValueError("executor_handoff_readback_conflict")
    if any(record[key] != value for key, value in _registration(os.getpid()).items()):
        raise ValueError("executor_handoff_identity_conflict")
    return ledger, record


# LLM: 只在本可信 bootstrap 中恢复进程级必经门；scope 丢失仍失败即拒绝，不以环境重建归属。
# 函数用途: 在固定 host 主入口运行期间保持原冻结分区及请求登记上下文。
def run_delegate(descriptor: int) -> int:
    payload = receive_context(descriptor)
    ledger, record = validate_context(payload)
    from ..tooling.background_process_host import main
    from .multiprocess_executor_runtime import executor_registration_scope

    bind_worker_requirement(lambda: ledger.partition)
    with executor_registration_scope(ledger, record):
        return main(payload["argv"][4:])
