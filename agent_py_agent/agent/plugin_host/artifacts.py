# LLM: D2只读canonical注册账，插件永远不提供路径；A/D1组合根提供已按原任务/线程权威裁剪的typed范围。
# 模块用途: 列出和读取已注册工作区制品，以owner/任务/业务根、FD no-follow、实际hash和size守住边界。
from __future__ import annotations

import base64
import hashlib
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, SCHEMA_VERSION, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.nofollow_fs import open_readonly_file_beneath
from ..path_access_policy import (
    host_config_root_for_path,
    host_credential_for_path,
    host_managed_store_for_path,
    host_state_for_path,
)
from .contracts import AuthorizedCall, DomainResult
from .contracts import validate_artifact_read as _validate_artifact_read
from .errors import HostAPIError
from .memory import _ObservationCursors, _owner_bytes, _page, _payload, _read_context, _result
from .schemas import canonical_json

# 单次完整hash读取的硬预算；过大制品明确拒绝，不跳过校验回正文。
ARTIFACT_HASH_MAX_BYTES = 64 * 1024 * 1024
# 每个hash读取块的字节上限，不一次性装入整个大文件。
ARTIFACT_IO_CHUNK_BYTES = 65536


# LLM: 该范围只能由宿主任务权威resolver提供，不能从插件JSON构造。
# 类用途: 保存单个已授权任务的注册根和业务可读根。
@dataclass(frozen=True)
class ArtifactTaskScope:
    owner: object
    task_id: str
    workspace_root: Path
    read_roots: tuple[Path, ...]


# LLM: 复用A0唯一closed schema，path/owner等未知或身份注入字段在任何领域IO前拒绝。
# 函数用途: 验证制品ref、固定hash及分片字节范围。
def validate_artifact_read(payload: dict) -> dict:
    return _validate_artifact_read(payload)


# LLM: 不resolve外部注册路径；保留原组件，在FD遍历之前拒绝相对路径、点段与非owner路径。
# 函数用途: 校验一个宿主提供的绝对路径属于当前owner。
def _owner_path(path, home) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(home.home_dir):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return path


# LLM: task_ids只允许A已核的created-by-installation集合；explicit任务也必须由resolver再次核原任务/thread归属。
# 函数用途: 对宿主返回的任务范围再次交集，拒绝跨owner和宽泛管理根。
def _allowed_scopes(authorized, context, scopes):
    task_grant = authorized.binding.grant.get("tasks", {})
    allowed = set(task_grant.get("explicit_ids", ()))
    if task_grant.get("created_by_installation") is True:
        allowed.update(authorized.resource_scope.get("task_ids", ()))
    home = context.owner_home
    business = (home.tasks_dir, home.workspace_dir, home.artifacts_dir, home.runs_dir, home.home_dir / "audits")
    result = []
    for scope in scopes:
        if not isinstance(scope, ArtifactTaskScope) or scope.owner != context.identity or scope.task_id not in allowed:
            continue
        root = _owner_path(scope.workspace_root, home)
        roots = tuple(_owner_path(path, home) for path in scope.read_roots)
        if not any(root.is_relative_to(base) for base in business):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        if not roots or any(not any(path.is_relative_to(base) for base in business) for path in roots):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        result.append(ArtifactTaskScope(scope.owner, scope.task_id, root, roots))
    if len(result) > 100:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return result


