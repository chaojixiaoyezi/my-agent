# LLM: 仅原C持宿主来源/P1/Popen，定位名不授权，每个消息核实际宿主；固定模块origin兼容.py/.pyc，同步核对发行闭包。
# 模块用途: 保持原生后台宿主的固定入口，来源、会话、通道和客户端仍由原模块各自负责。
import importlib.util
import os
import sys
from dataclasses import replace
from pathlib import Path

from .multiprocess_executor_delegation import (
    _check_host_argv,
    _send_handoff,
    receive_context,
    validate_context,
)
from .multiprocess_executor_start import start_executor
from .multiprocess_kernel_message import KernelMessageReceiver, KernelMessageSender
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_native_host_client import NativeHostClient, NativeHostProcess
from .multiprocess_native_host_lane import NativeHostLane
from .multiprocess_native_host_session import NativeHostSession
from .multiprocess_native_host_source import _HostSource
from .multiprocess_native_host_wire import _CONNECTED, _READY, host_addresses
from .multiprocess_worker_commit import WorkerCommitRejected

# Keep original public imports and protocol-test fault seam; no second implementation or writer.
__all__ = ['NativeHostClient', 'NativeHostProcess', 'NativeHostLane', 'NativeHostSession',
           '_HostSource', '_READY', '_CONNECTED', 'host_addresses', 'start_native_host', 'run_native_delegate']


# LLM: 仅解析随包固定模块origin，不启动进程、不读取FD或恢复权限；保持-I隔离，同步检查start调用及无源码发行测试。
# 函数用途: 为原匿名交接FD生成唯一native host启动参数，支持同包.py和.pyc。
def _native_host_bootstrap_argv(descriptor):
    return (sys.executable, '-I', str(Path(importlib.util.find_spec(
        "agent_py_agent.agent.gateway_parts.multiprocess_native_host_bootstrap").origin)), str(descriptor))


# LLM: Count0先于效果；C主体或W来源各保原T/source/P1，C先记原lease寿命pin、原W链不变；真实spawn/R/host sender守原门，不造writer或W许可，同步C/Whost tests。
# 函数用途: 在物理创建前保住host启动责任，原登记/R及handoff仍沿同一来源链，固定入口路径由无副作用helper生成。
def start_native_host(session, source, row, command):
    if type(source.partition.count) is int and source.partition.count == 0:
        return None
    from .multiprocess_native_host_start import NativeHostStartObserver, NativeHostStartSource
    _check_host_argv(command.argv)
    host = NativeHostLane(source,row,session._observer)
    startup = NativeHostStartSource(host,row)
    host.begin_start(startup)
    session._hosts[row['reservation_id']] = host
    reader = writer = -1
    try:
        host.retain_start_lifetime(row)
        reader,writer = os.pipe()
        argv = _native_host_bootstrap_argv(reader)
        delegated = replace(command,argv=argv,io=replace(command.io,pass_fds=(reader,)))
        launched = start_executor(startup,row,delegated,NativeHostStartObserver(host,session._observer))
        if launched is None or not launched.ok:
            return launched
        # No cleanup/finish inference if remember, handoff, READY or reply fails after R.
        token = launched.reservation['reservation_id']
        session._processes[token] = (launched.process,source,launched.reservation)
        host.remember(launched)
        _send_handoff(source,launched,command,writer)
        os.close(writer)
        writer = -1
        os.close(reader)
        reader = -1
        host.connect()
        return launched
    finally:
        host.end_start()
        if reader >= 0:
            os.close(reader)
        if writer >= 0:
            os.close(writer)


# LLM: 先关init FD/核规范P1，再同次认证原C父消息；host仅原SDK scope执行命令、全局writer纯内存限制绑定实际client，不冒充W、不从ledger恢复C权，核C/W正式host测试。
# 函数用途: Install native-only host execution scope; no creator/root/source object is transported or restored.
def run_native_delegate(descriptor):
    payload = receive_context(descriptor)
    ledger,row = validate_context(payload)
    addresses = host_addresses(ledger.root,ledger.partition,row['reservation_id'])
    receiver = KernelMessageReceiver(addresses[1])
    sender = None
    client = None
    try:
        sender = KernelMessageSender(addresses[0])
        client = NativeHostClient(sender,receiver,row)
        sender.send(_READY,timeout=5)
        response,peer = receive_control_packet(receiver,timeout=5)
        client._require_original()
        if response != _CONNECTED or peer != client._parent[0]:
            raise WorkerCommitRejected('original_coordinator_message_required')
        from ..common.executor_registration import bind_worker_requirement, executor_scope
        from ..common.global_write_guard import restrict_executor_global_writes
        from ..tooling.background_process_host import main
        bind_worker_requirement(lambda: ledger.partition)
        restrict_executor_global_writes()
        with executor_scope(client.launch,ledger.partition):
            result = main(payload['argv'][4:])
        client.complete_host()
        return result
    finally:
        # Endpoint closure is not pin retirement; original C retains unknowns if done reply failed.
        receiver.close()
        if sender is not None:
            sender.close()
