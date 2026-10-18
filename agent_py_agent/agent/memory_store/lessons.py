# LLM: C仅存IO策略，原lesson/HOT验证/锁/预算/排序/渲染门外；正文→routing与HOT→demote逐原末写保partial。
from __future__ import annotations

"""正式 lesson、确定性 routing index 与短小 HOT 的唯一正文仓库。"""

# LLM: lesson 正文只存在 memory/lessons/*.md；memory.jsonl、memory.md 和 HOT 不得复制详细正文。
# 模块用途: 审核后创建可复用教训、按元数据重建 routing，并从已接受 lesson 晋升短 HOT 规则。

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.json_io import jsonl_lines, locked_json_path, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import ensure_private_dir
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from .candidate_models import MemoryCandidate, utc_now_iso
from .security import scan_memory_content

LESSON_SCHEMA_VERSION = "my-agent.lesson.v1"
HOT_SCHEMA_VERSION = "my-agent.hot-rule.v1"
_LESSON_META_PREFIX = "<!-- my-agent-lesson-meta:"
_HOT_META_PREFIX = "<!-- my-agent-hot-meta:"
_META_SUFFIX = " -->"
# 单条热门规则元数据最多 300 字符：防止超长规则撑爆内存。
_MAX_HOT_RULE_CHARS = 300
# 注入预算：HOT 全量注入的硬顶（字符≈token，中文 1:1）。超出的按活跃度截断，
# 冷条目在写入时降级回 lessons（lesson 正文保留，路由仍可命中，零信息丢失）。
HOT_MAX_INJECT_CHARS = 5000
# 可重建导航投影（memory/routing/INDEX.md）的上限口径（设计 §3.5.3；参考 Claude Code 200 行/25KB 量级）：
# 文件换行数上限：≤ 200 行。
_ROUTING_INDEX_MAX_LINE_COUNT = 200
# UTF-8 总字节数上限：≤ 25600（25KiB）；字符数 ≤ 字节数（中文 1 字符≈3 字节，字节口径更严）。
# 超限时按最近更新优先保留、其余省略；它不是全部历史正文的存储上限——lesson 正文完整留在 memory/lessons/。
_ROUTING_INDEX_MAX_BYTES = 25 * 1024


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


# LLM: 只在已取得正式lesson记录后使用；异常携带本轮写入事实，不把派生失败冒充审核失败。
# 类用途: 保留正式正文成功但导航索引待恢复的回执。
class LessonRoutingPendingError(RuntimeError):
    # LLM: reference来自已提交目标；changed来自目标锁内实际写入，不解析异常文字。
    # 函数用途: 向Promotion保留具体正式引用及本次变更事实。
    def __init__(self, record: LessonRecord, changed: bool):
        super().__init__("LESSON_ROUTING_PENDING")
        self.promotion_ref = record.path + "#" + record.lesson_id
        self.formal_memory_changed = changed


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


