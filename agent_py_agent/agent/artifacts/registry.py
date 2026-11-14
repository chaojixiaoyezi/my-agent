
# LLM: 解析缓存只复用本次no-follow新读完整raw的投影，不缓存IO成功或权限结论；默认追加/fsync顺序不变。
# 模块用途: 保存产物唯一账本并以不可变投影消除重复JSON解析，对外返回独立记录。
"""LLM: Maintain the append-only artifact registry below one canonical task workspace.

模块用途: 保存用户可见交付物的唯一账本；收口、看板和父代理都从这里解析产物，宿主读写时
通过 no-follow 文件接口防止 shell 把内部目录替换成链接后越界。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.json_io import _flock_descriptor, _funlock_descriptor, path_thread_lock
from ..common.nofollow_fs import (
    NoFollowPathError,
    append_text_beneath,
    open_private_lock_beneath_tightened,
    read_bytes_beneath,
    read_text_beneath,
    write_bytes_atomic_beneath,
)
from ..runtime_errors import runtime_error_report
from .registry_projection_cache import detached_payload, registry_projection, trusted_root_identity
from .registry_reader import inheritance_scope_matches, lookup_record_with_errors

SCHEMA_VERSION = "artifact_registry.v1"
REGISTRY_RELATIVE_PATH = Path("data") / "artifacts" / "registry.jsonl"
ARTIFACT_ROLE_METADATA_KEY = "artifact_role"
ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE = "tool_output_archive"
SHELL_PREIMAGE_POLICY_METADATA_KEY = "shell_preimage_policy"
SHELL_PREIMAGE_POLICY_EXCLUDE = "exclude"
SHELL_PREIMAGE_POLICY_INCLUDE = "include"


@dataclass(frozen=True)
class ArtifactRegistration:
    """Inputs for registering one concrete artifact file."""

    workspace_root: Path
    path: str | Path
    artifact_id: str = ""
    run_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    kind: str = ""
    mime_type: str = ""
    source: str = ""
    created_by_tool: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactGroupRegistration:
    """Inputs for registering one logical deliverable made of multiple files."""

    workspace_root: Path
    paths: list[str | Path]
    artifact_id: str
    run_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    kind: str = "group"
    source: str = ""
    created_by_tool: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRegistryRecord:
    """One latest-or-historical artifact registry entry."""

    schema_version: str
    artifact_id: str
    run_id: str
    task_id: str
    agent_id: str
    kind: str
    mime_type: str
    path: str
    sha256: str
    size_bytes: int
    status: str
    source: str
    created_by_tool: str
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactRegistryReadReport:
    """Latest registry records plus non-fatal read/parse diagnostics."""

    records: dict[str, ArtifactRegistryRecord] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    bytes_read: int = 0
    rows_read: int = 0


@dataclass(frozen=True)
class ArtifactRegistryLookupReport:
    """Registry lookup result for one artifact id or path."""

    record: ArtifactRegistryRecord | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


# LLM: 身份由宿主传入，Attempt 编号只作来源标识；不从目录名、模型正文或注册内容推断 owner/request。
# 类用途: 描述一次登记表继承的准确来源和目标。
@dataclass(frozen=True)
class ArtifactRegistryAttempt:
    owner_home: Path
    workspace_root: Path
    owner_id: str
    request_id: str
    attempt_id: str


# LLM: 只继承登记表，不创建工具输出索引或读取权限；宿主须在来源退出、目标普通登记开始前显式调用。
# 函数用途: 为同一 owner/request 的下一次 Attempt 保留已登记产物。
def inherit_artifact_registry(previous: ArtifactRegistryAttempt, current: ArtifactRegistryAttempt) -> int:
    previous_home, current_home = _workspace_root_path(previous.owner_home), _workspace_root_path(current.owner_home)
    if not inheritance_scope_matches(
        {'owner_id': previous.owner_id, 'owner_home': str(previous_home), 'request_id': previous.request_id},
        {'owner_id': current.owner_id, 'owner_home': str(current_home), 'request_id': current.request_id},
    ):
        return 0
    if (not isinstance(previous.attempt_id, str) or not previous.attempt_id.strip()
            or not isinstance(current.attempt_id, str) or not current.attempt_id.strip()
            or previous.attempt_id == current.attempt_id):
        return 0
    source, target = _workspace_root_path(previous.workspace_root), _workspace_root_path(current.workspace_root)
    source.relative_to(current_home)
    target.relative_to(current_home)
    if source == target:
        return 0
    report = latest_artifact_records_report(source)
    if report.errors:
        raise ValueError('artifact inheritance source registry is unreadable')
    if not report.records:
        return 0
    # 启动阶段的继承重试串行化；普通登记不持此锁，调用方不得让它与继承并发。
    with _inheritance_registry_lock(current_home, target):
        current_report = latest_artifact_records_report(target)
        if current_report.errors:
            raise ValueError('artifact inheritance target registry is unreadable')
        inherited = [record for key, record in report.records.items() if key not in current_report.records]
        if inherited:
            lines = ''.join(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + '\n' for record in inherited)
            append_text_beneath(target, tuple(REGISTRY_RELATIVE_PATH.parts), lines)
        return len(inherited)


# LLM: 复用既有线程锁和跨平台描述符锁；锁文件及父目录必须从可信 home 逐层 no-follow 打开。
# 函数用途: 串行化目标登记表继承，不让创建锁的过程跟随被替换的工作目录链接。
@contextmanager
def _inheritance_registry_lock(owner_home: Path, workspace_root: Path):
    path = registry_path(workspace_root)
    relative = (*workspace_root.relative_to(owner_home).parts, *REGISTRY_RELATIVE_PATH.parts[:-1], path.name + '.lock')
    handle = path_thread_lock(path)
    with handle.lock:
        descriptor = open_private_lock_beneath_tightened(owner_home, relative)
        try:
            _flock_descriptor(descriptor)
            try:
                yield
            finally:
                _funlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def registry_path(workspace_root: str | Path) -> Path:
    """Return the canonical registry path for a run workspace."""

    return _workspace_root_path(workspace_root) / REGISTRY_RELATIVE_PATH


def register_artifact(request: ArtifactRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry and return the current record."""

    root = _workspace_root_path(request.workspace_root)
    path = Path(request.path).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=_artifact_id(request, resolved),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or _kind_from_path(resolved)),
        mime_type=str(request.mime_type or ""),
        path=str(resolved),
        sha256=_sha256_file(resolved),
        size_bytes=_size_bytes(resolved),
        status=_status_from_request(request, resolved),
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=dict(request.metadata or {}),
    )
    _append_record(root, record)
    return record


