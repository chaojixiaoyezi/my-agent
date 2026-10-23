
# LLM: runner 模型只消费有界事实投影；资料路径探测失败不构成启动门，中断继续传播，改动同步核对 runner prompt 测试。
# 模块用途: 精简子代理启动与续跑上下文，只读探测资料路径，不将内部 output 目录当写入目的地。
from __future__ import annotations

from pathlib import Path

from ...contracts.subagent_completion import (
    completion_takeover_hint_facts,
    completion_tool_failure_halt_facts,
)
from ...model_visible_refs import current_model_ref, current_model_text
from ...subagents import SubAgentExecutionContext

_HOST_CLOSEOUT_FILENAMES = frozenset(
    {"final_report.md", "output.json", "runner_result.json", "runner_response.md"}
)


# LLM: 精简只删除可选空节、引用完全相同的正文；身份、权限空值、负向 gate 和精确 refs 不参与裁剪。
# 函数用途: 生成子代理真正看到的单份任务与运行事实，直属孩子保持结构化，宿主收口文件继续隐藏。
def runner_context_summary_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    task = {
        "goal": current_model_text(context.goal),
        "thought": current_model_text(context.thought),
        "plan": [current_model_text(item) for item in list(context.plan or [])],
    }
    reads = _read_refs_prompt_payload(context)
    boundary = _write_boundary_prompt_payload(context.write_boundary)
    refs = _runner_ref_payload(context, bundle)
    workspace_refs = refs["workspace_refs"]
    payload = {
        "identity": _runner_identity_payload(context),
        "refs": refs,
        "task": task,
        "permissions": {
            "allowed_tools": list(context.allowed_tools or []),
            "allowed_skills": list(context.allowed_skills or []),
            "controlled_exec_grant_count": len(context.controlled_exec_grants or []),
        },
        "workspace_root": boundary.get("workspace_root") or boundary.get("execution_cwd")
        or workspace_refs.get("execution_cwd") or workspace_refs.get("owner_workspace_dir") or "",
        "write_boundary": boundary,
        "context_gate": _dict_prompt_subset(bundle.get("gate"), ["ok", "missing_fields", "blocking_reason"]),
        "input_refs": _model_visible_prompt_value(bundle["input_refs"]) if "input_refs" in bundle else _unique_strings(
            list(reads.get("resolved_read_paths") or []) + list(reads.get("resolved_hint_read_paths") or []),
        ),
        "conversation": _conversation_prompt_payload(bundle),
        "direct_children": _direct_children_prompt_payload(bundle.get("direct_children")),
        "collaboration": _collaboration_prompt_payload(bundle.get("collaboration")),
        "takeover": _bounded_object(bundle.get("takeover")),
        "context_packs": _context_packs_prompt_payload(context.context_packs, task),
        "task_envelope": _task_envelope_prompt_payload(bundle.get("task_envelope"), goal=task["goal"]),
        "tool_preflight": _tool_preflight_prompt_payload(bundle.get("tool_preflight")),
        "recovery": _recovery_prompt_payload(bundle.get("runner_recovery_preflight")),
        "read_refs": reads,
        "output_contract": _dict_prompt_subset(
            bundle.get("output_contract"),
            [
                "output_files",
                "output_refs",
                "required_files",
                "required_file_refs",
                "file_contract",
            ],
        ),
        "pending_requests": list(context.pending_requests or [])[:3],
        "open_gaps": list(context.open_gaps or [])[:3],
        "instructions": list(context.instructions or [])[:8],
    }
    # 显式scope/authority只透传上游事实，不从空节省略规则推断或扩展权限。
    payload.update(_dict_prompt_subset(bundle, ["scope", "authority"]))
    required = {"identity", "refs", "task", "permissions", "workspace_root", "write_boundary", "context_gate", "input_refs", "scope", "authority"}
    return {key: value for key, value in payload.items() if key in required or value not in ({}, [], "", None)}


