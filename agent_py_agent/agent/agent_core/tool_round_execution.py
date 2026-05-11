# LLM: Tool round execution helpers keep ToolLoopService thin while preserving runner trace contracts.
# 模块用途: 执行一轮模型工具调用、回写 live context，并识别子代理 output.json 收口信号。

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..backends import ModelResponse
from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .tool_call_context_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)

_STATEFUL_ORCHESTRATION_TOOLS = {"create_subagents", "schedule_child_subagents"}
_DEPENDENT_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
    "subagent_board",
    "subagent_message",
}


# LLM: ToolCallRecordParams keeps tool record inputs bundled for trace/archive reducers.
# 类用途: 集中保存工具调用记录字段；调用方用它写 live context、runner trace 和外置工具输出记录。
@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


# LLM: ToolCallExecuteParams keeps one tool execution request bundled before result recording.
# 类用途: 单次工具调用执行参数包，避免 runner trace 和执行入口继续增加散乱参数。
@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object


# LLM: ToolRoundExecutionRequest bundles callbacks needed to run one model tool round.
# 类用途: 保存一轮工具调用所需上下文和回调；模块本身不持有 agent service 状态。
@dataclass(frozen=True)
class ToolRoundExecutionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    tool_rounds: int
    response: ModelResponse
    calls: list[dict[str, object]]
    execute_one: Callable[[ToolCallExecuteParams], ToolExecutionResult]
    record_one: Callable[[ToolCallRecordParams], None]


# LLM: execute_tool_round runs all calls in one assistant round and returns whether the runner completed.
# 函数用途: 回写本轮工具上下文、逐个执行和记录工具调用，并检测当前子代理是否写出自己的 output.json。
def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    _append_assistant_tool_round_context(request)
    subagent_output_written = False
    stateful_orchestration_seen = False
    for idx, payload in enumerate(request.calls, start=1):
        tool_name = _tool_name(payload)
        if stateful_orchestration_seen and tool_name in _DEPENDENT_ORCHESTRATION_TOOLS:
            result = _deferred_orchestration_result(tool_name)
        else:
            result = request.execute_one(
                ToolCallExecuteParams(request.params, request.tool_rounds, idx, payload)
            )
        request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
        subagent_output_written = subagent_output_written or _is_subagent_output_json_write(
            request.agent, payload, result
        )
        stateful_orchestration_seen = (
            stateful_orchestration_seen or tool_name in _STATEFUL_ORCHESTRATION_TOOLS
        )
    return subagent_output_written


# LLM: _tool_name extracts a model-requested tool name without trusting payload shape.
# 函数用途: 从工具调用 payload 中读取工具名；坏 payload 返回空字符串，由正常执行路径处理错误。
def _tool_name(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tool") or "").strip()


# LLM: _deferred_orchestration_result prevents same-turn dispatch from using hallucinated run ids.
# 函数用途: 当模型同一轮先创建子代理又立刻调度时，延后后续编排工具，要求下一轮读取真实返回值。
def _deferred_orchestration_result(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name or "unknown",
        False,
        "同一轮已经执行过会创建或改变子代理树的工具调用，"
        "后续编排工具已延后。请先读取上一条工具的真实输出，"
        "下一轮再使用返回的 created_run_ids/actionable_run_ids 调用 dispatch_subagents。",
    )


# LLM: subagent_output_json_response turns the just-written output.json into the final runner contract.
# 函数用途: 读取当前子代理 output.json 并包成 SUBAGENT_RESULT，避免为了收口再发一轮模型请求。
def subagent_output_json_response(agent, fallback: ModelResponse) -> ModelResponse:
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "")
    try:
        task = agent.subagents.load(run_id)
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    payload = _enrich_subagent_output_payload(payload, task)
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到当前子代理已写出 output.json，已结束工具循环并等待父级验收。"
    )
    return ModelResponse(text=text, backend=fallback.backend)


