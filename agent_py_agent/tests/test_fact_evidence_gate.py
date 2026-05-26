from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.contracts.gates.fact_evidence import evaluate_fact_evidence_gate


def test_fact_evidence_gate_blocks_claims_without_tool_backed_sources() -> None:
    decision = evaluate_fact_evidence_gate(
        {
            "source_refs": [{"source_id": "src-1", "uri": "https://example.invalid/data"}],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "field": "measured_value",
                    "value": 42,
                    "source_ids": ["src-1"],
                    "verification_status": "VERIFIED",
                    "value_type": "exact",
                }
            ],
        },
        {
            "evidence_contract": {
                "required_fields": ["measured_value"],
                "allowed_value_types": ["exact"],
                "require_verified": True,
            },
            "require_tool_backed_sources": True,
        },
        archive_tool_calls=[],
    )

    assert decision.allowed is False
    assert decision.gate == "fact_evidence"
    assert "FACT_SOURCE_TOOL_BACKING_MISSING" in decision.finding_codes


def test_fact_evidence_gate_accepts_verified_claims_with_archive_backing() -> None:
    decision = evaluate_fact_evidence_gate(
        {
            "source_refs": [
                {
                    "source_id": "src-1",
                    "uri": "https://example.invalid/data",
                    "tool_call_ref": "call-fetch-1",
                }
            ],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "field": "measured_value",
                    "value": 42,
                    "source_ids": ["src-1"],
                    "verification_status": "VERIFIED",
                    "value_type": "exact",
                }
            ],
        },
        {
            "evidence_contract": {
                "required_fields": ["measured_value"],
                "allowed_value_types": ["exact"],
                "require_verified": True,
            },
            "require_tool_backed_sources": True,
        },
        archive_tool_calls=[{"scoped_call_id": "call-fetch-1", "tool": "fetch_url", "ok": True}],
    )

    assert decision.allowed is True
    assert decision.evidence["source_count"] == 1
    assert decision.evidence["claim_count"] == 1


def test_delivery_closeout_reports_fact_evidence_gate_findings_as_warnings(tmp_path: Path) -> None:
    params = _fact_evidence_closeout_params(tmp_path)
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    progress_events = _progress_events(tmp_path)

    assert response is not None
    assert report["fact_evidence_gate"]["status"] == "ALLOW"
    assert report["fact_evidence_gate"]["evidence"]["warning_codes"]
    assert report["final_closeout_gate"]["allowed"] is True
    assert progress_events[-1]["event_type"] == "delivery_closeout_progress"
    assert progress_events[-1]["ok"] is True


def _fact_evidence_closeout_params(tmp_path: Path):
    (tmp_path / "out.txt").write_text("finished artifact", encoding="utf-8")
    (tmp_path / "source_data.json").write_text(json.dumps(_unbacked_fact_payload(), ensure_ascii=False), encoding="utf-8")
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["fact_evidence_payload_ref"] = "source_data.json"
    params.delivery_contract["fact_evidence_contract"] = {
        "evidence_contract": {
            "required_fields": ["measured_value"],
            "allowed_value_types": ["exact"],
            "require_verified": True,
        },
        "require_tool_backed_sources": True,
    }
    return params


def _unbacked_fact_payload() -> dict[str, object]:
    return {
        "source_refs": [{"source_id": "src-1", "uri": "https://example.invalid/data"}],
        "claims": [{
            "claim_id": "claim-1",
            "field": "measured_value",
            "value": 42,
            "source_ids": ["src-1"],
            "verification_status": "VERIFIED",
            "value_type": "exact",
        }],
    }


def _progress_events(tmp_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (tmp_path / ".agent_delivery" / "progress_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_delivery_closeout_turns_bad_fact_payload_json_into_warning(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("finished artifact", encoding="utf-8")
    (tmp_path / "source_data.json").write_text('{"source_refs": [', encoding="utf-8")
    params = _delivery_closeout_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["fact_evidence_payload_ref"] = "source_data.json"
    params.delivery_contract["fact_evidence_contract"] = {
        "evidence_contract": {"required_fields": ["measured_value"]},
        "require_tool_backed_sources": True,
    }
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["fact_evidence_gate"]["status"] == "ALLOW"
    assert report["fact_evidence_gate"]["evidence"]["warning_codes"] == ["FACT_EVIDENCE_PAYLOAD_MISSING"]


def test_fact_evidence_gate_rejects_estimated_claims_without_methodology() -> None:
    decision = evaluate_fact_evidence_gate(
        {
            "source_refs": [
                {
                    "source_id": "src-1",
                    "uri": "https://example.invalid/data",
                    "tool_call_ref": "call-fetch-1",
                }
            ],
            "claims": [
                {
                    "claim_id": "claim-estimated",
                    "field": "monitored_value",
                    "value": 91,
                    "source_ids": ["src-1"],
                    "verification_status": "VERIFIED",
                    "value_type": "estimated",
                }
            ],
        },
        {
            "evidence_contract": {
                "required_fields": ["monitored_value"],
                "allowed_value_types": ["exact", "estimated"],
                "require_methodology_for_estimates": True,
                "require_verified": True,
            },
            "require_tool_backed_sources": True,
        },
        archive_tool_calls=[{"scoped_call_id": "call-fetch-1", "tool": "query_metric", "ok": True}],
    )

    assert decision.allowed is False
    assert "EVIDENCE_ESTIMATE_METHOD_MISSING" in decision.finding_codes


def _delivery_closeout_params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="make artifact",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=archive_tool_calls,
        delivery_contract={
            "case_id": "generic-artifact",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
        },
    )


def _write_file_archive_record() -> dict[str, object]:
    return {
        "tool": "write_file",
        "run_id": "run-1",
        "task_id": "task-1",
        "ok": True,
        "parameters": {"tool": "write_file", "path": "out.txt"},
        "runtime_gate": {
            "allowed": True,
            "status": "ALLOW",
            "evidence": {
                "tool_name": "write_file",
                "operation_id": "op-write-1",
                "idempotency_key": "idem-write-1",
            },
        },
    }
