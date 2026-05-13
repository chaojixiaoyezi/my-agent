# LLM: Subagent compact continuation prompt text must stay task-local and refs-first.
# 模块用途: 从子代理 run workspace 读取很小的恢复线索，生成 runner prompt 的接续段，不触碰主代理长期记忆。

from __future__ import annotations

"""Task-local compact continuation prompt section for subagent runners."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..subagent import SubAgentExecutionContext

_DEFAULT_SNIPPET_CHARS = 1200
_REF_KEYS = (
    "agent_run_task",
    "agent_run_checkpoint",
    "agent_run_summary",
    "agent_run_final_report",
    "agent_run_findings",
    "agent_run_timeline",
)


# LLM: SubagentCompactContinuationRequest keeps prompt continuation inputs explicit and bundle-shaped.
# 类用途: 包装子代理执行上下文和读取字符上限，避免 prompt 构建函数继续增加散参数。
@dataclass(frozen=True)
class SubagentCompactContinuationRequest:
    context: SubAgentExecutionContext
    max_chars: int = _DEFAULT_SNIPPET_CHARS


# LLM: build_subagent_compact_continuation_section renders only local refs and bounded snippets.
# 函数用途: 为子代理 runner prompt 生成任务本地 compact 接续段；不读取 SOUL/USER/AGENTS 或主 memory。
def build_subagent_compact_continuation_section(request: SubagentCompactContinuationRequest) -> str:
    refs = _workspace_refs(request.context)
    packet = _latest_continue_packet_path(refs)
    existing_refs = _existing_refs(refs)
    if not existing_refs and not packet:
        return ""
    lines = [
        "## Task-Local Compact Continuation",
        "",
        "- memory_scope: task_local",
        "- writes_main_memory: false",
        "- automatic_tool_execution: none",
        "- 只从下面的子代理任务目录接续；不要读取或写入主代理长期 memory。",
        "",
    ]
    lines.extend(_packet_lines(packet, request.max_chars))
    lines.extend(_ref_lines(existing_refs))
    lines.extend(_snippet_lines(existing_refs, request.max_chars))
    return "\n".join(lines).rstrip()


# LLM: _workspace_refs tolerates older context bundles and derives missing run-workspace refs.
# 函数用途: 从 context_bundle.workspace_refs 中取路径；缺少细分字段时从 agent_run_workspace 补出标准文件名。
def _workspace_refs(context: SubAgentExecutionContext) -> dict[str, str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    refs = bundle.get("workspace_refs") if isinstance(bundle.get("workspace_refs"), dict) else {}
    values = {str(key): str(value) for key, value in refs.items() if str(value or "").strip()}
    run_workspace = values.get("agent_run_workspace", "")
    if run_workspace:
        base = Path(run_workspace)
        values.setdefault("agent_run_task", str(base / "task.md"))
        values.setdefault("agent_run_checkpoint", str(base / "checkpoint.json"))
        values.setdefault("agent_run_summary", str(base / "summary.md"))
        values.setdefault("agent_run_final_report", str(base / "final_report.md"))
        values.setdefault("agent_run_findings", str(base / "findings.jsonl"))
        values.setdefault("agent_run_timeline", str(base / "timeline.jsonl"))
        values.setdefault("agent_run_compactions", str(base / "compactions"))
    compactions = values.get("agent_run_compactions", "")
    if compactions:
        values.setdefault("agent_run_latest_continue_packet", str(Path(compactions) / "latest_continue_packet.json"))
    return values


# LLM: _latest_continue_packet_path returns a real file only, so stale reserved refs do not thicken prompts.
# 函数用途: 找到已经存在的 latest_continue_packet.json；不存在时返回 None，避免暗示可恢复包已就绪。
def _latest_continue_packet_path(refs: dict[str, str]) -> Path | None:
    value = refs.get("agent_run_latest_continue_packet", "")
    if not value and refs.get("agent_run_compactions"):
        value = str(Path(refs["agent_run_compactions"]) / "latest_continue_packet.json")
    path = Path(value) if value else None
    return path if path and path.exists() else None


# LLM: _existing_refs filters prompt refs to files and dirs that are currently usable.
# 函数用途: 只展示当前存在的任务本地恢复文件，避免 runner 追不存在路径。
def _existing_refs(refs: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _REF_KEYS:
        value = refs.get(key, "")
        if value and Path(value).exists():
            result[key] = value
    return result


# LLM: _packet_lines summarizes continue packet metadata without dumping the whole JSON body.
# 函数用途: 渲染 latest_continue_packet 的 ready、mode、next_action 和 recommended paths。
def _packet_lines(path: Path | None, max_chars: int) -> list[str]:
    if not path:
        return []
    payload = _read_json(path)
    lines = ["### Continue Packet", "", f"- latest_continue_packet: {path}"]
    for key in ("ready_to_continue", "continue_mode", "next_action"):
        if key in payload:
            lines.append(f"- {key}: {_short(payload[key], max_chars)}")
    paths = payload.get("recommended_read_paths")
    if isinstance(paths, list) and paths:
        lines.append("- recommended_read_paths:")
        lines.extend(f"  - {_short(item, max_chars)}" for item in paths[:5])
    return lines + [""]


# LLM: _ref_lines lists durable task-local files before snippets so readers can inspect originals.
# 函数用途: 给 runner 和父级状态一个稳定路径清单，正文很长时仍可按路径追溯。
def _ref_lines(refs: dict[str, str]) -> list[str]:
    if not refs:
        return []
    lines = ["### Recovery Refs", ""]
    lines.extend(f"- {key}: {value}" for key, value in refs.items())
    return lines + [""]


# LLM: _snippet_lines gives tiny human-readable state hints while keeping artifact bodies external.
# 函数用途: 读取 checkpoint/summary/task 等小片段，帮助压缩后的子代理知道从哪继续。
def _snippet_lines(refs: dict[str, str], max_chars: int) -> list[str]:
    lines: list[str] = []
    for key in ("agent_run_checkpoint", "agent_run_summary", "agent_run_task", "agent_run_findings"):
        text = _read_text(Path(refs[key]), max_chars) if key in refs else ""
        if text:
            lines.extend([f"### {key}", "", text, ""])
    return lines


# LLM: _read_json is intentionally forgiving because corrupted packets should not crash prompt rendering.
# 函数用途: 容错读取 JSON 对象，失败时返回空对象并让其它 refs 继续可用。
def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")[: _DEFAULT_SNIPPET_CHARS * 4])
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _read_text bounds every file snippet so a huge log cannot flood the runner prompt.
# 函数用途: 安全读取短文本片段；超长内容追加截断提示，原文仍通过 refs 可追溯。
def _read_text(path: Path, max_chars: int) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = _pretty_json_text(raw) if path.suffix == ".json" else raw
    limit = max(200, int(max_chars or _DEFAULT_SNIPPET_CHARS))
    if len(text) <= limit:
        return text.strip()
    return f"{text[:limit].rstrip()}\n... [truncated; read ref for full content]"


# LLM: _pretty_json_text keeps escaped unicode readable to the runner and reviewers.
# 函数用途: 对 JSON 恢复片段做可读化渲染；解析失败时保留原文，避免损坏文件导致 prompt 失败。
def _pretty_json_text(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(payload, ensure_ascii=False, indent=2)


# LLM: _short keeps scalar packet fields readable inside one prompt bullet.
# 函数用途: 把 continue packet 的值压成单行摘要，避免列表或对象撑大 prompt。
def _short(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    limit = max(80, min(int(max_chars or _DEFAULT_SNIPPET_CHARS), 300))
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


__all__ = ["SubagentCompactContinuationRequest", "build_subagent_compact_continuation_section"]
