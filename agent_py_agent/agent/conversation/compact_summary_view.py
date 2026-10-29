# LLM: 本模块只读取原 committed checkpoint chain，并携带本次采用的临时视图；提交指针和账本仍是唯一权威。
# 摘要替代关系沿 summary_base_checkpoint_id，不得把提交链的局部覆盖无条件合并。
# 模块用途: 缓存深不可变验链和摘要内核，公开按需物化独立容器，保留原完整验证与base语义。
from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from .compact_checkpoint import iter_committed_compact_checkpoints
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope, scope_can_inherit

_VERIFIED_CHAINS = OrderedDict()
_SUMMARY_VIEWS = OrderedDict()
_SUMMARY_CACHE_LOCK = threading.Lock()

_VIEW_FIELDS = (
    'checkpoint_id', 'summary', 'operation_evidence', 'source_message_ids',
    'source_tool_refs', 'legacy_message_end_ids', 'generation', 'provider_compaction',
)
_MUTABLE_VIEW_FIELDS = frozenset({'operation_evidence', 'source_tool_refs', 'provider_compaction'})
_CACHE_ENTRY_LIMIT = 4 * 1024 * 1024
_CACHE_TOTAL_LIMIT = 16 * 1024 * 1024


# LLM: 内核只保留不可变元素，标签用于公开物化时还原原生list。
# 类用途: 区分原始列表与原始元组的冻结表示。
class _FrozenList(tuple):
    __slots__ = ()


# LLM: 只能在锁外复制JSON容器；代理背后的原字典也重新冻结，不能信任外部只读包装。
# 函数用途: 建立不含可变后代的内部投影，拒绝循环和未知对象。
def _freeze_json(value, active=None):
    if type(value) in {str, int, float, bool, type(None)}:
        return value
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise TypeError('Compact 投影不接受循环容器')
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            return MappingProxyType({key: _freeze_json(item, active) for key, item in value.items()})
        if type(value) in {list, _FrozenList}:
            return _FrozenList(_freeze_json(item, active) for item in value)
        if type(value) is tuple:
            return tuple(_freeze_json(item, active) for item in value)
        if type(value) is frozenset:
            return frozenset(_freeze_json(item, active) for item in value)
        raise TypeError('Compact 投影含未知容器')
    finally:
        active.remove(identity)


# LLM: 唯一解冻叶只复制请求子图，标量共享，结果绝不指回缓存。
# 函数用途: 给旧dict/list/tuple消费者恢复独立内建容器。
def _thaw_json(value):
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is _FrozenList:
        return [_thaw_json(item) for item in value]
    if type(value) is tuple:
        return tuple(_thaw_json(item) for item in value)
    if type(value) is frozenset:
        return frozenset(_thaw_json(item) for item in value)
    return value


# LLM: 冻结列表虽继承tuple却保留原列表语义；不解冻整图，不让容器类型变更绕过来源校验。
# 函数用途: 按原容器类型递归比较不可变投影，标量沿用原Python值比较。
def _frozen_equal(left, right):
    if left is right:
        return True
    if (type(left) is _FrozenList) != (type(right) is _FrozenList):
        return False
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (isinstance(left, Mapping) and isinstance(right, Mapping)
                and left.keys() == right.keys()
                and all(_frozen_equal(value, right[key]) for key, value in left.items()))
    if isinstance(left, tuple) or isinstance(right, tuple):
        return (isinstance(left, tuple) and isinstance(right, tuple) and len(left) == len(right)
                and all(_frozen_equal(a, b) for a, b in zip(left, right)))
    return left == right


# LLM: 只接收本模块已冻结值；不是提交权威，只是已验证结果的共享内核。
# 类用途: 保存一份可跨请求共享的不可变摘要字段。
@dataclass(frozen=True, slots=True)
class _SummarySnapshot:
    values: Mapping


# LLM: 预算是保守驻留估算而非RSS；固定节点/深度/字节硬界，未知对象拒绝缓存。
# 函数用途: 在锁外计费图而不repr或JSON序列化完整历史。
def _cache_resident_cost(key, value):
    seen = set()
    total = 256

    # LLM: 不预先列出所有后代；任一预算触顶立即停止，代理背后字典单独预留表成本。
    # 函数用途: 每个共享对象只计一次并限制递归深度。
    def walk(node, depth):
        nonlocal total
        if depth > 64:
            return False
        identity = id(node)
        if identity in seen:
            return True
        seen.add(identity)
        if len(seen) > 65536:
            return False
        total += sys.getsizeof(node)
        if isinstance(node, MappingProxyType):
            total += sys.getsizeof({}) + 192 * len(node)
        if total > _CACHE_ENTRY_LIMIT:
            return False
        if isinstance(node, Mapping):
            return all(walk(k, depth + 1) and walk(v, depth + 1) for k, v in node.items())
        if isinstance(node, (tuple, list, frozenset)):
            return all(walk(item, depth + 1) for item in node)
        if isinstance(node, (_SummarySnapshot, CompactScope)):
            total += sys.getsizeof(getattr(node, '__dict__', {}))
            return total <= _CACHE_ENTRY_LIMIT and all(
                walk(getattr(node, item.name), depth + 1) for item in fields(node))
        return type(node) in {str, int, float, bool, type(None)}

    return total if walk((key, value), 0) else _CACHE_ENTRY_LIMIT + 1


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


