from __future__ import annotations

"""正式 Memory 的结构化 scope 过滤与统一 Prompt 投影记录。"""

# LLM: Recall 只能消费 active long-term、正式 lesson 和正式 HOT；Candidate/Daily/Ops 不得从这里进入 Prompt。
# 模块用途: 从运行时结构化字段解析适用范围，并把三个正式来源投影成同一种 MemoryRecord。

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .jsonl import MemoryRecord
from .lessons import HotRuleRepository, LessonRecord, LessonRepository, hot_records_within_budget


# LLM: 活跃范围只来自 host 的 typed IDs，不解析用户自然语言或 applies_when 文本。
# 类用途: 保存本轮允许召回的 scope_type/scope_key 精确集合。
@dataclass(frozen=True)
class MemoryRecallScope:
    keys: tuple[tuple[str, str], ...]

    # LLM: personal/global 是 owner 内固定范围；其余范围必须由 task/runtime 的结构化字段显式提供。
    # 函数用途: 从 task_id 和 task_attributes 构造本轮精确适用范围。
    @classmethod
    def from_runtime(
        cls,
        *,
        task_id: str = "",
        task_attributes: Mapping[str, object] | None = None,
    ) -> MemoryRecallScope:
        pairs: list[tuple[str, str]] = [("global", "global"), ("personal", "personal")]
        attrs = task_attributes if isinstance(task_attributes, Mapping) else {}
        _append_runtime_identity(pairs, "project", task_id)
        direct_fields = {
            "company": "company_id",
            "project": "project_id",
            "task_class": "task_class",
            "session": "session_id",
            "temporary": "temporary_scope_key",
        }
        for scope_type, field in direct_fields.items():
            _append_runtime_identity(pairs, scope_type, attrs.get(field))
        _append_declared_scopes(pairs, attrs.get("memory_scope"))
        _append_declared_scopes(pairs, attrs.get("memory_scopes"))
        return cls(tuple(dict.fromkeys(pairs)))

    # LLM: Scope 匹配是精确 typed-key 比较；时间较新、文字相似或模型猜测都不能扩大范围。
    # 函数用途: 判断一个正式记录的 scope 是否适用于本轮。
    def allows(self, scope_type: object, scope_key: object) -> bool:
        pair = (str(scope_type or "").strip().lower(), str(scope_key or "").strip())
        return bool(pair[0] and pair[1] and pair in self.keys)


# LLM: 长期事实必须携带 promotion 写入的 typed scope；legacy 无范围记录等待迁移，不能默认全局召回。
# 函数用途: 过滤 active long-term 记录。
def long_term_record_matches_scope(record: MemoryRecord, scope: MemoryRecallScope) -> bool:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    return scope.allows(attributes.get("scope_type"), attributes.get("scope_key"))


# LLM: Routing 的文件读取票据只决定“哪些 lesson 被读过”；正文仍只从 LessonRepository 的正式文件恢复。
# 函数用途: 将本轮已成功路由读取且 scope 匹配的 lesson 转成 Prompt MemoryRecord。
def routed_lesson_records(
    repository: LessonRepository,
    *,
    read_paths: Iterable[str],
    scope: MemoryRecallScope,
    stale_days: float = 7.0,
    now: datetime | None = None,
) -> list[MemoryRecord]:
    selected = {str(path or "").strip() for path in read_paths if str(path or "").strip()}
    if not selected:
        return []
    current = now or datetime.now(timezone.utc)
    records: list[MemoryRecord] = []
    for lesson in repository.list():
        if lesson.path not in selected or not _lesson_scope_allowed(lesson, scope):
            continue
        content = lesson.content.strip() + _lesson_age_caveat(lesson, current, stale_days)
        records.append(_lesson_memory_record(lesson, content))
    return records


# LLM: HOT 每轮可读，但必须解析 marker、绑定现有 lesson 并继承其 scope；自由 Markdown 不具正式权威。
# 函数用途: 将适用的正式 HOT 短规则转成 Prompt MemoryRecord。
def hot_memory_records(
    hot: HotRuleRepository,
    lessons: LessonRepository,
    *,
    scope: MemoryRecallScope,
) -> list[MemoryRecord]:
    lesson_by_id = {lesson.lesson_id: lesson for lesson in lessons.list()}
    records: list[MemoryRecord] = []
    # 注入预算硬顶：全量注入但只带最近活跃的规则（活跃度排序，超出截断是正常行为）。
    for rule in hot_records_within_budget(hot.list()):
        lesson = lesson_by_id.get(rule.lesson_id)
        if lesson is None or not _lesson_scope_allowed(lesson, scope):
            continue
        records.append(
            MemoryRecord(
                role="system",
                content=rule.rule,
                kind="hot",
                created_at=_iso_epoch(rule.promoted_at),
                updated_at=_iso_epoch(rule.promoted_at),
                entry_id=rule.hot_id,
                source=f"lesson:{lesson.lesson_id}",
                attributes={
                    "origin": "reviewed",
                    "subject_key": lesson.subject_key,
                    "scope_type": lesson.scope.get("scope_type", ""),
                    "scope_key": lesson.scope.get("scope_key", ""),
                    "authority_ref": rule.lesson_ref,
                },
            )
        )
    return records


