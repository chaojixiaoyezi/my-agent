# LLM: Runtime handoff is a soft compact-resume hint, shared by chat, subagents, and long tasks.
# 模块用途: 从同一任务范围内读取最近补充提示和下级状态，生成通用运行交接摘要；不调度、不验收、不阻断。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: build_runtime_handoff returns soft resume clues and must not mutate task state.
# 函数用途: 汇总同范围的运行中提示和下级状态，供 compact 后继续接上当前工作。
def build_runtime_handoff(workspace: Path, ids: list[str]) -> dict[str, Any]:
    guidance = _recent_guidance(workspace, ids)
    tree = _agent_tree_handoff(workspace, ids)
    if not guidance and not tree["active_agents"] and tree["counts"]["total"] == 0:
        return {}
    return {
        "schema_version": "runtime_handoff.v1",
        "recent_guidance": guidance,
        "agent_tree": tree,
        "next_suggestion": (
            "继续推进当前任务；先查看进度账本和下级状态，再决定是否补充引导或接手。"
        ),
        "soft_only": True,
    }


# LLM: runtime_handoff_payload crops runtime handoff before embedding it into compact packets.
# 函数用途: 压缩 runtime_handoff 字段，只保留续接需要的短摘要和计数。
def runtime_handoff_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    guidance = payload.get("recent_guidance") if isinstance(payload.get("recent_guidance"), list) else []
    tree = payload.get("agent_tree") if isinstance(payload.get("agent_tree"), dict) else {}
    active = tree.get("active_agents") if isinstance(tree.get("active_agents"), list) else []
    return {
        "schema_version": str(payload.get("schema_version") or ""),
        "soft_only": bool(payload.get("soft_only", True)),
        "next_suggestion": str(payload.get("next_suggestion") or ""),
        "recent_guidance_count": len(guidance),
        "recent_guidance": [_short_guidance(row) for row in guidance[:5] if isinstance(row, dict)],
        "agent_tree": {
            "counts": dict(tree.get("counts", {}) if isinstance(tree.get("counts"), dict) else {}),
            "active_agents": [_short_agent(row) for row in active[:8] if isinstance(row, dict)],
        },
    }


# LLM: render_runtime_handoff_lines renders soft context for the next model turn.
# 函数用途: 把运行交接摘要渲染成可读 Markdown 行，供恢复上下文直接注入。
def render_runtime_handoff_lines(value: Any, *, title: str = "Runtime Handoff") -> list[str]:
    payload = value if isinstance(value, dict) else {}
    guidance = payload.get("recent_guidance") if isinstance(payload.get("recent_guidance"), list) else []
    tree = payload.get("agent_tree") if isinstance(payload.get("agent_tree"), dict) else {}
    active = tree.get("active_agents") if isinstance(tree.get("active_agents"), list) else []
    if not guidance and not active:
        return []
    lines = [f"## {title}", ""]
    suggestion = str(payload.get("next_suggestion") or "")
    if suggestion:
        lines.append(f"- next_suggestion: {suggestion}")
    lines.extend(_guidance_lines(guidance))
    lines.extend(_active_agent_lines(active))
    lines.append("")
    return lines


# LLM: _recent_guidance reads only scoped guidance rows for compact handoff.
# 函数用途: 从 guidance 账本中取当前 run/thread/task/case 相关的最近软提示。
def _recent_guidance(workspace: Path, ids: list[str]) -> list[dict[str, Any]]:
    guidance_dir = workspace / "data" / "conversations" / "guidance"
    if not guidance_dir.exists():
        return []
    id_set = set(ids)
    rows = [
        row
        for path in _guidance_candidate_files(guidance_dir, ids)
        for row in _read_jsonl_dicts(path)
        if str(row.get("target_id") or "").strip() in id_set
    ]
    rows.sort(key=lambda row: _safe_float(row.get("created_at")), reverse=True)
    return [_guidance_row(row) for row in rows[:8]]


# LLM: _guidance_candidate_files limits the guidance files scanned during compact.
# 函数用途: 优先列出精确匹配的 guidance 文件，再用少量候选兜底。
def _guidance_candidate_files(guidance_dir: Path, ids: list[str]) -> list[Path]:
    exact = [
        path
        for item_id in ids
        for path in _guidance_exact_paths(guidance_dir, item_id)
        if path.exists()
    ]
    return _dedupe_paths([*exact, *sorted(guidance_dir.glob("*.jsonl"))[:32]])


# LLM: _guidance_exact_paths builds literal file candidates without globbing ids.
# 函数用途: 按目标类型和安全文件名生成 guidance 精确路径，避免通配符串读。
def _guidance_exact_paths(guidance_dir: Path, item_id: str) -> list[Path]:
    safe = _safe_file_stem(item_id)
    return [guidance_dir / f"{target_type}.{safe}.jsonl" for target_type in ("thread", "agent_run", "task", "case")]


# LLM: _guidance_row normalizes guidance JSON rows for compact payloads.
# 函数用途: 把 guidance 原始记录整理成稳定字段，过滤掉无关大字段。
def _guidance_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "guidance_id": str(row.get("guidance_id") or ""),
        "target_type": str(row.get("target_type") or ""),
        "target_id": str(row.get("target_id") or ""),
        "message": str(row.get("message") or ""),
        "sender": str(row.get("sender") or ""),
        "priority": str(row.get("priority") or ""),
        "created_at": _safe_float(row.get("created_at")),
    }


