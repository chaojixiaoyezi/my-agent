# LLM: Orchestration-contract response decisions keep explicit collaboration requests from being bypassed.
# 模块用途: 用户明确要求子代理/协作时，最终回答前校验真实编排工具事实，不让 root 直接口头收口。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..backend import ModelResponse
from .tool_loop_repair_counters import ToolLoopRepairCounters, _inc_orchestration_contract


@dataclass(frozen=True)
class OrchestrationContractDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class OrchestrationContractDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters


def orchestration_contract_no_tool_call_decision(
    request: OrchestrationContractDecisionRequest,
) -> OrchestrationContractDecision | None:
    contract = orchestration_contract_from_params(request.params)
    if not _requires_orchestration(contract):
        return None
    missing = missing_orchestration_requirements(request.agent, request.params, contract)
    if not missing:
        return None
    if request.counters.orchestration_contract_redirects < _rework_budget(contract):
        request.params.tool_context.append(_rework_context(contract, missing))
        return OrchestrationContractDecision(
            "continue",
            None,
            [],
            _inc_orchestration_contract(request.counters),
        )
    return OrchestrationContractDecision(
        "break",
        _blocked_response(request.response, missing),
        [],
        request.counters,
    )


def orchestration_contract_from_params(params: object) -> dict[str, object]:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get("orchestration_contract"), dict):
        return dict(attrs["orchestration_contract"])
    contract = getattr(params, "delivery_contract", None)
    if isinstance(contract, dict) and isinstance(contract.get("orchestration_contract"), dict):
        return dict(contract["orchestration_contract"])
    return {}


def missing_orchestration_requirements(
    agent: object,
    params: object,
    contract: dict[str, object],
) -> list[dict[str, object]]:
    missing: list[dict[str, object]] = []
    executed = {str(item).strip() for item in getattr(params, "executed_tools", []) or [] if str(item).strip()}
    required_tools = _required_tools(contract)
    for tool in required_tools:
        if tool not in executed:
            missing.append({"kind": "missing_tool", "tool": tool})
    minimum = _minimum_subagent_count(contract, required_tools)
    created_count = _created_subagent_count(agent, params)
    if minimum > 0 and created_count < minimum:
        missing.append(
            {
                "kind": "minimum_subagent_count",
                "required": minimum,
                "actual": created_count,
            }
        )
    return missing


def _requires_orchestration(contract: dict[str, object]) -> bool:
    return _boolish(contract.get("requires_orchestration")) or bool(_required_tools(contract))


def _required_tools(contract: dict[str, object]) -> list[str]:
    tools = _text_list(contract.get("required_tools"))
    if not tools:
        tools = _text_list(contract.get("required_actions"))
    if not tools and _boolish(contract.get("requires_orchestration")):
        tools = ["create_subagents"]
    if tools and _execution_required(contract) and "dispatch_subagents" not in tools:
        tools.append("dispatch_subagents")
    return tools


def _minimum_subagent_count(contract: dict[str, object], required_tools: list[str]) -> int:
    if "minimum_subagent_count" in contract:
        return _non_negative_int(contract.get("minimum_subagent_count"), default=0)
    return 1 if "create_subagents" in required_tools else 0


def _created_subagent_count(agent: object, params: object) -> int:
    count = _created_subagent_count_from_archive(getattr(params, "archive_tool_calls", []) or [])
    if count > 0:
        return count
    listed = _listed_subagent_count(agent)
    if listed > 0:
        return listed
    executed = getattr(params, "executed_tools", []) or []
    return 1 if "create_subagents" in executed else 0


def _created_subagent_count_from_archive(records: list[object]) -> int:
    count = 0
    for record in records:
        if not isinstance(record, dict) or record.get("tool") != "create_subagents" or record.get("ok") is False:
            continue
        payload = _archive_output_payload(record)
        if not payload:
            count = max(count, 1)
            continue
        count = max(count, _payload_created_count(payload))
    return count


def _archive_output_payload(record: dict[str, object]) -> dict[str, object]:
    payload = _json_object(record.get("output_preview"))
    if payload:
        return payload
    path = str(record.get("output_path") or "").strip()
    if not path:
        return {}
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _json_object(artifact.get("content"))


def _payload_created_count(payload: dict[str, object]) -> int:
    values = [
        _non_negative_int(payload.get("created"), default=0),
        len(_text_list(payload.get("ids"))),
        len(_text_list(payload.get("created_run_ids"))),
        len(payload.get("tasks")) if isinstance(payload.get("tasks"), list) else 0,
    ]
    return max(values)


def _listed_subagent_count(agent: object) -> int:
    manager = getattr(agent, "subagents", None)
    list_runs = getattr(manager, "list_runs", None)
    if not callable(list_runs):
        return 0
    try:
        return len(list_runs())
    except (OSError, TypeError, ValueError, AttributeError):
        return 0


def _rework_context(contract: dict[str, object], missing: list[dict[str, object]]) -> str:
    required_tools = ", ".join(_required_tools(contract)) or "create_subagents"
    missing_text = json.dumps(missing, ensure_ascii=False, sort_keys=True)
    return "\n".join(
        [
            "[tool-system orchestration-contract-rework]",
            "这不是任务终止，是协作返工。",
            f"missing: {missing_text}",
            f"required_tools: {required_tools}",
            "用户已明确要求通过子代理/协作完成；不能直接用 root 自己读完、写完或口头说完成。",
            "请先调用真实 orchestration 工具满足缺口，例如 create_subagents，再按工具返回的 next_action 调度或查看状态。",
            "如果上次工具参数格式不对，请按 Tool Catalog 的 schema 重新调用；如果工具确实不可用，请给出 BLOCKED 和真实工具错误。",
        ]
    )


def _blocked_response(response: object, missing: list[dict[str, object]]) -> ModelResponse:
    backend = str(getattr(response, "backend", "") or "")
    text = (
        "[ORCHESTRATION_CONTRACT_BLOCKED] 用户明确要求协作/子代理，但本轮没有满足编排合同。"
        f" missing={json.dumps(missing, ensure_ascii=False, sort_keys=True)}。"
        "请换策略后重新发起或恢复任务。"
    )
    return ModelResponse(
        text=text,
        backend=backend,
        runtime_status="blocked",
        runtime_reason="ORCHESTRATION_CONTRACT_UNSATISFIED",
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def _text_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _rework_budget(contract: dict[str, object]) -> int:
    return _non_negative_int(contract.get("rework_budget"), default=2)


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "required"}


def _execution_required(contract: dict[str, object]) -> bool:
    value = contract.get("execution_required")
    if isinstance(value, bool):
        return value is not False
    if isinstance(value, int | float):
        return value != 0
    text = str(value or "").strip().casefold()
    return text not in {"0", "false", "no", "n", "off", "disabled"}


__all__ = [
    "OrchestrationContractDecision",
    "OrchestrationContractDecisionRequest",
    "missing_orchestration_requirements",
    "orchestration_contract_from_params",
    "orchestration_contract_no_tool_call_decision",
]
