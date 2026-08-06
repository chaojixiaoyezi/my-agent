from __future__ import annotations

"""正式 lesson、确定性 routing index 与短小 HOT 的唯一正文仓库。"""

# LLM: lesson 正文只存在 memory/lessons/*.md；memory.jsonl、memory.md 和 HOT 不得复制详细正文。
# 模块用途: 审核后创建可复用教训、按元数据重建 routing，并从已接受 lesson 晋升短 HOT 规则。

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path, write_text_file_atomic_unlocked
from .candidate_models import MemoryCandidate, utc_now_iso
from .security import scan_memory_content

LESSON_SCHEMA_VERSION = "my-agent.lesson.v1"
HOT_SCHEMA_VERSION = "my-agent.hot-rule.v1"
_LESSON_META_PREFIX = "<!-- my-agent-lesson-meta:"
_HOT_META_PREFIX = "<!-- my-agent-hot-meta:"
_META_SUFFIX = " -->"
_MAX_HOT_RULE_CHARS = 300


# LLM: LessonRecord 元数据与同文件正文一起提交；routing 只是可重建导航投影。
# 类用途: 表示一个正式 lesson 文件。
@dataclass(frozen=True)
class LessonRecord:
    schema_version: str
    lesson_id: str
    candidate_id: str
    subject_key: str
    scope: dict[str, str]
    content: str
    occurrence_count: int
    evidence_groups: tuple[str, ...]
    source_task_ids: tuple[str, ...]
    source_run_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    path: str
    status: str = "active"

    # LLM: routing/HOT 只消费无正文 metadata，详细 lesson content 继续留在唯一 Markdown 正文。
    # 函数用途: 返回可写入索引和引用的 lesson 元数据。
    def metadata(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("content", None)
        return payload


# LLM: HOT metadata 和短规则同处 memory-hot.md，不另建第二事实账本。
# 类用途: 表示一条总是注入的短规则及其正式 lesson 引用。
@dataclass(frozen=True)
class HotRuleRecord:
    schema_version: str
    hot_id: str
    candidate_id: str
    lesson_id: str
    lesson_ref: str
    rule: str
    occurrence_count: int
    evidence_groups: tuple[str, ...]
    promoted_at: str


# LLM: Repository 只接受 approved Candidate；模型和 subagent 都不能直接写 lesson 文件。
# 类用途: 管理 lesson 文件及其确定性 routing/INDEX.md。
class LessonRepository:
    # LLM: 构造器绑定唯一 lesson 正文目录与派生 routing index，不扫描 long-term lesson。
    # 函数用途: 初始化正式 lesson 仓库和索引落点。
    def __init__(self, lessons_dir: str | Path, routing_index_path: str | Path) -> None:
        self.lessons_dir = Path(lessons_dir)
        self.routing_index_path = Path(routing_index_path)
        self.lessons_dir.mkdir(parents=True, exist_ok=True)
        self.routing_index_path.parent.mkdir(parents=True, exist_ok=True)

    # LLM: lesson 晋升要求审核、证据和跨任务/运行/日期重复；confidence 不能替代这些闸。
    # 函数用途: 幂等创建一个正式 lesson 并重建 routing index。
    def promote(
        self,
        candidate: MemoryCandidate,
        *,
        min_occurrences: int = 2,
    ) -> LessonRecord:
        _validate_lesson_candidate(candidate, min_occurrences=min_occurrences)
        lesson_id = "lesson-" + hashlib.sha256(
            candidate.candidate_id.encode("utf-8")
        ).hexdigest()[:20]
        filename = _lesson_filename(candidate.subject_key, lesson_id)
        path = self.lessons_dir / filename
        groups = tuple(sorted(_evidence_groups(candidate)))
        now = utc_now_iso()
        record = LessonRecord(
            schema_version=LESSON_SCHEMA_VERSION,
            lesson_id=lesson_id,
            candidate_id=candidate.candidate_id,
            subject_key=candidate.subject_key,
            scope=dict(candidate.scope),
            content=candidate.content.strip(),
            occurrence_count=candidate.occurrence_count,
            evidence_groups=groups,
            source_task_ids=tuple(candidate.source_task_ids),
            source_run_ids=tuple(candidate.source_run_ids),
            created_at=now,
            updated_at=now,
            path=f"memory/lessons/{filename}",
        )
        with locked_json_path(path):
            if path.exists():
                existing = _read_lesson(path)
                if existing.candidate_id != candidate.candidate_id:
                    raise RuntimeError("lesson path collision")
                record = existing
            else:
                write_text_file_atomic_unlocked(path, _render_lesson(record))
        self.rebuild_routing_index()
        return record

    # LLM: list 只接受带 v1 元数据的正式 lesson；legacy plain Markdown 必须先迁移。
    # 函数用途: 严格列出当前正式 lesson。
    def list(self) -> list[LessonRecord]:
        records = [_read_lesson(path) for path in sorted(self.lessons_dir.glob("*.md"))]
        return sorted(records, key=lambda item: (item.subject_key, item.lesson_id))

    # LLM: lesson_id 是 HOT 的唯一目标；不得按正文模糊选一个 lesson。
    # 函数用途: 精确查找正式 lesson。
    def get(self, lesson_id: str) -> LessonRecord:
        target = str(lesson_id or "").strip()
        for record in self.list():
            if record.lesson_id == target:
                return record
        raise KeyError(target)

    # LLM: routing 完全由 lesson metadata 确定性重建，模型不能重写索引全文。
    # 函数用途: 更新 memory/routing/INDEX.md。
    def rebuild_routing_index(self) -> Path:
        sections = [_routing_section(record) for record in self.list()]
        text = "# Memory Routing Index\n\n"
        text += (
            "此文件由正式 lesson 元数据确定性生成；它是导航索引，不是第二份教训正文。\n"
        )
        if sections:
            text += "\n" + "\n\n".join(sections) + "\n"
        with locked_json_path(self.routing_index_path):
            write_text_file_atomic_unlocked(self.routing_index_path, text)
        return self.routing_index_path


# LLM: HOT 只能引用 LessonRepository 中已存在的 active lesson，不能从一次候选直接抄入。
# 类用途: 管理 memory-hot.md 中的少量短规则。
class HotRuleRepository:
    # LLM: 构造器只绑定正式 HOT 文件；legacy 自由文本由 migration 处理而非运行时兼容。
    # 函数用途: 初始化当前 owner 的高频短规则仓库。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # LLM: HOT 需要 approved hot candidate、默认三次观察和至少两个独立任务/运行/日期证据。
    # 函数用途: 将一个短规则绑定到正式 lesson 并幂等写入 HOT。
    def promote(
        self,
        candidate: MemoryCandidate,
        *,
        lesson: LessonRecord,
        min_occurrences: int = 3,
    ) -> HotRuleRecord:
        _validate_hot_candidate(candidate, lesson, min_occurrences=min_occurrences)
        hot_id = "hot-" + hashlib.sha256(candidate.candidate_id.encode("utf-8")).hexdigest()[:20]
        record = HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id=hot_id,
            candidate_id=candidate.candidate_id,
            lesson_id=lesson.lesson_id,
            lesson_ref=lesson.path,
            rule=candidate.content.strip(),
            occurrence_count=candidate.occurrence_count,
            evidence_groups=tuple(sorted(_evidence_groups(candidate))),
            promoted_at=utc_now_iso(),
        )
        with locked_json_path(self.path):
            existing = _read_hot_records(self.path)
            prior = next((item for item in existing if item.hot_id == hot_id), None)
            if prior is not None:
                if prior.candidate_id != candidate.candidate_id or prior.rule != record.rule:
                    raise RuntimeError("HOT id collision")
                return prior
            write_text_file_atomic_unlocked(self.path, _render_hot([*existing, record]))
        return record

    # LLM: 只解析程序 marker；自由文本 HOT 被视为 legacy，迁移前不能静默混入正式规则。
    # 函数用途: 列出正式 HOT 规则。
    def list(self) -> list[HotRuleRecord]:
        return _read_hot_records(self.path)


