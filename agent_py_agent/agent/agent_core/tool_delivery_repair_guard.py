# LLM: delivery repair guard keeps staged-delivery recovery actions actionable without relying on prompt prose as facts.
# 模块用途: 当 closeout 已经给出“先写 checkpoint / 先补非空结构化数据 / 先调 builder”这类结构化恢复动作时，阻止模型继续只读空转。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..backend import ModelResponse
from .tool_delivery_repair_attempt import strict_write_required
from .tool_delivery_repair_evidence_shape import violates_evidence_repair_shape
from .tool_delivery_repair_rejection_context import render_delivery_repair_rejection_context
from .tool_delivery_repair_required_calls import required_tool_calls
from .tool_shell_command_classifier import command_has_local_mutation

_MAX_REPAIRS = 2
_WRITE_FIRST_ACTIONS = {
    "invoke_builder_tool",
    "materialize_checkpoint",
    "repair_artifact_against_findings",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}
_PRODUCTIVE_TOOL_NAMES = {
    "append_file",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "run_command",
    "write_structured_json",
    "write_file",
}
_INSPECTION_ONLY_TOOL_NAMES = {
    "list_files",
    "list_tools",
    "read_file",
}
_EVIDENCE_GATHERING_TOOL_NAMES = {
    "fetch_url",
    "http_request",
    "read_artifact",
    "search",
}
_STRICT_REPAIR_PRODUCTIVE_TOOLS = {
    "__parse_error__", "append_file",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "write_structured_json",
    "write_file",
}
_RUN_COMMAND_INSPECTION_PREFIXES = ("cat ", "curl ", "find ", "ls", "pwd", "rg ", "wget ")


# LLM: _ProductivityContext stores structured runtime facts for the surrounding contract logic.
# 类用途: 保存当前模块使用的结构化字段，避免后续流程从普通自然语言推断机器事实。
@dataclass(frozen=True)
class _ProductivityContext:
    productive_tools: set[str]
    inspection_only_tools: set[str]
    strict_write_required: bool
    required_actions: list[dict[str, object]]


# LLM: delivery_repair_context exposes required staged-repair actions from closeout.json as one structured prompt hint.
# 函数用途: 读取 .agent_delivery/closeout.json 中的结构化恢复动作；若当前必须先修阶段产物，则返回下一轮模型可见的纠偏合同。
def delivery_repair_context(agent: object, repairs: int) -> str:
    payload = _delivery_repair_payload(agent)
    if not payload or repairs >= _MAX_REPAIRS:
        return ""
    return "\n".join(
        [
            "[tool-system delivery-required-repair]",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "当前仍处于阶段产物修复模式。下一轮必须优先执行能推进 required_actions 的真实工具调用，"
            "例如写出/修复 checkpoint、补齐非空结构化数据，或调用 builder tool。"
            "不要只重复 read_file、list_files、search、list_tools 这类检查动作。",
        ]
    )


# LLM: delivery_repair_rejection_context turns a blocked inspection turn into structured feedback.
# 函数用途: 当模型在必修阶段产物时调用了无进展工具，把被拒绝调用和下一步 required_tool_calls 一起反馈给下一轮。
def delivery_repair_rejection_context(agent: object, calls: list[dict[str, object]], repairs: int) -> str:
    return render_delivery_repair_rejection_context(_delivery_repair_payload(agent), calls, repairs, _MAX_REPAIRS)


# LLM: delivery_repair_block_response stops the loop once the model ignores required staged-repair actions multiple times.
# 函数用途: 模型连续忽略结构化阶段修复动作时，返回确定性阻断，避免在同一 no-progress 状态无限消耗轮次。
def delivery_repair_block_response(agent: object) -> ModelResponse | None:
    payload = _delivery_repair_payload(agent)
    if not payload:
        return None
    return ModelResponse(
        text=(
            "[DELIVERY_REQUIRED_REPAIR_BLOCKED] 结构化交付合同已经明确下一步必须先修阶段产物，"
            "但模型仍连续没有执行对应写入/构建动作，已停止本轮以避免继续空转。"
        ),
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
        runtime_status="blocked",
        runtime_reason="DELIVERY_REQUIRED_REPAIR",
    )