# LLM: Shell post-check and migrations already hold observed file facts. This append-only seam
# must persist those facts without resolving or opening the path again, because the path may have
# become a symlink or disappeared after an external process ran. Atomic publication is opt-in;
# the caller must hold its registry writer lock across the complete read and publication.
# 函数用途: 将安全观测到的产物状态登记到原账本，允许已持写锁的调用方选择整行原子发表。
def register_observed_artifact(
    request: ArtifactRegistration,
    *,
    observed_sha256: str,
    observed_size_bytes: int,
    atomic_publish: bool = False,
) -> ArtifactRegistryRecord:
    """Append trusted observations without touching the referenced filesystem path."""

    root = _workspace_root_path(request.workspace_root)
    path = Path(request.path).expanduser()
    if not path.is_absolute():
        raise ValueError("observed artifact path must be absolute")
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=str(request.artifact_id or "").strip(),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or _kind_from_path(path)),
        mime_type=str(request.mime_type or ""),
        path=str(path),
        sha256=str(observed_sha256 or ""),
        size_bytes=max(0, int(observed_size_bytes)),
        status=str(request.status or "").strip(),
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=dict(request.metadata or {}),
    )
    if not record.artifact_id or not record.status:
        raise ValueError("observed artifact requires artifact_id and status")
    _append_record(root, record, atomic_publish=atomic_publish)
    return record


