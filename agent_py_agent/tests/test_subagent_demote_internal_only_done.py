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
