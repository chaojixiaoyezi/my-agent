from __future__ import annotations

"""学习排除名单到候选账本的结构化标记同步。"""

# LLM: 排除名单生效后，历史上已生成的待定候选必须打上持久排除标记（不再走自动晋升），
#   判定只用结构化字段：候选 source_message_refs 的 thread_id 与 thread 通道绑定的
#   channel_conversation_id，绝不解析消息正文。
# 模块用途: 把 owner 学习排除名单同步成候选账本上的持久标记，并提供共享的会话排除判定。

from copy import deepcopy

from ..user_space.owner_policy import read_learning_exclusions, read_learning_exclusions_revision

# 候选被学习来源排除时写入的结构化原因码；promote(automatic=True) 会原样返回它并保持候选状态。
LEARNING_SOURCE_EXCLUDED = "LEARNING_SOURCE_EXCLUDED"


# LLM: 名单不可读时返回 unreadable=True，由 curator 决定 fail-closed（本轮不学任何会话）；
#   判定只看结构化字段，空名单等价于没有排除。
# 函数用途: 读取 owner 学习排除名单，返回 (会话 ID 集合, 是否不可读)。
def curator_learning_exclusions(home: object) -> tuple[frozenset[str], bool]:
    entries, error = read_learning_exclusions(home)
    return frozenset(item.channel_conversation_id for item in entries), error is not None


# LLM: 名单修订号只用于提交前记录与审计回溯（判定本身始终按最新名单重读）；读取失败按 0。
# 函数用途: 读取 owner 学习排除名单的修订号。
def curator_learning_exclusions_revision(home: object) -> int:
    return read_learning_exclusions_revision(home)


# LLM: thread 的任一通道绑定命中排除会话即视为被排除；没有绑定的老 thread 无法判定，按不排除处理。
# 函数用途: 判断一个会话 thread 是否属于学习排除名单。
def thread_is_excluded(thread: object, excluded_conversation_ids: frozenset[str]) -> bool:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    return any(
        str(getattr(binding, "channel_conversation_id", "") or "").strip()
        in excluded_conversation_ids
        for binding in bindings
    )


# LLM: 原候选窗口公平推进，每片只核本页引用；只有canonical绑定确证命中才持久标记，unknown仅留诊断。
# 函数用途: 有界同步历史待定候选排除标记，保持原批量写锁与幂等入口。
def sync_learning_exclusion_marks(
    *,
    conversation_store: object,
    candidate_service: object,
    excluded_conversation_ids: frozenset[str],
) -> dict[str, object]:
    if not excluded_conversation_ids:
        return {"marked": 0, "checked": 0}
    window_reader = getattr(candidate_service, "readonly_window", None)
    if not callable(window_reader):
        return {"marked": 0, "checked": 0, "coverage": "unknown",
                "errors": ({"code": "LEARNING_EXCLUSION_CANDIDATE_WINDOW_UNAVAILABLE",
                            "side_effects": "not_started", "next_action": "为候选仓库提供readonly_window有界读口。"},)}
    state = getattr(candidate_service, "_learning_exclusion_scan", {})
    signature = tuple(sorted(excluded_conversation_ids))
    cursor = state.get("cursor", {}) if state.get("excluded_ids") == signature else {}
    window = window_reader(deepcopy(cursor), max_records=128)
    rows, _refs, next_cursor, wrapped = window
    pending = [row for row in rows if getattr(row, "status", "") in {"pending_review", "approved"}
               and str(getattr(row, "promotion_exclusion", "") or "").strip() != LEARNING_SOURCE_EXCLUDED]
    report = candidate_exclusion_report(conversation_store=conversation_store, rows=pending,
                                       excluded_conversation_ids=excluded_conversation_ids)
    targets = sorted(report["excluded_candidate_ids"])
    marked = candidate_service.mark_promotion_excluded(targets, reason=LEARNING_SOURCE_EXCLUDED) if targets else 0
    # 公平调度续点不授学习/删除权；写标记失败保留原页，下次仍由canonical重验。
    candidate_service._learning_exclusion_scan = {"excluded_ids": signature, "cursor": deepcopy(next_cursor)}
    errors = (*getattr(window, "errors", ()), *report["errors"])
    return {"marked": marked, "checked": len(rows), "excluded_threads": report["checked_threads"],
            "coverage": "partial" if errors or not wrapped else "complete", "errors": errors,
            "unknown_candidate_ids": tuple(sorted(report["unknown_candidate_ids"]))}



