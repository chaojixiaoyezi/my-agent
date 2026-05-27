"""LLM: focused tests for dispatch result-ref helpers.

模块用途: 验证 dispatch 给父级的结果索引按 run 分组，避免父级从长路径列表里猜子代理产物名。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_dispatch_refs import (
    related_task_refs,
    related_task_result_refs,
)


# LLM: test_related_task_result_refs_uses_direct_runs_only protects parent handoff shape.
# 函数用途: 只按直接子代理生成主产物 refs；孙代理产物仍可作为证据，但不抢占父级最终汇总入口。
def test_related_task_result_refs_uses_direct_runs_only():
    tasks = _task_index(
        _direct_child_task(["/tmp/market/final.md", "/tmp/market/detail.md"]),
        _grandchild_task(),
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: tasks[run_id]))
    report = SimpleNamespace(records=[
        SimpleNamespace(run_id="child-1", runner_created_child_ids=["grandchild-1"]),
        SimpleNamespace(run_id="child-1", runner_created_child_ids=[]),
    ])

    refs = related_task_result_refs(agent, report, per_run_artifact_limit=1)

    assert refs == [_expected_child_row()]


# LLM: test_related_task_result_refs_recovers_output_artifact_summaries captures the 长期助手 handoff.
# 函数用途: task.artifact_refs 为空时，父级仍能从 output.json 得到主产物路径和短摘要，减少读取整篇报告。
def test_related_task_result_refs_recovers_output_artifact_summaries(tmp_path):
    output_json = tmp_path / "output.json"
    report = tmp_path / "entry_strategy_report.md"
    output_json.write_text(
        json.dumps(
            {
                "message": "runner closeout",
                "artifacts": [
                    {"kind": "report", "path": str(report), "summary": "进入策略报告，含渠道、定价和6个月计划"},
                ],
                "evidence_packets": [
                    {"claim": "策略报告完成", "artifact_refs": [str(report)]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tasks = {
        "child-1": SimpleNamespace(
            id="child-1",
            agent_name="小傻妞-策略",
            role="coordinator",
            status="DONE",
            verification_status="VERIFIED",
            latest_summary="",
            result="",
            artifact_refs=[],
            evidence_refs=[],
            output_json=str(output_json),
            runner_result_json="",
        ),
    }
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: tasks[run_id]))
    dispatch_report = SimpleNamespace(records=[SimpleNamespace(run_id="child-1", runner_created_child_ids=[])])

    refs = related_task_result_refs(agent, dispatch_report, per_run_artifact_limit=2)

    assert refs[0]["summary"] == "runner closeout"
    assert refs[0]["primary_artifact_refs"] == [str(report)]
    assert refs[0]["primary_artifact_summaries"] == [{
        "path": str(report),
        "kind": "report",
        "summary": "进入策略报告，含渠道、定价和6个月计划",
    }]


# LLM: registry records are the parent-facing source of truth when they exist.
# 函数用途: 子代理移动/重建产物后，父级 result refs 应优先拿 registry 的 artifact_id/path，而不是旧 task.artifact_refs。
def test_related_task_result_refs_prefers_registry_over_stale_task_ref(tmp_path):
    stale = tmp_path / "old" / "report.xlsx"
    current = tmp_path / "current" / "report.xlsx"
    registry_record = {
        "artifact_id": "artifact-report-1",
        "path": str(current),
        "kind": "xlsx",
        "status": "ready",
    }
    tasks = {
        "child-1": SimpleNamespace(
            id="child-1",
            agent_name="小傻妞-周报",
            role="worker",
            status="DONE",
            verification_status="VERIFIED",
            latest_summary="汇总表已生成",
            result="",
            artifact_refs=[str(stale)],
            evidence_refs=[],
            output_json="",
            runner_result_json="",
            attributes={"artifact_registry_refs": [registry_record]},
        ),
    }
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: tasks[run_id]))
    dispatch_report = SimpleNamespace(records=[SimpleNamespace(run_id="child-1", runner_created_child_ids=[])])

    refs = related_task_result_refs(agent, dispatch_report, per_run_artifact_limit=2)

    assert refs[0]["primary_artifact_ids"] == ["artifact-report-1"]
    assert refs[0]["primary_artifact_refs"] == [str(current)]
    assert refs[0]["primary_artifact_registry_refs"] == [registry_record]


# LLM: Flat deliverable refs should also follow the registry when it exists.
# 函数用途: dispatch 顶层 deliverable_artifact_refs 不能再把模型写过的旧路径当最终事实。
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


# LLM: _direct_child_task keeps the direct child fixture small and readable.
# 函数用途: 构造带 artifact/evidence/output refs 的直接子代理 task，验证父级只看直接 run。
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


# LLM: _grandchild_task proves nested descendants do not become direct result rows.
# 函数用途: 构造一条孙代理 task，dispatch refs 应忽略它作为父级主汇总入口。
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


# LLM: _task_index mirrors the manager lookup table used by dispatch refs.
# 函数用途: 按 run_id 索引测试 task，供 SimpleNamespace manager 的 load 回调读取。
def _task_index(*tasks: SimpleNamespace) -> dict[str, SimpleNamespace]:
    return {str(task.id): task for task in tasks}


# LLM: _expected_child_row pins the model-facing handoff contract for one direct child.
# 函数用途: 集中保存期望的 result_refs_by_run 行，避免主测试函数超长。
def _expected_child_row() -> dict[str, object]:
    return {
        "run_id": "child-1",
        "agent_name": "小傻妞-市场",
        "role": "coordinator",
        "status": "DONE",
        "verification_status": "VERIFIED",
        "summary": "市场报告完成",
        "primary_artifact_ids": [],
        "primary_artifact_refs": ["/tmp/market/final.md"],
        "primary_artifact_registry_refs": [],
        "primary_artifact_summaries": [],
        "evidence_refs": ["/tmp/market/output.json"],
        "output_json": "/tmp/market/output.json",
        "runner_result_json": "/tmp/market/runner_result.json",
    }
