from __future__ import annotations

"""把由自动晋升写入 AGENTS.md 的条目安全清理成学习排除名单的迁移。"""

# LLM: 迁移只动「由 memory-promotion 自动写入」的 AGENTS 条目：任何存在非 promotion 来源
#   记录（agent_tool、system_baseline、迁移自身）的条目一律保护；执行前先快照三份权威文件，
#   删除走 PersonaRepository 的唯一 CAS 通道，名单只追加、幂等，重跑只处理剩余条目。
# 模块用途: 计算并执行 AGENTS 自动晋升条目的清理迁移，记录快照、归档与迁移记录。

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
)
from ..common.json_io import (
    append_private_jsonl_records,
    read_jsonl_objects_report,
    write_private_json_file_atomic,
    write_private_text_file_atomic,
)
from ..user_space.owner_policy import LearningExclusionEntry, append_learning_exclusions

# 自动晋升写入 Persona 时使用的来源前缀（见 promotion._commit_persona）。
_PROMOTION_SOURCE_PREFIX = "memory-promotion:"
# PersonaRepository 的 batch 硬上限是 32 条操作。
_PERSONA_BATCH_OPERATIONS_COUNT = 32
_MIGRATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SNAPSHOT_META = "snapshot.json"
_ARCHIVE_NAME = "archive.jsonl"
_RECORD_NAME = "record.json"


@dataclass(frozen=True)
class AgentsCleanupRequest:
    # LLM: 迁移参数打包成一个请求对象，保持服务函数参数在工程限制内。
    migration_id: str
    conversations: tuple[str, ...] = ()
    actor: str = "memory-admin-cli"
    reason: str = ""


@dataclass(frozen=True)
class AgentsCleanupPlan:
    # LLM: 计划含条目定位与正文快照（供归档），to_dict 输出剔除正文，dry-run 不打印内容。
    entries_total: int
    promotion_entries: int
    protected_entries: int
    removable: tuple[dict[str, object], ...]
    sha256: str

    # 函数用途: 把计划转成不含条目正文的结构化报告。
    def to_dict(self) -> dict[str, object]:
        return {
            "entries_total": self.entries_total,
            "promotion_entries": self.promotion_entries,
            "protected_entries": self.protected_entries,
            "removable_count": len(self.removable),
            "removable_entry_ids": [str(item["entry_id"]) for item in self.removable],
            "removable": [
                {key: value for key, value in item.items() if key != "content"}
                for item in self.removable
            ],
            "sha256": self.sha256,
        }


# LLM: 计划与执行读同一份权威：当前 AGENTS 条目 + versions.jsonl；条目是否有 promotion 来源
#   用 source 前缀判定，任何非 promotion 记录都进入保护集合（宁可少删也不误删）。
# 函数用途: 计算当前可清理的自动晋升条目（只读，不修改任何文件）。
def plan_agents_cleanup(*, persona: PersonaRepository) -> AgentsCleanupPlan:
    report = persona.list_entries("agents")
    entries = report.get("entries")
    entry_rows = entries if isinstance(entries, list) else []
    promotion, protected = _source_index(persona.history("agents"))
    removable: list[dict[str, object]] = []
    promotion_hits = 0
    protected_hits = 0
    for entry in entry_rows:
        entry_id = str(entry.get("entry_id") or "").strip()
        if not entry_id:
            continue
        if entry_id in promotion:
            promotion_hits += 1
        if entry_id in protected:
            protected_hits += 1
        if entry_id not in promotion or entry_id in protected:
            continue
        info = promotion[entry_id]
        removable.append(
            {
                "entry_id": entry_id,
                "version": int(info.get("version") or 0),
                "candidate_id": str(info.get("candidate_id") or ""),
                "blocked": bool(entry.get("blocked")),
                "content": str(entry.get("content") or ""),
            }
        )
    return AgentsCleanupPlan(
        entries_total=len(entry_rows),
        promotion_entries=promotion_hits,
        protected_entries=protected_hits,
        removable=tuple(removable),
        sha256=str(report.get("sha256") or ""),
    )


# LLM: 执行顺序固定为 快照→归档→删除→名单→记录；快照存在即复用（不覆盖），重跑只处理剩余
#   条目，名单追加幂等；任何一步失败都保留已完成部分并如实抛出，重跑继续收敛。
# 函数用途: 执行一次 AGENTS 清理迁移并返回结构化报告。
def apply_agents_cleanup(
    *,
    home_paths: object,
    persona: PersonaRepository,
    candidate_service: object,
    request: AgentsCleanupRequest,
) -> dict[str, object]:
    migration_id = _validated_migration_id(request.migration_id)
    plan = plan_agents_cleanup(persona=persona)
    directory = _migration_dir(home_paths, migration_id)
    notes: list[str] = []
    removed_count = 0
    archived_count = 0
    snapshot_reused = False
    snapshot_meta: dict[str, object] | None = None
    if plan.removable:
        snapshot_meta = _ensure_snapshot(home_paths, directory)
        snapshot_reused = bool(snapshot_meta.get("reused"))
        archived_count = _archive_removable(directory, plan, candidate_service)
        removed_count = _remove_entries(persona, plan, migration_id)
    else:
        notes.append("nothing_to_remove")
    conversations_added = 0
    if request.conversations:
        conversations_added = append_learning_exclusions(
            home_paths, _conversation_entries(request)
        )
    report: dict[str, object] = {
        "ok": True,
        "migration_id": migration_id,
        "removable_count": len(plan.removable),
        "removed_count": removed_count,
        "archived_count": archived_count,
        "snapshot_dir": str(directory),
        "snapshot_reused": snapshot_reused,
        "snapshot": snapshot_meta,
        "archive_ref": str(directory / _ARCHIVE_NAME) if plan.removable else "",
        "conversations_added": conversations_added,
        "notes": notes,
    }
    if plan.removable or conversations_added:
        _write_record(directory, report, request)
    return report