# LLM: 原parser兼容宽松字段，插件公开视图补齐必要类型与schema检查；坏行仍交原reader记录诊断。
# 函数用途: 解析一个可公开的canonical注册条目。
def _registry_record(raw):
    if (not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION
            or type(raw.get("size_bytes")) is not int or raw["size_bytes"] < 0
            or type(raw.get("updated_at")) not in {int, float} or not math.isfinite(raw["updated_at"])):
        raise ValueError("registry")
    for key in ("artifact_id", "task_id", "path", "sha256"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError("registry")
    if (len(raw["artifact_id"].encode()) > 128 or len(raw["task_id"].encode()) > 128
            or re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is None):
        raise ValueError("registry")
    try:
        datetime.fromtimestamp(raw["updated_at"], timezone.utc)
    except (OverflowError, OSError) as error:
        raise ValueError("timestamp") from error
    return _record_from_payload(raw)


# LLM: 只读取任务resolver提供的canonical账，复用原latest-record解析，坏行只返回安全Code而不带路径。
# 函数用途: 读取一个任务的最新注册项、快照指纹和损坏诊断。
def _registry(context, scope):
    path = scope.workspace_root / REGISTRY_RELATIVE_PATH
    raw = _owner_bytes(context, path)
    try:
        rows, errors = latest_records_from_lines(path, raw.decode("utf-8").split("\n"), _registry_record)
    except UnicodeError:
        rows, errors = {}, [True]
    rows = {key: row for key, row in rows.items() if row.task_id == scope.task_id}
    return rows, hashlib.sha256(raw).hexdigest(), ["ARTIFACT_REGISTRY_CORRUPT"] if errors else []


# LLM: 原宿主保护目录之外，制品还排除任务控制记录和秘密惯用名；即使误注册也无下载资格。
# 函数用途: 拒绝控制、凭据、模型私有材料和注册账自身。
def _protected(path, context, scope) -> bool:
    home = context.owner_home
    if any(check(path, home.root) is not None for check in
           (host_managed_store_for_path, host_config_root_for_path, host_credential_for_path, host_state_for_path)):
        return True
    folded = tuple(part.casefold() for part in path.relative_to(home.home_dir).parts)
    private_dirs = {"data", "config", "secrets", ".ssh", ".git", "logs", "compact", "runtime", "memory"}
    private_names = {"permissions.json", "quota.json", "owner_status.json", "tool_policy.json", "memory_policy.json",
                     "soul.md", "user.md", "agents.md", "credentials.json", "token.json", "task.json", "state.json"}
    return (bool(private_dirs.intersection(folded)) or path.name.casefold() in private_names
            or path.name.casefold().startswith(".env") or path.suffix.casefold() in {".pem", ".key"}
            or path == scope.workspace_root / REGISTRY_RELATIVE_PATH)


# LLM: 业务读根与owner根双重约束；路径可能位于task工作区之外的owner业务目录，但必须是宿主明确批准根。
# 函数用途: 从注册项得到唯一允许读取的文件路径。
def _artifact_path(record, scope, context):
    path = _owner_path(record.path, context.owner_home)
    if (not any(path.is_relative_to(root) for root in scope.read_roots) or _protected(path, context, scope)
            or record.status != "ready" or record.metadata.get("artifact_role") == "tool_output_archive"):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return path


# LLM: nofollow逐段打开并检查regular/nlink；仅元数据读取可把真正缺失转不可读，安全拒绝仍抛404。
# 函数用途: 打开经过业务范围校验的制品，调用方负责关闭FD。
def _open_artifact(path, context, *, missing_ok=False):
    try:
        return open_readonly_file_beneath(context.owner_home.root, path.relative_to(context.owner_home.root).parts)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise HostAPIError("RESOURCE_NOT_FOUND", 404) from None
    except (OSError, ValueError):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404) from None