# LLM: 只接收显式 {scope_type, scope_key} 对象或对象数组，不从任意字符串推断 scope。
# 显式声明与运行时身份共用同一 canonical 合同（raw 归一、task 别名归一到 project、
# 嵌套/空 remainder/固定键错值 fail-closed），不再走弱校验放行坏值。
# 函数用途: 合并调用方声明的附加 Memory 范围。
def _append_declared_scopes(
    pairs: list[tuple[str, str]],
    value: object,
) -> None:
    if isinstance(value, Mapping):
        if "scope_type" in value or "scope_key" in value:
            _append_declared_scope(pairs, value.get("scope_type"), value.get("scope_key"))
            return
        for scope_type, keys in value.items():
            values = keys if isinstance(keys, (list, tuple, set)) else (keys,)
            for scope_key in values:
                _append_declared_scope(pairs, scope_type, scope_key)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _append_declared_scopes(pairs, item)


# LLM: 与写入侧共用 canonical 合同；坏值不扩大召回范围（fail-closed 静默跳过）。
# 函数用途: 将一条显式声明 scope 归一为合法 keys 后追加。
def _append_declared_scope(
    pairs: list[tuple[str, str]],
    scope_type: object,
    scope_key: object,
) -> None:
    from .scope_contract import canonical_scope_key, scope_key_aliases

    kind = str(scope_type or "").strip().lower()
    key = str(scope_key or "").strip()
    if not key:
        return
    try:
        canonical = canonical_scope_key(kind, key)
    except ValueError:
        return  # 嵌套/空 remainder/固定键错值 → fail-closed 不追加
    for alias in scope_key_aliases(kind, canonical):
        _append_scope_pair(pairs, kind, alias)


# LLM: 与写入侧共用 scope-key canonical 合同：raw 与 typed 落到同一键，
# 不再对 typed 值重复加前缀（双前缀身份分裂债务），project 经别名补出 task:<id> 兼容。
# 函数用途: 添加一个运行时身份对应的合法 scope keys。
def _append_runtime_identity(
    pairs: list[tuple[str, str]],
    scope_type: str,
    value: object,
) -> None:
    from .scope_contract import canonical_scope_key, scope_key_aliases

    key = str(value or "").strip()
    if not key:
        return
    try:
        canonical = canonical_scope_key(scope_type, key)
    except ValueError:
        return  # 坏值不扩大召回范围（fail-closed 静默跳过）
    for alias in scope_key_aliases(scope_type, canonical):
        _append_scope_pair(pairs, scope_type, alias)


# LLM: 只接受 Candidate schema 定义的 scope 类型和有限稳定 key；坏值不能扩大成 global。
# 函数用途: 校验并追加一个 scope pair。
def _append_scope_pair(
    pairs: list[tuple[str, str]],
    scope_type: object,
    scope_key: object,
) -> None:
    kind = str(scope_type or "").strip().lower()
    key = str(scope_key or "").strip()
    if kind not in {"global", "personal", "company", "project", "task_class", "session", "temporary"}:
        return
    if not key or len(key) > 160 or any(char.isspace() for char in key):
        return
    if kind == "global" and key != "global":
        return
    pairs.append((kind, key))


# LLM: lesson scope 与 long-term 使用完全相同的 typed matcher，不读取正文里的适用条件。
# 函数用途: 判断正式 lesson 是否适用于本轮。
def _lesson_scope_allowed(lesson: LessonRecord, scope: MemoryRecallScope) -> bool:
    return scope.allows(lesson.scope.get("scope_type"), lesson.scope.get("scope_key"))


# LLM: stale caveat 是非权威提示；它不修改 lesson 文件，也不以文件名猜时间。
# 函数用途: 为陈旧 lesson 增加与当前事实冲突时的优先级说明。
def _lesson_age_caveat(lesson: LessonRecord, now: datetime, stale_days: float) -> str:
    if stale_days <= 0:
        return ""
    updated = _iso_datetime(lesson.updated_at)
    if updated is None:
        return "\n[memory-age-caveat] 该 lesson 更新时间不可验证；与当前事实冲突时以当前事实为准。"
    age_days = max(0, int((now - updated).total_seconds() // 86_400))
    if age_days < stale_days:
        return ""
    return (
        f"\n[memory-age-caveat] 该 lesson 约 {age_days} 天未更新，是历史经验；"
        "与当前代码、文件或工具事实冲突时以当前事实为准。"
    )


# LLM: lesson 的 Prompt 记录只保存短 authority_ref 和 typed scope，不复制候选证据或内部审核字段。
# 函数用途: 构造正式 lesson 的统一 MemoryRecord 投影。
def _lesson_memory_record(lesson: LessonRecord, content: str) -> MemoryRecord:
    return MemoryRecord(
        role="system",
        content=content,
        kind="lesson",
        created_at=_iso_epoch(lesson.created_at),
        updated_at=_iso_epoch(lesson.updated_at),
        entry_id=lesson.lesson_id,
        source=f"candidate:{lesson.candidate_id}",
        attributes={
            "origin": "reviewed",
            "subject_key": lesson.subject_key,
            "scope_type": lesson.scope.get("scope_type", ""),
            "scope_key": lesson.scope.get("scope_key", ""),
            "authority_ref": lesson.path,
        },
    )


# LLM: 持久时间必须带 timezone；坏时间只降为 0，不参与“较新即真实”的判断。
# 函数用途: 解析 ISO 时间供确定性排序和陈旧提示使用。
def _iso_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


# LLM: epoch 仅是排序投影，不能改变正式记录里的原始 ISO 时间。
# 函数用途: 将合法 ISO 时间转换为 Unix 时间戳。
def _iso_epoch(value: object) -> float:
    parsed = _iso_datetime(value)
    return parsed.timestamp() if parsed is not None else 0.0


__all__ = [
    "MemoryRecallScope",
    "hot_memory_records",
    "long_term_record_matches_scope",
    "routed_lesson_records",
]
