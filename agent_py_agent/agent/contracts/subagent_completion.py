"""Versioned contracts and neutral projections for child completion envelopes."""

# LLM: This module is the neutral schema boundary shared by child publishers and
# conversation consumers. Changing a value requires updating both sides and the
# replay/compatibility tests; model prose never selects a schema version.
# 模块用途: 集中保存子代理完成交接包的协议版本，避免 Gateway 反向依赖子代理实现模块。

SUBAGENT_COMPLETION_SCHEMA_VERSION = "subagent-completion.v1"
CONVERSATION_SUBAGENT_COMPLETIONS_SCHEMA_VERSION = (
    "conversation-subagent-completions.v1"
)
DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS = 12


# LLM: Parent-facing completion context must be derived only from typed observation fields and
# exact root/direct-parent identities. Keep this projection shared by foreground and background
# consumers so a later continuation cannot lose or reinterpret a child result.
# 函数用途: 从完成观察账本生成一份有界直属子代理结果清单，并报告身份冲突而不猜测归属。
def subagent_completion_context_from_observations(
    observations: object,
    *,
    root_task_ids: set[str],
    workspace_task_id: str = "",
    visible_limit: int = DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
) -> tuple[dict[str, object], tuple[str, ...]]:
    selected_roots = {
        str(item).strip() for item in root_task_ids if str(item or "").strip()
    }
    if not selected_roots:
        return {}, ()
    latest_by_task: dict[str, tuple[float, dict[str, object]]] = {}
    issues: list[str] = []
    rows = observations if isinstance(observations, list | tuple) else ()
    for event in rows:
        projected, issue = _subagent_completion_item(event, selected_roots)
        if issue:
            issues.append(issue)
        if projected is None:
            continue
        task_id, observed_at, item = projected
        previous = latest_by_task.get(task_id)
        if previous is None or observed_at >= previous[0]:
            latest_by_task[task_id] = (observed_at, item)
    ordered = [
        item
        for _observed_at, item in sorted(
            latest_by_task.values(),
            key=lambda row: row[0],
        )
    ]
    limit = max(1, int(visible_limit or DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS))
    visible = ordered[-limit:]
    if not visible:
        return {}, tuple(issues)
    return {
        "schema": CONVERSATION_SUBAGENT_COMPLETIONS_SCHEMA_VERSION,
        "workspace_task_id": str(workspace_task_id or "").strip(),
        "completion_root_task_ids": sorted(
            {
                str(item.get("root_task_id") or "")
                for item in ordered
                if str(item.get("root_task_id") or "")
            }
        ),
        "total": len(ordered),
        "visible_count": len(visible),
        "omitted_count": max(0, len(ordered) - len(visible)),
        "items": visible,
    }, tuple(issues)


# LLM: Status and ownership come from the host event. Completion prose and refs are public
# integration evidence only; private runner JSON must never cross this neutral projection.
# 函数用途: 校验并净化单条完成观察，只允许 exact root 的直属孩子进入父代理上下文。
def _subagent_completion_item(
    event: object,
    root_task_ids: set[str],
) -> tuple[tuple[str, float, dict[str, object]] | None, str]:
    if str(getattr(event, "event_type", "") or "") != "subagent_runner_finished":
        return None, ""
    event_root_task_id = str(getattr(event, "root_task_id", "") or "").strip()
    if event_root_task_id not in root_task_ids:
        return None, ""
    if str(getattr(event, "parent_agent_id", "") or "").strip() != event_root_task_id:
        return None, ""
    metadata = getattr(event, "metadata", {})
    if not isinstance(metadata, dict):
        return None, ""
    if (
        str(metadata.get("completion_schema_version") or "")
        != SUBAGENT_COMPLETION_SCHEMA_VERSION
    ):
        return None, ""
    source_task_id = str(getattr(event, "source_agent_id", "") or "").strip()
    metadata_task_id = str(metadata.get("task_id") or "").strip()
    if source_task_id and metadata_task_id and source_task_id != metadata_task_id:
        return None, (
            "subagent completion identity mismatch: "
            f"source={source_task_id}, metadata={metadata_task_id}"
        )
    task_id = metadata_task_id or source_task_id
    if not task_id:
        return None, ""
    try:
        observed_at = float(getattr(event, "observed_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        observed_at = 0.0
    item: dict[str, object] = {
        "task_id": task_id,
        "root_task_id": event_root_task_id,
        "status": str(metadata.get("status") or ""),
        "turn_end_reason": str(metadata.get("turn_end_reason") or ""),
        "failure_type": str(metadata.get("failure_type") or ""),
        "completion_schema_version": SUBAGENT_COMPLETION_SCHEMA_VERSION,
        "completion_message": str(metadata.get("completion_message") or ""),
        "final_report_ref": str(metadata.get("final_report_ref") or ""),
        "declared_output_refs": _completion_refs(metadata.get("declared_output_refs")),
        "artifact_refs": _completion_refs(
            metadata.get("artifact_refs"),
            path_from_mapping=True,
        ),
        "observed_at": observed_at,
    }
    if metadata.get("completion_message_truncated") is True:
        item["completion_message_truncated"] = True
        item["completion_message_original_tokens"] = _nonnegative_int(
            metadata.get("completion_message_original_tokens")
        )
    return (task_id, observed_at, item), ""


# LLM: Output refs are open-world scalar identifiers. Legacy artifact mappings may contribute
# only their path; extra mapping fields and duplicate values remain private.
# 函数用途: 清洗完成信封里的路径或 URI 列表，每类最多保留二十个精确引用。
def _completion_refs(
    value: object,
    *,
    path_from_mapping: bool = False,
) -> list[str]:
    refs: list[str] = []
    for item in value if isinstance(value, list | tuple) else []:
        candidate = item.get("path") if path_from_mapping and isinstance(item, dict) else item
        ref = str(candidate or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= 20:
            break
    return refs


# LLM: Provider metadata may contain malformed legacy counters; the public completion envelope
# exposes a safe non-negative count without granting malformed prose any state authority.
# 函数用途: 把完成正文原始 token 数转成安全的非负整数。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

__all__ = [
    "CONVERSATION_SUBAGENT_COMPLETIONS_SCHEMA_VERSION",
    "DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS",
    "SUBAGENT_COMPLETION_SCHEMA_VERSION",
    "subagent_completion_context_from_observations",
]