# LLM: has_required_delivery_repair returns True only when closeout recovery actions require a write-first step.
# 函数用途: 让工具循环只在“必须先写/修/构建”的阶段收紧；普通 acceptance failed 不会触发这里。
def has_required_delivery_repair(agent: object) -> bool:
    return bool(_delivery_repair_payload(agent))


# LLM: is_delivery_repair_productive_call checks whether at least one tool call can satisfy the current staged-repair contract.
# 函数用途: 只要本轮工具调用里包含 builder 或明显的写入/执行动作，就视为在推进 staged repair，不做拦截。
def is_delivery_repair_productive_call(agent: object, calls: list[dict[str, object]]) -> bool:
    payload = _delivery_repair_payload(agent)
    if not payload:
        return True
    required_actions = [item for item in payload.get("required_actions", []) if isinstance(item, dict)]
    action_tools = {
        str(item.get(key) or "").strip()
        for item in required_actions
        for key in ("builder_tool", "writer_tool")
    }
    action_tools.update(
        str(tool).strip()
        for item in required_actions
        for tool in item.get("write_tools", [])
        if isinstance(item.get("write_tools"), list)
    )
    strict_write_required = bool(payload.get("strict_write_required"))
    context = _ProductivityContext(
        productive_tools=_productive_tools(payload, action_tools),
        inspection_only_tools=_inspection_only_tools(required_actions, strict_write_required),
        strict_write_required=strict_write_required,
        required_actions=required_actions,
    )
    return any(_call_is_productive(call, context) for call in calls)


# LLM: _delivery_repair_payload extracts only the active write-first recovery actions from closeout.json.
# 函数用途: 读取 closeout 报告并筛选 recommended_action 属于写入/构建优先级的恢复动作；不读取自然语言日志。
def _delivery_repair_payload(agent: object) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return {}
    actions = progress.get("recovery_actions")
    if not isinstance(actions, list):
        return {}
    required_actions = [
        {
            "builder_tool": str(item.get("builder_tool") or ""),
            "category": str(item.get("category") or ""),
            "checkpoint_shape_hint": str(item.get("checkpoint_shape_hint") or ""),
            "checkpoint_ref": str(item.get("checkpoint_ref") or ""),
            "code": str(item.get("code") or ""),
            "evidence_shape_hint": str(item.get("evidence_shape_hint") or ""),
            "missing_columns": str(item.get("missing_columns") or ""),
            "output_ref": str(item.get("output_ref") or ""),
            "recommended_action": str(item.get("recommended_action") or ""),
            "required_columns": item.get("required_columns") if isinstance(item.get("required_columns"), list) else [],
            "required_fields": item.get("required_fields") if isinstance(item.get("required_fields"), list) else [],
            "required_sheets_min": item.get("required_sheets_min") or 0,
            "retryable": bool(item.get("retryable", True)),
            "source_ref": str(item.get("source_ref") or ""),
            "writer_tool": str(item.get("writer_tool") or ""),
            "artifact_id": str(item.get("artifact_id") or ""),
            "artifact_path": str(item.get("artifact_path") or ""),
            "finding_codes": item.get("finding_codes") if isinstance(item.get("finding_codes"), list) else [],
            "finding_values": item.get("finding_values") if isinstance(item.get("finding_values"), list) else [],
            "repair_targets": item.get("repair_targets") if isinstance(item.get("repair_targets"), list) else [],
            "write_tools": item.get("write_tools") if isinstance(item.get("write_tools"), list) else [],
        }
        for item in actions
        if isinstance(item, dict)
        and str(item.get("recommended_action") or "").strip() in _WRITE_FIRST_ACTIONS
    ]
    if not required_actions:
        return {}
    pending_targets = progress.get("pending_materialization_targets")
    agent_root = Path(getattr(agent, "root", ".")).resolve()
    return {
            "pending_materialization_targets": pending_targets if isinstance(pending_targets, list) else [],
            "report_ref": str(report.get("report_ref") or ""),
            "required_actions": required_actions,
            "repair_target_snapshots": _repair_target_snapshots(required_actions, agent_root),
            "required_tool_calls": required_tool_calls(required_actions),
            "strict_write_required": strict_write_required(progress, agent_root=agent_root),
        }


