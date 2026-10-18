# LLM: Native-only read observations/cleanup use actual kernel instances, never ps fallback or new execution authority.
# 模块用途: Pair Store birth observations with original C's real child handle and bounded known-tree cleanup.
import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .multiprocess_executor_identity import _ProcBSDInfo, process_birth
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: This selects a read-only observer, not a launcher/issuer; default and legacy scopes retain their original probes.
# 函数用途: Detect only an original native W/host transport installed by the existing executor scope.
def native_observation_active():
    from ..common.executor_registration import _SCOPE
    from .multiprocess_native_executor_client import NativeExecutorClient, NativeExecutorRequest
    from .multiprocess_native_executor_scope import _ClaimExecutor
    from .multiprocess_native_host import NativeHostClient

    scope = _SCOPE.get()
    if scope is None or scope.creator_pid != os.getpid():
        return False
    bound = getattr(scope.launch, '__self__', None)
    if type(bound) is NativeExecutorRequest:
        if bound._origin is not bound or bound._creator_pid != os.getpid():
            return False
        transport = bound._transport
    elif type(bound) is NativeHostClient:
        transport = bound
    elif type(bound) is _ClaimExecutor and bound.creator_pid == os.getpid():
        # Read-only selection retains the original P5 bootstrap; this never supplies or changes a tool-call/write gate.
        from .multiprocess_bootstrap import require_worker_bootstrap
        bootstrap = bound.binding.snapshot.bootstrap
        require_worker_bootstrap(bootstrap, bootstrap.launch)
        transport = bound.client
    else:
        return False
    if type(transport) not in (NativeExecutorClient, NativeHostClient):
        return False
    transport._require_original()
    return True


# LLM: Keep legacy Store token shape; canonical P1 still retains the full kernel birth and remains sole executor authority.
# 函数用途: Project a known live/P1 birth to the original Store format without spawning an unregistered identity probe.
def store_birth(birth):
    if birth is None:
        return ''
    if sys.platform == 'darwin':
        return 'host:' + time.strftime('%a %b %e %H:%M:%S %Y', time.localtime(int(birth.split(':', 1)[0])))
    if sys.platform == 'linux':
        return 'proc:' + str(birth)
    return ''


# LLM: ESRCH is an explicit kernel no-live-instance result; EPERM/partial/unsupported remain unknown, not death.
# 函数用途: Observe whether the selected native process is no longer live, without waitpid ownership in host/W.
def kernel_terminated(pid):
    if sys.platform == 'darwin':
        info, error = _darwin_info(pid)
        return error == errno.ESRCH or info is not None and info.prefix[1] == 5
    if sys.platform == 'linux':
        try:
            return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] in {'Z', 'X'}
        except FileNotFoundError:
            return True
        except (OSError, IndexError):
            return False
    return False


# LLM: Fixed native libproc ABI; failed/short reads never manufacture birth/status.
# 函数用途: Read one selected macOS process's kernel state and retain the actual errno.
def _darwin_info(pid):
    try:
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        info = _ProcBSDInfo()
        ctypes.set_errno(0)
        size = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        return (info, 0) if size == ctypes.sizeof(info) else (None, ctypes.get_errno() or errno.EIO)
    except (OSError, AttributeError, OverflowError):
        return None, errno.EIO


