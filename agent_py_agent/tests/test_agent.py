"""覆盖智能体核心行为的小型回归测试。

这里故意保持为普通函数，因为仓库当前自带的是 `run_tests.py`
 这种轻量冒烟脚本，而不是引入完整测试框架依赖。
"""

from pathlib import Path
import json
import tempfile
import time

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.skills import SkillRegistry
from agent_py_agent.agent.subagent import parse_subagent_runner_output


class StructuredSubagentBackend(BaseBackend):
    """测试用后端：直接返回 runner 结构化结果。"""

    name = "structured_subagent_backend"

    def generate(self, prompt: str) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        return ModelResponse(
            text=(
                "我读取了当前上下文，但缺少 HTTP 检查能力。\n"
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "BLOCKED",\n'
                '  "summary": "已完成代码阅读，但无法发起 HTTP 检查。",\n'
                '  "used_tools": ["read_file", "write_file"],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "已确认需要接口健康检查", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [\n'
                '    {"problem": "需要请求接口确认状态码", "needed_capability": "http_request", "expected_output": "接口状态码", "tried": ["read_file"], "evidence": ["代码阅读不足以确认线上状态"], "constraints": {"method": "GET"}}\n'
                "  ],\n"
                '  "artifacts": [\n'
                '    {"path": "reports/api_notes.md", "kind": "report", "summary": "接口检查前置阅读记录"}\n'
                "  ],\n"
                '  "tests": [\n'
                '    {"name": "static-read", "command": "read_file api.py", "ok": true, "summary": "静态阅读完成"}\n'
                "  ],\n"
                '  "patches": [\n'
                '    {"path": "agent_py_agent/agent/core.py", "status": "planned", "summary": "需要授权后再接 HTTP 检查"}\n'
                "  ],\n"
                '  "lessons": ["缺少线上检查工具时，不要把静态阅读当成接口可用证据"],\n'
                '  "next_actions": ["route_capability_request", "rerun_subagent_after_grant"],\n'
                '  "blocked_reason": "当前上下文没有授权 HTTP 请求工具",\n'
                '  "failure_type": "capability_request"\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def test_memory_and_run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "prompts").mkdir()
        (root / "prompts/default.md").write_text("动态规则", encoding="utf-8")
        cfg = AgentConfig(memory_path="memory.jsonl", prompt_files=["prompts/default.md"])
        agent = SimpleAgent(cfg, root)
        result = agent.run("记住我喜欢表格", inject=["回答要短"])
        assert "echo 后端" in result.response
        assert (root / "memory.jsonl").exists()
        assert len(agent.recall("表格")) >= 1


def test_subagents():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs", max_subagents=2)
        agent = SimpleAgent(cfg, root)
        tasks = agent.spawn_subagents("做一个 CLI", 5)
        assert len(tasks) == 2
        assert (root / "subs" / tasks[0].id / "task.json").exists()
        assert (root / "subs" / tasks[0].id / "run.json").exists()
        assert tasks[0].root_id == tasks[0].id
        assert Path(tasks[0].status_file).exists()
        assert Path(tasks[0].work_log_file).exists()
        assert Path(tasks[0].acceptance_file).exists()
        assert Path(tasks[0].debrief_file).exists()
        assert Path(tasks[0].output_json).exists()
        assert Path(tasks[0].dependencies_json).exists()


def test_subagent_capability_records():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="审查项目",
            thought="先拆分风险面。",
            plan=["拆任务", "分配能力"],
            agent_name="review-parent",
            role="coordinator",
            owner="parent-owner",
            supervisor="root-supervisor",
            final_owner="final-owner",
            acceptance_checks=["必须有真实命令或文件证据"],
        )
        child = agent.subagents.create_run(
            goal="检查 API 调用",
            thought="先确认接口行为。",
            plan=["读取代码", "请求能力"],
            agent_name="api-checker",
            role="worker",
            parent_id=parent.id,
            root_id=parent.root_id,
            depth=1,
            allowed_tools=["read_file"],
        )

        request = agent.subagents.record_capability_request(
            child.id,
            problem="当前只有 read_file，无法确认接口是否可访问。",
            needed_capability="http_check",
            expected_output="判断接口状态码和返回体",
            tried=["read_file"],
        )
        grant = agent.subagents.record_capability_grant(
            child.id,
            request_id=request.id,
            tools=["fetch_url"],
            reason="允许低风险 GET 检查。",
        )
        gap = agent.subagents.record_capability_gap(
            child.id,
            missing_capability="authenticated_api_check",
            why_failed="缺少登录态和安全授权。",
            attempted_tools=["fetch_url"],
            suggested_skill="api-auth-debugging",
        )

        loaded_child = agent.subagents.load(child.id)
        loaded_parent = agent.subagents.load(parent.id)

        assert child.id in loaded_parent.child_ids
        assert loaded_parent.owner == "parent-owner"
        assert loaded_parent.final_owner == "final-owner"
        assert loaded_child.capability_requests[0].id == request.id
        assert loaded_child.capability_grants[0].id == grant.id
        assert loaded_child.capability_gaps[0].id == gap.id
        assert "fetch_url" in loaded_child.allowed_tools


def test_subagent_fake_done_requires_evidence():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="实现按钮交互",
            thought="先写代码，再真实验证入口。",
            plan=["实现", "验证真实入口"],
            acceptance_checks=["按钮点击有响应", "刷新页面不 404"],
        )

        try:
            agent.subagents.set_status(task.id, "DONE", require_evidence=True)
        except ValueError as exc:
            assert "缺少验收证据" in str(exc)
        else:
            raise AssertionError("没有验收证据时不应该允许 DONE")

        evidence = agent.subagents.record_evidence(
            task.id,
            kind="command",
            summary="运行 smoke test 通过",
            command="python3 smoke_test.py",
        )
        done = agent.subagents.set_status(
            task.id,
            "DONE",
            result="按钮交互已完成并通过 smoke test。",
            require_evidence=True,
        )

        assert evidence.ok
        assert done.status == "DONE"
        assert done.verification_status == "VERIFIED"
        assert done.result


