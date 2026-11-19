from __future__ import annotations

"""为后台 Curator 提供有界、只读的现有正式记忆上下文。"""

# LLM: 只暴露当前 owner 的 active 正式材料，不向模型授予仓库修改方法；关系建议所需原版本/正文长度只留宿主。
# 普通 Curator 投影不随可选增强改变；同步核对 curator_inputs 与关系建议的完整性/失效测试。
# 模块用途: 将 long-term、lesson 和 HOT 投影成短预览，供冲突判断而不复制派生索引或旧版本。

import hashlib
import json
import time
from dataclasses import dataclass, replace

# 单条记忆项预览最多 1200 字符：给模型看够判断的摘要，避免整条灌入。
_ITEM_PREVIEW_CHARS = 1_200


# LLM: 正式输入携带稳定身份和结构化 scope；预览始终是非权威历史上下文，宿主字段不能给无版本仓库伪造版本。
# 类用途: 表示一条可供策展比较、但不能由模型修改的现有正式记忆。
@dataclass(frozen=True)
class CuratorFormalMemoryInput:
    authority_type: str
    authority_id: str
    authority_ref: str
    content_preview: str
    content_hash: str
    subject_key: str
    scope_type: str
    scope_key: str
    updated_at: str
    authority_version: int | None = None
    content_chars: int | None = None

    # LLM: Only bounded scalar metadata crosses the provider boundary; candidate review and
    # persona internals are intentionally absent.
    # 函数用途: 生成 Curator 模型输入中的正式记忆投影。
    def to_model(self) -> dict[str, str]:
        return {
            "authority_type": self.authority_type,
            "authority_id": self.authority_id,
            "authority_ref": self.authority_ref,
            "content_preview": self.content_preview,
            "content_hash": self.content_hash,
            "subject_key": self.subject_key,
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "updated_at": self.updated_at,
        }


# 本批用户原话拼成的检索查询最多 1000 字符：够检索判断相关性，也不让超长输入拖慢索引查询。
FORMAL_QUERY_MAX_CHARS = 1_000
# 每次最多直读这么多条进了路由索引的 lesson，按查询排序后再取前几条；与路由索引本身的行数上限同量级。
_ROUTED_LESSON_READ_MAX_ENTRIES = 64


# LLM: 元素就是原投影，下游照旧按 tuple 使用；errors 只放结构化缺口码和计数，不放正文。
#   missing_pins 只收在完整覆盖下确认已不存在（被删、过期或换掉）的置顶目标，覆盖不全时为空，置顶保留。
# 类用途: 表示一次有界正式窗口读取的结果及其覆盖事实。
class FormalMemoryWindow(tuple):
    errors: tuple[dict[str, object], ...]
    missing_pins: tuple[str, ...]

    # 函数用途: 用投影列表和结构化覆盖事实构造只读窗口。
    def __new__(cls, items=(), *, errors=(), missing_pins=()):
        window = super().__new__(cls, items)
        window.errors = tuple(errors)
        window.missing_pins = tuple(missing_pins)
        return window