# LLM: _closeout_report keeps the repair guard grounded in the same machine report that closeout writes.
# 函数用途: 读取当前工作区 .agent_delivery/closeout.json；不存在或损坏时返回空对象，不让纠偏逻辑抛异常。
def _closeout_report(agent: object) -> dict[str, object]:
    path = Path(getattr(agent, "root", ".")).resolve() / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _call_is_productive treats pure inspection loops as non-productive but leaves real data gathering or execution actions alone.
# 函数用途: 只有明确的只读检查工具才会被 staged repair guard 拦下；抓取数据、执行命令和构建产物都算推进。
def _call_is_productive(
    call: dict[str, object],
    context: _ProductivityContext,
) -> bool:
    tool = str(call.get("tool") or "").strip()
    if _is_declared_repair_target_read(call, context.required_actions):
        return True
    if _is_checkpoint_repair_read(call, context.required_actions):
        return True
    if _is_evidence_repair_gathering(call, context.required_actions):
        return True
    if _violates_declared_writer_tool(call, context.required_actions):
        return False
    if violates_evidence_repair_shape(
        call,
        context.required_actions,
        path_matches=_same_path_ref,
        call_path=_call_path,
    ):
        return False
    if _violates_non_empty_rows_repair(call, context.required_actions):
        return False
    if tool == "run_command":
        return _run_command_is_productive(call, context)
    if tool in context.productive_tools:
        return True
    if context.strict_write_required:
        return False
    return tool not in context.inspection_only_tools


# LLM: _violates_declared_writer_tool keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _violates_declared_writer_tool(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    tool = str(call.get("tool") or "").strip()
    path = _call_path(call)
    if not tool or not path:
        return False
    for action in required_actions:
        writer_tool = str(action.get("writer_tool") or "").strip()
        checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
        if writer_tool and checkpoint_ref and tool != writer_tool and _same_path_ref(path, checkpoint_ref):
            return True
    return False


