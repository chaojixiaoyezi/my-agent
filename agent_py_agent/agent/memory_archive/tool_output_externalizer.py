# LLM: Memory archive module; keep large tool outputs out of compact ledgers and raw event rows.
# 模块用途: 将过大的工具输出外置为 artifact 文件，并返回可归档的摘要、hash 和路径。

from __future__ import annotations

"""externalizes large runtime tool outputs into artifact files.

Human version:
Tool results can be useful evidence, but large bodies do not belong in raw
archive rows, token ledgers, or compact metadata. This module writes the full
tool output to an artifact file and returns a compact record with preview,
hash, size, and path. It does not change what the current model turn sees.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

DEFAULT_EXTERNALIZE_MIN_CHARS = 1200
OUTPUT_PREVIEW_CHARS = 500
TOOL_OUTPUT_RECORD_SCHEMA = RuntimeMemorySchemaOptions("tool_output_archive_record")
TOOL_OUTPUT_ARTIFACT_SCHEMA = RuntimeMemorySchemaOptions("tool_output_artifact")
TOOL_OUTPUT_INDEX_SCHEMA = RuntimeMemorySchemaOptions("tool_output_index")


# LLM: ExternalizeToolOutputRequest 是工具输出外置的业务入口 bundle；后续阈值、格式和保留字段都放这里。
# 类用途: 保存工具调用、结果、运行标识和写入根目录；调用 externalize_tool_output_record 后才会写 artifact 文件。
@dataclass(frozen=True)
class ExternalizeToolOutputRequest:
    root: str | Path
    tool: str
    call_id: str
    output: str
    ok: bool
    run_id: str = ""
    task_id: str = ""
    request_id: str = ""
    min_chars: int = DEFAULT_EXTERNALIZE_MIN_CHARS


# LLM: externalize_tool_output_record 是 runtime 工具输出进入 memory archive artifact 的唯一入口。
# 函数用途: 大输出写 artifact 文件，小输出只返回 preview/hash；返回值可直接放入 archive_tool_calls。
def externalize_tool_output_record(request: ExternalizeToolOutputRequest) -> dict[str, Any]:
    output = str(request.output or "")
    digest = _sha256_text(output)
    record = _base_record(request, output, digest)
    if len(output) >= max(0, int(request.min_chars)):
        path = _write_output_artifact(request, output, digest)
        record.update({
            "output_externalized": True,
            "output_path": str(path),
            "artifact_ref": str(path),
        })
    return record


# LLM: _base_record 保持归档记录短小稳定；永远不把完整工具输出塞进返回 dict。
# 函数用途: 生成工具输出摘要字段，包括 preview、hash、size、状态和保留扩展字段。
def _base_record(request: ExternalizeToolOutputRequest, output: str, digest: str) -> dict[str, Any]:
    return {
        "version": TOOL_OUTPUT_RECORD_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_RECORD_SCHEMA),
        "tool": request.tool,
        "id": request.call_id,
        "ok": request.ok,
        "output_preview": _preview(output),
        "output_hash": digest,
        "output_size_bytes": len(output.encode("utf-8")),
        "output_externalized": False,
        "output_path": "",
        "reserved": runtime_memory_reserved_fields(TOOL_OUTPUT_RECORD_SCHEMA),
    }


# LLM: _write_output_artifact 是唯一文件写入点；路径固定在 memory_archive/artifacts/tool_outputs 下。
# 函数用途: 将完整工具输出写入 JSON artifact，内容和 metadata 放同一文件便于后续审计。
def _write_output_artifact(request: ExternalizeToolOutputRequest, output: str, digest: str) -> Path:
    path = _artifact_path(request, digest)
    created_at = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "version": TOOL_OUTPUT_ARTIFACT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_ARTIFACT_SCHEMA),
        "kind": "tool_output",
        "tool": request.tool,
        "call_id": request.call_id,
        "ok": request.ok,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "sha256": digest,
        "size_bytes": len(output.encode("utf-8")),
        "created_at": created_at,
        "content": output,
        "reserved": runtime_memory_reserved_fields(TOOL_OUTPUT_ARTIFACT_SCHEMA),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_index(path, payload)
    return path


# LLM: _append_index 维护工具输出 artifact 的轻量 manifest；只追加摘要，不复制正文。
# 函数用途: 将外置工具输出的路径、hash、size 和运行标识写入 index.jsonl，供 compact/resume 快速扫描。
def _append_index(path: Path, payload: dict[str, Any]) -> None:
    record = {
        "version": TOOL_OUTPUT_INDEX_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_INDEX_SCHEMA),
        "kind": payload["kind"],
        "tool": payload["tool"],
        "call_id": payload["call_id"],
        "request_id": payload["request_id"],
        "run_id": payload["run_id"],
        "task_id": payload["task_id"],
        "path": str(path),
        "sha256": payload["sha256"],
        "size_bytes": payload["size_bytes"],
        "created_at": payload["created_at"],
        "reserved": runtime_memory_reserved_fields(TOOL_OUTPUT_INDEX_SCHEMA),
    }
    index_path = path.parent / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: _artifact_path 负责生成文件名安全且稳定可查的工具输出 artifact 路径。
# 函数用途: 根据 root、工具名、call_id 和内容 hash 计算 artifact JSON 文件路径。
def _artifact_path(request: ExternalizeToolOutputRequest, digest: str) -> Path:
    return (
        Path(request.root)
        / "memory_archive"
        / "artifacts"
        / "tool_outputs"
        / f"{_safe_segment(request.tool)}-{_safe_segment(request.call_id)}-{digest[:12]}.json"
    )


# LLM: _preview 控制归档预览长度；完整内容只能去 artifact 文件读取。
# 函数用途: 截断工具输出为可读预览，避免 raw event 和 token ledger 被大输出撑大。
def _preview(output: str) -> str:
    if len(output) <= OUTPUT_PREVIEW_CHARS:
        return output
    return output[:OUTPUT_PREVIEW_CHARS] + f"\n... [truncated {len(output) - OUTPUT_PREVIEW_CHARS} chars]"


# LLM: _sha256_text 提供内容寻址和完整性校验所需的稳定 hash。
# 函数用途: 计算 UTF-8 文本的 sha256 hex digest。
def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# LLM: _safe_segment 避免工具名和 call id 把 artifact 写到预期目录外。
# 函数用途: 将任意标识符压成安全文件名片段。
def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value or "item"))
    return cleaned.strip("._") or "item"


__all__ = ["ExternalizeToolOutputRequest", "externalize_tool_output_record"]
