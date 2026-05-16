# LLM: Runner prompt context summary keeps live model prompts small and refs-first.
# 模块用途: 从完整 SubAgentExecutionContext 提取 runner 启动必需摘要，避免 prompt 内联大 context bundle。

from __future__ import annotations

from pathlib import Path

from ..subagent import SubAgentExecutionContext


# LLM: runner_context_summary_payload keeps real runner prompts small and refs-first.
# 函数用途: 只把 runner 开工必须知道的字段放进 prompt；完整 execution/context bundle 通过路径引用读取。
def runner_context_summary_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    return {
        "identity": _runner_identity_payload(context),
        "refs": _runner_ref_payload(context, bundle),
        "task": {
            "goal": context.goal,
            "thought": context.thought,
            "plan": list(context.plan or []),
            "acceptance_checks": list(context.acceptance_checks or []),
        },
        "permissions": {
            "allowed_tools": list(context.allowed_tools or []),
            "allowed_skills": list(context.allowed_skills or []),
            "controlled_exec_grant_count": len(context.controlled_exec_grants or []),
        },
        "write_boundary": dict(context.write_boundary or {}),
        "task_envelope": _task_envelope_prompt_payload(bundle.get("task_envelope")),
        "tool_preflight": _tool_preflight_prompt_payload(bundle.get("tool_preflight")),
        "input_contract": _input_contract_prompt_payload(context),
        "output_contract": _dict_prompt_subset(
            bundle.get("output_contract"),
            [
                "product_write_roots",
                "required_files",
                "required_file_refs",
                "final_report_ref",
                "agent_run_final_report_ref",
                "output_json_ref",
                "file_contract",
                "write_contract",
            ],
        ),
        "pending_requests": list(context.pending_requests or [])[:3],
        "open_gaps": list(context.open_gaps or [])[:3],
        "instructions": list(context.instructions or [])[:8],
    }


# LLM: _input_contract_prompt_payload lifts concrete dependency artifacts above natural-language task text.
# 函数用途: 把已存在的上游输入路径单独放进瘦身 prompt，避免 runner 先读不存在的口语路径就误判 BLOCKED。
def _input_contract_prompt_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    refs = _string_list(getattr(context.context_manifest, "required_read_paths", []))
    if not refs:
        return {}
    resolved, unresolved = _resolve_read_paths(refs, _read_roots(context))
    return {
        "required_read_paths": refs[:12],
        "resolved_read_paths": resolved[:12],
        "unresolved_read_paths": unresolved[:12],
        "read_policy": (
            "先读取 resolved_read_paths；某个自然语言路径不存在时，继续尝试同名或已解析候选，"
            "只有所有相关候选都失败才 BLOCKED。"
        ),
    }


# LLM: _runner_identity_payload isolates stable run identity fields for prompt summaries.
# 函数用途: 输出 run、parent、root、session 和角色信息，供模型识别自己是谁。
def _runner_identity_payload(context: SubAgentExecutionContext) -> dict[str, object]:
    return {
        "run_id": context.run_id,
        "agent_name": context.agent_name,
        "role": context.role,
        "status": context.status,
        "verification_status": context.verification_status,
        "runner_attempts": context.runner_attempts,
        "runner_last_error": context.runner_last_error,
        "parent_id": context.parent_id,
        "root_id": context.root_id or context.run_id,
        "depth": context.depth,
        "subagent_session_id": context.subagent_session_id,
        "agent_thread_id": context.agent_thread_id,
    }


# LLM: _runner_ref_payload gives the model exact files to read when summary fields are insufficient.
# 函数用途: 暴露完整 execution/context bundle、任务目录和 workspace refs，不展开大 JSON。
def _runner_ref_payload(context: SubAgentExecutionContext, bundle: dict[str, object]) -> dict[str, object]:
    workspace_refs = _dict_prompt_subset(
        bundle.get("workspace_refs"),
        [
            "agent_run_workspace",
            "own_context_bundle_ref",
            "own_legacy_context_bundle_ref",
            "parent_context_bundle_ref",
            "task_workspace",
        ],
    )
    return {
        "task_dir": context.task_dir,
        "execution_context_json": context.execution_context_json,
        "execution_context_file": context.execution_context_file,
        "context_bundle_json": context.context_bundle_json,
        "context_bundle_file": context.context_bundle_file,
        "workspace_refs": workspace_refs,
    }


