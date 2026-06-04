from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
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


def test_acceptance_submit_does_not_close_when_task_progress_has_open_items(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "已读 001-012，剩余 013-060 未读",
            "next_action": "继续读取 fragment-013 到 fragment-060",
            "items": [
                {"id": "fragment-001", "status": "completed"},
                {"id": "fragment-013", "status": "in_progress"},
                {"id": "fragment-014", "status": "pending"},
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
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["ok"] is False
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_OPEN_ITEMS" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }
    assert payload["failed_gates"][0]["gate"] == "task_progress_closeout"
    assert "继续读取 fragment-013" in payload["repair_guidance"]["message_zh"]


def test_acceptance_submit_requires_done_progress_items_to_have_auditable_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {"id": "fragment-001", "status": "done", "notes": "需要从存档上下文恢复"},
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

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_rejects_incomplete_progress_coverage_even_when_items_done(tmp_path: Path):
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

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_COVERAGE_INCOMPLETE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_treat_plain_evidence_text_as_source_ref(tmp_path: Path):
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

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_requires_final_artifact_to_include_progress_facts(tmp_path: Path):
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
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }
    assert "CPX-001-ABCDEF1234" in payload["repair_guidance"]["message_zh"]


def test_acceptance_submit_requires_progress_facts_to_come_from_source_reads(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("CPX-001-FAKE1234 SVX-001-FAKE5678 HOLD-FAKE90", encoding="utf-8")
    source_artifact = tmp_path / "source-read.json"
    source_artifact.write_text(
        json.dumps(
            {
                "tool": "read_file",
                "content": "fragment-001 contains CPX-001-REAL1234 SVX-001-REAL5678 HOLD-REAL90",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
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
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "source_artifact_ref": str(source_artifact),
                "parameters": {"path": "fragment-001.txt"},
                "output_preview": "fragment-001 contains CPX-001-REAL1234",
            },
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
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }
    assert "CPX-001-FAKE1234" in payload["repair_guidance"]["message_zh"]


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
