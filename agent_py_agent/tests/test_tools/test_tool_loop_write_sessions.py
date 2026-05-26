"""LLM: focused tool-loop tests for malformed tool markers and open write sessions.

给人看的解释：
这个文件只放工具循环里和协议修复、分块写入收口相关的测试，避免主 test_tool_loop 文件继续变大。
"""

import json
import re
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.open_write_session_config import OpenWriteSessionConfig
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: _MalformedToolMarkerBackend proves parser repair stays inside the real tool loop.
# 类用途: 第一次故意少写 TOOL_CALL 右中括号，第二次确认系统给了格式修复提示。
class _MalformedToolMarkerBackend:
    name = "fake_malformed_tool_marker_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "工具调用开始标记格式错误" in prompt
        assert "请重新输出标准工具调用格式" in prompt
        return ModelResponse(text="工具调用格式已修复。", backend=self.name)


# LLM: _UnfinishedWriteSessionBackend models a large-file write that forgets finish once.
# 类用途: 验证 open file_write_session 不会被普通最终回答绕过，系统会要求 finish 后再收口。
class _UnfinishedWriteSessionBackend:
    name = "fake_unfinished_write_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls == 3:
            return ModelResponse(text="文件已经写好了。", backend=self.name)
        if self.calls == 4:
            assert "open_file_write_sessions" in prompt
            assert "payload.finish_tool_call" in prompt
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="分块文件已提交。", backend=self.name)


# LLM: _WrongRootOpenSessionBackend reproduces task workspaces that differ from the agent root.
# 类用途: 第一轮打开分块写入；第二轮故意 list_files。系统必须按 tools.workspace_root 找到 open session 并拦截。
class _WrongRootOpenSessionBackend:
    name = "fake_wrong_root_open_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 3:
            assert session_id
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls == 4:
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="分块文件已提交。", backend=self.name)


# LLM: _DelayedFinishWriteSessionBackend ignores open-session repair for several model turns before finishing.
# 类用途: 验证 open session 只做周期提醒，不再因为固定 2 次忽略直接阻断任务。
class _DelayedFinishWriteSessionBackend:
    name = "fake_delayed_finish_write_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls == 3:
            return ModelResponse(text="文件已经写好了。", backend=self.name)
        if self.calls in {4, 5}:
            assert "open_file_write_sessions" in prompt
            return ModelResponse(text="文件已经写好了。", backend=self.name)
        if self.calls == 6:
            assert "open_file_write_sessions" in prompt
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="延迟提交后完成。", backend=self.name)


# LLM: _UnrelatedToolDuringOpenSessionBackend proves open sessions do not freeze unrelated research.
# 类用途: 分块写入未提交时，非冲突工具调用应继续执行，只在关键收口点保护事务。
class _UnrelatedToolDuringOpenSessionBackend:
    name = "fake_unrelated_tool_during_open_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls == 3:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 4:
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="文件已经写好了。", backend=self.name)


# LLM: _PeriodicOpenSessionReminderBackend checks non-conflicting tool turns still accumulate reminders.
# 类用途: open session 期间连续做非冲突工具时，系统按配置周期提醒，但不阻断工具执行。
class _PeriodicOpenSessionReminderBackend:
    name = "fake_periodic_open_session_reminder_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls in {3, 4}:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 5:
            assert "periodic_open_session_reminder" in prompt
            assert "open_file_write_sessions" in prompt
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="周期提醒后已提交。", backend=self.name)


# LLM: _BeginOnlyWriteSessionBackend opens a session so tests can inspect manifest scope.
# 类用途: 只创建分块写入会话，不提交，用于验证工具循环注入机器 request/run/task id。
class _BeginOnlyWriteSessionBackend:
    name = "fake_begin_only_write_session_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        return ModelResponse(
            text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
            backend=self.name,
        )


# LLM: _UnrelatedWriteAfterStaleSessionBackend proves stale sessions do not poison later runs.
# 类用途: 旧 run 留下 open file_write_session 时，新 run 仍然可以写入不相关目标。
class _UnrelatedWriteAfterStaleSessionBackend:
    name = "fake_unrelated_write_after_stale_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","path":"fresh.txt","content":"ok"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="新任务完成。", backend=self.name)


# LLM: _RepairFailedHtmlSessionBackend reproduces a staged commit failure followed by reset rewrite.
# 类用途: 先写坏 HTML 并触发 finish 失败，再根据 manifest last_finish_error 使用 reset 重写。
class _RepairFailedHtmlSessionBackend:
    name = "fake_repair_failed_html_session_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"big.html"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text=_bad_html_append_call(session_id), backend=self.name)
        if self.calls == 3:
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        if self.calls == 4:
            assert "ARTIFACT_INTEGRITY_FAILED" in prompt
            assert "reset_tool_call" in prompt
            return ModelResponse(text=_reset_call(session_id), backend=self.name)
        if self.calls == 5:
            return ModelResponse(text=_append_call(session_id), backend=self.name)
        if self.calls == 6:
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="坏 HTML 已重写并提交。", backend=self.name)


