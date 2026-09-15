# LLM: 创建回执只投影异步启动、可信 cwd 和结果引用；内部运行根不代表业务文件目录。
# 模块用途: 组装子代理派工条目和父级回执，不替父级决定分工或等待时机。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...action_protocol import subagent_schedule_envelope_from_payload
from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...contracts.idempotency import idempotency_key, operation_id
from ...model_visible_refs import current_model_ref, current_model_ref_list, current_model_text
from ...subagents.context_bundle_refs import execution_cwd
from ...subagents.role_templates import role_template_snapshot_for_task
from ..runner.ref_fields import (
    _file_refs_from_value,
    _normalize_file_ref,
    params_output_refs,
)
from .child_result_index import child_result_index
from .coordinator_policy import coordinator_parent_execution_scope
from .create_constraints import created_tasks, dispatchable_tasks, reused_tasks
from .dispatch.state_contract import dispatch_state_contract_payload
from .finding_relation import finding_investigation_payload


@dataclass(frozen=True)
class CreateSubagentsPayloadInput:
    agent: object
    resolutions: list
    allowed_tools: object
    request_params: dict[str, object]
    auto_start: dict[str, object] | None = None
    replacement_records: list[dict[str, object]] | None = None
    conversation_bind_errors: list[dict[str, object]] | None = None


# LLM: 创建回执只能陈述已记录、已启动和待事件的结构化事实；不得附带模型
# 查询/推进工具调用，否则 provider 会把回执当下一条操作指令反复轮询。
# 函数用途: 汇总一批创建或复用的子代理，并生成父级可读的启动回执。
def create_subagents_payload(request: CreateSubagentsPayloadInput) -> dict[str, object]:
    agent = request.agent
    resolutions = request.resolutions
    auto_start = request.auto_start
    request_params = request.request_params
    tasks = [item.task for item in resolutions]
    created = created_tasks(resolutions)
    reused = reused_tasks(resolutions)
    dispatchable = dispatchable_tasks(tasks)
    pending_start = _pending_start_tasks(dispatchable, request_params, auto_start)
    result_index = child_result_index(agent, tasks)
    payload: dict[str, object] = {
        "created": len(created),
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "pending_start_run_ids": [task.id for task in pending_start],
        "auto_start": _auto_start_payload(auto_start),
        "next_action": _next_action(dispatchable, request_params, auto_start),
        "allowed_tools": request.allowed_tools or "automatic",
        "operation_contract": _operation_contract(request_params, created, reused, pending_start),
        "replacement_records": request.replacement_records or [],
        "conversation_bind_errors": request.conversation_bind_errors or [],
        "scheduling_advice": _scheduling_advice(tasks, request_params, auto_start),
        "child_result_index": result_index,
        "child_output_read_order": _child_output_read_order(result_index),
        "parent_execution_scope": coordinator_parent_execution_scope(),
        "tasks": [_task_payload(task) for task in tasks],
    }
    payload.update(dispatch_state_contract_payload(agent))
    payload["schedule_lifecycle"] = _schedule_lifecycle_payload(
        tasks,
        auto_start,
        payload.get("current_turn_run_state"),
    )
    investigations = finding_investigation_payload(
        tasks,
        payload["schedule_lifecycle"],
    )
    if investigations:
        payload["finding_investigations"] = investigations
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(payload, tool="create_subagents").to_dict()
    return payload