# LLM: lesson 候选必须是 approved 且落点为 lesson，拒绝将长期事实换个文件名写入。
# 函数用途: 验证 lesson 晋升门槛。
def _validate_lesson_candidate(candidate: MemoryCandidate, *, min_occurrences: int) -> None:
    if candidate.status != "approved":
        raise ValueError("lesson candidate must be approved")
    if candidate.candidate_type != "lesson" or candidate.promotion_target != "lesson":
        raise ValueError("candidate is not a lesson promotion")
    if candidate.conflicts_with:
        raise ValueError("lesson candidate has unresolved conflicts")
    if not _has_evidence(candidate):
        raise ValueError("lesson candidate lacks evidence")
    groups = _evidence_groups(candidate)
    explicit = candidate.origin == "user_explicit" and bool(candidate.source_message_refs)
    if not explicit and (
        candidate.occurrence_count < max(2, min_occurrences) or len(groups) < 2
    ):
        raise ValueError("lesson requires repeated independent evidence")
    scan = scan_memory_content(candidate.content)
    if not scan.safe:
        raise ValueError("lesson content failed memory threat scan")


# LLM: HOT candidate 目标必须精确等于正式 lesson_id；主题相似或较新时间都不能代替引用。
# 函数用途: 验证 HOT 晋升门槛。
def _validate_hot_candidate(
    candidate: MemoryCandidate,
    lesson: LessonRecord,
    *,
    min_occurrences: int,
) -> None:
    if candidate.status != "approved":
        raise ValueError("HOT candidate must be approved")
    if candidate.candidate_type != "hot_rule" or candidate.promotion_target != "hot":
        raise ValueError("candidate is not a HOT promotion")
    if candidate.target_entry_id != lesson.lesson_id:
        raise ValueError("HOT candidate must target an exact formal lesson_id")
    if candidate.conflicts_with:
        raise ValueError("HOT candidate has unresolved conflicts")
    if len(candidate.content.strip()) > _MAX_HOT_RULE_CHARS:
        raise ValueError("HOT rule is too long; keep details in lesson")
    groups = _evidence_groups(candidate) | set(lesson.evidence_groups)
    if candidate.occurrence_count < max(3, min_occurrences) or len(groups) < 2:
        raise ValueError("HOT requires repeated evidence from at least two groups")
    if lesson.status != "active":
        raise ValueError("HOT target lesson is not active")
    scan = scan_memory_content(candidate.content)
    if not scan.safe:
        raise ValueError("HOT rule failed memory threat scan")


