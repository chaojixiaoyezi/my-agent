# LLM: This module owns the bounded in-memory lifetime of Gateway tool approvals. Approval
# decisions remain exact-call facts scoped by owner/thread/cwd/access; callers must never widen
# one cached item into a tool-wide, owner-wide, durable, or natural-language permission rule.
# 模块用途: 保存 Gateway 当前运行期内的“本会话允许”记录；相同会话和相同参数可复用，换用户、目录、权限或重启 Gateway 后必须重新询问。

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

_DEFAULT_MAX_SCOPES = 256
_DEFAULT_MAX_KEYS_PER_SCOPE = 128
_AGENT_CACHE_ATTR = "_gateway_tool_approval_session_cache"
_AGENT_CACHE_INIT_LOCK = threading.Lock()


# LLM: This cache is an availability optimization, never an authorization source beyond the
# exact active Gateway process. Eviction is fail-safe because it only causes another prompt.
# 类用途: 线程安全地保存少量会话审批键，并用 LRU 上限防止长期 Gateway 因大量会话持续涨内存。
@dataclass
class ToolApprovalSessionCache:
    max_scopes: int = _DEFAULT_MAX_SCOPES
    max_keys_per_scope: int = _DEFAULT_MAX_KEYS_PER_SCOPE
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _scopes: OrderedDict[str, OrderedDict[str, None]] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )

    # LLM: Invalid limits would turn a fail-safe cache into an unbounded store; normalize them
    # at construction and keep all later operations bounded under the same lock.
    # 函数用途: 校正缓存上限，避免错误参数让常驻 Gateway 的审批记录无限增长。
    def __post_init__(self) -> None:
        self.max_scopes = max(1, int(self.max_scopes or _DEFAULT_MAX_SCOPES))
        self.max_keys_per_scope = max(
            1,
            int(self.max_keys_per_scope or _DEFAULT_MAX_KEYS_PER_SCOPE),
        )

    # LLM: A hit requires both exact opaque scope and exact item key. Touching LRU order may
    # affect only future re-prompts and must never manufacture an approval.
    # 函数用途: 查询当前作用域是否已经批准过这一次完全相同的工具调用。
    def is_approved(self, scope: str, item_key: str) -> bool:
        normalized_scope = str(scope or "").strip()
        normalized_key = str(item_key or "").strip()
        if not normalized_scope or not normalized_key:
            return False
        with self._lock:
            keys = self._scopes.get(normalized_scope)
            if keys is None or normalized_key not in keys:
                return False
            keys.move_to_end(normalized_key)
            self._scopes.move_to_end(normalized_scope)
            return True

    # LLM: Only an explicit approved_session decision may call this method. Bounded eviction
    # deliberately degrades to asking again rather than retaining broader or durable authority.
    # 函数用途: 记住当前会话里一次精确批准；超出上限时淘汰旧记录，之后重新弹窗即可。
    def approve(self, scope: str, item_key: str) -> bool:
        normalized_scope = str(scope or "").strip()
        normalized_key = str(item_key or "").strip()
        if not normalized_scope or not normalized_key:
            return False
        with self._lock:
            keys = self._scopes.setdefault(normalized_scope, OrderedDict())
            keys[normalized_key] = None
            keys.move_to_end(normalized_key)
            while len(keys) > self.max_keys_per_scope:
                keys.popitem(last=False)
            self._scopes.move_to_end(normalized_scope)
            while len(self._scopes) > self.max_scopes:
                self._scopes.popitem(last=False)
        return True


# LLM: The scope digest freezes all structured authority boundaries that can change whether the
# same call is safe. Raw owner ids and host paths must not be retained in this operational key.
# 函数用途: 把用户、会话、聊天、目录和权限模式合成不泄露原值的稳定审批作用域。
def tool_approval_session_scope(
    *,
    owner_id: object,
    thread_id: object,
    conversation_id: object,
    cwd: object,
    access_mode: object,
    path_access_mode: object,
) -> str:
    values = {
        "owner_id": str(owner_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "cwd": str(cwd or "").strip(),
        "access_mode": str(access_mode or "").strip(),
        "path_access_mode": str(path_access_mode or "").strip(),
    }
    if not values["owner_id"] or not values["thread_id"] or not values["cwd"]:
        return ""
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "approval-session:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# LLM: One long-lived owner-scoped Agent owns one process-local cache, mirroring 会话运行时
# SessionServices lifetime. Dynamic attachment keeps small test doubles compatible.
# 函数用途: 取得当前 owner Agent 的唯一审批缓存；多个 Gateway 请求会复用，进程结束自然清空。
def agent_tool_approval_session_cache(agent: object) -> ToolApprovalSessionCache:
    existing = getattr(agent, _AGENT_CACHE_ATTR, None)
    if isinstance(existing, ToolApprovalSessionCache):
        return existing
    with _AGENT_CACHE_INIT_LOCK:
        existing = getattr(agent, _AGENT_CACHE_ATTR, None)
        if isinstance(existing, ToolApprovalSessionCache):
            return existing
        created = ToolApprovalSessionCache()
        setattr(agent, _AGENT_CACHE_ATTR, created)
        return created