def _auto_start_payload(auto_start: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(auto_start, dict):
        return {"status": "not_attempted"}
    allowed = {
        "status",
        "dispatch_mode",
        "run_ids",
        "deferred_run_ids",
        "started_run_ids",
        "failed_run_ids",
        "warnings",
    }
    payload = {key: auto_start[key] for key in allowed if key in auto_start}
    if str(payload.get("status") or "") == "started":
        payload["start_status"] = "accepted"
    return payload or {"status": str(auto_start.get("status") or "unknown")}


def _schedule_lifecycle_payload(
    tasks: list[object],
    auto_start: dict[str, object] | None,
    current_state: object,
) -> dict[str, object]:
    """区分任务记录、调度接收与 runner 运行，禁止用一个 started 混称三层事实。"""
    state = current_state if isinstance(current_state, dict) else {}
    task_ids = [_task_text(task, "id") for task in tasks if _task_text(task, "id")]
    raw_status = str((auto_start or {}).get("status") or "not_attempted")
    accepted = (
        _string_items((auto_start or {}).get("run_ids"))
        if raw_status in {"started", "accepted"}
        else []
    )
    running = [
        run_id
        for run_id in _string_items(state.get("running_run_ids"))
        if run_id in task_ids
    ]
    failed = list(
        dict.fromkeys(
            [
                *_string_items((auto_start or {}).get("failed_run_ids")),
                *_string_items(state.get("blocked_run_ids")),
            ]
        )
    )
    if failed and accepted:
        start_status = "partially_started"
    elif failed:
        start_status = "failed"
    elif accepted:
        start_status = "accepted"
    elif raw_status == "deferred":
        start_status = "deferred"
    else:
        start_status = "not_started"
    return {
        "requested_count": len(task_ids),
        "recorded_run_ids": task_ids,
        "start_accepted_run_ids": accepted,
        "running_run_ids": running,
        "failed_run_ids": failed,
        "start_status": start_status,
        "counts": {
            "recorded": len(task_ids),
            "start_accepted": len(accepted),
            "running": len(running),
            "failed": len(failed),
        },
        "authority": {
            "recorded": "subagent_store",
            "start_accepted": "background_start_receipt",
            "running": "task_state_machine",
        },
    }


def _pending_start_tasks(
    tasks: list,
    request_params: dict[str, object],
    auto_start: dict[str, object] | None,
) -> list:
    if bool(request_params.get("defer_start")):
        return tasks
    deferred = set(_string_items((auto_start or {}).get("deferred_run_ids")))
    if deferred:
        return [task for task in tasks if _task_text(task, "id") in deferred]
    if (auto_start or {}).get("status") in {"started", "not_needed"}:
        return []
    return tasks


def _operation_contract(
    request_params: dict[str, object],
    created: list,
    reused: list,
    pending_start: list,
) -> dict[str, object]:
    payload = {"params": request_params}
    return {
        "contract": "idempotency.v1",
        "operation": "create_subagents",
        "idempotency_key": idempotency_key("create_subagents", payload),
        "operation_id": operation_id("create_subagents", payload),
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "pending_start_run_ids": [task.id for task in pending_start],
    }


# LLM: 回执只报告异步启动与事件交付，不强迫结束当前回合或轮询；延迟创建仍读显式状态。
# 函数用途: 让父级继续不冲突的工作，需要孩子结果时才自然让出等待。
def _next_action(
    tasks,
    request_params: dict[str, object],
    auto_start: dict[str, object] | None = None,
) -> dict[str, object]:
    run_ids = [task.id for task in tasks]
    if not run_ids:
        return {
            "action": "report_creation_state",
            "run_ids": [],
            "reason": "create_subagents 没有可启动的新 run；按本回执事实处理即可。",
        }
    if bool(request_params.get("defer_start")):
        return {
            "action": "await_dependency_event",
            "run_ids": run_ids,
            "reason": "这是底层显式延迟创建；运行时会在依赖满足后自动恢复，不需要模型推进。",
        }
    deferred = _string_items((auto_start or {}).get("deferred_run_ids"))
    if deferred:
        return {
            "action": "await_lifecycle_event",
            "run_ids": run_ids,
            "reason": "部分子代理已自动启动；底层显式延迟的 run 会留在 pending_start_run_ids，等依赖满足后由系统启动。",
        }
    if (auto_start or {}).get("status") == "started":
        return {
            "action": "continue_independent_work",
            "run_ids": run_ids,
            "immediate_control_tools": ["send_guidance", "cancel_subagents"],
            "reason": (
                "本批子代理已在后台运行。你可以继续自己的独立工作、使用已完成结果或给直属孩子追加要求。"
                "需要的结果尚未到达且没有其他有用工作时，再自然结束本轮等待事件。"
                "孩子的结果或异常会交给直接父级，不必等全部兄弟完成；不要反复轮询或重复它们的工作。"
            ),
        }
    return {
        "action": "await_lifecycle_event",
        "run_ids": run_ids,
        "reason": "自动启动未完成；底层恢复器会处理可恢复故障，模型只需等生命周期事件或向用户说明真实阻塞。",
    }


# LLM: task_root 保留为内部运行定位符；普通相对文件只相对于同一宿主 execution_cwd。
# 函数用途: 明确告诉父级孩子实际在哪工作，避免拿内部目录拼业务文件路径。
def _task_payload(task: object) -> dict[str, object]:
    return {
        "id": _task_text(task, "id"),
        "goal": current_model_text(_task_text(task, "goal")),
        "status": _task_text(task, "status"),
        "task_root": current_model_ref(_task_text(task, "task_workspace_dir")),
        "task_root_role": "internal_run_state",
        "execution_cwd": execution_cwd(task),
        "attributes": _task_attributes(task),
    }


def _child_output_read_order(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        result.append(
            {
                "run_id": str(row.get("run_id") or ""),
                "agent_name": str(row.get("agent_name") or ""),
                "role": str(row.get("role") or ""),
                "status": str(row.get("status") or ""),
                "expected_outputs": list(row.get("expected_outputs") or []),
                "read_order": list(row.get("read_order") or []),
            }
        )
    return result


def _task_text(task: object, field: str) -> str:
    value = getattr(task, field, "")
    return value if isinstance(value, str) else ""


def _task_attributes(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return {}
    return {
        str(key): projected
        for key, value in attrs.items()
        if (projected := _task_attribute_value(str(key), value)) not in ("", [], None)
    }


def _task_attribute_value(key: str, value: object) -> object:
    ref_keys = {"input_refs", "output_refs", "output_files", "artifact_refs", "required_read_paths"}
    if key in ref_keys:
        return current_model_ref_list(value)
    if isinstance(value, str):
        return current_model_text(value)
    return value


# LLM: Quality-role ordering is advisory only; never expose hidden defer controls to the model.
# 函数用途: 提醒父代理等前置产物出现后再创建依赖型下级，不替模型设置启动门。
def _scheduling_advice(tasks: list, request_params: dict[str, object], auto_start: dict[str, object] | None) -> list[dict[str, object]]:
    del request_params
    advice: list[dict[str, object]] = []
    quality_tasks = [task for task in tasks if _is_dependent_quality_role(task) and _task_text(task, "id")]
    deferred = set(_string_items((auto_start or {}).get("deferred_run_ids")))
    early = [task for task in quality_tasks if _task_text(task, "id") not in deferred]
    if early:
        advice.append(
            {
                "code": "dependent_quality_task_started_early",
                "run_ids": [_task_text(task, "id") for task in early],
                "message": "测试、找错、验收、汇总这类任务通常依赖前置产物；如果产物还没出来，应等前置产物 refs 出现后再创建这些 child。",
            }
        )
    return advice


def _is_dependent_quality_role(task: object) -> bool:
    snapshot = role_template_snapshot_for_task(task)
    return bool(snapshot.get("depends_on_outputs"))


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


_BASE_FIELDS_EXCLUDED_FROM_ITEM = {
    "items",
    "goal",
    # 顶层 description 描述整批派工，不能扇出成每个 child 的同一句职责。
    # 每个 item 自己的 description 才是该 child 的展示短标题；省略时只读
    # 活动投影会退回该 item 的 goal 开头。
    "description",
    "plan",
    "context_manifest",
    # covers 是"该子代理负责哪些清单项"的逐个绑定;顶层值若扇出到每个 item,任何一个
    # 子代理 DONE 都会把全部绑定项打勾(假 credit)。batch 模式各 item 自带 covers。
    "covers",
    # Top-level delivery targets belong to the parent/root output. In batch mode
    # each child keeps its own explicit output refs; shared files are coordinated
    # by the parent prompt and task tree instead of a hidden create-time gate.
    "output_files",
    "output_refs",
    "artifact_refs",
    "required_output_files",
    "required_output_refs",
    "deliverables",
    "final_output",
    "final_output_path",
}
_SHARED_DIRECTIVE_ROLES = frozenset(
    {
        "primary_directive",
        "directive",
        "instruction",
        "brief",
    }
)


@dataclass(frozen=True)
class CreateSubagentItem:
    goal: str
    params: dict[str, object]


def create_items_from_params(params: dict[str, object]) -> list[CreateSubagentItem] | str:
    protocol_error = _batch_protocol_error(params)
    if protocol_error:
        return protocol_error
    raw_items = params.get("items")
    if raw_items is None:
        return []
    items = _json_list_param(raw_items)
    if not items:
        # items 显式传了但为空(模型很常反射性带一个 items:[] )——当作"没传 items",
        # 落到单 goal 模式用顶层 goal,而不是硬拒报错。真机实测:模型把单个子代理规格放顶层
        # (goal/output_files)却带空 items,旧逻辑直接 TOOL_INVALID_ARGUMENTS,导致子代理一个
        # 都派不出去、主代理只能退回独自写。下游若连 goal 也没有,_prepare_single_mode 会给
        # "缺少必填参数 goal" 的干净报错。
        return []
    parsed: list[CreateSubagentItem] = []
    # index 用 0 基下标:错误文案里 items[N] 是方括号数组写法,模型按 JSON 下标理解;
    # 旧的 1 基编号(items[2]=第2个)会让模型自纠时改错对象(真机空 goal 自纠场景)。
    for index, raw in enumerate(items):
        item = _create_item(params, raw, index)
        if isinstance(item, str):
            return item
        parsed.append(item)
    return parsed


def _batch_protocol_error(params: dict[str, object]) -> str:
    if "tasks" in params:
        return "create_subagents 批量派工只接受 items；请把 tasks 改成 items。"
    for key in ("replaces_run_ids", "supersedes_run_ids"):
        if key in params:
            return f"create_subagents 接管关系只接受 replacement_for_run_ids；请移除 {key}。"
    raw_items = params.get("items")
    item_error = _item_protocol_error(raw_items)
    if item_error:
        return item_error
    return ""


def _item_protocol_error(raw_items: object) -> str:
    items = _json_list_param(raw_items) if raw_items is not None else []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        bad_key = _unsupported_replacement_key(item)
        if bad_key:
            return f"items[{index}] 接管关系只接受 replacement_for_run_ids；请移除 {bad_key}。"
    return ""


def _unsupported_replacement_key(params: dict[str, object]) -> str:
    for key in ("replaces_run_ids", "supersedes_run_ids"):
        if key in params:
            return key
    return ""


def _create_item(
    base_params: dict[str, object],
    raw: object,
    index: int,
) -> CreateSubagentItem | str:
    if not isinstance(raw, dict):
        return f"items[{index}](第 {index + 1} 个)必须是 JSON 对象。整批未创建;修正后重发完整 items。"
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        # 空/全空白 goal 整批拒绝:一个空壳子代理落地就会 Context Gate BLOCKED 变僵尸
        # (真机 0/22 根因#1)。文案给自纠指引,别让模型失败即放弃退回独自写。
        return (
            f"items[{index}](第 {index + 1} 个)缺少必填 goal——每个 item 必须自带非空 goal,"
            "说清这个子代理具体做什么、产出什么。整批未创建,一个也没派出;补上 goal 后重发完整 items。"
        )
    merged = _create_item_params(base_params, raw, goal)
    return CreateSubagentItem(goal=goal, params=merged)


def _create_item_params(
    base_params: dict[str, object],
    raw: dict[str, object],
    goal: str,
) -> dict[str, object]:
    merged = _base_item_defaults(base_params)
    merged["_item_allowed_tools_explicit"] = "allowed_tools" in raw
    merged.update(raw)
    merged["goal"] = goal
    _merge_item_required_read_paths(merged, base_params, goal)
    return merged


# 不自动变成每个子代理的硬输入依赖，避免一个来源清单卡住所有 worker。
def _base_item_defaults(base_params: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in base_params.items()
        if key not in _BASE_FIELDS_EXCLUDED_FROM_ITEM
    }


# 顶层 required_read_paths 不自动复制到每个 worker，避免把一组输入清单误变成所有子代理的读提示。
def _merge_item_required_read_paths(
    merged: dict[str, object],
    base_params: dict[str, object],
    goal: str,
) -> None:
    refs = _merge_refs(
        [
            _shared_directive_pack_paths(base_params.get("context_packs")),
            string_list(merged.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS),
            _item_manifest_required_read_paths(merged.get("context_manifest")),
            _existing_goal_file_refs(goal, output_refs=params_output_refs(merged)),
        ]
    )
    if refs:
        merged["required_read_paths"] = refs


def _item_manifest_required_read_paths(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return string_list(value.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS)


def _shared_directive_pack_paths(value: object) -> list[str]:
    packs = _context_pack_list(value)
    paths: list[str] = []
    for pack in packs:
        role = str(pack.get("role") or pack.get("kind") or "").strip().lower()
        if role not in _SHARED_DIRECTIVE_ROLES:
            continue
        paths.extend(string_list(pack.get("path") or pack.get("ref"), TOOL_TEXT_LIST_OPTIONS))
    return paths


def _existing_goal_file_refs(goal: str, *, output_refs: list[str]) -> list[str]:
    refs = []
    for ref in _file_refs_from_value(goal):
        normalized = _normalize_file_ref(ref)
        if not normalized or _ref_matches_any_output(normalized, output_refs):
            continue
        if _ref_exists_now(normalized):
            refs.append(normalized)
    return refs


def _context_pack_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _merge_refs(groups: list[list[str]]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            _append_merged_ref(merged, seen, item)
    return merged


def _append_merged_ref(merged: list[str], seen: set[str], item: object) -> None:
    normalized = _normalize_file_ref(item)
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    merged.append(normalized)


def _ref_exists_now(ref: str) -> bool:
    path = Path(ref).expanduser()
    if path.is_absolute():
        return path.exists()
    return Path(ref).exists()


def _ref_matches_any_output(ref: str, output_refs: list[str]) -> bool:
    return any(_path_ref_matches(ref, output_ref) for output_ref in output_refs)


def _path_ref_matches(left: str, right: str) -> bool:
    left_text = str(left or "").strip().replace("\\", "/")
    right_text = str(right or "").strip().replace("\\", "/")
    if not left_text or not right_text:
        return False
    return (
        left_text == right_text
        or left_text.endswith("/" + right_text)
        or right_text.endswith("/" + left_text)
    )


def _json_list_param(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []
