from __future__ import annotations

"""一次性、可回滚的 Memory v2 数据迁移。"""

# LLM: 旧数据只能在显式 apply 时迁入唯一 Candidate/Daily/Long-term/Lesson 主链；正式运行永不双读旧路径。
# 模块用途: dry-run 检测旧候选和重复正文，应用前备份，失败时恢复，并写 owner 级 schema marker。

import hashlib
import json
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic,
    write_text_file_atomic,
)
from ..user_space.home_memory_seeds import (
    default_memory_hot_md,
    default_memory_md,
)
from .candidate_models import CandidateObservation, MemoryScope
from .candidates import CandidateService
from .daily import (
    DAILY_MEMORY_SCHEMA_VERSION,
    DailyMemoryEvent,
    DailyMemoryStore,
    daily_memory_path,
)
from .jsonl import JsonlMemory, MemoryRecord
from .lessons import HOT_SCHEMA_VERSION, LESSON_SCHEMA_VERSION, LessonRepository
from .operations import memory_content_hash
from .retention_models import RETENTION_SCHEMA_VERSION, MemoryRetentionPolicy

MEMORY_MIGRATION_SCHEMA_VERSION = "my-agent.memory-migration.v2"
_MARKER_NAME = "migration.json"
_LEGACY_ARCHIVE_DIR = "memory-migration"
# LLM: 唯一权威的遗留来源目录名；写入方已删除，只剩迁移读取方，任何新增都必须先补测试。
# 常量用途: 迁移扫描与稳态探针共用的遗留目录名清单。
_LEGACY_SOURCE_NAMES = ("memory_gate", "learning_drafts")
_MAX_CANDIDATE_CHARS = 2_000
_CONTENT_KEYS = ("content", "candidate_content", "body", "text", "lesson", "summary", "claim")


# LLM: finding 只报告路径、类别、数量和动作，不把旧正文打印到 CLI 或迁移审计。
# 类用途: 描述一个待迁移旧来源。
@dataclass(frozen=True)
class MemoryMigrationFinding:
    category: str
    source_path: str
    record_count: int
    action: str
    source_fingerprint: str

    # LLM: 报告序列化必须保持无正文，只输出定位、计数、动作和 source fingerprint。
    # 函数用途: 将一项迁移发现转换为机器可读字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: error_code 是自动化和 CLI 的稳定判断字段；message 不承担控制流。
# 类用途: 表示迁移扫描或应用失败。
@dataclass(frozen=True)
class MemoryMigrationError:
    error_code: str
    source_path: str
    message: str

    # LLM: error_code/path 是控制面字段，message 只供人读且不得夹带旧正文。
    # 函数用途: 将一项迁移错误转换为机器可读字典。
    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# LLM: 报告不包含旧正文；backup_dir 和 marker 让管理员可定位恢复证据。
# 类用途: 返回 dry-run 或 apply 的完整结构化结果。
@dataclass(frozen=True)
class MemoryMigrationReport:
    schema_version: str
    applied: bool
    already_current: bool
    findings: tuple[MemoryMigrationFinding, ...]
    errors: tuple[MemoryMigrationError, ...] = ()
    migrated_candidates: int = 0
    migrated_daily_events: int = 0
    removed_legacy_long_term: int = 0
    archived_sources: int = 0
    backup_dir: str = ""
    marker_path: str = ""
    run_id: str = ""

    # LLM: ok 只由结构化 errors 决定，不能按 message 文案或 finding 数量猜成功。
    # 函数用途: 判断本次 plan/apply 是否没有关闭式错误。
    @property
    def ok(self) -> bool:
        return not self.errors

    # LLM: 输出增加派生 ok，但不复制迁移源正文或备份内容。
    # 函数用途: 将完整迁移报告转换为 CLI 可序列化字典。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


# LLM: scan snapshot 是 apply 的唯一输入；应用阶段不得在备份后重新猜另一批源文件。
# 类用途: 保存一次扫描的 typed 路径集合和无正文 findings。
@dataclass(frozen=True)
class _MigrationSnapshot:
    findings: tuple[MemoryMigrationFinding, ...]
    errors: tuple[MemoryMigrationError, ...]
    paths: tuple[Path, ...]
    legacy_daily_paths: tuple[Path, ...]
    legacy_gate_dirs: tuple[Path, ...]
    learning_dirs: tuple[Path, ...]
    legacy_lesson_paths: tuple[Path, ...]
    legacy_navigation_paths: tuple[Path, ...]
    daily_destination_paths: tuple[Path, ...]
    long_term_records: tuple[MemoryRecord, ...]
    fingerprint_paths: tuple[Path, ...]
    fingerprint: str


# LLM: Mutable scan state is local to one dry-run/apply pass and is converted to an immutable fingerprinted snapshot.
# 类用途: 分阶段累积旧来源、损坏错误、待备份路径以及迁移后的 Daily 目标。
@dataclass
class _MigrationScanState:
    legacy_gate_dirs: tuple[Path, ...]
    learning_dirs: tuple[Path, ...]
    findings: list[MemoryMigrationFinding] = field(default_factory=list)
    errors: list[MemoryMigrationError] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    legacy_daily_paths: list[Path] = field(default_factory=list)
    legacy_lesson_paths: list[Path] = field(default_factory=list)
    legacy_navigation_paths: list[Path] = field(default_factory=list)
    long_term_records: tuple[MemoryRecord, ...] = ()


# LLM: Service 只依赖 owner layout 和三个正式仓库；它不注册工具、不在启动时自动 apply。
# 类用途: 规划并显式执行当前 owner 的 Memory v2 迁移。
class _MigrationServiceCore:
    # LLM: 构造器只登记精确 owner/workspace legacy 根；不得扩大扫描到任意父目录。
    # 函数用途: 初始化一次性迁移服务及 marker、backup 规范路径。
    def __init__(
        self,
        *,
        home_paths: object,
        candidates: CandidateService,
        long_term: JsonlMemory,
        daily: DailyMemoryStore,
        lessons: LessonRepository,
        legacy_workspace_roots: Iterable[str | Path] = (),
    ) -> None:
        self.home = home_paths
        self.candidates = candidates
        self.long_term = long_term
        self.daily = daily
        self.lessons = lessons
        self.owner_home = Path(home_paths.owner_home_dir)
        self.memory_dir = Path(home_paths.owner_memory_dir)
        self.marker_path = self.memory_dir / _MARKER_NAME
        self.backups_root = Path(home_paths.system_backups_dir) / _LEGACY_ARCHIVE_DIR
        self.legacy_learning_dirs = _legacy_workspace_learning_dirs(
            legacy_workspace_roots
        )

    # LLM: dry-run 只读所有旧来源并严格解析；任一坏 JSON/JSONL 都进入 fail-closed errors。
    # 函数用途: 返回当前 owner 的迁移计划，不修改任何文件。
    def plan(self) -> MemoryMigrationReport:
        snapshot = self._scan()
        marker, marker_error = self._read_marker()
        errors = (*snapshot.errors, *((marker_error,) if marker_error is not None else ()))
        already_current = bool(
            not snapshot.findings
            and not errors
            and marker.get("schema_version") == MEMORY_MIGRATION_SCHEMA_VERSION
            and marker.get("status") == "complete"
        )
        return MemoryMigrationReport(
            schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
            applied=False,
            already_current=already_current,
            findings=snapshot.findings,
            errors=errors,
            marker_path=str(self.marker_path),
        )

    # LLM: apply 在 owner 迁移锁内重新扫描、完整备份并执行；失败必须恢复所有被改目标且不写 complete marker。
    # 稳态短路：marker 已是同一迁移版本的 complete 且契约位置无遗留目录时，跳过整棵 owner home 的递归遍历
    # （该遍历在真实 owner home 上是分钟级：agents/ 42 万条、tasks/ 17 万条、data/ 16 万条）。
    # 函数用途: 显式应用 Memory v2 迁移并返回可恢复归档位置。
    def apply(self) -> MemoryMigrationReport:
        lock_path = self.memory_dir / ".migration-v2"
        with locked_json_path(lock_path):
            marker, marker_error = self._read_marker()
            if self._steady_state_current(marker, marker_error):
                return MemoryMigrationReport(
                    schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
                    applied=False,
                    already_current=True,
                    findings=(),
                    marker_path=str(self.marker_path),
                    run_id=str(marker.get("run_id") or ""),
                    backup_dir=str(marker.get("backup_dir") or ""),
                )
            snapshot = self._scan()
            errors = (*snapshot.errors, *((marker_error,) if marker_error is not None else ()))
            if errors:
                return MemoryMigrationReport(
                    schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
                    applied=False,
                    already_current=False,
                    findings=snapshot.findings,
                    errors=errors,
                    marker_path=str(self.marker_path),
                )
            if not snapshot.findings and marker.get("status") == "complete":
                return MemoryMigrationReport(
                    schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
                    applied=False,
                    already_current=True,
                    findings=(),
                    marker_path=str(self.marker_path),
                    run_id=str(marker.get("run_id") or ""),
                    backup_dir=str(marker.get("backup_dir") or ""),
                )
            run_id = _migration_run_id(snapshot.fingerprint)
            backup_dir = self.backups_root / run_id
            backup_map = self._backup(snapshot, backup_dir)
            try:
                counters = self._apply_snapshot(snapshot, backup_dir)
                write_json_file_atomic(
                    self.marker_path,
                    {
                        "schema_version": MEMORY_MIGRATION_SCHEMA_VERSION,
                        "status": "complete",
                        "run_id": run_id,
                        "source_fingerprint": snapshot.fingerprint,
                        "backup_dir": str(backup_dir),
                        "completed_at": _utc_now(),
                        # 记录本次确认过的遗留目录位置：下次稳态判断按点名复查这些位置，
                        # 不再为了重新发现它们而递归遍历整棵 owner home。
                        "legacy_gate_dirs": [str(path) for path in snapshot.legacy_gate_dirs],
                        "learning_dirs": [str(path) for path in snapshot.learning_dirs],
                        **counters,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - rollback must cover every migration failure.
                rollback_errors = self._restore_backup(backup_map)
                errors = [
                    MemoryMigrationError(
                        "MEMORY_MIGRATION_APPLY_FAILED",
                        "",
                        f"{type(exc).__name__}: {exc}",
                    ),
                    *rollback_errors,
                ]
                return MemoryMigrationReport(
                    schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
                    applied=False,
                    already_current=False,
                    findings=snapshot.findings,
                    errors=tuple(errors),
                    backup_dir=str(backup_dir),
                    marker_path=str(self.marker_path),
                    run_id=run_id,
                )
            return MemoryMigrationReport(
                schema_version=MEMORY_MIGRATION_SCHEMA_VERSION,
                applied=True,
                already_current=False,
                findings=snapshot.findings,
                migrated_candidates=counters["migrated_candidates"],
                migrated_daily_events=counters["migrated_daily_events"],
                removed_legacy_long_term=counters["removed_legacy_long_term"],
                archived_sources=counters["archived_sources"],
                backup_dir=str(backup_dir),
                marker_path=str(self.marker_path),
                run_id=run_id,
            )

    # LLM: 稳态判据只读 marker 与固定契约位置，不做递归遍历，也不改变任何迁移语义；
    # marker 缺失、读坏、schema 版本不同或任一契约位置出现遗留目录时一律回落到完整扫描。
    # 函数用途: 判断当前 owner 是否已经完成本版本迁移且没有新的遗留来源，决定能否跳过全量扫描。
    def _steady_state_current(
        self,
        marker: dict[str, object],
        marker_error: MemoryMigrationError | None,
    ) -> bool:
        if marker_error is not None:
            return False
        if marker.get("schema_version") != MEMORY_MIGRATION_SCHEMA_VERSION:
            return False
        if str(marker.get("status") or "") != "complete":
            return False
        return not _legacy_sources_at_contract_paths(
            owner_home=self.owner_home,
            tasks_dir=Path(
                getattr(self.home, "owner_tasks_dir", Path(self.owner_home) / "tasks")
            ),
            data_dir=Path(
                getattr(self.home, "owner_data_dir", Path(self.owner_home) / "data")
            ),
            recorded_gate_dirs=marker.get("legacy_gate_dirs"),
            recorded_learning_dirs=marker.get("learning_dirs"),
            explicit_learning_dirs=self.legacy_learning_dirs,
        )


# LLM: 扫描 mixin 只建立不可变快照，不写 marker、备份或任何规范账本。
# 类用途: 集中组织各类 legacy 数据的只读扫描步骤。
class _MigrationScanMixin:
    # LLM: Scanning delegates each fixed legacy authority to one bounded reader and never infers sources from prose.
    # 函数用途: 收集旧 ops、learning drafts、memory_gate、daily、long-term、导航和 lesson 的只读快照。
    def _scan(self) -> _MigrationSnapshot:
        state = _new_migration_scan_state(self)
        _scan_legacy_candidate_directories(self, state)
        _scan_legacy_ops(self, state)
        _scan_legacy_daily(self, state)
        _scan_legacy_long_term(self, state)
        _scan_legacy_lessons(self, state)
        _scan_legacy_navigation(self, state)
        _scan_legacy_retention(self, state)
        return _finalize_migration_snapshot(self, state)


# LLM: This mixin owns explicit apply mechanics only; source discovery remains the read-only scan mixin above.
# 类用途: 为已冻结快照执行全量备份、迁移写入、旧源归档与失败回滚。
class _MigrationApplyMixin:
    # LLM: 备份同时包含旧源和会被写入的正式目标；rollback 才能恢复到 apply 前精确状态。
    # 函数用途: 复制本次所有输入/输出文件并记录缺失目标。
    def _backup(self, snapshot: _MigrationSnapshot, backup_dir: Path) -> dict[Path, Path | None]:
        targets = {
            *snapshot.paths,
            self.candidates.path,
            self.long_term.path,
            Path(self.home.owner_memory_ops_jsonl),
            Path(self.home.owner_memory_md),
            Path(self.home.owner_memory_hot_md),
            Path(self.home.owner_memory_routing_index_md),
            self.marker_path,
            *snapshot.daily_destination_paths,
            *Path(self.home.owner_memory_daily_dir).glob("*.jsonl"),
            *Path(self.home.owner_memory_lessons_dir).glob("*.md"),
        }
        mapping: dict[Path, Path | None] = {}
        backup_dir.mkdir(parents=True, exist_ok=False)
        for source in sorted(targets, key=str):
            source = source.resolve(strict=False)
            if not source.exists() or not source.is_file():
                mapping[source] = None
                continue
            relative = _backup_relative_path(source, self.owner_home, Path(self.home.root))
            destination = backup_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            mapping[source] = destination
        write_json_file_atomic(
            backup_dir / "manifest.json",
            {
                "schema_version": MEMORY_MIGRATION_SCHEMA_VERSION,
                "created_at": _utc_now(),
                "files": [
                    {
                        "source": str(source),
                        "backup": str(destination or ""),
                        "existed": destination is not None,
                    }
                    for source, destination in sorted(mapping.items(), key=lambda item: str(item[0]))
                ],
            },
        )
        return mapping

    # LLM: 所有 legacy observations 一次性写 CandidateService；之后才清旧路径和正式 long-term。
    # 函数用途: 执行迁移快照并返回计数。
    def _apply_snapshot(self, snapshot: _MigrationSnapshot, backup_dir: Path) -> dict[str, int]:
        _assert_snapshot_unchanged(snapshot)
        observations: list[CandidateObservation] = []
        daily_events: list[DailyMemoryEvent] = []
        archived_sources = 0
        finding_categories = {finding.category for finding in snapshot.findings}

        observations.extend(self._legacy_directory_observations(snapshot, backup_dir))
        if "ops_candidates" in finding_categories:
            observations.extend(self._ops_observations(backup_dir))
        for path in snapshot.legacy_daily_paths:
            daily_events.extend(_daily_events_for_rewrite(path, backup_dir))
        for record in snapshot.long_term_records:
            record_observations, daily_event = _legacy_long_term_projection(
                record,
                backup_dir,
                source_path=self.long_term.path,
            )
            observations.extend(record_observations)
            if daily_event is not None:
                daily_events.append(daily_event)
        observations.extend(_legacy_markdown_observations(snapshot.legacy_lesson_paths, backup_dir))
        observations.extend(
            self._navigation_observations(snapshot.legacy_navigation_paths, backup_dir)
        )

        _assert_snapshot_unchanged(snapshot)
        committed_candidates = self.candidates.observe_many(tuple(observations)) if observations else []

        for path in snapshot.legacy_daily_paths:
            path.unlink(missing_ok=True)
        committed_daily = [self.daily.append(event) for event in daily_events]

        removed = 0
        if snapshot.long_term_records:
            removed = len(
                self.long_term.remove_migrated_legacy(
                    snapshot.long_term_records,
                    source="memory-migration-v2",
                    backup_ref=str(backup_dir / "manifest.json"),
                )
            )

        if "ops_candidates" in finding_categories:
            self._rewrite_ops_without_bodies()
        for directory in [*snapshot.legacy_gate_dirs, *snapshot.learning_dirs]:
            if directory.exists():
                _assert_migration_directory_source(
                    directory,
                    owner_home=self.owner_home,
                    external_learning_dirs=self.legacy_learning_dirs,
                )
                shutil.rmtree(directory)
                archived_sources += 1
        for path in snapshot.legacy_lesson_paths:
            if path.exists():
                _assert_owner_source(path, self.owner_home)
                path.unlink()
                archived_sources += 1
        memory_md = Path(self.home.owner_memory_md)
        memory_hot = Path(self.home.owner_memory_hot_md)
        if memory_md in snapshot.legacy_navigation_paths:
            write_text_file_atomic(memory_md, default_memory_md())
        if memory_hot in snapshot.legacy_navigation_paths:
            write_text_file_atomic(memory_hot, default_memory_hot_md())
        if "legacy_retention_policy" in finding_categories:
            _migrate_retention_policy(Path(self.home.owner_retention_json))
        if snapshot.legacy_lesson_paths:
            self.lessons.rebuild_routing_index()
        return {
            "migrated_candidates": len(committed_candidates),
            "migrated_daily_events": len(committed_daily),
            "removed_legacy_long_term": removed,
            "archived_sources": archived_sources,
        }

# LLM: This mixin converts frozen legacy sources and owns rollback helpers; it never selects new source paths.
# 类用途: 将旧正文投影为 Candidate，并负责 ops 脱敏、备份恢复和 marker 读取。
class _MigrationLegacyProjectionMixin:
    # LLM: 目录候选读取固定字段并以文件位置作证据；模型旧置信度不获得正式权威。
    # 函数用途: 将 learning_drafts 和 memory_gate JSON/JSONL 行转换为统一 pending Candidate。
    def _legacy_directory_observations(
        self,
        snapshot: _MigrationSnapshot,
        backup_dir: Path,
    ) -> list[CandidateObservation]:
        observations: list[CandidateObservation] = []
        for directory, origin in [
            *((path, "subagent_finding") for path in snapshot.legacy_gate_dirs),
            *((path, "model_inferred") for path in snapshot.learning_dirs),
        ]:
            for path in sorted(
                item
                for item in directory.rglob("*")
                if item.is_file() and not item.is_symlink()
            ):
                for index, row in enumerate(_read_legacy_objects(path), start=1):
                    content = _legacy_content(row)
                    if not content:
                        continue
                    observations.extend(
                        _candidate_chunks(
                            content,
                            category="lesson" if _looks_like_lesson(row, path) else "long_term_fact",
                            origin=origin,
                            source_path=path,
                            row_index=index,
                            backup_dir=backup_dir,
                        )
                    )
        return observations

    # LLM: 旧 ops 中的正文先转 Candidate，再用 hash-only operation record 替换；不得直接丢弃。
    # 函数用途: 提取旧 ops 候选观察。
    def _ops_observations(self, backup_dir: Path) -> list[CandidateObservation]:
        path = Path(self.home.owner_memory_ops_jsonl)
        report = read_jsonl_objects_report(path, context="memory_migration.ops.apply")
        observations: list[CandidateObservation] = []
        for index, row in enumerate(report.records, start=1):
            if not _ops_has_legacy_body(row):
                continue
            content = _legacy_content(row)
            if content:
                observations.extend(
                    _candidate_chunks(
                        content,
                        category="long_term_fact",
                        origin="model_inferred",
                        source_path=path,
                        row_index=index,
                        backup_dir=backup_dir,
                    )
                )
        return observations

    # LLM: memory.md/HOT 的 legacy 自由正文只能成为待审 Candidate；重置后运行时只读正式导航/marker。
    # 函数用途: 保存两个重复正文入口中的旧内容。
    def _navigation_observations(
        self,
        paths: Iterable[Path],
        backup_dir: Path,
    ) -> list[CandidateObservation]:
        observations: list[CandidateObservation] = []
        memory_hot = Path(self.home.owner_memory_hot_md)
        for path in paths:
            category = "lesson" if path == memory_hot else "long_term_fact"
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            observations.extend(
                _candidate_chunks(
                    text,
                    category=category,
                    origin="migrated_legacy",
                    source_path=path,
                    row_index=1,
                    backup_dir=backup_dir,
                )
            )
        return observations

    # LLM: ops v1 允许字段白名单固定；未知旧字段和所有正文只能留在加密/文件系统备份中。
    # 函数用途: 将 ops 重写为无正文审计。
    def _rewrite_ops_without_bodies(self) -> None:
        path = Path(self.home.owner_memory_ops_jsonl)
        if not path.exists():
            return
        report = read_jsonl_objects_report(path, context="memory_migration.ops.rewrite")
        rows = [_sanitized_ops_row(row) for row in report.records]
        text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        write_text_file_atomic(path, text)

    # LLM: rollback 按备份 manifest 精确恢复或删除 apply 新建文件；不能只恢复旧来源而留下半迁移正式数据。
    # 函数用途: 恢复 apply 前文件快照。
    def _restore_backup(self, mapping: dict[Path, Path | None]) -> list[MemoryMigrationError]:
        errors: list[MemoryMigrationError] = []
        for target, backup in sorted(mapping.items(), key=lambda item: str(item[0])):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            except OSError as exc:
                errors.append(
                    MemoryMigrationError(
                        "MEMORY_MIGRATION_ROLLBACK_FAILED",
                        str(target),
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        if not errors:
            try:
                restored = self.long_term.all()
                self.long_term._after_commit_indexes(restored)  # noqa: SLF001 - migration rollback rebuilds derivable indexes.
            except Exception as exc:  # noqa: BLE001 - surface a stable rollback error after authority restore.
                errors.append(
                    MemoryMigrationError(
                        "MEMORY_MIGRATION_ROLLBACK_INDEX_REBUILD_FAILED",
                        str(self.long_term.path),
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        return errors

    # LLM: marker 损坏必须阻断 plan/apply；缺失 marker 只是尚未迁移，不能与坏 JSON 混为一谈。
    # 函数用途: 读取 owner 迁移状态并返回结构化损坏错误。
    def _read_marker(self) -> tuple[dict[str, Any], MemoryMigrationError | None]:
        report = read_json_object_report(self.marker_path, context="memory_migration.marker")
        if report.load_error is None:
            return report.payload, None
        return {}, MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_MARKER",
            str(self.marker_path),
            str(
                report.load_error.get("error_type")
                or report.load_error.get("message")
                or "unreadable migration marker"
            ),
        )


# LLM: The public service composes lifecycle and bounded scan/apply helpers without adding a second migration path.
# 类用途: 对外提供唯一的 Memory v2 dry-run 与显式 apply 接口。
class MemoryMigrationService(
    _MigrationServiceCore,
    _MigrationScanMixin,
    _MigrationApplyMixin,
    _MigrationLegacyProjectionMixin,
):
    pass


# LLM: 遗留来源的廉价探针只按已知写入形状点名检查（owner home 根、data/、tasks/<date>/<task>/work/、
# 管理员显式传入的工作区根、以及上次 complete 扫描记录过的位置），命中即回落到完整递归扫描。
# 这里不枚举"所有可能深度"：memory_gate / learning_drafts 的写入方已被删除，稳态下不存在新来源。
# 函数用途: 用几十次 stat 代替一次整棵 owner home 的递归遍历，判断是否还有遗留目录需要迁移。
def _legacy_sources_at_contract_paths(
    *,
    owner_home: Path,
    tasks_dir: Path,
    data_dir: Path,
    recorded_gate_dirs: object = None,
    recorded_learning_dirs: object = None,
    explicit_learning_dirs: Iterable[str | Path] = (),
) -> bool:
    candidates: list[Path] = [
        *((owner_home / name) for name in _LEGACY_SOURCE_NAMES),
        data_dir / "learning_drafts",
    ]
    if tasks_dir.is_dir():
        # 历史写入方的真实形状：<tasks>/<date>/<task>/work/<legacy-name>（迁移测试同形状）。
        for name in _LEGACY_SOURCE_NAMES:
            candidates.extend(tasks_dir.glob(f"*/*/work/{name}"))
    candidates.extend(Path(path) for path in explicit_learning_dirs)
    candidates.extend(_recorded_paths(recorded_gate_dirs))
    candidates.extend(_recorded_paths(recorded_learning_dirs))
    return any(_path_present(path) for path in candidates)


# LLM: marker 里的历史位置是 JSON 列表；形状不对时按"无记录"处理，不能因此跳过扫描。
# 函数用途: 把 marker 记录的位置还原成路径列表。
def _recorded_paths(value: object) -> list[Path]:
    if not isinstance(value, (list, tuple)):
        return []
    return [Path(str(item)) for item in value if str(item or "").strip()]


# LLM: 存在性或符号链接任一成立都要回落完整扫描：符号链接在完整扫描里是 fail-closed 错误。
# 函数用途: 判断一个契约位置是否仍指向遗留来源。
def _path_present(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


# LLM: Directory discovery is restricted to explicit legacy names and administrator-supplied workspace roots.
# 函数用途: 初始化一次扫描状态，并把旧目录符号链接记录为关闭式错误。
def _new_migration_scan_state(service: object) -> _MigrationScanState:
    owner_home = Path(service.owner_home)
    legacy_gate_dirs = tuple(_safe_named_dirs(owner_home, "memory_gate"))
    learning_dirs = tuple(
        dict.fromkeys(
            [
                *_safe_named_dirs(owner_home, "learning_drafts"),
                *(
                    path
                    for path in service.legacy_learning_dirs
                    if path.exists() or path.is_symlink()
                ),
            ]
        )
    )
    state = _MigrationScanState(legacy_gate_dirs, learning_dirs)
    for path in _named_symlinks(
        owner_home,
        names={"memory_gate", "learning_drafts"},
    ):
        state.errors.append(
            MemoryMigrationError(
                "MEMORY_MIGRATION_LEGACY_SYMLINK",
                str(path),
                "legacy directory symlinks are not eligible for migration",
            )
        )
    return state


# LLM: Legacy candidate directories are parsed exhaustively but symlinks and non-directories never enter the source set.
# 函数用途: 扫描 learning_drafts 与 task-local memory_gate，统计可迁移记录并收集源文件。
def _scan_legacy_candidate_directories(
    service: object,
    state: _MigrationScanState,
) -> None:
    del service
    sources = [
        *((path, "task_memory_gate") for path in state.legacy_gate_dirs),
        *((path, "learning_drafts") for path in state.learning_dirs),
    ]
    for directory, category in sources:
        if directory.is_symlink():
            state.errors.append(
                MemoryMigrationError(
                    "MEMORY_MIGRATION_LEGACY_SYMLINK",
                    str(directory),
                    "legacy directory symlinks are not eligible for migration",
                )
            )
            continue
        if not directory.is_dir():
            state.errors.append(
                MemoryMigrationError(
                    "MEMORY_MIGRATION_INVALID_LEGACY_DIRECTORY",
                    str(directory),
                    "legacy candidate source is not a directory",
                )
            )
            continue
        entries = tuple(sorted(directory.rglob("*")))
        symlinks = [path for path in entries if path.is_symlink()]
        if symlinks:
            state.errors.append(
                MemoryMigrationError(
                    "MEMORY_MIGRATION_LEGACY_SYMLINK",
                    str(directory),
                    f"{len(symlinks)} symlink(s) are not eligible for migration",
                )
            )
        files = tuple(path for path in entries if path.is_file() and not path.is_symlink())
        state.paths.extend(files)
        count, file_errors = _count_legacy_records(files)
        state.errors.extend(file_errors)
        state.findings.append(
            _finding(category, directory, count, "migrate_to_candidates_then_archive")
        )


# LLM: Ops rows are considered legacy only when the fixed v1 body fields are present; hash-only audits remain untouched.
# 函数用途: 检测仍含候选正文的旧 ops 记录。
def _scan_legacy_ops(service: object, state: _MigrationScanState) -> None:
    path = Path(service.home.owner_memory_ops_jsonl)
    if not path.exists():
        return
    report = read_jsonl_objects_report(path, context="memory_migration.ops")
    state.errors.extend(_read_errors(report.load_errors, "MEMORY_MIGRATION_CORRUPT_OPS"))
    legacy_count = sum(1 for row in report.records if _ops_has_legacy_body(row))
    if legacy_count:
        state.paths.append(path)
        state.findings.append(
            _finding("ops_candidates", path, legacy_count, "migrate_and_redact_ops")
        )


# LLM: Daily v2 rows are schema- and shard-validated before any mixed file is selected for rewrite.
# 函数用途: 检测旧 Daily mirror，同时保护同文件中已经存在的 v2 事件。
def _scan_legacy_daily(service: object, state: _MigrationScanState) -> None:
    daily_dir = Path(service.home.owner_memory_daily_dir)
    for path in sorted(daily_dir.glob("*.jsonl")):
        report = read_jsonl_objects_report(path, context="memory_migration.daily")
        state.errors.extend(
            _read_errors(report.load_errors, "MEMORY_MIGRATION_CORRUPT_DAILY")
        )
        invalid_v2 = _invalid_daily_v2_count(report.records, day=path.stem)
        if invalid_v2:
            state.errors.append(
                MemoryMigrationError(
                    "MEMORY_MIGRATION_INVALID_DAILY_V2_ROW",
                    str(path),
                    f"{invalid_v2} v2 daily row(s) failed schema or shard validation",
                )
            )
        legacy = [
            row
            for row in report.records
            if row.get("schema_version") != DAILY_MEMORY_SCHEMA_VERSION
        ]
        if legacy:
            state.paths.append(path)
            state.legacy_daily_paths.append(path)
            state.findings.append(
                _finding("legacy_daily_mirror", path, len(legacy), "convert_to_daily_v2")
            )


# LLM: Existing v2 Daily rows must parse exactly and belong to their YYYY-MM-DD shard.
# 函数用途: 统计混合 Daily 文件中损坏或放错日期的 v2 记录。
def _invalid_daily_v2_count(records: Iterable[dict[str, object]], *, day: str) -> int:
    invalid = 0
    for row in records:
        if row.get("schema_version") != DAILY_MEMORY_SCHEMA_VERSION:
            continue
        try:
            event = DailyMemoryEvent.from_record(row)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if event.created_at[:10] != day:
            invalid += 1
    return invalid


# LLM: Long-term authority must parse completely before legacy dialogue or lesson records can be projected elsewhere.
# 函数用途: 检测旧 dialogue、lesson 和无 v2 身份的长期记录。
def _scan_legacy_long_term(service: object, state: _MigrationScanState) -> None:
    path = Path(service.home.owner_memory_long_term_jsonl)
    if not path.exists():
        return
    report = read_jsonl_objects_report(path, context="memory_migration.long_term")
    state.errors.extend(
        _read_errors(report.load_errors, "MEMORY_MIGRATION_CORRUPT_LONG_TERM")
    )
    invalid = [row for row in report.records if _memory_record(row) is None]
    if invalid:
        state.errors.append(
            MemoryMigrationError(
                "MEMORY_MIGRATION_INVALID_LONG_TERM_ROW",
                str(path),
                f"{len(invalid)} long-term row(s) failed schema validation",
            )
        )
        return
    try:
        active = tuple(service.long_term.all_including_expired())
    except Exception as exc:  # noqa: BLE001 - converted to stable migration error.
        state.errors.append(
            MemoryMigrationError(
                "MEMORY_MIGRATION_LONG_TERM_LOAD_FAILED",
                str(path),
                f"{type(exc).__name__}: {exc}",
            )
        )
        return
    state.long_term_records = tuple(record for record in active if _is_legacy_long_term(record))
    if state.long_term_records:
        state.paths.append(path)
        state.findings.append(
            _finding(
                "legacy_long_term",
                path,
                len(state.long_term_records),
                "candidate_or_daily_then_remove",
            )
        )


# LLM: Formal lesson markers are validated and preserved; only unmarked legacy Markdown enters migration.
# 函数用途: 检测旧 lesson 正文并阻断损坏的正式 marker。
def _scan_legacy_lessons(service: object, state: _MigrationScanState) -> None:
    directory = Path(service.home.owner_memory_lessons_dir)
    for path in sorted(directory.glob("*.md")):
        lesson_state, lesson_error = _lesson_file_state(path)
        if lesson_error is not None:
            state.errors.append(lesson_error)
        elif lesson_state == "legacy":
            state.legacy_lesson_paths.append(path)
            state.paths.append(path)
            state.findings.append(
                _finding("legacy_lesson_markdown", path, 1, "candidate_then_archive")
            )


# LLM: memory.md and memory-hot.md are selected only by formal/default markers, never by similarity to their prose.
#   "当前"文本有两个精确来源：home_memory_seeds 的正式默认，以及本 home 新 owner 实际会拿到的种子（根级模板副本，
#   来自 home_layout_v2.owner_navigation_seeds）；新 owner 刚初始化的文件因此不会被当成旧正文变成待审候选。
# 函数用途: 检测旧导航或重复 HOT 正文并保护正式 HOT marker。
def _scan_legacy_navigation(service: object, state: _MigrationScanState) -> None:
    from ..user_space.home_layout_v2 import owner_navigation_seeds

    seeded_memory, seeded_hot = _seeded_navigation_texts(service, owner_navigation_seeds)
    sources = (
        (Path(service.home.owner_memory_md), "legacy_memory_md", (default_memory_md(), seeded_memory), False),
        (
            Path(service.home.owner_memory_hot_md),
            "legacy_memory_hot",
            (default_memory_hot_md(), seeded_hot),
            True,
        ),
    )
    for path, category, defaults, allow_formal_hot in sources:
        text_state, text_error = _navigation_file_state(
            path,
            defaults=defaults,
            allow_formal_hot=allow_formal_hot,
        )
        if text_error is not None:
            state.errors.append(text_error)
        elif text_state == "legacy":
            state.legacy_navigation_paths.append(path)
            state.paths.append(path)
            state.findings.append(
                _finding(category, path, 1, "candidate_then_reset_navigation")
            )


# LLM: 只读根级模板路径；home 缺这两个字段（旧测试替身）或模板不可读时退回正式默认，不猜别的路径。
# 函数用途: 取本 home 新 owner 实际会拿到的 memory.md / memory-hot.md 种子文本。
def _seeded_navigation_texts(service: object, seeds) -> tuple[str, str]:
    home = service.home
    if getattr(home, "memory_md", None) is None or getattr(home, "memory_hot_md", None) is None:
        return default_memory_md(), default_memory_hot_md()
    try:
        return seeds(home)
    except (OSError, TypeError, ValueError):
        return default_memory_md(), default_memory_hot_md()


# LLM: Runtime reads only retention v2, so a valid v1 policy becomes an explicit one-time migration finding.
# 函数用途: 检测旧 retention policy 或记录损坏配置。
def _scan_legacy_retention(service: object, state: _MigrationScanState) -> None:
    path = Path(service.home.owner_retention_json)
    retention_state, retention_error = _retention_policy_state(path)
    if retention_error is not None:
        state.errors.append(retention_error)
    elif retention_state == "legacy":
        state.paths.append(path)
        state.findings.append(
            _finding("legacy_retention_policy", path, 1, "convert_to_retention_v2")
        )


# LLM: Snapshot identity covers every legacy source directory/file and every Daily destination derived from old dialogue.
# 函数用途: 去重路径、计算 apply 前指纹并冻结扫描结果。
def _finalize_migration_snapshot(
    service: object,
    state: _MigrationScanState,
) -> _MigrationSnapshot:
    unique_paths = tuple(dict.fromkeys(path.resolve(strict=False) for path in state.paths))
    fingerprint_paths = tuple(
        dict.fromkeys(
            path.resolve(strict=False)
            for path in [*unique_paths, *state.legacy_gate_dirs, *state.learning_dirs]
        )
    )
    daily_dir = Path(service.home.owner_memory_daily_dir)
    destinations = {
        path.resolve(strict=False) for path in state.legacy_daily_paths
    }
    for record in state.long_term_records:
        if str(record.kind or "").strip().lower() == "dialogue":
            created = _legacy_record_created_at(record)
            destinations.add(
                daily_memory_path(daily_dir, day=created[:10]).resolve(strict=False)
            )
    return _MigrationSnapshot(
        findings=tuple(state.findings),
        errors=tuple(state.errors),
        paths=unique_paths,
        legacy_daily_paths=tuple(state.legacy_daily_paths),
        legacy_gate_dirs=state.legacy_gate_dirs,
        learning_dirs=state.learning_dirs,
        legacy_lesson_paths=tuple(state.legacy_lesson_paths),
        legacy_navigation_paths=tuple(state.legacy_navigation_paths),
        daily_destination_paths=tuple(sorted(destinations, key=str)),
        long_term_records=state.long_term_records,
        fingerprint_paths=fingerprint_paths,
        fingerprint=_snapshot_fingerprint(fingerprint_paths),
    )


# LLM: 工作区旧 learning 路径必须由管理员显式传入；只允许精确的 data/learning_drafts 子目录。
# 函数用途: 将旧工作区根转换为可迁移目录，并拒绝文件系统根等过宽目标。
def _legacy_workspace_learning_dirs(
    roots: Iterable[str | Path],
) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in roots:
        root = Path(value).expanduser().resolve(strict=False)
        if root == Path(root.anchor):
            raise ValueError("legacy workspace root cannot be a filesystem root")
        result.append(root / "data" / "learning_drafts")
    return tuple(dict.fromkeys(result))


# LLM: owner 树中的旧目录 symlink 必须显式报错，不能因安全过滤而静默漏迁移。
# 函数用途: 找出指定旧目录名对应的符号链接。
def _named_symlinks(root: Path, *, names: set[str]) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.name in names and path.is_symlink()
        )
    )


# LLM: 目录名匹配后仍验证 resolve 在 owner_home 内，防止 symlink 或路径逃逸进入迁移范围。
# 函数用途: 安全查找旧目录。
def _safe_named_dirs(root: Path, name: str) -> list[Path]:
    result: list[Path] = []
    resolved_root = root.resolve(strict=False)
    for path in root.rglob(name):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if path.is_dir() and not path.is_symlink():
            result.append(path)
    return sorted(result)


# LLM: 旧记录计数严格解析 JSON/JSONL；坏文件返回稳定错误而非跳过。
# 函数用途: 统计旧目录内可迁移对象。
def _count_legacy_records(files: Iterable[Path]) -> tuple[int, list[MemoryMigrationError]]:
    count = 0
    errors: list[MemoryMigrationError] = []
    for path in files:
        try:
            count += len(_read_legacy_objects(path))
        except ValueError as exc:
            errors.append(
                MemoryMigrationError(
                    "MEMORY_MIGRATION_CORRUPT_LEGACY_SOURCE",
                    str(path),
                    str(exc),
                )
            )
    return count, errors


# LLM: 只解析 .json/.jsonl 文本对象；状态 Markdown 等非候选文件计为零但仍随目录归档。
# 函数用途: 读取一种旧候选文件。
def _read_legacy_objects(path: Path) -> list[dict[str, object]]:
    if path.suffix.lower() == ".jsonl":
        report = read_jsonl_objects_report(path, context="memory_migration.legacy_jsonl")
        if report.load_errors:
            raise ValueError(f"{len(report.load_errors)} unreadable JSONL row(s)")
        return report.records
    if path.suffix.lower() != ".json":
        return []
    report = read_json_object_report(path, context="memory_migration.legacy_json")
    if report.load_error is not None:
        raise ValueError("unreadable JSON object")
    payload = report.payload
    for key in ("candidates", "drafts", "items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            if any(not isinstance(item, dict) for item in value):
                raise ValueError(f"legacy {key} list contains a non-object item")
            return [dict(item) for item in value]
    return [payload] if payload else []


# LLM: 正文提取只服务迁移保存，选择固定字段；不把整段 JSON dump 当候选。
# 函数用途: 从未知旧记录取最可能的候选正文。
def _legacy_content(row: dict[str, object]) -> str:
    nested = row.get("candidate")
    if isinstance(nested, dict):
        value = _legacy_content(nested)
        if value:
            return value
    for key in _CONTENT_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# LLM: 路径/旧 type 只区分 lesson 与普通事实候选，不决定是否批准。
# 函数用途: 判断旧候选是否应落入 lesson 待审链。
def _looks_like_lesson(row: dict[str, object], path: Path) -> bool:
    kind = str(row.get("kind") or row.get("candidate_type") or row.get("target") or "").lower()
    return "lesson" in kind or "skill" in kind or "lesson" in path.name.lower()


# LLM: 超过 Candidate 上限的旧正文按确定字符段保存，所有段共享同一 source ref 且不静默截断。
# 函数用途: 将一段 legacy 内容转换为一个或多个统一观察。
def _candidate_chunks(
    content: str,
    *,
    category: str,
    origin: str,
    source_path: Path,
    row_index: int,
    backup_dir: Path,
) -> list[CandidateObservation]:
    text = " ".join(str(content or "").split())
    chunks = [text[index : index + _MAX_CANDIDATE_CHARS] for index in range(0, len(text), _MAX_CANDIDATE_CHARS)]
    result: list[CandidateObservation] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        digest = hashlib.sha256(
            f"{source_path}:{row_index}:{chunk_index}:{chunk}".encode("utf-8", "replace")
        ).hexdigest()[:24]
        is_lesson = category == "lesson"
        result.append(
            CandidateObservation(
                candidate_type="lesson" if is_lesson else category,
                content=chunk,
                subject_key=f"legacy.{category}.{digest}",
                scope=MemoryScope("personal", "personal", "由旧 Memory 数据迁移，待人工确认", ""),
                origin=origin if origin in {"model_inferred", "subagent_finding"} else "migrated_legacy",
                source_artifact_refs=(
                    {
                        "artifact_ref": _backup_ref(source_path, backup_dir),
                        "source_path_hash": _source_path_hash(source_path),
                        "row_index": row_index,
                        "content_hash": memory_content_hash(chunk),
                    },
                ),
                confidence=0.0,
                proposed_action="add",
                promotion_target="lesson" if is_lesson else "long_term",
                observation_id=f"migration:{digest}:{chunk_index}",
            )
        )
    return result


# LLM: dialogue 只迁入 daily；旧 fact/preference/lesson 只成为待审候选，不能因历史存在就自动正式化。
# 函数用途: 投影一条旧 active long-term 记录。
def _legacy_long_term_projection(
    record: MemoryRecord,
    backup_dir: Path,
    *,
    source_path: Path,
) -> tuple[list[CandidateObservation], DailyMemoryEvent | None]:
    kind = str(record.kind or "dialogue").strip().lower()
    created = _legacy_record_created_at(record)
    artifact_ref = _backup_ref(source_path, backup_dir)
    if kind == "dialogue":
        actor = "user" if record.role == "user" else "main_agent"
        return [], DailyMemoryEvent(
            event_type="conversation",
            summary=" ".join(record.content.split())[:1_500],
            actor=actor,
            origin="model_inferred" if actor != "user" else "user_explicit",
            artifact_refs=(
                {
                    "artifact_ref": artifact_ref,
                    "source_path_hash": _source_path_hash(source_path),
                    "entry_id": record.entry_id,
                    "content_hash": memory_content_hash(record.content),
                },
            ),
            created_at=created,
            curator_run_id="memory-migration-v2",
        )
    candidate_type = {
        "lesson": "lesson",
        "preference": "user_preference",
        "event": "event",
        "project": "project",
    }.get(kind, "long_term_fact")
    text = " ".join(record.content.split())
    chunks = [
        text[index : index + _MAX_CANDIDATE_CHARS]
        for index in range(0, len(text), _MAX_CANDIDATE_CHARS)
    ]
    observations: list[CandidateObservation] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        digest = hashlib.sha256(
            f"{record.entry_id}:{record.version}:{chunk_index}:{chunk}".encode(
                "utf-8",
                "replace",
            )
        ).hexdigest()[:24]
        observations.append(
            CandidateObservation(
                candidate_type=candidate_type,
                content=chunk,
                subject_key=f"legacy.{kind}.{digest}",
                scope=MemoryScope(
                    "personal",
                    "personal",
                    "由旧正式记忆迁移，待人工确认",
                    "",
                ),
                origin="migrated_legacy",
                source_artifact_refs=(
                    {
                        "artifact_ref": artifact_ref,
                        "source_path_hash": _source_path_hash(source_path),
                        "entry_id": record.entry_id,
                        "content_hash": memory_content_hash(record.content),
                    },
                ),
                proposed_action="add",
                promotion_target=(
                    "lesson"
                    if candidate_type == "lesson"
                    else "user"
                    if candidate_type == "user_preference"
                    else "long_term"
                ),
                observation_id=(
                    f"migration:long-term:{record.entry_id}:"
                    f"{record.version}:{chunk_index}"
                ),
            )
        )
    return observations, None


# LLM: 混合分片先保留既有 v2 事件，再把 legacy 行投影为摘要；重写后 event_id 和顺序由唯一 Store 校验。
# 函数用途: 为一个含新旧行的 daily 分片构造完整重写事件集。
def _daily_events_for_rewrite(path: Path, backup_dir: Path) -> list[DailyMemoryEvent]:
    report = read_jsonl_objects_report(path, context="memory_migration.daily.apply")
    events: list[DailyMemoryEvent] = []
    for index, row in enumerate(report.records, start=1):
        if row.get("schema_version") == DAILY_MEMORY_SCHEMA_VERSION:
            events.append(DailyMemoryEvent.from_record(row))
            continue
        content = _legacy_content(row)
        role = str(row.get("role") or row.get("speaker") or "system").lower()
        actor = "user" if role == "user" else "tool" if role == "tool" else "main_agent"
        origin = (
            "user_explicit"
            if actor == "user"
            else "tool_verified"
            if actor == "tool" and _legacy_tool_succeeded(row)
            else "model_inferred"
        )
        created = _legacy_iso(row.get("created_at") or row.get("observed_at"), fallback_day=path.stem)
        summary = " ".join(content.split())[:1_500]
        if not summary:
            summary = "旧 Daily 记录无可提炼正文；完整结构保存在迁移备份。"
        events.append(
            DailyMemoryEvent(
                event_type=(
                    "tool_result"
                    if actor == "tool"
                    else "conversation"
                    if content
                    else "summary"
                ),
                summary=summary,
                actor=actor,
                origin=origin,
                session_id=str(row.get("session_id") or ""),
                thread_id=str(row.get("thread_id") or ""),
                request_id=str(row.get("request_id") or ""),
                task_id=str(row.get("task_id") or ""),
                run_id=str(row.get("run_id") or ""),
                artifact_refs=(
                    {
                        "artifact_ref": _backup_ref(path, backup_dir),
                        "source_path_hash": _source_path_hash(path),
                        "row_index": index,
                        "content_hash": memory_content_hash(
                            content
                            or json.dumps(row, ensure_ascii=False, sort_keys=True)
                        ),
                    },
                ),
                created_at=created,
                curator_run_id="memory-migration-v2",
            )
        )
    return events


# LLM: legacy lesson Markdown 只成为待审 lesson Candidate，正式 marker 文件必须由 PromotionService 创建。
# 函数用途: 转换旧 lesson 文件。
def _legacy_markdown_observations(paths: Iterable[Path], backup_dir: Path) -> list[CandidateObservation]:
    observations: list[CandidateObservation] = []
    for path in paths:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            observations.extend(
                _candidate_chunks(
                    text,
                    category="lesson",
                    origin="migrated_legacy",
                    source_path=path,
                    row_index=1,
                    backup_dir=backup_dir,
                )
            )
    return observations


# LLM: current long-term 必须由 PromotionService 带 candidate_id 和 typed scope 写入；其余 active 行均需迁移。
# 函数用途: 识别旧 dialogue、kind=lesson 及无正式 provenance/scope 的历史记录。
def _is_legacy_long_term(record: MemoryRecord) -> bool:
    attributes = record.attributes if isinstance(record.attributes, dict) else {}
    return (
        str(record.kind or "").lower() in {"dialogue", "lesson"}
        or not str(attributes.get("candidate_id") or "")
        or not str(attributes.get("scope_type") or "")
        or not str(attributes.get("scope_key") or "")
    )


# LLM: MemoryRecord 宽松兼容未知字段，但迁移扫描必须确认最小 role/content 类型。
# 函数用途: 校验旧 long-term 行是否可安全构造。
def _memory_record(row: dict[str, object]) -> MemoryRecord | None:
    if not isinstance(row.get("role"), str) or not isinstance(row.get("content"), str):
        return None
    action = str(row.get("action") or "add").strip().lower()
    if action != "remove" and not str(row.get("content") or "").strip():
        return None
    known = MemoryRecord.__dataclass_fields__
    try:
        return MemoryRecord(**{key: value for key, value in row.items() if key in known})
    except (TypeError, ValueError):
        return None


# LLM: ops 只要任意正文键或嵌套 candidate 正文存在就必须迁移/清除。
# 函数用途: 检测旧 ops 候选正文。
def _ops_has_legacy_body(row: dict[str, object]) -> bool:
    return bool(_legacy_content(row))


# LLM: sanitized ops 只保留 v1 合同字段；正文仅留下不可逆 content_hash。
# 函数用途: 构造无正文操作审计行。
def _sanitized_ops_row(row: dict[str, object]) -> dict[str, object]:
    content = _legacy_content(row)
    allowed = {
        "schema",
        "event",
        "action",
        "entry_id",
        "entry_ids",
        "version",
        "origin",
        "subject_key",
        "scope_type",
        "scope_key",
        "content_hash",
        "content_hashes",
        "source",
        "result",
        "observed_at",
    }
    payload = {key: value for key, value in row.items() if key in allowed}
    payload["schema"] = "my-agent.memory-operation.v1"
    payload["event"] = str(payload.get("event") or "legacy_candidate_migrated")
    payload["result"] = str(payload.get("result") or "migrated")
    if content:
        payload["content_hash"] = memory_content_hash(content)
    return payload


# LLM: retention runtime 只接受 v2；旧 retention.v1 在迁移器中显式识别，未知/坏策略阻断整轮 apply。
# 函数用途: 区分当前、旧版和损坏的 owner retention 策略。
def _retention_policy_state(
    path: Path,
) -> tuple[str, MemoryMigrationError | None]:
    report = read_json_object_report(path, context="memory_migration.retention")
    if report.load_error is not None:
        return "error", MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY",
            str(path),
            str(report.load_error.get("error_type") or "unreadable retention policy"),
        )
    payload = report.payload
    if payload.get("schema_version") == RETENTION_SCHEMA_VERSION:
        try:
            MemoryRetentionPolicy.from_payload(payload)
        except (TypeError, ValueError) as exc:
            return "error", MemoryMigrationError(
                "MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY",
                str(path),
                str(exc),
            )
        return "current", None
    if payload.get("schema_version") == "retention.v1":
        try:
            _retention_v2_payload(payload)
        except (TypeError, ValueError) as exc:
            return "error", MemoryMigrationError(
                "MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY",
                str(path),
                str(exc),
            )
        return "legacy", None
    return "error", MemoryMigrationError(
        "MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY",
        str(path),
        "unsupported retention schema_version",
    )


# LLM: policy 转换必须在备份后一次性写 v2；正式 runtime 不保留旧 key fallback。
# 函数用途: 将一个已验证的 retention.v1 文件原子转换为当前策略。
def _migrate_retention_policy(path: Path) -> None:
    report = read_json_object_report(path, context="memory_migration.retention.apply")
    if report.load_error is not None:
        raise ValueError("MEMORY_MIGRATION_CORRUPT_RETENTION_POLICY")
    write_json_file_atomic(path, _retention_v2_payload(report.payload))


# LLM: v1 中已存在的数值保留管理员选择，新类别使用 v2 安全默认；错类型不静默纠正。
# 函数用途: 构造当前 retention policy payload。
def _retention_v2_payload(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("schema_version") != "retention.v1":
        raise ValueError("legacy retention schema_version is invalid")

    # LLM: nested reader rejects bool/negative values and preserves explicit v1 zero-disable semantics.
    # 函数用途: 读取一个旧版非负整数保留期。
    def old_days(key: str, default: int) -> int:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"legacy retention field {key} is invalid")
        return value

    # LLM: v1 布尔字段不接受字符串或数字，避免迁移改变管理员意图。
    # 函数用途: 读取一个旧版布尔开关。
    def old_bool(key: str, default: bool) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"legacy retention field {key} is invalid")
        return value

    held = payload.get("legal_hold_task_ids", [])
    if not isinstance(held, list) or any(
        not isinstance(item, str) or not item.strip() for item in held
    ):
        raise ValueError("legacy legal_hold_task_ids is invalid")
    return MemoryRetentionPolicy(
        conversation_days=365,
        audit_days=old_days("raw_days", 90),
        daily_days=old_days("daily_days", 365),
        tool_output_days_after_terminal=30,
        rejected_candidate_days=30,
        curator_run_days=90,
        compact_days=old_days("compact_days", 365),
        completed_task_days=old_days("task_completed_days", 365),
        cache_days=old_days("cache_days", 30),
        tmp_days=old_days("tmp_days", 7),
        trash_days=old_days("trash_days", 30),
        subagent_scratch_days=old_days("subagent_scratch_days", 30),
        legal_hold=old_bool("legal_hold", False),
        legal_hold_task_ids=tuple(dict.fromkeys(item.strip() for item in held)),
        maintenance_enabled=old_bool("maintenance_enabled", True),
        maintenance_interval_seconds=old_days("maintenance_interval_seconds", 86_400),
    ).to_dict()


# LLM: 正式 marker 若已出现但无法解析属于损坏，不能降级成 legacy 自由文本后静默迁移。
# 函数用途: 区分正式 lesson、旧 Markdown 和损坏的正式 lesson。
def _lesson_file_state(
    path: Path,
) -> tuple[str, MemoryMigrationError | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "error", MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_LESSON",
            str(path),
            f"{type(exc).__name__}: unreadable lesson",
        )
    first, separator, rest = text.partition("\n")
    prefix = "<!-- my-agent-lesson-meta:"
    suffix = " -->"
    if not first.startswith(prefix):
        return "legacy", None
    if not first.endswith(suffix) or not separator:
        return "error", MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_LESSON_MARKER",
            str(path),
            "formal lesson marker is incomplete",
        )
    try:
        payload = json.loads(first[len(prefix) : -len(suffix)])
    except json.JSONDecodeError:
        payload = None
    required = ("lesson_id", "candidate_id", "subject_key", "scope", "path")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != LESSON_SCHEMA_VERSION
        or any(not payload.get(key) for key in required)
        or not rest.partition("\n\n")[2].strip()
    ):
        return "error", MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_LESSON_MARKER",
            str(path),
            "formal lesson metadata or body failed validation",
        )
    return "formal", None


