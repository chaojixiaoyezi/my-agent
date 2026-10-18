# LLM: 只操作本session已登记的实际C子进程；token/PID文本不授信，查询/清理不创建执行者或改P1/P3。
# 模块用途: 给W进程代理提供原生只读poll和精确原实例signal，信号清理在请求停止后仍可进行。
import os
import signal

from . import multiprocess_executor_identity as identities
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: 严格固定无FD消息形状；信号是整数OS枚举，不能以bool/0/任意PID/PGID声明控制权。
# 函数用途: 校验进程定位帧与单调seq，实际对象与同包sender另由C核验。
def require_process_request(request, previous):
    op, seq, token = request.get('op'), request.get('seq'), request.get('process_token')
    fields = {'op', 'seq', 'process_token'} | ({'signal'} if op == 'executor_signal' else set())
    if (op not in {'executor_poll', 'executor_signal'} or set(request) != fields
            or type(seq) is not int or seq <= previous
            or type(token) is not str or not token or len(token) > 512):
        raise WorkerCommitRejected('commit_request_invalid')
    if op == 'executor_signal' and (
            type(request['signal']) is not int or request['signal'] <= 0
            or request['signal'] not in signal.valid_signals()):
        raise WorkerCommitRejected('commit_request_invalid')
    return seq, op


# LLM: 原session同次核W/P1；host末回送前WNOWAIT，Linux用户退出/信号仅原pidfd设施，真实reap结原域pin；finish不提前退域，同步P5。
# 函数用途: 查询原实际返回码，或在原C短门和Popen防回收锁内核出生身份并清理同一子进程。
def handle_process_request(session, request):
    entry = session._processes.get(request['process_token'])
    if entry is None:
        raise WorkerCommitRejected('original_executor_process_required')
    process, source, row = entry
    source._require_source_identity()
    if (source.issuer is not session._sources[0] or row['state'] != 'registered'
            or row['process_identity']['pid'] != process.pid or source.read(row['reservation_id']) != row):
        raise WorkerCommitRejected('original_executor_process_required')
    returncode = _original_returncode(session, request['process_token'], process)
    if request['op'] == 'executor_signal' and returncode is None:
        # 保留原实例直到kill完成，防其它Popen waiter在核验后回收PID；不持T等待用户命令或退出。
        with source._sources[4]._lifecycle_gate, process._waitpid_lock:
            source._require_source_identity()
            if process.returncode is None:
                if (identities.process_birth(process.pid) != row['process_identity']['start_time']
                        or os.getpgid(process.pid) != row['pgid'] or os.getsid(process.pid) != row['sid']):
                    raise WorkerCommitRejected('original_executor_process_required')
                try:
                    _signal_original(process, request['signal'])
                except ProcessLookupError:
                    # 原实例此刻已退出；事实回收仍交真实Popen，不把发送失败重试到另一个PID。
                    pass
        returncode = _original_returncode(session, request['process_token'], process)
    domain = getattr(process, '_native_execution_domain', None)
    if domain is not None and returncode is not None:
        domain.retire_lifetime()
    return {'op': 'process', 'seq': request['seq'], 'result': {'returncode': returncode}}


# LLM: 仅原实际HostLane/Popen未完成末回送时不reap，原kernel同包sender须仍可核出生；EXIT观察不结pin，其他用户执行者仍原poll及namespace barrier。
# 函数用途: 防短host已发terminal packet但被W轮询先回收PID，使原真实末回送误拒。
def _original_returncode(session, token, process):
    from .multiprocess_native_host_lane import NativeHostLane

    host = session._hosts.get(token)
    if type(host) is NativeHostLane:
        host._require_original()
        if host.process is not process:
            raise WorkerCommitRejected('original_native_host_process_required')
        if host.session is None or not host.session.completed:
            if process.returncode is not None:
                raise WorkerCommitRejected('original_native_host_reaped_before_completion')
            with process._waitpid_lock:
                value = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                return None if value is None else value.si_status if value.si_code == os.CLD_EXITED else -value.si_status
    return process.poll()


# LLM: 调用方已持原source生命周期/Popen锁，Linux仅原pidfd设施signal；非域保原Popen路径，不因缓存或数字PID授新实例。
# 函数用途: 向精确原进程发信号，不递归争已持有的Popen防回收锁。
def _signal_original(process, signum):
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    domain = getattr(process, '_native_execution_domain', None)
    if type(domain) is NativePidNamespaceDomain:
        domain._reference_origin.send_signal(signum)
    else:
        os.kill(process.pid, signum)
