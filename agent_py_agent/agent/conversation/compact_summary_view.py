# LLM: 本模块只读取原 committed checkpoint chain，并携带本次采用的临时视图；提交指针和账本仍是唯一权威。
# 摘要替代关系沿 summary_base_checkpoint_id，不得把提交链的局部覆盖无条件合并。
# 模块用途: 缓存稳定文件的完整验链元数据与独立范围视图，保留全部坏行验证及摘要base链语义。
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from .compact_checkpoint import iter_committed_compact_checkpoints
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope, scope_can_inherit

_VERIFIED_CHAINS = OrderedDict()
_SUMMARY_VIEWS = OrderedDict()
_SUMMARY_CACHE_LOCK = threading.Lock()


# LLM: 缓存包含owner/原路径/五元组/head/generation；文件追加即换key，即使head没变也不能跳过坏orphan。
# 函数用途: 从canonical文件事实建立两层缓存共同版本键，近期文件一律不复用。
def _checkpoint_cache_key(agent, thread):
    home = getattr(agent, "home_paths", None)
    root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not root:
        return None
    path = Path(root) / "conversations" / f"{thread.thread_id}.jsonl"
    try:
        stamp = cache_stat_signature(path.stat())
    except FileNotFoundError:
        return None
    if not cache_entry_trustworthy(stamp[3]):
        return None
    return ((getattr(thread, "owner_id", ""), getattr(thread, "owner_home", "")), str(path), stamp, thread.thread_id,
            thread.compact_checkpoint_id, int(thread.compact_generation or 0))


# LLM: 只在锁内操作有界LRU；返回值只供本模块只读消费，公开视图另做deepcopy。
# 函数用途: 读取一份已验证链或已选范围视图，不进行磁盘I/O。
def _cached_summary(cache, key):
    if key is None:
        return None
    with _SUMMARY_CACHE_LOCK:
        item = cache.get(key)
        if item is not None:
            cache.move_to_end(key)
            return item[1]
    return None


# LLM: 每层最多64项/16MiB，单项最多4MiB；大小计算在锁外，缓存不能变成无界正文仓库。
# 函数用途: 保存只读缓存投影并淘汰旧代，不改变原提交账本。
def _remember_summary(cache, key, value):
    if key is None:
        return
    size = len(repr(value).encode("utf-8", "surrogatepass"))
    if size > 4 * 1024 * 1024:
        return
    with _SUMMARY_CACHE_LOCK:
        cache[key] = (size, value)
        cache.move_to_end(key)
        while len(cache) > 64 or sum(row[0] for row in cache.values()) > 16 * 1024 * 1024:
            cache.popitem(last=False)


# LLM: 后端名称/模型外还绑定服务端协议端点，防同名后端换端点复用不可读密文；不把供应商记录当授权。
# 函数用途: 生成范围视图的后端兼容性缓存键。
def _backend_view_key(backend):
    provider_scope = getattr(backend, "provider_compaction_scope", None)
    protocol = json.dumps(provider_scope(), sort_keys=True) if callable(provider_scope) else ""
    return str(getattr(backend, "name", "") or ""), str(getattr(backend, "model_name", "") or ""), protocol

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from .models import ConversationThread


# LLM: 覆盖 refs 来自选中摘要的 base 链；legacy 消息只有终点，工具缺完整运行身份。
# 类用途: 把本次请求可用的摘要和精确/旧式覆盖证据交给历史与活动工具投影。
@dataclass(frozen=True)
class CompactSummaryView:
    checkpoint_id: str = ""
    summary: str = ""
    operation_evidence: dict[str, object] = field(default_factory=dict)
    source_message_ids: frozenset[str] = frozenset()
    source_tool_refs: tuple[dict[str, object], ...] = ()
    legacy_message_end_ids: tuple[str, ...] = ()
    generation: int = 0
    # 选中检查点的服务端压缩记录（空字典 = 普通文字摘要）；只有当前后端能读时才会选中带它的检查点。
    provider_compaction: dict[str, object] = field(default_factory=dict)


# LLM: Applied context is a per-run immutable snapshot of the chosen scope and its summary view;
# it is never a second checkpoint or a source of authority outside the matching thread.
# 类用途: 暂存本次请求实际采用的线程、摘要范围和视图，供历史与工具投影同次复用。
@dataclass(frozen=True)
class AppliedCompactContext:
    thread_id: str
    scope: CompactScope
    view: CompactSummaryView

    # LLM: Reject wrong types and detach nested ref/evidence dicts from the caller before source
    # hiding; the thread binding itself is checked at each runtime entry.
    # 函数用途: 校验线程与视图并复制内部字典，避免调用方随后改动本次覆盖范围。
    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise ValueError("Compact 应用线程身份缺失")
        if not isinstance(self.scope, CompactScope) or not isinstance(self.view, CompactSummaryView):
            raise TypeError("Compact 应用范围或摘要视图类型无效")
        object.__setattr__(self, "view", deepcopy(self.view))


