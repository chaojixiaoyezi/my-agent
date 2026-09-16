"""代理树的模型只读视图，与界面共用事实源但不暴露内部恢复路径。"""

# LLM: 这里只投影已经通过 owner/subtree 裁决的快照；不得重新查树、改状态或推断完成。
# 模块用途: 把界面大快照变成模型可直接使用的状态和产物引用，完整正文仍交统一归档保存。

from __future__ import annotations

import json

_NODE_FIELDS = (
    "run_id", "parent_run_id", "root_run_id", "agent_name", "role", "status",
    "failure_type", "current_tool", "updated_at", "seconds_since_progress",
    "not_done_reason", "goal_digest", "last_progress_summary", "blockers",
)
_LIVE_CHARS = 12000


# LLM: 输入必须是 agent_tree_status_payload 的已授权结果；UI/source/recovery refs 不属于读取合同。
# 函数用途: 为模型生成紧凑快照，保留每个代理身份、状态、阻塞原因和实际可读产物，不写任何文件。
def agent_tree_model_payload(snapshot: dict[str, object]) -> dict[str, object]:
    results = {
        row["run_id"]: row for row in _rows(snapshot.get("child_result_index"))
        if row.get("run_id")
    }
    main = snapshot.get("main")
    advice = snapshot.get("coordination_advice")
    return {
        "schema_version": "agent_tree_model_status.v1",
        "effect": "read_only",
        "scope": snapshot.get("scope"),
        "root_id": snapshot.get("root_id"),
        "main": _pick(main, ("run_id", "status", "current_tool")),
        "nodes": [_model_node(node, results.get(node.get("run_id"), {})) for node in _rows(snapshot.get("nodes"))],
        "scope_resolution": snapshot.get("scope_resolution", {}),
        "warnings": snapshot.get("warnings", []),
        "policy": {"read_only": True, "does_not_dispatch": True, "does_not_clear_pending_work": True},
        "guidance": advice.get("next_step_zh", "") if isinstance(advice, dict) else "",
        "read_policy": "read_order 是文件读取引用；空列表表示本快照尚无可读结果，不等于失败。不要猜内部状态路径。",
    }


# LLM: 不修改完整正文；长结果只压缩当前 prompt，未展示的节点/引用必须计数并由统一归档锚点恢复。
# 函数用途: 给大代理树保留有界且完整的 JSON 状态摘要，避免通用头尾裁剪吞掉运行状态或切坏路径。
def agent_tree_model_preview(payload: dict[str, object]) -> str:
    full = _json(payload)
    if len(full) <= _LIVE_CHARS:
        return full
    nodes = _rows(payload.get("nodes"))
    preview = {**payload, "nodes": [], "details_omitted": True, "total_nodes": len(nodes)}
    preview["details_read_policy"] = "完整节点及产物引用请使用本次工具的 read_artifact_hint；不要拼接归档路径。"
    selected: list[dict[str, object]] = []
    for node in nodes:
        row = _pick(node, ("run_id", "parent_run_id", "status", "failure_type", "current_tool", "readiness"))
        row["last_progress_summary"] = str(node.get("last_progress_summary") or "")[:160]
        refs = node.get("read_order")
        refs = refs if isinstance(refs, list) else []
        row["read_order"] = refs[:1]
        row["omitted_read_refs"] = max(0, len(refs) - 1)
        candidate = {**preview, "nodes": [*selected, row], "omitted_nodes": len(nodes) - len(selected) - 1}
        if len(_json(candidate)) > _LIVE_CHARS:
            break
        selected.append(row)
    preview["nodes"] = selected
    preview["omitted_nodes"] = len(nodes) - len(selected)
    return _json(preview)


# LLM: 只读取白名单状态字段及规范 result index，不把 recovery summary/checkpoint 当成交付结果。
# 函数用途: 生成一个代理的模型状态行，去掉重复分层字段和界面私有文件路径。
def _model_node(node: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    row = _pick(node, _NODE_FIELDS)
    row["readiness"] = result.get("readiness", "not_ready")
    row["read_order"] = list(result.get("read_order") or [])
    return row


# LLM: 不规范化状态别名、不裁剪身份或路径；仅删除没有信息的空值。
# 函数用途: 复制当前视图需要的字段，保持原始结构化事实。
def _pick(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields if key in value and value[key] not in (None, "", [], {})}


# LLM: 仅接受快照中的字典数组，不从正文推导或补建代理节点。
# 函数用途: 安全遍历已有节点或索引行。
def _rows(value: object) -> list[dict[str, object]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


# LLM: 模型视图与归档使用同一 JSON 编码，保持引用原文且不引入展示缩写。
# 函数用途: 将结构化快照编码成紧凑中文 JSON。
def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