def test_subagent_work_order_validation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="生成报告",
            thought="先准备标准工单目录。",
            plan=["写状态", "产出报告", "验收"],
            acceptance_checks=["报告文件存在", "验收文件已填写"],
        )

        validation = agent.subagents.validate_work_order(task.id)

        assert validation.ok
        assert not validation.missing
        for directory in [
            task.data_dir,
            task.output_dir,
            task.tests_dir,
            task.reports_dir,
            task.logs_dir,
            task.scratch_dir,
        ]:
            assert Path(directory).is_dir()
        assert task.allowed_write_roots == [task.task_dir]
        assert str(Path.home() / "Downloads") in task.forbidden_write_roots

        Path(task.acceptance_file).unlink()
        broken = agent.subagents.validate_work_order(task.id)

        assert not broken.ok
        assert task.acceptance_file in broken.missing


def test_subagent_takeover_records_locked_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="修复登录按钮",
            thought="子代理先尝试修复，必要时由父代理接管。",
            plan=["定位", "修改", "验收"],
            owner="child-worker",
            final_owner="child-worker",
        )

        record = agent.subagents.record_takeover(
            task.id,
            take_over_by="parent-supervisor",
            reason="子代理长时间无验收证据，父代理接管收口。",
            locked_files=["src/login.py", "tests/test_login.py"],
        )
        loaded = agent.subagents.load(task.id)

        assert loaded.status == "TAKEN_OVER"
        assert loaded.takeover_by == "parent-supervisor"
        assert loaded.final_owner == "parent-supervisor"
        assert "src/login.py" in loaded.locked_files
        assert loaded.takeover_records[0].id == record.id
        assert Path(loaded.takeover_file).exists()
        takeover_text = Path(loaded.takeover_file).read_text(encoding="utf-8")
        assert "parent-supervisor" in takeover_text
        assert "src/login.py" in takeover_text


def test_subagent_board_scales_and_flags():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        created = []
        for index in range(25):
            task = agent.subagents.create_run(
                goal=f"批量任务 {index}",
                thought="生成看板压力样本。",
                plan=["执行", "验收"],
                owner=f"worker-{index % 3}",
                final_owner="final-owner",
            )
            created.append(task)
        agent.subagents.record_capability_request(
            created[3].id,
            problem="缺少网页检索能力。",
            needed_capability="web_search",
        )
        agent.subagents.set_status(created[7].id, "DONE")
        agent.subagents.set_status(created[11].id, "BLOCKED", failure_type="tool_failure")
        board = agent.subagents.write_board(recent_limit=10)

        assert board.summary["total"] == 25
        assert len(board.recent) == 10
        assert any("open_capability_request" in item.risk_flags for item in board.hot_list)
        assert any("done_without_evidence" in item.risk_flags for item in board.hot_list)
        assert any("blocked" in item.risk_flags for item in board.hot_list)
        assert (root / "subs" / "subagent_board.json").exists()
        assert (root / "subs" / "SUBAGENT_BOARD.md").exists()