# LLM: _task_envelope_prompt_payload keeps protocol facts visible without copying every reserved field.
# 函数用途: 从 TaskEnvelope 里提取地址、目标、工具、写入和验收合同摘要。
def _task_envelope_prompt_payload(envelope: object) -> dict[str, object]:
    if not isinstance(envelope, dict):
        return {}
    return {
        "schema_version": envelope.get("schema_version") or "",
        "address": _dict_prompt_subset(
            envelope.get("address"),
            ["run_id", "root_id", "parent_id", "depth", "lineage", "workspace_ref"],
        ),
        "goal": envelope.get("goal") or "",
        "role": envelope.get("role") or "",
        "tool_contract": _dict_prompt_subset(
            envelope.get("tool_contract"),
            ["allowed_tools", "controlled_exec_grant_ids"],
        ),
        "write_contract": _dict_prompt_subset(
            envelope.get("write_contract"),
            ["product_write_roots", "allowed_write_roots", "forbidden_write_roots", "locked_files"],
        ),
        "acceptance": _dict_prompt_subset(envelope.get("acceptance"), ["checks"]),
        "context_refs": _dict_prompt_subset(envelope.get("context_refs"), ["context_bundle", "execution_context"]),
    }


# LLM: _tool_preflight_prompt_payload exposes stable issue codes without long diagnostics.
# 函数用途: 将 preflight 结果压成 ok、issue code 和有效工具列表。
def _tool_preflight_prompt_payload(preflight: object) -> dict[str, object]:
    if not isinstance(preflight, dict):
        return {}
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    return {
        "ok": bool(preflight.get("ok")),
        "issue_codes": [str(item.get("code") or "") for item in issues if isinstance(item, dict)],
        "effective_tools": list(preflight.get("effective_tools") or []),
    }


# LLM: _dict_prompt_subset copies only named JSON fields and drops unknown oversized bodies.
# 函数用途: 防止未来在 context bundle 里新增大字段时，runner prompt 自动膨胀。
def _dict_prompt_subset(value: object, keys: list[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value}


# LLM: _read_roots returns likely workspace roots for resolving relative required_read_paths.
# 函数用途: 读取 product_write_roots/output_contract root，再兜底 task_dir，让 summary 能给出真实可读绝对路径。
def _read_roots(context: SubAgentExecutionContext) -> list[Path]:
    roots: list[str] = []
    write_boundary = context.write_boundary if isinstance(context.write_boundary, dict) else {}
    roots.extend(_string_list(write_boundary.get("product_write_roots")))
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    contract = bundle.get("output_contract") if isinstance(bundle.get("output_contract"), dict) else {}
    roots.extend(_string_list(contract.get("product_write_roots")))
    roots.append(context.task_dir)
    return _unique_paths([Path(item).expanduser().resolve(strict=False) for item in roots if str(item or "").strip()])


# LLM: _resolve_read_paths keeps prompt-level path help deterministic and filesystem-backed.
# 函数用途: 将 required_read_paths 分成已存在的真实路径与未解析路径；相对路径按候选 root 尝试解析。
def _resolve_read_paths(refs: list[str], roots: list[Path]) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    unresolved: list[str] = []
    for ref in refs:
        path = Path(ref).expanduser()
        candidates = [path] if path.is_absolute() else [root / path for root in roots]
        existing = [str(item.resolve(strict=False)) for item in candidates if item.exists()]
        if existing:
            resolved.extend(existing)
        else:
            unresolved.append(ref)
    return _unique_strings(resolved), _unique_strings(unresolved)


# LLM: _string_list normalizes shallow config or manifest lists without importing subagent parsing helpers.
# 函数用途: 将单个字符串或列表安全转成字符串列表，供 prompt summary 保持轻依赖。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


# LLM: _unique_strings preserves first-seen order in model-facing path lists.
# 函数用途: 字符串去重，避免 prompt 里重复展示同一个候选路径。
def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


# LLM: _unique_paths preserves root priority while removing duplicate filesystem candidates.
# 函数用途: Path 去重，保持 product root 优先于 task_dir。
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