# LLM: Store objects remain host-side dependencies; read() returns plain immutable projections
# and therefore cannot expose remember/persona/file tools to the background model.
# 类用途: 从三个正式正文仓库按相关性有界读取 active 记录并执行统一字符预算。
@dataclass(frozen=True)
class FormalMemorySource:
    long_term: object
    lessons: object
    hot: object

    # LLM: 不再整库物化：long-term 走点查（置顶）+ 有界相关性检索，lesson 只看路由投影里的活跃项，HOT 按规范引用直读。
    #   可见范围与原 all() 相同（本 owner 自己的库、未过期 active），不做前台 personal/project 范围扩展。
    #   query 为空时 long-term 取只读词面检索的确定性有界子集、lesson 按路由热度顺序取前几条；条目数/字符硬帽不变，
    #   放不下的单条跳过，最后按类型+ID 稳定排序，同一批权威事实给模型的输入字节一致。
    # 函数用途: 返回本批相关的正式记忆有界窗口，并带回覆盖缺口与已确认消失的置顶目标。
    def read(
        self,
        *,
        max_items: int = 32,
        max_chars: int = 6_000,
        query: str = "",
        pinned_ids: tuple[str, ...] = (),
    ) -> FormalMemoryWindow:
        text = " ".join(str(query or "").split())[:FORMAL_QUERY_MAX_CHARS]
        errors: list[dict[str, object]] = []
        pinned, missing = _pinned_long_term_items(self.long_term, pinned_ids, errors)
        ranked = [
            *pinned,
            *_relevant_long_term_items(self.long_term, text, max_items, errors),
            *_lesson_items(self.lessons, text, max_items, errors),
            *_hot_items(self.hot, self.lessons),
        ]
        selected = _within_budget(ranked, max_items=max_items, max_chars=max_chars)
        return FormalMemoryWindow(sorted(selected, key=_formal_sort_key), errors=errors, missing_pins=missing)


# LLM: 优先级即列表顺序（置顶→相关 long-term→lesson→HOT）；同一权威只占一次，放不下的跳过而不是整窗截断。
# 函数用途: 按条目数和字符预算挑选正式窗口条目。
def _within_budget(
    items: list[CuratorFormalMemoryInput],
    *,
    max_items: int,
    max_chars: int,
) -> list[CuratorFormalMemoryInput]:
    selected: list[CuratorFormalMemoryInput] = []
    seen: set[tuple[str, str]] = set()
    used = 0
    for item in items:
        if len(selected) >= max_items:
            break
        key = (item.authority_type, item.authority_id)
        if key in seen:
            continue
        size = len(json.dumps(item.to_model(), ensure_ascii=False, sort_keys=True))
        if used + size > max_chars:
            continue
        seen.add(key)
        selected.append(item)
        used += size
    return selected


# LLM: 后台可见集合与原 all() 一致：本库、未过期；read_ids/检索本身只读当前 owner 库，不跨库也不扩范围。
# 函数用途: 判断一条正式记录能否进入后台整理窗口。
def _curator_visible(record: object, now: float) -> bool:
    expires_at = float(getattr(record, "expires_at", 0.0) or 0.0)
    return bool(str(getattr(record, "entry_id", "") or "").strip()) and (not expires_at or expires_at > now)


# LLM: 置顶目标只走 read_ids 有界点查；覆盖不全（MEMORY_AUTHORITY_INCOMPLETE）只记缺口、不判消失，置顶保留。
# 函数用途: 读出冲突置顶目标的当前版本，并列出完整覆盖下已确认不存在的目标。
def _pinned_long_term_items(
    repository: object,
    pinned_ids: tuple[str, ...],
    errors: list[dict[str, object]],
) -> tuple[list[CuratorFormalMemoryInput], tuple[str, ...]]:
    ids = tuple(dict.fromkeys(text for raw in pinned_ids if (text := str(raw or "").strip())))
    if not ids:
        return [], ()
    reader = getattr(repository, "read_ids", None)
    if not callable(reader):
        raise TypeError("formal long-term source lacks read_ids()")
    try:
        records = reader(ids)
    except ValueError as exc:
        if str(exc) != "MEMORY_AUTHORITY_INCOMPLETE":
            raise
        errors.append({"code": "MEMORY_AUTHORITY_INCOMPLETE", "detail": f"pinned_ids={len(ids)}"})
        return [], ()
    now = time.time()
    found = {record.entry_id: record for record in records if _curator_visible(record, now)}
    missing = tuple(entry_id for entry_id in ids if entry_id not in found)
    return [_long_term_item(found[entry_id]) for entry_id in ids if entry_id in found], missing