def test_subagent_due_check_report():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        active = agent.subagents.create_run(
            goal="实现长任务巡检",
            thought="模拟长时间运行且等待能力路由的子代理。",
            plan=["执行", "上抛能力", "等待父代理处理"],
            owner="worker-a",
            final_owner="final-owner",
        )
        agent.subagents.record_capability_request(
            active.id,
            problem="当前工具无法验证真实入口。",
            needed_capability="browser_smoke_test",
        )
        agent.subagents.record_capability_gap(
            active.id,
            missing_capability="browser_smoke_test",
            why_failed="没有浏览器自动化工具授权。",
            suggested_tool="playwright_smoke",
        )
        loaded = agent.subagents.load(active.id)
        loaded.created_at = time.time() - 30
        loaded.heartbeat_at = time.time() - 30
        agent.subagents.save(loaded)
        Path(loaded.acceptance_file).unlink()

        done = agent.subagents.create_run(
            goal="假完成样本",
            thought="没有证据就标记完成。",
            plan=["标记完成"],
        )
        agent.subagents.set_status(done.id, "DONE")

        report = agent.subagents.write_due_check(
            CapabilityConfig(
                subagent_heartbeat_timeout=1,
                subagent_run_timeout=1,
                subagent_min_evidence_for_done=1,
            )
        )
        kinds = {issue.kind for issue in report.issues}

        assert "missing_work_order_files" in kinds
        assert "open_capability_request" in kinds
        assert "open_capability_gap" in kinds
        assert "heartbeat_stale" in kinds
        assert "run_timeout" in kinds
        assert "fake_done_risk" in kinds
        assert "unverified_done" in kinds
        assert report.summary["P0"] >= 1
        assert (root / "subs" / "subagent_due_check.json").exists()
        assert (root / "subs" / "SUBAGENT_DUE_CHECK.md").exists()


def test_subagent_channel_probe_records_status():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="检查通道健康",
            thought="确认工单现场和机器 JSON 可用。",
            plan=["probe", "记录结果"],
        )

        ok_result = agent.subagents.probe_channel(task.id)
        ok_loaded = agent.subagents.load(task.id)

        assert ok_result.channel_status == "OK"
        assert ok_loaded.channel_status == "OK"
        assert ok_loaded.last_probe_at > 0
        assert ok_loaded.channel_checks
        assert Path(ok_loaded.channel_probe_file).exists()

        Path(ok_loaded.output_json).unlink()
        broken_result = agent.subagents.probe_channel(task.id)
        broken_loaded = agent.subagents.load(task.id)
        failed_names = {check.name for check in broken_result.checks if not check.ok}

        assert broken_result.channel_status == "BROKEN"
        assert broken_loaded.channel_status == "BROKEN"
        assert "work_order_files" in failed_names
        assert "output_json_readable" in failed_names


def test_subagent_channel_probe_report():
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


def test_subagent_action_plan_dry_run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)

        stale = agent.subagents.create_run(
            goal="长时间未推进任务",
            thought="模拟需要接管或重派。",
            plan=["执行", "等待"],
        )
        loaded = agent.subagents.load(stale.id)
        loaded.created_at = time.time() - 30
        loaded.heartbeat_at = time.time() - 30
        agent.subagents.save(loaded)

        fake_done = agent.subagents.create_run(
            goal="无证据完成任务",
            thought="模拟假完成。",
            plan=["标记完成"],
        )
        agent.subagents.set_status(fake_done.id, "DONE")

        request_task = agent.subagents.create_run(
            goal="等待能力路由任务",
            thought="模拟缺少工具。",
            plan=["请求能力"],
        )
        agent.subagents.record_capability_request(
            request_task.id,
            problem="缺少真实入口验收工具。",
            needed_capability="browser_smoke_test",
        )

        broken = agent.subagents.create_run(
            goal="坏通道任务",
            thought="模拟 output.json 损坏。",
            plan=["probe"],
        )
        Path(broken.output_json).unlink()
        agent.subagents.probe_channel(broken.id)

        report = agent.subagents.write_action_plan(
            CapabilityConfig(
                subagent_heartbeat_timeout=1,
                subagent_run_timeout=1,
                subagent_min_evidence_for_done=1,
            )
        )
        actions = {(item.run_id, item.action): item for item in report.actions}

        assert (stale.id, "takeover_or_reassign") in actions
        assert "heartbeat_stale" in actions[(stale.id, "takeover_or_reassign")].source_issue_kinds
        assert "run_timeout" in actions[(stale.id, "takeover_or_reassign")].source_issue_kinds
        assert (fake_done.id, "reopen_for_evidence") in actions
        assert (request_task.id, "route_capability_request") in actions
        assert (broken.id, "probe_or_repair_channel") in actions
        assert all(item.dry_run for item in report.actions)
        assert (root / "subs" / "subagent_action_plan.json").exists()
        assert (root / "subs" / "SUBAGENT_ACTION_PLAN.md").exists()


