# LLM: 子代理后台进程治理原语的唯一权威位置(孤儿子代理回收专项,实锤来源
#   REFACTORING_BACKLOG"孤儿子代理回收":R6a 主代理退出后后台 dispatch 进程继续
#   写占位符 21 分钟)。契约:①只做进程层事实操作(存活探测/信号/等待),不碰任务
#   状态——任务状态变更留给调用方(cancel 工具、出口回收)按各自语义处理;②终止
#   手法对照 会话运行时(SIGTERM→宽限→SIGKILL 升级)与 长期助手(树形终止):POSIX 上优先
#   按进程组发信号(后台 dispatch 进程用 start_new_session=True 启动,pid==pgid,
#   killpg 可一并终止其 fork 的非新会话子进程),无 killpg 平台回退单 pid;③全部
#   函数不抛 OSError——进程不存在/无权限都收敛为结构化返回值,调用方零异常分支。
#   改动时同步检查 orchestration/tools/cancel.py。
#   两个调用方,以及 tests/test_subagent_process_control.py。
# 模块用途: 主代理需要"杀掉后台子代理进程"时统一走这里:cancel_subagents 工具、
#   run 出口的孤儿回收都复用同一套存活探测和两阶段终止,避免各处自造 os.kill 细节。
from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

# SIGTERM 后给进程的清理宽限。
DEFAULT_TERMINATE_GRACE_SECONDS = 2.0
# SIGKILL 后确认进程消失的短等待(SIGKILL 不可忽略,只等内核回收)。
_KILL_CONFIRM_SECONDS = 1.0

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
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


# LLM: 等待进程退出(轮询 is_pid_alive)。返回 True=已退出。不发任何信号。
# 函数用途: 发完信号后确认进程是不是真的消失了。
def wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return not is_pid_alive(pid)


# LLM: 两阶段终止唯一入口:SIGTERM(进程组优先)→ 宽限等待 → 仍存活则 SIGKILL
#   升级 → 短确认。返回结构化报告(status ∈ no_pid/not_alive/terminated/killed/
#   kill_sent;escalated 标记是否升级到 SIGKILL),绝不抛异常。副作用:向目标
#   进程(组)发送信号。grace_seconds<=0 表示不等宽限直接判定(仍会先发 SIGTERM)。
# 函数用途: 回收一个后台子代理进程:先礼貌请它退出,不走再强杀,最后报告结局。
def terminate_pid_with_escalation(
    pid: int,
    *,
    grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> dict[str, object]:
    if pid <= 0:
        return {"status": "no_pid"}
    if not is_pid_alive(pid):
        return {"status": "not_alive", "pid": pid}
    _signal_pid_tree(pid, signal.SIGTERM)
    if wait_for_pid_exit(pid, grace_seconds):
        return {"status": "terminated", "pid": pid, "escalated": False}
    _signal_pid_tree(pid, signal.SIGKILL)
    exited = wait_for_pid_exit(pid, _KILL_CONFIRM_SECONDS)
    return {
        "status": "killed" if exited else "kill_sent",
        "pid": pid,
        "escalated": True,
        "exited": exited,
    }


# LLM: 信号发送内部实现:POSIX 上先按进程组(killpg)——后台 dispatch 进程
#   start_new_session=True 使 pid==pgid;组发送失败(非组长/平台不支持)回退
#   单 pid。所有 OSError 吞掉(进程可能恰好退出,属正常竞态)。
# 函数用途: 把信号尽量发给整棵进程树,发不了组就只发给该进程自己。
def _signal_pid_tree(pid: int, signum: int) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, signum)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signum)
    except OSError:
        pass


@dataclass(frozen=True)
class BackgroundStartUpdate:
    """background_start 状态更新的入参包(pid=0 表示沿用已落盘 pid)。"""

    launch_id: str
    status: str
    error: str = ""
    pid: int = 0


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

    record: dict[str, object] = {
        "launch_id": str(update.launch_id or ""),
        "status": str(update.status or ""),
        "updated_at": _time.time(),
        "error": str(update.error or ""),
    }
    effective_pid = _safe_pid(update.pid) or _safe_pid(previous.get("pid") if isinstance(previous, dict) else 0)
    if effective_pid > 0:
        record["pid"] = effective_pid
    return record


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
    "terminate_pid_with_escalation",
    "wait_for_pid_exit",
]