# LLM: 证据组只来自结构化 task/run/date，不用相似正文把一次经历伪装成多次。
# 函数用途: 计算 lesson/HOT 的独立证据来源。
def _evidence_groups(candidate: MemoryCandidate) -> set[str]:
    groups = {f"task:{item}" for item in candidate.source_task_ids if item}
    groups.update(f"run:{item}" for item in candidate.source_run_ids if item)
    for ref in [*candidate.source_message_refs, *candidate.source_tool_refs, *candidate.evidence_refs]:
        created = str(ref.get("created_at") or ref.get("observed_at") or "")
        if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", created):
            groups.add("date:" + created[:10])
    return groups


# LLM: 证据只看结构化引用/ID 是否存在，不把 confidence 当证据。
# 函数用途: 判断 lesson 是否至少有一类 provenance。
def _has_evidence(candidate: MemoryCandidate) -> bool:
    return bool(
        candidate.evidence_refs
        or candidate.source_message_refs
        or candidate.source_tool_refs
        or candidate.source_artifact_refs
        or candidate.source_task_ids
        or candidate.source_run_ids
    )


# LLM: 文件名由稳定 subject/id 生成，不把任意模型路径写进 owner 目录。
# 函数用途: 生成安全 lesson 文件名。
def _lesson_filename(subject_key: str, lesson_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(subject_key or "lesson")).strip("-.")
    return f"{(slug or 'lesson')[:80]}-{lesson_id[-8:]}.md"


# LLM: metadata JSON 不含完整证据正文；正式详细内容只渲染一次。
# 函数用途: 生成 lesson Markdown。
def _render_lesson(record: LessonRecord) -> str:
    metadata = json.dumps(record.metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    title = _one_line(record.subject_key or record.lesson_id, 120)
    return (
        f"{_LESSON_META_PREFIX}{metadata}{_META_SUFFIX}\n"
        f"# {title}\n\n"
        f"{record.content.strip()}\n"
    )


# LLM: 读取元数据 marker 后校验 path/content；缺 marker 的 legacy lesson 必须先迁移。
# 函数用途: 从 Markdown 恢复 LessonRecord。
def _read_lesson(path: Path) -> LessonRecord:
    text = path.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    if not first.startswith(_LESSON_META_PREFIX) or not first.endswith(_META_SUFFIX):
        raise ValueError(f"legacy lesson requires migration: {path.name}")
    payload = json.loads(first[len(_LESSON_META_PREFIX) : -len(_META_SUFFIX)])
    if not isinstance(payload, dict) or payload.get("schema_version") != LESSON_SCHEMA_VERSION:
        raise ValueError(f"invalid lesson metadata: {path.name}")
    content = rest.partition("\n\n")[2].strip()
    return LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id=str(payload.get("lesson_id") or ""),
        candidate_id=str(payload.get("candidate_id") or ""),
        subject_key=str(payload.get("subject_key") or ""),
        scope=dict(payload.get("scope") or {}),
        content=content,
        occurrence_count=int(payload.get("occurrence_count") or 0),
        evidence_groups=tuple(str(item) for item in payload.get("evidence_groups") or []),
        source_task_ids=tuple(str(item) for item in payload.get("source_task_ids") or []),
        source_run_ids=tuple(str(item) for item in payload.get("source_run_ids") or []),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        path=str(payload.get("path") or ""),
        status=str(payload.get("status") or "active"),
    )


