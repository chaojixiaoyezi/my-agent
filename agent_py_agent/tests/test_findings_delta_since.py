"""原生投递根修:子代理"先记逐条结论、后叫回主代理上报"的监控形态里,结论是上报 run 开始【前】
由子代理记的,run_started_at-1s 的默认窗口抓不到 → delta 空 → 主代理响应是
[RUN_TOOL_EVIDENCE_BLOCKED] 内部信号时,整条被投递枢纽拦下、发不到用户飞书。

修:叫回方(观察批/wake)把触发本轮的观察/信号时刻填进 findings_since,让 delta 覆盖"自 wake
触发以来"的新结论——即便主代理正文是内部信号,逐条结论也单独出站到用户面。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent.conversation.runtime import (
    BackgroundRunRequest,
    _content_with_findings_delta,
    _run_request,
)


def _write_finding(task_root, finding_id: str, created_at: float) -> None:
    ledger = task_root / "work" / "shared" / "findings.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": finding_id, "claim": f"检测到入侵 {finding_id}",
            "evidence_refs": ["log://auth/42"], "created_at": created_at,
        }) + "\n")


def test_delta_captures_earlier_subagent_finding_via_findings_since(tmp_path):
    # 子代理在 T=100 记的结论;上报 run 若用 run_started_at(=200)-1s 窗口 → 抓不到。
    _write_finding(tmp_path, "F-1", created_at=100.0)
    agent = SimpleNamespace(_current_run_task_workspace=str(tmp_path))
    rework = "[RUN_TOOL_EVIDENCE_BLOCKED]\n{...}\n[/RUN_TOOL_EVIDENCE_BLOCKED]"

    # ① 用 run_started_at=200(默认形态)→ 100<199 被排除 → delta 空 → 出站是原内部信号(会被过滤)
    _stored, send_default = _content_with_findings_delta(agent, rework, 200.0)
    assert send_default == rework  # delta 空,出站=REWORK 原文(投递枢纽会整条拦下)

    # ② 用 findings_since=99(观察时刻回溯)→ 抓到 F-1 → 结论块单独出站(不含内部信号记号)
    _stored2, send_fixed = _content_with_findings_delta(agent, rework, 99.0)
    assert "F-1" in send_fixed and "检测到入侵 F-1" in send_fixed
    assert "RUN_TOOL_EVIDENCE_BLOCKED" not in send_fixed  # 内部记号只进内部账,不出站


def test_delta_normal_response_appends_findings(tmp_path):
    # 主代理正文正常(非内部信号)时:结论追加在正文后一起出站
    _write_finding(tmp_path, "F-2", created_at=100.0)
    agent = SimpleNamespace(_current_run_task_workspace=str(tmp_path))
    stored, send = _content_with_findings_delta(agent, "已完成本轮分析。", 99.0)
    assert "已完成本轮分析。" in send and "F-2" in send


def test_background_run_request_carries_findings_since():
    # 管道:_run_request 读 findings_since;0/缺省=不覆盖(用 run_started_at 原形态)
    req = _run_request({"thread_id": "t1", "findings_since": 123.5, "now": 200.0})
    assert req.findings_since == 123.5
    assert _run_request({"thread_id": "t1", "now": 200.0}).findings_since == 0.0
    assert BackgroundRunRequest(thread_id="t1").findings_since == 0.0
