# LLM: 只优化注册索引查找，权限边界、最后匹配和正文哈希保持原契约。
# 模块用途: 按显式注册引用读取工具输出正文。
from __future__ import annotations

"""explicit refs-only-to-body reader for externalized tool output artifacts."""

import hashlib
import json
import os
from dataclasses import dataclass
from errno import ELOOP
from pathlib import Path
from typing import Any

from ...common.tool_index_stream import iter_decoded_tool_index_objects
from ...common.tool_output_paths import (
    tool_output_index_paths_for_lookup,
    tool_output_roots_for_lookup,
)
from .read_modes import (
    ArtifactContentReadRequest,
    ArtifactContentReadResult,
    read_artifact_content_by_mode,
)


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


@dataclass(frozen=True)
class _RegisteredArtifactRead:
    path: Path
    record: dict[str, Any]
    request: ReadToolOutputArtifactRequest


def read_tool_output_artifact(request: ReadToolOutputArtifactRequest) -> dict[str, Any]:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return _error_payload("missing_artifact_ref", artifact_ref, "artifact_ref is required")
    record = _find_index_record(root, artifact_ref, request)
    if record is None:
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
    allowed_roots = _tool_output_roots(root)
    if not any(_is_under_allowed_root(path, allowed_root) for allowed_root in allowed_roots):
        return _error_payload("artifact_path_outside_tool_outputs", artifact_ref, "registered path is outside tool_outputs")
    if not path.is_file():
        return _error_payload("artifact_missing", artifact_ref, "registered artifact file does not exist")
    return _read_registered_artifact(_RegisteredArtifactRead(path=path, record=record, request=request))


def estimate_tool_output_artifact_size(request: ReadToolOutputArtifactRequest) -> int | None:
    root = Path(request.root).expanduser().resolve(strict=False)
    artifact_ref = str(request.artifact_ref or "").strip()
    if not artifact_ref:
        return None
    record = _find_index_record(root, artifact_ref, request)
    if record is None:
        return None
    try:
        return max(0, int(record.get("size_bytes") or 0))
    except (TypeError, ValueError):
        return None


def _read_registered_artifact(read: _RegisteredArtifactRead) -> dict[str, Any]:
    path = read.path
    record = read.record
    request = read.request
    artifact_ref = request.artifact_ref
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _error_payload("artifact_unreadable", artifact_ref, f"{type(exc).__name__}: {exc}")
    content = payload.get("content")
    if payload.get("kind") != "tool_output" or not isinstance(content, str):
        return _error_payload("invalid_tool_output_artifact", artifact_ref, "artifact is not a tool_output content file")
    digest = _sha256_text(content)
    expected = str(payload.get("sha256") or record.get("sha256") or "")
    if expected and digest != expected:
        return _error_payload("artifact_hash_mismatch", artifact_ref, "artifact content hash does not match metadata")
    read_result = read_artifact_content_by_mode(
        ArtifactContentReadRequest(
            content=content,
            mode=request.mode,
            offset=request.offset,
            max_chars=request.max_chars,
            query=request.query,
        )
    )
    if not read_result.ok:
        return _error_payload(read_result.error_code, artifact_ref, read_result.message)
    base = _success_base_payload(read, payload, content, digest)
    base.update(_success_content_payload(read_result))
    base.update(read_result.metadata or {})
    return base


