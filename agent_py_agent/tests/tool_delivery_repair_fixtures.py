from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _delivery_repair_agent(root: Path, calls: list[dict[str, object]] | None = None) -> SimpleNamespace:
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str) -> list[dict[str, object]]:
            return list(calls or [])

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
        tools=_Tools(),
    )


def _tool_loop_params(delivery_contract: dict[str, object] | None = None):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="test",
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
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract=delivery_contract or _workbook_delivery_contract(),
    )


def _response_decision(agent: object, params: object, *, text: str = "CALL"):
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text=text, backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )


def _write_ready_source(root: Path) -> None:
    source = root / "outputs/report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "sheets": [
                    {
                        "name": "week",
                        "columns": ["项目名", "地址"],
                        "rows": [{"项目名": "demo", "地址": "https://example.com"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _failed_builder_closeout(root: Path) -> None:
    _write_closeout(
        root,
        {
            "ok": False,
            "artifacts": [
                {
                    "artifact_id": "report",
                    "kind": "xlsx",
                    "path": str(root / "outputs/report/report.xlsx"),
                    "ok": False,
                    "acceptance_report": {
                        "ok": False,
                        "findings": [{"code": "XLSX_REQUIRED_COLUMN_EMPTY_VALUES", "value": "项目名"}],
                    },
                }
            ],
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_path": str(root / "outputs/report/report.xlsx"),
                    }
                ]
            },
        },
    )


def _workbook_delivery_contract() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "report",
                "kind": "xlsx",
                "preferred_path": "outputs/report/report.xlsx",
                "validation_contract": {
                    "required_columns": ["项目名", "地址"],
                    "required_sheets_min": 1,
                    "staging_contract": {
                        "builder_tool": "data_to_workbook",
                        "source_json_ref": "outputs/report/source_data.json",
                        "workbook_ref": "outputs/report/report.xlsx",
                        "checkpoint_refs": [
                            "outputs/report/source_data.json",
                            "outputs/report/report.xlsx",
                        ],
                    },
                },
            }
        ]
    }


def _builder_call() -> dict[str, object]:
    return {
        "tool": "data_to_workbook",
        "path": "outputs/report/report.xlsx",
        "source_json_path": "outputs/report/source_data.json",
    }


def _evidence_repair_closeout() -> dict[str, object]:
    return {
        "ok": False,
        "delivery_progress": {
            "recovery_actions": [
                {
                    "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                    "recommended_action": "repair_evidence_refs",
                    "checkpoint_ref": "outputs/report/source_data.json",
                    "required_fields": ["项目名", "地址"],
                    "writer_tool": "write_structured_json",
                }
            ]
        },
    }


def _structure_and_evidence_repair_closeout() -> dict[str, object]:
    closeout = _evidence_repair_closeout()
    actions = closeout["delivery_progress"]["recovery_actions"]
    actions.insert(
        0,
        {
            "code": "STAGED_JSON_TOO_FEW_SHEETS",
            "recommended_action": "repair_structured_checkpoint_json",
            "checkpoint_ref": "outputs/report/source_data.json",
            "required_columns": ["项目名", "地址"],
            "writer_tool": "write_structured_json",
        },
    )
    return closeout


def _sheet_only_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
    }


def _structured_evidence_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "merge_existing": True,
        "data": {
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [
                {"field": "项目名", "value": "demo", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
                {"field": "地址", "value": "https://example.com", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
            ],
        },
    }


def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
