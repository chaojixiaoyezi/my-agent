"""LLM: Dispatch should expose concrete static-site acceptance failures.

模块用途: 用小型集成测试验证父级验收失败细节能回到 dispatch record，避免膨胀主 dispatch 测试文件。
"""

from __future__ import annotations

import json
import time

from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence


# LLM: test_static_site_failure_details_reach_dispatch_record verifies repair-ready facts.
# 函数用途: static_site_check 失败时，dispatch record 必须包含具体失效控件，方便父级重新派修复任务。
def test_static_site_failure_details_reach_dispatch_record(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    site = tmp_path / "deliverables"
    site.mkdir()
    (site / "index.html").write_text(
        "<!doctype html><html><body><a href='#'>Shop</a></body></html>",
        encoding="utf-8",
    )
    task = _acceptance_task(agent)
    _write_static_site_output(task)

    report = agent.dispatch_subagents(
        CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs()),
        CapabilityConfig(),
        params=DispatchParams(apply=True, max_runners=0, execute_acceptance_tests=True),
    )

    record = next(item for item in report.records if item.step == "acceptance" and item.run_id == task.id)
    assert record.action == "reject"
    assert "inert_control_hits=1" in record.parent_acceptance_test_failure_summary
    assert record.parent_acceptance_test_failure_details == ["inert_control_hits: index.html:a:Shop"]


# LLM: _acceptance_task creates the minimum awaiting-acceptance task with evidence.
# 函数用途: 构造一个可进入父级验收的子代理任务，避免复用大型 dispatch 测试模块。
def _acceptance_task(agent: SimpleAgent):
    task = agent.subagents.create_run(
        goal="静态页面验收失败要给出具体修复线索",
        thought="等待 tests。",
        plan=["tests"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.evidence.append(VerificationEvidence(kind="note", summary="有验收证据", ok=True, created_at=time.time()))
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-static-site",
        claim="静态页面已有验收证据",
        checked_scope="static site fixture",
        evidence_refs=[task.acceptance_file],
        artifact_refs=[task.output_json],
        confidence=0.9,
        created_at=time.time(),
    ))
    agent.subagents.save(task)
    return task


# LLM: _write_static_site_output keeps the output contract focused on one failing HTML page.
# 函数用途: 写入 output.json，声明一个会被 static_site_check 识别的失效锚点页面。
def _write_static_site_output(task) -> None:
    output = {
        "run_id": task.id,
        "patches": [],
        "blockers": [],
        "tests": [{
            "name": "site smoke",
            "validation_method": "static_site_check",
            "site_root": "deliverables",
            "required_files": ["index.html"],
            "html_files": ["index.html"],
        }],
    }
    with open(task.output_json, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
