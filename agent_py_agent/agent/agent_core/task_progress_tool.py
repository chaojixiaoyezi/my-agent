
from __future__ import annotations

"""LLM: Expose task progress as an advisory ledger, never a lifecycle gate.

模块用途: 让模型记录和读取软进度，同时用结构化状态同步已结束的子代理条目。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..task_progress import (
    invalid_coverage_statuses,
    invalid_item_statuses,
    read_task_progress,
    task_progress_display_identity,
    task_progress_display_items,
    task_progress_status_is_closed,
    with_task_progress_display_plan,
    write_task_progress,
)
from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolRuntimePolicy,
)
from .orchestration.dispatch_progress_seed import (
    reconcile_completed_child_covers,
    reconcile_completed_child_items,
)
from .orchestration.tool_specs import build_task_progress_model_spec
from .runner.context import current_subagent_run_id
from .runtime.owner_roots import runtime_owner_root
from .runtime.task_identity import (
    durable_task_id,
    progress_display_generation_id,
    progress_ledger_id,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: This model tool owns only progress-ledger validation/projection. It must
# never schedule another model turn or decide whether a task is complete.
# 类用途: 提供软进度清单的读写入口，不负责催办、续跑或验收。
class TaskProgressTool(BaseTool):
    model_spec = build_task_progress_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "read_only",
            by_parameter=(
                ("action", (("", "read_only"), ("read", "read_only"), ("update", "mutating"), ("create", "mutating"))),
            ),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        # seq 253 闭合：run_id 是逻辑 ID（run 标识）不是路径。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("run_id",),
            parameter_kinds={"run_id": "logical"},
        ),
        input_policy=ToolInputPolicy(internal_parameters=("__run_scope",)),
    )

    # LLM: Keep the agent reference only for canonical owner/run scope lookup.
    # 函数用途: 绑定当前代理，以便进度账本落到正确的 owner 和 run。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: Writes promote the conversation task before resolving its ledger,
    # then project canonical child state so stale input cannot hide a DONE child.
    # 函数用途: 在稳定任务账本中读写软进度，并把真实子代理终态同步到返回结果。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = _normalized_action(params.get("action"))
        if action_error := _invalid_action_result(action):
            return action_error
        if field_error := _invalid_action_fields_result(action, params):
            return field_error
        if action == "update":
            # 先晋升再选账本 key：否则首条 Todo 会写 request id，后续派工却写
            # task-path 指纹，同一任务会分裂成两本清单。
            from ..conversation.task_promotion import promote_current_conversation_task

            promote_current_conversation_task(self.agent)
        run_id = _target_run_id(self.agent, params, allow_explicit=action == "read")
        if not run_id:
            run_id = "main"
        root = runtime_owner_root(self.agent)
        if action == "update":
            if status_error := _invalid_status_result(params):
                return status_error
            if identity_error := _invalid_new_item_identity_result(root, run_id, params):
                return identity_error
            written_payload = write_task_progress(
                root,
                run_id,
                with_task_progress_display_plan(
                    params,
                    generation_id=progress_display_generation_id(self.agent),
                    item_ids=_updated_item_ids(params),
                ),
            )
            _reconcile_completed_child_covers_before_read(self.agent, root, run_id)
            reconcile_completed_child_items(self.agent, root, run_id)
            payload = read_task_progress(root, run_id)
            payload = _with_immediate_quality_hints(payload, written_payload)
            payload = _with_write_feedback(payload)
            payload = _with_evidence_source_feedback(self.agent, payload)
        else:
            _reconcile_completed_child_covers_before_read(self.agent, root, run_id)
            reconcile_completed_child_items(self.agent, root, run_id)
            payload = read_task_progress(root, run_id)
        payload = _with_execution_guidance(payload)
        display_items = task_progress_display_items(payload)
        generation_id, plan_revision = task_progress_display_identity(payload)
        model_payload = dict(payload)
        model_payload.pop("display_plan", None)
        return ToolHandlerOutcome(
            "task_progress",
            True,
            json.dumps(model_payload, ensure_ascii=False, indent=2),
            result_envelope={
                "task_progress_projection": {
                    "generation_id": generation_id,
                    "plan_revision": plan_revision,
                    "items": [
                        {
                            "id": str(item.get("id") or ""),
                            "title": str(item.get("title") or ""),
                            "status": str(item.get("status") or "pending"),
                        }
                        for item in display_items[:128]
                        if isinstance(item, dict)
                    ],
                }
            },
        )


# LLM: The model may update a subset of the durable ledger in one turn. Only
# exact item ids from that structured call seed the display plan; summaries,
# titles and prose never select rows.
# 函数用途: 取出本次 task_progress 写入明确涉及的待办 ID。
def _updated_item_ids(params: dict[str, object]) -> list[str]:
    items = params.get("items")
    values = items if isinstance(items, list | tuple) else ()
    return list(
        dict.fromkeys(
            str(item.get("id") or "").strip()
            for item in values
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        )
    )


# LLM: Open progress remains advisory, but the model needs the same 会话运行时
# persistence reminder after a read that exposed unfinished work. This response
# field cannot schedule turns, accept work, or infer task quality.
# 函数用途: 在进度工具结果里附一条软续做提示和可供 covers 使用的 exact id，不改清单或任务终态。
def _with_execution_guidance(payload: dict[str, object]) -> dict[str, object]:
    open_ids: list[str] = []
    for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
        if not isinstance(item, dict) or task_progress_status_is_closed(item.get("status")):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id:
            open_ids.append(item_id)
    coverage = payload.get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or task_progress_status_is_closed(target.get("status")):
            continue
        target_id = str(target.get("id") or "").strip()
        if target_id:
            open_ids.append(target_id)
    open_ids = list(dict.fromkeys(open_ids))
    if not open_ids:
        return payload
    guidance = {
        "schema_version": "task-progress-execution-guidance.v2",
        "severity": "soft",
        "blocking": False,
        "open_count": len(open_ids),
        "open_item_ids": open_ids[:24],
        "delegation_contract": _progress_delegation_contract(),
        "message": (
            "这些 exact id 仍是未完成计划，不是宿主完成判定。已有子代理负责时等待其 typed 生命周期事件；"
            "否则继续使用工具；若下级确实原样承接某项，应把对应 exact id 复制到 "
            "create_subagents.items[].covers。只有额外工作或关系不能确定时才省略；未绑定 child 完成后，"
            "父级仍要按已有 exact id 更新被证实完成的计划项。不能拿无关 open id 顶替；"
            "返工已关闭项时先对原 id 传 status=in_progress 和 "
            "correction=true。只要当前仍能推进，不要用列出未完成项代替继续工作，也不要重复创建同义清单。"
        ),
    }
    return {**payload, "execution_guidance": guidance}


# LLM: The delegation hint is a typed soft contract shared by every open-plan
# result; it suggests exact ids but never schedules, binds, or accepts work.
# 函数用途: 生成 Todo 与子代理之间的精确绑定及漏绑后收尾说明。
def _progress_delegation_contract() -> dict[str, object]:
    return {
        "tool": "create_subagents",
        "same_work_field": "items[].covers",
        "same_work_value_source": "open_item_ids",
        "same_work_rule": "copy_exact_id",
        "independent_or_uncertain_rule": "omit_covers",
        "unbound_completion_followup": {
            "tool": "task_progress",
            "action": "update",
            "identity_field": "items[].id",
            "matching": "exact_id_only",
        },
    }


# LLM: Canonical rereads intentionally discard ephemeral incoming-write hints.
# Reattach only that soft response metadata while keeping items/counts canonical.
# 函数用途: 对账后仍把本次写入产生的即时软提醒返回给模型。
def _with_immediate_quality_hints(
    canonical: dict[str, object],
    written: dict[str, object],
) -> dict[str, object]:
    hints = written.get("quality_hints")
    if not isinstance(hints, dict) or not hints.get("messages"):
        return canonical
    return {**canonical, "quality_hints": hints}


# LLM: New progress rows need stable machine identity and a human-readable label;
# partial updates may omit title only when that exact id already exists.
# 函数用途: 拒绝模型一次写入多条空白待办，同时保留已有项按 id 更新的能力。
def _invalid_new_item_identity_result(
    root: Path,
    run_id: str,
    params: dict[str, object],
) -> ToolHandlerOutcome | None:
    items = params.get("items")
    if not isinstance(items, list | tuple):
        return None
    existing_ids = {
        str(item.get("id") or "").strip()
        for item in read_task_progress(root, run_id).get("items", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    invalid: list[dict[str, object]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            invalid.append({"index": index, "reason": "item_must_be_object"})
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            invalid.append({"index": index, "reason": "id_required"})
            continue
        if item_id not in existing_ids and not str(item.get("title") or "").strip():
            invalid.append(
                {"index": index, "id": item_id, "reason": "new_item_title_required"}
            )
    if not invalid:
        return None
    payload = {
        "ok": False,
        "error": "task_progress 新进度项必须同时有稳定 id 和可读 title。",
        "invalid_items": invalid[:12],
        "how_to_fix": "新建项传 id/title/status；更新已有项可传 id/status 和要更新的字段。",
    }
    return ToolHandlerOutcome(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="not_started",
    )


def _reconcile_completed_child_covers_before_read(
    agent: object,
    root: Path,
    run_id: str,
) -> None:
    if current_subagent_run_id(agent):
        return
    params = getattr(agent, "_current_run_params", None)
    if params is None or progress_ledger_id(agent, params) != run_id:
        return
    reconcile_completed_child_covers(agent, root, run_id)


def _invalid_status_result(params: dict[str, object]) -> ToolHandlerOutcome | None:
    invalid = invalid_item_statuses(params)
    invalid_coverage = invalid_coverage_statuses(params)
    if not invalid and not invalid_coverage:
        return None
    payload = {
        "ok": False,
        "error": "task_progress status fields must be one of pending/in_progress/done/skipped/blocked.",
        "invalid_statuses": invalid[:12],
        "invalid_coverage_statuses": invalid_coverage[:12],
        "allowed_statuses": ["pending", "in_progress", "done", "skipped", "blocked"],
        "how_to_fix": "Move labels such as completed/read/ok into notes or summary, and use status=done when the item/check is complete.",
    }
    return ToolHandlerOutcome(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="not_started",
    )


def _normalized_action(value: object) -> str:
    action = str(value or "read").strip()
    # S-C1 延伸：MiniMax 用 create 建清单（账本不存在时 update 本就自动创建），
    # 归一为 update，与 schema 枚举（read/update/create）一致。
    if action == "create":
        return "update"
    return action if action in {"read", "update"} else action or "read"


def _invalid_action_result(action: str) -> ToolHandlerOutcome | None:
    if action in {"read", "update"}:
        return None
    payload = {
        "ok": False,
        "error": "task_progress action must be exactly read or update.",
        "invalid_action": action,
        "allowed_actions": ["read", "update"],
    }
    return ToolHandlerOutcome(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="not_started",
    )


# LLM: Each action accepts only its own fields; task progress never controls conversation
# selection or lifecycle.
# 函数用途: 拒绝与读写进度无关的参数，避免清单工具悄悄改变会话或工作目录。
def _invalid_action_fields_result(
    action: str,
    params: dict[str, object],
) -> ToolHandlerOutcome | None:
    action_fields = {
        "read": frozenset({"action", "run_id"}),
        "update": frozenset({"action", "summary", "next_action", "items", "coverage"}),
    }
    protocol_fields = frozenset(
        {
            "__run_scope",
            "__tool_call_id",
            "artifact_refs",
            "call_id",
            "idempotency_key",
            "kind",
            "metadata",
            "operation_id",
            "schema_version",
            "tool",
        }
    )
    invalid = sorted(set(params) - action_fields[action] - protocol_fields)
    if not invalid:
        return None
    payload = {
        "ok": False,
        "error": "task_progress fields must match the selected action.",
        "action": action,
        "invalid_fields": invalid,
        "allowed_fields": sorted(action_fields[action] - {"action"}),
        "how_to_fix": "Use action=read to inspect progress or action=update to record progress.",
    }
    return ToolHandlerOutcome(
        "task_progress",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="not_started",
    )

# LLM: An explicit read target that equals the current structured task identity
# is only an alias for this turn's canonical ledger. Keep true cross-run reads
# exact, and never infer aliases from prompt text or identifier prefixes.
# 函数用途: 解析进度账本编号；当前任务编号自动归一到唯一账本，其他历史编号仍按原值读取。
def _target_run_id(agent: object, params: dict[str, object], *, allow_explicit: bool) -> str:
    """账本键解析（与派工 seed 和任务工作区使用同一份任务身份）。
    唯一特殊分支=【后台唤醒轮】(_current_run_params.source=="background_main_agent"):
    其 run_id 是新的(bg-main-*),task_id 仍是主任务——账本按【任务】延续,否则派工 seed
    立的账在唤醒轮里读写不到、模型只能另立新账。其余场景原链不动（子代理按自己 run 隔离）。"""
    current = getattr(agent, "_current_run_params", None)
    explicit = str(params.get("run_id") or "").strip()
    scoped = _scope_run_id(params.get("__run_scope"))
    if explicit and allow_explicit:
        # 会话运行时 的 update_plan 由 session+turn 直接定域，不让模型另猜当前计划的
        # 存储键。这里仍保留查看其他历史 run 的扩展能力，但模型把当前 task_id
        # 原样传回来时，它只是当前账本的别名，必须经过同一 canonical resolver。
        if current is not None and explicit == durable_task_id(current):
            resolved = progress_ledger_id(
                agent,
                current,
                scoped_id=scoped or current_subagent_run_id(agent),
            )
            return resolved or explicit
        return explicit
    if current is None:
        return str(
            scoped
            or current_subagent_run_id(agent)
            or getattr(agent, "_main_agent_run_id", "")
            or getattr(agent, "_current_request_id", "")
            or "main"
        ).strip()
    resolved = progress_ledger_id(agent, current, scoped_id=scoped)
    return resolved or str(current_subagent_run_id(agent) or "main").strip()


def _scope_run_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("task_id") or value.get("request_id") or "").strip()


def _with_write_feedback(payload: dict[str, object]) -> dict[str, object]:
    hints = payload.get("quality_hints")
    if not isinstance(hints, dict) or not hints.get("messages"):
        return payload
    missing = _missing_evidence_ids(hints)
    feedback = {
        "severity": "soft",
        "blocking": False,
        "message": str(hints.get("soft_prompt") or "软提醒：有些进度项缺少证据，建议补上文件、来源或产物引用。"),
        "missing_evidence_item_ids": missing,
        "next_suggestions": list(hints.get("next_suggestions") or []),
    }
    return {**payload, "soft_feedback": feedback}


def _with_evidence_source_feedback(agent: object, payload: dict[str, object]) -> dict[str, object]:
    archive_calls = _current_archive_tool_calls(agent)
    if not archive_calls:
        return payload
    refs = _progress_evidence_refs(payload)
    local_refs = [ref for ref in refs if _looks_like_local_ref(ref)]
    if not local_refs:
        return payload
    successful, failed = _tool_path_index(archive_calls)
    failed_refs = [ref for ref in local_refs if _matches_any_ref(agent, ref, failed)]
    unseen_refs = [
        ref
        for ref in local_refs
        if ref not in failed_refs and not _matches_any_ref(agent, ref, successful)
    ]
    if not failed_refs and not unseen_refs:
        return payload
    feedback = dict(payload.get("soft_feedback") if isinstance(payload.get("soft_feedback"), dict) else {})
    feedback.setdefault("severity", "soft")
    feedback.setdefault("blocking", False)
    message = str(feedback.get("message") or "").strip()
    source_message = _evidence_source_message(failed_refs, unseen_refs)
    feedback["message"] = f"{message} {source_message}".strip() if message else source_message
    suggestions = list(feedback.get("next_suggestions") if isinstance(feedback.get("next_suggestions"), list) else [])
    suggestions.append("把失败或未确认的证据路径重新用 read_file/list_files 确认后，再把它写成结论。")
    feedback["next_suggestions"] = _dedupe_texts(suggestions)
    feedback["evidence_source_warnings"] = {
        "failed_refs": failed_refs[:12],
        "unseen_refs": unseen_refs[:12],
        "failed_count": len(failed_refs),
        "unseen_count": len(unseen_refs),
    }
    hints = dict(payload.get("quality_hints") if isinstance(payload.get("quality_hints"), dict) else {})
    messages = list(hints.get("messages") if isinstance(hints.get("messages"), list) else [])
    messages.append(source_message)
    hints["messages"] = _dedupe_texts(messages)
    hints["severity"] = "soft"
    hints["evidence_source_warning_count"] = len(failed_refs) + len(unseen_refs)
    if failed_refs:
        hints["evidence_failed_refs"] = failed_refs[:12]
    if unseen_refs:
        hints["evidence_unseen_refs"] = unseen_refs[:12]
    return {**payload, "quality_hints": hints, "soft_feedback": feedback}


def _current_archive_tool_calls(agent: object) -> list[dict[str, object]]:
    params = getattr(agent, "_current_tool_loop_params", None)
    records = getattr(params, "archive_tool_calls", None)
    if not isinstance(records, list):
        return []
    return [dict(item) for item in records if isinstance(item, dict)]


def _progress_evidence_refs(payload: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for item in _dict_items(payload.get("items")):
        refs.extend(_string_items(item.get("evidence")))
    coverage = payload.get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else []
    for target in _dict_items(targets):
        refs.extend(_string_items(target.get("evidence")))
    return _dedupe_texts(refs)


def _tool_path_index(archive_calls: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    successful: list[str] = []
    failed: list[str] = []
    for record in archive_calls:
        record_successful, record_failed = _classified_record_paths(record)
        successful.extend(record_successful)
        failed.extend(record_failed)
    return _dedupe_texts(successful), _dedupe_texts(failed)


def _classified_record_paths(record: dict[str, object]) -> tuple[list[str], list[str]]:
    paths = _record_path_refs(record)
    if not paths:
        return [], []
    if bool(record.get("ok")):
        return paths, []
    return ([], paths) if _path_failure(record) else ([], [])


def _record_path_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for source in (
        record,
        _mapping(record.get("parameters")),
        _mapping(record.get("tool_result_envelope")),
        _mapping(_mapping(record.get("tool_result_envelope")).get("output")),
    ):
        refs.extend(_path_values(source))
    tool_refs = record.get("tool_result_refs")
    if isinstance(tool_refs, list):
        for item in tool_refs:
            refs.extend(_path_values(_mapping(item)))
    return _dedupe_texts(refs)


def _path_values(source: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("path", "root", "artifact_ref", "source_ref", "target_path", "output_path", "uri", "url"):
        refs.extend(_string_items(source.get(key), allow_scalar=True))
    return refs


def _path_failure(record: dict[str, object]) -> bool:
    code = str(record.get("error_code") or "").strip()
    if code in {"PATH_NOT_FOUND", "PERMISSION_DENIED", "INVALID_PATH", "TOOL_INPUT_INVALID"}:
        return True
    envelope = record.get("tool_result_envelope")
    if isinstance(envelope, dict):
        return str(envelope.get("error_code") or "").strip() in {
            "PATH_NOT_FOUND",
            "PERMISSION_DENIED",
            "INVALID_PATH",
            "TOOL_INPUT_INVALID",
        }
    return False


def _matches_any_ref(agent: object, ref: str, candidates: list[str]) -> bool:
    return any(_same_ref(agent, ref, candidate) for candidate in candidates)


def _same_ref(agent: object, left: str, right: str) -> bool:
    left = _clean_ref(left)
    right = _clean_ref(right)
    if not left or not right:
        return False
    if left == right:
        return True
    if _has_scheme(left) or _has_scheme(right):
        return False
    left_path = _resolved_local_ref(agent, left)
    right_path = _resolved_local_ref(agent, right)
    if left_path == right_path:
        return True
    left_posix = left_path.as_posix()
    right_posix = right_path.as_posix()
    return left_posix.endswith(f"/{right}") or right_posix.endswith(f"/{left}")


def _resolved_local_ref(agent: object, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(getattr(agent, "root", ".")).expanduser() / path
    return path.resolve(strict=False)


def _clean_ref(value: str) -> str:
    text = str(value or "").strip()
    if not text or _has_scheme(text):
        return text
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return text


def _looks_like_local_ref(value: str) -> bool:
    text = _clean_ref(value)
    if not text or _has_scheme(text):
        return False
    return "/" in text or "\\" in text or "." in Path(text).name


def _has_scheme(value: str) -> bool:
    return "://" in value


def _evidence_source_message(failed_refs: list[str], unseen_refs: list[str]) -> str:
    parts: list[str] = []
    if failed_refs:
        parts.append(f"这些证据路径本轮读失败：{', '.join(failed_refs[:6])}")
    if unseen_refs:
        parts.append(f"这些证据路径本轮没有成功工具记录：{', '.join(unseen_refs[:6])}")
    return "软提醒：" + "；".join(parts) + "。"


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: object, *, allow_scalar: bool = False) -> list[str]:
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if allow_scalar:
        text = str(value or "").strip()
        return [text] if text else []
    return []


def _dedupe_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _missing_evidence_ids(hints: dict[str, object]) -> list[str]:
    values: list[str] = []
    for key in ("done_without_evidence_ids", "result_without_evidence_ids", "coverage_done_without_evidence_ids"):
        raw = hints.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item or "").strip())
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = ["TaskProgressTool"]