# LLM: STAGED_JSON_NO_ROWS repair requires actual structured rows, not just touching the checkpoint path.
# 函数用途: 对 write_non_empty_structured_rows 恢复动作做机器级校验，空 sheets/rows 不能算有效修复推进。
def _violates_non_empty_rows_repair(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "write_structured_json":
        return False
    path = _call_path(call)
    if not path:
        return False
    for action in required_actions:
        if str(action.get("recommended_action") or "") != "write_non_empty_structured_rows":
            continue
        if _same_path_ref(path, str(action.get("checkpoint_ref") or "")):
            return not _has_non_empty_structured_rows(call)
    return False


# LLM: _has_non_empty_structured_rows checks table payload shape without reading prose.
# 函数用途: 在 write_structured_json 参数中识别 rows/sheets/data 的非空行，支持分批写入和嵌套 data 包装。
def _has_non_empty_structured_rows(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    if not isinstance(value, dict):
        return False
    rows = value.get("rows")
    if isinstance(rows, list) and rows:
        return True
    sheets = value.get("sheets")
    if isinstance(sheets, list) and any(_has_non_empty_structured_rows(item) for item in sheets):
        return True
    data = value.get("data")
    if isinstance(data, (dict, list)):
        return _has_non_empty_structured_rows(data)
    return False


# LLM: _is_checkpoint_repair_read allows one structured checkpoint inspection before rewriting it.
# 函数用途: 修复结构化 JSON checkpoint 时，读取同一 checkpoint_ref 是获取机器数据，不是验收产物空转。
def _is_checkpoint_repair_read(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "read_file":
        return False
    path = _call_path(call)
    if not path:
        return False
    repair_actions = {"repair_evidence_refs", "repair_structured_checkpoint_json", "write_non_empty_structured_rows"}
    return any(
        str(action.get("recommended_action") or "") in repair_actions
        and _same_path_ref(path, str(action.get("checkpoint_ref") or ""))
        for action in required_actions
    )


# LLM: declared repair targets create a bounded read allowance before a mutation.
# 函数用途: 只允许读取 closeout 机器字段列出的 repair_targets/checkpoint_ref/artifact_path，避免修复前检查退化成随意探索。
def _is_declared_repair_target_read(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "read_file":
        return False
    path = _call_path(call)
    if not path:
        return False
    return any(_same_path_ref(path, target) for target in _repair_target_values(required_actions))


# LLM: _is_evidence_repair_gathering allows source collection when claims/source_refs are the failed contract.
# 函数用途: 证据修复可以先调用抓取/检索工具补 source_refs，但仍由后续 verifier 要求写入结构化 claims。
def _is_evidence_repair_gathering(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool not in _EVIDENCE_GATHERING_TOOL_NAMES:
        return False
    return any(
        str(action.get("recommended_action") or "") == "repair_evidence_refs"
        and bool(action.get("required_fields"))
        for action in required_actions
    )


# LLM: _call_path keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _call_path(call: dict[str, object]) -> str:
    for key in ("path", "file_path", "target_path"):
        if value := str(call.get(key) or "").strip():
            return value
    return ""


# LLM: _same_path_ref keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _same_path_ref(path: str, ref: str) -> bool:
    normalized_path = path.replace("\\", "/").rstrip("/")
    normalized_ref = ref.replace("\\", "/").strip("/")
    return (
        normalized_path == normalized_ref
        or normalized_path.endswith(f"/{normalized_ref}")
        or normalized_ref.endswith(f"/{normalized_path}")
    )


# LLM: _run_command_is_productive keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _run_command_is_productive(call: dict[str, object], context: _ProductivityContext) -> bool:
    command = _call_command(call)
    if not command:
        return False
    if context.strict_write_required:
        return False
    if command_has_local_mutation(command):
        writer_refs = _declared_writer_refs(context.required_actions)
        if writer_refs:
            return any(_command_references_ref(command, ref) for ref in writer_refs)
        return True
    if ">" in command:
        return True
    return not any(command.startswith(prefix) for prefix in _RUN_COMMAND_INSPECTION_PREFIXES)


# LLM: _declared_writer_refs keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _declared_writer_refs(required_actions: list[dict[str, object]]) -> list[str]:
    return [
        str(action.get("checkpoint_ref") or "").strip()
        for action in required_actions
        if str(action.get("writer_tool") or "").strip() and str(action.get("checkpoint_ref") or "").strip()
    ]


# LLM: _command_references_ref keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _command_references_ref(command: str, ref: str) -> bool:
    normalized_command = command.replace("\\", "/")
    normalized_ref = ref.replace("\\", "/").strip("/")
    return normalized_ref in normalized_command


# LLM: _productive_tools decides which tools count as progress for the current staged-repair phase.
# 函数用途: 根据 strict_write_required 和 builder_tool 集合计算本轮允许视为“有效修复推进”的工具名集合。
def _productive_tools(payload: dict[str, object], action_tools: set[str]) -> set[str]:
    if bool(payload.get("strict_write_required")):
        return {tool for tool in action_tools if tool} | _STRICT_REPAIR_PRODUCTIVE_TOOLS
    return {tool for tool in action_tools if tool} | _PRODUCTIVE_TOOL_NAMES


# LLM: _inspection_only_tools tightens the redirect rule only when the contract has advanced to a builder-ready stage.
# 函数用途: 基础检查工具一直算 inspection-only；只有进入 invoke_builder_tool 阶段时，read_artifact 才一并视为拖延动作。
def _inspection_only_tools(required_actions: list[dict[str, object]], strict_write_required: bool) -> set[str]:
    tools = set(_INSPECTION_ONLY_TOOL_NAMES)
    if not strict_write_required and _has_artifact_finding_repair_action(required_actions):
        tools.difference_update(_allowed_artifact_repair_inspection_tools(required_actions))
    if not strict_write_required and not _has_builder_ready_action(required_actions):
        return tools
    tools.update(_EVIDENCE_GATHERING_TOOL_NAMES)
    if strict_write_required or any(str(item.get("recommended_action") or "") == "invoke_builder_tool" for item in required_actions):
        tools.add("read_artifact")
    return tools


# LLM: Data-gathering tools stay available while checkpoints need facts, but not once a builder is ready.
# 函数用途: 判断当前修复动作是否已经进入 builder 阶段；builder ready 后继续读证据就是拖延，应回到构建工具。
def _has_builder_ready_action(required_actions: list[dict[str, object]]) -> bool:
    return any(str(item.get("recommended_action") or "") == "invoke_builder_tool" for item in required_actions)


# LLM: _has_artifact_finding_repair_action keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _has_artifact_finding_repair_action(required_actions: list[dict[str, object]]) -> bool:
    return any(str(item.get("recommended_action") or "") == "repair_artifact_against_findings" for item in required_actions)


# LLM: Artifact repair only treats inspection as progress when the artifact is still missing.
# 函数用途: 已存在但验收失败的产物需要写入/替换推进；重复 read_file 不再算修复动作。
def _allowed_artifact_repair_inspection_tools(required_actions: list[dict[str, object]]) -> set[str]:
    codes = {
        str(code)
        for action in required_actions
        if str(action.get("recommended_action") or "") == "repair_artifact_against_findings"
        for code in action.get("finding_codes", [])
        if isinstance(action.get("finding_codes"), list)
    }
    if codes and codes.issubset({"ARTIFACT_MISSING"}):
        return {"list_files"}
    return set()


# LLM: _call_command keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""


# LLM: _repair_target_snapshots gives the model bounded file facts when read_file is rejected during repair.
# 函数用途: 对 required repair_targets 读取小预览，避免模型为了获取补丁上下文反复调用检查工具。
def _repair_target_snapshots(required_actions: list[dict[str, object]], agent_root: Path) -> list[dict[str, object]]:
    targets = _repair_target_values(required_actions)
    snapshots: list[dict[str, object]] = []
    budget = 6000
    for target in targets[:4]:
        snapshot = _repair_target_snapshot(target, agent_root, max_chars=min(2400, budget))
        if snapshot:
            budget -= len(str(snapshot.get("preview") or ""))
            snapshots.append(snapshot)
        if budget <= 0:
            break
    return snapshots


# LLM: _repair_target_values collects candidate files from structured recovery actions.
# 函数用途: 只读取 repair_targets/checkpoint_ref/artifact_path 等机器字段，不解析提示词或日志正文。
def _repair_target_values(required_actions: list[dict[str, object]]) -> list[str]:
    values: list[str] = []
    for action in required_actions:
        targets = action.get("repair_targets")
        if isinstance(targets, list):
            values.extend(str(item) for item in targets if str(item))
        for key in ("checkpoint_ref", "artifact_path"):
            value = str(action.get(key) or "").strip()
            if value:
                values.append(value)
    return list(dict.fromkeys(values))


# LLM: _repair_target_snapshot reads a bounded text preview for one local repair target.
# 函数用途: 快照只面向小型文本文件；目录、大文件和二进制文件只返回存在性事实。
def _repair_target_snapshot(target: str, agent_root: Path, *, max_chars: int) -> dict[str, object]:
    path = _resolve_repair_target(target, agent_root)
    if not path.exists():
        return {"path": target, "exists": False}
    if path.is_dir():
        return {"path": target, "exists": True, "kind": "directory"}
    try:
        size = path.stat().st_size
    except OSError:
        return {"path": target, "exists": True, "readable": False}
    snapshot: dict[str, object] = {"path": target, "exists": True, "kind": "file", "size_bytes": size}
    if size > 12000:
        return snapshot
    try:
        snapshot["preview"] = path.read_text(encoding="utf-8")[:max(0, max_chars)]
    except UnicodeDecodeError:
        snapshot["text"] = False
    except OSError:
        snapshot["readable"] = False
    return snapshot


# LLM: _resolve_repair_target keeps snapshot reads scoped to the current agent root for relative refs.
# 函数用途: 支持 absolute repair_targets，同时让相对 ref 按 workspace root 解析。
def _resolve_repair_target(target: str, agent_root: Path) -> Path:
    path = Path(target).expanduser()
    return path if path.is_absolute() else agent_root / path