def test_subagent_action_apply_dry_run_and_apply():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        capability_config = CapabilityConfig(
            subagent_heartbeat_timeout=1,
            subagent_run_timeout=1,
            subagent_min_evidence_for_done=1,
        )

        fake_done = agent.subagents.create_run(
            goal="需要补证据",
            thought="模拟缺证据完成。",
            plan=["标记完成"],
        )
        agent.subagents.set_status(fake_done.id, "DONE")
        dry_report = agent.subagents.write_action_apply_report(
            capability_config,
            action_filter="reopen_for_evidence",
            run_id=fake_done.id,
        )
        assert dry_report.dry_run
        assert dry_report.records[0].applied is False
        assert agent.subagents.load(fake_done.id).status == "DONE"

        apply_report = agent.subagents.write_action_apply_report(
            capability_config,
            apply=True,
            action_filter="reopen_for_evidence",
            run_id=fake_done.id,
        )
        reopened = agent.subagents.load(fake_done.id)
        assert not apply_report.dry_run
        assert apply_report.records[0].applied
        assert reopened.status == "BLOCKED"
        assert reopened.failure_type == "missing_evidence"
        assert (root / "subs" / "subagent_action_apply_log.jsonl").exists()
        assert (root / "subs" / "ACTION_APPLY_LOG.md").exists()

        stale = agent.subagents.create_run(
            goal="需要接管",
            thought="模拟超时。",
            plan=["执行"],
        )
        loaded = agent.subagents.load(stale.id)
        loaded.created_at = time.time() - 30
        loaded.heartbeat_at = time.time() - 30
        agent.subagents.save(loaded)
        missing_owner = agent.subagents.write_action_apply_report(
            capability_config,
            apply=True,
            action_filter="takeover_or_reassign",
            run_id=stale.id,
        )
        assert not missing_owner.records[0].ok
        assert agent.subagents.load(stale.id).status == "PLANNING"

        takeover_report = agent.subagents.write_action_apply_report(
            capability_config,
            apply=True,
            action_filter="takeover_or_reassign",
            run_id=stale.id,
            take_over_by="parent-supervisor",
            locked_files=["src/example.py"],
        )
        taken = agent.subagents.load(stale.id)
        assert takeover_report.records[0].ok
        assert taken.status == "TAKEN_OVER"
        assert taken.takeover_by == "parent-supervisor"
        assert "src/example.py" in taken.locked_files
        assert Path(taken.takeover_file).exists()


def test_subagent_action_apply_repairs_work_order():
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
            problem="当前需要请求 REST API 并检查 HTTP 状态码和 JSON 返回。",
            needed_capability="http_request",
            expected_output="接口状态码和返回体摘要",
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
            problem="需要检查 REST API 返回和错误码。",
            needed_capability="api_testing",
            expected_output="API 检查报告",
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
            problem="需要 zzz_unmatched_capability_999 完成一个不存在的能力。",
            needed_capability="zzz_unmatched_capability_999",
            expected_output="未知输出",
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


def test_subagent_execution_context_uses_only_grants():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="检查接口健康",
            thought="只允许读取文件，缺接口检查能力时向父代理请求。",
            plan=["读取代码", "请求能力", "写验收证据"],
            allowed_tools=["read_file"],
            acceptance_checks=["必须有接口检查证据"],
        )
        request = agent.subagents.record_capability_request(
            task.id,
            problem="需要发起 HTTP GET 检查接口状态。",
            needed_capability="http_request",
            expected_output="接口状态码和摘要",
        )
        agent.subagents.record_capability_grant(
            task.id,
            request_id=request.id,
            skills=["api-check"],
            tools=["http_request"],
            capability_cards=[
                {
                    "id": "tool:http_request",
                    "kind": "tool",
                    "name": "http_request",
                    "description": "发起 HTTP 请求并返回状态码和响应摘要",
                    "risk_level": "low",
                    "source": "builtin",
                    "path": "",
                }
            ],
            reason="父代理授权低风险接口健康检查。",
        )
        agent.subagents.record_evidence(
            task.id,
            kind="command",
            summary="接口 smoke test 通过",
            command="python3 smoke_api.py",
        )

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
        assert Path(context.execution_context_json).exists()
        assert Path(context.execution_context_file).exists()
        assert "SUBAGENT EXECUTION CONTEXT" in markdown
        assert "http_request" in markdown


