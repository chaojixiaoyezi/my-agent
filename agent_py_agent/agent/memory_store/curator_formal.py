from __future__ import annotations

"""为后台 Curator 提供有界、只读的现有正式记忆上下文。"""

# LLM: 只暴露当前 owner 的 active 正式材料，不向模型授予仓库修改方法；关系建议所需原版本/正文长度只留宿主。
# 普通 Curator 投影不随可选增强改变；同步核对 curator_inputs 与关系建议的完整性/失效测试。
# 模块用途: 将 long-term、lesson 和 HOT 投影成短预览，供冲突判断而不复制派生索引或旧版本。

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

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


# LLM: Store objects remain host-side dependencies; read() returns plain immutable projections
# and therefore cannot expose remember/persona/file tools to the background model.
# 类用途: 从三个正式正文仓库读取 active 记录并执行统一字符预算。
@dataclass(frozen=True)
class FormalMemorySource:
    long_term: object
    lessons: object
    hot: object

    # LLM: A deterministic item/character cap prevents formal memory growth from defeating the
    # Curator's total input budget.
    # 函数用途: 返回按正式类型和稳定 ID 排序的有界上下文。
    def read(self, *, max_items: int = 32, max_chars: int = 6_000) -> tuple[CuratorFormalMemoryInput, ...]:
        items = [
            *_long_term_items(self.long_term),
            *_lesson_items(self.lessons),
            *_hot_items(self.hot, self.lessons),
        ]
        selected: list[CuratorFormalMemoryInput] = []
        used = 0
        for item in sorted(items, key=_formal_sort_key):
            size = len(json.dumps(item.to_model(), ensure_ascii=False, sort_keys=True))
            if selected and (len(selected) >= max_items or used + size > max_chars):
                break
            if size > max_chars and not selected:
                continue
            selected.append(item)
            used += size
        return tuple(selected)


# LLM: JsonlMemory.all() 已物化 active 记录并排除替换/删除旧版；保留其真实版本供关系建议复核，不把索引命中当正文。
# 函数用途: 投影 active long-term 记录。
def _long_term_items(repository: object) -> list[CuratorFormalMemoryInput]:
    reader = getattr(repository, "all", None)
    if not callable(reader):
        raise TypeError("formal long-term source lacks all()")
    result: list[CuratorFormalMemoryInput] = []
    for record in reader():
        attrs = record.attributes if isinstance(record.attributes, dict) else {}
        result.append(
            replace(_formal_item(
                authority_type="long_term",
                authority_id=str(record.entry_id or ""),
                authority_ref=f"memory/long_term/memory.jsonl#{record.entry_id}",
                content=str(record.content or ""),
                subject_key=str(attrs.get("subject_key") or ""),
                scope_type=str(attrs.get("scope_type") or ""),
                scope_key=str(attrs.get("scope_key") or ""),
                updated_at=str(record.updated_at or record.created_at or ""),
            ), authority_version=getattr(record, "version", None))
        )
    return result


# LLM: Lesson content comes only from marker-validated LessonRepository files; routing index is
# not read as a second authority.
# 函数用途: 投影正式 lesson 记录。
def _lesson_items(repository: object) -> list[CuratorFormalMemoryInput]:
    reader = getattr(repository, "list", None)
    if not callable(reader):
        raise TypeError("formal lesson source lacks list()")
    return [
        _formal_item(
            authority_type="lesson",
            authority_id=str(item.lesson_id),
            authority_ref=str(item.path),
            content=str(item.content),
            subject_key=str(item.subject_key),
            scope_type=str(item.scope.get("scope_type") or ""),
            scope_key=str(item.scope.get("scope_key") or ""),
            updated_at=str(item.updated_at),
        )
        for item in reader()
        if str(getattr(item, "status", "active")) == "active"
    ]


# LLM: HOT scope is inherited from its exact lesson_id; an orphan HOT row is excluded instead
# of being guessed global.
# 函数用途: 投影正式 HOT 短规则并绑定 lesson scope。
def _hot_items(repository: object, lessons: object) -> list[CuratorFormalMemoryInput]:
    hot_reader = getattr(repository, "list", None)
    lesson_reader = getattr(lessons, "list", None)
    if not callable(hot_reader) or not callable(lesson_reader):
        raise TypeError("formal HOT source lacks canonical repositories")
    lesson_by_id = {str(item.lesson_id): item for item in lesson_reader()}
    result: list[CuratorFormalMemoryInput] = []
    for item in hot_reader():
        lesson = lesson_by_id.get(str(item.lesson_id))
        if lesson is None or str(getattr(lesson, "status", "")) != "active":
            continue
        result.append(
            _formal_item(
                authority_type="hot",
                authority_id=str(item.hot_id),
                authority_ref=str(item.lesson_ref),
                content=str(item.rule),
                subject_key=str(lesson.subject_key),
                scope_type=str(lesson.scope.get("scope_type") or ""),
                scope_key=str(lesson.scope.get("scope_key") or ""),
                updated_at=str(item.promoted_at),
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
        content_chars=len(body),
    )


# LLM: Stable type/id ordering makes identical authority snapshots produce byte-identical model
# inputs across restarts and provider switches.
# 函数用途: 返回正式记忆投影的确定性排序键。
def _formal_sort_key(item: CuratorFormalMemoryInput) -> tuple[str, str]:
    priority = {"long_term": "0", "lesson": "1", "hot": "2"}
    return priority.get(item.authority_type, "9"), item.authority_id


__all__ = ["CuratorFormalMemoryInput", "FormalMemorySource"]
