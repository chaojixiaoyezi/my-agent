"""LLM: Tests for subagent action plan/apply, capability routing (tool/skill/gap),
and execution context; covers channel probe reports, action-plan dry-run,
action apply with reopen/takeover/repair, and execution context scoping.

给人看的解释：
测试子代理行动计划与执行、能力路由（工具/技能/缺口）、执行上下文：
通道探测报告、行动计划 dry-run、apply 重开/接管/修复、执行上下文范围。
"""

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.skills import SkillRegistry
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
)


def test_subagent_channel_probe_report():
    """LLM: Verifies channel probe report aggregates OK and BROKEN statuses."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        first = agent.subagents.create_run(
            goal="健康任务",
            thought="检查健康通道。",
            plan=["probe"],
        )
        second = agent.subagents.create_run(
            goal="坏通道任务",
            thought="模拟 JSON 缺失。",
            plan=["probe"],
        )
        Path(second.output_json).unlink()

        report = agent.subagents.write_channel_probe_report([first.id, second.id])
        statuses = {result.run_id: result.channel_status for result in report.results}

        assert statuses[first.id] == "OK"
        assert statuses[second.id] == "BROKEN"
        assert report.summary["total"] == 2
        assert report.summary["OK"] == 1
        assert report.summary["BROKEN"] == 1
        assert (root / "subs" / "subagent_channel_probe.json").exists()
        assert (root / "subs" / "SUBAGENT_CHANNEL_PROBE.md").exists()


def _make_stale_task(agent):
    """Helper to create a task with stale heartbeat."""
    task = agent.subagents.create_run(goal="长时间未推进任务", thought="模拟需要接管或重派。", plan=["执行", "等待"])
    loaded = agent.subagents.load(task.id)
    loaded.created_at = time.time() - 30
    loaded.heartbeat_at = time.time() - 30
    agent.subagents.save(loaded)
    return task


# LLM: _make_coordinator_handoff_fixture builds a stale coordinator, one child, and one candidate leader.
# 函数用途: 复用 coordinator 领导权恢复测试夹具，避免单个测试函数膨胀。
def _make_coordinator_handoff_fixture(agent):
    coordinator = agent.subagents.create_run(
        goal="协调子任务", thought="等待孩子完成。", plan=["dispatch", "collect"],
        agent_name="root-coord", role="coordinator",
    )
    child = agent.subagents.create_run(
        goal="实现模块", thought="被旧 coordinator 管理。", plan=["work"],
        parent_id=coordinator.id, root_id=coordinator.root_id, supervisor=coordinator.id,
    )
    grandchild = agent.subagents.create_run(
        goal="实现子模块", thought="被 child 管理。", plan=["work"],
        parent_id=child.id, root_id=coordinator.root_id, supervisor=child.id,
    )
    leader = agent.subagents.create_run(
        goal="接手协调", thought="新的 leader。", plan=["lead"],
        parent_id=coordinator.id, root_id=coordinator.root_id,
        supervisor=coordinator.id, role="coordinator",
    )
    stale = agent.subagents.load(coordinator.id)
    stale.heartbeat_at = time.time() - 30
    stale.updated_at = stale.heartbeat_at
    agent.subagents.save(stale)
    return coordinator, child, grandchild, leader


def test_subagent_action_plan_dry_run():
    """LLM: Verifies action-plan produces takeover, reopen, route, and probe actions."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)

        stale = _make_stale_task(agent)
        fake_done = agent.subagents.create_run(goal="无证据完成任务", thought="模拟假完成。", plan=["标记完成"])
        agent.subagents.set_status(fake_done.id, "DONE")

        request_task = agent.subagents.create_run(goal="等待能力路由任务", thought="模拟缺少工具。", plan=["请求能力"])
        agent.subagents.record_capability_request(request_task.id, RecordCapabilityRequestParams(problem="缺少真实入口验收工具。", needed_capability="browser_smoke_test"))

        broken = agent.subagents.create_run(goal="坏通道任务", thought="模拟 output.json 损坏。", plan=["probe"])
        Path(broken.output_json).unlink()
        agent.subagents.probe_channel(broken.id)

        cap = CapabilityConfig(subagent_heartbeat_timeout=1, subagent_run_timeout=1, subagent_min_evidence_for_done=1)
        report = agent.subagents.write_action_plan(cap)
        actions = {(item.run_id, item.action): item for item in report.actions}

        assert (stale.id, "takeover_or_reassign") in actions
        assert "heartbeat_stale" in actions[(stale.id, "takeover_or_reassign")].source_issue_kinds
        assert "run_timeout" in actions[(stale.id, "takeover_or_reassign")].source_issue_kinds
        assert actions[(stale.id, "takeover_or_reassign")].rescue_strategy == "takeover_or_shrink_scope_before_retry"
        assert actions[(stale.id, "takeover_or_reassign")].escalation_target == "parent"
        assert "run_timeout" in actions[(stale.id, "takeover_or_reassign")].rescue_trigger
        takeover_packet = actions[(stale.id, "takeover_or_reassign")].rescue_packet
        assert takeover_packet["schema_name"] == "subagent_rescue_packet"
        assert takeover_packet["dedupe_key"] == f"{stale.id}:takeover_or_reassign"
        assert takeover_packet["repeat_count"] == 2
        assert set(takeover_packet["issue_kinds"]) == {"heartbeat_stale", "run_timeout"}
        assert takeover_packet["retry_policy"]["max_attempts"] == 1
        assert takeover_packet["escalation"]["target"] == "parent"
        assert takeover_packet["manual_confirmation"]["required"] is True
        assert takeover_packet["reserved"]["auto_execute"] is False
        assert (fake_done.id, "reopen_for_evidence") in actions
        assert (request_task.id, "route_capability_request") in actions
        assert (broken.id, "probe_or_repair_channel") in actions
        assert actions[(request_task.id, "route_capability_request")].escalation_target == "capability_router"
        assert all(item.dry_run for item in report.actions)
        assert (root / "subs" / "subagent_action_plan.json").exists()
        assert (root / "subs" / "SUBAGENT_ACTION_PLAN.md").exists()