# LLM: memory.md 的自由正文必须迁移；HOT 只有完全符合程序 marker/render 合同才可原样保留。
# 函数用途: 区分默认导航、正式 HOT、旧自由文本和不可读文件。
def _navigation_file_state(
    path: Path,
    *,
    defaults: tuple[str, ...],
    allow_formal_hot: bool,
) -> tuple[str, MemoryMigrationError | None]:
    if not path.exists():
        return "current", None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "error", MemoryMigrationError(
            "MEMORY_MIGRATION_CORRUPT_NAVIGATION",
            str(path),
            f"{type(exc).__name__}: unreadable navigation file",
        )
    if any(text.strip() == default.strip() for default in defaults):
        return "current", None
    if not allow_formal_hot:
        return "legacy", None
    return _formal_hot_text_state(path, text)


# LLM: HOT 显示行必须能由 marker 数据精确重建；混入任意自由正文时整体按 legacy 备份迁移。
# 函数用途: 严格验证程序生成的 memory-hot.md。
def _formal_hot_text_state(
    path: Path,
    text: str,
) -> tuple[str, MemoryMigrationError | None]:
    lines = text.splitlines()
    prefix = "<!-- my-agent-hot-meta:"
    suffix = " -->"
    marker_indexes = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if not marker_indexes:
        return "legacy", None
    allowed_header = {
        "",
        "# Memory HOT",
        "这里只放经过正式 lesson 和重复证据晋升的短规则；详细说明见对应 lesson。",
    }
    consumed: set[int] = set()
    for index in marker_indexes:
        line = lines[index]
        if not line.endswith(suffix):
            return "error", MemoryMigrationError(
                "MEMORY_MIGRATION_CORRUPT_HOT_MARKER",
                str(path),
                "formal HOT marker is incomplete",
            )
        try:
            payload = json.loads(line[len(prefix) : -len(suffix)])
        except json.JSONDecodeError:
            payload = None
        required = ("hot_id", "candidate_id", "lesson_id", "lesson_ref", "rule")
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != HOT_SCHEMA_VERSION
            or any(not payload.get(key) for key in required)
        ):
            return "error", MemoryMigrationError(
                "MEMORY_MIGRATION_CORRUPT_HOT_MARKER",
                str(path),
                "formal HOT metadata failed validation",
            )
        display_index = index + 1
        expected = f"- {payload['rule']}（详见 `{payload['lesson_ref']}`）"
        if display_index >= len(lines) or lines[display_index] != expected:
            return "error", MemoryMigrationError(
                "MEMORY_MIGRATION_CORRUPT_HOT_DISPLAY",
                str(path),
                "formal HOT display does not match metadata",
            )
        consumed.update({index, display_index})
    for index, line in enumerate(lines):
        if index in consumed or line in allowed_header:
            continue
        return "legacy", None
    return "formal", None


# LLM: JSONL 读取错误转换为稳定迁移 error_code，不泄露正文。
# 函数用途: 规范底层读取诊断。
def _read_errors(errors: Iterable[dict[str, object]], code: str) -> list[MemoryMigrationError]:
    return [
        MemoryMigrationError(
            code,
            str(error.get("path") or ""),
            str(error.get("error_type") or error.get("message") or "unreadable data"),
        )
        for error in errors
    ]


# LLM: finding fingerprint 只含文件 hash，不含正文。
# 函数用途: 构造一个扫描结果。
def _finding(category: str, path: Path, count: int, action: str) -> MemoryMigrationFinding:
    return MemoryMigrationFinding(
        category=category,
        source_path=str(path),
        record_count=max(0, int(count)),
        action=action,
        source_fingerprint=_path_fingerprint(path),
    )


# LLM: snapshot hash 让 apply/report 可核对输入集合，但不代替锁内重新扫描。
# 函数用途: 计算所有旧源的稳定指纹。
def _snapshot_fingerprint(paths: Iterable[Path]) -> str:
    material = "\n".join(f"{path}:{_path_fingerprint(path)}" for path in sorted(paths, key=str))
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


# LLM: scan 后任一旧源新增、删除或改写都必须中止并回滚，不能把过期 plan 应用到另一批数据。
# 函数用途: 在迁移读取前和正式写入前重新核对完整源集合指纹。
def _assert_snapshot_unchanged(snapshot: _MigrationSnapshot) -> None:
    current = _snapshot_fingerprint(snapshot.fingerprint_paths)
    if current != snapshot.fingerprint:
        raise RuntimeError("MEMORY_MIGRATION_SOURCE_CHANGED")