# LLM: _agent_tree_handoff summarizes visible child agent states for resume.
# 函数用途: 汇总当前任务范围内的下级状态，给压缩后续接提供看板线索。
def _agent_tree_handoff(workspace: Path, ids: list[str]) -> dict[str, Any]:
    rows = [row for path in _agent_state_files(workspace, ids) if (row := _agent_state_payload(path))]
    return {
        "source": "workspace_task_agents",
        "counts": _agent_counts(rows),
        "active_agents": [row for row in rows if _active_status(row)][:12],
        "recent_agents": rows[:20],
    }


# LLM: _agent_state_files resolves task-local agent states with literal ids.
# 函数用途: 找到当前任务或指定代理对应的 state.json，避免扫描无关工作区。
def _agent_state_files(workspace: Path, ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item_id in ids:
        if item_id:
            paths.extend(sorted((workspace / "tasks" / item_id / "agents").glob("*/state.json")))
            paths.extend(_task_agent_state_files_for_id(workspace, item_id))
    return _dedupe_paths([path for path in paths if path.exists() and _inside_workspace(path, workspace)])


# LLM: _task_agent_state_files_for_id finds agent state files by exact run id.
# 函数用途: 在 tasks/*/agents/<run_id>/state.json 中查找指定代理状态。
def _task_agent_state_files_for_id(workspace: Path, item_id: str) -> list[Path]:
    return [
        path / "state.json"
        for path in sorted((workspace / "tasks").glob("*/agents/*"))
        if path.is_dir() and path.name == item_id and (path / "state.json").exists()
    ]


# LLM: _agent_state_payload crops agent state to resume-safe fields.
# 函数用途: 读取单个 state.json，并只返回 run、状态、当前工具和进展摘要。
def _agent_state_payload(path: Path) -> dict[str, Any]:
    payload = _read_json_dict(path)
    if not payload:
        return {}
    return {
        "run_id": str(payload.get("run_id") or path.parent.name),
        "parent_run_id": str(payload.get("parent_run_id") or ""),
        "status": str(payload.get("status") or ""),
        "current_tool": str(payload.get("current_tool") or ""),
        "last_progress_summary": str(payload.get("last_progress_summary") or payload.get("latest_summary") or ""),
        "state_ref": str(path),
    }


# LLM: _agent_counts groups child agent states by status for compact summaries.
# 函数用途: 统计下级代理总数和各状态数量，便于恢复后快速判断局面。
def _agent_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(rows)}
    for row in rows:
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


# LLM: _active_status marks unfinished child agents as active handoff items.
# 函数用途: 判断某个下级状态是否还需要恢复后继续关注。
def _active_status(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").lower() not in {"completed", "done", "succeeded"}


# LLM: _short_guidance keeps guidance payload compact for resume packets.
# 函数用途: 压短 guidance 记录，保留目标、发送者和提示正文。
def _short_guidance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_type": str(row.get("target_type") or ""),
        "target_id": str(row.get("target_id") or ""),
        "message": str(row.get("message") or ""),
        "sender": str(row.get("sender") or ""),
    }


# LLM: _short_agent keeps child state payload compact for resume packets.
# 函数用途: 压短下级代理状态，只保留续接判断最需要的字段。
def _short_agent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row.get("run_id") or ""),
        "status": str(row.get("status") or ""),
        "current_tool": str(row.get("current_tool") or ""),
        "last_progress_summary": str(row.get("last_progress_summary") or ""),
        "state_ref": str(row.get("state_ref") or ""),
    }


# LLM: _guidance_lines turns guidance rows into plain resume bullets.
# 函数用途: 将最近补充提示渲染成少量 Markdown 列表项。
def _guidance_lines(rows: list[Any]) -> list[str]:
    return [f"- guidance: {row.get('message')}" for row in rows[:5] if isinstance(row, dict) and row.get("message")]


# LLM: _active_agent_lines turns active child states into plain resume bullets.
# 函数用途: 将仍活跃的下级代理渲染成少量 Markdown 列表项。
def _active_agent_lines(rows: list[Any]) -> list[str]:
    return [
        f"- active_agent: {row.get('run_id', '')} status={row.get('status', '')} "
        f"tool={row.get('current_tool', '')} note={row.get('last_progress_summary', '')}"
        for row in rows[:5]
        if isinstance(row, dict)
    ]


# LLM: _read_json_dict safely reads optional JSON files during compact.
# 函数用途: 读取 JSON 对象文件，读不到或格式坏时返回空对象。
def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _read_jsonl_dicts safely reads optional JSONL guidance rows.
# 函数用途: 读取 JSONL 对象行，坏行自动跳过，不影响 compact。
def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [payload for line in lines if isinstance((payload := _json_line(line)), dict)]


# LLM: _json_line parses one JSONL row without throwing into compact.
# 函数用途: 解析单行 JSON，失败时返回 None。
def _json_line(line: str) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


# LLM: _inside_workspace prevents compact handoff from reading outside workspace.
# 函数用途: 判断候选状态文件是否仍位于工作区内部。
def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        resolved = path.resolve()
        root = workspace.resolve()
    except OSError:
        return False
    return root in (resolved, *resolved.parents)


# LLM: _dedupe_paths preserves file order while removing duplicate candidates.
# 函数用途: 对候选路径去重，避免同一状态或提示重复进入摘要。
def _dedupe_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


# LLM: _safe_file_stem mirrors conversation store file-name normalization.
# 函数用途: 将 run/thread/task/case id 转成安全文件名片段。
def _safe_file_stem(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in str(value or "")).strip("-") or "run"


# LLM: _safe_float keeps malformed timestamps from breaking handoff sorting.
# 函数用途: 容错转换时间戳，坏值按 0 处理。
def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["build_runtime_handoff", "render_runtime_handoff_lines", "runtime_handoff_payload"]
