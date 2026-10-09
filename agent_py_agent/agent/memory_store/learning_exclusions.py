from __future__ import annotations

"""学习排除名单到候选账本的结构化标记同步。"""

# LLM: 排除名单生效后，历史上已生成的待定候选必须打上持久排除标记（不再走自动晋升），
#   判定只用结构化字段：候选 source_message_refs 的 thread_id 与 thread 通道绑定的
#   channel_conversation_id，绝不解析消息正文。
# 模块用途: 把 owner 学习排除名单同步成候选账本上的持久标记，并提供共享的会话排除判定。

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


# LLM: 同步只做两件结构化事：把名单映射成被排除的 thread_id 集合，再给来源命中这些 thread 的
#   pending/approved 候选写排除标记；候选状态、人工审核路径和名单外的会话都不受影响。
# 函数用途: 把学习排除名单同步成候选账本标记，返回标记与检查计数。
def sync_learning_exclusion_marks(
    *,
    conversation_store: object,
    candidate_service: object,
    excluded_conversation_ids: frozenset[str],
) -> dict[str, object]:
    if not excluded_conversation_ids:
        return {"marked": 0, "checked": 0}
    thread_ids = excluded_thread_ids(conversation_store, excluded_conversation_ids)
    if not thread_ids:
        return {"marked": 0, "checked": 0}
    rows = candidate_service.list(statuses={"pending_review", "approved"}, limit=0)
    targets = [row.candidate_id for row in rows if _row_excluded(row, thread_ids)]
    marked = (
        candidate_service.mark_promotion_excluded(
            targets, reason=LEARNING_SOURCE_EXCLUDED
        )
        if targets
        else 0
    )
    return {"marked": marked, "checked": len(rows), "excluded_threads": len(thread_ids)}


# LLM: 映射方向只有 thread→channel 一种：排除名单按 channel_conversation_id，候选与 audit 引用按
#   thread_id，所以先把名单翻译成被排除的 thread_id 集合，消息/audit/候选判定全部走同一集合。
# 函数用途: 把排除会话 ID 集合翻译成对应的 thread_id 集合。
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


# LLM: 候选来源只看它自己保存的消息引用里的 thread_id；引用里没有 thread_id 的旧候选无法证明
#   来自名单会话，按不排除处理（宁可少标也不误标）。
# 函数用途: 判断一条候选是否来自被排除的会话。
def _row_excluded(row: object, excluded_thread_ids: set[str]) -> bool:
    refs = getattr(row, "source_message_refs", ()) or ()
    return any(
        str(ref.get("thread_id") or "").strip() in excluded_thread_ids
        for ref in refs
        if isinstance(ref, dict)
    )


# LLM: 定向判定用于补晋升路径：只对给定候选做结构化命中检查，不扫描全账本、不写标记；
#   名单不可读的 fail-closed 由调用方处理（这里只处理非空名单的纯读判定）。
# 函数用途: 返回给定候选中来源命中排除名单的候选 ID 集合。
def excluded_candidate_ids(
    *,
    conversation_store: object,
    rows: object,
    excluded_conversation_ids: frozenset[str],
) -> set[str]:
    if not excluded_conversation_ids:
        return set()
    thread_ids = excluded_thread_ids(conversation_store, excluded_conversation_ids)
    if not thread_ids:
        return set()
    return {
        str(getattr(row, "candidate_id", "") or "")
        for row in rows
        if _row_excluded(row, thread_ids)
    }


__all__ = [
    "LEARNING_SOURCE_EXCLUDED",
    "curator_learning_exclusions",
    "curator_learning_exclusions_revision",
    "excluded_candidate_ids",
    "excluded_thread_ids",
    "sync_learning_exclusion_marks",
    "thread_is_excluded",
]