# LLM: Repository 只接受 approved Candidate；模型和 subagent 都不能直接写 lesson 文件。lesson 正文与带正文摘录的 INDEX.md
#   都走私有原子写（0600，目录 0700），已有 0644 的旧文件下次写入即收紧；改动同步 test_memory_file_permissions。
# 类用途: 管理 lesson 文件及其确定性 routing/INDEX.md。
# LLM: C只保存IO策略，正文与routing仍同原权威文件；原审查/证据/幂等和partial不可绕过。
# 类用途: 管理原正式lesson和派生routing，C写口不新建业务入口。
class LessonRepository:
    # LLM: 构造器绑定唯一 lesson 正文目录与派生 routing index，不扫描 long-term lesson。
    # 函数用途: 初始化正式 lesson 仓库和索引落点。
    # LLM: 默认原ensure顺序；C两准确目录出生消费原scope，无source/callback字段。
    # 函数用途: 绑定原lesson/routing路径并按原顺序准备目录。
    def __init__(self, lessons_dir: str | Path, routing_index_path: str | Path, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self.lessons_dir = Path(lessons_dir)
        self.routing_index_path = Path(routing_index_path)
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        if self.coordinator_io:
            _coordinator_lesson_write(lambda: ensure_private_dir(self.lessons_dir))
            _coordinator_lesson_write(lambda: ensure_private_dir(self.routing_index_path.parent))
        else:
            ensure_private_dir(self.lessons_dir)
            ensure_private_dir(self.routing_index_path.parent)

    # LLM: 兼容既有调用方只取正式记录；权限、幂等及写入事实仍由同一锁内实现裁决。
    # 函数用途: 创建或复用一个正式lesson并重建派生索引。
    def promote(self, candidate: MemoryCandidate, *, min_occurrences: int = 2) -> LessonRecord:
        return self.promote_with_receipt(candidate, min_occurrences=min_occurrences)[0]

    # LLM: lesson目标锁内记录实际创建事实；派生routing重建不是正式正文变更，供Promotion准确投影。
    # 函数用途: 返回lesson和本轮是否真正创建，避免重试虚增提交。
    # LLM: C仅原锁FD和已render正文末写入门，原validate/read/collision/routing在外；正文已成功不洗后继失败。
    # 函数用途: 正式晋升或复用lesson，再沿原链重建routing。
    def promote_with_receipt(
        self,
        candidate: MemoryCandidate,
        *,
        min_occurrences: int = 2,
    ) -> tuple[LessonRecord, bool]:
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
        with locked_json_path(path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write})):
            changed = not path.exists()
            if not changed:
                record = _read_lesson(path)
            else:
                _write_lesson_text(path, _render_lesson(record), **({} if not self.coordinator_io else {"coordinator_io": True}))
            if record.candidate_id != candidate.candidate_id:
                raise RuntimeError("lesson path collision")
        _rebuild_committed_lesson_routing(self, record, changed)
        return record, changed

    # LLM: 无 v1 元数据 marker 的 .md 有两种合法语义,以迁移 marker 是否已建立区分:
    # 未迁移(升级前/全新 home)→ legacy,读路径容忍跳过,由 MemoryMigrationService 自愈,
    # 绝不因单个旧文件拖垮整表;迁移已 complete(正式 v2 区建立)后仍出现无 marker 文件
    # → 损坏,必须 fail-closed 不可静默吞(防模型拿到被破坏的正式记忆)。有 marker 但
    # metadata 非法的文件两种模式都抛(与迁移 CORRUPT_LESSON_MARKER 同一 fail-closed 约定)。
    # 函数用途: 按迁移状态容错或严格列出当前正式 lesson。
    def list(self) -> list[LessonRecord]:
        tolerate_legacy = not (self.lessons_dir.parent / "migration.json").exists()
        records: list[LessonRecord] = []
        for path in sorted(self.lessons_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            first, _, _ = text.partition("\n")
            if not first.startswith(_LESSON_META_PREFIX) or not first.endswith(_META_SUFFIX):
                if tolerate_legacy:
                    continue
                raise ValueError(f"legacy lesson requires migration: {path.name}")
            records.append(_read_lesson(path))
        return sorted(records, key=lambda item: (item.subject_key, item.lesson_id))

    # LLM: lesson_id 是 HOT 的唯一目标；不得按正文模糊选一个 lesson。
    # 函数用途: 精确查找正式 lesson。
    def get(self, lesson_id: str) -> LessonRecord:
        target = str(lesson_id or "").strip()
        for record in self.list():
            if record.lesson_id == target:
                return record
        raise KeyError(target)

    # LLM: routing 完全由 lesson metadata 确定性重建，模型不能重写索引全文。索引含正文摘录（topic），按私有原子写。
    # 函数用途: 以仅本人可读写的权限更新 memory/routing/INDEX.md。
    # LLM: 原list/render在Root外，C准确routing锁出生与现成text末写入门，普通错误仍由原caller转pending。
    # 函数用途: 从原lesson事实确定性重建同一个routing文件。
    def rebuild_routing_index(self) -> Path:
        text = _render_routing_index(self.list())
        with locked_json_path(self.routing_index_path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write})):
            _write_lesson_text(self.routing_index_path, text, **({} if not self.coordinator_io else {"coordinator_io": True}))
        return self.routing_index_path