# LLM: 有查询时与前台同一有界检索（latency_safe：不记访问、不等嵌入网络）；没有用户原话时改用只读词面检索
#   取同一读索引的确定性有界子集（空查询不能触发向量补齐）。覆盖不全时如实记缺口，绝不回退 all()。
# 函数用途: 选出与本批用户原话最相关的 long-term 记录。
def _relevant_long_term_items(
    repository: object,
    query: str,
    limit: int,
    errors: list[dict[str, object]],
) -> list[CuratorFormalMemoryInput]:
    if limit <= 0:
        return []
    now = time.time()
    if not query:
        reader = getattr(repository, "search_report", None)
        if not callable(reader):
            raise TypeError("formal long-term source lacks search_report()")
        records, diagnostics = reader("", limit)
        coverages = [row.get("coverage") for row in diagnostics or () if isinstance(row, dict)]
    else:
        search = getattr(repository, "search_scoped_candidates_report", None)
        if not callable(search):
            raise TypeError("formal long-term source lacks search_scoped_candidates_report()")
        records, facts = search(query, limit, lambda record: _curator_visible(record, now), latency_safe=True)
        coverages = [facts.get("coverage") if isinstance(facts, dict) else None]
    partial = next((row for row in coverages if isinstance(row, dict) and row.get("partial")), None)
    if partial is not None:
        errors.append({
            "code": "FORMAL_MEMORY_COVERAGE_PARTIAL",
            "detail": "scanned_records={};scanned_bytes={}".format(
                int(partial.get("scanned_records") or 0), int(partial.get("scanned_bytes") or 0)),
        })
    return [_long_term_item(record) for record in records if _curator_visible(record, now)]


# LLM: 保留真实版本供关系建议和替换目标复核；索引命中不当正文，正文只来自检索/点查回读的当前记录。
# 函数用途: 把一条 long-term 记录投影成正式窗口条目。
def _long_term_item(record: object) -> CuratorFormalMemoryInput:
    attrs = record.attributes if isinstance(record.attributes, dict) else {}
    return replace(_formal_item(
        authority_type="long_term",
        authority_id=str(record.entry_id or ""),
        authority_ref=f"memory/long_term/memory.jsonl#{record.entry_id}",
        content=str(record.content or ""),
        subject_key=str(attrs.get("subject_key") or ""),
        scope_type=str(attrs.get("scope_type") or ""),
        scope_key=str(attrs.get("scope_key") or ""),
        updated_at=str(record.updated_at or record.created_at or ""),
    ), authority_version=getattr(record, "version", None))


# LLM: 只看路由投影里的活跃 lesson（有界直读规范引用），有查询按词面排序、没有查询保留路由热度顺序；路由投影缺失=没有可路由 lesson，
#   symlink/损坏如实记缺口而不是猜空。正文只来自 marker 校验过的 lesson 文件。
# 函数用途: 选出与本批用户原话相关的正式 lesson。
def _lesson_items(
    repository: object,
    query: str,
    limit: int,
    errors: list[dict[str, object]],
) -> list[CuratorFormalMemoryInput]:
    if limit <= 0:
        return []
    routed = getattr(repository, "routed_refs", None)
    read_refs = getattr(repository, "read_refs", None)
    if not callable(routed) or not callable(read_refs):
        raise TypeError("formal lesson source lacks routed_refs()/read_refs()")
    status, refs = routed(limit=_ROUTED_LESSON_READ_MAX_ENTRIES)
    if status in {"symlink", "corrupt"}:
        errors.append({"code": "FORMAL_LESSON_ROUTING_UNREADABLE", "detail": str(status)})
        return []
    loaded = read_refs(refs) if refs else {}
    lessons = [lesson for ref in refs
               if (lesson := loaded.get(ref)) is not None and str(getattr(lesson, "status", "")) == "active"]
    if not query:
        return [_lesson_item(lesson) for lesson in lessons[:limit]]
    from ..retrieval.lexical import rank

    order = rank(query, [f"{lesson.subject_key} {lesson.content}" for lesson in lessons])
    return [_lesson_item(lessons[index]) for index in order[:limit]]


