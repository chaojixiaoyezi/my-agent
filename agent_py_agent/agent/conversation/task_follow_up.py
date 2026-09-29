# LLM: "这个会话任务还有没有后续工作"的唯一判定入口，只读结构化事实：活跃 Goal、指向该任务的待处理 guidance、
#   未终态子代理、指向该任务的待处理唤醒（不含定时触发自身）、尚未发出完成通知的受管后台命令、启用的进度策略。
#   存在的事实放进 present；某一项读不出来放进 unreadable（项目名 + 错误码），调用方按"读不到"处理，不能当成没有。
#   唤醒、进度策略、后台命令三项读的是 owner 全量数据，读错误先按记录归属限定：坏记录能解析出属于别的任务就不计入，
#   避免别的会话一个坏文件让所有定时收口都读不到；解析不出归属的仍计入。
#   不读回复正文、任务目标或模型话术。调用方（scheduler/active_run_closeout）凭返回值决定进 waiting 还是结算。
#   每一项事实最终靠什么机制消失（否则就是新的死锁入口，be 复审 S4）：
#   - open_subagents：子代理 runner 结束发完成唤醒；静默死亡的由孤儿巡查（orphan_supervision_interval_seconds）兜底；
#   - unsettled_subagent_completion：子代理已终态、父级完成唤醒还没处理完——收口 WAL 由恢复链补交付，
#     待处理的完成唤醒由后台调度器消费；“刚终态、唤醒还在发布途中”只在 recent_seconds 窗口内计入；
#   - pending_wakes：后台调度器消费待处理唤醒（反复失败的毒丸由唤醒毒丸结案兜底）；
#   - pending_process_completion：受管后台命令退出后发完成通知；一直运行的服务按设计持续等待；
#   - active_goal、pending_guidance、enabled_progress_policy：没有机制保证会消失（健康的 Goal 在两片之间总有一条待处理的
#     续跑唤醒——那一项才是会自己推进的事实；Goal 因结果未知收口后仍是 active、
#     没有活动回合时 guidance 不会变成唤醒、进度唤醒跑完策略仍启用），只算“宽限期内”的后续工作（GRACE_BOUND_FACTS），
#     过了宽限期由调用方忽略。
#   改动时联查 scheduler/active_run_closeout.py、conversation/task_promotion 与 test_scheduler_waiting_deadlock。
# 模块用途: 判断一个会话任务在本轮结束后是否还有会自行推进的后续工作，决定定时执行该等待还是该结算。
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

FOLLOW_UP_ACTIVE_GOAL = "active_goal"
FOLLOW_UP_PENDING_GUIDANCE = "pending_guidance"
FOLLOW_UP_OPEN_SUBAGENTS = "open_subagents"
FOLLOW_UP_PENDING_WAKES = "pending_wakes"
FOLLOW_UP_PENDING_PROCESS = "pending_process_completion"
FOLLOW_UP_ENABLED_POLICY = "enabled_progress_policy"
FOLLOW_UP_UNSETTLED_SUBAGENT = "unsettled_subagent_completion"
# 没有机制保证会自己消失的事实：只在宽限期内算后续工作，过了宽限期调用方不再计入（见模块头注释）。
GRACE_BOUND_FACTS = frozenset({FOLLOW_UP_ACTIVE_GOAL, FOLLOW_UP_PENDING_GUIDANCE, FOLLOW_UP_ENABLED_POLICY})
# 没有会话存储或任务 id 时整体读不出，按这一项记。
FOLLOW_UP_TASK_UNAVAILABLE = "task_unavailable"
# 定时触发本身不是后续工作：它只是这一轮的入口，处理完就会被确认。
_SCHEDULER_TRIGGER_REASON = "scheduled_job_due"
# 子代理完成唤醒的去重键前缀（subagents/runner_completion_wake）：subagent-finished:<run_id>:<status>[:<attempt>]。
_SUBAGENT_FINISHED_KEY_PREFIX = "subagent-finished:"
_CLOSEOUT_DELIVERED = "delivered"


# LLM: present 是确认存在的事实码（固定顺序、去重）；unreadable 是读不出来的项目，每项为 (项目名, 错误码)，
#   错误码取自结构化错误报告的 category:error_type，不含路径或正文。两者都为空才表示确认没有后续工作。
# 类用途: 一次后续工作判定的结果。
@dataclass(frozen=True)
class FollowUpFacts:
    present: tuple[str, ...] = ()
    unreadable: tuple[tuple[str, str], ...] = ()


