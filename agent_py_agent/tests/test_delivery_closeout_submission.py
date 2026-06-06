from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_closeout.closeout import _unique_archive_tool_calls
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.agent_core.tool_loop.repair_counters import ToolLoopRepairCounters
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends import ModelResponse


def test_tool_round_without_acceptance_submit_does_not_run_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("read_file")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL read_file]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_tool_round_with_acceptance_submit_runs_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    assert (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_acceptance_submit_uses_disk_write_file_index_for_artifact_provenance(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "write_file",
                "run_id": "run-1",
                "task_id": "task-1",
                "call_id": "write-out",
                "scoped_call_id": "run-1:write-out",
                "sha256": "write-out-sha",
                "parameters": {"tool": "write_file", "path": "out.txt"},
                "source_input": "write_file",
            }
        ],
    )
    params = _delivery_params(archive_tool_calls=[])
    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert "交付验收通过" in response.text
    assert report["runtime_gate"]["allowed"] is True
    assert report["artifacts"][0]["provenance"]["proof_kind"] == "tool_output_index"


def test_closeout_archive_dedup_ignores_records_without_structured_identity():
    records = [
        {"tool": "write_file", "path": "out.txt", "created_at": "1"},
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "read_file", "scoped_call_id": "run-1:read-1"},
    ]

    assert _unique_archive_tool_calls(records) == [
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "read_file", "scoped_call_id": "run-1:read-1"},
    ]


def test_acceptance_submit_does_not_block_materializer_warning_when_contract_is_valid(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["_contract_doctor"] = {
        "schema_version": "delivery_contract_doctor.v1",
        "ok": True,
        "should_rematerialize": True,
        "findings": [
            {
                "code": "DELIVERY_MATERIALIZER_SOURCE_COVERAGE_UNDECLARED",
                "severity": "warning",
                "location": "source_paths",
                "message": "source coverage not declared",
                "value": "data/long_field_journal.txt",
            }
        ],
        "repair_actions": [
            {
                "code": "DELIVERY_CONTRACT_REMATERIALIZATION_REQUIRED",
                "recommended_action": "rematerialize_delivery_contract_with_source_coverage_decision",
                "source_paths": ["data/long_field_journal.txt"],
            }
        ],
    }
    params.executed_tools.append("submit_for_acceptance")

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert "交付验收通过" in response.text
    assert report["ok"] is True


def test_acceptance_submit_blocks_when_required_source_read_is_partial(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("x" * 1000, encoding="utf-8")
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": str(source), "offset": 0, "max_chars": 100},
                "output_preview": "[char-window offset=0 chars=100 total_chars=1000]\nPARTIAL view only",
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )
    response, report, _ = _submit_acceptance(tmp_path, params)
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["runtime_gate"]["allowed"] is False
    assert "TARGET_COVERAGE_MISSING" in {
        finding["code"] for finding in report["runtime_gate"]["findings"]
    }
    assert "不能替代完整阅读证明" in payload["repair_guidance"]["message_zh"]
    assert "recommended_tool_call" in payload["repair_guidance"]["message_zh"]


def test_acceptance_submit_blocks_when_item_level_required_source_read_is_partial(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("x" * 1000, encoding="utf-8")
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": str(source), "offset": 0, "max_chars": 100},
                "output_preview": "[char-window offset=0 chars=100 total_chars=1000]\nPARTIAL view only",
            },
            {
                "tool": "search_text",
                "ok": True,
                "parameters": {"tool": "search_text", "path": str(source), "query": "x"},
                "output_preview": "matches found",
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "target_items": [
                    {
                        "target_id": str(source),
                        "source_ref": str(source),
                        "enforcement": "required",
                        "coverage_kind": "full_source_read",
                    }
                ],
            },
        },
    )
    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is None
    assert report["target_coverage_status"]["enforcement"] == "required"
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["target_coverage_status"]["should_block"] is True


