# LLM: 后台派工"活性"判据(P2 非阻塞出口门专项)。契约:给定一个子代理任务/run,
#   只读地判断它是否有【活着的后台派工】——background_start.pid 进程存活
#   (subagents.process_control.is_pid_alive,含 zombie reap),或进程内 dispatch 线程
#   仍注册在跑(gateway 隔离 owner 走 in-process 线程、无 pid,见 orchestration/
#   background/dispatch._start_inprocess_dispatch)。只做事实探测:不发信号、不改
#   任务状态、不写盘。三个消费方:final_exit_contract(干净 yield 判据)、
#   exit_orphan_recovery(live-pid 豁免)、completion(wake-capable 来源判定)。
#   改动时同步 tests/test_background_liveness.py。
# 模块用途: 回答"我派出去的子代理,现在是不是还有真在后台跑的进程/线程扛着",
#   让主代理能安全"派完撒手"(非阻塞 yield)而不误伤还在干活的后台帮手。
from __future__ import annotations

import threading
from pathlib import Path

from ...subagents.process_control import is_pid_alive
from ..delivery_closeout.subagent_aggregation import open_children_states

# LLM: wake-capable 来源 = 有持久会话 + 事件叫回能力(可安全"派完撒手",靠子代理
#   完成事件/提醒/用户下一句叫回)。cli_run 等单次来源不在此列——它们没有 wake,
#   必须当场收口/回收,不能非阻塞退出。与 completion 的 soft-wait 短路共用同一份
#   权威名单(避免三处新行为各自漂移)。
WAKE_CAPABLE_SOURCES = frozenset({"background_main_agent", "chat", "gateway"})


# 函数用途: 判断本轮 run 来源是否 wake-capable(可非阻塞撒手,靠事件叫回)。
def is_wake_capable_source(params) -> bool:
    return str(getattr(params, "source", "") or "").strip() in WAKE_CAPABLE_SOURCES


# 函数用途: 从任务对象 / canonical dict 读后台进程 pid,没有或坏值一律返回 0。
def task_background_pid(task) -> int:
    background = _task_attributes(task).get("background_start")
    if not isinstance(background, dict):
        return 0
    try:
        pid = int(background.get("pid") or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


# LLM: 单个子代理的后台活性(Step1 判据)。两条独立信号,任一为真即 live:
#   ① background_start.pid 进程存活;② 进程内 dispatch 线程仍注册且线程对象存活
#   (echo / 隔离 owner 的 in-process 派工无 pid,只能靠线程判活)。agent 缺省 None
#   时只查 pid(纯 pid 场景,无需线程注册表)。
# 函数用途: 判断一个子代理任务是否还有活着的后台派工(进程存活或进程内线程在跑)。
def is_task_background_live(task, agent=None) -> bool:
    pid = task_background_pid(task)
    if pid > 0 and is_pid_alive(pid):
        return True
    run_id = _task_run_id(task)
    if agent is None or not run_id:
        return False
    return _has_live_inprocess_dispatch_thread(agent, run_id)


# LLM: 干净 yield 的核心判据(final_exit_contract Step2 / orphan 豁免复用):当且仅当
#   第一层存在未收口子代理(与 open_task_state_summary 的 open_children 同一判据),
#   且它们【全部】都有活着的后台派工时为真。任一未收口子代理不 live(死 pid / 无记录 /
#   失败僵尸)即为假——这样"还有真僵尸要处置"的场景照旧走验收门,不被误 yield。
# 函数用途: 判断"这轮 open 的子代理是不是全都还在后台好好跑着",是则可安全撒手。
def open_children_all_background_live(agent, task_root: Path | None) -> bool:
    open_children = open_children_states(task_root)
    if not open_children:
        return False
    manager = getattr(agent, "subagents", None)
    return all(_child_is_background_live(agent, manager, child) for child in open_children)


# 函数用途: 取单个 open 子代理的权威记录(pid 落在任务 attributes,projection 常缺)后判活。
def _child_is_background_live(agent, manager, child: dict) -> bool:
    task = _authoritative_task(manager, str(child.get("run_id") or "")) or child
    return is_task_background_live(task, agent)


# 函数用途: 优先用 manager 权威任务(pid 的权威事实源);缺 manager / 加载失败回退 None。
def _authoritative_task(manager, run_id: str):
    if manager is None or not run_id:
        return None
    try:
        return manager.load(run_id)
    except Exception:
        return None


# 函数用途: 进程内 dispatch 线程活性:注册表里某 launch 含此 run_id 且其线程对象仍存活。
def _has_live_inprocess_dispatch_thread(agent, run_id: str) -> bool:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return False
    live_names = {thread.name for thread in threading.enumerate() if thread.is_alive()}
    return any(_entry_has_live_thread(entry, run_id, live_names) for entry in registry.values())


# 函数用途: 单条派工登记是否命中该 run_id 且其线程仍存活。
def _entry_has_live_thread(entry, run_id: str, live_names: set[str]) -> bool:
    if not isinstance(entry, dict) or run_id not in (entry.get("run_ids") or []):
        return False
    thread_name = str(entry.get("thread_name") or "")
    return bool(thread_name) and thread_name in live_names


# 函数用途: 兼容任务对象与 canonical dict 两种形态,取 attributes(非 dict 归一空 dict)。
def _task_attributes(task) -> dict:
    attrs = getattr(task, "attributes", None)
    if attrs is None and isinstance(task, dict):
        attrs = task.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


# 函数用途: 兼容任务对象与 canonical dict 两种形态,取 run_id(id 优先,再 run_id)。
def _task_run_id(task) -> str:
    for key in ("id", "run_id"):
        value = getattr(task, key, None)
        if value is None and isinstance(task, dict):
            value = task.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = [
    "WAKE_CAPABLE_SOURCES",
    "is_task_background_live",
    "is_wake_capable_source",
    "open_children_all_background_live",
    "task_background_pid",
]