def test_subagent_action_apply_dry_run_and_apply():
    """LLM: Verifies dry-run does not mutate state, and apply reopens or takes over."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        cap = CapabilityConfig(subagent_heartbeat_timeout=1, subagent_run_timeout=1, subagent_min_evidence_for_done=1)

        # Test dry-run does not mutate
        fake_done = agent.subagents.create_run(goal="需要补证据", thought="模拟缺证据完成。", plan=["标记完成"])
        agent.subagents.set_status(fake_done.id, "DONE")
        dry_report = agent.subagents.write_action_apply_report(cap, action_filter="reopen_for_evidence", run_id=fake_done.id)
        assert dry_report.dry_run and dry_report.records[0].applied is False
        assert dry_report.records[0].rescue_strategy == "reopen_and_request_missing_evidence"
        assert agent.subagents.load(fake_done.id).status == "DONE"

        # Test apply reopens
        apply_report = agent.subagents.write_action_apply_report(cap, apply=True, action_filter="reopen_for_evidence", run_id=fake_done.id)
        reopened = agent.subagents.load(fake_done.id)
        assert not apply_report.dry_run and apply_report.records[0].applied
        assert reopened.status == "BLOCKED" and reopened.failure_type == "missing_evidence"
        assert (root / "subs" / "subagent_action_apply_log.jsonl").exists()
        assert (root / "subs" / "ACTION_APPLY_LOG.md").exists()

        # Test takeover with missing owner fails
        stale = _make_stale_task(agent)
        missing = agent.subagents.write_action_apply_report(cap, apply=True, action_filter="takeover_or_reassign", run_id=stale.id)
        assert not missing.records[0].ok and agent.subagents.load(stale.id).status == "PLANNING"

        # Test takeover with owner succeeds
        takeover = agent.subagents.write_action_apply_report(
            cap, apply=True, action_filter="takeover_or_reassign", run_id=stale.id,
            take_over_by="parent-supervisor", locked_files=["src/example.py"],
        )
        taken = agent.subagents.load(stale.id)
        assert takeover.records[0].ok and taken.status == "TAKEN_OVER"
        assert taken.takeover_by == "parent-supervisor" and "src/example.py" in taken.locked_files
        assert Path(taken.takeover_file).exists()


def test_subagent_action_apply_recovers_coordinator_leadership():
    """LLM: Verifies stale coordinator children can be reassigned to an explicit leader run."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        coordinator, child, grandchild, leader = _make_coordinator_handoff_fixture(agent)

        missing = agent.subagents.write_action_apply_report(
            CapabilityConfig(subagent_heartbeat_timeout=1),
            apply=True,
            action_filter="recover_coordinator_leadership",
            run_id=coordinator.id,
        )
        assert not missing.records[0].ok

        applied = agent.subagents.write_action_apply_report(
            CapabilityConfig(subagent_heartbeat_timeout=1),
            apply=True,
            action_filter="recover_coordinator_leadership",
            run_id=coordinator.id,
            take_over_by=leader.id,
        )

        assert applied.records[0].ok
        assert agent.subagents.load(coordinator.id).status == "TAKEN_OVER"
        assert agent.subagents.load(coordinator.id).takeover_by == leader.id
        reloaded_coordinator = agent.subagents.load(coordinator.id)
        reloaded_leader = agent.subagents.load(leader.id)
        reloaded_child = agent.subagents.load(child.id)
        reloaded_grandchild = agent.subagents.load(grandchild.id)
        assert reloaded_coordinator.child_ids == [leader.id]
        assert reloaded_child.parent_id == leader.id
        assert reloaded_child.supervisor == leader.id
        assert reloaded_child.final_owner == leader.id
        assert child.id in reloaded_leader.child_ids
        assert reloaded_child.depth == reloaded_leader.depth + 1
        assert reloaded_grandchild.depth == reloaded_child.depth + 1


