from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# 防回归(收尾一公里·增量结论账): "结论只活在最终一次性输出里"→ 收尾崩/被取消=过程
# 价值清零(E2 实锤:12 个漏报里 10 个来自两条被取消的路)。钉子覆盖:
# ①record_finding 确认即入账(run 账本/任务共享账本两个落点);②_write_findings 从
# 覆盖写改按 id 合并——工具行不再被 task.findings 快照清掉;③崩溃收尾兜底把账带回
# 结构化输出;④cancel 回执带账本指针;⑤tree 节点透出 findings_recorded。

from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.agent_core.runtime.record_finding_tool import RecordFindingTool


def _run_task(tmp_path: Path) -> SimpleNamespace:
    ledger = tmp_path / "work" / "agents" / "subagent-1" / "findings.jsonl"
    return SimpleNamespace(agent_run_findings_jsonl=str(ledger))


def _agent_for_run(tmp_path: Path) -> SimpleNamespace:
    task = _run_task(tmp_path)
    return SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: task))


def _read_ledger(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_finding_appends_to_run_ledger(tmp_path):
    agent = _agent_for_run(tmp_path)
    previous = set_current_subagent_context(agent, run_id="subagent-1", attempt_id="attempt-9")
    try:
        result = RecordFindingTool(agent).execute(
            {"claim": "EVT-C-032700 probe.verified=true 真命中", "evidence_refs": ["EVT-C-032700"], "kind": "hit"}
        )
    finally:
        restore_current_subagent_context(agent, previous)
    assert result.ok, result.output
    rows = _read_ledger(tmp_path / "work" / "agents" / "subagent-1" / "findings.jsonl")
    assert len(rows) == 1
    assert rows[0]["claim"].startswith("EVT-C-032700")
    assert rows[0]["run_id"] == "subagent-1"
    assert rows[0]["attempt_id"] == "attempt-9"
    assert rows[0]["id"].startswith("rf-")


def test_record_finding_main_agent_falls_back_to_task_workspace(tmp_path):
    agent = SimpleNamespace(_current_run_task_workspace=str(tmp_path / "req_root"))
    result = RecordFindingTool(agent).execute({"claim": "模块 A 冒烟通过"})
    assert result.ok, result.output
    rows = _read_ledger(tmp_path / "req_root" / "work" / "shared" / "findings.jsonl")
    assert rows and rows[0]["claim"] == "模块 A 冒烟通过"


def test_record_finding_requires_claim(tmp_path):
    result = RecordFindingTool(_agent_for_run(tmp_path)).execute({"claim": "  "})
    assert not result.ok


def test_record_finding_without_workspace_reports_error():
    result = RecordFindingTool(SimpleNamespace()).execute({"claim": "x"})
    assert not result.ok


# ---- ②_write_findings 合并语义:工具行 + task.findings 快照共存,幂等 ----

from agent_py_agent.agent.memory_archive.agent_run_workspace import _write_findings


def test_write_findings_merge_preserves_tool_rows(tmp_path):
    path = tmp_path / "findings.jsonl"
    path.write_text(
        json.dumps({"id": "rf-abc", "claim": "工具边干边写的结论", "source": "record_finding_tool"}) + "\n",
        encoding="utf-8",
    )
    task = SimpleNamespace(id="subagent-1", root_id="req_root", findings=[
        {"id": "finding-1", "claim": "最终块解析出的结论", "evidence_refs": ["r1"]},
    ])
    _write_findings(path, task, now=1000.0)
    _write_findings(path, task, now=1001.0)  # 幂等:再写一遍不翻倍
    rows = _read_ledger(path)
    ids = {row["id"] for row in rows}
    assert ids == {"rf-abc", "finding-1"}


# ---- ③崩溃收尾兜底把账带回结构化输出 ----

import agent_py_agent.agent.agent_core.subagent_mixin as sm


def _agent_with_ledger_and_products(tmp_path: Path) -> SimpleNamespace:
    ledger = tmp_path / "work" / "agents" / "subagent-1" / "findings.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"id": "rf-1", "claim": "命中 EVT-X", "evidence_refs": ["EVT-X"]}) + "\n",
        encoding="utf-8",
    )
    product = tmp_path / "work" / "child_outputs" / "03.md"
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text("盯守报告正文", encoding="utf-8")
    task = SimpleNamespace(
        agent_run_findings_jsonl=str(ledger),
        task_workspace_dir=str(tmp_path),
        attributes={},
    )
    return SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: task))