# LLM: 旧 v1 只有前缀终点，必须在完整 canonical 行内解析，不能在任务筛选后猜边界；新版本只认精确 ID。
# 函数用途: 计算本次摘要实际替代的消息，缺失旧边界视为读取不完整而不是清空历史。
def compact_covered_message_ids(view, rows) -> frozenset[str]:
    covered = set(view.source_message_ids)
    positions = {row.message_id: index for index, row in enumerate(rows)}
    for end in view.legacy_message_end_ids:
        if end not in positions:
            raise OSError("legacy compact source boundary is missing from canonical history")
        covered.update(row.message_id for row in rows[:positions[end] + 1])
    return frozenset(covered)


# LLM: v1/v2 的摘要属于全线程，previous 兼任摘要 base；v3 读取严格显式字段。
# 函数用途: 将一条已提交 checkpoint 解释为其历史版本的摘要范围。
def _checkpoint_scope(row: dict[str, object]) -> CompactScope:
    schema = row.get("schema")
    if schema in {"conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"}:
        return THREAD_COMPACT_SCOPE
    if schema != "conversation_compact_checkpoint.v3":
        raise OSError("conversation compact checkpoint schema is unknown")
    try:
        return CompactScope.from_dict(row.get("scope"))
    except (TypeError, ValueError) as exc:
        raise OSError("conversation compact checkpoint scope is invalid") from exc


# LLM: 旧提交链没有独立摘要 base；只在 v1/v2 上显式采用 previous，不推断 v3 默认值。
# 函数用途: 读取一个摘要实际继承的 checkpoint 引用。
def _summary_base_id(row: dict[str, object]) -> str:
    if row.get("schema") in {"conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"}:
        value = row.get("previous_checkpoint_id")
    else:
        if "summary_base_checkpoint_id" not in row:
            raise OSError("conversation compact summary base is missing")
        value = row["summary_base_checkpoint_id"]
    if type(value) is not str:
        raise OSError("conversation compact summary base is invalid")
    return value


# LLM: 所有已知版本原writer均记录摘要哈希；旧版本不免除完整性检查，不能将坏旧摘要当新候选基础。
# 函数用途: 验证摘要正文与原哈希一致，拒绝缺失或损坏的语义基础。
def _validate_summary(row: dict[str, object]) -> None:
    summary = row.get("summary")
    digest = row.get("summary_sha256")
    if type(summary) is not str or not summary or type(digest) is not str:
        raise OSError("conversation compact summary is invalid")
    if hashlib.sha256(summary.encode("utf-8")).hexdigest() != digest:
        raise OSError("conversation compact summary digest mismatches")


# LLM: v3精确引用必须完整，不修补缺失身份；旧版本引用由各自读取合同解释。
# 函数用途: 校验新检查点的消息与工具覆盖，供同链摘要视图共用。
def _validated_v3_sources(row: dict[str, object]) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    message_ids = row.get("source_message_ids")
    tool_refs = row.get("source_tool_refs")
    if type(message_ids) is not list or type(tool_refs) is not list:
        raise OSError("conversation compact source refs are missing")
    if any(type(item) is not str or not item for item in message_ids):
        raise OSError("conversation compact source message ids are invalid")
    if len(set(message_ids)) != len(message_ids):
        raise OSError("conversation compact source message ids repeat")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    required = {"run_id", "attempt_id", "turn_id", "call_id"}
    for item in tool_refs:
        if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {"request_id"}:
            raise OSError("conversation compact source tool ref shape is invalid")
        if any(type(item[key]) is not str or not item[key] for key in required):
            raise OSError("conversation compact source tool identity is invalid")
        if "request_id" in item and type(item["request_id"]) is not str:
            raise OSError("conversation compact source request id is invalid")
        identity = tuple(str(item[key]) for key in ("run_id", "attempt_id", "turn_id", "call_id"))
        if identity in seen:
            raise OSError("conversation compact source tool refs repeat")
        seen.add(identity)
        result.append(dict(item))
    if not message_ids and not result:
        raise OSError("conversation compact checkpoint has no source coverage")
    return tuple(message_ids), tuple(result)


