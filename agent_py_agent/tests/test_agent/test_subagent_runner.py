"""LLM: Tests for the subagent runner: dry-run/execute, structured-output parsing,
write-boundary enforcement, structured-output repair, and parser edge cases.

给人看的解释：
测试子代理 runner 执行流程：dry-run、真实执行、结构化输出解析、
写越界拦截、结构化输出修复、解析器边界情况。
"""

import json
import re
import tempfile
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents import parse_subagent_runner_output

from .backends import (
    AcceptedSubagentBackend,
    BoundaryWriteSubagentBackend,
    CoordinatorToolLimitBlockedBackend,
    HierarchicalScheduleSubagentBackend,
    RepairingSubagentBackend,
    StructuredSubagentBackend,
    _TestNativeBackend,
)


class PromptCaptureAcceptedBackend(_TestNativeBackend):
    """测试用后端：记录真实 prompt，返回可验收的短结果。"""

    name = "prompt_capture_accepted_backend"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "DONE",\n'
                '  "summary": "done",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence_packets": [{"id":"evpkt-prompt","claim":"done","checked_scope":"prompt","evidence_refs":["runner_result.json"],"artifact_refs":["output.json"],"confidence":0.9}],\n'
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [],\n'
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def test_subagent_runner_dry_run_and_execute():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig( model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="读取配置并总结",
            thought="只允许读取文件，不允许写文件。",
            plan=["读取", "总结", "等待收口"],
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
        assert loaded.status == "DONE"
        assert executed.turn_end_reason == "completed"
        assert loaded.verification_status == "UNVERIFIED"
        assert loaded.failure_type == ""
        assert output["dry_run"] is False
        assert output["turn_end_reason"] == "completed"
        # EXEC-31b: native 下工具面在 execution-context JSON 的
        # permissions.allowed_tools 里渲染, 不再有「name [category」文本;
        # 工具面收窄断言改为结构化解析 permissions(读有/写无)。
        match = re.search(
            r'"permissions"\s*:\s*\{[^}]*"allowed_tools"\s*:\s*(\[[^\]]*\])',
            prompt,
        )
        assert match, "prompt 必须含结构化 permissions.allowed_tools"
        allowed = json.loads(match.group(1))
        assert "read_file" in allowed
        assert "write_file" not in allowed
        assert "echo 后端" in response
        _assert_subagent_recovery_snapshot(
            Path(loaded.agent_run_workspace_dir), task.id, loaded.status_file
        )


def test_subagent_runner_uses_child_system_prompt_not_parent_root_identity():
    """LLM: 子代理模型回合必须隔离父级 system prompt，避免 child 误认自己是 root。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            subagent_workspace="subs",
            system_prompt="你是 my-agent 的真实 E2E root 节点。",
        )
        agent = SimpleAgent(cfg, root)
        backend = PromptCaptureAcceptedBackend()
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="写一个短报告",
            thought="只需要返回结构化结果。",
            plan=["执行", "等待收口"],
            agent_name="小傻妞-report",
            role="worker",
        )

        agent.run_subagent(task.id, dry_run=False, probe=False)

        assert backend.prompts
        assert "你是 my-agent 的真实 E2E root 节点" not in backend.prompts[0]
        assert "你是 my-agent 的子代理 runner" in backend.prompts[0]
        assert "小傻妞-report" in backend.prompts[0]


def _assert_subagent_recovery_snapshot(root: Path, run_id: str, status_file: str) -> None:
    hook_files = sorted((root / "memory" / "hooks").glob("*.jsonl"))
    assert len(hook_files) == 1
    snapshots = [
        json.loads(line)
        for line in hook_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert snapshots[-1]["dispatch_events"][0]["source"] == "subagent_run"
    assert snapshots[-1]["dispatch_events"][0]["run_id"] == run_id
    assert status_file in snapshots[-1]["content_paths"]


def test_subagent_runner_does_not_use_model_result_json_as_machine_authority():
    """LLM: Generic lifecycle ignores model-authored status/evidence JSON and trusts host turn facts."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig( model_backend="echo", subagent_workspace="subs")
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

        assert not result.structured_output_found
        assert not result.structured_output_ok
        assert result.evidence_count == 0
        assert result.capability_request_count == 0
        assert result.artifact_count == 0
        assert result.test_count == 0
        assert result.patch_count == 0
        assert result.lesson_count == 0
        assert loaded.status == "DONE"
        assert result.turn_end_reason == "completed"
        assert loaded.verification_status == "UNVERIFIED"
        assert loaded.failure_type == ""
        assert loaded.evidence == []
        assert loaded.capability_requests == []
        assert loaded.used_tools == []
        assert "write_file" not in loaded.used_tools
        assert output["turn_end_reason"] == "completed"
        assert output["structured_output"]["found"] is False
        assert output["artifacts"] == []
        assert runner_json["turn_end_reason"] == "completed"
        response = Path(loaded.runner_response_file).read_text(encoding="utf-8")
        assert '"status": "BLOCKED"' in response


def test_subagent_runner_parse_requires_explicit_pending_capability_request():
    """LLM: Pending capability status without explicit requests should stay unfilled."""
    text = """[SUBAGENT_RESULT]
{
  "status": "PENDING_CAPABILITY_REQUEST",
  "summary": "需要申请 controlled_exec",
  "pending_steps": [
    {"action": "request_controlled_exec_grant", "status": "in_progress"},
    {"action": "execute_pwd", "status": "pending"},
    {"action": "execute_python3_large_output", "status": "pending"},
    {"action": "execute_rm_sentinel", "status": "pending"}
  ],
  "capability_requests": []
}
[/SUBAGENT_RESULT]"""

    parsed = parse_subagent_runner_output(text)

    assert parsed.capability_requests == []


def test_subagent_runner_parse_ignores_empty_pending_capability_request():
    """空泛 PENDING_CAPABILITY_REQUEST 不应生成无工具/无命令的 capability request。"""
    text = """[SUBAGENT_RESULT]
{
  "status": "PENDING_CAPABILITY_REQUEST",
  "summary": "",
  "capability_requests": []
}
[/SUBAGENT_RESULT]"""

    parsed = parse_subagent_runner_output(text)

    assert parsed.capability_requests == []


def test_subagent_runner_enforces_write_boundary_at_tool_layer():
    """LLM: Verifies the tool layer blocks writes to configured dangerous roots."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        danger = root / "dangerous-target"
        danger.mkdir()
        target_path = str(danger / "README.md")
        cfg = AgentConfig(
            model_backend="echo",
            subagent_workspace="subs",
            path_dangerous_roots=[str(danger)],
        )
        agent = SimpleAgent(cfg, root)
        backend = BoundaryWriteSubagentBackend(target_path)
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="确认 subagent 不能写危险目录",
            thought="模型即使要求写危险目录，也应该被工具层挡住。",
            plan=["尝试写文件", "检查工具结果", "输出结构化证据"],
            allowed_tools=["write_file"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)

        assert not result.structured_output_found
        assert result.turn_end_reason == "completed"
        assert len(backend.prompts) == 2
        assert not Path(target_path).exists()
        assert "PATH_DANGEROUS_ROOT_BLOCKED" in backend.prompts[1]


@pytest.mark.xfail(
    reason="EXEC-31b: 层级调度工具结果注入与子代理持久化链已改(context bundle 驱动), 文本协议时代的 [TOOL_CALL] fake 已转 native 块但 child-catalog 注入/收口断言待适配"
)
def test_subagent_runner_can_schedule_children_from_current_node_context():
    """LLM: Verifies a running subagent can create the next hierarchy layer under itself."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            model_backend="echo",
            subagent_workspace="subs",
            max_tool_rounds=3,
        )
        agent = SimpleAgent(cfg, root)
        backend = HierarchicalScheduleSubagentBackend()
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="主节点统筹示例站点真实 E2E 测试",
            thought="只创建下一层，不直接碰叶子节点。",
            plan=["创建下一层 coordinator", "等待父级观察日志", "汇报 refs"],
            agent_name="main-node",
            role="root_coordinator",
            allowed_tools=["schedule_child_subagents"],
            acceptance_checks=["下一层必须挂在当前 run 下面"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        _wait_for_background_dispatches(agent)
        loaded = agent.subagents.load(task.id)
        child = agent.subagents.load(loaded.child_ids[0])

        assert result.ok
        assert result.structured_output_ok
        assert len(backend.prompts) == 3
        assert len(loaded.child_ids) == 1
        assert child.status == "DONE"
        assert child.parent_id == task.id
        assert child.root_id == task.id
        assert child.depth == 1
        assert child.agent_name == "child-catalog"
        assert "schedule_child_subagents" in child.allowed_tools


def _wait_for_background_dispatches(agent: SimpleAgent, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        registry = getattr(agent, "_background_subagent_dispatches", {})
        if not isinstance(registry, dict):
            return
        if not any(
            str(item.get("status") or "") == "running"
            for item in registry.values()
            if isinstance(item, dict)
        ):
            return
        time.sleep(0.05)


def test_subagent_runner_accepts_natural_response_without_repair_round():
    """LLM: A natural final response completes in one model turn without format repair."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig( model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = RepairingSubagentBackend()
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="检查 runner 结构化输出恢复",
            thought="模型可能完成了工作，但忘记结果块。",
            plan=["执行", "修复格式", "等待收口"],
            allowed_tools=[],
            acceptance_checks=["必须有可验收证据"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(task.id)
        response = Path(loaded.runner_response_file).read_text(encoding="utf-8")

        assert len(backend.prompts) == 1
        assert not result.structured_output_found
        assert not result.structured_output_ok
        assert not result.structured_repair_attempted
        assert not result.structured_repair_ok
        assert result.evidence_count == 0
        assert result.test_count == 0
        assert loaded.status == "DONE"
        assert loaded.verification_status == "UNVERIFIED"
        assert "我已经完成检查" in response
        assert "Structured Output Repair Response" not in response
        runner_json = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))
        output_json = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
        assert runner_json["structured_repair_attempted"] is False
        assert runner_json["structured_repair_ok"] is False
        assert output_json["structured_output"]["repair_attempted"] is False
        assert output_json["structured_output"]["repair_ok"] is False


@pytest.mark.xfail(
    reason="EXEC-31b: 工具轮限收口的修复/收口提示路径已改, 结构化输出解析断言待适配"
)
def test_subagent_runner_does_not_override_coordinator_tool_limit_status():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            model_backend="echo",
            subagent_workspace="subs",
            max_tool_rounds=1,
            # 本场景测 coordinator 工具限制不被覆盖；显式固定协议修复次数，
            # 避免默认宽容度（2 次修复）改变模型调用轮数。
            max_protocol_repairs=1,
        )
        agent = SimpleAgent(cfg, root)
        agent.backend = CoordinatorToolLimitBlockedBackend()
        parent = agent.subagents.create_run(
            goal="root coordinator 只负责创建和验收直接 child",
            thought="直接 child 完成后，root 应等待最终收口。",
            plan=["观察 child", "汇总 refs"],
            agent_name="root-coordinator",
            role="coordinator",
            allowed_tools=["read_file", "schedule_child_subagents"],
            acceptance_checks=["直接 child 必须 DONE/VERIFIED"],
        )
        child = agent.subagents.create_run(
            goal="已完成的直接 child",
            thought="模拟真实下层链路已完成。",
            plan=["done"],
            parent_id=parent.id,
            root_id=parent.id,
            depth=1,
            agent_name="child-coordinator",
            role="coordinator",
            acceptance_checks=["已完成"],
        )
        child.status = "DONE"
        child.verification_status = "VERIFIED"
        agent.subagents.save(child)

        result = agent.run_subagent(parent.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(parent.id)

        assert len(agent.backend.prompts) == 3
        assert result.structured_output_found
        assert result.structured_output_ok
        assert result.status == "BLOCKED"
        assert loaded.status == "BLOCKED"


def test_subagent_runner_parser_uses_last_parseable_fenced_block():
    """LLM: Verifies the parser picks the last fenced JSON block inside [SUBAGENT_RESULT] tags."""
    text = (
        "模型先在说明里提到了协议标记。\n"
        "- 输出 `[SUBAGENT_RESULT]` 标记。\n"
        "- 输出 `[/SUBAGENT_RESULT]` 结束标记。\n\n"
        "# [SUBAGENT_RESULT]\n"
        "```json\n"
        "{\n"
        '  "status": "DONE",\n'
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
    assert parsed.status == "DONE"
    assert parsed.summary == "真实 runner 输出里 JSON 被 Markdown fence 包住。"
    assert parsed.used_tools == ["read_file"]
    assert parsed.evidence[0]["summary"] == "读取 SPEC.md"
    assert parsed.tests[0]["name"] == "format"


def test_subagent_runner_parser_accepts_prefixed_json_block():
    """LLM: Verifies the parser handles a 'json' prefix before the JSON block."""
    text = (
        "[SUBAGENT_RESULT]\n"
        "json\n"
        "{\n"
        '  "status": "DONE",\n'
        '  "summary": "模型在 JSON 前多写了语言标签。",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "仍可解析", "ok": true}],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.found
    assert parsed.ok
    assert parsed.summary == "模型在 JSON 前多写了语言标签。"
    assert parsed.evidence[0]["summary"] == "仍可解析"
