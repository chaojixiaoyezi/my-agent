# LLM: "这个会话任务还有没有后续工作"的唯一判定入口，只读结构化事实：活跃 Goal、指向该任务的待处理 guidance、
#   未终态子代理、指向该任务的待处理唤醒（不含定时触发自身）、尚未发出完成通知的受管后台命令、启用的进度策略。
#   存在的事实放进 present；某一项读不出来放进 unreadable（项目名 + 错误码），调用方按"读不到"处理，不能当成没有。
#   唤醒、进度策略、后台命令三项读的是 owner 全量数据，读错误先按记录归属限定：坏记录能解析出属于别的任务就不计入，
#   避免别的会话一个坏文件让所有定时收口都读不到；解析不出归属的仍计入。
#   不读回复正文、任务目标或模型话术。调用方（scheduler/active_run_closeout）凭返回值决定进 waiting 还是结算。
#   改动时联查 scheduler/active_run_closeout.py、conversation/task_promotion 与 test_scheduler_waiting_deadlock。
# 模块用途: 判断一个会话任务在本轮结束后是否还有会自行推进的后续工作，决定定时执行该等待还是该结算。
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

FOLLOW_UP_ACTIVE_GOAL = "active_goal"
FOLLOW_UP_PENDING_GUIDANCE = "pending_guidance"
FOLLOW_UP_OPEN_SUBAGENTS = "open_subagents"
FOLLOW_UP_PENDING_WAKES = "pending_wakes"
FOLLOW_UP_PENDING_PROCESS = "pending_process_completion"
FOLLOW_UP_ENABLED_POLICY = "enabled_progress_policy"
# 没有会话存储或任务 id 时整体读不出，按这一项记。
FOLLOW_UP_TASK_UNAVAILABLE = "task_unavailable"
# 定时触发本身不是后续工作：它只是这一轮的入口，处理完就会被确认。
_SCHEDULER_TRIGGER_REASON = "scheduled_job_due"


# LLM: present 是确认存在的事实码（固定顺序、去重）；unreadable 是读不出来的项目，每项为 (项目名, 错误码)，
#   错误码取自结构化错误报告的 category:error_type，不含路径或正文。两者都为空才表示确认没有后续工作。
# 类用途: 一次后续工作判定的结果。
@dataclass(frozen=True)
class FollowUpFacts:
    present: tuple[str, ...] = ()
    unreadable: tuple[tuple[str, str], ...] = ()


# LLM: 读到但无法归属的错误原样抛出，由 task_follow_up_facts 记为该项读不出；只携带结构化错误码。
# 类用途: 某一项后续工作事实读不出来。
class _Unreadable(Exception):
    # 函数用途: 记下读不出来的那一项的结构化错误码。
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# LLM: 逐项检查；ignore_wake_ids 用来排除调用方正在处理的唤醒自身。某一项读取失败只记这一项，其余项照常检查。
# 函数用途: 列出一个会话任务当前仍存在的后续工作事实，以及读不出来的项目。
def task_follow_up_facts(
    agent: object, thread_id: str, task_id: str, ignore_wake_ids: Iterable[str] = (),
) -> FollowUpFacts:
    store = getattr(agent, "conversation_store", None)
    selected = str(task_id or "").strip()
    if store is None or not selected:
        return FollowUpFacts(unreadable=((FOLLOW_UP_TASK_UNAVAILABLE, "state:missing_store_or_task"),))
    ignored = frozenset(str(item) for item in ignore_wake_ids if str(item or "").strip())
    checks: tuple[tuple[str, Callable[[], bool]], ...] = (
        (FOLLOW_UP_ACTIVE_GOAL, lambda: _has_active_goal(store, thread_id, selected)),
        (FOLLOW_UP_PENDING_GUIDANCE, lambda: bool(store.guidance.pending("task", selected, limit=1))),
        (FOLLOW_UP_OPEN_SUBAGENTS, lambda: _has_open_subagents(agent, selected)),
        (FOLLOW_UP_PENDING_WAKES, lambda: _has_pending_wakes(store, selected, ignored)),
        (FOLLOW_UP_PENDING_PROCESS, lambda: _has_pending_process(agent, selected)),
        (FOLLOW_UP_ENABLED_POLICY, lambda: _has_enabled_policy(store, selected)),
    )
    present: list[str] = []
    unreadable: list[tuple[str, str]] = []
    for item, check in checks:
        found, error_code = _run_check(check)
        if error_code:
            unreadable.append((item, error_code))
        if found:
            present.append(item)
    return FollowUpFacts(tuple(present), tuple(unreadable))


# LLM: 返回 (是否存在, 错误码)；读取失败时错误码非空、是否存在为 False，调用方必须把它记为读不出，不能当成没有。
# 函数用途: 执行一项后续工作检查并把异常收成结构化错误码。
def _run_check(check: Callable[[], bool]) -> tuple[bool, str]:
    try:
        return bool(check()), ""
    except Exception as exc:  # noqa: BLE001 - 读不到就记这一项，不能当成没有后续工作
        return False, _error_code(exc)


# LLM: 只有 active 的 Goal 会自己产生续跑唤醒；暂停、受阻、额度或预算受限的 Goal 需要人来推动，不算后续工作。
# 函数用途: 判断任务是否挂着一个活跃的持续目标。
def _has_active_goal(store: object, thread_id: str, task_id: str) -> bool:
    goal = store.goals.load(thread_id, task_id=task_id)
    return goal is not None and str(getattr(goal, "status", "") or "") == "active"