def register_artifact_group(request: ArtifactGroupRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry for a logical artifact group."""

    root = _workspace_root_path(request.workspace_root)
    member_paths = [_resolve_member_path(root, path) for path in request.paths]
    common_path = _group_common_path(root, member_paths)
    member_rows = [_member_metadata(root, path) for path in member_paths]
    status = str(request.status or "").strip() or ("ready" if all(Path(row["path"]).is_file() for row in member_rows) else "missing")
    metadata = {
        **dict(request.metadata or {}),
        "artifact_type": "file_group",
        "members": member_rows,
    }
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=str(request.artifact_id or "").strip() or _group_artifact_id(request, member_paths),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or "group"),
        mime_type="",
        path=str(common_path),
        sha256=_sha256_group(member_rows),
        size_bytes=sum(int(row.get("size_bytes") or 0) for row in member_rows),
        status=status,
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=metadata,
    )
    _append_record(root, record)
    return record


def latest_artifact_records(workspace_root: str | Path) -> dict[str, ArtifactRegistryRecord]:
    """Read the registry and return latest record by artifact_id."""

    return latest_artifact_records_report(workspace_root).records


# LLM: 每次仍读取完整raw并验证可信根，缓存命中仅省split/loads；IO拒绝必须返回当次诊断。
# 函数用途: 安全读取最新产物记录与坏行报告，返回独立副本。
def latest_artifact_records_report(workspace_root: str | Path, *, max_bytes: int | None = None,
                                   max_rows: int | None = None, max_diagnostics: int | None = None) -> ArtifactRegistryReadReport:
    """Read latest records and keep malformed rows as diagnostics."""

    root = _workspace_root_path(workspace_root)
    path = registry_path(root)
    if max_bytes is None and (max_rows is not None or max_diagnostics is not None):
        raise ValueError("bounded registry parsing requires a byte budget")
    if max_bytes is not None:
        max_rows = 1024 if max_rows is None else max_rows
        max_diagnostics = 8 if max_diagnostics is None else max_diagnostics
        if any(type(value) is not int or value < 1 for value in (max_rows, max_diagnostics)):
            raise ValueError("registry row and diagnostic budgets must be positive integers")
    try:
        if max_bytes is None:
            text = read_text_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts))
        else:
            if type(max_bytes) is not int or max_bytes < 1:
                raise ValueError("artifact registry read budget must be a positive byte count")
            try:
                # 固定合法相对路径/正字节预算；底层超过预算时只读预算+1哨兵并抛ValueError。
                raw = read_bytes_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts), max_bytes=max_bytes)
            except ValueError:
                return ArtifactRegistryReadReport(bytes_read=max_bytes + 1, errors=[{
                    "error_code": "ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED", "max_bytes": max_bytes,
                    "coverage": "partial", "effect_outcome": "not_started",
                    "next_action": "原登记簿超出本次读取字节预算；核验原受限精确引用，不得当作空登记簿。",
                }])
            text = raw.decode("utf-8") if raw is not None else None
        identity = trusted_root_identity(root) if text is not None else None
    except (NoFollowPathError, OSError, UnicodeError) as exc:
        error = runtime_error_report(exc, context="artifact_registry.read")
        error["path"] = str(path)
        return ArtifactRegistryReadReport(errors=[error])
    if text is None:
        return ArtifactRegistryReadReport()
    if max_bytes is not None:
        return _bounded_registry_report(text, len(raw), max_rows, max_diagnostics)
    return _projection_report(path, registry_projection(identity, text))


def bounded_registry_report(raw: bytes, *, max_rows: int = 1024, max_diagnostics: int = 8) -> ArtifactRegistryReadReport:
    """只解析原句柄已经限额取得的快照；不重开路径，不截断后伪造latest记录。"""
    if not isinstance(raw, bytes) or any(type(value) is not int or value < 1 for value in (max_rows, max_diagnostics)):
        raise ValueError('bounded registry snapshot and budgets are invalid')
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        return ArtifactRegistryReadReport(bytes_read=len(raw), errors=[{
            'error_code': 'ARTIFACT_REGISTRY_ENCODING_INVALID', 'exception_type': type(error).__name__,
            'coverage': 'partial', 'effect_outcome': 'not_started'}])
    return _bounded_registry_report(text, len(raw), max_rows, max_diagnostics)


# 先数物理行，再解析；兼容splitlines的CRLF/Unicode换行，不先构造无限错误对象树。
_REGISTRY_LINE_END = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')


def _bounded_registry_report(text, byte_count, max_rows, max_diagnostics):
    def budget_error(code, rows):
        return ArtifactRegistryReadReport(bytes_read=byte_count, rows_read=rows, errors=[{
            'error_code': code, 'coverage': 'partial', 'effect_outcome': 'not_started',
            'next_action': '登记读取或解析达到预算，未覆盖内容仍未知；核验原精确引用，不把截断当空或不存在。'}])

    rows, end = 0, 0
    for match in _REGISTRY_LINE_END.finditer(text):
        rows, end = rows + 1, match.end()
        if rows > max_rows:
            return budget_error('ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED', rows)
    rows += int(end < len(text))
    if rows > max_rows:
        return budget_error('ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED', rows)
    records, errors = {}, []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = _record_from_payload(json.loads(line))
            if record is None:
                raise ValueError('invalid registry record')
            if record.artifact_id:
                records[record.artifact_id] = record
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            errors.append({'error_code': 'ARTIFACT_REGISTRY_ROW_INVALID', 'exception_type': type(error).__name__,
                           'line': number, 'coverage': 'partial', 'effect_outcome': 'not_started'})
            if len(errors) >= max_diagnostics and number < rows:
                # 达到上限即停，末条诊断明确未覆盖；不能把此前记录当作当前latest。
                errors[-1] = budget_error('ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED', number).errors[0]
                return ArtifactRegistryReadReport(errors=errors, bytes_read=byte_count, rows_read=rows)
    return ArtifactRegistryReadReport(records=records if not errors else {}, errors=errors,
                                      bytes_read=byte_count, rows_read=rows)


# LLM: 原JSON容器上的record转换已随投影完成；只解冻预聚合的最新记录并转换坏行诊断，不重复克隆已被覆盖的历史行。
# 函数用途: 由当前raw投影组装独立的最新记录和逐行诊断。
def _projection_report(path: Path, projection) -> ArtifactRegistryReadReport:
    errors = []
    for row in projection.diagnostic_rows:
        try:
            if row.error is not None:
                raise row.failure()
            if row.record_error is not None:
                kind, message = row.record_error
                raise {"OverflowError": OverflowError, "RecursionError": RecursionError}[kind](message)
            raise ValueError("invalid artifact registry record")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            error = runtime_error_report(exc, context="artifact_registry.read_line")
            errors.append({**error, "path": str(path), "line": row.line})
    latest = {
        artifact_id: _detached_record(record)
        for artifact_id, record in projection.latest_records.items()
    }
    return ArtifactRegistryReadReport(records=latest, errors=errors)


# LLM: 标量字段已按原JSON类型转换且不可变；只复制metadata树，不重复逐字段转换或共享可变结果。
# 函数用途: 快速创建一个调用方可以独立修改metadata的产物记录。
def _detached_record(record: ArtifactRegistryRecord) -> ArtifactRegistryRecord:
    values = vars(record).copy()
    values["metadata"] = detached_payload(record.metadata)
    return ArtifactRegistryRecord(**values)


# LLM: 每次安全重读raw，复用JSON投影但不复用操作完成结论；保留历史匹配与原损坏拒绝口径。
# 函数用途: 一次安全读取账本，批量找出同一 shell operation 已落账的产物编号。
def artifact_operation_recorded_ids(
    workspace_root: str | Path,
    *,
    operation_key: str,
    expected_backup_refs: dict[str, str],
) -> set[str]:
    if not expected_backup_refs:
        return set()
    root = _workspace_root_path(workspace_root)
    try:
        text = read_text_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts))
        identity = trusted_root_identity(root) if text is not None else None
    except (NoFollowPathError, OSError, UnicodeError) as exc:
        raise OSError("artifact registry cannot be scanned safely") from exc
    recorded: set[str] = set()
    if text is None:
        return recorded
    for row in registry_projection(identity, text or "").records:
        try:
            if row.error is not None:
                raise row.failure()
        except json.JSONDecodeError as exc:
            raise OSError("artifact registry contains malformed rows") from exc
        payload = row.payload
        if not isinstance(payload, Mapping):
            raise OSError("artifact registry contains invalid rows")
        artifact_id = row.artifact_id
        metadata = payload.get("metadata")
        if (
            artifact_id in expected_backup_refs
            and isinstance(metadata, Mapping)
            and metadata.get("protection_operation_id") == operation_key
            and metadata.get("backup_ref") == expected_backup_refs[artifact_id]
        ):
            recorded.add(artifact_id)
    return recorded


# LLM: Durable recovery checks may need to match a historical row after a later append superseded
# it. Read every row and compare only explicit metadata keys supplied by host code.
# 函数用途: 扫描完整产物账本，确认某个 artifact_id 是否存在匹配指定结构化元数据的历史记录。
def artifact_metadata_record_exists(
    workspace_root: str | Path,
    *,
    artifact_id: str,
    required_metadata: dict[str, object],
) -> bool:
    root = _workspace_root_path(workspace_root)
    try:
        text = read_text_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts))
    except (NoFollowPathError, OSError, UnicodeError) as exc:
        raise OSError("artifact registry cannot be scanned safely") from exc
    if text is None:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError("artifact registry contains malformed rows") from exc
        if not isinstance(payload, dict):
            raise OSError("artifact registry contains invalid rows")
        metadata = payload.get("metadata")
        values = metadata if isinstance(metadata, dict) else {}
        if (
            str(payload.get("artifact_id") or "") == artifact_id
            and all(values.get(key) == value for key, value in required_metadata.items())
        ):
            return True
    return False


def resolve_artifact_record(
    workspace_root: str | Path,
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> ArtifactRegistryRecord | None:
    """Resolve by id first, then by exact current path."""

    return resolve_artifact_record_report(workspace_root, artifact_id, path=path).record


def resolve_artifact_record_report(
    workspace_root: str | Path,
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> ArtifactRegistryLookupReport:
    """Resolve a registry record and preserve registry read diagnostics."""

    report = latest_artifact_records_report(workspace_root)
    record, errors = lookup_record_with_errors(report.records, report.errors, artifact_id, path=path)
    return ArtifactRegistryLookupReport(record=record, errors=errors)


# LLM: Registry writes are anchored at the trusted task root and reject symlinked framework
# directories. Keep fsync semantics because recovery decisions consume this ledger after crashes.
# Atomic mode requires the caller's writer lock over read + replace; post-rename errors stay unknown.
# 函数用途: 默认安全追加并刷盘；显式原子模式保留完整旧字节，再一次发表旧账本与完整新行。
def _append_record(
    workspace_root: Path, record: ArtifactRegistryRecord, *, atomic_publish: bool = False,
) -> None:
    line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    try:
        from .registry_locator import prepare_record

        prepare_record(workspace_root, record)
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning('ARTIFACT_LOCATOR_PREPARE_FAILED error_type=%s next_action=bounded_locator_recovery', type(error).__name__)
    if atomic_publish:
        parts = tuple(REGISTRY_RELATIVE_PATH.parts)
        previous = read_bytes_beneath(workspace_root, parts) or b""
        write_bytes_atomic_beneath(workspace_root, parts, previous + line.encode("utf-8"))
    else:
        append_text_beneath(workspace_root, tuple(REGISTRY_RELATIVE_PATH.parts), line)
    # 索引失败不把已成功原登记谎报成未执行；正式查询沿原run索引有界自愈，miss始终不作确定不存在。
    try:
        from .registry_locator import publish_record

        publish_record(workspace_root, record)
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning('ARTIFACT_LOCATOR_UPDATE_FAILED canonical_registry_committed=true error_type=%s next_action=bounded_locator_recovery', type(error).__name__)


# LLM: The workspace path is host-owned but its leaf may be replaced by shell. Normalize only
# lexical components here; no registry entry point may resolve and follow that replacement link.
# 函数用途: 返回不跟随符号链接的绝对工作区路径。
def _workspace_root_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(Path(value).expanduser()))))


# LLM: 原JSON容器上保留唯一字段转换和非法metadata拒绝；不得先冻结list再改变str或哈希语义。
# 函数用途: 按原合同把JSON对象转换成产物记录，供投影在冷解析时调用一次。
def _record_from_payload(payload: object) -> ArtifactRegistryRecord | None:
    if not isinstance(payload, dict):
        return None
    try:
        return ArtifactRegistryRecord(
            schema_version=str(payload.get("schema_version") or ""),
            artifact_id=str(payload.get("artifact_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            kind=str(payload.get("kind") or ""),
            mime_type=str(payload.get("mime_type") or ""),
            path=str(payload.get("path") or ""),
            sha256=str(payload.get("sha256") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            status=str(payload.get("status") or ""),
            source=str(payload.get("source") or ""),
            created_by_tool=str(payload.get("created_by_tool") or ""),
            updated_at=float(payload.get("updated_at") or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )
    except (TypeError, ValueError):
        return None


def _artifact_id(request: ArtifactRegistration, path: Path) -> str:
    explicit = str(request.artifact_id or "").strip()
    if explicit:
        return explicit
    seed = "|".join(
        [
            str(request.run_id or ""),
            str(request.task_id or ""),
            str(request.agent_id or ""),
            str(path),
        ]
    )
    return f"art_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _group_artifact_id(request: ArtifactGroupRegistration, paths: list[Path]) -> str:
    seed = "|".join([str(request.run_id or ""), str(request.task_id or ""), *[str(path) for path in paths]])
    return f"artgrp_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _kind_from_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "artifact"


def _status_from_request(request: ArtifactRegistration, path: Path) -> str:
    explicit = str(request.status or "").strip()
    if explicit:
        return explicit
    return "ready" if path.is_file() else "missing"


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _resolve_member_path(root: Path, path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)


def _group_common_path(root: Path, paths: list[Path]) -> Path:
    if not paths:
        return root
    try:
        common = Path(os.path.commonpath([str(path) for path in paths]))
    except ValueError:
        return root
    return common if common.is_dir() else common.parent


def _member_metadata(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "relative_path": _relative_path(root, path),
        "kind": _kind_from_path(path),
        "sha256": _sha256_file(path),
        "size_bytes": _size_bytes(path),
        "exists": path.is_file(),
    }


def _relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _sha256_group(member_rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(member_rows, key=lambda item: str(item.get("relative_path") or item.get("path") or "")):
        digest.update(str(row.get("relative_path") or row.get("path") or "").encode("utf-8"))
        digest.update(str(row.get("sha256") or "").encode("utf-8"))
        digest.update(str(row.get("size_bytes") or 0).encode("utf-8"))
    return digest.hexdigest()
