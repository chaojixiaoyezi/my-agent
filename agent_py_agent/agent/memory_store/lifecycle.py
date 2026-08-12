from __future__ import annotations

"""会话生命周期到统一 Memory Curator 请求的 typed 适配。"""

# LLM: close/reset 只能登记固定 reason，不能自己运行模型或写 daily/candidate/long-term。
# 模块用途: 让 CLI、Gateway 和未来 IM 会话管理器共用同一 Curator 请求入口。

from dataclasses import asdict, dataclass

_SESSION_EVENT_REASONS = {
    "close": "session_close",
    "reset": "reset",
}


# LLM: 调用方只依赖稳定 reason/requested 字段，不解析 Curator 的人类说明文字。
# 类用途: 表示一次会话生命周期提炼请求已经进入 durable state。
@dataclass(frozen=True)
class MemoryCuratorLifecycleRequest:
    event: str
    reason: str
    requested: bool
    pending_reasons: tuple[str, ...] = ()
    requested_at: str = ""

    # LLM: HTTP/CLI 输出只投影 bounded state，不包含会话或候选正文。
    # 函数用途: 生成跨入口一致的 JSON 对象。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["pending_reasons"] = list(self.pending_reasons)
        return payload


# LLM: event 是结构化枚举；普通聊天正文不得被猜成 close/reset。
# 函数用途: 向当前 owner 唯一 MemoryCuratorService 登记会话关闭或重置请求。
def request_memory_curator_for_session(
    agent: object,
    *,
    event: str,
) -> MemoryCuratorLifecycleRequest:
    normalized = str(event or "").strip().lower()
    reason = _SESSION_EVENT_REASONS.get(normalized)
    if reason is None:
        raise ValueError("memory curator session event must be close or reset")
    policy = getattr(agent, "owner_policy", None)
    if policy is not None and not bool(getattr(policy, "memory_enabled", True)):
        return MemoryCuratorLifecycleRequest(
            event=normalized,
            reason=reason,
            requested=False,
            pending_reasons=("memory_policy_disabled",),
            requested_at="",
        )  # 总闸关闭:会话关闭不登记 curator 请求(effective flag,与调度层短路同源)
    curator = getattr(agent, "memory_curator", None)
    request = getattr(curator, "request", None)
    if not callable(request):
        raise RuntimeError("MemoryCuratorService is not available for this owner")
    result = request(reason)
    result = result if isinstance(result, dict) else {}
    return MemoryCuratorLifecycleRequest(
        event=normalized,
        reason=reason,
        requested=result.get("requested") is True,
        pending_reasons=tuple(
            str(item)
            for item in (result.get("pending_reasons") or [])
            if str(item or "").strip()
        ),
        requested_at=str(result.get("requested_at") or ""),
    )


# LLM: 用户退出不能因后台记忆故障崩溃；失败只意味着 state 中没有本次请求，不伪造成功。
# 函数用途: 为 UI 关闭路径提供 best-effort 请求并返回是否真实登记。
def request_memory_curator_for_session_best_effort(
    agent: object,
    *,
    event: str,
) -> bool:
    try:
        return request_memory_curator_for_session(agent, event=event).requested
    except Exception:  # noqa: BLE001 - session close must remain available.
        return False


__all__ = [
    "MemoryCuratorLifecycleRequest",
    "request_memory_curator_for_session",
    "request_memory_curator_for_session_best_effort",
]