def test_acceptance_submit_uses_disk_tool_output_index_for_coverage_after_compact(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {line}" for line in range(1, 1001)), encoding="utf-8")
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "call_id": "read-full-window",
                "scoped_call_id": "run-1:read-full-window",
                "sha256": "read-full-window-sha",
                "parameters": {"tool": "read_file", "path": str(source), "start_line": 1, "end_line": 500},
                "source_input": str(source),
            }
        ],
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "parameters": {"tool": "read_file", "path": str(source)},
                "output_preview": "\n".join(f"{line}: line {line}" for line in range(1, 27)),
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["target_coverage_status"]["repair_hints"][0]["covered_until_line"] == 500
    assert report["target_coverage_status"]["repair_hints"][0]["recommended_tool_call"] == {
        "tool": "read_file",
        "path": str(source),
        "start_line": 501,
    }


def test_acceptance_submit_ignores_disk_tool_output_index_from_other_run(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {line}" for line in range(1, 11)), encoding="utf-8")
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "old-run",
                "task_id": "old-task",
                "call_id": "old-read-full",
                "scoped_call_id": "old-run:old-read-full",
                "sha256": "old-read-full-sha",
                "parameters": {"tool": "read_file", "path": str(source)},
                "output_preview": "\n".join(f"{line}: line {line}" for line in range(1, 11)),
            }
        ],
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )

    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["runtime_gate"]["allowed"] is False


