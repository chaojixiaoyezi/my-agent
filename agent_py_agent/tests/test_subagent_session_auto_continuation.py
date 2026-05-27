from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.model_task import SubAgentTask
from agent_py_agent.agent.subagents.services.session_progress import (
    SubagentToolProgressRequest,
    record_runtime_subagent_tool_progress,
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
        goal="长任务：分多轮写完示例站恢复说明",
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
    assert result.status == "DONE"
    assert result.verification_status == "VERIFIED"
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
    assert result.status == "DONE"
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
            tool="apply_patch",
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

    assert snapshot["summary"] == "最近 apply_patch 算法测试方案.md；已记录标题：第1章：排序；第2章：搜索"
    assert packet["latest_summary"] == snapshot["summary"]
    assert packet["work_progress"]["latest_written_path"].endswith("算法测试方案.md")
    assert packet["work_progress"]["headings"] == ["第1章：排序", "第2章：搜索"]
    assert packet["restore_refs"]["agent_run_latest_tool_progress"].endswith("latest_tool_progress.json")
    assert packet["restore_refs"]["agent_run_tool_progress"].endswith("tool_progress.jsonl")
    assert packet["recommended_read_paths"][1].endswith("latest_tool_progress.json")


# LLM: read-only tool artifacts are sources, not product progress anchors.
# 函数用途: 防止 web_search/web_fetch 的 archive JSON 被误当成交付产物，导致子代理续跑提示偏向反复读取 progress。
def test_read_only_tool_artifact_does_not_update_task_local_product_progress(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)

    snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="web_search",
            payload={"query": "DeepSeek paper 2026"},
            output=json.dumps(
                {
                    "ok": True,
                    "artifact_ref": str(tmp_path / "memory_archive" / "tool_outputs" / "web_search-1.json"),
                    "artifact_path": str(tmp_path / "memory_archive" / "tool_outputs" / "web_search-1.json"),
                }
            ),
            ok=True,
            tool_round=1,
            tool_index=1,
        )
    )

    assert snapshot == {}
    assert not (Path(task.agent_run_workspace_dir) / "progress" / "latest_tool_progress.json").exists()


# LLM: read-only progress should remain visible in the tree without becoming product progress.
# 函数用途: 父代理看子代理 summary/tree 时能看到 read-only 工具仍在推进，避免误判为卡住后重派。
def test_read_only_tool_updates_observable_subagent_progress_without_product_snapshot(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)
    saved: list[SubAgentTask] = []

    class Manager:
        def load(self, run_id: str) -> SubAgentTask:
            assert run_id == task.id
            return task

        def save(self, item: SubAgentTask) -> None:
            saved.append(item)

    agent = SimpleNamespace(subagents=Manager())
    record_runtime_subagent_tool_progress(
        agent,
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id=task.id),
            payload={"query": "DeepSeek 2026 paper"},
            result=SimpleNamespace(
                tool="web_search",
                ok=True,
                output=json.dumps(
                    {
                        "ok": True,
                        "artifact_ref": str(tmp_path / "memory_archive" / "web_search.json"),
                    }
                ),
                result_envelope={},
            ),
            tool_rounds=2,
            idx=1,
        ),
    )

    assert saved
    assert task.current_tool == "web_search"
    assert task.last_progress_summary == "最近成功调用工具: web_search"
    assert task.latest_summary == "最近成功调用工具: web_search"
    assert task.current_step == "RUNNING"
    assert not (Path(task.agent_run_workspace_dir) / "progress" / "latest_tool_progress.json").exists()


# LLM: complete HTML progress should steer runners toward structured closeout instead of endless writing.
# 函数用途: 复现真实 E2E 里 HTML 已闭合但子代理继续读写不收口；进度包应提示写 output.json 交最终收口。
def test_task_local_write_progress_completed_html_prompts_output_json_closeout(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)
    artifact = tmp_path / "deliverables" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body><main id='hero'>done</main></body></html>", encoding="utf-8")

    snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="apply_patch",
            payload={"path": str(artifact), "content": "</body></html>"},
            output="已追加文件: index.html\nHTML 完整性提示: 当前结构没有发现明显问题。",
            ok=True,
            tool_round=3,
            tool_index=1,
        )
    )
    packet = json.loads(Path(task.agent_run_latest_session_continue_packet_json).read_text(encoding="utf-8"))

    assert "停止继续写正文" in snapshot["next_action"]
    assert "output.json" in snapshot["next_action"]
    assert packet["work_progress"]["next_action"] == snapshot["next_action"]


