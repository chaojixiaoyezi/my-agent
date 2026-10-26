# LLM: 索引缓存只复用同FD元数据下的解析/字面索引，不缓存授权；匹配路径、scope与正文仍逐次严格验证。
# 模块用途: 缓存索引及已验证正文计算，避免逻辑引用触发历史路径解析风暴。
from __future__ import annotations

"""explicit refs-only-to-body reader for externalized tool output artifacts."""

import codecs
import hashlib
import json
import os
import stat
import threading
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from errno import ELOOP
from pathlib import Path
from typing import Any

from ...artifacts.registry_projection_cache import _retained_tree_bytes
from ...common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath
from ...common.tool_index_stream import iter_decoded_tool_index_objects
from ...common.tool_output_paths import (
    tool_output_index_paths_for_lookup,
)
from ..tool_output_externalizer import _BLOB_BLOCK_SIZE
from .read_modes import (
    ArtifactContentReadRequest,
    ArtifactContentReadResult,
    _normalize_read_mode,
    _read_chars_limit,
    read_artifact_content_by_mode,
)


# LLM: Cache only verified content/index objects; locks guard LRU mutations, never filesystem IO, hashing or parsing.
# 类用途: 为内联正文和blob字符索引提供双上限的线程安全进程内缓存。
class _VerifiedArtifactCache:
    # LLM: Each cache has at most 32 keys; oversized values are returned by callers but never retained.
    # 函数用途: 创建独立的条目数与驻留字节预算。
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.entries: OrderedDict = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    # LLM: A hit is a computation reuse hint only, not evidence that current file bytes or scope are valid.
    # 函数用途: 取出并提升最近访问条目，不执行读取或授权。
    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None:
                self.entries.move_to_end(key)
                return entry[0]
        return None

    # LLM: Measure trees outside the lock and replace/evict atomically; shared objects count once per entry.
    # 函数用途: 超预算不留存，按字节及32键上限淘汰旧结果。
    def put(self, key, value):
        cost = _retained_tree_bytes((key, value), set())
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.bytes -= old[1]
            if cost > self.max_bytes:
                return
            while self.entries and (len(self.entries) >= 32 or self.bytes + cost > self.max_bytes):
                _, (_, removed) = self.entries.popitem(last=False)
                self.bytes -= removed
            self.entries[key] = value, cost
            self.bytes += cost


_INLINE_ARTIFACT_CACHE = _VerifiedArtifactCache(16 * 1024 * 1024)
_BLOB_INDEX_CACHE = _VerifiedArtifactCache(8 * 1024 * 1024)
_ARTIFACT_INDEX_CACHE = _VerifiedArtifactCache(16 * 1024 * 1024)
_INDEX_LITERAL_FIELDS = ("path", "sha256", "scoped_call_id", "call_id", "artifact_id", "file_path", "archive_path", "source_ref", "uri")
# 外置blob流式解码字符偏移检查点的间隔字符数
_BLOB_CHECKPOINT_CHARS = 4096


# LLM: Index contains only verified dimensions, block hashes and UTF-8 character boundaries, never a full body string.
# 类用途: 保存可复用的blob字符到字节位置及块校验索引。
@dataclass(frozen=True)
class _BlobIndex:
    total_bytes: int
    total_chars: int
    is_ascii: bool
    char_checkpoints: tuple[tuple[int, int], ...]
    block_hashes: tuple[str, ...]


# LLM: Distinguish failed integrity verification from malformed metadata and unreadable paths/UTF-8.
# 类用途: 在内部传播正文完整性失败，统一转成artifact_hash_mismatch。
class _ArtifactHashMismatch(ValueError):
    pass


# LLM: 仅宿主构造读取身份；offset/模式是内容窗口，不授予跨run权限。
# 类用途: 描述一次显式归档读取，不接受模型提供的prepared对象。
@dataclass(frozen=True)
class ReadToolOutputArtifactRequest:
    root: str | Path
    artifact_ref: str
    offset: int = 0
    max_chars: int = -1
    mode: str = "slice"
    query: str = ""
    run_id: str = ""
    task_id: str = ""
    request_id: str = ""
    scope_mode: str = ""


# LLM: 计划只保存请求局部发现与记录浅副本；不是目录快照或授权凭证，read仍复核scope和安全打开。
# 类用途: 让预算估算和正文读取共用同一次查找结果。
@dataclass(frozen=True)
class PreparedToolOutputArtifactRead:
    request: ReadToolOutputArtifactRequest
    root: Path
    artifact_ref: str
    index_paths: tuple[Path, ...]
    record: Mapping[str, Any] | None
    read_error: str = ""


# LLM: 正文打开必须使用认证root而非候选parent；path只是已解析的读取目标。
# 类用途: 将安全打开所需的认证根、目标和注册元数据交给正文读取器。
@dataclass(frozen=True)
class _RegisteredArtifactRead:
    root: Path
    path: Path
    record: dict[str, Any]
    request: ReadToolOutputArtifactRequest


# LLM: 缺失根目录不等于不受信，仅在根本身为符号链接或平台缺少严格dir_fd能力时返回明确错误描述。
# 函数用途: 校验宿主归档根本身是否满足严格no-follow打开前提。
def _verify_trusted_artifact_root(trusted_root: Path) -> str:
    try:
        descriptor = open_directory_beneath(trusted_root, (), require_dir_fd=True)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    else:
        os.close(descriptor)
        return ""


