"""LLM: Tests for the subagent runner: dry-run/execute, structured-output parsing,
write-boundary enforcement, structured-output repair, and parser edge cases.

给人看的解释：
测试子代理 runner 执行流程：dry-run、真实执行、结构化输出解析、
写越界拦截、结构化输出修复、解析器边界情况。
"""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import parse_subagent_runner_output

from .backends import (
    AcceptedSubagentBackend,
    BoundaryWriteSubagentBackend,
    CoordinatorToolLimitBlockedBackend,
    HierarchicalScheduleSubagentBackend,
    RepairingSubagentBackend,
    StructuredSubagentBackend,
)


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
        _assert_subagent_recovery_snapshot(root, task.id, loaded.status_file)


def _assert_subagent_recovery_snapshot(root: Path, run_id: str, status_file: str) -> None:
    # LLM: recovery snapshot assertions stay outside the runner flow test body.
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


def test_subagent_runner_parses_structured_output():
    """LLM: Verifies structured output parsing populates evidence, capability_requests, artifacts, etc."""
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
        assert loaded.used_tools == []
        assert "write_file" not in loaded.used_tools
        assert output["next_action"] == "route_capability_request"
        assert sorted(output["structured_output"]["ignored_unauthorized_tools"]) == ["read_file", "write_file"]
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


def test_subagent_runner_parse_recovers_pending_capability_request():
    """LLM: Pending capability status with pending_steps should recover a parent-routable request."""
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

    assert len(parsed.capability_requests) == 1
    request = parsed.capability_requests[0]
    assert request["needed_capability"] == "controlled_exec"
    assert request["requested_tools"] == ["controlled_exec"]
    assert request["requested_commands"] == ["pwd", "python3", "rm"]


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
    """LLM: Verifies the tool layer blocks writes outside allowed_write_roots."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = BoundaryWriteSubagentBackend()
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="确认 subagent 不能写出自己的工单目录",
            thought="模型即使要求写 README，也应该被工具层挡住。",
            plan=["尝试写文件", "检查工具结果", "输出结构化证据"],
            allowed_tools=["write_file"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)

        assert result.structured_output_found
        assert result.structured_output_ok
        assert len(backend.prompts) == 2
        assert not (root / "README.md").exists()
        assert "写入被阻止" in backend.prompts[1]


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
            goal="主节点统筹购物站点真实 E2E 测试",
            thought="只创建下一层，不直接碰叶子节点。",
            plan=["创建下一层 coordinator", "等待父级观察日志", "汇报 refs"],
            agent_name="main-node",
            role="root_coordinator",
            allowed_tools=["schedule_child_subagents"],
            acceptance_checks=["下一层必须挂在当前 run 下面"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(task.id)
        child = agent.subagents.load(loaded.child_ids[0])

        assert result.ok
        assert result.structured_output_ok
        assert len(backend.prompts) == 2
        assert len(loaded.child_ids) == 1
        assert child.parent_id == task.id
        assert child.root_id == task.id
        assert child.depth == 1
        assert child.agent_name == "小傻妞-child-catalog"
        assert "schedule_child_subagents" in child.allowed_tools


def test_subagent_runner_repairs_missing_structured_output():
    """LLM: Verifies the repair round-trip when the first model response lacks a structured block."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = RepairingSubagentBackend()
        agent.backend = backend
        task = agent.subagents.create_run(
            goal="检查 runner 结构化输出恢复",
            thought="模型可能完成了工作，但忘记结果块。",
            plan=["执行", "修复格式", "等待验收"],
            allowed_tools=[],
            acceptance_checks=["必须有可验收证据"],
        )

        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(task.id)
        response = Path(loaded.runner_response_file).read_text(encoding="utf-8")

        assert len(backend.prompts) == 2
        assert result.structured_output_found
        assert result.structured_output_ok
        assert result.structured_repair_attempted
        assert result.structured_repair_ok
        assert result.evidence_count == 1
        assert result.test_count == 1
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"
        assert "Structured Output Repair Response" in response
        runner_json = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))
        output_json = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
        assert runner_json["structured_repair_attempted"] is True
        assert runner_json["structured_repair_ok"] is True
        assert output_json["structured_output"]["repair_attempted"] is True
        assert output_json["structured_output"]["repair_ok"] is True


# LLM: coordinator finalization should trust verified direct children over a late tool-limit cleanup miss.
# 函数用途: 复现真实 E2E 中 root 已经带出完成子链路，却因为最后多查一次撞到工具上限被误标 BLOCKED 的问题。
def test_subagent_runner_keeps_completed_coordinator_awaiting_acceptance_after_tool_limit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            model_backend="echo",
            subagent_workspace="subs",
            max_tool_rounds=0,
        )
        agent = SimpleAgent(cfg, root)
        agent.backend = CoordinatorToolLimitBlockedBackend()
        parent = agent.subagents.create_run(
            goal="root coordinator 只负责创建和验收直接 child",
            thought="直接 child 完成后，root 应等待父级验收。",
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
        assert result.status == "AWAITING_ACCEPTANCE"
        assert result.verification_status == "NEEDS_ACCEPTANCE"
        assert result.blocked_reason == ""
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.failure_type == ""
        assert child.id in result.structured_summary


def test_subagent_runner_parser_uses_last_parseable_fenced_block():
    """LLM: Verifies the parser picks the last fenced JSON block inside [SUBAGENT_RESULT] tags."""
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


def test_subagent_runner_parser_accepts_prefixed_json_block():
    """LLM: Verifies the parser handles a 'json' prefix before the JSON block."""
    text = (
        "[SUBAGENT_RESULT]\n"
        "json\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
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
