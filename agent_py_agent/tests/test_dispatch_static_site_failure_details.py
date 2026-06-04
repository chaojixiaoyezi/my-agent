"""LLM: Dispatch should expose concrete static-site acceptance failures.

模块用途: 用小型集成测试验证最终收口失败细节能回到 dispatch record，避免膨胀主 dispatch 测试文件。
"""

from __future__ import annotations

import json
import time

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents import EvidencePacket, VerificationEvidence


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
        params=DispatchParams(apply=True, max_runners=0, start_runners=True),
    )

    assert not any(item.step == "acceptance" and item.run_id == task.id for item in report.records)
    assert task.status == "DONE"
    assert task.verification_status == "VERIFIED"


def _acceptance_task(agent: SimpleAgent):
    task = agent.subagents.create_run(
        goal="静态页面验收失败要给出具体修复线索",
        thought="等待 tests。",
        plan=["tests"],
    )
    task.status = "DONE"
    task.verification_status = "VERIFIED"
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
