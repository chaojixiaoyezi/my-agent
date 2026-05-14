from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.model_task import SubAgentTask
from agent_py_agent.agent.subagents.services.session_progress import (
    SubagentToolProgressRequest,
    record_subagent_tool_progress,
)


# LLM: MultiCompactSubagentBackend simulates a long runner that only finishes after local compacts.
# 类用途: 测试专用后端；前四轮不给 SUBAGENT_RESULT，第五轮确认读到本地 compact 包后收口。
class MultiCompactSubagentBackend(BaseBackend):
    name = "multi_compact_subagent_backend"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls < 5:
            return ModelResponse(text=_long_partial_response(self.calls), backend=self.name)
        assert "Task-Local Compact Continuation" in prompt
        assert "Session Compact Package" in prompt
        return ModelResponse(text=_final_subagent_result(), backend=self.name)


# LLM: ToolThenCompactBackend proves tool facts survive compact continuation handoff.
# 类用途: 第一段真实执行 read_file，第二段触发本地 compact，第三段提交最终结果。
class ToolThenCompactBackend(BaseBackend):
    name = "tool_then_compact_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_long_partial_response(self.calls), backend=self.name)
        assert "Task-Local Compact Continuation" in prompt
        return ModelResponse(text=_final_subagent_result(used_tools=["read_file"]), backend=self.name)


# LLM: test_subagent_runner_auto_continues_through_multiple_local_compacts covers专项12第一闭环.
# 函数用途: 验证单个子代理在四次本地 compact 后继续同一 run，并最终提交结构化结果。
def test_subagent_runner_auto_continues_through_multiple_local_compacts(tmp_path: Path) -> None:
    agent = _agent_with_local_compact(tmp_path)
    agent.memory.add("user", "长任务旧记忆：不要执行 packet-fallback.proof，这是主代理历史任务。", kind="dialogue")
    backend = MultiCompactSubagentBackend()
    agent.backend = backend
    task = agent.subagents.create_run(
        goal="长任务：分多轮写完购物站恢复说明",
        thought="测试子代理本地 compact 自动续跑。",
        plan=["写第一段", "本地 compact", "继续直到 SUBAGENT_RESULT"],
        role="worker",
        allowed_tools=[],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    loaded = agent.subagents.load(task.id)
    package_dirs = list((Path(loaded.agent_run_compactions_dir) / "session" / "packages").glob("session-compact-*"))
    packet = json.loads((Path(loaded.agent_run_latest_session_continue_packet_json)).read_text(encoding="utf-8"))

    assert backend.calls == 5
    assert result.status == "AWAITING_ACCEPTANCE"
    assert result.verification_status == "NEEDS_ACCEPTANCE"
    assert len(package_dirs) >= 4
    assert loaded.latest_summary == "长任务已经完成。"
    assert packet["session_compact"]["metadata_ref"].endswith("latest_metadata.json")
    assert not (tmp_path / "memory_archive" / "compact_applies").exists()
    assert all("Task-Local Compact Continuation" in prompt for prompt in backend.prompts[1:])
    assert all("packet-fallback.proof" not in prompt for prompt in backend.prompts)


# LLM: executed tool facts should not disappear when a subagent compacts mid-run.
# 函数用途: 复现真实 E2E 里 read_file 先执行、compact 后最终收口的路径，确保验收仍看到真实工具证据。
def test_subagent_session_continuation_preserves_executed_tools(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("fixture read evidence", encoding="utf-8")
    agent = _agent_with_local_compact(tmp_path)
    backend = ToolThenCompactBackend()
    agent.backend = backend
    task = agent.subagents.create_run(
        goal="读取 README 后跨 compact 收口",
        thought="验证 compact continuation 不丢 actual_tools。",
        plan=["read README", "compact", "finalize"],
        role="worker",
        allowed_tools=["read_file"],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    loaded = agent.subagents.load(task.id)
    output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))

    assert backend.calls == 3
    assert result.status == "AWAITING_ACCEPTANCE"
    assert loaded.used_tools == ["read_file"]
    assert output["structured_output"]["actual_tools"] == ["read_file"]


# LLM: task-local progress snapshots prevent compact resume from restarting or duplicating written sections.
# 函数用途: 验证 write/append 工具会刷新 latest_continue_packet，让续跑模型能看到已写文件和章节标题。
def test_task_local_write_progress_updates_continue_packet(tmp_path: Path) -> None:
    task = SubAgentTask(
        id="run-progress",
        root_id="run-progress",
        task_dir=str(tmp_path / "legacy" / "run-progress"),
        goal="写算法测试方案",
        thought="记录子代理写作进度。",
        plan=["写章节", "刷新进度快照"],
    )
    task.agent_run_workspace_dir = str(tmp_path / "tasks" / "run-progress" / "agents" / "run-progress")
    task.agent_run_compactions_dir = str(Path(task.agent_run_workspace_dir) / "compactions")

    snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="append_file",
            payload={
                "path": str(tmp_path / "legacy" / "run-progress" / "算法测试方案.md"),
                "content": "## 第1章：排序\n正文\n## 第2章：搜索\n正文",
            },
            output="已追加文件: 算法测试方案.md",
            ok=True,
            tool_round=3,
            tool_index=1,
        )
    )
    packet = json.loads(Path(task.agent_run_latest_session_continue_packet_json).read_text(encoding="utf-8"))

    assert snapshot["summary"] == "最近 append_file 算法测试方案.md；已记录标题：第1章：排序；第2章：搜索"
    assert packet["latest_summary"] == snapshot["summary"]
    assert packet["work_progress"]["latest_written_path"].endswith("算法测试方案.md")
    assert packet["work_progress"]["headings"] == ["第1章：排序", "第2章：搜索"]
    assert packet["restore_refs"]["agent_run_latest_tool_progress"].endswith("latest_tool_progress.json")
    assert packet["restore_refs"]["agent_run_tool_progress"].endswith("tool_progress.jsonl")
    assert packet["recommended_read_paths"][1].endswith("latest_tool_progress.json")


