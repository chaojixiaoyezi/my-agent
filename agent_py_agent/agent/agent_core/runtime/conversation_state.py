"""把权威会话 Compact 代次投影成模型可见的结构化运行事实。

模型可以阅读历史摘要和工具事件，但不能据此可靠推算当前已经 Compact 了几次。
本模块只投影 ``ConversationThread.compact_generation`` 或同一活动回合刚提交的代次，
不参与 Compact 决策，也不从自然语言、TUI 行或模型回复反推状态。
"""

from __future__ import annotations

import json

_LIVE_COMPACT_STATE_KEY = "_conversation_runtime_compact_state"
_RUNTIME_STATE_SCHEMA = "conversation_runtime_state.v1"


# LLM: A committed live Compact must update the next model-safe-point fact without mutating the
# immutable conversation seed. Canonical and turn-local generations remain explicitly distinct.
# 函数用途: 记录当前工具循环刚完成的 Compact 代次，供下一次模型调用读取准确数字。
def record_conversation_compact_generation(
    params: object,
    generation: int,
    *,
    canonical: bool,
) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    normalized = max(0, int(generation or 0))
    current = state.get(_LIVE_COMPACT_STATE_KEY)
    current = current if isinstance(current, dict) else {}
    previous = max(0, int(current.get("compact_generation") or 0))
    if normalized < previous:
        return False
    state[_LIVE_COMPACT_STATE_KEY] = {
        "compact_generation": normalized,
        "authority": (
            "conversation_thread.compact_generation"
            if canonical
            else "active_turn.compact_generation"
        ),
    }
    return True


# LLM: This is a model-facing projection only. Choose the newest typed generation from the
# immutable thread seed and the same-turn committed state; never parse summaries or UI events.
# 函数用途: 生成当前主代理或子代理可直接读取的 Compact 次数事实，避免模型从历史文字猜测。
def conversation_runtime_state_section(params: object) -> str:
    seed = getattr(params, "conversation_history_seed", None)
    has_seed = seed is not None
    seed_generation = max(
        0,
        int(getattr(seed, "compact_generation", 0) or 0),
    )
    generation = seed_generation
    authority = "conversation_history_seed.compact_generation"

    state = getattr(params, "live_archive_state", None)
    live = state.get(_LIVE_COMPACT_STATE_KEY) if isinstance(state, dict) else None
    live = live if isinstance(live, dict) else {}
    has_live = bool(live)
    live_generation = max(0, int(live.get("compact_generation") or 0))
    if has_live and (not has_seed or live_generation >= generation):
        generation = live_generation
        authority = str(live.get("authority") or "active_turn.compact_generation")

    if not has_seed and not has_live:
        return ""
    payload = {
        "schema": _RUNTIME_STATE_SCHEMA,
        "compact_generation": generation,
        "authority": authority,
        "current": True,
        "supersedes_prior_runtime_state": True,
    }
    return "# Conversation Runtime State\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "conversation_runtime_state_section",
    "record_conversation_compact_generation",
]