# LLM: source 前缀是唯一判定口径：memory-promotion 记录贡献可清理集合，其他记录（含 batch
#   的 operations）全部保护；promotion 的 remove 不贡献（条目已不在当前文档）。
# 函数用途: 从版本记录里分出自动晋升来源与受保护条目。
def _source_index(
    rows: list[dict[str, object]],
) -> tuple[dict[str, dict[str, object]], set[str]]:
    promotion: dict[str, dict[str, object]] = {}
    protected: set[str] = set()
    for row in rows:
        source = str(row.get("source") or "")
        if source.startswith(_PROMOTION_SOURCE_PREFIX):
            _index_promotion_row(promotion, row, source)
            continue
        _index_protected_row(protected, row)
    return promotion, protected


# LLM: 同一条目可能有多条晋升记录（replace 链），保留最大 version 作为归档版本。
# 函数用途: 记录一条 promotion 版本记录到索引。
def _index_promotion_row(
    promotion: dict[str, dict[str, object]],
    row: dict[str, object],
    source: str,
) -> None:
    if str(row.get("action") or "") not in {"add", "replace"}:
        return
    entry_id = str(row.get("entry_id") or "").strip()
    if not entry_id:
        return
    version = int(row.get("version") or 0)
    current = promotion.get(entry_id)
    if current is not None and version < int(current.get("version") or 0):
        return
    promotion[entry_id] = {
        "version": version,
        "candidate_id": source.rsplit(":", 1)[-1],
    }


# LLM: 任何非 promotion 来源的 entry_id 都保护：单条记录的 entry_id 与 batch 记录的操作条目。
# 函数用途: 记录一条受保护版本记录到集合。
def _index_protected_row(protected: set[str], row: dict[str, object]) -> None:
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        protected.add(entry_id)
    operations = row.get("operations")
    if isinstance(operations, list):
        protected.update(
            str(operation.get("entry_id") or "").strip()
            for operation in operations
            if isinstance(operation, dict)
        )
        protected.discard("")


# LLM: 快照写三份权威文件（AGENTS.md、persona/versions.jsonl、memory/candidates.jsonl）与
#   一个完成标记 snapshot.json；标记存在即视为快照完整并复用，绝不覆盖已有快照。
# 函数用途: 确保迁移快照存在，返回快照元数据。
def _ensure_snapshot(home_paths: object, directory: Path) -> dict[str, object]:
    meta_path = directory / _SNAPSHOT_META
    if meta_path.exists():
        return {"reused": True, "snapshot_dir": str(directory)}
    files = {
        "agents": Path(str(getattr(home_paths, "owner_agents_md", "") or "")),
        "versions": Path(str(getattr(home_paths, "owner_home_dir", "") or ""))
        / "persona"
        / "versions.jsonl",
        "candidates": Path(str(getattr(home_paths, "owner_memory_candidates_jsonl", "") or "")),
    }
    snapshot_dir = directory / "snapshot"
    _ensure_dir(snapshot_dir)
    hashes: dict[str, object] = {}
    for name, source in files.items():
        if not source.name or not source.exists():
            hashes[name] = None
            continue
        content = source.read_text(encoding="utf-8")
        write_private_text_file_atomic(snapshot_dir / name, content)
        encoded = content.encode("utf-8")
        hashes[name] = {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
    meta = {
        "migration_id": directory.name.removeprefix("agents-migration-"),
        "created_at": _now_iso(),
        "files": hashes,
    }
    write_private_json_file_atomic(meta_path, meta)
    return {"reused": False, "snapshot_dir": str(directory), "files": hashes}


# LLM: 归档先于删除，含被删条目正文与来源候选引用；重跑时按 entry_id 去重，不重复归档。
# 函数用途: 把本次将删除的条目追加到迁移归档，返回新增行数。
def _archive_removable(
    directory: Path,
    plan: AgentsCleanupPlan,
    candidate_service: object,
) -> int:
    archive_path = directory / _ARCHIVE_NAME
    existing = _read_archive_entry_ids(archive_path)
    records: list[dict[str, object]] = []
    for item in plan.removable:
        entry_id = str(item["entry_id"])
        if entry_id in existing:
            continue
        candidate_id = str(item.get("candidate_id") or "")
        records.append(
            {
                "entry_id": entry_id,
                "candidate_id": candidate_id,
                "version": int(item.get("version") or 0),
                "content": str(item.get("content") or ""),
                "source_message_refs": _candidate_refs(candidate_service, candidate_id),
                "archived_at": _now_iso(),
            }
        )
    if records:
        append_private_jsonl_records(archive_path, records)
    return len(records)


# LLM: 删除沿 PersonaRepository 的 batch 通道分批（每批 ≤32），批间用上一批返回的 sha 做 CAS；
#   每批 source 记录迁移身份，便于版本账本回溯。
# 函数用途: 分批删除计划内条目，返回实际删除条数。
def _remove_entries(
    persona: PersonaRepository,
    plan: AgentsCleanupPlan,
    migration_id: str,
) -> int:
    sha = plan.sha256
    removed = 0
    items = list(plan.removable)
    for start in range(0, len(items), _PERSONA_BATCH_OPERATIONS_COUNT):
        chunk = items[start : start + _PERSONA_BATCH_OPERATIONS_COUNT]
        operations = tuple(
            PersonaMutationRequest(
                target="agents",
                action="remove",
                entry_id=str(item["entry_id"]),
            )
            for item in chunk
        )
        result = persona.mutate_batch(
            PersonaBatchMutationRequest(
                target="agents",
                operations=operations,
                expected_sha256=sha,
                source=f"migration:{migration_id}",
            )
        )
        sha = str(result.get("sha256") or sha)
        removed += sum(
            1
            for operation in result.get("operations") or ()
            if isinstance(operation, dict) and operation.get("changed")
        )
    return removed


# LLM: 迁移记录写到迁移目录，记录最后一次执行的计数与快照位置，不含条目正文。
# 函数用途: 写入迁移记录文件。
def _write_record(
    directory: Path,
    report: dict[str, object],
    request: AgentsCleanupRequest,
) -> None:
    _ensure_dir(directory)
    record = {
        "migration_id": report.get("migration_id", ""),
        "applied_at": _now_iso(),
        "removed_count": report.get("removed_count", 0),
        "removable_count": report.get("removable_count", 0),
        "conversations": [str(item) for item in request.conversations if str(item).strip()],
        "conversations_added": report.get("conversations_added", 0),
        "snapshot_dir": report.get("snapshot_dir", ""),
        "archive_ref": report.get("archive_ref", ""),
        "actor": request.actor,
        "notes": report.get("notes", []),
    }
    write_private_json_file_atomic(directory / _RECORD_NAME, record)


# LLM: 来源候选引用只做结构化复制；候选不存在或读取失败时归档仍保留（引用为空），不阻断迁移。
# 函数用途: 读取一条候选的来源消息引用。
def _candidate_refs(candidate_service: object, candidate_id: str) -> list[dict[str, object]]:
    if not candidate_id or candidate_service is None:
        return []
    try:
        candidate = candidate_service.get(candidate_id)
    except Exception:
        return []
    refs = getattr(candidate, "source_message_refs", ()) or ()
    return [dict(ref) for ref in refs if isinstance(ref, dict)]


# LLM: 归档去重只按 entry_id：同一条目重跑时不重复写正文。
# 函数用途: 读取归档里已有的 entry_id 集合。
def _read_archive_entry_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    report = read_jsonl_objects_report(path, context="agents_migration.archive")
    return {str(row.get("entry_id") or "") for row in report.records}


# LLM: 名单条目一次构造，added_at/actor 来自请求，保证迁移审计可回溯。
# 函数用途: 把请求里的会话列表转成学习排除条目。
def _conversation_entries(request: AgentsCleanupRequest) -> tuple[LearningExclusionEntry, ...]:
    return tuple(
        LearningExclusionEntry(
            channel_conversation_id=str(conversation).strip(),
            reason=request.reason or "agents-cleanup",
            added_at=_now_iso(),
            added_by=request.actor,
        )
        for conversation in request.conversations
        if str(conversation).strip()
    )


# LLM: migration_id 会进入目录名，只允许安全字符，防止路径穿越。
# 函数用途: 校验并返回迁移编号。
def _validated_migration_id(value: str) -> str:
    migration_id = str(value or "").strip()
    if not _MIGRATION_ID_PATTERN.match(migration_id):
        raise ValueError("migration_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return migration_id


# LLM: 迁移目录固定在 owner persona/migrations 下，按 migration_id 寻址。
# 函数用途: 返回一次迁移的目录路径。
def _migration_dir(home_paths: object, migration_id: str) -> Path:
    owner_home = Path(str(getattr(home_paths, "owner_home_dir", "") or ""))
    return owner_home / "persona" / "migrations" / f"agents-migration-{migration_id}"


# LLM: 专用目录按 0700 收敛；chmod 失败不阻断（后续文件写仍按 0600）。
# 函数用途: 确保迁移目录存在并尽量收紧权限。
def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


# 函数用途: 返回当前 UTC 时间的 ISO 字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AgentsCleanupPlan",
    "AgentsCleanupRequest",
    "apply_agents_cleanup",
    "plan_agents_cleanup",
]