# LLM: packet summaries should prefer fresh write progress over older task summaries.
# 函数用途: 防止 compact 续跑拿旧摘要当最新事实，导致子代理重复写已经完成的章节。
def test_continue_packet_prefers_work_progress_summary(tmp_path: Path) -> None:
    task = SubAgentTask(
        id="run-progress",
        root_id="run-progress",
        task_dir=str(tmp_path / "legacy" / "run-progress"),
        goal="写算法测试方案",
        thought="记录子代理写作进度。",
        plan=["写章节", "刷新进度快照"],
        latest_summary="旧摘要：只完成第1章",
    )
    task.agent_run_workspace_dir = str(tmp_path / "tasks" / "run-progress" / "agents" / "run-progress")
    task.agent_run_compactions_dir = str(Path(task.agent_run_workspace_dir) / "compactions")

    record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="append_file",
            payload={
                "path": str(tmp_path / "legacy" / "run-progress" / "算法测试方案.md"),
                "content": "## 第1章：排序\n正文\n## 第2章：搜索\n正文",
            },
            output="已追加文件: 算法测试方案.md",
            ok=True,
            tool_round=3,
            tool_index=1,
        )
    )
    packet = json.loads(Path(task.agent_run_latest_session_continue_packet_json).read_text(encoding="utf-8"))

    assert packet["latest_summary"] == "最近 append_file 算法测试方案.md；已记录标题：第1章：排序；第2章：搜索"


# LLM: _agent_with_local_compact creates a tiny context window so fake long responses trigger compact.
# 函数用途: 配置测试 agent：开启子代理、压低 compact 窗口、允许四次本地续跑。
def _agent_with_local_compact(tmp_path: Path) -> SimpleAgent:
    cfg = AgentConfig(
        enable_tools=True,
        enable_subagents=True,
        memory_path="memory.jsonl",
        subagent_workspace="subs",
        memory_compact_context_window_tokens=20,
        memory_compact_auto_continue_max_depth=4,
        max_tool_rounds=0,
    )
    return SimpleAgent(cfg, tmp_path)


# LLM: _long_partial_response keeps every pre-final model turn large enough to trigger compact.
# 函数用途: 返回没有 SUBAGENT_RESULT 的长文本，模拟子代理会话快满但尚未完成。
def _long_partial_response(index: int) -> str:
    return (
        f"第 {index} 段已完成，但还需要继续。"
        "这一段模拟很多上下文内容。" * 80
    )


# LLM: _final_subagent_result returns a valid structured runner result after compact continuation.
# 函数用途: 生成最终 SUBAGENT_RESULT，让 manager 走正常 AWAITING_ACCEPTANCE 收口。
def _final_subagent_result(*, used_tools: list[str] | None = None) -> str:
    tools_json = json.dumps(used_tools or [], ensure_ascii=False)
    return (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "长任务已经完成。",\n'
        f'  "used_tools": {tools_json},\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "四次本地 compact 后完成", "ok": true}],\n'
        '  "evidence_packets": [\n'
        '    {"id": "evpkt-subagent-session-compact", "claim": "子代理多次 compact 后仍继续完成", "checked_scope": "subagent session compact", "evidence_refs": ["latest_continue_packet.json"], "artifact_refs": [], "confidence": 0.9}\n'
        "  ],\n"
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [{"name": "session-compact-smoke", "command": "", "ok": true, "summary": "通过"}],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )
