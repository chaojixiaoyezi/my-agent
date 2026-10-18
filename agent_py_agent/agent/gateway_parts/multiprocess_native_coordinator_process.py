# LLM: C后台host句柄只观察原HostLane实际Popen，完成末回送前WNOWAIT保sender出生；只复用原标准流通信实现，不fake W transport或恢复C权限，同步真正短host竞态/生命周期测试。
# 模块用途: 给C主体原后台工具提供Popen兼容句柄，让poll/wait不能先回收尚待认证的宿主。
import os
import signal

from .multiprocess_executor_start import _registration
from .multiprocess_native_executor_client import NativeExecutorProcess
from .multiprocess_native_executor_process import _original_returncode
from .multiprocess_native_host_authority import host_lifecycle_gate
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: client字段持实际C root而非W IPC client；父类仅提供原PIPE通信/等待通用实现，控制/identity全部本类核原source/HostLane/Popen。
# 类用途: 保留真实C host内核实例至精确末回送认证，不把返回码缓存当责任结清。
class CoordinatorNativeHostProcess(NativeExecutorProcess):
    # LLM: physical Popen继续唯一保存在C root/host/P1；stdio是原C创建的流，代理不造新进程或新账，row/token仅原对象定位。
    # 函数用途: 在真实R/handoff/READY成功后捕获本地C的原宿主句柄。
    def __init__(self, root, result, args):
        process, row = result.process, result.reservation
        host = root._hosts[row['reservation_id']]
        self._physical = self._physical_origin = process
        self._host = self._host_origin = host
        self._root_origin = root
        super().__init__(root, row['reservation_id'], process.pid, args, process)
        self._require_original()

    # LLM: 同C/root/source/HostLane/Popen及规范P1逐次核，copy/fork/换壳先于锁；closing仍可精确观察，不能授新spawn。
    # 函数用途: 防进程代理借其它C来源或实例执行信号/回收。
    def _require_original(self):
        from .multiprocess_native_coordinator_host import CoordinatorNativeHostRoot
        if (self._origin is not self or self._creator_pid != os.getpid()
                or type(self._client) is not CoordinatorNativeHostRoot or self._client is not self._root_origin
                or self._physical is not self._physical_origin or self._host is not self._host_origin
                or self._client._hosts.get(self._token) is not self._host
                or self._host.process is not self._physical or self.pid != self._physical.pid):
            raise WorkerCommitRejected('original_coordinator_native_host_process_required')
        self._client._require_original()
        self._host._require_original()
        if self._host.source.read(self._host.row['reservation_id']) != self._host.row:
            raise WorkerCommitRejected('original_coordinator_native_host_process_required')

    # LLM: 实际WNOWAIT观测每次重读，不用代理返回码让原Popen提前reap；只有host.completed真实末回送后原poll可回收，timeout只沿父类等待预算。
    # 函数用途: 返回原实际宿主退出码，同时保存其PID/出生供排队末回送认证。
    def _poll(self, timeout=None):
        self._require_original()
        self.returncode = _original_returncode(self._client, self._token, self._physical)
        return self.returncode

    # LLM: 合法清理信号只消费实际C Popen/HostLane/P1身份和原lease门；无T等待，不从代理PID文本授信，退出状态不重试到复用PID。
    # 函数用途: 在本地C对原实例安全发送信号，仍不把它当全域/末回送证明。
    def send_signal(self, signum):
        self._require_original()
        signum = int(signum) if isinstance(signum, signal.Signals) else signum
        if type(signum) is not int or signum <= 0 or signum not in signal.valid_signals():
            raise ValueError('invalid_executor_signal')
        if self._poll() is not None:
            return
        process, row = self._physical, self._host.row
        with host_lifecycle_gate(self._host.source), process._waitpid_lock:
            self._require_original()
            if process.returncode is None:
                if _registration(process.pid) != {key: row[key] for key in ('process_identity', 'pgid', 'sid')}:
                    raise WorkerCommitRejected('original_coordinator_native_host_process_required')
                try:
                    os.kill(process.pid, signum)
                except ProcessLookupError:
                    pass
        self._poll()