# LLM: Preserve child rows and capability requests as bounded dictionaries;
# generic list bounding would stringify them and erase machine-readable fields.
# 函数用途: 把直属孩子状态包缩小后保持为结构化 JSON。
def _direct_children_prompt_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    payload = _dict_prompt_subset(
        value,
        [
            "schema_version",
            "parent_run_id",
            "total",
            "counts",
            "all_terminal",
            "active_run_ids",
            "attention_run_ids",
        ],
    )
    rows: list[dict[str, object]] = []
    raw_rows = value.get("items")
    selected_rows = list(raw_rows or []) if isinstance(raw_rows, list | tuple) else []
    for item in selected_rows[:12]:
        if not isinstance(item, dict):
            continue
        rows.append(_direct_child_prompt_row(item))
    payload["items"] = rows
    return payload


# LLM: One direct-child row keeps only bounded status/refs, the contract-projected
# tool_failure_halt and takeover_hint, and at most three structured capability requests; raw responses remain behind refs.
# 函数用途: 将一个直属孩子裁成父级 prompt 可安全读取的小对象，被授权门反复拦下而停止时带上收口事实。
def _direct_child_prompt_row(item: dict[str, object]) -> dict[str, object]:
    row = _dict_prompt_subset(
        item,
        [
            "run_id",
            "status",
            "turn_end_reason",
            "failure_type",
            "goal",
            "latest_summary",
            "completion_schema_version",
            "completion_message",
            "completion_message_truncated",
            "completion_message_original_tokens",
            "final_report_ref",
            "declared_output_refs",
            "artifact_refs",
            "evidence_refs",
        ],
    )
    # 授权阶段同码连续失败收口事实沿完成合同的同一投影交给递归父级，不另拼字段。
    row.update(completion_tool_failure_halt_facts(item))
    row.update(completion_takeover_hint_facts(item))
    requests = item.get("open_capability_requests")
    if isinstance(requests, list | tuple):
        row["open_capability_requests"] = [
            _dict_prompt_subset(
                request,
                [
                    "request_id",
                    "capability_type",
                    "needed_capability",
                    "tools",
                    "skills",
                    "path_scope",
                ],
            )
            for request in requests[:3]
            if isinstance(request, dict)
        ]
    return row


def _read_refs_prompt_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    refs = _path_ref_list(getattr(context.context_manifest, "required_read_paths", []))
    hints = _path_ref_list(getattr(context.context_manifest, "hint_read_paths", []))
    if not refs and not hints:
        return {}
    resolved, unresolved = _resolve_read_paths(refs, _read_roots(context))
    hint_resolved, _ = _resolve_read_paths(hints, _read_roots(context))
    return {
        "declared_read_path_count": len(refs),
        "resolved_read_paths": resolved[:12],
        "unresolved_read_path_count": len(unresolved),
        "hint_read_path_count": len(hints),
        "resolved_hint_read_paths": hint_resolved[:12],
        "read_policy": "这些路径是可读线索/授权范围，不是启动前置条件；缺失时记录限制并继续按任务判断。",
    }


# LLM: 身份只投影canonical context；task_id与真实runner参数一致取root，不以JSON别名建立权限或新身份。
# 函数用途: 显示当前孩子及父根关系，保留旧键并提供准确的run/task字段。
def _runner_identity_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    return {
        "run_id": context.run_id,
        "task_id": context.root_id or context.run_id,
        "agent_name": context.agent_name,
        "role": context.role,
        "status": context.status,
        "runner_attempts": context.runner_attempts,
        "runner_last_error": context.runner_last_error,
        "parent_id": context.parent_id,
        "root_id": context.root_id or context.run_id,
        "parent_run_id": context.parent_id,
        "root_run_id": context.root_id or context.run_id,
        "depth": context.depth,
        "subagent_session_id": context.subagent_session_id,
        "agent_thread_id": context.agent_thread_id,
    }


def _conversation_prompt_payload(bundle: dict[str, object]) -> dict[str, object]:
    if not isinstance(bundle, dict):
        return {}
    conversation = bundle.get("conversation")
    return dict(conversation) if isinstance(conversation, dict) else {}


