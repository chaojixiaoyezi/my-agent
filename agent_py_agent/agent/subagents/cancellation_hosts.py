# LLM: runner先中断exact attempt；前台工具退出未确认的宿主只协作等待，独占升级也不能制造孤儿进程组。
# 模块用途: 分开精确线程中断、只读宿主观察及独占升级，保护共享宿主与其它任务。
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..concurrency.interrupt import interrupt_by_name
from ..tooling.process_registry import _birth_token_matches, capture_process_birth_token
from .process_control import is_pid_alive
from .runner_session_liveness import (
    has_fresh_runner_session,
    runner_session_matches_stop,
    runner_session_of,
)


# LLM: 原实例身份只用于退出核验；cleanup_unconfirmed禁止单PID升级，report不能授权清理进程树。
# 类用途: 把原宿主身份交给锁外清理，共享/身份不明者不携带信号授权。
@dataclass(frozen=True)
class FrozenRunnerStop:
    report: dict[str, object]
    pid: int = 0
    birth_token: str = ""
    relay_interval_seconds: float = 0.0
    cleanup_unconfirmed: bool = False
    canonical_state_ref: str = ""
    run_id: str = ""
    attempt_id: str = ""
    session_id: str = ""


# LLM: 每个 worker 都登记 exact attempt 名；旧 batch 线程不是执行身份，不能回退中断共享或换代线程。
# 函数用途: 向已选定执行轮发送有界中断，缺失令牌如实返回，不改变生命周期。
def signal_runner_attempt(run_id: str, attempt_id: str) -> str:
    if attempt_id and interrupt_by_name(f"subagent-runner-attempt:{run_id}:{attempt_id}"):
        return "attempt_signaled"
    return "not_found"


