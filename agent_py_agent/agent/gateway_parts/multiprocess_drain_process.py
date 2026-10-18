# LLM: P6只观察原C直接W退出；R前P4出生与原waitid/reap同源，Darwin公开ABI和zombie出生缺失未知，缓存/PID不补历史、不证明外native整树；同步ABI/CLI tests。
# 模块用途: 给协调者保留直接worker的真实内核waitstatus，避免把returncode缓存当退出证据。
import os
import subprocess
import sys
import time

from .multiprocess_executor_identity import process_birth
from .multiprocess_records import serialize_record

# 等待只是检查节拍（秒），不作为进程死亡或释放责任的依据。
_WORKER_EXIT_OBSERVATION_POLL_SECONDS = 0.01


# LLM: 只保存实际Popen与R前P4 identity，不签发worker或owner权限；实例不能复制/换PID/跨C，真实waitid先于任何cached returncode消费。
# 类用途: 记录一个原C直接worker的准确退出观察。
class OriginalWorkerExitObservation:
    # LLM: 原Popen/P4活时内核出生必须准确；仅核平台公开waitid能力，不spawn/信号/reap，未知不补缓存或出生。
    # 函数用途: 固定本worker的原子对象引用、PID和真实出生身份。
    def __init__(self, process, identity):
        if (type(process) is not subprocess.Popen or identity.get("pid") != process.pid
                or process_birth(process.pid) != identity.get("start_time")):
            raise ValueError("drain_original_worker_birth_required")
        self.process = self._process_origin = process
        self.identity_bytes = self._identity_origin = serialize_record(identity)
        self.pid, self._creator_pid, self._origin = process.pid, os.getpid(), self
        self._exit = self._exit_origin = None
        self._unknown = False
        self._poll_origin = self._wait_origin = None
        self._reap_allowed = self._reap_allowed_origin = None
        self.supported = _waitstatus_supported()

    # LLM: 首绑定先于任何原C waiter；只为本准确Popen替换观察，不改spawn/信号/业务权限；缓存污染仍unknown。
    # 函数用途: 让原Popen的poll/wait唯一消费本观察生产的真实waitstatus。
    def install(self, *, reap_allowed=None):
        self.require_original()
        if self.supported is not True:
            raise ValueError('drain_original_waitstatus_platform_unavailable')
        if self._poll_origin is not None:
            raise ValueError("drain_original_worker_waiter_rebind")
        if reap_allowed is not None and not callable(reap_allowed):
            raise ValueError("drain_original_reap_guard_required")
        self._reap_allowed = self._reap_allowed_origin = reap_allowed

        # LLM: Popen原私有poll接口无超时，退出码只来自本原内核收据，绝不从可变returncode反填。
        # 函数用途: 原Popen及其信号前置检查共享准确退出观察。
        def poll(_deadstate=None):
            return self._returncode() if self.observe() else None

        # LLM: 原Popen等待接口保持timeout语义；真实内核状态未知不能作为缓存成功返回。
        # 函数用途: 有界或无限等原直接子进程，不发送信号。
        def wait(timeout):
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self.observe():
                if self._unknown:
                    raise RuntimeError("worker_exit_unconfirmed")
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.process.args, timeout)
                time.sleep(_WORKER_EXIT_OBSERVATION_POLL_SECONDS)
            return self._returncode()

        self.process._internal_poll = self._poll_origin = poll
        self.process._wait = self._wait_origin = wait
        return self

    # LLM: 原C在同Popen锁内生产WNOWAIT/actualreap收据；Linux原birth，Darwin原sysctl同出生/直接C zombie，缺任一事实unknown，不读缓存反填。
    # 函数用途: 在真正回收之前取得并保存实际退出状态，活/未知均不确认。
    def observe(self):
        self.require_original()
        if self._unknown:
            return False
        if self._exit is not None:
            return self.confirmed
        if self.supported is not True:
            self._unknown = True
            return False
        with self.process._waitpid_lock:
            self.require_original()
            try:
                info = _original_waitid(self.pid, nowait=True)
            except (ChildProcessError, OSError, ValueError):
                self._unknown = True
                return False
            if info is None:
                if self.process.returncode is not None:
                    self._unknown = True
                return False
            from .multiprocess_records import deserialize_record

            identity = deserialize_record(self.identity_bytes)
            if info.si_pid != self.pid or _waitstatus_birth(self.pid) != identity["start_time"]:
                self._unknown = True
                return False
            if self._reap_allowed is not None and self._reap_allowed() is not True:
                return False
            try:
                reaped = _original_waitid(self.pid, nowait=False)
            except (ChildProcessError, OSError, ValueError):
                self._unknown = True
                return False
            if reaped is None or not _waitstatus_matches(info, reaped):
                self._unknown = True
                return False
            code = reaped.si_status if reaped.si_code == _exited_code() else -reaped.si_status
            if self.process.returncode not in (None, code):
                self._unknown = True
            self.process.returncode = code
            self._exit = self._exit_origin = reaped if sys.platform == 'darwin' else info
            return self.confirmed

    # LLM: 期限仅限制门外等候，活或未知保持原对象，不发送信号、不回退cached wait；原native树另行证明。
    # 函数用途: 在关闭预算内等待本worker真实退出，未确认返回False。
    def wait_until(self, deadline):
        while not self.observe():
            if self._unknown or time.monotonic() >= deadline:
                return False
            time.sleep(min(_WORKER_EXIT_OBSERVATION_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return True

    # LLM: 只能消费同一原内核结果对象；普通None/nonNone、复制或改cached returncode不能成为原退出收据。
    # 函数用途: 返回本直接worker是否有准确、未污染的内核退出事实。
    @property
    def confirmed(self):
        self.require_original()
        if self._unknown or self._exit is None or self._exit is not self._exit_origin:
            return False
        if self.process.returncode != self._returncode():
            self._unknown = True
        return not self._unknown

    # LLM: 只消费保存的原waitid结果，不能接外部returncode或PID；保持exit/SIG状态语义。
    # 函数用途: 从真实内核收据返回标准Popen退出码。
    def _returncode(self):
        return self._exit.si_status if self._exit.si_code == _exited_code() else -self._exit.si_status

    # LLM: 原对象/PID/身份字节/PID创建者精确一致；只作物理来源限制，不授业务许可。
    # 函数用途: 拒绝复制退出观察、进程替换和身份换壳。
    def require_original(self):
        if (self._origin is not self or self._creator_pid != os.getpid()
                or self.process is not self._process_origin or self.process.pid != self.pid
                or self.identity_bytes is not self._identity_origin
                or self._reap_allowed is not self._reap_allowed_origin
                or self._poll_origin is not None and (self.process._internal_poll is not self._poll_origin
                                                     or self.process._wait is not self._wait_origin)):
            raise ValueError("drain_original_worker_required")


# LLM: Linux原waitid要求保持；Darwin只显式公开ABI adapter，无支持仍unknown，不用waitpid缓存兜底。
# 函数用途: 确认原平台是否能生产准确退出收据，无进程副作用。
def _waitstatus_supported():
    if sys.platform == 'linux':
        return hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT')
    if sys.platform == 'darwin':
        from .multiprocess_drain_darwin_waitid import darwin_waitid_available

        return darwin_waitid_available()
    return False


# LLM: 同原child锁内执行，Darwin adapter保原WNOHANG/WNOWAIT语义，Linux选项与原实现完全相同。
# 函数用途: 统一实际内核peek/reap入口，不读返回码缓存。
def _original_waitid(pid, *, nowait):
    if sys.platform == 'darwin':
        from .multiprocess_drain_darwin_waitid import darwin_waitid

        return darwin_waitid(pid, nowait=nowait)
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | (os.WNOWAIT if nowait else 0))


# LLM: WNOWAIT已返回准确EXIT后仍须原内核出生；Darwin libproc对zombie不可读，改用公开sysctl准确原类型而非放宽门。
# 函数用途: 核仍未reap的原child出生和直接父C绑定。
def _waitstatus_birth(pid):
    if sys.platform == 'darwin':
        from .multiprocess_drain_darwin_waitid import darwin_zombie_birth

        try:
            return darwin_zombie_birth(pid)
        except (OSError, ValueError, OverflowError):
            return None
    return process_birth(pid)


# LLM: Darwin公开CLD_EXITED枚举=1，Linux原os常量不变，不解析正文或普通returncode。
# 函数用途: 区分原内核正常退出与信号退出。
def _exited_code():
    return 1 if sys.platform == 'darwin' else os.CLD_EXITED


# LLM: Darwin公开waitid正常退出可带全32bit，标准Popen消费原waitpid低8bit；SIG/core仍精确code/status，Linux三字段原样。
# 函数用途: 核原peek与实际reap同一PID和标准退出状态，拒绝别实例/停态结果。
def _waitstatus_matches(peek, reaped):
    status = peek.si_status
    if sys.platform == 'darwin' and peek.si_code == _exited_code():
        status &= 0xff
    return (peek.si_pid, peek.si_code, status) == (reaped.si_pid, reaped.si_code, reaped.si_status)