# LLM: 主根完整scoped引用先查；短引用、物理路径及未匹配仍发现全部索引，保留scope、同名歧义和读取故障。
# 函数用途: 为预算和读取生成同一次计划，主根精确命中时不展开历史目录。
def prepare_tool_output_artifact_read(request: ReadToolOutputArtifactRequest) -> PreparedToolOutputArtifactRead:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return PreparedToolOutputArtifactRead(request, root, "", (), None)
    primary = root / "blobs" / "tool_outputs" / "index.jsonl"
    paths = (primary,) if _primary_index_has_scoped_ref(root, artifact_ref, request) else tuple(tool_output_index_paths_for_lookup(root))
    open_errors: list[str] = []
    try:
        record = _find_index_record(root, artifact_ref, request, index_paths=paths, open_errors=open_errors)
    except TypeError:
        record = _find_index_record(root, artifact_ref, request, index_paths=paths)
    read_error = ""
    if record is None:
        read_error = open_errors[0] if open_errors else _verify_trusted_artifact_root(Path(request.root).expanduser())
    return PreparedToolOutputArtifactRead(
        request,
        root,
        artifact_ref,
        paths,
        dict(record) if record is not None else None,
        read_error=read_error,
    )


# LLM: 只允许同root/ref及逐字段一致的宿主scope复用；内容窗口可变，不把旧scope判断当授权缓存。
# 函数用途: 拒绝错配的准备对象，并按当前请求重新准备。
def _prepared_for_request(request: ReadToolOutputArtifactRequest, prepared: PreparedToolOutputArtifactRead | None) -> PreparedToolOutputArtifactRead:
    if isinstance(prepared, PreparedToolOutputArtifactRead):
        root = Path(request.root).expanduser().resolve(strict=False)
        if (prepared.root == root and prepared.artifact_ref == str(request.artifact_ref or "").strip()
                and all(getattr(prepared.request, key) == getattr(request, key)
                        for key in ("run_id", "task_id", "request_id", "scope_mode"))):
            return prepared
    return prepare_tool_output_artifact_read(request)


# LLM: prepared只复用索引事实；每次读仍核scope、认证root与允许目录，并重新打开/计算全文hash。
# 函数用途: 读取已注册工具输出，保持原错误和模型外宿主事实合同。
def read_tool_output_artifact(request: ReadToolOutputArtifactRequest, *, prepared: PreparedToolOutputArtifactRead | None = None) -> dict[str, Any]:
    plan = _prepared_for_request(request, prepared)
    root, artifact_ref, record = plan.root, plan.artifact_ref, plan.record
    if not artifact_ref:
        return _error_payload("missing_artifact_ref", artifact_ref, "artifact_ref is required")
    if record is None:
        if plan.read_error:
            return _error_payload("artifact_unreadable", artifact_ref, plan.read_error)
        return _error_payload("artifact_not_registered", artifact_ref, "artifact ref was not found in tool output index")
    if not _record_allowed_for_read_scope(record, request):
        return _error_payload(
            "tool_permission_denied",
            artifact_ref,
            "artifact ref belongs to another run and is outside the current read scope",
        )
    registered_path = str(record.get("path", "") or "").strip()
    if not registered_path:
        # 记录命中(scoped_call_id 在 index 里)但 path 为空 —— 该工具输出从未外置成可读 blob
        # (compaction 期间 output_externalized=false / CONTEXT_COMPACT_DEFERRED,或低于外置阈值)。
        # 旧逻辑让空 path 落成 Path("")→cwd→误报 artifact_path_outside_tool_outputs,模型照
        # reducer policy"use scoped_call_id for read_artifact"反复重试同一个读不到的 ref。改为返回
        # 明确语义 + 原始来源,让模型直接 read_file 源(真机 stage4:每 compaction 周期省 ~3 轮瞎试)。
        source = _record_source_hint(record)
        message = (
            "该工具输出未外置成可读 artifact(output_externalized=false);"
            + (f"直接 read_file 原始来源:{source}" if source else "请改用直接读取(read_file/重跑工具)推进,勿重试该 ref")
        )
        return _error_payload("artifact_not_externalized", artifact_ref, message)
    path = Path(registered_path).expanduser().resolve(strict=False)
    allowed_roots = tuple(path.parent for path in plan.index_paths if _is_under_allowed_root(path, root))
    if not any(_is_under_allowed_root(path, allowed_root) for allowed_root in allowed_roots):
        return _error_payload("artifact_path_outside_tool_outputs", artifact_ref, "registered path is outside tool_outputs")
    return _read_registered_artifact(_RegisteredArtifactRead(root=root, path=path, record=record, request=request))


# LLM: 元数据字节数只用于预算预留，不是实际字符费用或权限证据；与read共用同身份计划。
# 函数用途: 不打开正文地估计归档大小，缺失或非法元数据返回None。
def estimate_tool_output_artifact_size(request: ReadToolOutputArtifactRequest, *, prepared: PreparedToolOutputArtifactRead | None = None) -> int | None:
    record = _prepared_for_request(request, prepared).record
    if record is None:
        return None
    try:
        return max(0, int(record.get("size_bytes") or 0))
    except (TypeError, ValueError):
        return None


