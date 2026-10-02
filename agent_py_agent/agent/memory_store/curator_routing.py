from __future__ import annotations

"""记忆整理按“消息来源会话的主代理模型”分组的纯函数。"""

# LLM: 只做分组、挑组、按组投影游标和汇总多组结果，不读写任何账本、不建后端、不联网。路由表由组合根按
#   ConversationThread.model_profile_id（结构化选择事实）只读解析后注入，本模块不 import settings/backends。
#   每组是一次完整的 Curator 运行（租约、提取、校验、一次事务提交都沿原合同）；改动须同步
#   memory_store/curator.py 的 _execute/_run_once 与 test_curator_thread_model_routing.py。
# 模块用途: 给后台记忆整理挑出这一次要处理的会话组和它的模型，并让熔断只看同一组的历史。

from collections.abc import Callable
from dataclasses import dataclass, replace

from .curator_inputs import CuratorInputBatch
from .curator_models import CuratorModelRoute, CuratorModelRouting, CuratorRunResult

# 一次触发里最多连续处理几组模型：每组是一次完整运行（至少一次模型调用），会话很多时也不让一次触发跑太久，
# 没处理到的组照常留到下一次触发。
CURATOR_MODEL_GROUP_MAX_COUNT = 8
# 会话自己的模型在提取/校验阶段确定性失败时，用 owner 默认模型补跑的失败码（输出不是合规 JSON、证据校验不过）。
# 网络、额度、超时、提交失败都不补跑：那些换模型没用或可能已经写过东西。
CURATOR_DEFAULT_FALLBACK_CODES = frozenset({"CURATOR_SCHEMA_INVALID"})
# 会话自己的模型连不上（服务商故障、额度、超时）时不在同一次运行里补跑：一次运行的租约只够一整次提取，超时类失败已经
# 用掉了它（3a 2026-10-02 生产：某会话选的模型连续 ProviderTransientError，那组排在最前，整个 owner 的整理都卡住）。
# 同一组同一起始游标上连续连接类失败几次后，下一次运行这组直接用 owner 默认模型：2 次足以排除一次抖动。
CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT = 2
# 本组最近一次推进游标的是“连不上让给默认”的补跑时，下一批只给会话自己的模型 1 次机会：失败 1 次就直接换默认，
# 会话模型仍被定期重试，又不会每批白白失败两次；会话模型自己成功推进后回到上面的 2 次（3a 2026-10-02 生产后续）。
CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT = 1
CURATOR_TRANSIENT_FALLBACK_CODES = frozenset({"CURATOR_MODEL_FAILED", "CURATOR_MODEL_TIMEOUT"})


# LLM: 不可变；route 是本次用的那组模型，batch 只含这组会话的消息（工具审计事件只在默认组带上），
#   thread_ids 是这组会话（按批内出现顺序），remaining_groups 是本批里还没处理的组数（含只剩审计事件的默认组）。
# 类用途: 一次运行挑出来的那组输入和模型。
@dataclass(frozen=True)
class RoutedBatch:
    route: CuratorModelRoute
    batch: CuratorInputBatch
    thread_ids: tuple[str, ...]
    remaining_groups: int
    include_audit: bool
    warnings: tuple[str, ...] = ()


# LLM: 组的顺序确定：批内第一条消息（按会话最近更新时间从早到晚收集）所在的组先跑；只有工具审计事件时跟默认组走。
#   一直失败的组只要还排在最前，全局起始游标就不变，原“同游标连续 3 次”熔断照常生效。
#   用了默认模型兜底的会话按原因计数，写成结构化警告 curator_thread_model_fallback:<原因>:<条数>；路由表整批共用的
#   警告（如 curator_profile_unavailable_fallback:<原因>）排在最前，每组都带。
# 函数用途: 从一批输入里切出这一次要处理的那组会话，并算出还剩几组。
def route_batch(batch: CuratorInputBatch, routing: CuratorModelRouting) -> RoutedBatch:
    if not batch.messages:
        return RoutedBatch(routing.default, batch, (), 0, True, routing.warnings)
    first = routing.route_for(batch.messages[0].thread_id)
    chosen = tuple(item for item in batch.messages if routing.route_for(item.thread_id).group_key == first.group_key)
    thread_ids = tuple(dict.fromkeys(item.thread_id for item in chosen))
    include_audit = first.group_key == routing.default.group_key
    others = {routing.route_for(item.thread_id).group_key for item in batch.messages} - {first.group_key}
    audit_left = bool(batch.audit_events) and not include_audit and routing.default.group_key not in others
    sub = replace(batch, messages=chosen, audit_events=batch.audit_events if include_audit else ())
    return RoutedBatch(first, sub, thread_ids, len(others) + int(audit_left), include_audit,
                       (*routing.warnings, *_fallback_warnings(routing, thread_ids)))


