"""Versioned contracts and neutral projections for child completion envelopes."""

# LLM: 中性完成合同连接子代理发布方和前后台消费者；窗口字段与授权阶段连续失败收口事实只复制宿主事件的冻结事实，
# 不重新计时或改变终态。字段变更须同步活动回合、后台预算投影、递归父级快照与交接回归。
# 模块用途: 统一子代理交接协议和可见事实，避免各条消费链丢字段或反向依赖子代理实现。

SUBAGENT_COMPLETION_SCHEMA_VERSION = "subagent-completion.v1"
CONVERSATION_SUBAGENT_COMPLETIONS_SCHEMA_VERSION = (
    "conversation-subagent-completions.v1"
)
# 默认在结果里展示的已完成子代理条数；太多会把结果摘要撑长。
DEFAULT_VISIBLE_SUBAGENT_COMPLETION_COUNT = 12
# 子代理因同一错误码在授权阶段连续失败而收口时，随完成信封交给直属父级的结构化事实版本。
TOOL_FAILURE_HALT_SCHEMA_VERSION = "subagent-tool-failure-halt.v1"
# 收口事实只保留原因码、工具、错误码、阶段、次数和参数名；参数值与输出正文一律不进入事件。
_TOOL_FAILURE_HALT_TEXT_FIELDS = ("reason_code", "tool", "error_code", "failure_stage")
_TOOL_FAILURE_HALT_LIST_FIELDS = ("tools", "argument_names")
# 工具失败停机详情里单段文本的最大字符数；超长截断，保持回执可读。
_TOOL_FAILURE_HALT_TEXT_LIMIT_CHARS = 120
# 工具失败停机详情里最多列出的清单条目数；防止失败原因列表无限增长。
_TOOL_FAILURE_HALT_LIST_COUNT = 8


# LLM: Parent-facing completion context must be derived only from typed observation fields and
# exact root/direct-parent identities. Keep this projection shared by foreground and background
# consumers so a later continuation cannot lose or reinterpret a child result.
# 函数用途: 从完成观察账本生成一份有界直属子代理结果清单，并报告身份冲突而不猜测归属。
def subagent_completion_context_from_observations(
    observations: object,
    *,
    root_task_ids: set[str],
    workspace_task_id: str = "",
    visible_limit: int = DEFAULT_VISIBLE_SUBAGENT_COMPLETION_COUNT,
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
    limit = max(1, int(visible_limit or DEFAULT_VISIBLE_SUBAGENT_COMPLETION_COUNT))
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


# LLM: 状态、归属、未满声明窗口和授权阶段收口事实均来自宿主事件；正文不参与裁决，私有 runner JSON 不外露。
# 函数用途: 校验直属孩子身份后提取完成消息、冻结窗口与收口事实，不从摘要推断是否完成。
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
        **completion_service_window_facts(metadata),
        **completion_tool_failure_halt_facts(metadata),
    }
    if metadata.get("completion_message_truncated") is True:
        item["completion_message_truncated"] = True
        item["completion_message_original_tokens"] = _nonnegative_int(
            metadata.get("completion_message_original_tokens")
        )
    return (task_id, observed_at, item), ""


# LLM: 前后台投影共用此只读筛选；只接受既有宿主字段的原生类型，不回算时钟或修正状态。
# 整秒剩余量可以为零，因为发布端会将不足一秒的未满窗口取整；不能据此自动完成或重派。
# 函数用途: 成对保留有效的未满声明窗口及当时剩余秒数，缺失或损坏的数据不补造。
def completion_service_window_facts(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("service_window_incomplete") is not True:
        return {}
    remaining = value.get("service_window_remaining_seconds")
    if type(remaining) is not int or remaining < 0:
        return {}
    return {
        "service_window_incomplete": True,
        "service_window_remaining_seconds": remaining,
    }


# LLM: 前台活动回合、后台完成清单和递归父级快照共用这一份有界投影；只接受当前版本、正整数次数和短文本，
#   参数值、输出正文、路径值一律不复制。宿主状态仍是完成权威，本字段不改终态、不触发重派。
# 函数用途: 从完成信封里取出“同一错误码在授权阶段连续失败而收口”的结构化事实，缺失或损坏时返回空。
def completion_tool_failure_halt_facts(value: object) -> dict[str, object]:
    halt = value.get("tool_failure_halt") if isinstance(value, dict) else None
    if not isinstance(halt, dict) or halt.get("schema_version") != TOOL_FAILURE_HALT_SCHEMA_VERSION:
        return {}
    count = halt.get("consecutive_failures")
    if type(count) is not int or count <= 0:
        return {}
    projected: dict[str, object] = {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION,
        "consecutive_failures": count,
    }
    for key in _TOOL_FAILURE_HALT_TEXT_FIELDS:
        projected[key] = str(halt.get(key) or "").strip()[:_TOOL_FAILURE_HALT_TEXT_LIMIT_CHARS]
    for key in _TOOL_FAILURE_HALT_LIST_FIELDS:
        projected[key] = _bounded_names(halt.get(key))
    return {"tool_failure_halt": projected}


# LLM: 名称列表只保留去重后的短字符串，最多八个；非列表输入视为空，不从字符串里切分。
# 函数用途: 把工具名或参数名列表收成有界的干净列表。
def _bounded_names(value: object) -> list[str]:
    names: list[str] = []
    for item in value if isinstance(value, list | tuple) else ():
        name = str(item or "").strip()[:_TOOL_FAILURE_HALT_TEXT_LIMIT_CHARS]
        if name and name not in names:
            names.append(name)
        if len(names) >= _TOOL_FAILURE_HALT_LIST_COUNT:
            break
    return names


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
    "DEFAULT_VISIBLE_SUBAGENT_COMPLETION_COUNT",
    "SUBAGENT_COMPLETION_SCHEMA_VERSION",
    "TOOL_FAILURE_HALT_SCHEMA_VERSION",
    "completion_service_window_facts",
    "completion_tool_failure_halt_facts",
    "subagent_completion_context_from_observations",
]