# LLM: routing 只包含短 topic、结构化 scope 和 authority_path，不复制 lesson 正文。
# 函数用途: 生成一个确定性索引段。
def _routing_section(record: LessonRecord) -> str:
    keywords = ", ".join(_trigger_keywords(record.subject_key))
    scope = f"{record.scope.get('scope_type', '')}:{record.scope.get('scope_key', '')}"
    return "\n".join(
        (
            f"## lesson.{record.lesson_id}",
            f"topic: {_one_line(record.content, 140)}",
            f"trigger_keywords: {keywords}",
            f"authority_path: {record.path}",
            "inject_mode: on_hit",
            f"scope: {scope}",
            "stale_check: 按文件修改时间提示陈旧，当前事实和代码优先",
        )
    )


# LLM: trigger keywords 只由稳定 subject_key 机械拆分，模型不能借重建索引注入新规则。
# 函数用途: 生成路由关键词。
def _trigger_keywords(subject_key: str) -> list[str]:
    values = [part for part in re.split(r"[.:/_-]+", subject_key) if len(part) >= 2]
    return list(dict.fromkeys([subject_key, *values]))[:12]


# LLM: HOT 文件完全由 marker records 渲染，详细解释只保留 lesson 链接。
# 函数用途: 生成 memory-hot.md。
def _render_hot(records: list[HotRuleRecord]) -> str:
    lines = [
        "# Memory HOT",
        "",
        "这里只放经过正式 lesson 和重复证据晋升的短规则；详细说明见对应 lesson。",
        "",
    ]
    for record in sorted(records, key=lambda item: item.hot_id):
        metadata = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lines.append(f"{_HOT_META_PREFIX}{metadata}{_META_SUFFIX}")
        lines.append(f"- {record.rule}（详见 `{record.lesson_ref}`）")
    return "\n".join(lines).rstrip() + "\n"


# LLM: HOT 只读取程序 marker，marker 中 rule 与下一行展示不再分别作为两个权威输入。
# 函数用途: 恢复正式 HOT records。
def _read_hot_records(path: Path) -> list[HotRuleRecord]:
    if not path.exists():
        return []
    records: list[HotRuleRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith(_HOT_META_PREFIX) or not line.endswith(_META_SUFFIX):
            continue
        payload = json.loads(line[len(_HOT_META_PREFIX) : -len(_META_SUFFIX)])
        if not isinstance(payload, dict) or payload.get("schema_version") != HOT_SCHEMA_VERSION:
            raise ValueError("invalid HOT metadata")
        records.append(
            HotRuleRecord(
                schema_version=HOT_SCHEMA_VERSION,
                hot_id=str(payload.get("hot_id") or ""),
                candidate_id=str(payload.get("candidate_id") or ""),
                lesson_id=str(payload.get("lesson_id") or ""),
                lesson_ref=str(payload.get("lesson_ref") or ""),
                rule=str(payload.get("rule") or ""),
                occurrence_count=int(payload.get("occurrence_count") or 0),
                evidence_groups=tuple(str(item) for item in payload.get("evidence_groups") or []),
                promoted_at=str(payload.get("promoted_at") or ""),
            )
        )
    return records


# LLM: 索引/HOT 展示字段必须单行有界，正文换行不能改变文档结构。
# 函数用途: 清洗一个短展示值。
def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


__all__ = [
    "HOT_SCHEMA_VERSION",
    "LESSON_SCHEMA_VERSION",
    "HotRuleRecord",
    "HotRuleRepository",
    "LessonRecord",
    "LessonRepository",
]
