"""LLM: focused tests for dispatch result-ref helpers.

模块用途: 验证 dispatch 给父级的结果索引按 run 分组，避免父级从长路径列表里猜子代理产物名。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.refs import related_task_refs


def test_related_task_refs_prefers_registry_for_artifact_refs(tmp_path):
    stale = tmp_path / "old" / "report.xlsx"
    current = tmp_path / "current" / "report.xlsx"
    tasks = {
        "child-1": SimpleNamespace(
            id="child-1",
            artifact_refs=[str(stale)],
            evidence_refs=[],
            output_json="",
            attributes={
                "artifact_registry_refs": [
                    {
                        "artifact_id": "artifact-report-1",
                        "path": str(current),
                        "status": "ready",
                    },
                ],
            },
        ),
    }
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: tasks[run_id]))
    report = SimpleNamespace(records=[SimpleNamespace(run_id="child-1", runner_created_child_ids=[])])

    refs = related_task_refs(agent, report, "artifact_refs", limit=2)

    assert refs == [str(current)]


def _direct_child_task(artifacts: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id="child-1",
        agent_name="小傻妞-市场",
        role="coordinator",
        status="DONE",
        verification_status="VERIFIED",
        latest_summary="市场报告完成",
        result="",
        artifact_refs=artifacts,
        evidence_refs=["/tmp/market/output.json"],
        output_json="/tmp/market/output.json",
        runner_result_json="/tmp/market/runner_result.json",
        attributes={},
    )


def _grandchild_task() -> SimpleNamespace:
    return SimpleNamespace(
        id="grandchild-1",
        agent_name="小小傻妞-细分",
        role="worker",
        status="DONE",
        verification_status="VERIFIED",
        latest_summary="孙代理报告",
        result="",
        artifact_refs=["/tmp/grandchild/report.md"],
        evidence_refs=[],
        output_json="/tmp/grandchild/output.json",
        runner_result_json="/tmp/grandchild/runner_result.json",
        attributes={},
    )


def _task_index(*tasks: SimpleNamespace) -> dict[str, SimpleNamespace]:
    return {str(task.id): task for task in tasks}


def _expected_child_row() -> dict[str, object]:
    return {
        "run_id": "child-1",
        "agent_name": "小傻妞-市场",
        "role": "coordinator",
        "status": "DONE",
        "summary": "市场报告完成",
        "primary_artifact_ids": [],
        "primary_artifact_refs": ["/tmp/market/final.md"],
        "primary_artifact_registry_refs": [],
        "primary_artifact_summaries": [],
        "evidence_refs": ["/tmp/market/output.json"],
        "run_closeout_ref": "/tmp/market/output.json",
        "runner_result_ref": "/tmp/market/runner_result.json",
    }
