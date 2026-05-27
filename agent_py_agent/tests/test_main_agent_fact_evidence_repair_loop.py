from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifacts import _existing_report


def test_fact_evidence_repair_loop_blocks_then_accepts_tool_backed_claims(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("finished artifact", encoding="utf-8")
    _write_source_payload(tmp_path, tool_backed=False)
    params = _params(archive_tool_calls=[_write_file_archive_record()])
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    first = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    first_report = _existing_report(tmp_path)

    assert first is not None
    assert first_report["fact_evidence_gate"]["allowed"] is True
    assert first_report["fact_evidence_gate"]["evidence"]["advisory_status"] == "NEED_REPAIR"
    assert "FACT_SOURCE_TOOL_BACKING_MISSING" in first_report["fact_evidence_gate"]["evidence"]["warning_codes"]
    assert first_report.get("contract_recovery") is None
    assert params.tool_context == []

    _write_source_payload(tmp_path, tool_backed=True)
    params.tool_context.clear()
    params.archive_tool_calls.append(_fetch_source_archive_record())
    second = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    second_report = _existing_report(tmp_path)

    assert second is not None
    assert second_report["fact_evidence_gate"]["allowed"] is True
    assert "warning_codes" not in second_report["fact_evidence_gate"]["evidence"]
    assert second_report["final_closeout_gate"]["allowed"] is True
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in second.text


def _write_source_payload(root: Path, *, tool_backed: bool) -> None:
    source: dict[str, object] = {"source_id": "src-1", "uri": "https://example.invalid/data"}
    if tool_backed:
        source["tool_call_ref"] = "call-fetch-1"
    (root / "source_data.json").write_text(
        json.dumps(
            {
                "source_refs": [source],
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
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="生成一份带可核验证据的结果文件，放到 out.txt",
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
            "case_id": "generic-fact-loop",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
            "fact_evidence_payload_ref": "source_data.json",
            "fact_evidence_contract": {
                "evidence_contract": {
                    "required_fields": ["measured_value"],
                    "allowed_value_types": ["exact"],
                    "require_verified": True,
                },
                "require_tool_backed_sources": True,
            },
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


def _fetch_source_archive_record() -> dict[str, object]:
    return {
        "scoped_call_id": "call-fetch-1",
        "tool": "fetch_url",
        "run_id": "run-1",
        "task_id": "task-1",
        "ok": True,
        "parameters": {"tool": "fetch_url", "url": "https://example.invalid/data"},
        "runtime_gate": {
            "allowed": True,
            "status": "ALLOW",
            "evidence": {
                "tool_name": "fetch_url",
                "operation_id": "call-fetch-1",
                "idempotency_key": "idem-fetch-1",
            },
        },
    }