# LLM: _enrich_subagent_output_payload derives traceable packets from files already written by the runner.
# 函数用途: output.json 自动收口时，如果模型漏写 evidence_packets，就用已有报告/产物路径补最小证据包并回写文件。
def _enrich_subagent_output_payload(payload: dict[str, object], task) -> dict[str, object]:
    if _has_traceable_evidence_packets(payload) or _has_malformed_evidence_packets(payload):
        return payload
    evidence_refs, artifact_refs = _derive_output_json_refs(payload, task)
    if not evidence_refs and not artifact_refs:
        return payload
    enriched = dict(payload)
    enriched["evidence_packets"] = [
        {
            "id": _evidence_packet_id(task),
            "claim": _evidence_packet_claim(enriched),
            "checked_scope": "output_json closeout refs",
            "evidence_refs": evidence_refs[:8],
            "artifact_refs": artifact_refs[:8],
            "confidence": 0.7,
        }
    ]
    _write_enriched_output_json(task, enriched)
    return enriched


# LLM: _has_traceable_evidence_packets checks the strict acceptance contract before auto-enrichment.
# 函数用途: 判断 output.json 是否已经有带 refs 的 evidence packet；已有好证据时不改模型结果。
def _has_traceable_evidence_packets(payload: dict[str, object]) -> bool:
    packets = payload.get("evidence_packets")
    if not isinstance(packets, list):
        return False
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        if _string_refs(packet.get("evidence_refs")) or _string_refs(packet.get("artifact_refs")):
            return True
    return False


# LLM: _has_malformed_evidence_packets avoids hiding explicitly bad packets with a synthetic good one.
# 函数用途: 如果模型写了非空但无 refs 的 evidence_packets，保留验收器严格拒绝；只修复完全缺失/空列表场景。
def _has_malformed_evidence_packets(payload: dict[str, object]) -> bool:
    packets = payload.get("evidence_packets")
    if not isinstance(packets, list) or not packets:
        return False
    return not _has_traceable_evidence_packets(payload)


# LLM: _derive_output_json_refs collects only existing refs so auto-enrichment stays evidence-based.
# 函数用途: 从 artifacts/evidence/reports/output.json 中提取真实存在的路径；不读取正文，不虚构产物。
def _derive_output_json_refs(payload: dict[str, object], task) -> tuple[list[str], list[str]]:
    artifact_refs = _payload_path_refs(payload.get("artifacts"))
    evidence_refs = _payload_path_refs(payload.get("evidence"))
    evidence_refs.extend(_task_report_refs(task))
    if artifact_refs or evidence_refs:
        output_ref = _existing_path(getattr(task, "output_json", ""))
        if output_ref:
            evidence_refs.append(output_ref)
    return _unique_strings(evidence_refs), _unique_strings(artifact_refs)


# LLM: _payload_path_refs extracts file refs from model-provided evidence/artifact arrays.
# 函数用途: 遍历结构化数组里的 path/file_path/url 字段，只保留可追溯引用，避免把大正文塞进证据包。
def _payload_path_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        refs.extend(_payload_item_refs(item))
    return refs


# LLM: _payload_item_refs keeps per-item ref extraction shallow for size guards.
# 函数用途: 从单个 evidence/artifact 对象中取 path/file_path/url 引用；坏形态返回空列表。
def _payload_item_refs(item: object) -> list[str]:
    if not isinstance(item, dict):
        return []
    refs: list[str] = []
    for key in ("path", "file_path", "url"):
        ref = _ref_string(item.get(key))
        if ref:
            refs.append(ref)
    return refs


# LLM: _task_report_refs prefers runner-written reports as closeout evidence for coordinator nodes.
# 函数用途: output.json 本身太短时，从当前 task 的 reports 目录找已存在报告作为 evidence_refs。
def _task_report_refs(task) -> list[str]:
    reports_dir_text = str(getattr(task, "reports_dir", "") or "").strip()
    if not reports_dir_text:
        return []
    reports_dir = Path(reports_dir_text)
    if not reports_dir.is_dir():
        return []
    names = (
        "coordinator_report.md",
        "runner_result.json",
        "test_execution.json",
        "acceptance_review.json",
        "parent_acceptance_auto_execution.json",
        "parent_acceptance_decision.json",
        "failure_handoff.json",
        "takeover_readiness.json",
    )
    return [ref for name in names if (ref := _existing_path(reports_dir / name))]