# 函数用途: 把一条正式 lesson 投影成正式窗口条目。
def _lesson_item(item: object) -> CuratorFormalMemoryInput:
    return _formal_item(
        authority_type="lesson",
        authority_id=str(item.lesson_id),
        authority_ref=str(item.path),
        content=str(item.content),
        subject_key=str(item.subject_key),
        scope_type=str(item.scope.get("scope_type") or ""),
        scope_key=str(item.scope.get("scope_key") or ""),
        updated_at=str(item.updated_at),
        authority_version=getattr(item, "revision", None),
    )


# LLM: HOT scope is inherited from its exact lesson; 规则只按规范 lesson_ref 批量直读核验活跃与精确 ID，
#   孤儿或非规范引用的 HOT 行排除而不是猜成全局。
# 函数用途: 投影正式 HOT 短规则并绑定 lesson scope。
def _hot_items(repository: object, lessons: object) -> list[CuratorFormalMemoryInput]:
    hot_reader = getattr(repository, "list", None)
    if not callable(hot_reader):
        raise TypeError("formal HOT source lacks list()")
    rules = list(hot_reader())
    if not rules:
        return []
    read_refs = getattr(lessons, "read_refs", None)
    if not callable(read_refs):
        raise TypeError("formal HOT source lacks lesson read_refs()")
    from .lessons import _parse_canonical_lesson_ref

    candidates: list[tuple[object, str]] = []
    for rule in rules:
        ref = str(getattr(rule, "lesson_ref", "") or "").strip()
        try:
            _path, _filename, fragment_id = _parse_canonical_lesson_ref(ref)
        except ValueError:
            continue
        if fragment_id is None or fragment_id == str(rule.lesson_id):
            candidates.append((rule, ref))
    loaded = read_refs(list(dict.fromkeys(ref for _rule, ref in candidates))) if candidates else {}
    result: list[CuratorFormalMemoryInput] = []
    for rule, ref in candidates:
        lesson = loaded.get(ref)
        if (lesson is None or str(getattr(lesson, "lesson_id", "")) != str(rule.lesson_id)
                or str(getattr(lesson, "status", "")) != "active"):
            continue
        result.append(
            _formal_item(
                authority_type="hot",
                authority_id=str(rule.hot_id),
                authority_ref=str(rule.lesson_ref),
                content=str(rule.rule),
                subject_key=str(lesson.subject_key),
                scope_type=str(lesson.scope.get("scope_type") or ""),
                scope_key=str(lesson.scope.get("scope_key") or ""),
                updated_at=str(rule.promoted_at),
            )
        )
    return result


# LLM: hash 来自原规范化全正文，宿主长度用于核对短预览是否完整；普通 provider 投影仍有界，不加入第二种版本或正文来源。
# 函数用途: 规范化一条正式记忆投影。
def _formal_item(
    *,
    authority_type: str,
    authority_id: str,
    authority_ref: str,
    content: str,
    subject_key: str,
    scope_type: str,
    scope_key: str,
    updated_at: str,
    authority_version: int | None = None,
) -> CuratorFormalMemoryInput:
    body = " ".join(str(content or "").split())
    return CuratorFormalMemoryInput(
        authority_type=authority_type,
        authority_id=authority_id,
        authority_ref=authority_ref,
        content_preview=body[:_ITEM_PREVIEW_CHARS],
        content_hash="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        subject_key=subject_key,
        scope_type=scope_type,
        scope_key=scope_key,
        updated_at=updated_at,
        authority_version=authority_version,
        content_chars=len(body),
    )


# LLM: Stable type/id ordering makes identical authority snapshots produce byte-identical model
# inputs across restarts and provider switches.
# 函数用途: 返回正式记忆投影的确定性排序键。
def _formal_sort_key(item: CuratorFormalMemoryInput) -> tuple[str, str]:
    priority = {"long_term": "0", "lesson": "1", "hot": "2"}
    return priority.get(item.authority_type, "9"), item.authority_id


__all__ = ["FORMAL_QUERY_MAX_CHARS", "CuratorFormalMemoryInput", "FormalMemorySource", "FormalMemoryWindow"]
