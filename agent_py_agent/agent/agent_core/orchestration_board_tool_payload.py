# LLM: Subagent board payload helpers keep model-facing refs and rows out of the tool class body.
# 模块用途: 生成 subagent_board 的 filtered items、顶层 refs 和稳定 JSON row；不读取 artifact 正文。

from __future__ import annotations

from .orchestration_board_payload import board_status_filter, clip_board_text, scoped_board_items


# LLM: board_items_for_payload scopes and filters board rows before rendering.
# 函数用途: 把作用域、状态过滤和 limit 裁剪集中到 execute 外，保持工具入口短而稳定。
def board_items_for_payload(agent: object, raw_items: list[object], tool_params: dict[str, object], limit: int) -> list[object]:
    status_filter = board_status_filter(tool_params.get("status"))
    items = scoped_board_items(agent, raw_items)
    if status_filter:
        items = [item for item in items if item.status.upper() == status_filter]
    return items[:limit]


# LLM: board_payload_item is the stable JSON row shape for subagent_board.
# 函数用途: 将单条看板 item 转成模型可读字段；refs 只列路径，不读取正文。
def board_payload_item(item: object) -> dict[str, object]:
    return {
        "id": item.id,
        "root_id": str(getattr(item, "root_id", "") or ""),
        "parent_id": str(getattr(item, "parent_id", "") or ""),
        "depth": int(getattr(item, "depth", 0) or 0),
        "agent_name": str(getattr(item, "agent_name", "") or ""),
        "role": str(getattr(item, "role", "") or ""),
        "goal": clip_board_text(item.goal),
        "status": item.status,
        "verification_status": item.verification_status,
        "channel_status": item.channel_status,
        "risk_flags": item.risk_flags,
        "evidence_count": item.evidence_count,
        "child_count": int(getattr(item, "child_count", 0) or 0),
        "child_status_counts": dict(getattr(item, "child_status_counts", {}) or {}),
        "open_request_count": item.open_request_count,
        "open_gap_count": item.open_gap_count,
        "latest_summary": clip_board_text(str(getattr(item, "latest_summary", "") or ""), limit=180),
        "blocker_count": int(getattr(item, "blocker_count", 0) or 0),
        "running_seconds": int(float(getattr(item, "running_seconds", 0.0) or 0.0)),
        "seconds_since_progress": int(float(getattr(item, "seconds_since_progress", 0.0) or 0.0)),
        "target_tokens": list(getattr(item, "target_tokens", []) or []),
        "artifact_ids": item_registry_ids(item),
        "artifact_refs": item_artifact_refs(item),
        "artifact_registry_refs": item_registry_preview(item),
        "evidence_refs": item_ref_preview(item, "evidence_refs"),
        "workspace_refs": _item_workspace_refs(item),
        "recovery_refs": _item_recovery_refs(item),
        "task_dir": item.task_dir,
        "output_json": str(getattr(item, "output_json", "") or ""),
    }


# LLM: board_child_result_index gives parents the compact facts before verbose board rows.
# 函数用途: 把子代理状态、摘要和 refs 汇总成顶层索引，供父级先核对，不读取产物正文。
def board_child_result_index(items: list[object], *, limit: int = 20) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items[:limit]:
        rows.append({
            "run_id": str(getattr(item, "id", "") or ""),
            "agent_name": str(getattr(item, "agent_name", "") or ""),
            "role": str(getattr(item, "role", "") or ""),
            "status": str(getattr(item, "status", "") or ""),
            "verification_status": str(getattr(item, "verification_status", "") or ""),
            "summary": clip_board_text(str(getattr(item, "latest_summary", "") or ""), limit=220),
            "running_seconds": int(float(getattr(item, "running_seconds", 0.0) or 0.0)),
            "seconds_since_progress": int(float(getattr(item, "seconds_since_progress", 0.0) or 0.0)),
            "artifact_ids": item_registry_ids(item, limit=3),
            "artifact_refs": item_artifact_refs(item, limit=3),
            "artifact_registry_refs": item_registry_preview(item, limit=3),
            "evidence_refs": item_ref_preview(item, "evidence_refs", limit=3),
            "workspace_refs": _item_workspace_refs(item),
            "recovery_refs": _item_recovery_refs(item),
            "output_json": str(getattr(item, "output_json", "") or ""),
        })
    return rows