# LLM: _ref_string accepts existing local paths and URLs as compact evidence refs.
# 函数用途: 将候选引用归一化成字符串；本地路径必须真实存在，URL 保留给外部证据。
def _ref_string(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return _existing_path(text)


# LLM: _existing_path resolves filesystem refs without globbing or reading file contents.
# 函数用途: 检查候选路径是否存在；失败时返回空字符串，避免把模型散文当证据。
def _existing_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        return str(path) if path.exists() else ""
    except OSError:
        return ""


# LLM: _evidence_packet_id creates deterministic-enough ids from real task identity.
# 函数用途: 给自动补齐的 evidence packet 一个稳定、短小、可读的 id。
def _evidence_packet_id(task) -> str:
    run_id = str(getattr(task, "id", "") or "run").strip() or "run"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in run_id)
    return f"evpkt-output-json-{safe[:48]}"


# LLM: _evidence_packet_claim keeps auto-generated evidence honest and bounded.
# 函数用途: 从 summary/status 生成简短 claim；没有摘要时说明只是 output.json 收口证据，不替模型夸大完成度。
def _evidence_packet_claim(payload: dict[str, object]) -> str:
    summary = str(payload.get("summary") or "").strip()
    if summary:
        return summary[:200]
    status = str(payload.get("status") or "AWAITING_ACCEPTANCE").strip()
    return f"runner wrote output.json closeout with status={status}"


# LLM: _write_enriched_output_json keeps disk state aligned with the synthetic final model response.
# 函数用途: 将补过 evidence_packets 的 payload 回写 task.output_json，让后续验收读取同一份证据。
def _write_enriched_output_json(task, payload: dict[str, object]) -> None:
    try:
        Path(task.output_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


# LLM: _string_refs normalizes evidence_refs/artifact_refs values from model JSON.
# 函数用途: 接受字符串列表作为 refs；其他形态按空处理，交给验收器继续严格检查。
def _string_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


# LLM: _unique_strings preserves first-seen evidence order while removing duplicates.
# 函数用途: 压缩自动证据包 refs，避免同一路径在 evidence_refs 中重复出现。
def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


# LLM: _append_assistant_tool_round_context protects the next live prompt from large tool payloads.
# 函数用途: 把模型刚生成的工具调用摘要写回 tool_context；大正文只保留长度/hash/预览，不反复塞进后续提示词。
def _append_assistant_tool_round_context(request: ToolRoundExecutionRequest) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(request.response.text, request.calls)
    )
    request.params.tool_context.append(
        f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}"
    )


# LLM: _is_subagent_output_json_write detects the runner's own structured completion artifact.
# 函数用途: 判断本轮工具调用是否成功写入当前子代理的 output.json；只用于提前收敛 runner。
def _is_subagent_output_json_write(agent, payload: object, result: ToolExecutionResult) -> bool:
    if not (result.ok and result.tool == "write_file" and isinstance(payload, dict)):
        return False
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "")
    if not run_id:
        return False
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return False
    return _same_path(_tool_payload_path(payload), getattr(task, "output_json", ""))


# LLM: _tool_payload_path accepts both flat and bundled filesystem tool arguments.
# 函数用途: 识别模型写 output.json 时常见的 path / filesystem.path 两种形态，用于提前收口 runner。
def _tool_payload_path(payload: dict[str, object]) -> object:
    if payload.get("path"):
        return payload.get("path")
    filesystem = payload.get("filesystem")
    if isinstance(filesystem, dict):
        return filesystem.get("path")
    return ""


# LLM: _same_path compares model paths as filesystem literals without glob behavior.
# 函数用途: 将工具入参路径和任务 output_json 路径解析后比较，无法解析时保守返回 False。
def _same_path(left: object, right: object) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except OSError:
        return False