# LLM: Query only one known parent/group, not all user processes; saturation and API errors are explicit gaps.
# 函数用途: Enumerate selected native descendants or process-group members within a bounded kernel buffer.
def _darwin_members(kind, identifier):
    try:
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_listpids
        query.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        buffer = (ctypes.c_int * 8192)()
        ctypes.set_errno(0)
        size = query(kind, identifier, buffer, ctypes.sizeof(buffer))
        if size < 0 or ctypes.get_errno() or size >= ctypes.sizeof(buffer) or size % ctypes.sizeof(ctypes.c_int):
            return [], False
        return [p for p in buffer[:size // ctypes.sizeof(ctypes.c_int)] if p > 0], True
    except (OSError, AttributeError, OverflowError):
        return [], False


# LLM: Original C retains the exact unreaped Popen/P1. Missing current birth plus unavailable status is an observation gap, not a new identity or death proof; a concrete different live birth still rejects.
# 函数用途: 查询缺口保留已知实例和unknown，不误报来源漂移，也不查询未知坐标或授权新实例。
def command_snapshot(process, row, retained):
    root, identity = process.pid, row['process_identity']['start_time']
    if process.returncode is not None:
        return dict(retained), False
    live = process_birth(root)
    exited = os.waitid(os.P_PID, root, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if live is None and exited is None:
        return dict(retained), False
    if live != identity and (exited is None or exited.si_pid != root):
        raise WorkerCommitRejected('original_executor_process_required')
    if sys.platform != 'darwin':
        # Linux uses existing proc-based tree observation; this branch is not macOS evidence.
        from ..tooling.process_registry import _process_tree_snapshot
        observed, complete = _process_tree_snapshot(root)
        snapshot = dict(retained)
        for pid, token in observed.items():
            birth = int(token[5:]) if token.startswith('proc:') and token[5:].isdigit() else None
            if pid in retained and birth is not None and birth != retained[pid]:
                complete = False
            else:
                snapshot[pid] = birth
        return snapshot, complete
    members, complete = _darwin_members(2, row['pgid'])
    pending, seen = [root, *members, *retained], set()
    snapshot = dict(retained)
    while pending and len(seen) < 8192:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        birth = process_birth(pid)
        if pid in retained and birth is not None and birth != retained[pid]:
            # Never rebind a remembered old instance merely because the numeric PID now resolves again.
            complete = False
            continue
        if pid == root:
            birth = identity
        if birth is not None:
            snapshot[pid] = birth
            children, observed = _darwin_members(6, pid)
            complete = complete and observed
            pending.extend(children)
        elif not kernel_terminated(pid):
            snapshot.setdefault(pid, None)
            complete = False
    return snapshot, complete and not pending


# LLM: 只signal原已知实例且等待在门外；Linux回收仅委托原私有表设施真实waitpid，R前v2域可证全树；Darwin只未fork单例，枚举/缓存/root wait不授信，核stop/P5。
# 函数用途: 停止及复查原实例，已wait不再重复waitid；fork域和未知观察仍不能结清。
def stop_command_tree(process, row, retained):
    from ..tooling.process_registry import ProcessTerminationReceipt

    if type(process) is not subprocess.Popen or process.pid != row['process_identity']['pid']:
        raise WorkerCommitRejected('original_executor_process_required')
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    domain = getattr(process, '_native_execution_domain', None)
    snapshot, complete = command_snapshot(process, row, retained)
    retained.update(snapshot)
    signalled = False
    for signum, seconds in ((signal.SIGTERM, .5), (signal.SIGKILL, .5)):
        if type(domain) is NativePidNamespaceDomain:
            process.send_signal(signum)
            signalled = True
        for pid, birth in snapshot.items() if type(domain) is not NativePidNamespaceDomain else ():
            if birth is not None and process_birth(pid) == birth:
                try:
                    os.kill(pid, signum)
                    signalled = True
                except ProcessLookupError:
                    pass
                except OSError:
                    complete = False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(kernel_terminated(p) or b is not None and process_birth(p) not in (None, b)
                   for p, b in snapshot.items()):
                break
            time.sleep(.01)
        # Reobserve before any early break: a root may fork/reparent during TERM even when every old instance is dead.
        current, current_complete = command_snapshot(process, row, retained)
        snapshot.update(current)
        retained.update(current)
        complete = complete and current_complete
        if all(kernel_terminated(p) or b is not None and process_birth(p) not in (None, b)
               for p, b in snapshot.items()):
            break
    unresolved = tuple(p for p, b in snapshot.items()
                       if not (kernel_terminated(p) or b is not None and process_birth(p) not in (None, b)))
    # Linux域只能由原专有设施消费waitstatus；普通Popen缓存或这里旧raw waitpid不能绕过原屏障来源。
    if type(domain) is NativePidNamespaceDomain:
        process.poll()
    else:
        complete = _reap_observed_root(process) and complete
    # A kernel no-live result can precede waitid readiness on macOS; bounded real waitpid, not inference, supplies the exit code.
    if process.returncode is None and kernel_terminated(process.pid):
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass
    # Snapshot query coverage is not containment: a new-session child may escape and be reparented between reads.
    process._native_host_observation_gap = getattr(process, '_native_host_observation_gap', False) or not complete
    # Only the original pre-R kernel history can prove that this domain never had descendants at all.
    from .multiprocess_native_command_domain import NativeSingletonDomain
    domain = getattr(process, '_native_command_domain', None)
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    if type(domain) is NativePidNamespaceDomain and domain.empty(process, row):
        return ProcessTerminationReceipt('native_pid_namespace_exit', True, process.returncode, len(snapshot), ())
    if type(domain) is NativeSingletonDomain and domain.empty(process, row) and not unresolved:
        return ProcessTerminationReceipt('native_singleton_kernel_exit', True, process.returncode, len(snapshot), ())
    return ProcessTerminationReceipt('native_known_tree_uncontained' if signalled else 'native_exited_uncontained',
                                      False, process.returncode, len(snapshot), unresolved)



# LLM: 仅非Linux contained原域保原标准reap路径；Linux必须原设施消费，不能凭root returncode或raw waitpid授全域退出。
# 函数用途: 保留Darwin/旧设施的原WNOWAIT→实际waitpid回收，不改变原singleton历史边界。
def _reap_observed_root(process):
    complete = True
    with process._waitpid_lock:
        if process.returncode is None:
            observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None:
                pid, status = os.waitpid(process.pid, os.WNOHANG)
                if pid == process.pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                else:
                    complete = False
    return complete