def test_subagent_runner_dry_run_and_execute():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="读取配置并总结",
            thought="只允许读取文件，不允许写文件。",
            plan=["读取", "总结", "等待验收"],
            allowed_tools=["read_file"],
            acceptance_checks=["输出里说明已读取的文件"],
        )

        dry = agent.run_subagent(task.id, dry_run=True, instruction="先做 dry-run。")
        dry_loaded = agent.subagents.load(task.id)
        dry_prompt = Path(dry_loaded.runner_prompt_file).read_text(encoding="utf-8")

        assert dry.dry_run
        assert dry.ok
        assert dry_loaded.status == "PLANNING"
        assert Path(dry_loaded.execution_context_json).exists()
        assert Path(dry_loaded.runner_result_file).exists()
        assert "SubAgent Runner Task" in dry_prompt

        executed = agent.run_subagent(task.id, dry_run=False, instruction="用 echo 后端执行。")
        loaded = agent.subagents.load(task.id)
        output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
        prompt = Path(loaded.runner_prompt_file).read_text(encoding="utf-8")
        response = Path(loaded.runner_response_file).read_text(encoding="utf-8")

        assert not executed.dry_run
        assert executed.ok
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"
        assert output["dry_run"] is False
        assert output["next_action"] == "run_acceptance"
        assert "read_file [filesystem]" in prompt
        assert "write_file [filesystem]" not in prompt
        assert "echo 后端" in response


def test_subagent_runner_parses_structured_output():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        agent.backend = StructuredSubagentBackend()
        task = agent.subagents.create_run(
            goal="检查接口健康",
            thought="先读代码，如果缺 HTTP 能力则上抛。",
            plan=["读取", "上抛能力请求"],
            allowed_tools=["read_file"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(task.id)
        output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
        runner_json = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))

        assert result.structured_output_found
        assert result.structured_output_ok
        assert result.evidence_count == 1
        assert result.capability_request_count == 1
        assert result.artifact_count == 1
        assert result.test_count == 1
        assert result.patch_count == 1
        assert result.lesson_count == 1
        assert loaded.status == "BLOCKED"
        assert loaded.verification_status == "UNVERIFIED"
        assert loaded.failure_type == "capability_request"
        assert loaded.evidence[0].summary == "已确认需要接口健康检查"
        assert loaded.capability_requests[0].needed_capability == "http_request"
        assert loaded.capability_requests[0].status == "OPEN"
        assert loaded.used_tools == ["read_file"]
        assert "write_file" not in loaded.used_tools
        assert output["next_action"] == "route_capability_request"
        assert output["structured_output"]["ignored_unauthorized_tools"] == ["write_file"]
        assert output["structured_output"]["capability_request_count"] == 1
        assert output["artifacts"][0]["path"] == "reports/api_notes.md"
        assert output["tests"][0]["name"] == "static-read"
        assert output["patches"][0]["status"] == "planned"
        assert output["lessons"] == ["缺少线上检查工具时，不要把静态阅读当成接口可用证据"]
        assert output["next_actions"] == ["route_capability_request", "rerun_subagent_after_grant"]
        assert runner_json["blocked_reason"] == "当前上下文没有授权 HTTP 请求工具"
        debrief = Path(loaded.debrief_file).read_text(encoding="utf-8")
        assert "Runner Artifacts" in debrief
        assert "Runner Lessons" in debrief


def test_subagent_runner_parser_uses_last_parseable_fenced_block():
    text = (
        "模型先在说明里提到了协议标记。\n"
        "- 输出 `[SUBAGENT_RESULT]` 标记。\n"
        "- 输出 `[/SUBAGENT_RESULT]` 结束标记。\n\n"
        "# [SUBAGENT_RESULT]\n"
        "```json\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "真实 runner 输出里 JSON 被 Markdown fence 包住。",\n'
        '  "used_tools": ["read_file"],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "read_file", "summary": "读取 SPEC.md", "ok": true}],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [{"name": "format", "command": "", "ok": true, "summary": "parsed"}],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "```\n"
        "[/SUBAGENT_RESULT]\n"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.found
    assert parsed.ok
    assert parsed.status == "AWAITING_ACCEPTANCE"
    assert parsed.summary == "真实 runner 输出里 JSON 被 Markdown fence 包住。"
    assert parsed.used_tools == ["read_file"]
    assert parsed.evidence[0]["summary"] == "读取 SPEC.md"
    assert parsed.tests[0]["name"] == "format"