# LLM: 旧 v2 无 run/turn 原始身份，保留原调用和请求证据并标记 legacy，消费者不可精确匹配。
# 函数用途: 提取一条 checkpoint 自己的覆盖，不把后续提交误认为这条摘要的来源。
def _row_sources(row: dict[str, object]) -> tuple[tuple[str, ...], tuple[dict[str, object], ...], tuple[str, ...]]:
    schema = row.get("schema")
    if schema == "conversation_compact_checkpoint.v3":
        messages, tools = _validated_v3_sources(row)
        return messages, tools, ()
    if schema == "conversation_compact_checkpoint.v1":
        if row.get("source_kind") != "transcript":
            raise OSError("legacy transcript checkpoint source kind is invalid")
        end = row.get("source_end_message_id")
        if type(end) is not str or not end:
            raise OSError("legacy transcript checkpoint boundary is invalid")
        return (), (), (end,)
    if row.get("source_kind") != "live_tool_ir":
        raise OSError("legacy live checkpoint source kind is invalid")
    call_ids = row.get("source_tool_call_ids")
    if type(call_ids) is not list or any(type(item) is not str or not item for item in call_ids):
        raise OSError("legacy live checkpoint source calls are invalid")
    request_id, attempt_id = row.get("request_id"), row.get("attempt_id")
    if type(request_id) is not str or type(attempt_id) is not str:
        raise OSError("legacy live checkpoint provenance is invalid")
    return (), tuple({
        "legacy": True, "call_id": call_id,
        "request_id": request_id, "attempt_id": attempt_id,
    } for call_id in call_ids), ()


# LLM: 原reader逆序给出已提交行；先选适用范围，再只沿其显式base寻找当前后端可读摘要。
# 所有摘要/覆盖仍验证，正文随消费释放；同一文件快照只保留最终采用的一份正文，不能从纯元数据索引恢复正文。
# 函数用途: 构造只读摘要基础索引并在扫描中完成换模型后的读取选择，完整验链后才返回。
def _indexed_rows(chain, scope: CompactScope, backend):
    by_id: dict[str, dict[str, object]] = {}
    selected = None
    selection_started = False
    next_base_id = ""
    # provider_compaction（服务端压缩记录，约 2KB）留在索引里：兼容性回退要沿 base 链逐个检查，不能只看选中行。
    keys = ("checkpoint_id", "schema", "generation", "created_at", "scope", "previous_checkpoint_id",
            "summary_base_checkpoint_id", "source_kind", "source_message_ids", "source_tool_refs",
            "source_end_message_id", "source_tool_call_ids", "request_id", "attempt_id", "provider_compaction")
    for row in chain:
        row_scope = _checkpoint_scope(row)
        _validate_summary(row)
        _summary_base_id(row)
        created_at = row.get("created_at")
        if type(created_at) not in {int, float} or not math.isfinite(created_at) or created_at <= 0:
            raise OSError("conversation compact checkpoint creation time is invalid")
        if row.get("schema") == "conversation_compact_checkpoint.v3":
            _validated_v3_sources(row)
        by_id[str(row["checkpoint_id"])] = {key: row[key] for key in keys if key in row}
        candidate = (str(row["checkpoint_id"]) == next_base_id if selection_started
                     else scope_can_inherit(scope, row_scope, created_at))
        if candidate:
            selection_started = True
            next_base_id = ""
            if _checkpoint_is_readable(backend, row):
                selected = {**by_id[str(row["checkpoint_id"])], "summary": row["summary"],
                            "operation_evidence": row.get("operation_evidence")}
            else:
                next_base_id = _summary_base_id(row)
    for row in by_id.values():
        base_id = _summary_base_id(row)
        if not base_id:
            continue
        base = by_id.get(base_id)
        if base is None or int(base["generation"]) >= int(row["generation"]):
            raise OSError("conversation compact summary base is missing or newer")
        if not scope_can_inherit(_checkpoint_scope(row), _checkpoint_scope(base), base["created_at"]):
            raise OSError("conversation compact summary base crosses scope")
    return by_id, selected


# LLM: L1按文件版本完整验链，L2另按scope/backend选择摘要；命中返回deepcopy，文件追加/owner/head变化立即失效。
# 函数用途: 复用已验证Compact来源，保持摘要base链覆盖和调用方嵌套对象隔离。
def resolve_compact_summary_view(
    agent: SimpleAgent,
    thread: ConversationThread,
    scope: CompactScope,
) -> CompactSummaryView:
    if not isinstance(scope, CompactScope):
        raise TypeError("Compact 作用域类型无效")
    key = _checkpoint_cache_key(agent, thread)
    backend = getattr(agent, "backend", None)
    view_key = (key, scope, _backend_view_key(backend)) if key is not None else None
    cached = _cached_summary(_SUMMARY_VIEWS, view_key)
    if cached is not None and _checkpoint_cache_key(agent, thread) == key:
        return deepcopy(cached)
    by_id, selected = _summary_selection(agent, thread, scope, backend, key)
    view = _view_from_selected(by_id, selected)
    if key is not None and _checkpoint_cache_key(agent, thread) == key:
        _remember_summary(_SUMMARY_VIEWS, view_key, deepcopy(view))
    return view


