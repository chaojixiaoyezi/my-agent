from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.tests.test_main_agent_delivery_closeout import _agent


def test_submit_for_acceptance_without_contract_requires_rework_for_current_task_subagents():
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

        # run 出口合同(P1-1)生效后:模型最终回复会被出口检查打回一轮续航
        # (子代理仍 RUNNING、closeout 阻断),第二次出口时进展签名未变即放行,
        # 故 backend 比旧行为多调用一次(3 轮工具上限 + 1 轮续航)。
        assert backend.calls == 4
        assert backend.saw_subagent_rework is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        gate = report["subagent_aggregation_gate"]
        assert gate["allowed"] is False
        assert gate["status"] == "NEED_REPAIR"
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
        assert gate["allowed"] is False
        assert gate["status"] == "NEED_REPAIR"
        assert gate["findings"][0]["code"] == "SUBAGENTS_UNFINISHED"
        assert gate["evidence"]["unfinished_children"][0]["run_id"] == "subagent-completed-alias"


def test_submit_for_acceptance_requires_parent_resolution_for_open_child_capability_request():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "goattack"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        _write_child_canonical_state(
            task_root,
            run_id="subagent-capability-open",
            status="BLOCKED",
            latest_summary="需要父级解锁输出目录。",
            capability_requests=[{"id": "capreq-1", "status": "OPEN", "needed_capability": "write_output"}],
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "requirements.txt")

        _agent(workspace, backend, max_tool_rounds=3).run(
            "写一个 Python 项目。",
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
        assert gate["allowed"] is False
        assert gate["findings"][0]["code"] == "SUBAGENTS_CAPABILITY_REQUESTS_OPEN"
        assert gate["evidence"]["open_capability_request_children"][0]["run_id"] == "subagent-capability-open"
        # A3 结构化软引导钉子:finding 必须直接告诉主代理调什么工具、传哪些 request id
        # (R4b 实测主代理 0 次调用 resolve_capability_requests 的针对性修复)。
        finding_evidence = gate["findings"][0]["evidence"]
        assert finding_evidence["recommended_tool"] == "resolve_capability_requests"
        assert finding_evidence["open_capability_request_ids"] == ["capreq-1"]
        # required_actions 必须用真实工具名,不得诱导模型调用不存在的工具
        assert "resolve_capability_requests" in gate["evidence"]["required_actions"]
        assert "resolve_open_capability_requests" not in gate["evidence"]["required_actions"]
        # 子代理 unresolved/open 投影必须带系统级工具失败账本对照字段(A1)
        assert "tool_failure_codes" in gate["evidence"]["open_capability_request_children"][0]


def test_submit_for_acceptance_allows_cancelled_child_after_parent_resolution():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "goattack"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        _write_child_canonical_state(
            task_root,
            run_id="subagent-cancelled",
            status="CANCELLED",
            latest_summary="父级已取消并接管。",
            capability_requests=[{"id": "capreq-closed", "status": "CLOSED"}],
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "requirements.txt")

        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "写一个 Python 项目。",
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

        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        gate = report["subagent_aggregation_gate"]
        assert gate["allowed"] is True
        assert gate["evidence"]["terminal_statuses"]["CANCELLED"] == 1


def test_submit_for_acceptance_blocks_done_child_with_missing_declared_outputs():
    # R4 形态：子代理 DONE、声明 ~40 个产物实交 1 个，旧链路仍 ok=true。
    # 现在声明对账必须拦住：声明位置缺文件 → SUBAGENTS_DECLARED_OUTPUTS_MISSING。
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "goattack"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        _write_child_canonical_state(
            task_root,
            run_id="subagent-done-empty",
            status="DONE",
            latest_summary="自称完成。",
            attributes={
                "output_files": [
                    str(output_dir / "goattack-python" / "app" / "main.py"),
                    "goattack-python/requirements.txt",
                ]
            },
            task_workspace_dir=str(task_root),
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "requirements.txt")

        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "写一个 Python 项目。",
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

        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        gate = report["subagent_aggregation_gate"]
        assert gate["allowed"] is False
        assert any(f["code"] == "SUBAGENTS_DECLARED_OUTPUTS_MISSING" for f in gate["findings"])
        undelivered = gate["evidence"]["undelivered_children"]
        assert undelivered[0]["run_id"] == "subagent-done-empty"
        assert undelivered[0]["missing_count"] == 2


def test_submit_for_acceptance_allows_done_child_with_delivered_outputs():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        task_root = workspace / "tasks" / "2026-06-03" / "goattack"
        output_dir = task_root / "output"
        work_dir = task_root / "work"
        delivered = output_dir / "goattack-python" / "app" / "main.py"
        delivered.parent.mkdir(parents=True, exist_ok=True)
        delivered.write_text("print('ok')\n", encoding="utf-8")
        _write_child_canonical_state(
            task_root,
            run_id="subagent-done-delivered",
            status="DONE",
            latest_summary="真实交付。",
            attributes={"output_files": [str(delivered)]},
            task_workspace_dir=str(task_root),
        )
        backend = _NoContractTaskOutputReportWithRunningChildBackend(output_dir / "requirements.txt")

        result = _agent(workspace, backend, max_tool_rounds=3).run(
            "写一个 Python 项目。",
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

        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert report["subagent_aggregation_gate"]["allowed"] is True


def _write_child_canonical_state(
    task_root: Path,
    *,
    run_id: str,
    status: str,
    latest_summary: str = "",
    capability_requests: list[dict[str, object]] | None = None,
    attributes: dict[str, object] | None = None,
    task_workspace_dir: str = "",
) -> None:
    path = task_root / "work" / "agents" / run_id / "canonical_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "id": run_id,
        "run_id": run_id,
        "status": status,
        "progress": 0.25,
        "latest_summary": latest_summary,
        "capability_requests": capability_requests or [],
    }
    if attributes is not None:
        payload["attributes"] = attributes
    if task_workspace_dir:
        payload["task_workspace_dir"] = task_workspace_dir
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
        assert "subagent" in prompt
        self.saw_subagent_rework = True
        return ModelResponse(text="收到，需要等待子代理完成后再汇总。", backend=self.name)