def test_subagent_action_apply_parent_timeout_child_recovery_is_record_only():
    """LLM: Verifies parent-timeout child recovery records guidance without reparenting children."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="协调孩子", thought="父节点会超时。", plan=["dispatch", "collect"],
            agent_name="root-coord", role="coordinator",
        )
        child = agent.subagents.create_run(
            goal="等待 leaf", thought="孩子未完成。", plan=["dispatch leaf"],
            parent_id=parent.id, root_id=parent.root_id, supervisor=parent.id,
        )
        timed_out = agent.subagents.load(parent.id)
        timed_out.status = "TIMEOUT"
        agent.subagents.save(timed_out)

        report = agent.subagents.write_action_apply_report(
            CapabilityConfig(subagent_heartbeat_timeout=3600, subagent_run_timeout=3600),
            apply=True,
            action_filter="recover_child_after_parent_timeout",
            run_id=parent.id,
        )

        reloaded_parent = agent.subagents.load(parent.id)
        reloaded_child = agent.subagents.load(child.id)
        assert report.records[0].ok is True
        assert report.records[0].action == "recover_child_after_parent_timeout"
        assert reloaded_parent.status == "TIMEOUT"
        assert reloaded_child.parent_id == parent.id
        assert reloaded_child.status == "PLANNING"


def test_subagent_action_apply_repairs_work_order():
    """LLM: Verifies apply with repair_work_order recreates missing output_json."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="修复工单现场",
            thought="模拟 output.json 缺失。",
            plan=["修复"],
        )
        Path(task.output_json).unlink()

        report = agent.subagents.write_action_apply_report(
            CapabilityConfig(),
            apply=True,
            action_filter="repair_work_order",
            run_id=task.id,
        )

        assert report.records[0].ok
        assert Path(task.output_json).exists()
        assert (root / "subs" / "subagent_action_apply_report.json").exists()
        assert (root / "subs" / "SUBAGENT_ACTION_APPLY.md").exists()


def test_subagent_capability_route_grants_tool():
    """LLM: Verifies capability router grants a tool and updates allowed_tools."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="检查 API 返回",
            thought="需要 HTTP 工具。",
            plan=["请求能力"],
        )
        request = agent.subagents.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="当前需要请求 REST API 并检查 HTTP 状态码和 JSON 返回。",
                needed_capability="http_request",
                expected_output="接口状态码和返回体摘要",
                capability_type="shell",
                requested_tools=["http_request"],
                requested_commands=["curl"],
                path_scope=[str(root)],
                network_scope=["https://api.example.test"],
                output_budget={"stdout_bytes": 4096},
                risk_level="low",
            ),
        )
        router = CapabilityRouter(
            config=CapabilityConfig(capability_candidate_limit=3, capability_grant_max_tools=1),
            tool_specs=agent.tools.specs(),
        )

        dry = agent.subagents.write_capability_route_report(router, apply=False, run_ids=[task.id])
        assert dry.records[0].status == "WOULD_GRANT"
        assert agent.subagents.load(task.id).capability_requests[0].status == "OPEN"

        applied = agent.subagents.write_capability_route_report(router, apply=True, run_ids=[task.id])
        routed = agent.subagents.load(task.id)

        assert applied.records[0].status == "GRANTED"
        assert routed.capability_requests[0].id == request.id
        assert routed.capability_requests[0].status == "GRANTED"
        assert routed.capability_grants
        assert "http_request" in routed.allowed_tools
        grant = routed.capability_grants[0]
        assert grant.grant_type == "shell"
        assert grant.command_allowlist == ["curl"]
        assert grant.path_scope == [str(root)]
        assert grant.network_scope == ["https://api.example.test"]
        assert grant.output_budget["stdout_bytes"] == 4096
        assert applied.records[0].request_scope["requested_commands"] == ["curl"]
        assert applied.records[0].grant_scope["command_allowlist"] == ["curl"]
        assert (root / "subs" / "subagent_capability_route_report.json").exists()
        assert (root / "subs" / "SUBAGENT_CAPABILITY_ROUTE.md").exists()
        assert (root / "subs" / "subagent_capability_route_log.jsonl").exists()


def test_subagent_capability_route_grants_skill():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill_dir = root / "skills" / "api-check"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: api-check
description: 检查 REST API 返回和错误码
when_to_use: 用户需要测试接口、检查 HTTP 状态或分析 JSON 返回
tags: [api, http, rest]
capabilities: [api_testing]
risk_level: low
---

读取接口文档，发起最小请求，检查状态码和返回体。
""",
            encoding="utf-8",
        )
        skills = SkillRegistry([root / "skills"])
        skills.scan()
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="检查 API",
            thought="需要 API skill。",
            plan=["请求能力"],
        )
        agent.subagents.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="需要检查 REST API 返回和错误码。",
                needed_capability="api_testing",
                expected_output="API 检查报告",
            ),
        )
        router = CapabilityRouter(
            config=CapabilityConfig(capability_candidate_limit=3, capability_grant_max_skills=1),
            skill_registry=skills,
        )

        report = agent.subagents.write_capability_route_report(router, apply=True, run_ids=[task.id])
        routed = agent.subagents.load(task.id)

        assert report.records[0].status == "GRANTED"
        assert "api-check" in routed.allowed_skills
        assert routed.capability_grants[0].capability_cards[0]["kind"] == "skill"