# LLM: 冷读完整消费原链并验证全部摘要/base关系，L1只留元数据；热读选中行仍经原lookup逐字节hash核对。
# 函数用途: 分离完整验链和按范围选摘要，避免每个scope重复解析旧链。
def _summary_selection(agent, thread, scope, backend, key):
    by_id = _cached_summary(_VERIFIED_CHAINS, key)
    if by_id is None:
        with closing(iter_committed_compact_checkpoints(agent, thread)) as chain:
            by_id, selected = _indexed_rows(chain, scope, backend)
        if key is not None and _checkpoint_cache_key(agent, thread) == key:
            _remember_summary(_VERIFIED_CHAINS, key, by_id)
        return by_id, selected
    next_base = None
    for row in by_id.values():
        candidate = (row["checkpoint_id"] == next_base if next_base is not None
                     else scope_can_inherit(scope, _checkpoint_scope(row), row["created_at"]))
        if not candidate:
            continue
        if _checkpoint_is_readable(backend, row):
            from .compact_checkpoint_scan import checkpoint_row_lookup

            with checkpoint_row_lookup(Path(key[1])) as lookup:
                selected = lookup(str(row["checkpoint_id"]))
            if selected is None:
                raise OSError("conversation compact checkpoint chain is incomplete")
            return by_id, selected
        next_base = _summary_base_id(row)
    return by_id, None


# LLM: 只沿选中摘要的summary_base链累计精确覆盖，不扩大到同提交链其他scope；不修改L1共享元数据。
# 函数用途: 从已验证的选择构造独立摘要视图，空范围返回空视图。
def _view_from_selected(by_id, selected):
    if selected is None:
        return CompactSummaryView()
    messages: set[str] = set()
    tools: list[dict[str, object]] = []
    ends: list[str] = []
    seen_tools: set[tuple[object, ...]] = set()
    path: list[dict[str, object]] = []
    row: dict[str, object] | None = selected
    while row is not None:
        path.append(row)
        base_id = _summary_base_id(row)
        row = by_id.get(base_id) if base_id else None
    for item in reversed(path):
        item_messages, item_tools, item_ends = _row_sources(item)
        messages.update(item_messages)
        ends.extend(item_ends)
        for ref in item_tools:
            identity = (ref.get("legacy", False), ref.get("run_id"), ref.get("attempt_id"),
                        ref.get("turn_id"), ref.get("call_id"), ref.get("request_id"))
            if identity not in seen_tools:
                seen_tools.add(identity)
                tools.append(ref)
    evidence = selected.get("operation_evidence")
    if not isinstance(evidence, dict):
        raise OSError("conversation compact operation evidence is invalid")
    return CompactSummaryView(
        checkpoint_id=str(selected["checkpoint_id"]),
        summary=str(selected["summary"]),
        operation_evidence=dict(evidence),
        source_message_ids=frozenset(messages),
        source_tool_refs=tuple(tools),
        legacy_message_end_ids=tuple(ends),
        generation=int(selected["generation"]),
        provider_compaction=_validated_provider_compaction(selected),
    )


# LLM: 只比较服务端记录的协议与端点，普通文字摘要可读；选择和base遍历均由同次流式扫描负责。
# 函数用途: 判断当前后端能否读取一条原检查点，不读取文件或扩大来源覆盖。
def _checkpoint_is_readable(backend, row):
    from .compact_remote import provider_compaction_compatible

    record = row.get("provider_compaction")
    return record is None or provider_compaction_compatible(backend, record)


# LLM: 只接受带 item.encrypted_content 字符串的字典；缺失返回空字典，坏形状视为账本损坏（与 summary 校验同一严格度）。
# 函数用途: 校验并复制检查点里的服务端压缩记录。
def _validated_provider_compaction(row: dict[str, object]) -> dict[str, object]:
    record = row.get("provider_compaction")
    if record is None:
        return {}
    from .compact_remote import provider_compaction_content

    if type(record) is not dict or not provider_compaction_content(record):
        raise OSError("conversation compact provider compaction record is invalid")
    return dict(record)


__all__ = ["AppliedCompactContext", "CompactSummaryView", "resolve_compact_summary_view"]
