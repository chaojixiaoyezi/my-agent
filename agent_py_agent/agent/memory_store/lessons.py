# LLM: C仅存IO策略，原lesson/HOT验证/锁/预算/排序/渲染门外；正文→routing与HOT→demote逐原末写保partial。
from __future__ import annotations

"""正式 lesson、确定性 routing index 与短小 HOT 的唯一正文仓库。"""

# LLM: lesson 正文只存在 memory/lessons/*.md；memory.jsonl、memory.md 和 HOT 不得复制详细正文。
# 模块用途: 审核后创建可复用教训、按元数据重建 routing，并从已接受 lesson 晋升短 HOT 规则。

import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.json_io import (
    jsonl_lines,
    locked_json_path,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import NoFollowPathError, ensure_private_dir, read_text_beneath
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from .candidate_models import MemoryCandidate, normalize_lesson_asset_refs, utc_now_iso
from .jsonl import MemorySubjectConflict
from .operations import normalized_memory_content
from .retention_scan_budget import (
    RETENTION_SCAN_SLICE_BYTES,
    RETENTION_SCAN_SLICE_ENTRIES,
    RETENTION_SCAN_SLICE_MS,
)
from .scope_contract import canonical_scope_key, validate_typed_scope_key
from .security import scan_memory_content

LESSON_SCHEMA_VERSION = "my-agent.lesson.v1"
HOT_SCHEMA_VERSION = "my-agent.hot-rule.v1"
LESSON_HEAT_SCHEMA_VERSION = "my-agent.lesson-heat.v1"
_LESSON_META_PREFIX = "<!-- my-agent-lesson-meta:"
_HOT_META_PREFIX = "<!-- my-agent-hot-meta:"
_META_SUFFIX = " -->"
# 每日秒数，用于将 lesson 静默未命中天数换算成秒级阈值。
_LESSON_DAY_SECONDS = 86400
# 默认自动归档未引用天数（与 AgentConfig.memory_lesson_archive_days 默认值对齐）。
_DEFAULT_LESSON_ARCHIVE_DAYS = 15
# 内存中待合并命中字典的上限条目数，防止长时间未维护时内存无界增长。
_MAX_PENDING_LESSON_HIT_ENTRIES_COUNT = 8192
# 规范 lesson 文件名 stem 与归档月桶目录校验规则。
_CANONICAL_LESSON_STEM_RE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]{0,79})-[0-9a-f]{8}")
_ARCHIVE_BUCKET_RE = re.compile(r"\d{4}-\d{2}")
_LIFECYCLE_LOCK_LOCAL = threading.local()
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
    asset_refs: tuple[str, ...] = ()
    revision: int = 1
    superseded_by: str = ""
    supersedes: tuple[str, ...] = ()
    superseded_at: str = ""

    # LLM: routing/HOT 只消费无正文 metadata，详细 lesson content 继续留在唯一 Markdown 正文；
    #   未变更默认新字段时省略落盘键，确保既有未修改文件的字节稳定性。
    # 函数用途: 返回可写入索引和引用的 lesson 元数据。
    def metadata(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("content", None)
        if int(self.revision) == 1:
            payload.pop("revision", None)
        if not self.superseded_by:
            payload.pop("superseded_by", None)
        if not self.supersedes:
            payload.pop("supersedes", None)
        if not self.superseded_at:
            payload.pop("superseded_at", None)
        return payload


# LLM: 热度侧车丢失或损坏时明确拒绝返回无冲突，防止在未对账前制造双权威。
# 类用途: 标识 lesson_heat.json 不可用或尚未完成对账的结构化异常。
class LessonHeatUnavailableError(RuntimeError):
    """lesson_heat.json is missing, corrupt, or incomplete for active lookup."""


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
    # LLM: 默认原ensure顺序；C两准确目录出生消费原scope，无source/callback字段；heat 侧车懒加载对账。
    # 函数用途: 绑定原lesson/routing路径并按原顺序准备目录。
    def __init__(self, lessons_dir: str | Path, routing_index_path: str | Path, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self.lessons_dir = Path(lessons_dir)
        self.routing_index_path = Path(routing_index_path)
        self.heat_path = self.routing_index_path.parent / "lesson_heat.json"
        self._pending_hits_lock = threading.Lock()
        self._pending_hits: dict[str, tuple[int, float]] = {}
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        if self.coordinator_io:
            _coordinator_lesson_write(lambda: ensure_private_dir(self.lessons_dir))
            _coordinator_lesson_write(lambda: ensure_private_dir(self.routing_index_path.parent))
        else:
            ensure_private_dir(self.lessons_dir)
            ensure_private_dir(self.routing_index_path.parent)

    def _hits_lock(self) -> threading.Lock:
        lock = getattr(self, "_pending_hits_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._pending_hits_lock = lock
        return lock

    def _pending_hits_map(self) -> dict[str, tuple[int, float]]:
        pending = getattr(self, "_pending_hits", None)
        if pending is None:
            pending = {}
            self._pending_hits = pending
        return pending

    def _heat_path(self) -> Path:
        path = getattr(self, "heat_path", None)
        return path if path is not None else self.routing_index_path.parent / "lesson_heat.json"

    # LLM: 同一线程允许重入已持有的 lifecycle 锁，跨线程与跨进程仍由 routing 文件锁严格串行。
    # 函数用途: 提供覆盖主/次级操作与 HOT 同步的统一生命周期互斥锁。
    @contextmanager
    def lifecycle_lock(self) -> Iterator[None]:
        key = os.fspath(self.routing_index_path.absolute())
        depths = getattr(_LIFECYCLE_LOCK_LOCAL, "depths", None)
        if depths is None:
            depths = {}
            _LIFECYCLE_LOCK_LOCAL.depths = depths
        current = depths.get(key, 0)
        if current > 0:
            depths[key] = current + 1
            try:
                yield
            finally:
                remaining = depths.get(key, 1) - 1
                if remaining > 0:
                    depths[key] = remaining
                else:
                    depths.pop(key, None)
            return
        _ensure_dir_if_missing(self.routing_index_path.parent, coordinator_io=self.coordinator_io)
        options = {} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write}
        with locked_json_path(self.routing_index_path, **options):
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)

    # LLM: 兼容既有调用方只取正式记录；权限、幂等及写入事实仍由同一锁内实现裁决。
    # 函数用途: 创建或复用一个正式lesson并重建派生索引。
    def promote(self, candidate: MemoryCandidate, *, min_occurrences: int = 2) -> LessonRecord:
        return self.promote_with_receipt(candidate, min_occurrences=min_occurrences)[0]

    # LLM: lesson目标锁内记录实际创建事实；派生routing与heat重建在 lifecycle_lock 内同步，防同主题竞态双写。
    # 函数用途: 正式晋升或复用lesson，同主题异内容显式冲突，再沿原链重建routing与heat。
    def promote_with_receipt(
        self,
        candidate: MemoryCandidate,
        *,
        min_occurrences: int = 2,
    ) -> tuple[LessonRecord, bool]:
        _validate_lesson_candidate(candidate, min_occurrences=min_occurrences)
        subject_key = str(candidate.subject_key or "").strip()
        if str(candidate.proposed_action or "add").strip().lower() == "remove":
            raise ValueError("LESSON_ACTION_UNSUPPORTED: promote_with_receipt does not support proposed_action='remove'")
        scope_type, scope_key = _canonical_lesson_scope(candidate.scope)
        lesson_id = "lesson-" + hashlib.sha256(
            candidate.candidate_id.encode("utf-8")
        ).hexdigest()[:20]
        filename = _lesson_filename(subject_key, lesson_id)
        path = self.lessons_dir / filename
        groups = tuple(sorted(_evidence_groups(candidate)))
        now = utc_now_iso()
        record = LessonRecord(
            schema_version=LESSON_SCHEMA_VERSION,
            lesson_id=lesson_id,
            candidate_id=candidate.candidate_id,
            subject_key=subject_key,
            scope={"scope_type": scope_type, "scope_key": scope_key},
            content=candidate.content.strip(),
            occurrence_count=candidate.occurrence_count,
            evidence_groups=groups,
            source_task_ids=tuple(candidate.source_task_ids),
            source_run_ids=tuple(candidate.source_run_ids),
            created_at=now,
            updated_at=now,
            path=f"memory/lessons/{filename}",
            asset_refs=tuple(normalize_lesson_asset_refs(candidate.asset_refs)),
        )
        with self.lifecycle_lock():
            _ensure_dir_if_missing(self.lessons_dir, coordinator_io=self.coordinator_io)
            path_options = {} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write}
            reused_existing: LessonRecord | None = None
            if path.exists():
                with locked_json_path(path, **path_options):
                    changed = False
                    record = _read_lesson(path)
                    if record.candidate_id != candidate.candidate_id:
                        raise RuntimeError("lesson path collision")
            else:
                existing = (
                    self.active_for_subject(subject_key, {"scope_type": scope_type, "scope_key": scope_key})
                    if subject_key
                    else None
                )
                if existing is not None:
                    if normalized_memory_content(existing.content) == normalized_memory_content(record.content):
                        reused_existing = existing
                    else:
                        raise MemorySubjectConflict(subject_key, existing.lesson_id)
                if reused_existing is not None:
                    record = reused_existing
                    changed = False
                else:
                    rendered_text = _render_lesson(record)
                    with locked_json_path(path, **path_options):
                        changed = not path.exists()
                        if not changed:
                            record = _read_lesson(path)
                            if record.candidate_id != candidate.candidate_id:
                                raise RuntimeError("lesson path collision")
                        else:
                            _write_lesson_text(
                                path,
                                rendered_text,
                                **({} if not self.coordinator_io else {"coordinator_io": True}),
                            )
            _rebuild_committed_lesson_routing(self, record, changed)
        return record, changed

    # LLM: 仅按规范 ref（memory/lessons/<canonical>.md[#lesson_id]）不随符号链接直读，空输入零 I/O。
    # 函数用途: 批量校验并按引用读取活跃或未归档 lesson 正文，缺失文件静默省略。
    def read_refs(self, refs: Iterable[str]) -> dict[str, LessonRecord]:
        raw_refs = list(refs)
        if not raw_refs:
            return {}
        parsed_refs = [(_raw, *_parse_canonical_lesson_ref(_raw)) for _raw in raw_refs]
        _reject_symlink_dir(self.lessons_dir)
        if not self.lessons_dir.exists():
            return {}
        tolerate_legacy = not (self.lessons_dir.parent / "migration.json").exists()
        loaded: dict[str, LessonRecord | None] = {}
        for _, rel_path, filename, _ in parsed_refs:
            if filename in loaded:
                continue
            try:
                raw_text = read_text_beneath(self.lessons_dir, (filename,))
            except FileNotFoundError:
                loaded[filename] = None
                continue
            except NoFollowPathError:
                raise
            if raw_text is None:
                loaded[filename] = None
                continue
            try:
                record = _parse_lesson_text(raw_text, filename)
            except ValueError as exc:
                if tolerate_legacy and "requires migration" in str(exc):
                    logger.debug("跳过未迁移 legacy lesson(%s): ref=%s", exc, rel_path)
                    loaded[filename] = None
                    continue
                raise
            if record.path != rel_path:
                raise ValueError(
                    f"LESSON_HEADER_PATH_MISMATCH: expected={rel_path!r} actual={record.path!r}"
                )
            loaded[filename] = record
        result: dict[str, LessonRecord] = {}
        for raw_ref, _rel_path, filename, fragment_id in parsed_refs:
            record = loaded.get(filename)
            if record is None:
                continue
            if fragment_id is not None and record.lesson_id != fragment_id:
                raise ValueError(
                    f"LESSON_HEADER_ID_MISMATCH: expected={fragment_id!r} actual={record.lesson_id!r}"
                )
            result[raw_ref] = record
        return result

    # LLM: 召回热路径零磁盘 I/O 记录命中，待维护窗口持锁合并进 lesson_heat.json。
    # 函数用途: 记录本轮被读取引用的 lesson_id 命中信号（单次调用内自动去重）。
    def note_hits(self, lesson_ids: Iterable[str], *, at: datetime | None = None) -> None:
        unique_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in lesson_ids:
            lid = str(raw_id or "").strip()
            if lid and lid not in seen:
                seen.add(lid)
                unique_ids.append(lid)
        if not unique_ids:
            return
        hit_epoch = _to_epoch(at) if at is not None else time.time()
        with self._hits_lock():
            pending = self._pending_hits_map()
            for lid in unique_ids:
                prev_count, prev_epoch = pending.get(lid, (0, 0.0))
                pending[lid] = (prev_count + 1, max(prev_epoch, hit_epoch))
                while len(pending) > _MAX_PENDING_LESSON_HIT_ENTRIES_COUNT:
                    pending.pop(next(iter(pending)))

    def _drain_pending_hits(self) -> dict[str, tuple[int, float]]:
        with self._hits_lock():
            drained = self._pending_hits_map()
            self._pending_hits = {}
            return drained

    def _restore_pending_hits(self, drained: dict[str, tuple[int, float]]) -> None:
        if not drained:
            return
        with self._hits_lock():
            pending = self._pending_hits_map()
            for lid, (count, ts) in drained.items():
                prev_count, prev_ts = pending.get(lid, (0, 0.0))
                pending[lid] = (prev_count + count, max(prev_ts, ts))
                while len(pending) > _MAX_PENDING_LESSON_HIT_ENTRIES_COUNT:
                    pending.pop(next(iter(pending)))

    # LLM: 基于 lesson_heat.json 做 O(1) 活跃主题查找；侧车损坏或缺失且文件非空时拒绝假阴性。
    # 函数用途: 按规范 subject_key 与 scope 返回当前唯一活跃 lesson，没有则返回 None。
    def active_for_subject(self, subject_key: str, scope: Mapping[str, str]) -> LessonRecord | None:
        subj = str(subject_key or "").strip()
        if not subj:
            raise ValueError("LESSON_SUBJECT_REQUIRED: subject_key must be non-empty")
        scope_type, scope_key = _canonical_lesson_scope(scope)
        kind, state_or_err = self._read_heat_state()
        if kind == "symlink":
            raise NoFollowPathError(f"LESSON_HEAT_SYMLINK_REJECTED: {state_or_err}")
        if kind == "corrupt":
            raise LessonHeatUnavailableError(
                f"LESSON_HEAT_UNAVAILABLE: {state_or_err}; side=read_only; "
                "next=run LessonRepository.maintain() to rebuild lesson_heat.json"
            )
        if kind == "missing":
            if self._has_any_canonical_lesson_file():
                raise LessonHeatUnavailableError(
                    "LESSON_HEAT_UNAVAILABLE: lesson_heat.json is missing while lesson files exist; "
                    "side=read_only; next=run LessonRepository.maintain() to rebuild lesson_heat.json"
                )
            return None
        heat = state_or_err
        rows = heat.get("rows", {})
        candidates: list[tuple[int, str, str, str, dict[str, object]]] = []
        for lid, row in rows.items():
            if row.get("status") != "active" or row.get("subject_key") != subj:
                continue
            row_scope = _canonical_scope_pair_safe(
                str(row.get("scope_type") or ""),
                str(row.get("scope_key") or ""),
            )
            if row_scope != (scope_type, scope_key):
                continue
            candidates.append(
                (
                    int(row.get("revision") or 1),
                    str(row.get("updated_at") or ""),
                    str(row.get("created_at") or ""),
                    str(lid),
                    row,
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        for _rev, _upd, _crt, _lid, row in candidates:
            rel_path = str(row.get("path") or "")
            if not rel_path:
                continue
            loaded = self.read_refs([rel_path])
            record = loaded.get(rel_path)
            if record is None:
                continue
            if (
                record.status == "active"
                and record.subject_key == subj
                and _canonical_lesson_scope(record.scope) == (scope_type, scope_key)
            ):
                return record
        cursor = heat.get("maintenance_cursor") or {}
        if cursor.get("phase") == "active" and cursor.get("after_name"):
            raise LessonHeatUnavailableError(
                "LESSON_HEAT_PARTIAL: active lesson index scan is incomplete; "
                "side=read_only; next=continue calling LessonRepository.maintain() until backlog=False"
            )
        return None

    # LLM: 严格 CAS 替换既有活跃教训：先写后继确定性文件，再将旧头改写为 superseded（保留旧正文）并退役对应 HOT 规则。
    # 函数用途: 原子替代过时教训并清理关联过时 HOT，支持中断后按确定性身份幂等续跑恢复。
    def supersede_with_receipt(
        self,
        candidate: MemoryCandidate,
        *,
        target_lesson_id: str,
        hot: HotRuleRepository,
        expected_revision: int | None = None,
        min_occurrences: int = 2,
    ) -> tuple[LessonRecord, bool]:
        _validate_lesson_candidate(candidate, min_occurrences=min_occurrences)
        subject_key = str(candidate.subject_key or "").strip()
        if str(candidate.proposed_action or "add").strip().lower() == "remove":
            raise ValueError("LESSON_ACTION_UNSUPPORTED: supersede_with_receipt does not accept proposed_action='remove'")
        scope_type, scope_key = _canonical_lesson_scope(candidate.scope)
        target_id = str(target_lesson_id or "").strip()
        if not target_id:
            raise ValueError("SUPERSESSION_TARGET_REQUIRED: target_lesson_id must be a non-empty lesson_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError(
                "SUPERSESSION_EXPECTED_REVISION_REQUIRED: expected_revision must be an integer >= 1; "
                f"actual={expected_revision!r}; side=not_started; "
                "next=read target.revision and pass expected_revision explicitly"
            )
        successor_id = "lesson-" + hashlib.sha256(
            f"{candidate.candidate_id}\0supersede\0{target_id}\0{expected_revision}".encode()
        ).hexdigest()[:20]
        successor_filename = _lesson_filename(subject_key, successor_id)
        successor_path = self.lessons_dir / successor_filename
        groups = tuple(sorted(_evidence_groups(candidate)))

        with self.lifecycle_lock():
            _reject_symlink_dir(self.lessons_dir)
            if not self.lessons_dir.exists():
                raise KeyError(target_id)
            replaying_successor = successor_path.exists()
            kind, state_or_err = self._read_heat_state()
            if kind == "symlink":
                raise NoFollowPathError(f"LESSON_HEAT_SYMLINK_REJECTED: {state_or_err}")
            target: LessonRecord | None = None
            if kind == "ok" and target_id in state_or_err.get("rows", {}):
                row = state_or_err["rows"][target_id]
                loaded = self.read_refs([row["path"]])
                target = loaded.get(row["path"])
            elif replaying_successor:
                try:
                    target = self.get(target_id)
                except KeyError:
                    target = None
            elif kind == "corrupt":
                raise LessonHeatUnavailableError(
                    f"LESSON_HEAT_UNAVAILABLE: {state_or_err}; side=not_started; "
                    "next=run LessonRepository.maintain() before superseding"
                )
            elif kind == "missing" and self._has_any_canonical_lesson_file():
                raise LessonHeatUnavailableError(
                    "LESSON_HEAT_UNAVAILABLE: lesson_heat.json is missing while lesson files exist; "
                    "side=not_started; next=run LessonRepository.maintain() before superseding"
                )
            if target is None:
                raise KeyError(target_id)
            if (
                target.subject_key != subject_key
                or _canonical_lesson_scope(target.scope) != (scope_type, scope_key)
            ):
                raise ValueError(
                    "SUPERSESSION_SCOPE_OR_SUBJECT_MISMATCH: candidate subject/scope does not match target lesson "
                    f"(target=({target.subject_key!r}, {target.scope!r}), "
                    f"candidate=({subject_key!r}, {{'scope_type': {scope_type!r}, 'scope_key': {scope_key!r}}}))"
                )

            target_path = self.lessons_dir / Path(target.path).name
            path_options = {} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write}
            wrote_successor = False
            wrote_target = False
            updated_target: LessonRecord | None = None

            if target.status == "active":
                if target.revision != expected_revision:
                    raise ValueError(
                        f"LESSON_REVISION_CONFLICT: expected_revision={expected_revision}, "
                        f"actual_revision={target.revision}, target_lesson_id={target.lesson_id!r}; "
                        "side=not_started; next=reload active target and retry with current revision"
                    )
                now_iso = utc_now_iso()
                candidate_successor = LessonRecord(
                    schema_version=LESSON_SCHEMA_VERSION,
                    lesson_id=successor_id,
                    candidate_id=candidate.candidate_id,
                    subject_key=subject_key,
                    scope={"scope_type": scope_type, "scope_key": scope_key},
                    content=candidate.content.strip(),
                    occurrence_count=candidate.occurrence_count,
                    evidence_groups=groups,
                    source_task_ids=tuple(candidate.source_task_ids),
                    source_run_ids=tuple(candidate.source_run_ids),
                    created_at=now_iso,
                    updated_at=now_iso,
                    path=f"memory/lessons/{successor_filename}",
                    status="active",
                    asset_refs=tuple(normalize_lesson_asset_refs(candidate.asset_refs)),
                    revision=expected_revision + 1,
                    superseded_by="",
                    supersedes=(target.lesson_id,),
                    superseded_at="",
                )
                successor_text = _render_lesson(candidate_successor)
                with locked_json_path(successor_path, **path_options):
                    if successor_path.exists():
                        successor_record = _read_lesson(successor_path)
                        if (
                            successor_record.candidate_id != candidate.candidate_id
                            or target.lesson_id not in successor_record.supersedes
                        ):
                            raise RuntimeError("lesson path collision")
                    else:
                        _write_lesson_text(
                            successor_path,
                            successor_text,
                            **({} if not self.coordinator_io else {"coordinator_io": True}),
                        )
                        successor_record = candidate_successor
                        wrote_successor = True
                # 先在锁外校验与渲染旧教训新头，再进目标锁更新，保留原正文。
                with locked_json_path(target_path, **path_options):
                    current_target = _read_lesson(target_path)
                    if current_target.status == "active":
                        if current_target.revision != expected_revision:
                            raise ValueError(
                                f"LESSON_REVISION_CONFLICT: expected_revision={expected_revision}, "
                                f"actual_revision={current_target.revision}, target_lesson_id={current_target.lesson_id!r}; "
                                "side=successor_may_exist; next=reload target state"
                            )
                        updated_target = replace(
                            current_target,
                            status="superseded",
                            superseded_by=successor_record.lesson_id,
                            superseded_at=successor_record.created_at,
                            updated_at=successor_record.created_at,
                        )
                        target_text = _render_lesson(updated_target)
                        _write_lesson_text(
                            target_path,
                            target_text,
                            **({} if not self.coordinator_io else {"coordinator_io": True}),
                        )
                        wrote_target = True
                    elif (
                        current_target.status == "superseded"
                        and current_target.superseded_by == successor_record.lesson_id
                        and current_target.revision == expected_revision
                    ):
                        updated_target = current_target
                    else:
                        raise ValueError(
                            f"LESSON_REVISION_CONFLICT: target={current_target.lesson_id!r} "
                            f"status={current_target.status!r} superseded_by={current_target.superseded_by!r}"
                        )
            elif (
                target.status == "superseded"
                and target.superseded_by == successor_id
                and target.revision == expected_revision
                and successor_path.exists()
            ):
                successor_record = _read_lesson(successor_path)
                if (
                    successor_record.candidate_id != candidate.candidate_id
                    or target.lesson_id not in successor_record.supersedes
                ):
                    raise RuntimeError("lesson path collision")
                updated_target = target
            else:
                raise ValueError(
                    f"LESSON_REVISION_CONFLICT: target={target.lesson_id!r} status={target.status!r} "
                    f"revision={target.revision} expected_revision={expected_revision}; "
                    "side=not_started; next=inspect active_for_subject() before superseding"
                )

            formal_changed = wrote_successor or wrote_target
            try:
                _retire_hot_rules_for_lesson(hot, target_lesson_id=target.lesson_id)
            except Exception as exc:
                if bool(getattr(hot, "coordinator_io", False)) and isinstance(
                    exc,
                    (
                        GlobalWriteCommitUnavailable,
                        GlobalWriteInWorkerError,
                        LocalControlCoordinatorUnavailable,
                        OwnerManagementIngressUnavailable,
                    ),
                ):
                    raise
                raise LessonRoutingPendingError(successor_record, formal_changed) from exc
            hints = [successor_record] + ([updated_target] if updated_target is not None else [])
            _rebuild_committed_lesson_routing(self, successor_record, formal_changed, hint_records=hints)
        return successor_record, formal_changed

    # LLM: 无 v1 元数据 marker 的 .md 有两种合法语义,以迁移 marker 是否已建立区分:
    # 未迁移(升级前/全新 home)→ legacy,读路径容忍跳过,由 MemoryMigrationService 自愈,
    # 绝不因单个旧文件拖垮整表;迁移已 complete(正式 v2 区建立)后仍出现无 marker 文件
    # → 损坏,必须 fail-closed 不可静默吞(防模型拿到被破坏的正式记忆)。有 marker 但
    # metadata 非法的文件两种模式都抛(与迁移 CORRUPT_LESSON_MARKER 同一 fail-closed 约定)。
    # 副本名在读取前跳过；写入器允许 slug 内有点，但末尾必须是八位小写哈希。
    # 仅满八十字符的截断 slug 可能保留尾部标点；规范文件仍走原迁移和元数据校验。
    # 函数用途: 按迁移状态容错或严格列出当前正式 lesson。
    def list(self) -> list[LessonRecord]:
        tolerate_legacy = not (self.lessons_dir.parent / "migration.json").exists()
        records: list[LessonRecord] = []
        for path in sorted(self.lessons_dir.glob("*.md")):
            name = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]{0,79})-[0-9a-f]{8}", path.stem)
            if name is None or (len(name[1]) < 80 and name[1].endswith((".", "-"))):
                logger.debug("Ignoring non-canonical lesson name=%s", path.name)
                continue
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

    # LLM: 重建时同时同步 lesson_heat.json 与 INDEX.md；heat 缺失或损坏时自愈扫描活跃 lessons。
    # 函数用途: 按热度序重新生成 200 行/25KB 内可重建导航投影。
    def rebuild_routing_index(self) -> Path:
        with self.lifecycle_lock():
            _ensure_dir_if_missing(self.routing_index_path.parent, coordinator_io=self.coordinator_io)
            return self._rebuild_routing_and_heat_locked()

    # LLM: 有界维护接口——合并命中信号、按需对账并按 15 天静默窗口永久非破坏性归档到 archive/YYYY-MM/。
    # 函数用途: 执行一轮受预算限制的 lesson 热数据对账、双活性收敛、归档迁移与归档计数。
    def maintain(
        self,
        *,
        now: datetime | None = None,
        archive_days: int = _DEFAULT_LESSON_ARCHIVE_DAYS,
        batch_count: int = 0,
        batch_bytes: int = 0,
        batch_ms: int = 0,
        pinned_ids: Iterable[str] = (),
        pin_loader: Callable[[], tuple[Iterable[str], bool]] | None = None,
    ) -> dict[str, object]:
        days = int(archive_days)
        if days < 0:
            raise ValueError(f"archive_days must be non-negative; actual={days}")
        now_dt = _normalize_utc_datetime(now)
        now_epoch = max(0.0, now_dt.timestamp())
        now_iso = now_dt.isoformat(timespec="seconds")
        cutoff_seconds = float(days * _LESSON_DAY_SECONDS)
        limit_count = int(batch_count) if int(batch_count) > 0 else RETENTION_SCAN_SLICE_ENTRIES
        limit_bytes = int(batch_bytes) if int(batch_bytes) > 0 else RETENTION_SCAN_SLICE_BYTES
        limit_ms = int(batch_ms) if int(batch_ms) > 0 else RETENTION_SCAN_SLICE_MS

        with self.lifecycle_lock():
            _ensure_dir_if_missing(self.routing_index_path.parent, coordinator_io=self.coordinator_io)
            kind, state_or_err = self._read_heat_state()
            if kind == "symlink":
                raise NoFollowPathError(f"LESSON_HEAT_SYMLINK_REJECTED: {state_or_err}")
            if kind == "ok":
                heat = _copy_heat_state(state_or_err)
            else:
                heat = _empty_heat_state(
                    now_epoch,
                    now_iso,
                    reason="heat_rebuilt" if kind == "corrupt" else "archive_not_counted",
                )

            rows: dict[str, dict[str, object]] = heat["rows"]
            pins, pins_complete, pin_reason = self._collect_live_pins(pinned_ids, pin_loader)
            drained_hits = self._drain_pending_hits()
            try:
                unmatched_hits = _apply_hits_to_rows(rows, drained_hits)
                started_mono = time.monotonic()
                scanned = 0
                bytes_read = 0
                archived_delta = 0
                scan_had_error = False

                def budget_available() -> bool:
                    if scanned >= limit_count:
                        return False
                    if scanned == 0:
                        return True
                    elapsed_ms = (time.monotonic() - started_mono) * 1000.0
                    return bytes_read < limit_bytes and elapsed_ms < limit_ms

                cursor = dict(heat.get("maintenance_cursor") or {})
                phase = str(cursor.get("phase") or "")
                after_name = str(cursor.get("after_name") or "")
                archive_bucket = str(cursor.get("archive_bucket") or "")
                sweep_gen = str(cursor.get("source_generation") or "")
                if phase not in {"active", "archive_move", "archive_count"}:
                    phase = "active"
                    after_name = ""
                    archive_bucket = ""
                    sweep_gen = uuid.uuid4().hex
                elif not sweep_gen:
                    sweep_gen = uuid.uuid4().hex

                newly_discovered: set[str] = set()
                active_exhausted = False
                tolerate_legacy = not (self.lessons_dir.parent / "migration.json").exists()

                # 阶段 1: 有界扫描 lessons/ 顶层规范文件（永不递归 archive/）。
                if phase == "active":
                    canonical_names = _list_top_canonical_lessons(self.lessons_dir)
                    present_set = set(canonical_names)
                    for lid in list(rows):
                        if Path(str(rows[lid].get("path") or "")).name not in present_set:
                            rows.pop(lid, None)
                    pending_names = [name for name in canonical_names if name > after_name]
                    for name in pending_names:
                        if not budget_available():
                            active_exhausted = True
                            break
                        try:
                            raw_text = read_text_beneath(self.lessons_dir, (name,))
                            if raw_text is None:
                                after_name = name
                                continue
                            bytes_read += len(raw_text.encode("utf-8"))
                            scanned += 1
                            after_name = name
                            record = _parse_lesson_text(raw_text, name)
                        except ValueError as exc:
                            after_name = name
                            if tolerate_legacy and "requires migration" in str(exc):
                                continue
                            logger.warning("lesson maintain skipped unreadable file=%s err=%s", name, exc)
                            scan_had_error = True
                            continue
                        except (OSError, NoFollowPathError) as exc:
                            after_name = name
                            scanned += 1
                            logger.warning("lesson maintain skipped unreadable path=%s err=%s", name, exc)
                            scan_had_error = True
                            continue

                        prior_row = rows.get(record.lesson_id)
                        if prior_row is None:
                            newly_discovered.add(record.lesson_id)
                        rows[record.lesson_id] = _build_heat_row(
                            record,
                            prior_row=prior_row,
                            now_epoch=now_epoch,
                            unmatched_hits=unmatched_hits,
                            disk_filename=name,
                        )
                    if not active_exhausted:
                        _reconcile_duplicate_active_subjects(
                            self.lessons_dir,
                            rows,
                            now_iso=now_iso,
                            now_epoch=now_epoch,
                            coordinator_io=self.coordinator_io,
                        )
                        phase = "archive_move"
                        after_name = ""
                        archive_bucket = ""

                # 仅在 active 扫描完成时刷新全局路由并结算未路由时钟，避免半截扫描误判冷条目。
                active_complete = phase != "active"
                routed_ids: list[str]
                routing_text: str | None = None
                if active_complete:
                    routed_ids, routing_text = _compute_routing_from_rows(
                        self.lessons_dir,
                        rows,
                        tolerate_legacy=tolerate_legacy,
                        drop_missing=True,
                    )
                    routed_set = set(routed_ids)
                    for lid, row in rows.items():
                        if row.get("status") != "active" or lid in routed_set:
                            row["unrouted_at"] = 0.0
                        else:
                            last_hit = float(row.get("last_hit_at") or 0.0)
                            unrouted_at = float(row.get("unrouted_at") or 0.0)
                            if lid in newly_discovered or unrouted_at <= 0.0:
                                row["unrouted_at"] = max(now_epoch, last_hit)
                else:
                    routed_ids = [
                        str(lid) for lid in heat.get("routed_ids", []) if str(lid) in rows
                    ]

                # 阶段 2: 超过 archive_days 且未被置顶的冷/已替代教训，同文件系统原子迁入 archive/YYYY-MM/。
                if phase == "archive_move":
                    if days > 0 and pins_complete:
                        eligible: list[tuple[str, str, float, dict[str, object]]] = []
                        routed_set = set(routed_ids)
                        for lid, row in rows.items():
                            rel_path = str(row.get("path") or "")
                            fname = Path(rel_path).name
                            if not fname or fname <= after_name:
                                continue
                            if lid in pins or rel_path in pins:
                                continue
                            if row.get("status") == "superseded":
                                sup_at = float(row.get("superseded_at") or 0.0)
                                if sup_at > 0.0 and (now_epoch - sup_at) >= cutoff_seconds:
                                    eligible.append((fname, lid, sup_at, row))
                            elif row.get("status") == "active" and lid not in routed_set:
                                unrouted_at = float(row.get("unrouted_at") or 0.0)
                                last_hit_at = float(row.get("last_hit_at") or 0.0)
                                activity_at = max(unrouted_at, last_hit_at)
                                if unrouted_at > 0.0 and (now_epoch - activity_at) >= cutoff_seconds:
                                    eligible.append((fname, lid, activity_at, row))
                        eligible.sort(key=lambda item: item[0])
                        move_exhausted = False
                        bucket_name = f"{now_dt.year:04d}-{now_dt.month:02d}"
                        for fname, lid, _ts, row in eligible:
                            if not budget_available():
                                move_exhausted = True
                                break
                            scanned += 1
                            after_name = fname
                            moved, move_bytes = _archive_lesson_file(
                                self.lessons_dir,
                                fname,
                                lid=lid,
                                expected_status=str(row.get("status") or ""),
                                bucket_name=bucket_name,
                                now_epoch=now_epoch,
                                cutoff_seconds=cutoff_seconds,
                                coordinator_io=self.coordinator_io,
                            )
                            bytes_read += move_bytes
                            if moved:
                                rows.pop(lid, None)
                                archived_delta += 1
                        if not move_exhausted:
                            phase = "archive_count"
                            after_name = ""
                            archive_bucket = ""
                    else:
                        phase = "archive_count"
                        after_name = ""
                        archive_bucket = ""

                # 阶段 3: 有界统计 lessons/archive/YYYY-MM/ 中的永久归档条目总数。
                archive_coverage = dict(heat.get("archive_count_coverage") or {})
                archived_total = heat.get("lessons_archived_count")
                if archived_delta > 0 and phase != "archive_count":
                    # 已搬运新归档但未能进入重新计数阶段时，立即作废旧归档计数避免报告伪 complete。
                    archived_total = None
                    archive_coverage = {
                        "status": "partial",
                        "reason": "archive_moved_pending_recount",
                        "as_of": now_iso,
                    }
                if phase == "archive_count":
                    archive_root = self.lessons_dir / "archive"
                    if not archive_root.exists():
                        archived_total = 0
                        archive_coverage = {
                            "status": "complete",
                            "reason": "no_archive_dir",
                            "as_of": now_iso,
                        }
                        phase = "done"
                        after_name = ""
                        archive_bucket = ""
                    else:
                        resuming_archive = bool(archive_bucket or after_name)
                        tally = (
                            int(archived_total)
                            if resuming_archive and type(archived_total) is int and archived_total >= 0
                            else 0
                        )
                        (
                            tally,
                            done_archive,
                            next_bucket,
                            next_after,
                            archive_scans,
                            archive_error,
                        ) = _count_archive_slice(
                            archive_root,
                            start_bucket=archive_bucket,
                            after_name=after_name,
                            running_tally=tally,
                            scanned=scanned,
                            limit_count=limit_count,
                            started_mono=started_mono,
                            limit_ms=limit_ms,
                        )
                        scanned += archive_scans
                        archive_bucket = next_bucket
                        after_name = next_after
                        archived_total = tally
                        if archive_error:
                            scan_had_error = True
                        if done_archive:
                            phase = "done"
                            after_name = ""
                            archive_bucket = ""
                            archive_coverage = {
                                "status": "complete" if not archive_error else "partial",
                                "reason": "archive_dir_unreadable" if archive_error else "archive_counted",
                                "as_of": now_iso,
                            }
                        else:
                            archive_coverage = {
                                "status": "partial",
                                "reason": "slice_budget_exhausted",
                                "as_of": now_iso,
                            }

                backlog = phase != "done"
                new_cursor = {
                    "phase": phase,
                    "after_name": after_name,
                    "archive_bucket": archive_bucket,
                    "source_generation": sweep_gen,
                }
                new_heat: dict[str, object] = {
                    "schema_version": LESSON_HEAT_SCHEMA_VERSION,
                    "generation": uuid.uuid4().hex,
                    "updated_at": now_epoch,
                    "rows": rows,
                    "routed_ids": routed_ids,
                    "lessons_archived_count": archived_total,
                    "archive_count_coverage": archive_coverage,
                    "maintenance_cursor": new_cursor,
                    "last_maintenance_at": now_epoch,
                }
                heat_text = json.dumps(new_heat, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                _write_lesson_text(
                    self._heat_path(),
                    heat_text,
                    **({} if not self.coordinator_io else {"coordinator_io": True}),
                )
                drained_hits = {}
                if active_exhausted and unmatched_hits:
                    self._restore_pending_hits(unmatched_hits)
                if routing_text is not None:
                    existing_routing = (
                        self.routing_index_path.read_text(encoding="utf-8")
                        if self.routing_index_path.exists()
                        else None
                    )
                    if existing_routing != routing_text:
                        _write_lesson_text(
                            self.routing_index_path,
                            routing_text,
                            **({} if not self.coordinator_io else {"coordinator_io": True}),
                        )

                overall_status = "complete"
                overall_reason = "ok"
                if not pins_complete:
                    overall_status = "partial"
                    overall_reason = pin_reason or "pin_loader_incomplete"
                elif backlog:
                    overall_status = "partial"
                    overall_reason = "slice_budget_exhausted"
                elif scan_had_error or archive_coverage.get("status") != "complete":
                    overall_status = "partial"
                    overall_reason = str(archive_coverage.get("reason") or "scan_partial")

                return {
                    "coverage": {
                        "status": overall_status,
                        "reason": overall_reason,
                        "as_of": now_iso,
                    },
                    "scanned": scanned,
                    "routed": len(routed_ids),
                    "archived": archived_delta,
                    "lessons_active_count": (
                        sum(1 for r in rows.values() if r.get("status") == "active")
                        if active_complete
                        else None
                    ),
                    "lessons_routed_count": len(routed_ids) if active_complete else None,
                    "lessons_archived_count": (
                        archived_total if archive_coverage.get("status") == "complete" else None
                    ),
                    "backlog": backlog,
                    "cursor": new_cursor,
                }
            except Exception:
                self._restore_pending_hits(drained_hits)
                raise

    # LLM: 提供内部快速/完整对账辅助；在已持 lifecycle_lock 下同步 heat 与 INDEX.md。
    # 函数用途: 更新指定或全部活跃教训的热度索引并重渲染 INDEX.md。
    def _rebuild_routing_and_heat_locked(self, *, hint_records: list[LessonRecord] | None = None) -> Path:
        kind, state_or_err = self._read_heat_state()
        now_epoch = time.time()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tolerate_legacy = not (self.lessons_dir.parent / "migration.json").exists()
        if hint_records and kind == "ok":
            heat = _copy_heat_state(state_or_err)
            rows = heat["rows"]
            for rec in hint_records:
                rows[rec.lesson_id] = _build_heat_row(
                    rec,
                    prior_row=rows.get(rec.lesson_id),
                    now_epoch=now_epoch,
                )
            routed_ids, routing_text = _compute_routing_from_rows(
                self.lessons_dir,
                rows,
                hint_by_id={r.lesson_id: r for r in hint_records},
                tolerate_legacy=tolerate_legacy,
                drop_missing=True,
            )
            _refresh_unrouted_clocks(rows, set(routed_ids), now_epoch)
            new_heat: dict[str, object] = {
                "schema_version": LESSON_HEAT_SCHEMA_VERSION,
                "generation": uuid.uuid4().hex,
                "updated_at": now_epoch,
                "rows": rows,
                "routed_ids": routed_ids,
                "lessons_archived_count": heat.get("lessons_archived_count"),
                "archive_count_coverage": dict(
                    heat.get("archive_count_coverage")
                    or {"status": "unavailable", "reason": "archive_not_counted", "as_of": now_iso}
                ),
            }
            if "maintenance_cursor" in heat:
                new_heat["maintenance_cursor"] = dict(heat["maintenance_cursor"])
            if "last_maintenance_at" in heat:
                new_heat["last_maintenance_at"] = heat["last_maintenance_at"]
        else:
            existing_rows = state_or_err.get("rows", {}) if kind == "ok" else {}
            records = self.list()
            rows = {}
            for rec in records:
                rows[rec.lesson_id] = _build_heat_row(
                    rec,
                    prior_row=existing_rows.get(rec.lesson_id) if isinstance(existing_rows, dict) else None,
                    now_epoch=now_epoch,
                )
            routed_ids = []
            routing_text = _render_routing_index_compat(records, heat_rows=rows, out_routed_ids=routed_ids)
            _refresh_unrouted_clocks(rows, set(routed_ids), now_epoch)
            archive_root = self.lessons_dir / "archive"
            if kind == "ok":
                archived_count = state_or_err.get("lessons_archived_count")
                archive_coverage = dict(
                    state_or_err.get("archive_count_coverage")
                    or {"status": "unavailable", "reason": "archive_not_counted", "as_of": now_iso}
                )
            elif not archive_root.exists():
                archived_count = 0
                archive_coverage = {"status": "complete", "reason": "no_archive_dir", "as_of": now_iso}
            else:
                archived_count = None
                archive_coverage = {"status": "unavailable", "reason": "archive_not_counted", "as_of": now_iso}
            new_heat = {
                "schema_version": LESSON_HEAT_SCHEMA_VERSION,
                "generation": uuid.uuid4().hex,
                "updated_at": now_epoch,
                "rows": rows,
                "routed_ids": routed_ids,
                "lessons_archived_count": archived_count,
                "archive_count_coverage": archive_coverage,
            }
            if kind == "ok" and "maintenance_cursor" in state_or_err:
                new_heat["maintenance_cursor"] = dict(state_or_err["maintenance_cursor"])
            if kind == "ok" and "last_maintenance_at" in state_or_err:
                new_heat["last_maintenance_at"] = state_or_err["last_maintenance_at"]

        heat_text = json.dumps(new_heat, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _write_lesson_text(
            self._heat_path(),
            heat_text,
            **({} if not self.coordinator_io else {"coordinator_io": True}),
        )
        _write_lesson_text(
            self.routing_index_path,
            routing_text,
            **({} if not self.coordinator_io else {"coordinator_io": True}),
        )
        return self.routing_index_path

    def _read_heat_state(self) -> tuple[str, dict[str, object] | str]:
        heat_path = self._heat_path()
        routing_dir = self.routing_index_path.parent
        if routing_dir.is_symlink() or heat_path.is_symlink():
            return ("symlink", "routing directory or lesson_heat.json is a symlink")
        if not heat_path.exists():
            return ("missing", "lesson_heat.json does not exist")
        try:
            raw = read_text_beneath(routing_dir, (heat_path.name,))
        except NoFollowPathError as exc:
            return ("symlink", str(exc))
        except FileNotFoundError:
            return ("missing", "lesson_heat.json does not exist")
        except (OSError, UnicodeDecodeError) as exc:
            return ("corrupt", f"unreadable lesson_heat.json: {type(exc).__name__}")
        if raw is None:
            return ("missing", "lesson_heat.json does not exist")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ("corrupt", "lesson_heat.json contains invalid JSON")
        if not _is_valid_heat_doc(data):
            return ("corrupt", "lesson_heat.json schema validation failed")
        return ("ok", data)

    def _has_any_canonical_lesson_file(self) -> bool:
        _reject_symlink_dir(self.lessons_dir)
        if not self.lessons_dir.exists():
            return False
        with os.scandir(self.lessons_dir) as entries:
            for entry in entries:
                if entry.name == "archive":
                    continue
                if entry.is_file(follow_symlinks=False) and _is_canonical_lesson_filename(entry.name):
                    return True
        return False

    def _collect_live_pins(
        self,
        pinned_ids: Iterable[str],
        pin_loader: Callable[[], tuple[Iterable[str], bool]] | None,
    ) -> tuple[set[str], bool, str]:
        pins: set[str] = set()
        _add_normalized_pin_tokens(pins, pinned_ids)
        if pin_loader is not None:
            try:
                loaded_ids, ok = pin_loader()
            except Exception as exc:  # noqa: BLE001 - unreadable pin source refuses archival safely
                logger.warning("lesson maintain pin_loader failed: %s: %s", type(exc).__name__, exc)
                return pins, False, "pin_loader_failed"
            if loaded_ids is not None:
                _add_normalized_pin_tokens(pins, loaded_ids)
            if not ok:
                return pins, False, "pin_loader_incomplete"
            return pins, True, "ok"
        default_hot = self.lessons_dir.parent / "memory-hot.md"
        if default_hot.exists():
            try:
                hot_options = {} if not self.coordinator_io else {"birth_commit": _coordinator_lesson_write}
                with locked_json_path(default_hot, **hot_options):
                    for row in _read_hot_records(default_hot):
                        _add_normalized_pin_tokens(pins, (row.lesson_id, row.lesson_ref))
            except Exception as exc:  # noqa: BLE001 - unreadable HOT refuses archival safely
                logger.warning("lesson maintain failed to inspect memory-hot.md: %s: %s", type(exc).__name__, exc)
                return pins, False, "hot_pins_unreadable"
        return pins, True, "ok"


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
        lessons_sibling = self.path.parent / "lessons"
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
            if (lessons_sibling / "archive").is_dir() and str(lesson.path or "").startswith("memory/lessons/"):
                target_name = Path(lesson.path).name
                try:
                    target_raw = read_text_beneath(lessons_sibling, (target_name,))
                except (OSError, NoFollowPathError):
                    target_raw = None
                if target_raw is None:
                    raise ValueError(
                        f"HOT_TARGET_ARCHIVED_OR_MISSING: lesson {lesson.lesson_id!r} ({lesson.path!r}) "
                        "is no longer active in memory/lessons/; side=not_started; "
                        "next=restore or refresh lesson before promoting to HOT"
                    )
                target_record = _parse_lesson_text(target_raw, target_name)
                if target_record.status != "active":
                    raise ValueError(
                        f"HOT_TARGET_ARCHIVED_OR_MISSING: lesson {lesson.lesson_id!r} ({lesson.path!r}) "
                        f"has status={target_record.status!r}; side=not_started; "
                        "next=promote HOT against the active successor lesson"
                    )
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
    return _parse_lesson_text(path.read_text(encoding="utf-8"), path.name)


# LLM: 统一文本解析入口供 _read_lesson 与 O_NOFOLLOW read_refs 共用，保持字段校验一致。
# 函数用途: 将规范 lesson Markdown 正文与头部元数据解析为 LessonRecord。
def _parse_lesson_text(text: str, filename: str) -> LessonRecord:
    first, _, rest = text.partition("\n")
    if not first.startswith(_LESSON_META_PREFIX) or not first.endswith(_META_SUFFIX):
        raise ValueError(f"legacy lesson requires migration: {filename}")
    try:
        payload = json.loads(first[len(_LESSON_META_PREFIX) : -len(_META_SUFFIX)])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid lesson metadata: {filename}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != LESSON_SCHEMA_VERSION:
        raise ValueError(f"invalid lesson metadata: {filename}")
    raw_revision = payload.get("revision", 1)
    if type(raw_revision) is not int or raw_revision < 1:
        raise ValueError(f"invalid lesson metadata: {filename}")
    raw_superseded_by = payload.get("superseded_by", "")
    if not isinstance(raw_superseded_by, str):
        raise ValueError(f"invalid lesson metadata: {filename}")
    raw_supersedes = payload.get("supersedes", ())
    if not isinstance(raw_supersedes, (list, tuple)) or any(not isinstance(item, str) for item in raw_supersedes):
        raise ValueError(f"invalid lesson metadata: {filename}")
    raw_superseded_at = payload.get("superseded_at", "")
    if not isinstance(raw_superseded_at, str):
        raise ValueError(f"invalid lesson metadata: {filename}")
    raw_status = str(payload.get("status") or "active")
    if raw_status not in {"active", "superseded"}:
        raise ValueError(f"invalid lesson metadata: {filename}")
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
        status=raw_status,
        asset_refs=tuple(normalize_lesson_asset_refs(payload.get("asset_refs", []))),
        revision=raw_revision,
        superseded_by=raw_superseded_by,
        supersedes=tuple(raw_supersedes),
        superseded_at=raw_superseded_at,
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
def _render_routing_index(
    records: list[LessonRecord],
    *,
    heat_rows: Mapping[str, Mapping[str, object]] | None = None,
    total_active_count: int | None = None,
    out_routed_ids: list[str] | None = None,
) -> str:
    active_records = [
        record for record in records if getattr(record, "status", "active") == "active"
    ]
    ordered = sorted(
        active_records,
        key=lambda record: _lesson_sort_key(record, heat_rows),
        reverse=True,
    )
    total_count = len(ordered) if total_active_count is None else max(len(ordered), int(total_active_count))
    kept_sections, kept_ids = _fit_routing_prefix(ordered, total_count=total_count)
    if out_routed_ids is not None:
        out_routed_ids.clear()
        out_routed_ids.extend(kept_ids)
    return _assemble_routing_index(kept_sections, total_count - len(kept_sections))


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
    "LESSON_HEAT_SCHEMA_VERSION",
    "LESSON_SCHEMA_VERSION",
    "HotRuleRecord",
    "HotRuleRepository",
    "LessonHeatUnavailableError",
    "LessonRecord",
    "LessonRepository",
    "LessonRoutingPendingError",
]


# LLM: 仅包住正式正文提交后的派生步骤；原验证错误不进入本函数，重试不重写正文。
# 函数用途: 将routing与heat重建失败与lesson审核门槛不满足明确区分。
# LLM: 只在C对原scope真实四类bare raise，普通routing异常仍原LessonRoutingPendingError，正文partial保持。
# 函数用途: 区分真实来源失效与原派生索引待恢复，避免失败后继续付费。
def _rebuild_committed_lesson_routing(repository, record, changed, *, hint_records=None):
    try:
        if hint_records is not None and hasattr(repository, "_rebuild_routing_and_heat_locked"):
            repository._rebuild_routing_and_heat_locked(hint_records=hint_records)
        elif hasattr(repository, "_rebuild_routing_and_heat_locked"):
            repository._rebuild_routing_and_heat_locked(hint_records=[record])
        else:
            repository.rebuild_routing_index()
    except Exception as exc:
        if getattr(repository, "coordinator_io", False) and isinstance(
            exc,
            (
                GlobalWriteCommitUnavailable,
                GlobalWriteInWorkerError,
                LocalControlCoordinatorUnavailable,
                OwnerManagementIngressUnavailable,
            ),
        ):
            raise
        raise LessonRoutingPendingError(record, changed) from exc


# LLM: 调用方已持原目标锁且已完成验证/渲染，默认原writer调用形状；C只提交准确path的现成raw，不传任意业务callback。
# 函数用途: 将lesson、heat、routing或HOT原正文做一次原子末写，失败保持原partial。
def _write_lesson_text(path, raw, *, coordinator_io=False):
    if coordinator_io:
        return _coordinator_lesson_write(lambda: write_private_text_file_atomic_unlocked(
            path, raw, replace_once=True))
    return write_private_text_file_atomic_unlocked(path, raw)


# LLM: 每次固定本地写都消费当前原global scope，不储存source/授权或把预算、扫描、锁等待放Root。
# 函数用途: 为C原lesson/HOT资源的准确出生与raw末写调用原同步门。
def _coordinator_lesson_write(operation):
    return execute_global_write(operation, require_scope=True)


def _ensure_dir_if_missing(directory: Path, *, coordinator_io: bool) -> None:
    if directory.exists():
        return
    if coordinator_io:
        _coordinator_lesson_write(lambda: ensure_private_dir(directory))
    else:
        ensure_private_dir(directory)


def _reject_symlink_dir(directory: Path) -> None:
    if directory.is_symlink():
        raise NoFollowPathError(f"LESSON_DIR_SYMLINK_REJECTED: symlink directory rejected: {directory}")


def _is_canonical_lesson_filename(filename: str) -> bool:
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        return False
    stem = filename[:-3]
    match = _CANONICAL_LESSON_STEM_RE.fullmatch(stem)
    if match is None:
        return False
    if len(match[1]) < 80 and match[1].endswith((".", "-")):
        return False
    return True


def _parse_canonical_lesson_ref(ref: str) -> tuple[str, str, str | None]:
    if not isinstance(ref, str) or not ref or ref != ref.strip() or any(ch in ref for ch in "\r\n\0\\"):
        raise ValueError(f"INVALID_LESSON_REF: ref={ref!r}; next=use 'memory/lessons/<canonical>.md[#lesson_id]'")
    if "#" in ref:
        base, fragment = ref.split("#", 1)
        if not fragment or "#" in fragment or "/" in fragment:
            raise ValueError(f"INVALID_LESSON_REF: invalid fragment in {ref!r}")
        fragment_id: str | None = fragment
    else:
        base = ref
        fragment_id = None
    prefix = "memory/lessons/"
    if not base.startswith(prefix):
        raise ValueError(f"INVALID_LESSON_REF: ref must start with 'memory/lessons/': {ref!r}")
    filename = base[len(prefix) :]
    if not _is_canonical_lesson_filename(filename):
        raise ValueError(f"INVALID_LESSON_REF: non-canonical lesson filename in {ref!r}")
    return f"{prefix}{filename}", filename, fragment_id


def _canonical_lesson_scope(scope: object) -> tuple[str, str]:
    if isinstance(scope, Mapping):
        raw_type = scope.get("scope_type")
        raw_key = scope.get("scope_key")
    else:
        raw_type = getattr(scope, "scope_type", "")
        raw_key = getattr(scope, "scope_key", "")
    stype = str(raw_type or "").strip().lower()
    skey = str(raw_key or "").strip()
    if not stype or not skey:
        raise ValueError(f"INVALID_LESSON_SCOPE: scope={scope!r}")
    canonical = canonical_scope_key(stype, skey)
    validate_typed_scope_key(stype, canonical)
    return stype, canonical


def _canonical_scope_pair_safe(scope_type: str, scope_key: str) -> tuple[str, str]:
    stype = str(scope_type or "").strip().lower()
    skey = str(scope_key or "").strip()
    if not skey or skey == "legacy":
        return stype, skey
    try:
        return stype, canonical_scope_key(stype, skey)
    except (ValueError, TypeError):
        return stype, skey


def _normalize_utc_datetime(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _to_epoch(value: datetime) -> float:
    return max(0.0, _normalize_utc_datetime(value).timestamp())


def _parse_iso_epoch(value: object) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    return _to_epoch(parsed)


def _lesson_sort_key(
    record: LessonRecord,
    heat_rows: Mapping[str, Mapping[str, object]] | None,
) -> tuple[bool, float, float, str, str, str]:
    row = heat_rows.get(record.lesson_id) if heat_rows else None
    hits = int(row.get("hits") or 0) if isinstance(row, Mapping) else 0
    last_hit_at = float(row.get("last_hit_at") or 0.0) if isinstance(row, Mapping) else 0.0
    return (
        hits > 0,
        last_hit_at,
        _parse_iso_epoch(record.updated_at),
        record.updated_at or "",
        record.created_at or "",
        record.lesson_id,
    )


def _fit_routing_prefix(
    ordered: list[LessonRecord],
    *,
    total_count: int,
) -> tuple[list[str], list[str]]:
    base_header = (
        "# Memory Routing Index\n\n"
        "此文件由正式 lesson 元数据确定性生成；它是导航索引，不是第二份教训正文。\n"
    )
    base_lines = base_header.count("\n")
    base_bytes = len(base_header.encode("utf-8"))
    kept_sections: list[str] = []
    kept_ids: list[str] = []
    running_lines = 0
    running_bytes = 0
    best_k = 0
    for idx, record in enumerate(ordered):
        section = _routing_section(record)
        sec_lines = section.count("\n")
        sec_bytes = len(section.encode("utf-8"))
        # _assemble_routing_index 在非空时加入前导 "\n"、段间 "\n\n" 与尾随 "\n"：每段贡献 +2 行 / +2 字节。
        running_lines += sec_lines + 2
        running_bytes += sec_bytes + 2
        k = idx + 1
        omitted = total_count - k
        footer_lines = 1 if omitted > 0 else 0
        footer_bytes = (
            len(f"（已省略 {omitted} 条导航；lesson 正文仍完整保存在 memory/lessons/。）\n".encode())
            if omitted > 0
            else 0
        )
        if base_lines + running_lines > _ROUTING_INDEX_MAX_LINE_COUNT:
            break
        if base_bytes + running_bytes > _ROUTING_INDEX_MAX_BYTES:
            break
        kept_sections.append(section)
        kept_ids.append(record.lesson_id)
        if (
            base_lines + running_lines + footer_lines <= _ROUTING_INDEX_MAX_LINE_COUNT
            and base_bytes + running_bytes + footer_bytes <= _ROUTING_INDEX_MAX_BYTES
        ):
            best_k = k
    while best_k > 0:
        omitted = total_count - best_k
        text = _assemble_routing_index(kept_sections[:best_k], omitted)
        if _routing_index_within_bounds(text):
            return kept_sections[:best_k], kept_ids[:best_k]
        best_k -= 1
    return [], []


def _render_routing_index_compat(
    records: list[LessonRecord],
    *,
    heat_rows: Mapping[str, Mapping[str, object]] | None = None,
    total_active_count: int | None = None,
    out_routed_ids: list[str] | None = None,
) -> str:
    params = inspect.signature(_render_routing_index).parameters
    if "heat_rows" in params and "total_active_count" in params and "out_routed_ids" in params:
        return _render_routing_index(
            records,
            heat_rows=heat_rows,
            total_active_count=total_active_count,
            out_routed_ids=out_routed_ids,
        )
    active = [r for r in records if getattr(r, "status", "active") == "active"]
    ordered = sorted(active, key=lambda r: _lesson_sort_key(r, heat_rows), reverse=True)
    if out_routed_ids is not None:
        _, kept_ids = _fit_routing_prefix(
            ordered,
            total_count=len(ordered) if total_active_count is None else total_active_count,
        )
        out_routed_ids.clear()
        out_routed_ids.extend(kept_ids)
    return _render_routing_index(ordered)


def _is_valid_heat_doc(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    allowed_top = {
        "schema_version",
        "generation",
        "updated_at",
        "rows",
        "routed_ids",
        "lessons_archived_count",
        "archive_count_coverage",
        "maintenance_cursor",
        "last_maintenance_at",
    }
    required_top = {
        "schema_version",
        "generation",
        "updated_at",
        "rows",
        "routed_ids",
        "lessons_archived_count",
        "archive_count_coverage",
    }
    if not required_top <= set(data) or not set(data) <= allowed_top:
        return False
    if data.get("schema_version") != LESSON_HEAT_SCHEMA_VERSION:
        return False
    if not isinstance(data.get("generation"), str) or not data["generation"]:
        return False
    if not isinstance(data.get("updated_at"), (int, float)) or isinstance(data["updated_at"], bool) or data["updated_at"] < 0:
        return False
    routed_ids = data.get("routed_ids")
    if not isinstance(routed_ids, list) or any(not isinstance(item, str) or not item for item in routed_ids):
        return False
    if len(set(routed_ids)) != len(routed_ids):
        return False
    archived_count = data.get("lessons_archived_count")
    if archived_count is not None and (type(archived_count) is not int or archived_count < 0):
        return False
    coverage = data.get("archive_count_coverage")
    if not isinstance(coverage, dict):
        return False
    if coverage.get("status") not in {"complete", "partial", "unavailable"}:
        return False
    if not isinstance(coverage.get("reason"), str) or not coverage["reason"]:
        return False
    cursor = data.get("maintenance_cursor")
    if cursor is not None:
        if not isinstance(cursor, dict) or set(cursor) != {"phase", "after_name", "archive_bucket", "source_generation"}:
            return False
        if any(not isinstance(v, str) for v in cursor.values()):
            return False
    rows = data.get("rows")
    if not isinstance(rows, dict):
        return False
    row_keys = {
        "path",
        "subject_key",
        "scope_type",
        "scope_key",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "last_hit_at",
        "hits",
        "unrouted_at",
        "superseded_at",
    }
    for lid, row in rows.items():
        if not isinstance(lid, str) or not lid or not isinstance(row, dict) or set(row) != row_keys:
            return False
        if row["status"] not in {"active", "superseded"}:
            return False
        if not isinstance(row["path"], str) or not re.fullmatch(r"memory/lessons/[^/]+\.md", row["path"]):
            return False
        if type(row["revision"]) is not int or row["revision"] < 1:
            return False
        if type(row["hits"]) is not int or row["hits"] < 0:
            return False
        for num_field in ("last_hit_at", "unrouted_at", "superseded_at"):
            val = row[num_field]
            if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                return False
        for str_field in ("subject_key", "scope_type", "scope_key", "created_at", "updated_at"):
            if not isinstance(row[str_field], str):
                return False
    return True


def _empty_heat_state(now_epoch: float, now_iso: str, *, reason: str) -> dict[str, object]:
    return {
        "schema_version": LESSON_HEAT_SCHEMA_VERSION,
        "generation": uuid.uuid4().hex,
        "updated_at": now_epoch,
        "rows": {},
        "routed_ids": [],
        "lessons_archived_count": None,
        "archive_count_coverage": {
            "status": "unavailable",
            "reason": reason,
            "as_of": now_iso,
        },
        "maintenance_cursor": {
            "phase": "active",
            "after_name": "",
            "archive_bucket": "",
            "source_generation": "",
        },
    }


def _copy_heat_state(state: dict[str, object]) -> dict[str, object]:
    copied = dict(state)
    rows = state.get("rows")
    copied["rows"] = {str(k): dict(v) for k, v in rows.items()} if isinstance(rows, dict) else {}
    copied["routed_ids"] = list(state.get("routed_ids") or [])
    copied["archive_count_coverage"] = dict(state.get("archive_count_coverage") or {})
    if isinstance(state.get("maintenance_cursor"), dict):
        copied["maintenance_cursor"] = dict(state["maintenance_cursor"])
    return copied


def _apply_hits_to_rows(
    rows: dict[str, dict[str, object]],
    drained_hits: dict[str, tuple[int, float]],
) -> dict[str, tuple[int, float]]:
    unmatched: dict[str, tuple[int, float]] = {}
    for lid, (delta, hit_ts) in drained_hits.items():
        row = rows.get(lid)
        if row is None:
            unmatched[lid] = (delta, hit_ts)
            continue
        row["hits"] = int(row.get("hits") or 0) + int(delta)
        row["last_hit_at"] = max(float(row.get("last_hit_at") or 0.0), float(hit_ts))
        if row.get("status") == "active" and float(row.get("unrouted_at") or 0.0) > 0.0:
            row["unrouted_at"] = max(float(row["unrouted_at"]), float(hit_ts))
    return unmatched


def _build_heat_row(
    record: LessonRecord,
    *,
    prior_row: Mapping[str, object] | None,
    now_epoch: float,
    unmatched_hits: dict[str, tuple[int, float]] | None = None,
    disk_filename: str | None = None,
) -> dict[str, object]:
    stype, skey = _canonical_scope_pair_safe(
        str(record.scope.get("scope_type") or ""),
        str(record.scope.get("scope_key") or ""),
    )
    hits = int(prior_row.get("hits") or 0) if prior_row else 0
    last_hit_at = float(prior_row.get("last_hit_at") or 0.0) if prior_row else 0.0
    if unmatched_hits and record.lesson_id in unmatched_hits:
        delta, hit_ts = unmatched_hits.pop(record.lesson_id)
        hits += int(delta)
        last_hit_at = max(last_hit_at, float(hit_ts))
    if record.status == "superseded":
        parsed_sup = _parse_iso_epoch(record.superseded_at)
        prior_sup = float(prior_row.get("superseded_at") or 0.0) if prior_row else 0.0
        superseded_at = parsed_sup if parsed_sup > 0.0 else (prior_sup if prior_sup > 0.0 else now_epoch)
        unrouted_at = 0.0
    else:
        superseded_at = 0.0
        prior_unrouted = float(prior_row.get("unrouted_at") or 0.0) if prior_row else 0.0
        unrouted_at = max(prior_unrouted, last_hit_at) if prior_unrouted > 0.0 else 0.0
    row_path = f"memory/lessons/{disk_filename}" if disk_filename else record.path
    return {
        "path": row_path,
        "subject_key": record.subject_key,
        "scope_type": stype,
        "scope_key": skey,
        "status": record.status,
        "revision": int(record.revision),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_hit_at": last_hit_at,
        "hits": hits,
        "unrouted_at": unrouted_at,
        "superseded_at": superseded_at,
    }


def _refresh_unrouted_clocks(
    rows: dict[str, dict[str, object]],
    routed_set: set[str],
    now_epoch: float,
) -> None:
    for lid, row in rows.items():
        if row.get("status") != "active" or lid in routed_set:
            row["unrouted_at"] = 0.0
        elif float(row.get("unrouted_at") or 0.0) <= 0.0:
            row["unrouted_at"] = max(now_epoch, float(row.get("last_hit_at") or 0.0))


def _row_sort_tuple(lid: str, row: Mapping[str, object]) -> tuple[bool, float, float, str, str, str]:
    hits = int(row.get("hits") or 0)
    last_hit_at = float(row.get("last_hit_at") or 0.0)
    upd = str(row.get("updated_at") or "")
    crt = str(row.get("created_at") or "")
    return (hits > 0, last_hit_at, _parse_iso_epoch(upd), upd, crt, lid)


def _compute_routing_from_rows(
    lessons_dir: Path,
    rows: dict[str, dict[str, object]],
    *,
    hint_by_id: Mapping[str, LessonRecord] | None = None,
    tolerate_legacy: bool = True,
    drop_missing: bool = True,
) -> tuple[list[str], str]:
    active_ids = [lid for lid, row in rows.items() if row.get("status") == "active"]
    active_ids.sort(key=lambda lid: _row_sort_tuple(lid, rows[lid]), reverse=True)
    # 仅按热度顺序按需加载足以填满 200 行 / 25KiB 路由窗的首部记录，避免大规模下全读正文。
    candidate_records: list[LessonRecord] = []
    approx_lines = 3
    approx_bytes = 256
    active_survivors = len(active_ids)
    for lid in active_ids:
        row = rows[lid]
        record = hint_by_id.get(lid) if hint_by_id else None
        if record is None:
            fname = Path(str(row.get("path") or "")).name
            if not _is_canonical_lesson_filename(fname):
                if drop_missing:
                    rows.pop(lid, None)
                    active_survivors -= 1
                continue
            try:
                raw_text = read_text_beneath(lessons_dir, (fname,))
                if raw_text is None:
                    if drop_missing:
                        rows.pop(lid, None)
                        active_survivors -= 1
                    continue
                record = _parse_lesson_text(raw_text, fname)
            except FileNotFoundError:
                if drop_missing:
                    rows.pop(lid, None)
                    active_survivors -= 1
                continue
            except ValueError as exc:
                if tolerate_legacy and "requires migration" in str(exc):
                    active_survivors -= 1
                    continue
                if drop_missing:
                    active_survivors -= 1
                continue
            except (OSError, NoFollowPathError):
                active_survivors -= 1
                continue
        if record.status != "active":
            rows[lid]["status"] = record.status
            active_survivors -= 1
            continue
        candidate_records.append(record)
        sec = _routing_section(record)
        approx_lines += sec.count("\n") + 2
        approx_bytes += len(sec.encode("utf-8")) + 2
        if approx_lines > _ROUTING_INDEX_MAX_LINE_COUNT + 16 or approx_bytes > _ROUTING_INDEX_MAX_BYTES + 2048:
            break
    routed_ids: list[str] = []
    routing_text = _render_routing_index_compat(
        candidate_records,
        heat_rows=rows,
        total_active_count=max(len(candidate_records), active_survivors),
        out_routed_ids=routed_ids,
    )
    return routed_ids, routing_text


def _list_top_canonical_lessons(lessons_dir: Path) -> list[str]:
    _reject_symlink_dir(lessons_dir)
    if not lessons_dir.exists():
        return []
    names: list[str] = []
    with os.scandir(lessons_dir) as entries:
        for entry in entries:
            if entry.name == "archive":
                continue
            if entry.is_file(follow_symlinks=False) and _is_canonical_lesson_filename(entry.name):
                names.append(entry.name)
    names.sort()
    return names


def _reconcile_duplicate_active_subjects(
    lessons_dir: Path,
    rows: dict[str, dict[str, object]],
    *,
    now_iso: str,
    now_epoch: float,
    coordinator_io: bool,
) -> None:
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for lid, row in rows.items():
        if row.get("status") != "active":
            continue
        key = (
            str(row.get("subject_key") or ""),
            str(row.get("scope_type") or ""),
            str(row.get("scope_key") or ""),
        )
        grouped.setdefault(key, []).append(lid)
    path_options = {} if not coordinator_io else {"birth_commit": _coordinator_lesson_write}
    for _key, group_ids in grouped.items():
        if len(group_ids) <= 1:
            continue
        loaded: list[tuple[LessonRecord, Path]] = []
        for lid in group_ids:
            fname = Path(str(rows[lid].get("path") or "")).name
            if not _is_canonical_lesson_filename(fname):
                continue
            fpath = lessons_dir / fname
            try:
                raw_text = read_text_beneath(lessons_dir, (fname,))
                if raw_text is None:
                    continue
                rec = _parse_lesson_text(raw_text, fname)
            except (OSError, ValueError, NoFollowPathError):
                continue
            if rec.status == "active":
                loaded.append((rec, fpath))
        if len(loaded) <= 1:
            continue
        loaded.sort(
            key=lambda pair: (
                int(pair[0].revision),
                _parse_iso_epoch(pair[0].updated_at),
                _parse_iso_epoch(pair[0].created_at),
                pair[0].lesson_id,
            ),
            reverse=True,
        )
        winner = loaded[0][0]
        for loser, loser_path in loaded[1:]:
            with locked_json_path(loser_path, **path_options):
                try:
                    raw_loser = read_text_beneath(lessons_dir, (loser_path.name,))
                    if raw_loser is None:
                        continue
                    current = _parse_lesson_text(raw_loser, loser_path.name)
                except (FileNotFoundError, OSError, ValueError, NoFollowPathError):
                    continue
                if current.status != "active":
                    continue
                demoted = replace(
                    current,
                    status="superseded",
                    superseded_by=winner.lesson_id,
                    superseded_at=now_iso,
                    updated_at=now_iso,
                )
                rendered = _render_lesson(demoted)
                _write_lesson_text(
                    loser_path,
                    rendered,
                    **({} if not coordinator_io else {"coordinator_io": True}),
                )
            rows[loser.lesson_id] = _build_heat_row(
                demoted,
                prior_row=rows.get(loser.lesson_id),
                now_epoch=now_epoch,
            )


def _archive_lesson_file(
    lessons_dir: Path,
    filename: str,
    *,
    lid: str,
    expected_status: str,
    bucket_name: str,
    now_epoch: float,
    cutoff_seconds: float,
    coordinator_io: bool,
) -> tuple[bool, int]:
    src_path = lessons_dir / filename
    if not _is_canonical_lesson_filename(filename) or src_path.is_symlink() or not src_path.exists():
        return False, 0
    path_options = {} if not coordinator_io else {"birth_commit": _coordinator_lesson_write}
    archive_root = lessons_dir / "archive"
    bucket_dir = archive_root / bucket_name
    if archive_root.is_symlink() or bucket_dir.is_symlink():
        raise NoFollowPathError(f"LESSON_ARCHIVE_SYMLINK_REJECTED: {bucket_dir}")
    bytes_loaded = 0
    with locked_json_path(src_path, **path_options):
        if not src_path.exists() or src_path.is_symlink():
            return False, 0
        try:
            raw = read_text_beneath(lessons_dir, (filename,))
            if raw is None:
                return False, 0
            bytes_loaded = len(raw.encode())
            record = _parse_lesson_text(raw, filename)
        except (FileNotFoundError, OSError, ValueError, NoFollowPathError):
            return False, bytes_loaded
        if record.lesson_id != lid or record.status != expected_status:
            return False, bytes_loaded
        if record.status == "superseded" and record.superseded_at:
            sup_at = _parse_iso_epoch(record.superseded_at)
            if sup_at > 0.0 and (now_epoch - sup_at) < cutoff_seconds:
                return False, bytes_loaded
        _ensure_dir_if_missing(archive_root, coordinator_io=coordinator_io)
        _ensure_dir_if_missing(bucket_dir, coordinator_io=coordinator_io)
        dest_path = bucket_dir / filename
        if dest_path.exists() and not dest_path.is_symlink():
            try:
                existing = _read_lesson(dest_path)
            except (OSError, ValueError):
                existing = None
            if existing is None or existing.lesson_id != lid:
                stem = filename[:-3]
                dest_path = bucket_dir / f"{stem}.{uuid.uuid4().hex[:8]}.md"

        def _move_op() -> None:
            os.replace(src_path, dest_path)
            os.chmod(dest_path, 0o600)
            for dpath in (bucket_dir, lessons_dir):
                dfd = os.open(dpath, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)

        if coordinator_io:
            _coordinator_lesson_write(_move_op)
        else:
            _move_op()
    return True, bytes_loaded


def _count_archive_slice(
    archive_root: Path,
    *,
    start_bucket: str,
    after_name: str,
    running_tally: int,
    scanned: int,
    limit_count: int,
    started_mono: float,
    limit_ms: int,
) -> tuple[int, bool, str, str, int, bool]:
    if archive_root.is_symlink():
        return running_tally, True, "", "", 0, True
    try:
        buckets = sorted(
            entry.name
            for entry in os.scandir(archive_root)
            if _ARCHIVE_BUCKET_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        return running_tally, True, "", "", 0, True
    pending_buckets = [b for b in buckets if not start_bucket or b >= start_bucket]
    added_scans = 0
    had_error = False
    for bucket in pending_buckets:
        bucket_path = archive_root / bucket
        try:
            files = sorted(
                entry.name
                for entry in os.scandir(bucket_path)
                if entry.is_file(follow_symlinks=False) and _is_canonical_lesson_filename(entry.name)
            )
        except OSError:
            had_error = True
            continue
        prev_after = after_name if bucket == start_bucket else ""
        if bucket == start_bucket and after_name:
            files = [name for name in files if name > after_name]
        for fname in files:
            elapsed_ms = (time.monotonic() - started_mono) * 1000.0
            if scanned >= limit_count or (scanned > 0 and elapsed_ms >= limit_ms):
                return running_tally, False, bucket, prev_after, added_scans, had_error
            running_tally += 1
            scanned += 1
            added_scans += 1
            prev_after = fname
    return running_tally, True, "", "", added_scans, had_error


def _add_normalized_pin_tokens(pins: set[str], tokens: Iterable[str]) -> None:
    for raw in tokens:
        token = str(raw or "").strip()
        if not token:
            continue
        pins.add(token)
        if "#" in token:
            base, frag = token.split("#", 1)
            if base:
                pins.add(base)
            if frag:
                pins.add(frag)


# LLM: 沿用 HOT-file 叶子锁移除已替代教训的关联规则，保留其它规则与预算并支持幂等重放。
# 函数用途: 教训被替代时同步退役过时 HOT 规则，绝不把旧规则抄到新教训上。
def _retire_hot_rules_for_lesson(hot: HotRuleRepository, *, target_lesson_id: str) -> list[HotRuleRecord]:
    if not hot.path.exists():
        return []
    hot_cio = bool(getattr(hot, "coordinator_io", False))
    options = {} if not hot_cio else {"birth_commit": _coordinator_lesson_write}
    with locked_json_path(hot.path, **options):
        existing = _read_hot_records(hot.path)
        removed = [row for row in existing if row.lesson_id == target_lesson_id]
        if not removed:
            return []
        remaining = [row for row in existing if row.lesson_id != target_lesson_id]
        rendered = _render_hot(remaining)
        _write_lesson_text(
            hot.path,
            rendered,
            **({} if not hot_cio else {"coordinator_io": True}),
        )
        return removed