# LLM: 复用会话任务收尾的同一条判据（canonical 子代理状态 + 精确请求血缘），但读取失败抛出而不是当成有子代理，
#   这样调用方能记下"读不到"。子代理列表按记录读、坏记录本来就被跳过，只有整体读取失败才会抛。
# 函数用途: 判断任务是否还有未终态的子代理。
def _has_open_subagents(agent: object, task_id: str) -> bool:
    from .task_promotion import conversation_task_open_subagents_or_raise

    return conversation_task_open_subagents_or_raise(agent, task_id)


# LLM: 读取全部待处理唤醒；读到的记录里已有匹配就直接算存在，同类坏记录不能遮住确认存在的后续工作；没有匹配时，
#   读错误里能解析出 root_task_id 且不是本任务的不计入，其余抛 _Unreadable。排除调用方正在处理的唤醒和定时触发本身。
# 函数用途: 判断是否还有指向该任务、尚未处理的唤醒（子代理生命周期、后台命令完成、Goal 续跑等）。
def _has_pending_wakes(store: object, task_id: str, ignored: frozenset[str]) -> bool:
    signals, errors = store.wakes.pending_report(limit=0)
    if any(
        str(getattr(signal, "root_task_id", "") or "").strip() == task_id
        and str(getattr(signal, "wake_signal_id", "") or "") not in ignored
        and str(getattr(signal, "reason", "") or "").strip().lower() != _SCHEDULER_TRIGGER_REASON
        for signal in signals
    ):
        return True
    _raise_unscoped(errors, lambda payload: _owned_elsewhere(payload, "root_task_id", task_id))
    return False


# LLM: 后台命令的完成义务由 process_events 的权威记录判定（读错误已在那里按归属限定）；仍读不出时它抛出。
# 函数用途: 判断任务是否还有尚未发出完成通知的受管后台命令。
def _has_pending_process(agent: object, task_id: str) -> bool:
    from .process_events import task_has_pending_process_completion

    return task_has_pending_process_completion(agent, task_id)


# LLM: 只看 enabled 的进度策略；已有匹配直接算存在（同类坏记录不遮住它）；没有匹配时，读错误里能解析出 task_id 且
#   不是本任务的不计入，其余抛 _Unreadable。
# 函数用途: 判断任务是否还有启用中的进度策略会定期唤醒它。
def _has_enabled_policy(store: object, task_id: str) -> bool:
    policies, errors = store.progress.list_report(enabled_only=True)
    if any(str(getattr(policy, "task_id", "") or "").strip() == task_id for policy in policies):
        return True
    _raise_unscoped(errors, lambda payload: _owned_elsewhere(payload, "task_id", task_id))
    return False


# LLM: 逐条读错误报告里的 path 指向的那个文件并解析 JSON（只读）；owned_elsewhere 判为别的任务的跳过，
#   剩下的第一条按其结构化错误码抛 _Unreadable。
# 函数用途: 把 owner 全量读取的错误限定到本任务。
def _raise_unscoped(errors: list[dict[str, object]], owned_elsewhere: Callable[[dict[str, object]], bool]) -> None:
    for error in errors:
        payload = _peek_json(error.get("path") if isinstance(error, dict) else None)
        if payload is None or not owned_elsewhere(payload):
            raise _Unreadable(_report_code(error))


# LLM: 字段是字符串且不等于本任务，才算确定属于别处；字段缺失或类型不对都按可能属于本任务处理。
# 函数用途: 判断一条读不出的记录是否确定属于别的任务。
def _owned_elsewhere(payload: dict[str, object], key: str, task_id: str) -> bool:
    value = payload.get(key)
    return isinstance(value, str) and value.strip() != task_id


# LLM: 只读一个文件；读不出或不是 JSON 对象返回 None（无法归属）。
# 函数用途: 尽力解析一条坏记录，只为判断它属于哪个任务。
def _peek_json(path: object) -> dict[str, object] | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# LLM: 错误码取结构化报告的 category:error_type，不带路径与消息。
# 函数用途: 把一条读取错误报告压成结构化错误码。
def _report_code(error: object) -> str:
    report = error if isinstance(error, dict) else {}
    return f"{report.get('category') or 'unknown'}:{report.get('error_type') or 'unknown'}"


# LLM: _Unreadable 已带错误码；其它异常经 runtime_error_report 取 category，加异常类名，不读消息。
# 函数用途: 为一次读取失败生成结构化错误码。
def _error_code(exc: Exception) -> str:
    if isinstance(exc, _Unreadable):
        return exc.code
    from ..runtime_errors import runtime_error_report

    return f"{runtime_error_report(exc).get('category') or 'unknown'}:{type(exc).__name__}"


__all__ = [
    "FOLLOW_UP_ACTIVE_GOAL",
    "FOLLOW_UP_ENABLED_POLICY",
    "FOLLOW_UP_OPEN_SUBAGENTS",
    "FOLLOW_UP_PENDING_GUIDANCE",
    "FOLLOW_UP_PENDING_PROCESS",
    "FOLLOW_UP_PENDING_WAKES",
    "FOLLOW_UP_TASK_UNAVAILABLE",
    "FollowUpFacts",
    "task_follow_up_facts",
]
