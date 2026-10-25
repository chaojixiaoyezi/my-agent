# LLM: runner 只中断 exact attempt；冻结进程身份仅供观察退出，绝不授权杀整棵宿主树，联测共享宿主及接续。
# 模块用途: 分开子代理的精确线程中断和锁外宿主观察，保护 Gateway 与其它任务。
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ..concurrency.interrupt import interrupt_by_name
from ..tooling.process_registry import _birth_token_matches, capture_process_birth_token
from .process_control import is_pid_alive
from .runner_session_liveness import has_fresh_runner_session, runner_session_of


# LLM: 原实例只用于只读存活核对；report 不能转成进程树排他授权。
# 类用途: 把原宿主身份交给锁外观察，不扫描新执行或发送宿主信号。
@dataclass(frozen=True)
class FrozenRunnerStop:
    report: dict[str, object]
    pid: int = 0
    birth_token: str = ""


# LLM: 每个 worker 都登记 exact attempt 名；旧 batch 线程不是执行身份，不能回退中断共享或换代线程。
# 函数用途: 向已选定执行轮发送有界中断，缺失令牌如实返回，不改变生命周期。
def signal_runner_attempt(run_id: str, attempt_id: str) -> str:
    if attempt_id and interrupt_by_name(f"subagent-runner-attempt:{run_id}:{attempt_id}"):
        return "attempt_signaled"
    return "not_found"


# LLM: 调用者持 creation guard；整个有限批次也可能启动接续后代，因此 launch 不证明进程树排他。
# 函数用途: 冻结宿主的只读观察身份，缺身份或共享时如实报告，不把 PID 当清理许可。
def freeze_runner_stop(task: object, tasks: list, closed_run_ids: set[str], *, kill_process: bool) -> FrozenRunnerStop:
    session = runner_session_of(task) if has_fresh_runner_session(task) else {}
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
    return FrozenRunnerStop({"status": "cooperative_requested", "pid": pid}, pid, birth)


# LLM: 共用原出生比较兼容旧locale，原生复核失败仍是未知；只读观察不授整树信号权限，不把未知算退出。
# 函数用途: 核对冻结宿主是否退出或确被新实例替换，同实例与无法观察分开报告。
def cleanup_runner_stop(stop: FrozenRunnerStop) -> dict[str, object]:
    report = dict(stop.report)
    if not stop.pid:
        return report
    if not is_pid_alive(stop.pid):
        return {**report, "status": "exited", "host_exited": True}
    current = capture_process_birth_token(stop.pid)
    if not current:
        return {**report, "host_exited": False, "identity_confirmed": False}
    try:
        same = _birth_token_matches(stop.pid, stop.birth_token, current)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {**report, "host_exited": False, "identity_confirmed": False}
    if not same:
        return {**report, "status": "instance_replaced", "host_exited": True}
    return {**report, "host_exited": False, "identity_confirmed": True}