# LLM: board_ref_preview gives root agents direct deliverable refs before they try task_dir guesses.
# 函数用途: 汇总看板条目的 artifact/evidence refs，限制数量后放到 subagent_board 顶层。
def board_ref_preview(items: list[object], attr: str, *, limit: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for item in items:
        item_refs = item_artifact_refs(item, limit=limit) if attr == "artifact_refs" else item_ref_preview(item, attr, limit=limit)
        if _append_unique_refs(refs, seen, item_refs, limit):
            return refs
    return refs


def board_artifact_id_preview(items: list[object], *, limit: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for item in items:
        if _append_unique_refs(refs, seen, item_registry_ids(item, limit=limit), limit):
            return refs
    return refs


# LLM: _append_unique_refs bounds board refs without nesting the caller.
# 函数用途: 按顺序追加唯一 ref；达到 limit 返回 True。
def _append_unique_refs(target: list[str], seen: set[str], refs: list[str], limit: int) -> bool:
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        target.append(ref)
        if len(target) >= limit:
            return True
    return False


# LLM: item_ref_preview normalizes refs from dataclass rows and MagicMock fixtures defensively.
# 函数用途: 读取单个看板条目的 refs 字段；非列表值一律忽略，避免测试 mock 误变成字符串。
def item_ref_preview(item: object, attr: str, *, limit: int = 8) -> list[str]:
    value = getattr(item, attr, None)
    if not isinstance(value, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for ref in value:
        text = str(ref or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return refs


def item_artifact_refs(item: object, *, limit: int = 8) -> list[str]:
    registry_paths = [
        str(row.get("path") or "").strip()
        for row in item_registry_preview(item, limit=limit)
        if str(row.get("path") or "").strip()
    ]
    if registry_paths:
        return _unique_strings(registry_paths)[:limit]
    return item_ref_preview(item, "artifact_refs", limit=limit)


def item_registry_ids(item: object, *, limit: int = 8) -> list[str]:
    ids = [
        str(row.get("artifact_id") or "").strip()
        for row in item_registry_preview(item, limit=limit)
        if str(row.get("artifact_id") or "").strip()
    ]
    return _unique_strings(ids)[:limit]


def item_registry_preview(item: object, *, limit: int = 8) -> list[dict[str, object]]:
    value = getattr(item, "artifact_registry_refs", None)
    if not isinstance(value, list | tuple):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item_ref in value:
        if not isinstance(item_ref, dict):
            continue
        row = dict(item_ref)
        artifact_id = str(row.get("artifact_id") or "").strip()
        path = str(row.get("path") or "").strip()
        key = artifact_id or path
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _item_workspace_refs makes current runtime refs explicit so parent agents stop guessing legacy paths.
# 函数用途: 给看板条目展示当前 task workspace 和 agent run workspace；旧目录只留 legacy 字段。
def _item_workspace_refs(item: object) -> dict[str, str]:
    task_workspace = _text_attr(item, "task_workspace") or _text_attr(item, "task_dir")
    refs = {
        "task_workspace": task_workspace,
        "agent_run_workspace": _text_attr(item, "agent_run_workspace"),
    }
    return {key: value for key, value in refs.items() if value}


# LLM: _item_recovery_refs gives parents exact status/progress files before any manual path probing.
# 函数用途: 暴露 checkpoint、summary、final_report 和 latest_tool_progress 的当前路径。
def _item_recovery_refs(item: object) -> dict[str, str]:
    refs = {
        "checkpoint": _text_attr(item, "checkpoint_ref"),
        "summary": _text_attr(item, "summary_ref"),
        "final_report": _text_attr(item, "final_report_ref"),
        "latest_tool_progress": _text_attr(item, "latest_tool_progress_ref"),
    }
    return {key: value for key, value in refs.items() if value}


# LLM: _text_attr ignores MagicMock auto-attributes so tests and partial rows stay clean.
# 函数用途: 只接受真实字符串属性，避免 mock 的自动属性被渲染进模型提示。
def _text_attr(item: object, attr: str) -> str:
    value = getattr(item, attr, "")
    return value if isinstance(value, str) else ""