# LLM: HOT 只能引用 LessonRepository 中已存在的 active lesson，不能从一次候选直接抄入。HOT 文件装规则正文，晋升与降级重写都走
#   私有原子写（0600，目录 0700），已有 0644 的旧文件下次写入即收紧；改动同步 test_memory_file_permissions。
# 类用途: 管理 memory-hot.md 中的少量短规则。
# LLM: C只保存IO策略，原HOT预算/排序/首次写与第二demote都保持，各固定末写单独末核。
# 类用途: 管理原HOT文件，不跳过原降级治理。
class HotRuleRepository:
    # LLM: 构造器只绑定正式 HOT 文件；legacy 自由文本由 migration 处理而非运行时兼容。
    # 函数用途: 初始化当前 owner 的高频短规则仓库。
    # LLM: 默认原目录初始化；C只将准确目录出生交当前scope，不缓存来源。
    # 函数用途: 绑定原HOT路径和可选本地IO策略。
    def __init__(self, path: str | Path, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self.path = Path(path)
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        if self.coordinator_io:
            _coordinator_lesson_write(lambda: ensure_private_dir(self.path.parent))
        else:
            ensure_private_dir(self.path.parent)

    # LLM: 旧接口只返回记录，写入门槛与真实副作用事实全部委托同一实现。
    # 函数用途: 兼容调用者把短规则绑定到正式lesson。
    def promote(self, candidate: MemoryCandidate, *, lesson: LessonRecord, min_occurrences: int = 3) -> HotRuleRecord:
        return self.promote_with_receipt(candidate, lesson=lesson, min_occurrences=min_occurrences)[0]

    # LLM: HOT锁内识别幂等复用，返回真实写入事实；不用锁外exists或候选状态猜测副作用。
    # 函数用途: 返回短规则以及本轮是否改写了正式HOT。
    # LLM: C不把原排序/预算包Root；HOT首次正文写与demote第二写分别核源，第二失败保第一次真实事实。
    # 函数用途: 晋升原HOT并执行原超预算降级，不假报整段原子成功。
    def promote_with_receipt(
        self,
        candidate: MemoryCandidate,
        *,
        lesson: LessonRecord,
        min_occurrences: int = 3,
    ) -> tuple[HotRuleRecord, bool]:
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
        with locked_json_path(self.path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write})):
            existing = _read_hot_records(self.path)
            prior = next((item for item in existing if item.hot_id == hot_id), None)
            if prior is not None and (prior.candidate_id != candidate.candidate_id or prior.rule != record.rule):
                raise RuntimeError("HOT id collision")
            if prior is not None:
                return prior, False
            _write_lesson_text(self.path, _render_hot([*existing, record]), **({} if not self.coordinator_io else {"coordinator_io": True}))
            demoted = _demote_hot_excess_unlocked(self.path, **({} if not self.coordinator_io else {"coordinator_io": True}))
        if demoted:
            logger.warning(
                "HOT budget exceeded: demoted %d cold rule(s) back to lessons: %s",
                len(demoted),
                ", ".join(item.hot_id for item in demoted),
            )
        return record, True

    # LLM: 只解析程序 marker；自由文本 HOT 被视为 legacy，迁移前不能静默混入正式规则。
    # 函数用途: 列出正式 HOT 规则。
    def list(self) -> list[HotRuleRecord]:
        return _read_hot_records(self.path)

    # LLM: 降级是数据治理而非删除——lesson 正文与路由索引不变，失去的只是"每轮全量注入"资格。
    # 函数用途: 返回被降级的冷 HOT 记录（超预算时按活跃度从低到高移除）。
    # LLM: C锁等待/读/预算/排序仍Root外，原降级条件不变，仅准确FD与末写入门。
    # 函数用途: 按原预算移出冷HOT记录，lesson本体保持。
    def demote_cold_rules(self) -> list[HotRuleRecord]:
        with locked_json_path(self.path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write})):
            return _demote_hot_excess_unlocked(self.path, **({} if not self.coordinator_io else {"coordinator_io": True}))


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


# LLM: 导航投影按"最近更新优先"排序（结构化 updated_at/created_at 为活跃信号），行数/字节双帽内截断；
#   省略只影响索引导航，lesson 正文文件仍是唯一正文，不因索引省略而删除。
# 函数用途: 把 lesson 元数据渲染成有界导航索引文本（超限省略尾部并标注）。
def _render_routing_index(records: list[LessonRecord]) -> str:
    ordered = sorted(
        records,
        key=lambda record: (record.updated_at or "", record.created_at or "", record.lesson_id),
        reverse=True,
    )
    sections = [_routing_section(record) for record in ordered]
    while True:
        omitted = len(ordered) - len(sections)
        text = _assemble_routing_index(sections, omitted)
        if _routing_index_within_bounds(text) or not sections:
            return text
        sections.pop()


# LLM: 拼接口径与重建前完全一致（标题、说明、空行分隔），省略标注只在截断时追加。
# 函数用途: 组装导航索引文本。
def _assemble_routing_index(sections: list[str], omitted: int) -> str:
    text = "# Memory Routing Index\n\n"
    text += "此文件由正式 lesson 元数据确定性生成；它是导航索引，不是第二份教训正文。\n"
    if sections:
        text += "\n" + "\n\n".join(sections) + "\n"
    if omitted:
        text += f"（已省略 {omitted} 条导航；lesson 正文仍完整保存在 memory/lessons/。）\n"
    return text