# 函数用途: 把本组里退回默认模型的会话按结构化原因计数成警告。
def _fallback_warnings(routing: CuratorModelRouting, thread_ids: tuple[str, ...]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for thread_id in thread_ids:
        reason = routing.fallback_reasons.get(thread_id, "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return tuple(f"curator_thread_model_fallback:{reason}:{count}" for reason, count in sorted(counts.items()))


# LLM: 游标投影只取本组会话的游标（默认组再加审计游标），用来比较“是不是同一批输入”；不改原 cursor 结构。
# 函数用途: 返回把运行记录里的起始游标投影到本组的函数。
def group_cursor_view(thread_ids: tuple[str, ...], include_audit: bool) -> Callable[[dict], object]:
    def view(cursor: dict) -> object:
        per_thread = cursor.get("per_thread_cursors") if isinstance(cursor, dict) else None
        per_thread = per_thread if isinstance(per_thread, dict) else {}
        audit = str(cursor.get("last_audit_event_id") or "") if include_audit and isinstance(cursor, dict) else ""
        return tuple(str(per_thread.get(thread_id) or "") for thread_id in thread_ids), audit
    return view


# LLM: 只保留和本组有关的历史：别的模型的失败不算；成功只有真推进了本组游标才算（会打断失败计数）。
#   这样别的组夹在中间成功，不会把本组的确定性失败计数清零，同一批输入不会被无限重放。
# 函数用途: 从最近运行记录（新到旧）里筛出本组的熔断历史。
def group_breaker_history(rows: list, identity: tuple[str, str], view: Callable[[dict], object]) -> list:
    kept = []
    for row in rows:
        if row.status == "succeeded" and view(row.cursor_before) == view(row.cursor_after):
            continue
        if row.status == "failed" and (row.provider, row.model) != identity:
            continue
        kept.append(row)
    return kept


# LLM: 运行记录里“会话模型连不上、这次用默认模型”的结构化警告码，写（curator._transient_fallback）和读
#   （_transient_fallback_threshold）共用这一处格式，不能各拼各的。
# 函数用途: 生成连接类失败让给默认模型时写进运行记录的警告码。
def transient_fallback_warning(code: str) -> str:
    return f"curator_thread_model_failed:{code}:transient"


# LLM: previous 新到旧且只含本组相关行，其中第一条成功就是最近一次推进本组游标的运行。它带“连不上让给默认”的警告码
#   （按 transient_fallback_warning 全文比对，不解析别的文字）时，阈值降到 CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT；
#   否则（会话模型自己成功推进、确定性失败的补跑、账里看不到成功）用 CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT。不加新状态。
# 函数用途: 算这组这一次要在同一输入上连续连不上几次，才改用默认模型。
def _transient_fallback_threshold(previous: list) -> int:
    advanced = next((row for row in previous if row.status == "succeeded"), None)
    fallback = {transient_fallback_warning(code) for code in CURATOR_TRANSIENT_FALLBACK_CODES}
    if advanced is not None and fallback & set(advanced.warnings):
        return CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT
    return CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT


# LLM: 纯函数；previous 是 group_breaker_history 筛过的本组历史（新到旧，会话自己的模型那组身份）。最近“阈值”条
#   （_transient_fallback_threshold：平时 2，上一次推进本组游标的是连不上的默认补跑时 1）全是同一起始游标（投影到本组）上的
#   连接类失败时，返回其中最近一次的失败码；中间夹一次推进本组游标的成功就不算。只看结构化状态、失败码、警告码和游标。
# 函数用途: 判断这组是否该改用默认模型，返回触发它的失败码；不该时返回空串。
def transient_fallback_code(cursor_before: dict, previous: list, view: Callable[[dict], object]) -> str:
    threshold = _transient_fallback_threshold(previous)
    window = previous[:threshold]
    if len(window) < threshold:
        return ""
    if all(row.status == "failed" and row.failure_code in CURATOR_TRANSIENT_FALLBACK_CODES
           and view(row.cursor_before) == view(cursor_before) for row in window):
        return str(window[0].failure_code)
    return ""


# LLM: 多组依次运行时给调用方一个结果：计数相加，状态、失败码、run_id、模型取最后一组（失败即停在那组）；
#   警告依次拼接并加 curator_model_groups:<组数>。只有一组时原样返回。
# 函数用途: 把一次触发里各组的运行结果汇总成一个。
def combined_run_result(results: list[CuratorRunResult]) -> CuratorRunResult:
    if len(results) == 1:
        return results[0]
    last = results[-1]
    return replace(
        last,
        processed_messages=sum(item.processed_messages for item in results),
        processed_audit_events=sum(item.processed_audit_events for item in results),
        daily_events=sum(item.daily_events for item in results),
        candidates=sum(item.candidates for item in results),
        warnings=(*(warning for item in results for warning in item.warnings), f"curator_model_groups:{len(results)}"),
    )


__all__ = [
    "CURATOR_DEFAULT_FALLBACK_CODES",
    "CURATOR_MODEL_GROUP_MAX_COUNT",
    "CURATOR_TRANSIENT_FALLBACK_CODES",
    "CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT",
    "CURATOR_TRANSIENT_REPEAT_FALLBACK_FAILURE_COUNT",
    "RoutedBatch",
    "combined_run_result",
    "group_breaker_history",
    "group_cursor_view",
    "route_batch",
    "transient_fallback_code",
    "transient_fallback_warning",
]
