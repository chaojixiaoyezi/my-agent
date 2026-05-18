"""LLM: focused tool-loop tests for malformed tool markers and open write sessions.

给人看的解释：
这个文件只放工具循环里和协议修复、分块写入收口相关的测试，避免主 test_tool_loop 文件继续变大。
"""

import re
import tempfile
from pathlib import Path

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
            assert "必须先调用 file_write_session finish" in prompt
            return ModelResponse(text=_finish_call(session_id), backend=self.name)
        return ModelResponse(text="分块文件已提交。", backend=self.name)


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
        assert result.tool_rounds == 3
        assert agent.backend.calls == 5
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
