# LLM: 子代理宿主的存活与启动记录入口；不改任务状态，不裁决共享宿主是否可终止。
#   终止复用 tooling.process_registry 的进程树原语，域回执必须保留未确认后代；
#   不能只凭宿主退出声称命令已停止。改动同步检查 cancel、capability_auto_sweep
#   及 test_subagent_process_control、test_dispatch_liveness_and_revive。
# 模块用途: 保存子代理宿主事实，将完整进程树终止结果交给取消和回收调用方处理。
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

# SIGTERM 后给进程的清理宽限。
DEFAULT_TERMINATE_GRACE_SECONDS = 2.0

# 进程实例身份(每次进程启动唯一):runner 会话记录它,宿主已死回收据此判"这条会话是不是
# 本进程这一代记的"。异代(网关重启/被 SIGKILL 后换新进程)记下的 worker_pid 属于旧进程——
# 旧 pid 可能已被别的进程复用(kill -0 成功)或落到 root 进程(EPERM 也当"活"),拿它当"还活着"
# 的证据会把早已随旧进程消亡的 in-process runner 永久冻在 RUNNING(补岗/复活/回收三条路都不碰
# RUNNING → P2 真机实锤重启只 1/5 恢复)。判活先看心跳过期(宿主真死的权威信号),pid 只在
# 【同代】才作 GC 抖动豁免的二次保守判据。纯进程内常量,不落盘、不跨进程比较字面值。
PROCESS_EPOCH = f"{os.getpid()}-{os.urandom(6).hex()}"

# 后台派工子进程(base owner 的 durable dispatch subprocess)启动时置此环境变量,让子进程里
# 记录的 runner 会话如实标 in_process=False——epoch 换代回收【只适用 in-process runner】(它随
# 记录它的网关进程存亡);独立子进程 runner 不随网关重启死,仍走原 pid-liveness(保留 GC 抖动
# 豁免),不被换代误杀。in-process 线程跑在网关进程、无此环境变量,如实标 in_process=True。
RUNNER_SUBPROCESS_ENV = "MY_AGENT_RUNNER_SUBPROCESS"


def running_in_dispatch_subprocess() -> bool:
    """当前进程是否是后台派工子进程(而非网关主进程内的 in-process runner 线程)。"""
    return bool(os.environ.get(RUNNER_SUBPROCESS_ENV))


# LLM: 存活探测唯一入口(kill -0 形态,长期助手 _is_host_pid_alive 同款)。
#   先尝试非阻塞 reap:被信号终止的直接子进程在父进程 wait 前是 zombie,
#   kill -0 对 zombie 仍成功——出口回收恰好发生在父进程内(dispatch 进程是
#   主代理的直接子进程),不 reap 会把已死进程误判为活、白等宽限再误升 SIGKILL。
#   EPERM(进程存在但无权限)按存活处理;pid<=0 一律不存活。
# 函数用途: 判断一个落盘 pid 现在还有没有对应的活进程。
def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _reap_if_exited_child(pid):
        return False
    if _linux_process_is_zombie(pid):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# LLM: ``kill(pid, 0)`` reports a Linux zombie as present even though it can
# never execute another instruction.  Recovery may inspect a worker from a
# process other than its direct parent, where waitpid cannot reap it, so the
# kernel process state is the portable-enough Linux fallback.  Other platforms
# simply skip this check and retain the conservative kill-0 behavior.
# 函数用途：把 Linux 的 Z/X 进程态判为已死亡，避免审计子代理强杀后被误判成卡死。
def _linux_process_is_zombie(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        with open(
            f"/proc/{pid}/stat",
            encoding="ascii",
            errors="replace",
        ) as handle:
            stat = handle.read()
    except OSError:
        return False
    closing_paren = stat.rfind(")")
    if closing_paren < 0:
        return False
    tail = stat[closing_paren + 1 :].strip()
    state = tail.split(maxsplit=1)[0] if tail else ""
    return state in {"Z", "X"}


# LLM: 非阻塞回收:waitpid(WNOHANG) 对活着的子进程返回 (0,0) 无副作用,对已
#   退出未回收的子进程 reap 并返回 True;非自己子进程(跨进程回收场景)抛
#   ChildProcessError,按 False 处理交给 kill -0 判定。
# 函数用途: 自己拉起的子进程死了先收尸,别把僵尸当活人。
def _reap_if_exited_child(pid: int) -> bool:
    if not hasattr(os, "waitpid") or not hasattr(os, "WNOHANG"):
        return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return waited == pid


# LLM: 调用方须先证明该独立宿主没有应保留的活动兄弟；本入口会终止整棵后代树。
#   只将公共终止回执映射为既有取消/回收状态，未确认必须为 kill_sent，禁止因此重派。
#   无进程句柄不虚构退出码；OS 强制终止也不证明 native 历史已落盘。
# 函数用途: 停止已获准回收的子代理宿主及其命令，返回全树核对证据供上层收口。
def terminate_pid_with_escalation(
    pid: int,
    *,
    grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> dict[str, object]:
    if pid <= 0:
        return {"status": "no_pid"}
    if not is_pid_alive(pid):
        return {"status": "not_alive", "pid": pid}
    from ..tooling.process_registry import terminate_process_tree

    receipt = terminate_process_tree(pid, None, grace_seconds=grace_seconds)
    escalated = receipt.method in {"SIGTERM->SIGKILL", "taskkill/T/F", "popen.kill"}
    status = "killed" if escalated else "terminated"
    return {
        "status": status if receipt.confirmed else "kill_sent",
        "pid": pid,
        "escalated": escalated,
        "exited": receipt.confirmed,
        "termination": asdict(receipt),
    }


@dataclass(frozen=True)
class BackgroundStartUpdate:
    """background_start 状态更新的入参包(pid=0 表示沿用已落盘 pid)。"""

    launch_id: str
    status: str
    error: str = ""
    pid: int = 0
    replace_launch: bool = False


# LLM: background_start 记录构造的唯一权威(R7a 实锤:agent 侧 mark 与 CLI 侧
#   mark_background_launch 各自手写 dict,CLI 侧覆盖时抹掉 pid → 孤儿回收进程层
#   失效 terminated_processes=[])。契约:update.pid>0 显式写入;否则保留 previous
#   里已落盘的 pid——pid 是孤儿回收/cancel 的定位事实,任何状态更新不得抹掉。
#   两个调用方:orchestration/background/dispatch.mark_background_start、
#   cli/dispatch_background.mark_background_launch;改字段时同步两边测试。
# 函数用途: 所有想往任务上写 background_start 状态的人都从这里拿记录,保证
#   谁也不会顺手把 pid 弄丢。
def build_background_start_record(previous: object, update: BackgroundStartUpdate) -> dict[str, object]:
    import time as _time

    previous_record = dict(previous) if isinstance(previous, dict) else {}
    previous_launch_id = str(previous_record.get("launch_id") or "").strip()
    next_launch_id = str(update.launch_id or "").strip()
    if (
        previous_launch_id
        and next_launch_id
        and previous_launch_id != next_launch_id
        and not update.replace_launch
    ):
        # A late writer from an abandoned launch must not overwrite the
        # authoritative lifecycle/PID of its replacement.
        return previous_record
    record: dict[str, object] = {
        "launch_id": next_launch_id,
        "status": str(update.status or ""),
        "updated_at": _time.time(),
        "error": str(update.error or ""),
    }
    same_launch = not previous_launch_id or previous_launch_id == next_launch_id
    effective_pid = _safe_pid(update.pid)
    if effective_pid <= 0 and same_launch:
        effective_pid = _safe_pid(previous_record.get("pid"))
    if effective_pid > 0:
        record["pid"] = effective_pid
    return record


# LLM: Reclaiming a launch updates the same canonical background_start record;
# callers must not hand-edit status dictionaries and accidentally lose the pid.
# 函数用途: 将已失效 runner 的后台启动残留标成 reclaimed，令同一任务可以安全续派。
def reclaim_background_start(task: object) -> bool:
    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        return False
    previous = attrs.get("background_start")
    if not isinstance(previous, dict):
        return False
    if str(previous.get("status") or "").strip() not in {"launching", "running"}:
        return False
    attrs["background_start"] = build_background_start_record(
        previous,
        BackgroundStartUpdate(
            launch_id=str(previous.get("launch_id") or ""),
            status="reclaimed",
        ),
    )
    return True


# 函数用途: 把任意来源的 pid 值安全转成正整数,坏值一律按 0(无 pid)处理。
def _safe_pid(value: object) -> int:
    try:
        pid = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


__all__ = [
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "BackgroundStartUpdate",
    "build_background_start_record",
    "is_pid_alive",
    "reclaim_background_start",
    "terminate_pid_with_escalation",
]