def test_acceptance_submit_warns_when_task_progress_has_open_items(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "已读 001-012，剩余 013-060 未读",
            "next_action": "继续读取 fragment-013 到 fragment-060",
            "items": [
                {"id": "fragment-001", "status": "done"},
                {"id": "fragment-013", "status": "in_progress"},
                {"id": "fragment-014", "status": "pending"},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    response, report, payload = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert report["ok"] is True
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_OPEN_ITEMS" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }
    assert payload.get("failed_gates", []) == []


def test_acceptance_submit_keeps_completed_progress_items_open(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "全部片段已完成",
            "next_action": "提交验收",
            "items": [
                {"id": "fragment-001", "status": "completed", "evidence": ["fragment-001.txt line 12"]},
                {"id": "fragment-002", "status": "completed", "evidence": ["fragment-002.txt line 18"]},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    response, report, _payload = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert report["task_progress_closeout_gate"]["evidence"]["counts"] == {"total": 2, "other": 2}
    assert "TASK_PROGRESS_OPEN_ITEMS" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_warns_when_done_progress_items_lack_auditable_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {"id": "fragment-001", "status": "done"},
                {"id": "fragment-002", "status": "done", "evidence": ["fragment-002.txt line 12"]},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_warns_on_incomplete_progress_coverage_even_when_items_done(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "模型误以为可以提交验收。",
            "next_action": "提交验收",
            "items": [{"id": "fragment-001", "status": "done", "evidence": ["fragment-001.txt:12"]}],
            "coverage": {
                "goal": "读取两个片段并写入报告。",
                "targets": [
                    {
                        "id": "fragment-001",
                        "status": "done",
                        "checks": {"读取": "done", "写报告": "done"},
                        "evidence": ["fragment-001.txt:12"],
                    },
                    {
                        "id": "fragment-002",
                        "status": "pending",
                        "checks": {"读取": "pending", "写报告": "pending"},
                    },
                ],
            },
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_COVERAGE_INCOMPLETE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_parse_plain_evidence_text_as_closeout_fact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-014",
                    "status": "done",
                    "evidence": ["输入目录不存在"],
                }
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_require_final_artifact_to_include_progress_note_tokens(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("finished artifact without expected facts", encoding="utf-8")
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-001",
                    "status": "done",
                    "evidence": ["CP_CODE=CPX-001-ABCDEF1234", "SECRET_VALUE=SVX-001-1234ABCDEF"],
                }
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    response, report, _payload = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_scan_artifact_text_for_task_progress_fact_tokens(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "真实编号 CP-001-037；错误编号 CP-001-001",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-001",
                    "status": "done",
                    "notes": "检查点=CP-001-037",
                    "evidence": ["source.txt@offset=0"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0, "max_chars": 1000},
                "output_preview": "源文只出现真实编号 CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_parse_checkpoint_facts_from_progress_notes(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节001 南京 继续观察 审批延迟 CP-001",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "章节001",
                    "title": "南京现场复盘",
                    "status": "done",
                    "notes": "地点:南京;最终决定:继续观察;风险词:审批延迟;检查点:CP-001-037",
                    "evidence": ["source.txt@offset=0"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0},
                "output_preview": "章节001 地点:南京;最终决定:继续观察;风险词:审批延迟;检查点:CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_block_short_checkpoint_token_not_seen_in_source(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节080 北京 CP-080-966\n章节081 南京 CP-081",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "章节080",
                    "status": "done",
                    "notes": "地点:北京;检查点:CP-080-966",
                    "evidence": ["source.txt@offset=2050000"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 2050000},
                "output_preview": "源文最后一个编号 CP-080-966；后面没有下一章。",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_leaves_artifact_source_fact_validation_to_structured_contracts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "真实编号 CP-080-966；错误编号 CP-081-003",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-080",
                    "status": "done",
                    "evidence": ["source.txt@offset=2050000"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 2050000},
                "output_preview": "源文最后一个编号 CP-080-966；没有下一章。",
            },
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "out.txt"},
                "output_preview": "读回报告：错误编号 CP-081-003",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_check_artifact_fact_tokens_against_externalized_source_read(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("错误编号 CP-027-999", encoding="utf-8")
    source_blob = tmp_path / "read-source.json"
    source_blob.write_text(
        json.dumps({"content": "源文真实编号 CP-027-002"}, ensure_ascii=False),
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-027",
                    "status": "done",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "path": str(source_blob),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_infer_dense_numeric_progress_gap_from_item_names(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    items = [
        {
            "id": f"ch{index:03d}",
            "status": "done",
            "notes": f"检查点=CP-{index:03d}-OKAY",
            "evidence": [f"source.txt@ch{index:03d}"],
        }
        for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]
    ]
    (tmp_path / "out.txt").write_text(
        "\n".join(f"CP-{index:03d}-OKAY" for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]),
        encoding="utf-8",
    )
    write_task_progress(tmp_path, "run-1", {"items": items})
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0, "max_chars": 1000},
                "output_preview": " ".join(f"CP-{index:03d}-OKAY" for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    finding_codes = {finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]}
    assert "TASK_PROGRESS_NUMERIC_SEQUENCE_GAP" not in finding_codes


def test_acceptance_submit_counts_bare_numeric_progress_items_in_neighbor_sequence(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    indices = list(range(37, 45))
    (tmp_path / "out.txt").write_text(
        "\n".join(f"ch{index:03d} {_checkpoint(index)}" for index in indices),
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                _numeric_progress_item(index, bare=40 <= index <= 42)
                for index in indices
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 900000},
                "output_preview": " ".join(_checkpoint(index) for index in indices),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_NUMERIC_SEQUENCE_GAP"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_plain_task_progress_facts_in_final_artifact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "ch002 错误汇总：武汉 / 补充证据 / CP-002-074",
        encoding="utf-8",
    )
    source_content = "ch002 地点：西安；最终决定：暂停发布；风险词：审批延迟；检查点编号：CP-002-074"
    source_artifact = _write_json_source_artifact(
        tmp_path / "source-read.json",
        source_content,
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "ch002",
                    "status": "done",
                    "result": "地点：西安；最终决定：暂停发布；风险词：审批延迟；检查点编号：CP-002-074",
                    "evidence": ["source-read.json"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _read_file_archive(source_artifact, "long_field_journal.txt", source_content),
            _write_file_archive_record(),
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_splits_chinese_sentence_key_value_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节 017：最终决定 暂停发布，风险词 凭证缺口，检查点 CP-017-629。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "ch017",
                    "status": "done",
                    "notes": "最终决定：暂停发布。风险词：凭证缺口。检查点编号：CP-017-629",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "章节 017 最终决定：暂停发布；风险词：凭证缺口；检查点编号：CP-017-629",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] in {
            "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT",
            "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED",
        }
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_ignores_operational_cursor_facts_in_task_progress(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "报告已按章节整理完成，没有复述每个中间读取 offset。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "read-window-001",
                    "status": "done",
                    "result": "已读取offset=250000；待读取范围=250000~300000；覆盖字符=300000",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 250000},
                "output_preview": "第六段读取窗口，业务内容已归纳。",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] in {
            "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT",
            "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED",
        }
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_ignores_placeholder_fact_tokens_in_final_artifact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节 001 使用检查点 CP-001-037；检查点格式示例为 CP-XXX-YYY。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "001",
                    "status": "done",
                    "notes": "检查点编号：CP-001-037",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "章节 001；检查点编号：CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_complete_artifact_sequence_from_text(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    rows = []
    items = []
    source_facts = []
    for index in range(5, 15):
        cp = f"CP-{index:03d}-{index * 37 % 997:03d}"
        rows.append(f"| {index:03d} | 城市 | {cp} | 风险 | 决定 |")
        items.append(
            {
                "id": f"{index:03d}",
                "status": "done",
                "notes": f"检查点编号：{cp}",
                "evidence": ["source.txt"],
            }
        )
        source_facts.append(cp)
    (tmp_path / "out.txt").write_text(
        "# 报告\n\n## 完整章节清单（共 14 项）\n\n"
        "| 章节 | 城市 | 检查点 | 风险词 | 最终决定 |\n"
        "|------|------|--------|--------|----------|\n"
        + "\n".join(rows),
        encoding="utf-8",
    )
    write_task_progress(tmp_path, "run-1", {"items": items})
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": " ".join(source_facts),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_NUMERIC_SEQUENCE_GAP" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_treat_repair_notes_as_required_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("修正后编号 CP-054-004", encoding="utf-8")
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "cp-fix",
                    "status": "done",
                    "notes": "报告曾经使用 CP-054-001，已改回账本编号 CP-054-004",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "源文真实编号 CP-054-004",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_progress_facts_to_validate_source_reads(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("CPX-001-FAKE1234 SVX-001-FAKE5678 HOLD-FAKE90", encoding="utf-8")
    source_artifact = _write_json_source_artifact(
        tmp_path / "source-read.json",
        "fragment-001 contains CPX-001-REAL1234 SVX-001-REAL5678 HOLD-REAL90",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-001",
                    "status": "done",
                    "evidence": [
                        "fragment-001.txt line 12",
                        "CPX-001-FAKE1234",
                        "SVX-001-FAKE5678",
                        "HOLD-FAKE90",
                    ],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _read_file_archive(source_artifact, "fragment-001.txt", "fragment-001 contains CPX-001-REAL1234"),
            _write_file_archive_record(),
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_background_wait_tool_round_yields_without_continuing_tool_loop(tmp_path: Path):
    params = replace(_delivery_params(archive_tool_calls=[]), source="background_main_agent")
    params.executed_tools.append("wait")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL wait]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "不继续轮询" in response.text
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_cli_run_wait_tool_round_does_not_finish_task(tmp_path: Path):
    params = replace(_delivery_params(archive_tool_calls=[]), source="cli_run")
    params.executed_tools.append("wait")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL wait]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_no_tool_final_answer_does_not_submit_delivery_acceptance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


def test_failed_closeout_context_includes_unified_repair_guidance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    payload = _last_tool_context_payload(params)

    assert response is None
    assert payload["ok"] is False
    assert payload["repair_guidance"]["mode"] == "closeout_rework"
    assert payload["repair_guidance"]["required_actions"]
    assert "如果还需要读取或搜索" in payload["repair_guidance"]["message_zh"]


def test_submit_with_contract_but_no_required_artifact_returns_rework_context(tmp_path: Path):
    params = replace(
        _delivery_params(archive_tool_calls=[]),
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "case_id": "missing-artifact",
            "artifacts": [],
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["reason"] == "required_artifacts_missing"
    assert payload["repair_guidance"]["mode"] == "closeout_rework"
    assert "没有 required artifact" in payload["repair_guidance"]["message_zh"]


def test_tool_loop_service_does_not_replay_existing_failed_closeout_context():
    source = Path("agent_py_agent/agent/agent_core/_tool_loop_service.py").read_text(
        encoding="utf-8"
    )

    assert "append_existing_failed_closeout_context" not in source


def test_no_tool_working_text_does_not_submit_delivery(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="我继续阅读更多项目，然后再汇总写报告。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


def test_existing_closeout_context_does_not_make_plain_text_submit(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.tool_context.append("[delivery-contract-check]\n{}")
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert params.tool_context == ["[delivery-contract-check]\n{}"]


def test_failed_closeout_does_not_intercept_read_tools_with_delivery_repair_gate(tmp_path: Path):
    _write_builder_ready_closeout(tmp_path)
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent_with_calls(tmp_path, [{"tool": "list_files", "path": "outputs/report"}])

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_LIST", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "list_files", "path": "outputs/report"}]
    assert not any("delivery-required-repair" in item for item in params.tool_context)


def test_no_tool_final_answer_does_not_close_when_valid_without_submit(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件在 out.txt。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件在 out.txt。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_message_delivery_contract_closes_without_artifact_ref_failure(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="已经检查完，结论直接回复给你。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["delivery_mode"] == "message"
    assert report["artifacts"] == []
    assert report["runtime_gate"]["allowed"] is True


def test_requires_artifact_false_contract_closes_without_artifacts_field(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[], contract={"case_id": "answer-only", "requires_artifact": False})
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="检查完成，不需要生成文件。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text


def _agent(root: Path):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: []),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _agent_with_calls(root: Path, calls: list[dict[str, object]]):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: list(calls)),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _write_valid_artifact(root: Path) -> None:
    (root / "out.txt").write_text("finished artifact", encoding="utf-8")


def _write_tool_output_index(root: Path, rows: list[dict[str, object]]) -> None:
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_builder_ready_closeout(root: Path) -> None:
    delivery_dir = root / ".agent_delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    (delivery_dir / "closeout.json").write_text(
        json.dumps(
            {
                "ok": False,
                "delivery_progress": {
                    "recovery_actions": [
                        {
                            "code": "STAGING_BUILDER_READY",
                            "recommended_action": "invoke_builder_tool",
                            "builder_tool": "write_file",
                            "source_ref": "outputs/report/source.json",
                            "output_ref": "outputs/report/report.xlsx",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _last_tool_context_payload(params: ToolLoopExecuteParams) -> dict[str, object]:
    return json.loads(params.tool_context[-1].split("\n", 1)[1])


def _submit_acceptance(
    root: Path,
    params: ToolLoopExecuteParams,
) -> tuple[object, dict[str, object], dict[str, object]]:
    params.executed_tools.append("submit_for_acceptance")
    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=_agent(root),
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    payload = _last_tool_context_payload(params) if params.tool_context else {}
    return response, report, payload


def _checkpoint(index: int) -> str:
    return f"CP-{index:03d}-{index * 37 % 997:03d}"


def _numeric_progress_item(index: int, *, bare: bool = False) -> dict[str, object]:
    item_id = f"{index:03d}" if bare else f"ch{index:03d}"
    return {
        "id": item_id,
        "title": item_id,
        "status": "done",
        "evidence": [_checkpoint(index)],
    }


def _write_json_source_artifact(path: Path, content: str) -> Path:
    path.write_text(
        json.dumps({"tool": "read_file", "content": content}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _read_file_archive(source_artifact: Path, source_path: str, output_preview: str) -> dict[str, object]:
    return {
        "tool": "read_file",
        "ok": True,
        "run_id": "run-1",
        "source_artifact_ref": str(source_artifact),
        "parameters": {"path": source_path},
        "output_preview": output_preview,
    }


def _delivery_params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
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


def _message_delivery_params(
    *,
    archive_tool_calls: list[dict[str, object]],
    contract: dict[str, object] | None = None,
) -> ToolLoopExecuteParams:
    params = _delivery_params(archive_tool_calls=archive_tool_calls)
    return replace(
        params,
        delivery_contract=dict(
            contract
            or {
                "case_id": "message-only",
                "delivery_mode": "message",
                "artifacts": [],
            }
        ),
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