# LLM: Every call safely opens and hashes raw wrapper bytes; a verified inline hit skips JSON/encoding, never scope or FD reads.
# 函数用途: 安全读取包装并按实际字节复用内联缓存，或转入blob验证读取链。
def _read_registered_artifact(read: _RegisteredArtifactRead) -> dict[str, Any]:
    path, artifact_ref = read.path, read.request.artifact_ref
    try:
        descriptor = open_readonly_file_beneath(read.request.root, path.relative_to(read.root).parts)
        with os.fdopen(descriptor, "rb") as handle:
            raw_bytes = handle.read()
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        key = str(read.root), str(path), raw_digest, str(read.record.get("sha256") or "")
        cached = _INLINE_ARTIFACT_CACHE.get(key)
        if cached is not None:
            return _inline_read_result(read, *cached)
        payload = json.loads(raw_bytes)
    except FileNotFoundError:
        return _error_payload("artifact_missing", artifact_ref, "registered artifact file does not exist")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _error_payload("artifact_unreadable", artifact_ref, f"{type(exc).__name__}: {exc}")
    if not isinstance(payload, dict) or payload.get("kind") != "tool_output":
        return _error_payload("invalid_tool_output_artifact", artifact_ref, "artifact is not a tool_output content file")
    # 声明为 blob_v1 的包装文件必须以侧文件为唯一正文来源，拒绝注入内联 content 覆盖侧文件校验。
    if payload.get("artifact_format") == "blob_v1":
        if "content" in payload:
            return _error_payload("invalid_tool_output_artifact", artifact_ref, "artifact is not a tool_output content file")
        return _read_blob_artifact(read, payload)
    content = payload.get("content")
    if not isinstance(content, str):
        return _error_payload("invalid_tool_output_artifact", artifact_ref, "artifact is not a tool_output content file")
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    expected = str(payload.get("sha256") or read.record.get("sha256") or "")
    if expected and digest != expected:
        return _error_payload("artifact_hash_mismatch", artifact_ref, "artifact content hash does not match metadata")
    meta = {key: value for key, value in payload.items() if key != "content"}
    verified = meta, content, digest, len(encoded)
    if len(encoded) <= 4 * 1024 * 1024:
        _INLINE_ARTIFACT_CACHE.put(key, verified)
    return _inline_read_result(read, *verified)


# LLM: Reuse only a body verified against the current raw-wrapper digest and registry SHA; size is passed, never encoded twice.
# 函数用途: 从已验证内联正文按原模式生成结果。
def _inline_read_result(read, payload, content, digest, size_bytes):
    request = read.request
    read_result = read_artifact_content_by_mode(
        ArtifactContentReadRequest(
            content=content,
            mode=request.mode,
            offset=request.offset,
            max_chars=request.max_chars,
            query=request.query,
        )
    )
    return _verified_read_result(read, payload, digest, size_bytes, read_result)


# LLM: Both formats expose the same result/error contract and verified byte size independently from the sliced text.
# 函数用途: 将内联或blob窗口统一组装为原宿主读取协议。
def _verified_read_result(read, payload, digest, size_bytes, read_result):
    if not read_result.ok:
        return _error_payload(read_result.error_code, read.request.artifact_ref, read_result.message)
    base = _success_base_payload(read, payload, read_result.content, digest, size_bytes=size_bytes)
    base.update(_success_content_payload(read_result))
    base.update(read_result.metadata or {})
    return base


# LLM: Sidecar names are fixed siblings, never user-selected paths; optional block manifests still require full SHA verification.
# 函数用途: 校验blob包装元数据，拒绝穿越和无法解释的分块格式。
def _blob_metadata(read, payload):
    name = str(payload.get("content_blob") or "")
    if name != f"{read.path.stem}.blob" or any(mark in name for mark in ("/", "\\", "\x00")) or name in {".", ".."}:
        raise ValueError("artifact blob must be the canonical sibling basename")
    if type(payload.get("blob_block_size")) is not int or payload["blob_block_size"] != _BLOB_BLOCK_SIZE:
        raise ValueError("invalid artifact blob block size")
    hashes = payload.get("blob_block_hashes", [])
    if not isinstance(hashes, list) or any(not isinstance(value, str) for value in hashes):
        raise ValueError("invalid artifact blob block hashes")
    if "blob_block_hashes" in payload and not hashes and payload.get("size_bytes") != 0:
        raise _ArtifactHashMismatch("nonempty artifact blob has an empty block manifest")
    expected = str(payload.get("sha256") or read.record.get("sha256") or "")
    if not expected:
        raise ValueError("artifact blob requires a SHA256 digest")
    return read.path.with_name(name), expected, tuple(hashes)


# LLM: FD identity/stat is an index hint and a race barrier, never proof of bytes; warm reads also hash the entire opened FD.
# 函数用途: 提取blob索引缓存键和读中变化检查需要的稳定字段。
def _blob_stat_signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: Strict open uses the original authenticated root; validation, hashing and window reads share one FD, not a reopened path.
# 函数用途: 安全读取侧文件，统一处理缺失、损坏、非法UTF-8和元数据错误。
def _read_blob_artifact(read, payload):
    artifact_ref = read.request.artifact_ref
    try:
        path, expected, hashes = _blob_metadata(read, payload)
        descriptor = open_readonly_file_beneath(read.request.root, path.relative_to(read.root).parts)
        with os.fdopen(descriptor, "rb") as handle:
            before = _blob_stat_signature(os.fstat(handle.fileno()))
            key = (str(read.root), str(path), *before, expected, hashes)
            logical_key = str(read.root), str(path), expected
            index = _BLOB_INDEX_CACHE.get(key)
            cold = index is None
            if cold:
                index = _build_blob_index(handle, expected, hashes)
            else:
                if _stream_blob_digest(handle) != expected:
                    raise _ArtifactHashMismatch("artifact blob content hash does not match metadata")
                index = _BLOB_INDEX_CACHE.get(logical_key) or index
            if (index.total_bytes != before[2] or payload.get("size_bytes") != index.total_bytes
                    or payload.get("total_chars") != index.total_chars
                    or _blob_stat_signature(os.fstat(handle.fileno())) != before):
                raise _ArtifactHashMismatch("artifact blob dimensions or opened file changed")
            result = _blob_content_result(handle, index, read.request, expected)
            if _blob_stat_signature(os.fstat(handle.fileno())) != before:
                raise _ArtifactHashMismatch("artifact blob changed during read")
            if cold:
                _BLOB_INDEX_CACHE.put(key, index)
                _BLOB_INDEX_CACHE.put(logical_key, index)
        return _verified_read_result(read, payload, expected, index.total_bytes, result)
    except FileNotFoundError:
        return _error_payload("artifact_missing", artifact_ref, "registered artifact blob file does not exist")
    except _ArtifactHashMismatch as exc:
        return _error_payload("artifact_hash_mismatch", artifact_ref, str(exc))
    except (OSError, UnicodeDecodeError) as exc:
        return _error_payload("artifact_unreadable", artifact_ref, f"{type(exc).__name__}: {exc}")
    except ValueError as exc:
        return _error_payload("invalid_tool_output_artifact", artifact_ref, str(exc))


