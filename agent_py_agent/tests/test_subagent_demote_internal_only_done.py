from __future__ import annotations

from types import SimpleNamespace

import agent_py_agent.agent.agent_core.subagent_mixin as sm
from agent_py_agent.agent.subagents.model_runtime import SubAgentParsedOutput


def _done():
    return SubAgentParsedOutput(found=True, ok=True, status="DONE")


def _params():
    return SimpleNamespace(run_id="subagent-1")


def test_demote_when_all_products_in_workspace(monkeypatch):
    """声称 DONE,但产物全在子代理工作区 work/(final_report、child_outputs 小结)→ 降级重跑。
    真飞书 u2 实锤:子代理把活全写进 work/agents/.../final_report.md，用户要的 ip_top10.txt 一个没写。"""
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": "/h/tasks/t/work/agents/subagent-1/final_report.md"},
        {"path": "/h/tasks/t/output/work/child_outputs/01-agent-d1-worker.md"},
    ])
    out = sm._demote_internal_only_done(object(), _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "BLOCKED"


def test_no_demote_when_real_delivery_outside_workspace(monkeypatch):
    """有 work/ 之外的真交付物(用户指定路径 / output_dir)→ 真完成,不动。"""
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": "/home/u/data/ip_top10.txt"},
        {"path": "/h/tasks/t/work/agents/subagent-1/final_report.md"},
    ])
    out = sm._demote_internal_only_done(object(), _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "DONE"


def test_no_demote_when_no_products(monkeypatch):
    """纯查询/回答任务本就无产物 → 不误伤。"""
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [])
    out = sm._demote_internal_only_done(object(), _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "DONE"


def test_no_demote_when_capability_request(monkeypatch):
    """带 capability_request 的正当求助不降级。"""
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": "/h/tasks/t/work/agents/subagent-1/final_report.md"},
    ])
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", capability_requests=[{"problem": "x"}])
    out = sm._demote_internal_only_done(object(), _params(), structured)
    assert str(getattr(out, "status", "")).upper() == "DONE"


# ---- 纪律⑤:派工声明的协作槽产物已兑现 → 不降级(编队收尾一公里实锤) ----
# 真机形态:主代理派工点名 output_files=work/child_outputs/03-stream-8903.md,子代理
# 如约写好(37 条候选研判都在里面),收尾却因"产物全在 work/ 内"被降级 BLOCKED;
# 降级引导反而驱使 attempt2 把编队总报告写进主代理交付区(交付面污染)。


def _agent_with_declared(tmp_path, declared_rel: str, *, write: bool):
    task_root = tmp_path / "req_root"
    declared = task_root / declared_rel
    if write:
        declared.parent.mkdir(parents=True, exist_ok=True)
        declared.write_text("8903 路盯守报告:候选研判 37 条", encoding="utf-8")
    task = SimpleNamespace(
        attributes={"output_files": [str(declared)]},
        task_workspace_dir=str(task_root),
    )
    return SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: task))


def test_no_demote_when_declared_collab_slot_outputs_satisfied(tmp_path, monkeypatch):
    """派工声明的 child_outputs 产物已落地 → 被点名的协作槽交付不是捷径,不降级。"""
    agent = _agent_with_declared(tmp_path, "work/child_outputs/03-stream-8903.md", write=True)
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": str(tmp_path / "req_root/work/child_outputs/03-stream-8903.md")},
    ])
    out = sm._demote_internal_only_done(agent, _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "DONE"


def test_demote_when_declared_outputs_missing(tmp_path, monkeypatch):
    """声明了产物却没写出来 → 照旧降级(防偷懒纪律不回退)。"""
    agent = _agent_with_declared(tmp_path, "work/child_outputs/03-stream-8903.md", write=False)
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": str(tmp_path / "req_root/work/agents/subagent-1/final_report.md")},
    ])
    out = sm._demote_internal_only_done(agent, _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "BLOCKED"


def test_demote_when_nothing_declared(monkeypatch):
    """无声明(r1d 原始形态)→ 照旧降级,豁免只给显式点名的交付。"""
    task = SimpleNamespace(attributes={}, task_workspace_dir="/h/tasks/t")
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: task))
    monkeypatch.setattr(sm, "_registered_ready_products", lambda a, r: [
        {"path": "/h/tasks/t/work/agents/subagent-1/final_report.md"},
    ])
    out = sm._demote_internal_only_done(agent, _params(), _done())
    assert str(getattr(out, "status", "")).upper() == "BLOCKED"
