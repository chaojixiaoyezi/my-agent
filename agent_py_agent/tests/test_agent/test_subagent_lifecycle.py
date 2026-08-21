"""LLM: Tests for core subagent lifecycle: capability records, evidence
storage, work-order validation, takeover, board, due-check, and
channel probe status recording.

给人看的解释：
测试子代理核心生命周期：能力记录、证据存储、工单校验、接管、
看板、巡检、通道状态探测。
"""

import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
)


def test_subagent_capability_records():
    """LLM: Verifies capability requests, grants, and gaps are persisted on child runs."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="审查项目", thought="先拆分风险面。", plan=["拆任务", "分配能力"],
            agent_name="review-parent", role="coordinator",
            owner="parent-owner", supervisor="root-supervisor", final_owner="final-owner",
            acceptance_checks=["必须有真实命令或文件证据"],
        )
        child = agent.subagents.create_run(
            goal="检查 API 调用", thought="先确认接口行为。", plan=["读取代码", "请求能力"],
            agent_name="api-checker", role="worker", parent_id=parent.id, root_id=parent.root_id,
            depth=1, allowed_tools=["read_file"],
        )

        request = agent.subagents.lifecycle.record_capability_request(
            child.id, RecordCapabilityRequestParams(
                problem="当前只有 read_file，无法确认接口是否可访问。",
                needed_capability="http_check", expected_output="判断接口状态码和返回体", tried=["read_file"],
            ),
        )
        grant = agent.subagents.lifecycle.record_capability_grant(child.id, RecordCapabilityGrantParams(
            request_id=request.id, tools=["web_fetch"], reason="允许低风险 GET 检查。",
        ))
        gap = agent.subagents.lifecycle.record_capability_gap(child.id, RecordCapabilityGapParams(
            missing_capability="authenticated_api_check", why_failed="缺少登录态和安全授权。",
            attempted_tools=["web_fetch"], suggested_skill="api-auth-debugging",
        ))

        loaded_child = agent.subagents.load(child.id)
        loaded_parent = agent.subagents.load(parent.id)

        assert child.id in loaded_parent.child_ids
        assert loaded_parent.owner == "parent-owner" and loaded_parent.final_owner == "final-owner"
        assert loaded_child.capability_requests[0].id == request.id
        assert loaded_child.capability_grants[0].id == grant.id
        assert loaded_child.capability_gaps[0].id == gap.id
        assert "web_fetch" in loaded_child.allowed_tools


def test_subagent_done_status_does_not_require_machine_evidence():
    """LLM: DONE is a lifecycle fact; optional evidence is not a completion gate."""
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

        done = agent.subagents.lifecycle.set_status(
            task.id,
            "DONE",
            result="按钮交互本轮已经自然结束。",
        )

        assert done.status == "DONE"
        assert done.verification_status == "UNVERIFIED"
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
        sparks = Path(task.skill_sparks_file).read_text(encoding="utf-8")
        assert "promotion: requires review" in sparks
        assert "task-local" in sparks

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
            # owner 继承 manager.owner_id（B.4 授权门 owner 一致性：跨 owner 域
            # 接管是越权，拒绝场景见 test_authorization_gate.py 的 owner 不匹配用例）。
            final_owner="child-worker",
        )

        record = agent.subagents.record_takeover(
            task.id,
            take_over_by="parent-supervisor",
            reason="子代理长时间无交付证据，父代理接管收口。",
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
        agent.subagents.lifecycle.record_capability_request(
            created[3].id,
            RecordCapabilityRequestParams(
                problem="缺少网页检索能力。",
                needed_capability="web_search",
            ),
        )
        agent.subagents.lifecycle.set_status(created[7].id, "DONE")
        agent.subagents.lifecycle.set_status(created[11].id, "BLOCKED", failure_type="tool_failure")
        board = agent.subagents.board.write_board(recent_limit=10)

        assert board.summary["total"] == 25
        assert len(board.recent) == 10
        assert any("open_capability_request" in item.risk_flags for item in board.hot_list)
        assert any("blocked" in item.risk_flags for item in board.hot_list)
        assert (root / "subs" / "subagent_board.json").exists()
        assert (root / "subs" / "SUBAGENT_BOARD.md").exists()


def test_subagent_board_keeps_current_subagent_paths():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="tasks/current/work/agents")
        agent = SimpleAgent(cfg, root)
        agent.subagents.create_run(goal="整理材料", thought="测试当前路径直通", plan=["执行"])

        board = agent.subagents.board.write_board(recent_limit=10)
        board_text = (agent.subagents.workspace / "subagent_board.json").read_text(encoding="utf-8")

        assert board.summary["total"] == 1
        assert "/tasks/current/work/agents/" in board_text
        assert "[internal_legacy_subagent_path_hidden]" not in board_text


def _make_due_check_stale_active_run(agent):
    active = agent.subagents.create_run(
        goal="实现长任务巡检",
        thought="模拟长时间运行且等待能力路由的子代理。",
        plan=["执行", "上抛能力", "等待父代理处理"],
        owner="worker-a",
        final_owner="final-owner",
    )
    agent.subagents.lifecycle.record_capability_request(
        active.id,
        RecordCapabilityRequestParams(
            problem="当前工具无法验证真实入口。",
            needed_capability="browser_smoke_test",
        ),
    )
    agent.subagents.lifecycle.record_capability_gap(
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


def test_subagent_due_check_report():
    """LLM: Verifies due-check detects objective runtime, channel, and capability faults."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        _make_due_check_stale_active_run(agent)

        report = agent.subagents.board.write_due_check(
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

        ok_result = agent.subagents.channel_probe.probe_channel(task.id)
        ok_loaded = agent.subagents.load(task.id)

        assert ok_result.channel_status == "OK"
        assert ok_loaded.channel_status == "OK"
        assert ok_loaded.last_probe_at > 0
        assert ok_loaded.channel_checks
        assert Path(ok_loaded.channel_probe_file).exists()

        Path(ok_loaded.output_json).unlink()
        broken_result = agent.subagents.channel_probe.probe_channel(task.id)
        broken_loaded = agent.subagents.load(task.id)
        failed_names = {check.name for check in broken_result.checks if not check.ok}

        assert broken_result.channel_status == "BROKEN"
        assert broken_loaded.channel_status == "BROKEN"
        assert "work_order_files" in failed_names
        assert "output_json_readable" in failed_names