# LLM: Use OpenSSL's bounded file digest where available; Python 3.10 keeps a 256KiB fallback, never stat-only validation.
# 函数用途: 热读先对同一FD完整哈希，非返回窗口的篡改也必须失败。
def _stream_blob_digest(handle):
    handle.seek(0)
    if hasattr(hashlib, "file_digest"):
        return hashlib.file_digest(handle, "sha256").hexdigest()
    digest = hashlib.sha256()
    while block := handle.read(256 * 1024):
        digest.update(block)
    return digest.hexdigest()


# LLM: Incremental strict UTF-8 retains at most one 64KiB chunk; checkpoints point to real character starts, including split emoji.
# 函数用途: 冷读逐块验证全文和块哈希，建立每4096字符一个字节位置的索引。
def _build_blob_index(handle, expected, manifest):
    handle.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    digest, hashes, checkpoints = hashlib.sha256(), [], [(0, 0)]
    total_bytes, total_chars, is_ascii = 0, 0, True
    while block := handle.read(_BLOB_BLOCK_SIZE):
        digest.update(block)
        block_hash = hashlib.sha256(block).hexdigest()
        if manifest and (len(hashes) >= len(manifest) or block_hash != manifest[len(hashes)]):
            raise _ArtifactHashMismatch("artifact blob block hash does not match metadata")
        hashes.append(block_hash)
        byte_start = total_bytes - len(decoder.getstate()[0])
        text = decoder.decode(block)
        _append_char_checkpoints(checkpoints, text, total_chars, byte_start)
        total_bytes += len(block)
        total_chars += len(text)
        is_ascii = is_ascii and block.isascii()
    decoder.decode(b"", final=True)
    if digest.hexdigest() != expected or (manifest and len(hashes) != len(manifest)):
        raise _ArtifactHashMismatch("artifact blob content hash does not match metadata")
    if checkpoints[-1] != (total_chars, total_bytes):
        checkpoints.append((total_chars, total_bytes))
    return _BlobIndex(total_bytes, total_chars, is_ascii, tuple(checkpoints), tuple(hashes))


# LLM: Byte starts account for decoder-carried bytes; only disjoint segments are encoded, never the whole decoded blob.
# 函数用途: 将当前小块中的4096字符边界转换成精确UTF-8字节位置。
def _append_char_checkpoints(checkpoints, text, char_start, byte_start):
    target = checkpoints[-1][0] + _BLOB_CHECKPOINT_CHARS
    cursor = 0
    while target <= char_start + len(text):
        position = target - char_start
        byte_start += len(text[cursor:position].encode("utf-8"))
        checkpoints.append((target, byte_start))
        cursor = position
        target += _BLOB_CHECKPOINT_CHARS


# LLM: Search/unbounded reads verify the actual returned bytes again before decoding; bounded modes recheck target blocks.
# 函数用途: 保持所有原模式和分页字段，对有界窗口避免生成完整多MB字符串。
def _blob_content_result(handle, index, request, expected):
    mode, limit = _normalize_read_mode(request.mode), _read_chars_limit(request.max_chars)
    if mode == "search" or limit == 0:
        handle.seek(0)
        raw = handle.read()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise _ArtifactHashMismatch("artifact blob changed before full decode")
        content = raw.decode("utf-8")
        return read_artifact_content_by_mode(ArtifactContentReadRequest(
            content, mode, request.offset, limit, request.query,
        ))
    if mode not in {"slice", "head", "tail"}:
        result = read_artifact_content_by_mode(ArtifactContentReadRequest("", mode=mode))
        return replace(result, total_chars=index.total_chars)
    start = max(0, int(request.offset or 0)) if mode == "slice" else 0
    if mode == "tail":
        start = max(0, index.total_chars - limit)
    end = min(index.total_chars, start + limit)
    content = _blob_window(handle, index, start, end)
    return ArtifactContentReadResult(
        ok=True, mode=mode, content=content, offset=start, max_chars=limit,
        total_chars=index.total_chars, has_more_before=start > 0,
        has_more_after=end < index.total_chars,
        truncated=mode != "tail" and end < index.total_chars,
    )


# LLM: Recheck returned blocks against cold-verified hashes on the same FD; decode only complete-character checkpoint boundaries.
# 函数用途: 按ASCII直达或UTF-8检查点定位窗口，只读目标附近的完整块。
def _blob_window(handle, index, start, end):
    if start >= index.total_chars or end <= start:
        return ""
    if index.is_ascii:
        base_char, byte_start, byte_end = start, start, end
    else:
        points = index.char_checkpoints
        base_char, byte_start = points[bisect_right(points, (start, index.total_bytes + 1)) - 1]
        _, byte_end = points[bisect_left(points, (end, -1))]
    first, last = byte_start // _BLOB_BLOCK_SIZE, (byte_end - 1) // _BLOB_BLOCK_SIZE
    handle.seek(first * _BLOB_BLOCK_SIZE)
    blocks = []
    for number in range(first, last + 1):
        block = handle.read(_BLOB_BLOCK_SIZE)
        if hashlib.sha256(block).hexdigest() != index.block_hashes[number]:
            raise _ArtifactHashMismatch("artifact blob window changed after verification")
        blocks.append(block)
    raw = b"".join(blocks)
    byte_base = first * _BLOB_BLOCK_SIZE
    text = raw[byte_start - byte_base:byte_end - byte_base].decode("utf-8")
    return text[start - base_char:end - base_char]


# LLM: The reader may retain host-only path/scope facts for CLI and audit, but it must also expose a
# stable logical ref; supplied verified size avoids encoding a full body for accounting again.
# 函数用途: 组装读取成功的完整宿主事实，并附上后续模型可安全复用的逻辑引用。
def _success_base_payload(
    read: _RegisteredArtifactRead,
    payload: dict[str, Any],
    content: str,
    digest: str,
    *,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    record = read.record
    artifact_ref = read.request.artifact_ref
    return {
        "ok": True,
        "artifact_ref": artifact_ref,
        "canonical_artifact_ref": str(
            record.get("scoped_call_id")
            or record.get("call_id")
            or artifact_ref
        ),
        "artifact_path": str(read.path),
        "kind": "tool_output",
        "tool": str(payload.get("tool") or record.get("tool") or ""),
        "call_id": str(payload.get("call_id") or record.get("call_id") or ""),
        "request_id": str(payload.get("request_id") or record.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or record.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or record.get("task_id") or ""),
        "sha256": digest,
        "size_bytes": size_bytes if size_bytes is not None else len(content.encode("utf-8")),
        "content_hash_verified": True,
        "reads_artifact_body": True,
    }


# LLM: Model-facing callers need explicit window direction. Tail reads reach the real end even when
# they omit a prefix, while slice/head expose next_offset only when more content follows.
# 函数用途: 把正文读取结果转换成不含糊的窗口与续读信息。
def _success_content_payload(read_result: ArtifactContentReadResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "read_mode": read_result.mode,
        "content_offset": read_result.offset,
        "content_max_chars": read_result.max_chars,
        "content_chars": len(read_result.content),
        "total_chars": read_result.total_chars,
        "has_more_before": read_result.has_more_before,
        "has_more_after": read_result.has_more_after,
        "truncated": read_result.truncated,
        "content": read_result.content,
    }
    if read_result.mode != "search":
        payload["window_start"] = read_result.offset
        payload["window_end"] = read_result.offset + len(read_result.content)
        if read_result.has_more_after:
            payload["next_offset"] = payload["window_end"]
    return payload

# LLM: 安全展开波浪号前缀；非~路径保持原Path.expanduser行为，~unknownuser回退原Path对象而不抛RuntimeError中断逻辑引用查询。
# 函数用途: 兼容含~的模型引用输入，避免~unknownuser触发异常且不吞非~路径的异常合同。
def _safe_expanduser(path: Path) -> Path:
    if not str(path).startswith("~"):
        return path.expanduser()
    try:
        return path.expanduser()
    except RuntimeError:
        return path


# LLM: 全部索引扫描完成后才处理候选路径错误；非绝对引用先匹配原样逻辑身份，仅无命中时才按物理wrapper路径/唯一文件名回退.json。
# 函数用途: 复用查找结果，保留含斜杠的合法.blob逻辑call_id、冒号目录相对sidecar、原scope和唯一basename修复，并去重open_errors。
def _find_index_record(
    root: Path,
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
    *,
    index_paths: tuple[Path, ...] | None = None,
    open_errors: list[str] | None = None,
) -> dict[str, Any] | None:
    if artifact_ref.endswith(".blob") and _safe_expanduser(Path(artifact_ref)).is_absolute():
        return _lookup_index_record(
            root,
            f"{artifact_ref[:-5]}.json",
            request,
            index_paths=index_paths,
            open_errors=open_errors,
            physical_only=True,
        )
    matched = _lookup_index_record(
        root, artifact_ref, request, index_paths=index_paths, open_errors=open_errors
    )
    if matched is not None or not artifact_ref.endswith(".blob"):
        return matched
    fallback_errors: list[str] | None = [] if open_errors is not None else None
    matched = _lookup_index_record(
        root,
        f"{artifact_ref[:-5]}.json",
        request,
        index_paths=index_paths,
        open_errors=fallback_errors,
        physical_only=True,
    )
    if open_errors is not None and fallback_errors:
        open_errors.extend(err for err in fallback_errors if err not in open_errors)
    return matched


# LLM: 缓存记录离开查找层前深复制，不能让调用方改写下一次scope事实；物理别名仍按原优先级扫描。
# 函数用途: 按既定作用域优先级定位记录并隔离缓存对象。
def _lookup_index_record(
    root: Path,
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
    *,
    index_paths: tuple[Path, ...] | None = None,
    open_errors: list[str] | None = None,
    physical_only: bool = False,
) -> dict[str, Any] | None:
    try:
        ref_path = _safe_expanduser(Path(artifact_ref))
        resolved_ref = ref_path.resolve(strict=False) if ref_path.is_absolute() or _looks_like_path(artifact_ref) else None
    except Exception:
        # 原先先读取索引再求引用路径；极端引用路径错误不能遮蔽索引的读取/解码错误。
        _index_records_for_root(root, index_paths=index_paths, trusted_root=request.root)
        raise
    query = _ArtifactIndexQuery(
        artifact_ref,
        resolved_ref,
        open_errors=open_errors if open_errors is not None else [],
        physical_only=physical_only,
    )
    records = _index_records_for_root(root, query, index_paths=index_paths, trusted_root=request.root)
    for candidate in records:
        if candidate.error is not None:
            raise candidate.error
    matches = [item.record for item in records if item.exact_match]
    scoped = _scoped_matches(matches, artifact_ref, request, physical_only=physical_only)
    if scoped:
        return deepcopy(scoped[-1])
    if matches and _request_has_scope(request):
        if resolved_ref is not None and not _request_has_strong_scope(request):
            return deepcopy(matches[-1])
        return None
    if matches:
        return deepcopy(matches[-1])
    if resolved_ref is not None:
        basename = [item.record for item in records if item.basename_match]
        return deepcopy(basename[0]) if ref_path.name.endswith(".json") and len(basename) == 1 else None
    return None


# LLM: 字面匹配先于IO；无物理目标时绝不解析无关路径，命中后的验证由候选层负责。
# 函数用途: 先用纯内存字段判断逻辑引用，再按需比较物理路径。
def _record_matches_ref(
    record: dict[str, Any],
    artifact_ref: str,
    resolved_ref: Path | None,
    *,
    path_cache: _ArtifactPathCache | None = None,
    physical_only: bool = False,
) -> bool:
    literal_refs = (
        {str(record.get("path", "") or "")}
        if physical_only
        else {str(record.get(key, "") or "") for key in _INDEX_LITERAL_FIELDS}
    )
    if artifact_ref in literal_refs:
        return True
    if resolved_ref is None:
        return False
    record_path = _resolve_index_record_path(str(record.get("path", "") or ""), cache=path_cache)
    comparable_ref = str(resolved_ref) if isinstance(record_path, str) else resolved_ref
    return record_path == comparable_ref


# LLM: cache仅活在本次query；空路径精确求值一次，规范POSIX父目录只缓存解析线索，所有异常路径仍回原实现。
# 函数用途: 去重高频路径解析，不缓存授权和hash结果。
def _resolve_index_record_path(path: str, *, cache: _ArtifactPathCache | None = None) -> str | Path:
    if path == "" and cache is not None:
        if cache.empty_path is None:
            cache.empty_path = Path("").expanduser().resolve(strict=False)
        return cache.empty_path
    canonical_posix = os.name == "posix" and path.startswith("/") and not path.startswith("//") and os.path.normpath(path) == path
    if not canonical_posix:
        return Path(path).expanduser().resolve(strict=False)
    if cache is not None and _clean_index_parent(os.path.dirname(path), cache):
        try:
            if not stat.S_ISLNK(os.lstat(path).st_mode):
                return path
        except FileNotFoundError:
            return path
        except OSError:
            pass
    return _resolved_posix_index_path(path)


# LLM: 干净父目录仅是query内性能线索；只缓存最多256个已证实的干净目录，防止海量缺失路径膨胀缓存。
# 函数用途: 每次查询对相同规范父目录只做一次真实路径和目录确认。
def _clean_index_parent(parent: str, cache: _ArtifactPathCache) -> bool:
    cached = cache.clean_parents.get(parent)
    if cached is not None:
        return cached
    try:
        clean = os.path.realpath(parent, strict=False) == parent and stat.S_ISDIR(os.stat(parent).st_mode)
    except OSError:
        clean = False
    if clean and len(cache.clean_parents) < 256:
        cache.clean_parents[parent] = True
    return clean


# LLM: 与Path.resolve(strict=False)相同：realpath后stat复核环链，普通缺失/权限错误不抛，ELOOP仍RuntimeError。
# 函数用途: 复用标准库真实路径解析而不为每个历史文件名创建Path对象。
def _resolved_posix_index_path(path: str) -> str:
    try:
        resolved = os.path.realpath(path, strict=False)
    except OSError as exc:
        _raise_index_path_loop(exc)
        raise
    try:
        os.stat(resolved)
    except OSError as exc:
        _raise_index_path_loop(exc)
    return resolved


# LLM: POSIX环链的错误类别和文字沿原Path.resolve；不将其它OSError误报为环链。
# 函数用途: 保留nonstrict解析遇到真实链接循环时的错误合同。
def _raise_index_path_loop(exc: OSError) -> None:
    if exc.errno == ELOOP:
        raise RuntimeError("Symlink loop from %r" % exc.filename)


# LLM: 优先返回与请求scope按run_id->request_id->task_id层级精确匹配的记录；引用显式含冒号时优先保留带真实scope且身份命中exact的scoped_call_id记录，防止裸call_id或同task兄弟run遮蔽当前run输出。
# 函数用途: 按请求scope与显式冒号引用优先级筛选索引匹配项。
def _scoped_matches(
    matches: list[dict[str, Any]],
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
    *,
    physical_only: bool = False,
) -> list[dict[str, Any]]:
    exact = _exact_scope_matches(matches, request)
    if not physical_only and ":" in artifact_ref:
        scoped_id_matches = [
            record
            for record in matches
            if _record_has_scope(record) and str(record.get("scoped_call_id", "") or "") == artifact_ref
        ]
        if scoped_id_matches:
            exact_scoped = [
                record for record in scoped_id_matches if any(item is record for item in exact)
            ]
            return exact_scoped or scoped_id_matches
        if exact:
            return exact
        if (
            any(str(record.get("scoped_call_id", "") or "") == artifact_ref for record in matches)
            or not _looks_like_path(artifact_ref)
        ):
            return matches
    if exact:
        return exact
    if _request_has_scope(request):
        return [] if any(_record_has_scope(record) for record in matches) else matches
    return [record for record in matches if _record_has_scope(record)]


# LLM: 按run_id、request_id、task_id由强到弱匹配请求scope；当request带run_id时优先命中同run_id记录。
# 函数用途: 为短引用与同task多run场景提供确定性的当前run优先匹配。
def _exact_scope_matches(
    matches: list[dict[str, Any]],
    request: ReadToolOutputArtifactRequest,
) -> list[dict[str, Any]]:
    req_run = str(request.run_id or "").strip()
    if req_run:
        run_matches = [record for record in matches if str(record.get("run_id") or "").strip() == req_run]
        if run_matches:
            return run_matches
    for key in ("request_id", "task_id"):
        req_val = str(getattr(request, key) or "").strip()
        if not req_val:
            continue
        key_matches = [
            record
            for record in matches
            if str(record.get(key) or "").strip() == req_val
            and (not req_run or not str(record.get("run_id") or "").strip())
        ]
        if key_matches:
            return key_matches
    return [record for record in matches if _record_scope_matches_request(record, request)]


def _request_has_scope(request: ReadToolOutputArtifactRequest) -> bool:
    return any(str(getattr(request, key) or "").strip() for key in ("run_id", "task_id", "request_id"))


def _request_has_strong_scope(request: ReadToolOutputArtifactRequest) -> bool:
    return any(str(getattr(request, key) or "").strip() for key in ("task_id", "request_id"))


def _record_scope_matches_request(
    record: dict[str, Any],
    request: ReadToolOutputArtifactRequest,
) -> bool:
    return any(
        str(getattr(request, key) or "").strip()
        and str(record.get(key) or "").strip() == str(getattr(request, key) or "").strip()
        for key in ("run_id", "task_id", "request_id")
    )


def _record_has_scope(record: dict[str, Any]) -> bool:
    return any(str(record.get(key) or "").strip() for key in ("run_id", "task_id", "request_id"))


# LLM: A current-run artifact scope accepts only records carrying the exact
# trusted run id injected by the gateway; missing scope fails closed.
# 函数用途: 防止同一任务下的来源工作者通过猜 ref 读取兄弟子代理工具输出。
def _record_allowed_for_read_scope(
    record: dict[str, Any],
    request: ReadToolOutputArtifactRequest,
) -> bool:
    if str(request.scope_mode or "").strip().lower() != "current_run":
        return True
    run_id = str(request.run_id or "").strip()
    return bool(
        run_id
        and str(record.get("run_id") or "").strip() == run_id
    )


def _unique_record_by_basename(records: list[dict[str, Any]], filename: str) -> dict[str, Any] | None:
    if not filename.endswith(".json"):
        return None
    matches = [
        record
        for record in records
        if Path(str(record.get("path", "") or "")).name == filename
    ]
    return matches[0] if len(matches) == 1 else None


# LLM: 生命周期严格限于本次query，目录解析线索不是已授权/内容未变的证明。
# 类用途: 缓存空路径结果与干净父目录，减少重复系统调用。
@dataclass
class _ArtifactPathCache:
    empty_path: Path | None = None
    clean_parents: dict[str, bool] = field(default_factory=dict)


# LLM: 每行仅计算一次精确/同名匹配；异常留到所有索引读完，保留原异常优先级。
# 类用途: 保存一个候选的匹配事实或延迟路径错误。
@dataclass(frozen=True)
class _ArtifactIndexCandidate:
    record: dict[str, Any]
    exact_match: bool
    basename_match: bool
    error: Exception | None = None


# LLM: query内缓存不跨请求共享，也不改变共用流式解码器的错误合同；open_errors记录安全打开拒绝原因。
# 类用途: 将查找条件与路径计算缓存传给索引扫描。
@dataclass(frozen=True)
class _ArtifactIndexQuery:
    artifact_ref: str
    resolved_ref: Path | None
    paths: _ArtifactPathCache = field(default_factory=_ArtifactPathCache)
    open_errors: list[str] = field(default_factory=list)
    physical_only: bool = False


# LLM: 已安全打开的FD携带索引名供原读取计量识别；是整数描述符，不会再次按路径打开。
# 类用途: 将安全FD及只读展示路径交给标准Path.open/io.open流包装。
class _ArtifactIndexDescriptor(int):
    # LLM: io.open按整数FD接管关闭责任，path只供既有诊断/计量观察，不参与打开或授权。
    # 函数用途: 为一个已经nofollow验证的索引FD附上原路径元数据。
    def __new__(cls, descriptor: int, path: Path):
        instance = super().__new__(cls, descriptor)
        instance.path, instance.name, instance.parent = path, path.name, path.parent
        return instance


# LLM: 共用流式解码器只要求exists/open协议；保留未经resolve替换的认证根为锚，路径投影只给相对组件，能力不足失败关闭。
# 类用途: 让原索引解码器消费锚定FD而不是重新打开可变路径。
@dataclass(frozen=True)
class _ArtifactIndexSource:
    root: Path
    path: Path
    relative_parts: tuple[str, ...]

    # LLM: 用lexists不跟随符号链接，确保悬空链接进入open被严格拒绝而非当作缺失文件跳过。
    # 函数用途: 沿共用读取器协议报告索引路径节点是否存在。
    def exists(self) -> bool:
        return os.path.lexists(self.path)

    # LLM: Path.open最终把整数交给io.open，复用原索引计量；流创建失败关闭未移交FD，不按路径重开。
    # 函数用途: 安全打开索引并将同一FD包装成共用解码器所需的二进制流。
    def open(self, mode: str):
        descriptor = open_readonly_file_beneath(self.root, self.relative_parts)
        try:
            return Path.open(_ArtifactIndexDescriptor(descriptor, self.path), mode)
        except BaseException:
            os.close(descriptor)
            raise


# LLM: 每次先安全打开索引，再复用解析；字面索引保留全部同名记录供scope裁决，物理别名仍扫描。
# 函数用途: 从缓存索引定位候选，保留打开与严格解码错误合同。
def _index_records(index_path: Path, query: _ArtifactIndexQuery | None = None, *, root: Path | None = None, trusted_root: Path | None = None) -> list[_ArtifactIndexCandidate]:
    source = _ArtifactIndexSource(trusted_root or root, index_path, index_path.relative_to(root).parts) if root is not None else index_path
    if query is None:
        _validate_artifact_index_file(source)
        return []
    records: list[_ArtifactIndexCandidate] = []
    try:
        rows, by_literal = _parsed_artifact_index(source)
        selected = by_literal.get(query.artifact_ref, ()) if query.resolved_ref is None and not query.physical_only else rows
        for record in selected:
            candidate = _artifact_index_candidate(record, query)
            if candidate is not None:
                records.append(candidate)
    except FileNotFoundError:
        return []
    except OSError as exc:
        query.open_errors.append(f"{type(exc).__name__}: {exc}")
        return []
    return records


# LLM: 逻辑命中才验证注册路径，坏的命中不能被缓存藏掉；物理查找仍保留别名及延迟路径错误。
# 函数用途: 一次计算候选并验证命中路径，保留待报告错误。
def _artifact_index_candidate(record: dict[str, Any], query: _ArtifactIndexQuery) -> _ArtifactIndexCandidate | None:
    try:
        exact = _record_matches_ref(
            record,
            query.artifact_ref,
            query.resolved_ref,
            path_cache=query.paths,
            physical_only=query.physical_only,
        )
        if exact:
            _resolve_index_record_path(str(record.get("path", "") or ""), cache=query.paths)
        basename = bool(
            (query.resolved_ref is not None or query.physical_only)
            and Path(str(record.get("path", "") or "")).name == _safe_expanduser(Path(query.artifact_ref)).name
        )
        return _ArtifactIndexCandidate(record, exact, basename) if exact or basename else None
    except Exception as exc:
        return _ArtifactIndexCandidate(record, False, False, exc)


# LLM: 错误引用仍先读完原索引以保持异常优先级，但无查询不能退回全集列表；IO沿原逐文件忽略。
# 函数用途: 只验证索引读取和解码，不保留任何记录。
def _validate_artifact_index_file(path: Path) -> None:
    try:
        for _ in iter_decoded_tool_index_objects(path):
            pass
    except OSError:
        pass


# LLM: 只借用外层已打开FD，不重新打开路径；原共享解码器仍负责通用换行和晚解码异常。
# 类用途: 让索引缓存从同一个严格打开的流建立解析投影。
@dataclass(frozen=True)
class _OpenArtifactIndex:
    handle: Any

    # LLM: 外层with已经打开，存在性不再查询磁盘。
    # 函数用途: 向共享解码器报告当前流存在。
    def exists(self):
        return True

    # LLM: nullcontext不接管关闭责任，唯一外层with负责关闭FD。
    # 函数用途: 将已经打开的二进制流借给共享解码器。
    def open(self, mode):
        return nullcontext(self.handle)


# LLM: 32键/16MiB有界缓存以路径和FD完整stat失效；开盘与解析不持缓存锁，不缓存权限和失败。
# 函数用途: 安全打开索引并建立字面引用到有序记录列表的常数时间查找表。
def _parsed_artifact_index(source):
    with source.open("rb") as handle:
        before = _blob_stat_signature(os.fstat(handle.fileno()))
        key = str(getattr(source, "root", "")), str(getattr(source, "path", source)), *before
        cached = _ARTIFACT_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
        rows = tuple(row for _, row in iter_decoded_tool_index_objects(_OpenArtifactIndex(handle)))
        by_literal: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for ref in {str(row.get(field, "") or "") for field in _INDEX_LITERAL_FIELDS}:
                by_literal.setdefault(ref, []).append(row)
        result = rows, by_literal
        if _blob_stat_signature(os.fstat(handle.fileno())) == before:
            _ARTIFACT_INDEX_CACHE.put(key, result)
        return result


# LLM: 仅完整scoped身份可绕过历史发现；真实查找仍执行一次，保留安全打开、命中路径和scope验证。
# 函数用途: 用已解析主根字面表判断能否只查主根，不额外调用记录查找入口。
def _primary_index_has_scoped_ref(root, artifact_ref, request):
    if ":" not in artifact_ref or Path(artifact_ref).is_absolute():
        return False
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    source = _ArtifactIndexSource(Path(request.root).expanduser(), index, index.relative_to(root).parts)
    try:
        _rows, literals = _parsed_artifact_index(source)
    except OSError:
        return False
    matches = list(literals.get(artifact_ref, ()))
    if _request_has_scope(request):
        matches = _exact_scope_matches(matches, request)
    return any(str(row.get("scoped_call_id") or "") == artifact_ref
               and _record_has_scope(row) and _record_allowed_for_read_scope(row, request) for row in matches)


def _record_source_hint(record: dict[str, Any]) -> str:
    # 未外置记录里仍留着"这条 tool output 当初读/跑的是什么"——优先 source_input,其次原始
    # 参数里的 path,给模型一个能直接 read_file 的具体来源,免去 compaction 后瞎找。
    params = record.get("parameters") if isinstance(record.get("parameters"), dict) else {}
    return str(
        record.get("source_input")
        or record.get("source_path")
        or (params.get("path") if isinstance(params, dict) else "")
        or ""
    ).strip()


def _error_payload(error_code: str, artifact_ref: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "artifact_ref": artifact_ref,
        "error_code": error_code,
        "message": message,
        "reads_artifact_body": False,
    }


# LLM: 发现路径不是权限源，丢弃已逃出规范root的索引；原认证根另送nofollow，保留顺序并复用prepared的发现。
# 函数用途: 沿同一次发现安全扫描候选，下一查询重新发现目录。
def _index_records_for_root(root: Path, query: _ArtifactIndexQuery | None = None, *, index_paths: tuple[Path, ...] | None = None, trusted_root: str | Path | None = None) -> list[_ArtifactIndexCandidate]:
    records: list[_ArtifactIndexCandidate] = []
    anchor = root.expanduser().resolve(strict=False)
    paths = tool_output_index_paths_for_lookup(root) if index_paths is None else index_paths
    for index_path in paths:
        if _is_under_allowed_root(index_path, anchor):
            records.extend(_index_records(index_path, query, root=anchor, trusted_root=Path(trusted_root) if trusted_root is not None else root))
    return records


# LLM: 两端已由prepare/显式目标解析为规范绝对路径；这里只做包含判定，最终安全性由认证root逐段打开保证。
# 函数用途: 拒绝目录发现逃逸，不再次resolve候选目录来扩大授权。
def _is_under_allowed_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _looks_like_path(value: str) -> bool:
    return any(mark in value for mark in ("/", "\\")) or value.endswith(".json")