def test_subagent_capability_route_creates_gap_when_no_match():
    """LLM: Verifies unmatched capability becomes a GAP with capability_gaps recorded."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="未知能力任务",
            thought="模拟没有匹配能力。",
            plan=["请求能力"],
        )
        agent.subagents.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="需要 zzz_unmatched_capability_999 完成一个不存在的能力。",
                needed_capability="zzz_unmatched_capability_999",
                expected_output="未知输出",
                capability_type="mcp",
                requested_mcp_tools=["zzz_unmatched_mcp_999"],
                escalation_target="root-reviewer",
            ),
        )
        router = CapabilityRouter(
            config=CapabilityConfig(capability_candidate_limit=3),
            tool_specs=agent.tools.specs(),
        )

        report = agent.subagents.write_capability_route_report(router, apply=True, run_ids=[task.id])
        routed = agent.subagents.load(task.id)

        assert report.records[0].status == "GAP"
        assert routed.capability_requests[0].status == "GAP"
        assert routed.capability_gaps
        assert routed.capability_gaps[0].gap_type == "mcp"
        assert routed.capability_gaps[0].requested_scope["requested_mcp_tools"] == ["zzz_unmatched_mcp_999"]
        assert "root-reviewer" in routed.capability_gaps[0].escalation_chain
        assert report.records[0].request_scope["capability_type"] == "mcp"


def test_subagent_execution_context_uses_only_grants():
    """LLM: Verifies execution context only includes granted tools/skills, not all."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="检查接口健康", thought="只允许读取文件，缺接口检查能力时向父代理请求。",
            plan=["读取代码", "请求能力", "写验收证据"],
            allowed_tools=["read_file"], acceptance_checks=["必须有接口检查证据"],
        )
        request = agent.subagents.record_capability_request(
            task.id, RecordCapabilityRequestParams(problem="需要发起 HTTP GET 检查接口状态。", needed_capability="http_request", expected_output="接口状态码和摘要"),
        )
        agent.subagents.record_capability_grant(task.id, RecordCapabilityGrantParams(
            request_id=request.id, skills=["api-check"], tools=["http_request"],
            capability_cards=[{"id": "tool:http_request", "kind": "tool", "name": "http_request",
                               "description": "发起 HTTP 请求并返回状态码和响应摘要", "risk_level": "low", "source": "builtin", "path": ""}],
            reason="父代理授权低风险接口健康检查。",
        ))
        agent.subagents.record_evidence(task.id, RecordEvidenceParams(kind="command", summary="接口 smoke test 通过", command="python3 smoke_api.py"))

        context = agent.subagents.write_execution_context(task.id, max_cards=1)
        payload = json.loads(Path(context.execution_context_json).read_text(encoding="utf-8"))
        markdown = Path(context.execution_context_file).read_text(encoding="utf-8")

        assert payload["run_id"] == task.id
        assert payload["allowed_tools"] == ["read_file", "http_request"]
        assert payload["allowed_skills"] == ["api-check"]
        assert payload["granted_cards"][0]["name"] == "http_request"
        assert "write_file" not in payload["allowed_tools"]
        assert payload["pending_requests"][0]["status"] == "OPEN"
        assert "不要读取或展开全局 skill/tool registry" in "\n".join(payload["instructions"])
        assert "SUBAGENT EXECUTION CONTEXT" in markdown and "http_request" in markdown