# LLM: thread_id/task_id 定位会话任务；ignore_wake_ids 是调用方正在处理的唤醒；now 为判定时刻（None 取当前时间），
#   只用于“子代理刚终态”的窗口判断。
# 类用途: 一次后续工作判定的输入。
@dataclass(frozen=True)
class FollowUpQuery:
    thread_id: str
    task_id: str
    ignore_wake_ids: tuple[str, ...] = ()
    now: float | None = None


# LLM: 读到但无法归属的错误原样抛出，由 task_follow_up_facts 记为该项读不出；只携带结构化错误码。
# 类用途: 某一项后续工作事实读不出来。
class _Unreadable(Exception):
    # 函数用途: 记下读不出来的那一项的结构化错误码。
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# LLM: 逐项检查；某一项读取失败只记这一项，其余项照常检查。recent_seconds 是“子代理刚终态、完成唤醒可能还在
#   发布途中”的窗口（组合根按调度宽限期绑定），0 表示不看这个窗口。
# 函数用途: 列出一个会话任务当前仍存在的后续工作事实，以及读不出来的项目。
def task_follow_up_facts(agent: object, query: FollowUpQuery, *, recent_seconds: float = 0.0) -> FollowUpFacts:
    store = getattr(agent, "conversation_store", None)
    selected = str(query.task_id or "").strip()
    if store is None or not selected:
        return FollowUpFacts(unreadable=((FOLLOW_UP_TASK_UNAVAILABLE, "state:missing_store_or_task"),))
    ignored = frozenset(str(item) for item in query.ignore_wake_ids if str(item or "").strip())
    recent_since = float(time.time() if query.now is None else query.now) - max(0.0, float(recent_seconds))
    checks: tuple[tuple[str, Callable[[], bool]], ...] = (
        (FOLLOW_UP_ACTIVE_GOAL, lambda: _has_active_goal(store, query.thread_id, selected)),
        (FOLLOW_UP_PENDING_GUIDANCE, lambda: bool(store.guidance.pending("task", selected, limit=1))),
        (FOLLOW_UP_OPEN_SUBAGENTS, lambda: _has_open_subagents(agent, selected)),
        (FOLLOW_UP_UNSETTLED_SUBAGENT, lambda: _has_unsettled_subagent(agent, selected, recent_since)),
        (FOLLOW_UP_PENDING_WAKES, lambda: _has_pending_wakes(agent, selected, ignored)),
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
#   这样调用方能记下"读不到"、并受限期约束。血缘里的记录按 id 精确读，缺失或损坏都会抛出。
# 函数用途: 判断任务是否还有未终态的子代理。
def _has_open_subagents(agent: object, task_id: str) -> bool:
    from .task_promotion import conversation_task_open_subagents_or_raise

    return conversation_task_open_subagents_or_raise(agent, task_id)


# LLM: 只看本任务血缘里（按 id 精确读，缺失或损坏抛出）、直属会话（父级不是已持久化的子代理，与完成通知器同一判据）、
#   状态会发完成唤醒（SUBAGENT_WAKE_STATUSES）的已终态子代理。
#   任一满足即算：收口 WAL 仍在且还没交付；有去重键为 subagent-finished:<run_id>: 前缀的待处理唤醒（它的 root_task_id
#   是子代理树的根，不一定是本任务，所以要按键找）；终止时刻（ended_at，缺失时用 updated_at）不早于 recent_since。
#   终态状态先落盘、WAL 后写，“刚终态”窗口盖住这段发布间隙。读失败抛出，由调用方记为读不出。
# 函数用途: 判断任务是否有已结束、但完成结果还没交回父级的子代理。
def _has_unsettled_subagent(agent: object, task_id: str, recent_since: float) -> bool:
    from ..subagents.models import SUBAGENT_WAKE_STATUSES, task_status_in
    from ..subagents.runner_completion_wake import has_persisted_subagent_parent
    from .task_promotion import conversation_task_lineage_runs_or_raise

    finished = [run for run in conversation_task_lineage_runs_or_raise(agent, task_id)
                if task_status_in(getattr(run, "status", ""), SUBAGENT_WAKE_STATUSES)
                and not has_persisted_subagent_parent(agent.subagents.load, run)]
    if not finished:
        return False
    pending_keys = _pending_dedupe_keys(agent.conversation_store)
    return any(_completion_unsettled(run, pending_keys, recent_since) for run in finished)


# LLM: 三个条件见 _has_unsettled_subagent；只读子代理记录的结构化属性与待处理唤醒的去重键。
# 函数用途: 判断一个已终态子代理的完成结果是否还没交回父级。
def _completion_unsettled(run: object, pending_keys: frozenset[str], recent_since: float) -> bool:
    from ..subagents.services.runtime_closeout import pending_closeout

    fact = pending_closeout(run)
    if fact is not None and str(fact.get("delivery") or "") != _CLOSEOUT_DELIVERED:
        return True
    prefix = f"{_SUBAGENT_FINISHED_KEY_PREFIX}{getattr(run, 'id', '')}:"
    if any(key.startswith(prefix) for key in pending_keys):
        return True
    ended = float(getattr(run, "ended_at", 0.0) or getattr(run, "updated_at", 0.0) or 0.0)
    return ended >= recent_since


# LLM: 待处理唤醒的去重键集合；读错误不在这里判定（_has_pending_wakes 负责按归属限定并报告）。
# 函数用途: 读取当前所有待处理唤醒的去重键。
def _pending_dedupe_keys(store: object) -> frozenset[str]:
    signals, _errors = store.wakes.pending_report(limit=0)
    return frozenset(str(getattr(signal, "dedupe_key", "") or "") for signal in signals)


# LLM: 读取全部待处理唤醒；读到的记录里已有匹配就直接算存在，同类坏记录不能遮住确认存在的后续工作；没有匹配时，
#   读错误里能解析出 root_task_id 且不是本任务的不计入，其余抛 _Unreadable。例外：去重键以 subagent-finished:<本任务血缘
#   子代理 id>: 开头的坏唤醒（它的 root_task_id 是子代理树的根）仍按可能属于本任务处理。排除调用方正在处理的唤醒和定时触发本身。
# 函数用途: 判断是否还有指向该任务、尚未处理的唤醒（子代理生命周期、后台命令完成、Goal 续跑等）。
def _has_pending_wakes(agent: object, task_id: str, ignored: frozenset[str]) -> bool:
    signals, errors = agent.conversation_store.wakes.pending_report(limit=0)
    if any(
        str(getattr(signal, "root_task_id", "") or "").strip() == task_id
        and str(getattr(signal, "wake_signal_id", "") or "") not in ignored
        and str(getattr(signal, "reason", "") or "").strip().lower() != _SCHEDULER_TRIGGER_REASON
        for signal in signals
    ):
        return True
    if errors:
        lineage = frozenset(agent.subagent_run_ids_for_request(task_id))
        _raise_unscoped(errors, lambda payload: _owned_elsewhere(payload, "root_task_id", task_id)
                        and not _finished_wake_of(payload, lineage))
    return False


# LLM: 只看去重键：subagent-finished:<run_id>:… 且 run_id 在本任务血缘里，才算本任务子代理的完成唤醒。
# 函数用途: 判断一条读不出的唤醒是不是本任务血缘里某个子代理的完成唤醒。
def _finished_wake_of(payload: dict[str, object], lineage: frozenset[str]) -> bool:
    key = payload.get("dedupe_key")
    if not isinstance(key, str) or not key.startswith(_SUBAGENT_FINISHED_KEY_PREFIX):
        return False
    return key[len(_SUBAGENT_FINISHED_KEY_PREFIX):].split(":", 1)[0] in lineage


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


# LLM: _Unreadable 已带错误码；带结构化 report 的异常（进程权威、子代理血缘）取 report 的 category:error_type；
#   其它异常经 runtime_error_report 取 category，加异常类名，不读消息。
# 函数用途: 为一次读取失败生成结构化错误码。
def _error_code(exc: Exception) -> str:
    if isinstance(exc, _Unreadable):
        return exc.code
    report = getattr(exc, "report", None)
    if isinstance(report, dict) and report.get("category"):
        return _report_code(report)
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
    "FOLLOW_UP_UNSETTLED_SUBAGENT",
    "GRACE_BOUND_FACTS",
    "FollowUpFacts",
    "FollowUpQuery",
    "task_follow_up_facts",
]