# LLM: 安全拒绝不降成元数据可见；readable只说明路径/类型/大小前提，不代表已实算hash。
# 函数用途: 安全投影闭合制品元数据，不输出内部路径和任意metadata。
def _artifact_view(record, scope, context):
    readable = False
    descriptor = _open_artifact(_artifact_path(record, scope, context), context, missing_ok=True)
    if descriptor is not None:
        try:
            readable = os.fstat(descriptor).st_size == record.size_bytes and record.size_bytes <= ARTIFACT_HASH_MAX_BYTES
        finally:
            os.close(descriptor)
    try:
        stamp = datetime.fromtimestamp(record.updated_at, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    return {"artifact_ref": record.artifact_id, "task_id": record.task_id, "name": Path(record.path).name[:256],
            "media_type": record.mime_type[:128] or "application/octet-stream", "byte_size": record.size_bytes,
            "sha256": record.sha256, "created_at": stamp, "readable": readable}


# LLM: inode/链接数/大小/mtime/ctime都参与读前读后fence，避免hash后改文件或换目录仍回成功。
# 函数用途: 保存一个打开文件的稳定性指纹。
def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 返回片段与hash来自同一次FD顺序读取；不在hash后按路径重新读正文，且实际读取受硬预算约束。
# 函数用途: 计算完整制品摘要，同时截取被请求的有限字节区间。
def _hash_and_slice(descriptor, payload):
    digest, total, fragments = hashlib.sha256(), 0, []
    start, stop = payload["offset_bytes"], payload["offset_bytes"] + payload["max_bytes"]
    while True:
        chunk = os.read(descriptor, ARTIFACT_IO_CHUNK_BYTES)
        if not chunk:
            break
        end = total + len(chunk)
        if end > ARTIFACT_HASH_MAX_BYTES:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        digest.update(chunk)
        left, right = max(start, total), min(stop, end)
        if left < right:
            fragments.append(chunk[left - total:right - total])
        total = end
    return digest.hexdigest(), total, b"".join(fragments)


# LLM: 内容、大小和expected哈希都必须相同；回包前再FD打开复核当前路径仍指向同inode，竞态不降级成功。
# 函数用途: 从注册制品读出一片经完整校验的base64内容。
def _read_artifact(record, scope, context, payload):
    path = _artifact_path(record, scope, context)
    if payload["expected_sha256"] != record.sha256:
        raise HostAPIError("ARTIFACT_CHANGED", 409)
    if record.size_bytes > ARTIFACT_HASH_MAX_BYTES:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    descriptor = _open_artifact(path, context)
    try:
        before = os.fstat(descriptor)
        if before.st_size != record.size_bytes:
            raise HostAPIError("ARTIFACT_CHANGED", 409)
        if payload["offset_bytes"] > before.st_size:
            raise HostAPIError("INVALID_SCHEMA", 400)
        digest, size, chunk = _hash_and_slice(descriptor, payload)
        if (_fingerprint(before) != _fingerprint(os.fstat(descriptor)) or digest != record.sha256
                or size != record.size_bytes):
            raise HostAPIError("ARTIFACT_CHANGED", 409)
        current = _open_artifact(path, context)
        try:
            if _fingerprint(before) != _fingerprint(os.fstat(current)):
                raise HostAPIError("ARTIFACT_CHANGED", 409)
        finally:
            os.close(current)
    except OSError:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    finally:
        os.close(descriptor)
    end = payload["offset_bytes"] + len(chunk)
    return {"artifact_ref": record.artifact_id, "sha256": digest, "offset_bytes": payload["offset_bytes"],
            "next_offset_bytes": end, "total_bytes": size, "data_base64": base64.b64encode(chunk).decode("ascii"),
            "eof": end == size}


# LLM: 领域port不拥有任务真相。task_scopes(authorized,context)必须由组合根读原TaskLink/thread/grant后返回
# ArtifactTaskScope；缺接缝时HOST_NOT_READY，不扫描目录、不借main根回退。A0 .call签名保持不变。
# 类用途: 实现注册制品的list/get/read三个同步只读动作。
class ArtifactReadService:
    # LLM: 仅保留宿主接缝和内存游标签名器，不初始化任何目录或Agent。
    # 函数用途: 绑定任务权威的只读范围解析器。
    def __init__(self, task_scopes=None):
        self._task_scopes = task_scopes
        self._cursors = _ObservationCursors()

    # LLM: 请求只接ref/task ID，路径仅由当前宿主范围和原注册账取得；调用结束仍须A末核。
    # 函数用途: 取得当前授权任务集合并分发制品动作。
    def call(self, authorized: AuthorizedCall) -> DomainResult:
        payload = _payload(authorized, {"workspace.artifacts.list", "workspace.artifacts.get", "workspace.artifacts.read"})
        context = _read_context(authorized)
        if self._task_scopes is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        scopes = _allowed_scopes(authorized, context, self._task_scopes(authorized, context))
        if authorized.call.action == "workspace.artifacts.list":
            data, errors = self._list(authorized, context, scopes, payload)
        else:
            scope, record = self._lookup(context, scopes, payload["artifact_ref"])
            data = (_read_artifact(record, scope, context, payload)
                    if authorized.call.action == "workspace.artifacts.read" else _artifact_view(record, scope, context))
            errors = []
        return _result(authorized, context, data, errors)

    # LLM: 页绑定原账与可见投影指纹；安全拒绝项不泄露元数据，损坏账不报告完整。
    # 函数用途: 列出一个已授权任务的注册制品。
    def _list(self, authorized, context, scopes, payload):
        matches = [scope for scope in scopes if scope.task_id == payload["task_id"]]
        if len(matches) != 1:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        scope = matches[0]
        records, snapshot, errors = _registry(context, scope)
        rows = []
        for _, record in sorted(records.items()):
            try:
                rows.append(_artifact_view(record, scope, context))
            except HostAPIError as error:
                if error.code != "RESOURCE_NOT_FOUND":
                    raise
        snapshot = hashlib.sha256(snapshot.encode() + canonical_json(rows).encode()).hexdigest()
        state = self._cursors.binding(authorized, payload), snapshot
        return _page(rows, payload, state, self._cursors, errors), errors

    # LLM: 只在授权task账中匹配精确ref，不使用原lookup的path回退；重复ref或坏账不挑一条猜成功。
    # 函数用途: 从任务注册账找出唯一可读取的制品记录。
    def _lookup(self, context, scopes, ref):
        matches = []
        for scope in scopes:
            records, _, errors = _registry(context, scope)
            if errors:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            if ref in records:
                matches.append((scope, records[ref]))
        if len(matches) != 1:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        return matches[0]