# LLM: malformed tool markers must not become user-visible final answers.
# 函数用途: 复现真实 E2E 中 `[TOOL_CALL` 少写 `]` 后被当最终回复的问题。
def test_tool_loop_recovers_malformed_tool_opening_marker():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _MalformedToolMarkerBackend()

        result = agent.run("读取 notes 后继续", save=False, allowed_tools=["read_file"])

        assert result.response == "工具调用格式已修复。"
        assert result.tool_rounds == 1
        assert agent.backend.calls == 2


# LLM: open file_write_session manifests are machine state and must block final closeout.
# 函数用途: 模型忘记 finish 分块写入时，工具循环应继续要求提交目标文件，而不是假装完成。
def test_tool_loop_requires_finish_for_open_file_write_session():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _UnfinishedWriteSessionBackend()

        result = agent.run("写一个较大的 HTML 文件", save=False, allowed_tools=["file_write_session"])

        assert result.response == "分块文件已提交。"
        assert result.tool_rounds == 4
        assert agent.backend.calls == 5
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: open-session guards must use the real tool workspace, not a stale agent root.
# 函数用途: 复现真实任务隔离目录下 agent.root 与 tools.workspace_root 不一致时，open session 被旁路检查绕过的问题。
def test_tool_loop_open_session_guard_uses_tool_workspace_root():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td) / "task-workspace"
        unrelated_root = Path(td) / "unrelated-root"
        unrelated_root.mkdir()
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        agent.root = unrelated_root
        agent.backend = _WrongRootOpenSessionBackend()

        result = agent.run(
            "写一个较大的 HTML 文件",
            save=False,
            allowed_tools=["file_write_session", "list_files"],
        )

        assert result.response == "分块文件已提交。"
        assert agent.backend.calls == 5
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: open-session reminders must not hard-block a later valid finish.
# 函数用途: 连续几轮未处理 open session 后，如果模型最终 finish，任务仍应正常完成。
def test_tool_loop_open_session_reminders_do_not_block_late_finish():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=8)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _DelayedFinishWriteSessionBackend()

        result = agent.run("写一个较大的 HTML 文件", save=False, allowed_tools=["file_write_session"])

        assert result.response == "延迟提交后完成。"
        assert result.runtime_status == "ok"
        assert result.runtime_reason == ""
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: open sessions protect the staged target without freezing unrelated tools.
# 函数用途: open file_write_session 期间允许 list_files 这类非冲突工具继续执行，最终仍必须 finish。
def test_tool_loop_allows_unrelated_tools_while_file_write_session_open():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=8)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _UnrelatedToolDuringOpenSessionBackend()

        result = agent.run(
            "写一个较大的 HTML 文件，中途可以继续检查目录",
            save=False,
            allowed_tools=["file_write_session", "list_files"],
        )

        assert result.response == "文件已经写好了。"
        assert result.executed_tools == [
            "file_write_session",
            "file_write_session",
            "list_files",
            "file_write_session",
        ]
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: non-conflicting turns should still receive periodic open-session reminders.
# 函数用途: 验证周期提醒按模型回合累计，不因放行 list_files 而丢计数。
def test_tool_loop_periodically_reminds_during_unrelated_open_session_work():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=8)
        agent = SimpleAgent(cfg, workspace)
        agent._open_write_session_config = OpenWriteSessionConfig(reminder_interval=2)
        agent.backend = _PeriodicOpenSessionReminderBackend()

        result = agent.run(
            "写一个较大的 HTML 文件，中途可以继续检查目录",
            save=False,
            allowed_tools=["file_write_session", "list_files"],
        )

        assert result.response == "周期提醒后已提交。"
        assert result.executed_tools == [
            "file_write_session",
            "file_write_session",
            "list_files",
            "list_files",
            "file_write_session",
        ]
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: new file_write_session manifests must carry the active run scope.
# 函数用途: 后续 open session 修复只依赖 manifest.scope 机器字段，不靠提示词或历史 stdout 判断归属。
def test_tool_loop_writes_runtime_scope_into_file_write_session_manifest():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=1)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _BeginOnlyWriteSessionBackend()

        agent.run(
            "打开一个分块写入会话",
            save=False,
            allowed_tools=["file_write_session"],
            request_id="scope-request",
            run_id="scope-run",
            task_id="scope-task",
        )

        manifests = sorted((workspace / ".agent_file_write_sessions").glob("*/manifest.json"))
        assert len(manifests) == 1
        scope = json.loads(manifests[0].read_text(encoding="utf-8"))["scope"]
        assert scope == {
            "request_id": "scope-request",
            "run_id": "scope-run",
            "task_id": "scope-task",
        }