# LLM: 非空协作请求和响应策略仍保留结构；仅无请求、无计数、无策略时省去空外壳，不代替协作账。
# 函数用途: 为孩子显示待回应的协作材料，空集合不重复占启动上下文。
def _collaboration_prompt_payload(collaboration: object) -> dict[str, object]:
    if not isinstance(collaboration, dict):
        return {}
    requests = collaboration.get("targeted_requests")
    if not isinstance(requests, list):
        requests = []
    if not requests and not collaboration.get("targeted_request_count") and not collaboration.get("responder_policy"):
        return {}
    return {
        "targeted_request_count": int(collaboration.get("targeted_request_count") or len(requests)),
        "targeted_requests": [_collaboration_request_prompt_payload(item) for item in requests[:5] if isinstance(item, dict)],
        "responder_policy": collaboration.get("responder_policy") or "",
    }


def _collaboration_request_prompt_payload(request: dict[str, object]) -> dict[str, object]:
    payload = _dict_prompt_subset(
        request,
        [
            "case_id",
            "request_id",
            "case_ref",
            "request_ref",
            "case_title",
            "question",
            "problem_statement",
            "priority",
            "deadline_at",
            "target_agent_ids",
            "required_capabilities",
            "recommended_tools",
            "context_refs",
        ],
    )
    payload["observed_facts"] = _bounded_object_list(request.get("observed_facts"), limit=4)
    payload["query_hints"] = _bounded_object_list(request.get("query_hints"), limit=4)
    payload["query_intent"] = _bounded_object(request.get("query_intent"))
    payload["routing_requirements"] = _bounded_object(request.get("routing_requirements"))
    payload["response_contract"] = _bounded_object(request.get("response_contract"))
    return {key: value for key, value in payload.items() if value not in ({}, [], "", None)}