# LLM: The reader may retain host-only path/scope facts for CLI and audit, but it must also expose a
# stable logical ref so the tool layer can project a path-free continuation contract.
# 函数用途: 组装读取成功的完整宿主事实，并附上后续模型可安全复用的逻辑引用。
def _success_base_payload(
    read: _RegisteredArtifactRead,
    payload: dict[str, Any],
    content: str,
    digest: str,
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
        "size_bytes": len(content.encode("utf-8")),
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

# LLM: 字面逻辑ref才可按原字节预筛；路径解析/链接/同名回退不能证明字节必要性，回退完整解析但只存候选。
# 函数用途: 沿原最后匹配、scope和basename回退规则查找已注册artifact，不保存历史全集。
def _find_index_record(
    root: Path,
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
) -> dict[str, Any] | None:
    try:
        ref_path = Path(artifact_ref).expanduser()
        resolved_ref = ref_path.resolve(strict=False) if ref_path.is_absolute() or _looks_like_path(artifact_ref) else None
    except Exception:
        # 原先先读取索引再求引用路径；极端引用路径错误不能遮蔽索引的读取/解码错误。
        _index_records_for_root(root)
        raise
    records = _index_records_for_root(root, _ArtifactIndexQuery(artifact_ref, resolved_ref))
    matches = [
        record
        for record in records
        if _record_matches_ref(record, artifact_ref, resolved_ref)
    ]
    scoped = _scoped_matches(matches, artifact_ref, request)
    if scoped:
        return scoped[-1]
    if matches and _request_has_scope(request):
        if resolved_ref is not None and not _request_has_strong_scope(request):
            return matches[-1]
        return None
    if matches:
        return matches[-1]
    if resolved_ref is not None:
        return _unique_record_by_basename(records, ref_path.name)
    return None


# LLM: 原匹配仍先验证每行路径；普通POSIX规范绝对路径不构造大量interned Path，其他路径保持原生Path语义。
# 函数用途: 沿原逻辑引用/路径匹配与错误合同核对一条注册记录。
def _record_matches_ref(
    record: dict[str, Any],
    artifact_ref: str,
    resolved_ref: Path | None,
) -> bool:
    record_path = _resolve_index_record_path(str(record.get("path", "") or ""))
    literal_refs = {
        str(record.get("path", "") or ""),
        str(record.get("sha256", "") or ""),
        str(record.get("scoped_call_id", "") or ""),
        str(record.get("call_id", "") or ""),
    }
    comparable_ref = str(resolved_ref) if isinstance(record_path, str) else resolved_ref
    return artifact_ref in literal_refs or bool(resolved_ref is not None and record_path == comparable_ref)


# LLM: 仅规范POSIX绝对路径可省Path构造；相对、~、..、双根与Windows均走原实现，不能改解析和大小写规则。
# 函数用途: 为索引匹配解析路径，避免不相关文件名使标准库intern池扩容。
def _resolve_index_record_path(path: str) -> str | Path:
    canonical_posix = os.name == "posix" and path.startswith("/") and not path.startswith("//") and os.path.normpath(path) == path
    if not canonical_posix:
        return Path(path).expanduser().resolve(strict=False)
    return _resolved_posix_index_path(path)


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


def _scoped_matches(
    matches: list[dict[str, Any]],
    artifact_ref: str,
    request: ReadToolOutputArtifactRequest,
) -> list[dict[str, Any]]:
    if ":" in artifact_ref:
        return matches
    exact = [record for record in matches if _record_scope_matches_request(record, request)]
    if exact:
        return exact
    if _request_has_scope(request):
        return [] if any(_record_has_scope(record) for record in matches) else matches
    return [record for record in matches if _record_has_scope(record)]


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


# LLM: 描述查找的必要字节条件与最终原判定输入，不改变artifact读取权限或正文哈希验证。
# 类用途: 将查找条件一起传给共用流式读取器，避免宽参数列表。
@dataclass(frozen=True)
class _ArtifactIndexQuery:
    artifact_ref: str
    resolved_ref: Path | None


# LLM: IO失败整份丢弃；坏JSON/非对象忽略，严格UTF-8不吞。query=None只用于引用错误时验证优先级，不收集历史。
# 函数用途: 流式读取一个工具索引的artifact查找候选，不整读文本或累积不相关记录。
def _index_records(index_path: Path, query: _ArtifactIndexQuery | None = None) -> list[dict[str, Any]]:
    if query is None:
        _validate_artifact_index_file(index_path)
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = iter_decoded_tool_index_objects(index_path)
        records.extend(record for _, record in lines if _artifact_index_candidate(record, query))
    except OSError:
        return []
    return records


# LLM: 候选错误暂存到原最终查找；索引与文件系统在扫描过程中没有事务快照保证，不宣称并发链接变化等价。
# 函数用途: 判断解析后的行是否可能用于精确ref或路径同名回退。
def _artifact_index_candidate(record: dict[str, Any], query: _ArtifactIndexQuery) -> bool:
    try:
        return _record_matches_ref(record, query.artifact_ref, query.resolved_ref) or bool(
            query.resolved_ref is not None
            and Path(str(record.get("path", "") or "")).name == Path(query.artifact_ref).expanduser().name
        )
    except Exception:
        return True


# LLM: 错误引用仍先读完原索引以保持异常优先级，但无查询不能退回全集列表；IO沿原逐文件忽略。
# 函数用途: 只验证索引读取和解码，不保留任何记录。
def _validate_artifact_index_file(path: Path) -> None:
    try:
        for _ in iter_decoded_tool_index_objects(path):
            pass
    except OSError:
        pass


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


# LLM: 所有根只枚举一次，按原目录/行顺序合并候选；最终选择仍由原_find_index_record负责。
# 函数用途: 收集owner/runs/tasks中的artifact候选，避免extend整份历史索引。
def _index_records_for_root(root: Path, query: _ArtifactIndexQuery | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index_path in tool_output_index_paths_for_lookup(root):
        records.extend(_index_records(index_path, query))
    return records


def _tool_output_roots(root: Path) -> tuple[Path, ...]:
    return tool_output_roots_for_lookup(root)


def _is_under_allowed_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _looks_like_path(value: str) -> bool:
    return any(mark in value for mark in ("/", "\\")) or value.endswith(".json")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