# LLM: 锁内只取不可变引用和维护LRU；公开返回值由独立外壳按需物化。
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


# LLM: 每层64项/16MiB，单项4MiB；冻结、计费和退休对象析构均不占LRU锁。
# 函数用途: 保存有界不可变投影；拒绝缓存不改变本次真实读取结果。
def _remember_summary(cache, key, value):
    if key is None:
        return
    if _cache_resident_cost(key, value) > _CACHE_ENTRY_LIMIT:
        return
    frozen = value if isinstance(value, _SummarySnapshot) else _freeze_json(value)
    size = _cache_resident_cost(key, frozen)
    if size > _CACHE_ENTRY_LIMIT:
        return
    retired = []
    with _SUMMARY_CACHE_LOCK:
        previous = cache.pop(key, None)
        if previous is not None:
            retired.append(previous)
        cache[key] = (size, frozen)
        cache.move_to_end(key)
        while len(cache) > 64 or sum(row[0] for row in cache.values()) > _CACHE_TOTAL_LIMIT:
            retired.append(cache.popitem(last=False))


# LLM: 后端名称/模型外还绑定服务端协议端点，防同名后端换端点复用不可读密文；不把供应商记录当授权。
# 函数用途: 生成范围视图的后端兼容性缓存键。
def _backend_view_key(backend):
    provider_scope = getattr(backend, "provider_compaction_scope", None)
    protocol = json.dumps(provider_scope(), sort_keys=True) if callable(provider_scope) else ""
    return str(getattr(backend, "name", "") or ""), str(getattr(backend, "model_name", "") or ""), protocol

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from .models import ConversationThread


# LLM: 公开字段保持原dataclass和内建容器合同，实际共享状态仅在深不可变内核中。
# 类用途: 为摘要调用方建立独立外壳，按公开访问物化可变字段，不复制未使用的历史图。
@dataclass(frozen=True, eq=False)
class CompactSummaryView:
    checkpoint_id: str = ""
    summary: str = ""
    operation_evidence: dict[str, object] = field(default_factory=dict, repr=False)
    source_message_ids: frozenset[str] = frozenset()
    source_tool_refs: tuple[dict[str, object], ...] = field(default=(), repr=False)
    legacy_message_end_ids: tuple[str, ...] = ()
    generation: int = 0
    # 选中检查点的服务端压缩记录（空字典 = 普通文字摘要）；只有当前后端能读时才会选中带它的检查点。
    provider_compaction: dict[str, object] = field(default_factory=dict, repr=False)
    __hash__ = None

    # LLM: 首次构造隔离输入；私有属性不是dataclass字段，asdict/replace仍只消费原公开字段集。
    # 函数用途: 冻结摘要输入并准备独立的延迟导出容器。
    def __post_init__(self):
        values = {name: object.__getattribute__(self, name) for name in _VIEW_FIELDS}
        values['source_message_ids'] = frozenset(values['source_message_ids'])
        values['source_tool_refs'] = tuple(values['source_tool_refs'])
        values['legacy_message_end_ids'] = tuple(values['legacy_message_end_ids'])
        snapshot = _SummarySnapshot(_freeze_json(values))
        for name in _VIEW_FIELDS:
            object.__setattr__(self, name, snapshot.values[name])
        object.__setattr__(self, '_snapshot', snapshot)
        object.__setattr__(self, '_exports', {})
        object.__setattr__(self, '_export_lock', threading.Lock())

    # LLM: 锁外解冻，短锁只发布首个结果；并发读取返回同一导出，不覆盖已经公开的修改。
    # 函数用途: 只在旧消费者主动读取可变字段时复制该字段。
    def __getattribute__(self, name):
        if name in _MUTABLE_VIEW_FIELDS:
            try:
                snapshot = object.__getattribute__(self, '_snapshot')
            except AttributeError:
                return object.__getattribute__(self, name)
            exports = object.__getattribute__(self, '_exports')
            lock = object.__getattribute__(self, '_export_lock')
            with lock:
                if name in exports:
                    return exports[name]
            value = _thaw_json(snapshot.values[name])
            with lock:
                return exports.setdefault(name, value)
        return object.__getattribute__(self, name)

    # LLM: 只复制固定字段引用，不访问公开属性或遍历refs。
    # 函数用途: 从已冻结内核建立O(1)独立外壳。
    @classmethod
    def _from_snapshot(cls, snapshot):
        result = object.__new__(cls)
        for name in _VIEW_FIELDS:
            object.__setattr__(result, name, snapshot.values[name])
        object.__setattr__(result, '_snapshot', snapshot)
        object.__setattr__(result, '_exports', {})
        object.__setattr__(result, '_export_lock', threading.Lock())
        return result

    # LLM: 已公开字段可能被任意修改，重新绑定必须捕获；未公开字段原样共享，不假装能追踪外部写入。
    # 函数用途: 为Applied、deepcopy和值比较固定当前外壳状态。
    def _snapshot_for_application(self):
        snapshot = object.__getattribute__(self, '_snapshot')
        with object.__getattribute__(self, '_export_lock'):
            exports = tuple(object.__getattribute__(self, '_exports').items())
        if not exports:
            return snapshot
        values = dict(snapshot.values)
        for name, value in exports:
            values[name] = _freeze_json(value)
        return _SummarySnapshot(MappingProxyType(values))

    # LLM: carry等旧消费者仍获得独立可变外壳，未公开冻结历史不用再次遍历。
    # 函数用途: 兼容deepcopy合同并复用不可变内核。
    def __deepcopy__(self, memo):
        result = memo.get(id(self))
        if result is None:
            result = type(self)._from_snapshot(self._snapshot_for_application())
            memo[id(self)] = result
        return result

    # LLM: 比较实际字段但不触发未公开refs的完整导出，保留原来源变更检测含义。
    # 函数用途: 维持视图按值比较而非按外壳身份比较。
    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return _frozen_equal(self._snapshot_for_application().values,
                             other._snapshot_for_application().values)


# LLM: Applied context is a per-run immutable snapshot of the chosen scope and its summary view;
# it is never a second checkpoint or a source of authority outside the matching thread.
# 类用途: 暂存本次请求实际采用的线程、摘要范围和视图，供历史与工具投影同次复用。
@dataclass(frozen=True)
class AppliedCompactContext:
    thread_id: str
    scope: CompactScope
    view: CompactSummaryView

    # LLM: 本次覆盖绑定独立外壳，已导出值捕获、未导出内核共享；线程检查与原边界不变。
    # 函数用途: 校验并固定本次应用视图，避免无条件深复制全部历史。
    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise ValueError("Compact 应用线程身份缺失")
        if not isinstance(self.scope, CompactScope) or not isinstance(self.view, CompactSummaryView):
            raise TypeError("Compact 应用范围或摘要视图类型无效")
        snapshot = self.view._snapshot_for_application()
        object.__setattr__(self, "view", CompactSummaryView._from_snapshot(snapshot))


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


# LLM: 冻结L1仅解冻scope子图，磁盘与内核仍走同一版本校验，不放宽持久schema。
# 函数用途: 将一条已提交 checkpoint 解释为其历史版本的摘要范围。
def _checkpoint_scope(row: dict[str, object]) -> CompactScope:
    schema = row.get("schema")
    if schema in {"conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"}:
        return THREAD_COMPACT_SCOPE
    if schema != "conversation_compact_checkpoint.v3":
        raise OSError("conversation compact checkpoint schema is unknown")
    try:
        return CompactScope.from_dict(_thaw_json(row.get("scope")))
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


# LLM: L1选择仅在冷边界恢复原行形状，沿原严格校验；旧v2身份仍标legacy，不补全猜测。
# 函数用途: 提取一条 checkpoint 自己的覆盖，不把后续提交误认为这条摘要的来源。
def _row_sources(row: dict[str, object]) -> tuple[tuple[str, ...], tuple[dict[str, object], ...], tuple[str, ...]]:
    if isinstance(row, MappingProxyType):
        row = _thaw_json(row)
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


# LLM: 原完整验链和版本键不变，L1/L2仅保存冻结图；warm返回固定大小独立外壳。
# 函数用途: 复用已验证摘要，避免热命中深复制全部来源。
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
        return CompactSummaryView._from_snapshot(cached)
    by_id, selected = _summary_selection(agent, thread, scope, backend, key)
    view = _view_from_selected(by_id, selected)
    if key is not None and _checkpoint_cache_key(agent, thread) == key:
        _remember_summary(_SUMMARY_VIEWS, view_key, view._snapshot_for_application())
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


# LLM: provider协议仍接真正dict，只解冻候选小记录，不向旧读取器泄漏冻结代理。
# 函数用途: 判断当前后端能否读取一条原检查点，不读取文件或扩大来源覆盖。
def _checkpoint_is_readable(backend, row):
    from .compact_remote import provider_compaction_compatible

    record = row.get("provider_compaction")
    return record is None or provider_compaction_compatible(backend, _thaw_json(record))


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
