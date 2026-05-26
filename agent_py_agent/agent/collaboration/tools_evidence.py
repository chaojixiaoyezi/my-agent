# LLM: Evidence submission tool stores refs-first response packets.
# 模块用途: 实现 submit_evidence 工具，不内联重型日志或业务专项数据。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools import BaseTool, ToolExecutionResult
from .tool_specs import build_submit_evidence_spec
from .tool_values import dict_value, dict_values, error, float_value, ok, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent


class SubmitEvidenceTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_submit_evidence_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("submit_evidence", "case_id_required", "case_id is required")
        evidence = self.agent.collaboration_store.submit_evidence({"case_id": case_id, **_evidence_kwargs(params)})
        return ok("submit_evidence", _evidence_payload(case_id, evidence.evidence_id, params))


def _evidence_kwargs(params: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": str(params.get("request_id") or ""),
        "source_agent_id": str(params.get("source_agent_id") or ""),
        "matched": bool(params.get("matched", False)),
        "summary": str(params.get("summary") or ""),
        "evidence_refs": string_values(params.get("evidence_refs")),
        "queried_scopes": string_values(params.get("queried_scopes")),
        "used_query_hints": string_values(params.get("used_query_hints")),
        "miss_reason": str(params.get("miss_reason") or ""),
        "response_facts": dict_values(params.get("response_facts")),
        "followup_suggestions": dict_values(params.get("followup_suggestions")),
        "query_actions": dict_values(params.get("query_actions")),
        "confidence": float_value(params.get("confidence")),
        "limitations": string_values(params.get("limitations")),
        "metadata": dict_value(params.get("metadata")),
    }


def _evidence_payload(case_id: str, evidence_id: str, params: dict[str, object]) -> dict[str, object]:
    request_id = str(params.get("request_id") or "").strip()
    payload = {"case_id": case_id, "request_id": request_id, "evidence_id": evidence_id, "case_ref": f"collaboration://case/{case_id}", "evidence_ref": f"collaboration://evidence/{evidence_id}"}
    if request_id:
        payload["request_ref"] = f"collaboration://request/{request_id}"
    return payload