# LLM: 目录指纹递归组合文件 hash；symlink 不进入旧数据迁移。
# 函数用途: 计算一个源路径指纹。
def _path_fingerprint(path: Path) -> str:
    if path.is_dir():
        material = "\n".join(
            f"{item.relative_to(path)}:{_file_hash(item)}"
            for item in sorted(path.rglob("*"))
            if item.is_file() and not item.is_symlink()
        )
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()
    return _file_hash(path)


# LLM: 文件 hash 是迁移幂等/备份证据，不读取为模型上下文。
# 函数用途: 计算 SHA-256 或 missing 标记。
def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


# LLM: run_id 同一秒还绑定 snapshot 前缀，避免并发/重试覆盖备份目录。
# 函数用途: 生成迁移运行编号。
def _migration_run_id(fingerprint: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"memory-v2-{stamp}-{fingerprint[:12]}"


# LLM: 备份相对路径不允许 ..；owner 外的 system 文件放 external/hash 下。
# 函数用途: 为 manifest 生成安全目标路径。
def _backup_relative_path(source: Path, owner_home: Path, home_root: Path) -> Path:
    for prefix, label in ((owner_home, "owner"), (home_root, "home")):
        try:
            return Path(label) / source.relative_to(prefix.resolve(strict=False))
        except ValueError:
            continue
    digest = hashlib.sha256(str(source).encode("utf-8", "replace")).hexdigest()[:16]
    return Path("external") / digest / source.name


# LLM: artifact_ref 必须指向实际存在的 manifest；source_path_hash 在 manifest 内精确定位原文件。
# 函数用途: 返回本次迁移备份清单地址。
def _backup_ref(source: Path, backup_dir: Path) -> str:
    del source
    return str(backup_dir / "manifest.json")


# LLM: 原路径可能包含 owner 身份，候选只保存不可逆 hash，完整路径仅留在管理员备份 manifest。
# 函数用途: 为备份来源生成脱敏定位键。
def _source_path_hash(source: Path) -> str:
    return hashlib.sha256(str(source.resolve(strict=False)).encode("utf-8", "replace")).hexdigest()


# LLM: legacy 时间只接受 epoch 或 ISO；坏值回退分片日期 UTC 零点。
# 函数用途: 生成合法 Daily created_at。
def _legacy_iso(value: object, *, fallback_day: str) -> str:
    try:
        fallback = datetime.fromisoformat(f"{fallback_day}T00:00:00+00:00")
    except ValueError:
        fallback = datetime.now(timezone.utc)
    if isinstance(value, (int, float)) and float(value) > 0:
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return parsed.isoformat() if parsed.date().isoformat() == fallback_day else fallback.isoformat()
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
                return (
                    parsed.isoformat()
                    if parsed.date().isoformat() == fallback_day
                    else fallback.isoformat()
                )
        except ValueError:
            pass
    return fallback.isoformat()


# LLM: 旧长期记录缺时间时只给迁移时刻，不得用“较新”推导其事实优先级。
# 函数用途: 生成旧长期记录对应 Daily 的合法 created_at。
def _legacy_record_created_at(record: MemoryRecord) -> str:
    timestamp = float(record.created_at or 0.0)
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return _utc_now()


# LLM: 旧工具行只有明确成功终态才可保留 tool_verified；未知/失败一律降为 model_inferred 摘要。
# 函数用途: 判断 legacy Daily 工具事件是否有结构化成功证据。
def _legacy_tool_succeeded(row: dict[str, object]) -> bool:
    if row.get("success") is True:
        return True
    status = str(row.get("status") or row.get("result_status") or "").strip().lower()
    return status in {"success", "succeeded", "completed", "ok"}


# LLM: 删除旧路径前重新验证其仍在 owner_home 内且不是 symlink，防止 scan/apply 间目标漂移。
# 函数用途: 为显式迁移的文件和目录删除做最终路径边界检查。
def _assert_owner_source(path: Path, owner_home: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"migration source became a symlink: {path.name}")
    resolved = path.resolve(strict=False)
    root = owner_home.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("migration source escaped owner home") from exc
    if not relative.parts:
        raise RuntimeError("migration cannot delete the owner home root")


# LLM: owner 外仅允许删除构造器登记的精确 learning_drafts 目录，绝不接受其父工作区或任意子路径。
# 函数用途: 在归档后删除旧候选目录前复核最终目标边界。
def _assert_migration_directory_source(
    path: Path,
    *,
    owner_home: Path,
    external_learning_dirs: Iterable[Path],
) -> None:
    if path.is_symlink():
        raise RuntimeError(f"migration source became a symlink: {path.name}")
    resolved = path.resolve(strict=False)
    owner_root = owner_home.resolve(strict=False)
    try:
        relative = resolved.relative_to(owner_root)
    except ValueError:
        relative = None
    if relative is not None:
        if not relative.parts:
            raise RuntimeError("migration cannot delete the owner home root")
        return
    allowed = {
        candidate.resolve(strict=False)
        for candidate in external_learning_dirs
    }
    if resolved not in allowed:
        raise RuntimeError("migration source is not an explicit legacy learning directory")


# LLM: 迁移时间统一 UTC aware ISO，不用于判定新旧事实真伪。
# 函数用途: 返回当前迁移审计时间。
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MEMORY_MIGRATION_SCHEMA_VERSION",
    "MemoryMigrationError",
    "MemoryMigrationFinding",
    "MemoryMigrationReport",
    "MemoryMigrationService",
]