def test_registry_fallback_carries_ledger_findings(tmp_path, monkeypatch):
    agent = _agent_with_ledger_and_products(tmp_path)
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": str(tmp_path / "work" / "child_outputs" / "03.md"), "name": "03.md", "bytes": 10},
    ])
    out = sm._structured_from_registered_products(agent, SimpleNamespace(run_id="subagent-1"))
    assert out is not None
    assert [f["id"] for f in out.findings] == ["rf-1"]
    assert "增量结论账 1 条" in out.summary


# ---- ④cancel 回执带账本指针 ----

from agent_py_agent.agent.agent_core.orchestration.tools.cancel import _findings_ledger_snapshot


def test_cancel_findings_snapshot_counts_rows(tmp_path):
    ledger = tmp_path / "findings.jsonl"
    ledger.write_text('{"id":"rf-1"}\n{"id":"rf-2"}\n', encoding="utf-8")
    task = SimpleNamespace(agent_run_findings_jsonl=str(ledger))
    path, count = _findings_ledger_snapshot(task)
    assert path == str(ledger)
    assert count == 2


def test_cancel_findings_snapshot_missing_file(tmp_path):
    task = SimpleNamespace(agent_run_findings_jsonl=str(tmp_path / "nope.jsonl"))
    path, count = _findings_ledger_snapshot(task)
    assert count == 0


# ---- ⑤tree 节点透出 findings_recorded 与 findings_ledger ----

from agent_py_agent.agent.agent_core.agent_tree.node_rendering import (
    _findings_recorded,
    _workspace_refs,
)


def test_node_findings_recorded_counts_ledger(tmp_path):
    ledger = tmp_path / "findings.jsonl"
    ledger.write_text('{"id":"rf-1"}\n\n{"id":"rf-2"}\n', encoding="utf-8")
    payload = {"workspace_refs": {"agent_run_findings": str(ledger)}}
    assert _findings_recorded(payload) == 2
    refs = _workspace_refs({"agent_run_findings": str(ledger)})
    assert refs.get("findings_ledger")


# ---- ⑥registry 产出兜底只认本 run 的登记(跨 run 污染实锤) ----

import agent_py_agent.agent.artifacts.registry as artifact_registry


def test_registered_products_scoped_to_own_run(tmp_path, monkeypatch):
    """共享任务根的 registry 混着编队全体登记:兜底收尾只认本 run(+无主老数据),
    不再把兄弟的产物当自己的收尾依据。"""
    mine = tmp_path / "mine.md"
    sibling = tmp_path / "sibling.md"
    legacy = tmp_path / "legacy.md"
    for f in (mine, sibling, legacy):
        f.write_text("正文内容", encoding="utf-8")
    records = {
        "a1": SimpleNamespace(run_id="subagent-1", status="ready", path=str(mine)),
        "a2": SimpleNamespace(run_id="subagent-2", status="ready", path=str(sibling)),
        "a3": SimpleNamespace(run_id="", status="ready", path=str(legacy)),
    }
    monkeypatch.setattr(artifact_registry, "latest_artifact_records", lambda ws: records)
    monkeypatch.setattr(sm, "_subagent_workspace_dir", lambda a, r: str(tmp_path))
    products = sm._registered_ready_products(object(), "subagent-1")
    assert sorted(p["name"] for p in products) == ["legacy.md", "mine.md"]