# LLM: 帽口径 = 换行数（行）与 UTF-8 字节数；两个帽任一超出即需截断。
# 函数用途: 判断导航索引文本是否在行数/字节上限内。
def _routing_index_within_bounds(text: str) -> bool:
    return (
        text.count("\n") <= _ROUTING_INDEX_MAX_LINE_COUNT
        and len(text.encode("utf-8")) <= _ROUTING_INDEX_MAX_BYTES
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
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    for line in jsonl_lines(path.read_text(encoding="utf-8")):
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


# LLM: 活跃度排序只用结构化信号（promoted_at/occurrence_count），不解析 rule 正文。
# 函数用途: 活跃度键——新提升 + 高出现次数 = 更活跃。
def _hot_activity_key(record: HotRuleRecord) -> tuple[str, int]:
    return (record.promoted_at or "", record.occurrence_count)


# LLM: 截断是注入侧安全网；排序与写侧降级对称（保留活跃、淘汰冷旧）。
# 函数用途: 按活跃度从高到低截断 HOT 到预算内（字符），返回保留的记录。
def hot_records_within_budget(
    records: list[HotRuleRecord],
    *,
    max_chars: int = HOT_MAX_INJECT_CHARS,
) -> list[HotRuleRecord]:
    ordered = sorted(records, key=_hot_activity_key, reverse=True)
    kept: list[HotRuleRecord] = []
    total = 0
    for record in ordered:
        cost = len(record.rule or "")
        if kept and total + cost > max_chars:
            break
        kept.append(record)
        total += cost
    return kept


# LLM: 只降级冷记录；同一批都超预算时逐条让位直到回到预算内，绝不整批清空。重写按私有原子写（0600）。
# 函数用途: 写入后检查 HOT 总字符，超预算则按活跃度从低到高移除并重写文件；返回被降级记录。
# LLM: 两原caller都透传C策略，原总量/排序/逐条让位/render保持门外，只有最终raw写进门。
# 函数用途: 保原HOT超额降级和第二次末写，不漏治理副作用。
def _demote_hot_excess_unlocked(path: Path, *, coordinator_io: bool = False) -> list[HotRuleRecord]:
    existing = _read_hot_records(path)
    total = sum(len(record.rule or "") for record in existing)
    if total <= HOT_MAX_INJECT_CHARS:
        return []
    cold_first = sorted(existing, key=_hot_activity_key)
    demoted: list[HotRuleRecord] = []
    for record in cold_first:
        if total <= HOT_MAX_INJECT_CHARS:
            break
        total -= len(record.rule or "")
        demoted.append(record)
    if demoted:
        kept = [record for record in existing if record not in demoted]
        _write_lesson_text(path, _render_hot(kept), **({} if not coordinator_io else {"coordinator_io": True}))
    return demoted


__all__ = [
    "HOT_SCHEMA_VERSION",
    "LESSON_SCHEMA_VERSION",
    "HotRuleRecord",
    "HotRuleRepository",
    "LessonRecord",
    "LessonRepository",
]


# LLM: 仅包住正式正文提交后的派生步骤；原验证错误不进入本函数，重试不重写正文。
# 函数用途: 将routing重建失败与lesson审核门槛不满足明确区分。
# LLM: 只在C对原scope真实四类bare raise，普通routing异常仍原LessonRoutingPendingError，正文partial保持。
# 函数用途: 区分真实来源失效与原派生索引待恢复，避免失败后继续付费。
def _rebuild_committed_lesson_routing(repository, record, changed):
    try:
        repository.rebuild_routing_index()
    except Exception as exc:
        if getattr(repository, "coordinator_io", False) and isinstance(exc, (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                        LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable)):
            raise
        raise LessonRoutingPendingError(record, changed) from exc


# LLM: 调用方已持原目标锁且已完成验证/渲染，默认原writer调用形状；C只提交准确path的现成raw，不传任意业务callback。
# 函数用途: 将lesson、routing或HOT原正文做一次原子末写，失败保持原partial。
def _write_lesson_text(path, raw, *, coordinator_io=False):
    if coordinator_io:
        return _coordinator_lesson_write(lambda: write_private_text_file_atomic_unlocked(
            path, raw, replace_once=True))
    return write_private_text_file_atomic_unlocked(path, raw)


# LLM: 每次固定本地写都消费当前原global scope，不储存source/授权或把预算、扫描、锁等待放Root。
# 函数用途: 为C原lesson/HOT资源的准确出生与raw末写调用原同步门。
def _coordinator_lesson_write(operation):
    return execute_global_write(operation, require_scope=True)