# LLM: 调用者持 creation guard；整个有限批次也可能启动接续后代，因此 launch 不证明进程树排他。
# 函数用途: 冻结原宿主的独占身份，缺身份或共享时不交付可升级的 PID 与 birth token。
def freeze_runner_stop(task: object, tasks: list, closed_run_ids: set[str], *, kill_process: bool,
                       tools_exit_confirmed: bool = False) -> FrozenRunnerStop:
    session = runner_session_of(task)
    # 超时只表示需重核；原停止回执仍绑定同attempt时保留身份，不能当成无宿主。
    if not has_fresh_runner_session(task) and not runner_session_matches_stop(task, session):
        session = {}
    if not session or session.get("in_process") is not False:
        return FrozenRunnerStop({"status": "no_pid"})
    try:
        pid = int(session.get("worker_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return FrozenRunnerStop({"status": "no_pid"})
    if not kill_process:
        return FrozenRunnerStop({"status": "skipped", "pid": pid})
    record = (getattr(task, "attributes", {}) or {}).get("background_start") or {}
    launch_id = str(record.get("launch_id") or "")
    birth = str(session.get("worker_birth_token") or "")
    if (not birth or not launch_id or session.get("launch_id") != launch_id
            or not session.get("attempt_id") or session["attempt_id"] != record.get("attempt_id")
            or record.get("pid") != pid):
        return FrozenRunnerStop({"status": "identity_unavailable", "pid": pid})
    siblings = []
    for candidate in tasks:
        if candidate.id in closed_run_ids:
            continue
        other = (getattr(candidate, "attributes", {}) or {}).get("background_start") or {}
        other_session = runner_session_of(candidate)
        if other.get("launch_id") == launch_id or other.get("pid") == pid or other_session.get("worker_pid") == pid:
            siblings.append(candidate.id)
    if siblings:
        return FrozenRunnerStop({
            "status": "shared_host_cooperative", "pid": pid, "escalated": False,
            "shared_run_ids": sorted(siblings),
        })
    interval = session.get("interval_seconds", 5.0)
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        interval = 5.0
    # 前台命令有独立进程组；未取得原worker的工具退出证明前，单杀宿主会把它托孤。
    attrs = getattr(task, 'attributes', {}) or {}
    return FrozenRunnerStop({"status": "cooperative_requested", "pid": pid}, pid, birth,
                            float(interval), not tools_exit_confirmed,
                            str(attrs.get('canonical_state_ref') or ''), str(task.id),
                            str(session.get('attempt_id') or ''), str(session.get('session_id') or ''))


def _original_tools_exit_confirmed(stop: FrozenRunnerStop, diagnostics: dict | None = None) -> bool:
    """只读原规范会话的实际收尾；worker PID消失或换代不能替代工具退出证明。"""
    if not stop.cleanup_unconfirmed:
        return True
    if not all((stop.canonical_state_ref, stop.run_id, stop.attempt_id, stop.session_id)):
        if diagnostics is not None:
            diagnostics['proof_reason'] = 'ORIGINAL_RUNNER_SESSION_IDENTITY_MISSING'
            diagnostics['proof_next_action'] = '核对原run停止回执、规范文件定位与session/attempt身份；缺失只能保留未确认责任，不得推测进程归属'
        return False
    from ..common.nofollow_fs import read_bytes_beneath

    path = Path(stop.canonical_state_ref)
    try:
        raw = read_bytes_beneath(path.parent, (path.name,), require_dir_fd=True)
        if raw is None:
            if diagnostics is not None:
                diagnostics.update(proof_reason='ORIGINAL_SESSION_EXIT_RECEIPT_MISSING',
                                   proof_next_action='核对原规范会话文件；缺失只能保留未确认责任，禁止据worker消失宣称工具已退出')
            return False
        payload = json.loads(raw)
        session = (payload.get('attributes') or {}).get('runner_session') or {}
        receipt = ((payload.get('attributes') or {}).get('cancel_subagents') or {}).get('stop_receipt') or {}
        ended = session.get('ended_at')
        if diagnostics is not None:
            diagnostics['proof_session_status'] = session.get('status')
        confirmed = (payload.get('id') == stop.run_id and payload.get('runner_active_attempt_id', '') in ('', stop.attempt_id)
                and receipt.get('schema') == 'runner-stop.v1' and receipt.get('run_id') == stop.run_id
                and receipt.get('attempt_id') == stop.attempt_id and session.get('run_id') == stop.run_id
                and session.get('session_id') == stop.session_id and session.get('attempt_id') == stop.attempt_id
                and session.get('worker_pid') == stop.pid and session.get('worker_birth_token') == stop.birth_token
                and session.get('status') in {'completed', 'failed'}
                and type(ended) in (int, float) and math.isfinite(ended) and ended > 0)
        if diagnostics is not None:
            diagnostics['proof_reason'] = 'ORIGINAL_SESSION_EXIT_CONFIRMED' if confirmed else 'ORIGINAL_SESSION_EXIT_NOT_CONFIRMED'
        return confirmed
    except (OSError, ValueError, TypeError, AttributeError) as error:
        if diagnostics is not None:
            diagnostics.update(proof_reason='ORIGINAL_SESSION_EXIT_PROOF_UNREADABLE',
                               proof_error_type=type(error).__name__, proof_errno=getattr(error, 'errno', None),
                               proof_next_action='核对原规范会话文件存在性、权限与JSON格式，再核同run退出回执；禁止换目标或据此杀进程')
        return False


# LLM: 共用原出生比较兼容旧locale，原生复核失败仍是未知；只读观察不授整树信号权限，不把未知算退出。
# 函数用途: 核对冻结宿主是否退出或确被新实例替换，同实例与无法观察分开报告。
def cleanup_runner_stop(stop: FrozenRunnerStop) -> dict[str, object]:
    report = dict(stop.report)
    if not stop.pid:
        return report
    if stop.cleanup_unconfirmed:
        report['tools_exit_confirmed'] = False
    if not is_pid_alive(stop.pid):
        if stop.cleanup_unconfirmed:
            report['tools_exit_confirmed'] = _original_tools_exit_confirmed(stop, report)
        return {**report, "status": "exited", "host_exited": True}
    current = capture_process_birth_token(stop.pid)
    if not current:
        return {**report, "host_exited": False, "identity_confirmed": False}
    try:
        same = _birth_token_matches(stop.pid, stop.birth_token, current)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {**report, "host_exited": False, "identity_confirmed": False}
    if not same:
        if stop.cleanup_unconfirmed:
            report['tools_exit_confirmed'] = _original_tools_exit_confirmed(stop, report)
        return {**report, "status": "instance_replaced", "host_exited": True}
    return {**report, "host_exited": False, "identity_confirmed": True}


# LLM: 先覆盖原取消relay；工具退出未确认只返回未知，其余独占升级每次信号前复核原出生身份。
# 函数用途: 在锁外等待协作退出，必要时单 PID 发信号；共享/身份不明零信号，不把发送成功当作退出。
def escalate_exclusive_runner_stop(
    stop: FrozenRunnerStop, *, grace_seconds: float = 1.5, kill_grace_seconds: float = 1.5,
    sleep=time.sleep, now=time.monotonic,
) -> dict[str, object]:
    signals = []
    if stop.report.get("status") != "cooperative_requested" or stop.pid <= 0 or not stop.birth_token.strip():
        return {"escalated": False, "signals": signals, "host_exited": False, "identity_confirmed": False}
    if not math.isfinite(grace_seconds) or not math.isfinite(kill_grace_seconds):
        raise ValueError("runner 停止宽限期必须是有限秒数")

    # 复用原只读字典合同；观察失败是未知，不能当作原实例消失或信号许可。
    def observe() -> dict[str, object]:
        try:
            return cleanup_runner_stop(stop)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {**stop.report, "host_exited": False, "identity_confirmed": False}

    # 使用单调时钟有界等待；退出、PID 换代或身份未知时立即停止，不追踪新的执行。
    def wait(seconds: float) -> dict[str, object]:
        deadline = now() + max(0.0, seconds)
        while True:
            observed = observe()
            if observed.get("host_exited") is True or observed.get("identity_confirmed") is not True:
                return observed
            remaining = deadline - now()
            if remaining <= 0:
                return observed
            sleep(min(0.05, remaining))

    # 覆盖原持久取消的一次relay；超长/坏节拍也只等待有限预算，不能卡住控制入口。
    relay_wait = min(10.0, max(0.0, stop.relay_interval_seconds) + 0.5) if stop.relay_interval_seconds else 0.0
    observed = wait(max(grace_seconds, relay_wait))
    # 等待轮询只看原PID；预算到期才读一次规范退出回执，避免反复整文件读取。
    if stop.cleanup_unconfirmed and observed.get('host_exited') is not True:
        observed['tools_exit_confirmed'] = _original_tools_exit_confirmed(stop, observed)
    if stop.cleanup_unconfirmed and observed.get("tools_exit_confirmed") is not True and observed.get("host_exited") is not True:
        return {**observed, "escalated": False, "signals": signals,
                "cleanup_state": "unconfirmed", "reason_code": "RUNNER_TOOL_EXIT_UNCONFIRMED",
                "next_action": "等待原runner转送取消后核同run停止回执；仍未确认时重试原run取消，禁止改停其它run或共享宿主"}
    for signum, timeout in ((signal.SIGTERM, grace_seconds), (getattr(signal, "SIGKILL", None), kill_grace_seconds)):
        if observed.get("host_exited") is True or observed.get("identity_confirmed") is not True:
            break
        # 等待完成的观察不能代替发信号前的核验，防止在两个阶段之间复用 PID。
        observed = observe()
        if observed.get("host_exited") is True or observed.get("identity_confirmed") is not True:
            break
        if signum is None:
            observed["signal_error_type"] = "UnsupportedSignal"
            break
        try:
            os.kill(stop.pid, signum)
        except OSError as exc:
            # ESRCH 也需重新观察，不能把信号异常冒充退出；权限失败保留原实例的真实状态。
            observed = observe()
            observed["signal_error_type"] = type(exc).__name__
            break
        signals.append(signum)
        observed = wait(timeout)
    return {**observed, "escalated": bool(signals), "signals": signals}