# LLM: 映射方向只有 thread→channel 一种：排除名单按 channel_conversation_id，候选与 audit 引用按
#   thread_id，所以先把名单翻译成被排除的 thread_id 集合，消息/audit/候选判定全部走同一集合。
# 函数用途: 显式完整清单导出；后台同步和候选准入不得调用此穷举入口。
def excluded_thread_ids(
    conversation_store: object,
    excluded_conversation_ids: frozenset[str],
) -> set[str]:
    list_report = getattr(getattr(conversation_store, "threads", None), "list_report", None)
    if not callable(list_report):
        return set()
    threads, _errors = list_report(limit=0)
    return {
        str(getattr(thread, "thread_id", "") or "")
        for thread in threads
        if thread_is_excluded(thread, excluded_conversation_ids)
    }


def candidate_exclusion_report(
    *, conversation_store: object, rows: object,
    excluded_conversation_ids: frozenset[str], max_thread_reads: int = 128,
) -> dict[str, object]:
    """批内共享精确来源快照；缺失、坏来源和超预算只阻断关联候选，不扫描历史或冒充排除事实。"""
    if type(max_thread_reads) is not int or max_thread_reads < 0:
        raise ValueError("max_thread_reads必须为非负整数")
    excluded, unknown, errors = set(), set(), []
    result = {"excluded_candidate_ids": excluded, "unknown_candidate_ids": unknown,
              "errors": errors, "checked_threads": 0}
    if not excluded_conversation_ids:
        result["errors"] = ()
        return result
    reader = getattr(getattr(conversation_store, "threads", None), "load_report", None)
    observations = {}
    for row in rows or ():
        candidate_id = str(getattr(row, "candidate_id", "") or "")
        refs = getattr(row, "source_message_refs", ()) or ()
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            thread_id = str(ref.get("thread_id") or "").strip()
            if not thread_id:
                continue  # 无thread_id的旧引用保持原语义；其它宿主来源/证据门仍负责可学资格。
            if thread_id not in observations:
                reason, detail, error_type = "", "", ""
                thread = None
                if not callable(reader):
                    reason = "THREAD_SOURCE_READ_API_UNAVAILABLE"
                elif result["checked_threads"] >= max_thread_reads:
                    reason = "THREAD_SOURCE_READ_BUDGET_EXHAUSTED"
                else:
                    result["checked_threads"] += 1
                    try:
                        thread, error = reader(thread_id)
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, TypeError) as exc:
                        reason, detail, error_type = "THREAD_SOURCE_UNREADABLE", str(exc), type(exc).__name__
                    else:
                        if error:
                            reason, detail = "THREAD_SOURCE_UNREADABLE", str(error.get("message") or error.get("error") or error)
                            error_type = str(error.get("error_type") or error.get("code") or "")
                        elif thread is None:
                            reason = "THREAD_SOURCE_MISSING"
                        elif str(getattr(thread, "thread_id", "")) != thread_id:
                            reason = "THREAD_SOURCE_IDENTITY_MISMATCH"
                observations[thread_id] = "unknown" if reason else (
                    "excluded" if thread_is_excluded(thread, excluded_conversation_ids) else "allowed")
                if reason:
                    errors.append({"code": reason, "thread_id": thread_id, "error_type": error_type,
                                   "message": detail or "无法核实候选引用的canonical线程来源。",
                                   "side_effects": "not_started",
                                   "next_action": "保留该候选；核对准确thread_id及原线程读口，下一维护片重验，不按缺失来源放行。"})
            observation = observations[thread_id]
            if observation == "excluded":
                excluded.add(candidate_id)
                unknown.discard(candidate_id)
                break  # 一项确证排除已足以否决，不再读取无关引用。
            if observation == "unknown":
                unknown.add(candidate_id)
                if not callable(reader) or result["checked_threads"] >= max_thread_reads:
                    break  # 该候选已经unknown；不再为未读取的长引用列表制造无界诊断。
    unknown.difference_update(excluded)
    result["errors"] = tuple(errors)
    return result


def excluded_candidate_ids(
    *, conversation_store: object, rows: object,
    excluded_conversation_ids: frozenset[str],
) -> set[str]:
    """兼容原集合调用；仅作本次保守gate，unknown不是可持久化的排除事实。"""
    report = candidate_exclusion_report(conversation_store=conversation_store, rows=rows,
                                        excluded_conversation_ids=excluded_conversation_ids)
    return report["excluded_candidate_ids"] | report["unknown_candidate_ids"]


__all__ = [
    "LEARNING_SOURCE_EXCLUDED",
    "curator_learning_exclusions",
    "curator_learning_exclusions_revision",
    "candidate_exclusion_report",
    "excluded_candidate_ids",
    "excluded_thread_ids",
    "sync_learning_exclusion_marks",
    "thread_is_excluded",
]