# LLM: stale open sessions must be scoped to their original request.
# 函数用途: 旧 run 崩溃留下的分块写入不能阻止后续独立任务调用普通写入工具。
def test_tool_loop_ignores_stale_scoped_open_file_write_session_for_new_run():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        _write_open_session_manifest(workspace, request_id="old-request")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _UnrelatedWriteAfterStaleSessionBackend()

        result = agent.run("写一个新文件", save=False, allowed_tools=["write_file"])

        assert result.response == "新任务完成。"
        assert agent.backend.calls == 2
        assert (workspace / "fresh.txt").read_text(encoding="utf-8") == "ok"


# LLM: failed staged commits must be repairable by reset without deleting the target contract.
# 函数用途: 验证 finish 失败后，工具循环把 last_finish_error 暴露给模型，模型可 reset 后从 chunk 0 重写。
def test_tool_loop_repairs_failed_html_session_with_reset():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=8)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _RepairFailedHtmlSessionBackend()

        result = agent.run("写一个较大的 HTML 文件", save=False, allowed_tools=["file_write_session"])

        assert result.response == "坏 HTML 已重写并提交。"
        assert agent.backend.calls == 7
        assert (workspace / "big.html").read_text(encoding="utf-8") == "<html><body>ok</body></html>"


# LLM: _append_call keeps the fake backend body small while preserving exact tool JSON.
# 函数用途: 构造追加 chunk 的 file_write_session 工具调用。
def _append_call(session_id: str) -> str:
    return (
        "[TOOL_CALL]\n"
        f'{{"tool":"file_write_session","action":"append","session_id":"{session_id}",'
        '"chunk_index":0,"content":"<html><body>ok</body></html>"}}\n'
        "[/TOOL_CALL]"
    )


# LLM: _bad_html_append_call creates a real artifact-integrity failure, not a prompt-only branch.
# 函数用途: 构造带 href="#" 且 </html> 后有正文的坏 HTML chunk。
def _bad_html_append_call(session_id: str) -> str:
    return (
        "[TOOL_CALL]\n"
        f'{{"tool":"file_write_session","action":"append","session_id":"{session_id}",'
        '"chunk_index":0,"content":"<html><body><a href=\'#\'>bad</a></body></html><p>late</p>"}}\n'
        "[/TOOL_CALL]"
    )


# LLM: _reset_call keeps reset repair tests tied to the public file_write_session contract.
# 函数用途: 构造清空 chunks 但保留 session/target 的 reset 工具调用。
def _reset_call(session_id: str) -> str:
    return (
        "[TOOL_CALL]\n"
        f'{{"tool":"file_write_session","action":"reset","session_id":"{session_id}","discard_chunks":true}}\n'
        "[/TOOL_CALL]"
    )


# LLM: _finish_call keeps the fake backend body small while preserving exact tool JSON.
# 函数用途: 构造提交 session 的 file_write_session 工具调用。
def _finish_call(session_id: str) -> str:
    return (
        "[TOOL_CALL]\n"
        f'{{"tool":"file_write_session","action":"finish","session_id":"{session_id}"}}\n'
        "[/TOOL_CALL]"
    )


# LLM: _session_id_from_prompt reads the structured tool result JSON emitted by the registry.
# 函数用途: 测试后端从上一轮工具回执中提取 begin 返回的 session_id。
def _session_id_from_prompt(prompt: str) -> str:
    match = re.search(r'"session_id":\s*"([^"]+)"', prompt)
    return match.group(1) if match else ""


# LLM: _write_open_session_manifest creates a scoped stale manifest without invoking tools.
# 函数用途: 构造旧 request 留下的 open file_write_session，验证新 run 按 scope 隔离。
def _write_open_session_manifest(workspace: Path, *, request_id: str) -> None:
    session = workspace / ".agent_file_write_sessions" / "stale-session"
    session.mkdir(parents=True)
    (session / "manifest.json").write_text(
        (
            '{"version":1,"session_id":"stale-session","status":"open",'
            '"target_path":{"raw":"old.txt","display":"old.txt","resolved":"'
            + str(workspace / "old.txt")
            + '"},'
            '"temp_path":{"resolved":"'
            + str(session / "write.tmp")
            + '"},'
            '"scope":{"request_id":"'
            + request_id
            + '","run_id":"","task_id":""},'
            '"chunks":{}}'
        ),
        encoding="utf-8",
    )
