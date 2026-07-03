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
from ...subagents.runner_session_liveness import has_fresh_runner_session
from ..delivery_closeout.subagent_aggregation import open_children_states

# LLM: wake-capable 来源 = 有持久会话 + 事件叫回能力(可安全"派完撒手",靠子代理
#   完成事件/提醒/用户下一句叫回)。cli_run 等单次来源不在此列——它们没有 wake,
#   必须当场收口/回收,不能非阻塞退出。与 completion 的 soft-wait 短路共用同一份
#   权威名单(避免三处新行为各自漂移)。
WAKE_CAPABLE_SOURCES = frozenset({"background_main_agent", "chat", "gateway"})


# 函数用途: 判断本轮 run 来源是否 wake-capable(可非阻塞撒手,靠事件叫回)。
def is_wake_capable_source(params) -> bool:
    return str(getattr(params, "source", "") or "").strip() in WAKE_CAPABLE_SOURCES


# LLM: 用户交互轮"开着子代理也放行"判据(P2 非阻塞的用户侧)。命中 = 本轮是用户发起的
#   交互(gateway/chat 的聊天/查进度)、任务里有上一轮派出的 open 子代理、本轮自己没派活、
#   也不是 background 自发叫回轮 → 本轮不为"子代理未收口"背锅,原文放行(不 rework/不 closeout)。
#   final_exit_contract(tool loop 出口)与 _finalization_service(出口之后的收尾)两层共用这
#   一份判据——曾因 finalization 层没有此解耦、把"派完之后的查进度轮"在 tool loop 放行后又
#   重跑 closeout 返工(SUBAGENTS_UNFINISHED),两层策略必须同源、不许漂移。open_summary 由
#   调用方给(open_task_state_summary 的结果,避免此处重复扫盘)。
# 函数用途: 判断"这轮该不该因为有 open 子代理就被交付门返工"——用户交互轮答否。
def user_interaction_open_children_passthrough(params, open_summary) -> bool:
    if not is_wake_capable_source(params):
        return False
    if int(open_summary.get("open_capability_requests") or 0) > 0:
        return False
    if int(open_summary.get("children_total") or 0) <= 0:
        return False
    # 本轮自己派了活(create_subagents)→ 走"派完撒手带声明"分支,不归这里。
    if "create_subagents" in (getattr(params, "executed_tools", None) or []):
        return False
    # background 自发叫回轮(子代理完成事件/定时唤醒)→ 就是来整合交付子代理成果的,照常走门。
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        return False
    return True


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


# LLM: 单个子代理的后台活性(Step1 判据)。三条独立信号,任一为真即 live:
#   ① background_start.pid 进程存活;② 进程内 dispatch 线程仍注册且线程对象存活
#   (echo / 隔离 owner 的 in-process 派工无 pid,只能靠线程判活);③ runner 会话
#   心跳新鲜(runner_session_liveness,跨 agent 实例/跨进程的耐久事实——真机实锤:
#   gateway 唤醒轮跑在后台线程私有池的另一实例上,②的注册表恒为空,活着的编队
#   子代理被出口回收整批误判为死;心跳每 ~5s 写进任务权威 store,谁读都成立)。
# 函数用途: 判断一个子代理任务是否还有活着的后台派工(进程存活/线程在跑/心跳在跳)。
def is_task_background_live(task, agent=None) -> bool:
    pid = task_background_pid(task)
    if pid > 0 and is_pid_alive(pid):
        return True
    if has_fresh_runner_session(task):
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


# LLM: 叫回轮"整合时机"判据(§8-2 dispatch 整合churn专项):open 子代理全部「活着 或
#   在续派轨道上(可派孤儿,机制层 supervision/auto_start 会拉起)」→ 编队还没到齐,
#   本轮不是整合时机。任何一个救不回(不可派/尝试爆表/等父裁能力申请/状态读不出)
#   → False,照常走门让模型裁决 takeover/cancel。空 open 集恒 False(编队到齐就该整合,
#   不许 all() 空集真值误 yield)。
# 函数用途: 判断"叫回轮现在整合是不是太早"——编队全员活着/在复活中时答是。
def open_children_all_live_or_reviving(agent, task_root: Path | None) -> bool:
    open_children = open_children_states(task_root)
    if not open_children:
        return False
    manager = getattr(agent, "subagents", None)
    return all(
        _child_is_background_live(agent, manager, child) or _child_is_reviving(manager, child)
        for child in open_children
    )


# 复活轨道的尝试上限,与 capability_auto_sweep._ORPHAN_REVIVE_ATTEMPT_CAP 同值同义
# (机制层只肯复活 4 次以内的孤儿;超限的不算"在续派轨道上")。改动时两处同步。
_REVIVING_ATTEMPT_CAP = 4


# 函数用途: 单个 open 子代理是否"在续派轨道上"(可派孤儿:PLANNING/PENDING、尝试
#   未爆表、无父裁 OPEN 能力申请/缺口)。判据与机制层复活同口径,全结构化。
def _child_is_reviving(manager, child: dict) -> bool:
    from ...contracts.state_machine import DISPATCHABLE_STATES, normalize_status
    from ...subagents.model_capabilities import capability_request_requires_parent_resolution

    task = _authoritative_task(manager, str(child.get("run_id") or ""))
    if task is None:
        return False
    if normalize_status(str(getattr(task, "status", "") or "")) not in DISPATCHABLE_STATES:
        return False
    if int(getattr(task, "runner_attempts", 0) or 0) >= _REVIVING_ATTEMPT_CAP:
        return False
    if any(
        capability_request_requires_parent_resolution(getattr(item, "status", "OPEN"))
        for item in getattr(task, "capability_requests", []) or []
    ):
        return False
    return not any(
        str(getattr(item, "status", "") or "") == "OPEN"
        for item in getattr(task, "capability_gaps", []) or []
    )


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
    "open_children_all_live_or_reviving",
    "task_background_pid",
    "user_interaction_open_children_passthrough",
]