# LLM: 用未截断原文做精确比较，但只登记最终投影保留的字段；引用必须可解析，不同尾部不得误合并。
# 函数用途: 以明确的本地字段引用替代相同任务或父级说明，保留不同正文和资料来源。
def _context_packs_prompt_payload(value: object, task: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    seen = {str(task[key]): f"task.{key}" for key in ("goal", "thought") if task.get(key)}
    packs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        pack = _bounded_object(item)
        for key in ("summary", "handoff_note"):
            text = current_model_text(item.get(key) or "")
            if not text or key not in pack:
                continue
            if text in seen:
                pack.pop(key)
                pack[f"{key}_ref"] = seen[text]
            else:
                seen[text] = f"context_packs[{len(packs)}].{key}"
        if pack:
            packs.append(pack)
        if len(packs) >= 5:
            break
    return packs


# LLM: 运行记录与业务 cwd 分开命名；移除旧内部 work/output 路径提示，不改宿主恢复字段。
# 函数用途: 给子代理明确工作目录及恢复位置，避免重复出两套“交付目录”。
def _runner_ref_payload(context: SubAgentExecutionContext, bundle: dict[str, object]) -> dict[str, object]:
    workspace_refs = _dict_prompt_subset(
        bundle.get("workspace_refs"),
        [
            "execution_cwd",
            "owner_workspace_dir",
            "agent_work_dir",
            "own_context_bundle_ref",
            "parent_context_bundle_ref",
            "task_root",
            "agent_run_checkpoint",
            "agent_run_summary",
        ],
    )
    return {
        "runtime_state_root": current_model_ref(workspace_refs.pop("task_root", "") or context.task_dir),
        "context_bundle_json": current_model_ref(context.context_bundle_json),
        "context_bundle_file": current_model_ref(context.context_bundle_file),
        "workspace_refs": workspace_refs,
    }


# LLM: envelope 不产生第二份目标；只有与 task.goal 完全一致时才替换为可解析的字段引用。
# 函数用途: 保留 envelope 地址和授权合同，避免重复父级长任务，独有目标仍完整内联。
def _task_envelope_prompt_payload(envelope: object, *, goal: str = "") -> dict[str, object]:
    if not isinstance(envelope, dict):
        return {}
    payload = {
        "schema_version": envelope.get("schema_version") or "",
        "address": _dict_prompt_subset(
            envelope.get("address"),
            ["run_id", "root_id", "parent_id", "depth", "lineage", "workspace_ref"],
        ),
        "goal": current_model_text(envelope.get("goal") or ""),
        "role": envelope.get("role") or "",
        "tool_contract": _dict_prompt_subset(
            envelope.get("tool_contract"),
            ["allowed_tools", "controlled_exec_grant_ids"],
        ),
        "write_contract": _dict_prompt_subset(
            envelope.get("write_contract"),
            ["output_files", "output_refs", "forbidden_write_roots", "locked_files"],
        ),
        "context_refs": _dict_prompt_subset(envelope.get("context_refs"), ["context_bundle"]),
    }
    if goal and payload["goal"] == goal:
        payload.pop("goal")
        payload["goal_ref"] = "task.goal"
    return payload


# LLM: Tool execution keeps the full host boundary internally, but the model
# sees only cwd, permission roots, and typed grants. Host status/closeout file
# slots are never task instructions or delivery targets.
# 函数用途: 把运行时写围栏裁成子代理真正需要理解的权限事实，隐藏宿主维护的状态文件。
def _write_boundary_prompt_payload(value: object) -> dict[str, object]:
    return _dict_prompt_subset(
        value,
        [
            "workspace_root",
            "execution_cwd",
            "owner_workspace_dir",
            "role",
            "allowed_write_roots",
            "allowed_read_roots",
            "read_scope_mode",
            "artifact_read_scope_mode",
            "artifact_read_root",
            "product_write_roots",
            "product_write_policy",
            "forbidden_write_roots",
            "locked_files",
            "controlled_exec_grants",
            "effective_permissions",
            "shell_access_mode",
        ],
    )


# LLM: Recovery may expose bounded checkpoint/summary/task refs, never the
# current run's host-generated response/result/closeout files. This also cleans
# old durable bundles before they reach a resumed model prompt.
# 函数用途: 保留真正能续跑的恢复线索，并移除宿主最终收口文件及其提示文字。
def _recovery_prompt_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        return {}
    payload = dict(value)
    refs = [str(item or "").strip() for item in payload.get("recovery_refs") or []]
    blocked = [ref for ref in refs if _is_host_closeout_ref(ref)]
    payload["recovery_refs"] = [ref for ref in refs if ref and ref not in blocked]
    instruction = current_model_text(payload.get("runner_instruction") or "")
    for ref in blocked:
        instruction = instruction.replace(ref, "")
    payload["runner_instruction"] = instruction
    return _bounded_object(payload)


# LLM: Filename checks are confined to the host-owned recovery-ref list; normal
# required_file_refs remain authoritative even when a business file shares a
# familiar name such as final_report.md.
# 函数用途: 识别恢复清单里的宿主收口文件，不按名称误删用户明确声明的业务产物。
def _is_host_closeout_ref(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and Path(text).name in _HOST_CLOSEOUT_FILENAMES)


def _tool_preflight_prompt_payload(preflight: object) -> dict[str, object]:
    if not isinstance(preflight, dict):
        return {}
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    return {
        "ok": bool(preflight.get("ok")),
        "issue_codes": [str(item.get("code") or "") for item in issues if isinstance(item, dict)],
        "effective_tools": list(preflight.get("effective_tools") or []),
    }


def _dict_prompt_subset(value: object, keys: list[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: _model_visible_prompt_value(value[key]) for key in keys if key in value}


def _bounded_object_list(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [
        _bounded_object(item)
        for item in value[:limit]
        if isinstance(item, dict)
    ]


# LLM: 普通字段维持原数量预算，显式引用额外保留；不读取路径或从描述推断新的资料授权。
# 函数用途: 缩短可选资料说明，同时保住继续读取所需的完整引用。
def _bounded_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    selected = dict(list(value.items())[:12])
    selected.update((key, item) for key, item in value.items() if _is_reference_key(str(key)))
    for key, item in selected.items():
        bounded = _bounded_value(item, key=str(key))
        if bounded is not None:
            result[str(key)] = bounded
    return result


# LLM: 限长只适用于描述，结构化 ref 是工具的精确地址；裁短引用会改变其身份和读取目标。
# 函数用途: 识别声明为引用的字段，让恢复和资料地址绕过有损文本摘要。
def _is_reference_key(key: str) -> bool:
    return key in {"id", "ref", "refs", "path", "url"} or key.endswith((
        "_id", "_ids", "_ref", "_refs", "_path", "_paths", "_url", "_urls", "_root", "_roots",
    ))


# LLM: 精确引用复用原模型可见净化器，不裁地址；普通描述仍受原字符/结构预算，返回值不授权。
# 函数用途: 把单个资料字段转成精简展示值，避免长恢复地址变成不可用前缀。
def _bounded_value(item: object, *, key: str = "") -> object:
    if _is_reference_key(key):
        return _model_visible_prompt_value(item)
    if isinstance(item, str):
        return current_model_text(item)[:300]
    if isinstance(item, bool | int | float):
        return item
    if isinstance(item, list | tuple):
        return [current_model_text(entry)[:160] for entry in item[:8]]
    if isinstance(item, dict):
        return _bounded_child_dict(item)
    return current_model_text(item)[:160] if item is not None else None


# LLM: 嵌套普通值保持原字段窗口；精确引用不受位置影响并逐字净化保留，不改变执行上下文。
# 函数用途: 显示可选资料内的小字典，保住其中的真实引用地址。
def _bounded_child_dict(item: dict[object, object]) -> dict[str, object]:
    selected = dict(list(item.items())[:8])
    selected.update((key, value) for key, value in item.items() if _is_reference_key(str(key)))
    return {
        str(child_key): _model_visible_prompt_value(child_value) if _is_reference_key(str(child_key))
        else current_model_text(child_value)[:160]
        for child_key, child_value in selected.items()
    }


def _read_roots(context: SubAgentExecutionContext) -> list[Path]:
    roots: list[str] = []
    write_boundary = context.write_boundary if isinstance(context.write_boundary, dict) else {}
    roots.extend(_path_ref_list(write_boundary.get("product_write_roots")))
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    contract = bundle.get("output_contract") if isinstance(bundle.get("output_contract"), dict) else {}
    roots.extend(_path_ref_list(contract.get("product_write_roots")))
    roots.append(context.task_dir)
    return _unique_paths([Path(item).expanduser().resolve(strict=False) for item in roots if str(item or "").strip()])


# LLM: 逐候选只读查询路径，文件系统失败沿原 unresolved 投影；不得吞掉停止信号或把解析结果作为授权。
# 函数用途: 保留仍可找到的资料引用，坏候选不妨碍其余目录和引用，供启动与恢复提示使用。
def _resolve_read_paths(refs: list[str], roots: list[Path]) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    unresolved: list[str] = []
    for ref in refs:
        path = Path(ref).expanduser()
        candidates = [path] if path.is_absolute() else [root / path for root in roots]
        existing: list[str] = []
        for item in candidates:
            try:
                if item.exists():
                    existing.append(str(item.resolve(strict=False)))
            except InterruptedError:
                raise
            except OSError:
                continue
        if existing:
            resolved.extend(existing)
        else:
            unresolved.append(ref)
    return _unique_strings(resolved), _unique_strings(unresolved)


def _path_ref_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        ref = current_model_ref(value)
        return [ref] if ref else []
    if isinstance(value, list | tuple | set):
        return [ref for item in value if (ref := current_model_ref(item))]
    return []


def _model_visible_prompt_value(value: object) -> object:
    if isinstance(value, str):
        return current_model_text(value)
    if isinstance(value, list | tuple | set):
        return [current_model_text(item) if isinstance(item, str) else item for item in value]
    if isinstance(value, dict):
        return {str(key): _model_visible_prompt_value(item) for key, item in value.items()}
    return value


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _unique_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(value)
    return result
