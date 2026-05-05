"""LLM: Tests for core subagent lifecycle: capability records, evidence
requirements, work-order validation, takeover, board, due-check, and
channel probe status recording.

给人看的解释：
测试子代理核心生命周期：能力记录、证据要求、工单校验、接管、
看板、巡检、通道状态探测。
"""

import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
)


def test_subagent_capability_records():
    """LLM: Verifies capability requests, grants, and gaps are persisted on child runs."""
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
            RecordCapabilityGrantParams(
                request_id=request.id,
                tools=["fetch_url"],
                reason="允许低风险 GET 检查。",
            ),
        )
        gap = agent.subagents.record_capability_gap(
            child.id,
            RecordCapabilityGapParams(
                missing_capability="authenticated_api_check",
                why_failed="缺少登录态和安全授权。",
                attempted_tools=["fetch_url"],
                suggested_skill="api-auth-debugging",
            ),
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
    """LLM: Verifies that setting DONE without evidence raises ValueError."""
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
    """LLM: Verifies work-order directory structure and missing-file detection."""
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
    """LLM: Verifies that recording a takeover persists locked files and owner."""
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
    """LLM: Verifies board summary, hot-list risk flags, and file output at scale."""
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
    """LLM: Verifies due-check detects stale heartbeats, timeouts, fake-done, and gaps."""
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
            RecordCapabilityGapParams(
                missing_capability="browser_smoke_test",
                why_failed="没有浏览器自动化工具授权。",
                suggested_tool="playwright_smoke",
            ),
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
    """LLM: Verifies channel probe records OK/OK or BROKEN when output_json is missing."""
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
