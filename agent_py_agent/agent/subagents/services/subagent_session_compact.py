# LLM: Subagent session compact packages turn save=False runner compact signals into task-local refs.
# 模块用途: 把子代理模型回合的压缩线索写进 agent run workspace，不写主代理 memory_archive。

from __future__ import annotations

"""Task-local session compact package writer for subagent runners."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import SubAgentTask
from .compact_continue_packet import (
    SubagentContinuePacketRequest,
    subagent_restore_refs,
    write_subagent_continue_packet,
)

_SCHEMA_VERSION = "subagent_session_compact.v1"


# LLM: SubagentSessionCompactRequest keeps compact package writes bundle-shaped and testable.
# 类用途: 汇总写子代理本地 compact 包所需的 task、compact 信号和 runner output 小字段。
@dataclass(frozen=True)
class SubagentSessionCompactRequest:
    task: SubAgentTask
    compact_payload: dict[str, object]
    output_payload: dict[str, object]


# LLM: write_subagent_session_compact writes only under task.agent_run_compactions_dir.
# 函数用途: 生成子代理本地 compact metadata/summary/restore refs，并刷新 latest_continue_packet。
def write_subagent_session_compact(request: SubagentSessionCompactRequest) -> dict[str, str]:
    if not _should_write(request.compact_payload):
        return {}
    compactions = _compactions_dir(request.task)
    if not compactions:
        return {}
    compactions.mkdir(parents=True, exist_ok=True)
    package_dir = compactions / "packages" / _package_id(request.task)
    package_dir.mkdir(parents=True, exist_ok=True)
    refs = _package_refs(compactions, package_dir)
    metadata = _metadata_payload(request, refs)
    refs["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    refs["restore_refs"].write_text(json.dumps(metadata["restore_refs"], ensure_ascii=False, indent=2), encoding="utf-8")
    refs["summary"].write_text(_summary_text(metadata), encoding="utf-8")
    _write_latest_refs(request.task, refs)
    _append_session_ledger(compactions / "session_compact_ledger.jsonl", metadata, refs)
    write_subagent_continue_packet(SubagentContinuePacketRequest(request.task, request.output_payload))
    return {"metadata_ref": str(refs["latest_metadata"]), "summary_ref": str(refs["latest_summary"])}


# LLM: _should_write treats compact suggestions and explicit auto states as package-worthy signals.
# 函数用途: 过滤空 compact 输入；只有达到压缩阈值或已有 auto 状态时才写本地 compact 包。
def _should_write(payload: dict[str, object]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if bool(payload.get("suggested")):
        return True
    status = str(payload.get("auto_status") or payload.get("status") or "").strip()
    return status not in {"", "ok", "skipped_below_threshold", "skipped_after_guarded_continuation"}


# LLM: _compactions_dir keeps the writer inert for old tasks without run workspaces.
# 函数用途: 返回 agent run workspace 的 compactions 目录；旧任务缺字段时不写任何文件。
def _compactions_dir(task: SubAgentTask) -> Path | None:
    value = str(getattr(task, "agent_run_compactions_dir", "") or "").strip()
    return Path(value) if value else None


# LLM: _package_id makes compact package ids stable enough for humans and unique enough for repeated cycles.
# 函数用途: 用时间戳和 run_id 生成本地 compact 包目录名，支持同一子代理多次压缩。
def _package_id(task: SubAgentTask) -> str:
    return f"session-compact-{int(time.time() * 1000)}-{_safe_id(task.id)}"


# LLM: _package_refs centralizes package and latest ref names so prompt readers use one convention.
# 函数用途: 生成 package 内部文件和 compactions/latest_* 快捷引用路径。
def _package_refs(compactions: Path, package_dir: Path) -> dict[str, Path]:
    return {
        "metadata": package_dir / "metadata.json",
        "summary": package_dir / "summary.md",
        "restore_refs": package_dir / "restore_refs.json",
        "latest_metadata": compactions / "latest_metadata.json",
        "latest_summary": compactions / "latest_summary.md",
    }


# LLM: _metadata_payload keeps subagent compact data refs-first and main-memory-free.
# 函数用途: 组装子代理本地 compact metadata，不复制产物正文，只保存恢复路径和下一步。
def _metadata_payload(request: SubagentSessionCompactRequest, refs: dict[str, Path]) -> dict[str, Any]:
    task = request.task
    compact = request.compact_payload
    restore_refs = subagent_restore_refs(task)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_session_compact_package",
        "package_id": refs["metadata"].parent.name,
        "created_at": time.time(),
        "owner": {"owner_type": "subagent_run", "owner_id": task.id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "status": task.status,
        "verification_status": task.verification_status,
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "next_action": _next_action(task, request.output_payload),
        "source": _source_payload(compact),
        "token_budget": _dict_payload(compact.get("token_budget")),
        "restore_refs": restore_refs,
        "package_refs": {key: str(value) for key, value in refs.items()},
        "reserved": {},
    }


# LLM: _source_payload stores compact decision facts without copying full prompt/response text.
# 函数用途: 保存触发 compact 的状态、比例、消息和推荐命令，便于调试但不膨胀恢复包。
def _source_payload(compact: dict[str, object]) -> dict[str, object]:
    return {
        "suggested": bool(compact.get("suggested")),
        "status": str(compact.get("status") or ""),
        "auto_status": str(compact.get("auto_status") or ""),
        "ratio": _float_value(compact.get("ratio")),
        "message": str(compact.get("message") or ""),
        "commands": _string_list(compact.get("commands")),
    }


# LLM: _summary_text gives humans and LLM runners a tiny digest while metadata keeps exact refs.
# 函数用途: 渲染 latest_summary.md；只列状态、下一步和核心 ref，不展开大文件。
def _summary_text(metadata: dict[str, Any]) -> str:
    refs = metadata.get("restore_refs", {}) if isinstance(metadata.get("restore_refs"), dict) else {}
    lines = [
        "# Subagent Session Compact",
        "",
        f"- schema_version: {metadata.get('schema_version', '')}",
        f"- run_id: {metadata.get('owner', {}).get('owner_id', '')}",
        f"- memory_scope: {metadata.get('memory_scope', '')}",
        f"- writes_main_memory: {str(metadata.get('writes_main_memory', False)).lower()}",
        f"- status: {metadata.get('status', '')}",
        f"- current_step: {metadata.get('current_step', '')}",
        f"- next_action: {metadata.get('next_action', '')}",
        "",
        "## Restore Refs",
    ]
    lines.extend(f"- {key}: {value}" for key, value in refs.items())
    return "\n".join(lines).rstrip() + "\n"


# LLM: _write_latest_refs keeps latest_metadata/latest_summary as copied files, not symlinks.
# 函数用途: 兼容跨平台和打包场景，把最新 compact 包复制到固定 latest 文件名。
def _write_latest_refs(task: SubAgentTask, refs: dict[str, Path]) -> None:
    refs["latest_metadata"].write_text(refs["metadata"].read_text(encoding="utf-8"), encoding="utf-8")
    refs["latest_summary"].write_text(refs["summary"].read_text(encoding="utf-8"), encoding="utf-8")
    task.agent_run_latest_compaction_metadata_json = str(refs["latest_metadata"])
    task.agent_run_latest_compaction_summary_md = str(refs["latest_summary"])


# LLM: _append_session_ledger leaves an append-only breadcrumb for every subagent compact package.
# 函数用途: 在 session_compact_ledger.jsonl 追加 compact package 审计行，不复制 metadata 正文。
def _append_session_ledger(path: Path, metadata: dict[str, Any], refs: dict[str, Path]) -> None:
    row = {
        "schema_version": "subagent_session_compact_ledger.v1",
        "event_type": "subagent_session_compact",
        "run_id": metadata.get("owner", {}).get("owner_id", ""),
        "package_id": metadata.get("package_id", ""),
        "metadata_ref": str(refs["latest_metadata"]),
        "summary_ref": str(refs["latest_summary"]),
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "created_at": metadata.get("created_at", 0.0),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# LLM: _next_action prefers structured runner next actions before status fallback.
# 函数用途: 从 output_payload 和 task 状态提取压缩后继续的下一步。
def _next_action(task: SubAgentTask, output_payload: dict[str, object]) -> str:
    for item in [*_string_list(output_payload.get("next_actions")), output_payload.get("next_action"), task.current_step]:
        text = str(item or "").strip()
        if text:
            return text
    return "resume subagent runner from task-local compact package"


# LLM: _dict_payload normalizes optional nested token budget payloads.
# 函数用途: 只保留 dict 形态的 token_budget，避免坏输入污染 metadata 结构。
def _dict_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


# LLM: _string_list normalizes scalar/list config-like fields for metadata.
# 函数用途: 把 compact commands 等字段统一成字符串列表。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


# LLM: _float_value makes compact ratio robust to missing or string inputs.
# 函数用途: 转换 compact ratio，失败时返回 0.0。
def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# LLM: _safe_id keeps generated package paths filesystem-friendly.
# 函数用途: 清理 run_id 中不适合做路径片段的字符。
def _safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value or "run"))
    return cleaned[:48] or "run"


__all__ = ["SubagentSessionCompactRequest", "write_subagent_session_compact"]
