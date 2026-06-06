from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.tests.test_main_agent_delivery_closeout import _agent


def test_submit_for_acceptance_without_contract_warns_for_current_task_subagents():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "all-agent-架构分析"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        _write_child_canonical_state(
            task_root,
            run_id="subagent-running-1",
            status="RUNNING",
            latest_summary="正在分析 langchain 和 langgraph。",
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "final_analysis_report.md")

        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "写一份最终分析报告。",
            params=RunParams(
                save=False,
                task_attributes={
                    "run_workspace": {
                        "task_root": str(task_root),
                        "output_dir": str(output_dir),
                        "work_dir": str(work_dir),
                    }
                },
            ),
        )
        report = _task_closeout_report(task_root)

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert backend.saw_subagent_rework is False
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert report["ok"] is True
        gate = report["subagent_aggregation_gate"]
        assert gate["allowed"] is True
        assert gate["findings"][0]["code"] == "SUBAGENTS_UNFINISHED"
        assert gate["evidence"]["unfinished_children"][0]["run_id"] == "subagent-running-1"


def test_subagent_aggregation_gate_does_not_treat_completed_alias_as_finished():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "all-agent-架构分析"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        _write_child_canonical_state(
            task_root,
            run_id="subagent-completed-alias",
            status="COMPLETED",
            latest_summary="旧状态别名，不应直接当成完成。",
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "final_analysis_report.md")

        _agent(workspace, backend, max_tool_rounds=3).run(
            "写一份最终分析报告。",
            params=RunParams(
                save=False,
                task_attributes={
                    "run_workspace": {
                        "task_root": str(task_root),
                        "output_dir": str(output_dir),
                        "work_dir": str(work_dir),
                    }
                },
            ),
        )
        report = _task_closeout_report(task_root)

        gate = report["subagent_aggregation_gate"]
        assert gate["allowed"] is True
        assert gate["findings"][0]["code"] == "SUBAGENTS_UNFINISHED"
        assert gate["evidence"]["unfinished_children"][0]["run_id"] == "subagent-completed-alias"


def _write_child_canonical_state(task_root: Path, *, run_id: str, status: str, latest_summary: str = "") -> None:
    path = task_root / "work" / "agents" / run_id / "canonical_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": run_id,
                "run_id": run_id,
                "status": status,
                "progress": 0.25,
                "latest_summary": latest_summary,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _task_closeout_report(task_root: Path) -> dict[str, object]:
    return json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))


class _NoContractTaskOutputReportWithRunningChildBackend:
    name = "fake_no_contract_task_output_report_with_running_child_backend"

    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.calls = 0
        self.saw_subagent_rework = False

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps(
                        {
                            "tool": "write_file",
                            "path": str(self.report_path),
                            "content": "# 最终分析报告\n\n已完成。",
                        },
                        ensure_ascii=False,
                    )
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"最终报告已写入 task output，提交验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "subagent-aggregation-check" in prompt
        assert "SUBAGENTS_UNFINISHED" in prompt
        assert "subagent-running-1" in prompt
        self.saw_subagent_rework = True
        return ModelResponse(text="收到，需要等待子代理完成后再汇总。", backend=self.name)