# LLM: fake hash links should keep the runner in repair mode before structured closeout.
# 函数用途: 复现家具页真实产物残留 href="#"；进度包应提示先修复明显失效链接，再写 output.json。
def test_task_local_write_progress_placeholder_hash_link_prompts_repair(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)
    artifact = tmp_path / "deliverables" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "<html><body><main id='hero'>done</main><a href='#'>品牌故事</a><a href='#missing'>空间系列</a></body></html>",
        encoding="utf-8",
    )

    snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="apply_patch",
            payload={"path": str(artifact), "content": ""},
            output="已修改文件: index.html",
            ok=True,
            tool_round=4,
            tool_index=1,
        )
    )

    assert "先修复" in snapshot["next_action"]
    assert "placeholder_hash_link" in snapshot["next_action"]
    assert "品牌故事 href=#" in snapshot["next_action"]
    assert snapshot["artifact_integrity"]["issues"][0]["code"] == "placeholder_hash_link"
    assert snapshot["artifact_integrity"]["issues"][0]["count"] == 1
    assert "品牌故事 href=#" in snapshot["artifact_integrity"]["issues"][0]["examples"]
    assert "空间系列 href=#missing" in snapshot["next_action"]


# LLM: many fake links should steer the runner toward batch repair instead of one-link loops.
# 函数用途: 复现真实 E2E 中 17 个 href="#" 被一轮只替换一个，进度包应提示批量修复策略。
def test_task_local_write_progress_many_placeholder_links_prompts_batch_repair(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)
    artifact = tmp_path / "deliverables" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        (
            "<html><body><main id='hero'>done</main>"
            "<a href='#'>查看全部产品</a><a href='#'>了解更多</a>"
            "<a href='#'>微信</a><a href='#'>微博</a><a href='#'>小红书</a>"
            "</body></html>"
        ),
        encoding="utf-8",
    )

    snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="apply_patch",
            payload={"path": str(artifact), "content": "</body></html>"},
            output="已追加文件: index.html",
            ok=True,
            tool_round=3,
            tool_index=1,
        )
    )

    assert "placeholder_hash_linkx5" in snapshot["next_action"]
    assert "一次性批量修复" in snapshot["next_action"]
    assert "apply_patch" in snapshot["next_action"]
    assert "不要一轮只替换一个链接" in snapshot["next_action"]


# LLM: internal output.json writes must not erase unresolved product repair facts.
# 函数用途: 复现真实 E2E 中 worker 写 output.json 后覆盖 href 问题；最新进度仍应指向产品文件和修复建议。
def test_task_local_output_json_closeout_preserves_product_integrity_progress(tmp_path: Path) -> None:
    task = _progress_task(tmp_path)
    output_json = tmp_path / "legacy" / "run-progress" / "output.json"
    output_json.parent.mkdir(parents=True)
    task.output_json = str(output_json)
    artifact = tmp_path / "deliverables" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "<html><body><main id='hero'>done</main><a href='#'>联系客服</a></body></html>",
        encoding="utf-8",
    )

    product_snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="apply_patch",
            payload={"path": str(artifact), "content": ""},
            output="已修改文件: index.html",
            ok=True,
            tool_round=8,
            tool_index=1,
        )
    )
    closeout_snapshot = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool="write_file",
            payload={"path": str(output_json), "content": '{"status":"DONE"}'},
            output="已写入文件: output.json",
            ok=True,
            tool_round=9,
            tool_index=1,
        )
    )
    packet = json.loads(Path(task.agent_run_latest_session_continue_packet_json).read_text(encoding="utf-8"))

    assert product_snapshot["latest_written_path"] == str(artifact)
    assert closeout_snapshot["latest_written_path"] == str(artifact)
    assert closeout_snapshot["closeout_written_path"] == str(output_json)
    assert "placeholder_hash_link" in closeout_snapshot["next_action"]
    assert closeout_snapshot["artifact_integrity"]["warning_codes"] == ["placeholder_hash_link"]
    assert packet["work_progress"]["latest_written_path"] == str(artifact)
    assert packet["work_progress"]["closeout_written_path"] == str(output_json)


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
            tool="apply_patch",
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

    assert packet["latest_summary"] == "最近 apply_patch 算法测试方案.md；已记录标题：第1章：排序；第2章：搜索"


# LLM: _progress_task keeps progress snapshot tests focused on state transitions, not task boilerplate.
# 函数用途: 创建带 agent_run_workspace/compactions 路径的最小子代理任务。
def _progress_task(tmp_path: Path) -> SubAgentTask:
    task = SubAgentTask(
        id="run-progress",
        root_id="run-progress",
        task_dir=str(tmp_path / "legacy" / "run-progress"),
        goal="写 HTML 产物",
        thought="记录子代理写作进度。",
        plan=["写文件", "刷新进度快照"],
    )
    task.agent_run_workspace_dir = str(tmp_path / "tasks" / "run-progress" / "agents" / "run-progress")
    task.agent_run_compactions_dir = str(Path(task.agent_run_workspace_dir) / "compactions")
    return task


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
# 函数用途: 生成最终 SUBAGENT_RESULT，让 manager 走正常 DONE 收口。
def _final_subagent_result(*, used_tools: list[str] | None = None) -> str:
    tools_json = json.dumps(used_tools or [], ensure_ascii=False)
    return (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "DONE",\n'
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
