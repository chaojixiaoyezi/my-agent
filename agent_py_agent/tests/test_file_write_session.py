from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling.file_write_session import FileWriteSessionTool


# LLM: Tests read envelopes instead of prose so session failures stay machine-actionable.
# 函数用途: 创建测试工作区里的 FileWriteSessionTool，允许按场景收紧 chunk 上限。
def _tool(workspace: Path, *, max_chunk_chars: int = 32) -> FileWriteSessionTool:
    workspace.mkdir()
    return FileWriteSessionTool(workspace, max_chunk_chars=max_chunk_chars)


# LLM: Session ids come from begin envelopes and must be reused by append/finish/abort calls.
# 函数用途: 启动写入 session 并返回工具结果里的 session_id。
def _begin(tool: FileWriteSessionTool, target_path: str = "out/large.txt") -> str:
    result = tool.execute({"action": "begin", "target_path": target_path})
    assert result.ok is True
    assert result.result_envelope["target_path"]["raw"] == target_path
    assert result.result_envelope["manifest_path"].endswith("manifest.json")
    return str(result.result_envelope["session_id"])


# LLM: FileWriteSessionTool contract tests must cover recoverable large-write sessions, not prompt prose.
# 类用途: 覆盖大文件写入 session 的成功、幂等、错误和边界场景。
class TestFileWriteSessionTool:
    # LLM: The happy path locks the large-write contract around staged chunks and atomic final commit.
    # 函数用途: 验证多 chunk 正常写入、manifest 记录和 finish 前不落最终文件。
    def test_multi_chunk_write_finishes_atomically(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)

        append_0 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "hello "})
        append_1 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 1, "content": "world"})

        target = workspace / "out" / "large.txt"
        manifest_path = Path(append_1.result_envelope["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert append_0.ok is True
        assert append_1.ok is True
        assert target.exists() is False
        assert manifest["target_path"]["display"] == "out/large.txt"
        assert manifest["chunks"]["0"]["size"] == 6
        assert manifest["chunks"]["1"]["size"] == 5

        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert finish.ok is True
        assert finish.result_envelope["target_path"]["display"] == "out/large.txt"
        assert target.read_text(encoding="utf-8") == "hello world"
        assert not manifest_path.parent.exists()

    # LLM: Retry-safe append is required because model/tool calls can be replayed after partial failure.
    # 函数用途: 验证相同 chunk_index 和相同内容重复提交时幂等，不重复写入。
    def test_duplicate_chunk_index_is_idempotent(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)

        first = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abc"})
        duplicate = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abc"})
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert first.ok is True
        assert duplicate.ok is True
        assert duplicate.result_envelope["duplicate"] is True
        assert (workspace / "out" / "large.txt").read_text(encoding="utf-8") == "abc"
        assert finish.result_envelope["chunks_committed"] == 1

    # LLM: Out-of-order chunks can be staged, but finish must reject gaps using a structured code.
    # 函数用途: 验证乱序提交可恢复，缺 chunk 时 finish 返回机器可读错误。
    def test_out_of_order_chunks_require_no_gaps_on_finish(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)

        append_1 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 1, "content": "world"})
        missing = tool.execute({"action": "finish", "session_id": session_id})

        assert append_1.ok is True
        assert missing.ok is False
        assert missing.error_code == "TOOL_INVALID_ARGUMENTS"
        assert missing.result_envelope["code"] == "MISSING_CHUNK"
        assert missing.result_envelope["missing_chunk_indexes"] == [0]
        assert not (workspace / "out" / "large.txt").exists()

        append_0 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "hello "})
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert append_0.ok is True
        assert finish.ok is True
        assert (workspace / "out" / "large.txt").read_text(encoding="utf-8") == "hello world"

    # LLM: Abort must clean staged state and prevent accidental final writes.
    # 函数用途: 验证 abort 删除 session 临时目录，后续 finish 失败且目标文件不存在。
    def test_abort_removes_session_state(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)
        append = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abc"})
        manifest_path = Path(append.result_envelope["manifest_path"])

        abort = tool.execute({"action": "abort", "session_id": session_id})
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert append.ok is True
        assert abort.ok is True
        assert abort.result_envelope["status"] == "aborted"
        assert not manifest_path.parent.exists()
        assert finish.ok is False
        assert finish.result_envelope["code"] == "SESSION_NOT_FOUND"
        assert not (workspace / "out" / "large.txt").exists()

    # LLM: The session tool must keep large chunks out of a single tool call even though final files may be large.
    # 函数用途: 验证超过单 chunk 上限时拒绝并返回结构化错误码。
    def test_rejects_oversized_chunk(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=4)
        session_id = _begin(tool)

        result = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abcde"})

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert result.result_envelope["code"] == "CHUNK_TOO_LARGE"
        assert result.result_envelope["max_chunk_chars"] == 4

    # LLM: Begin is the path boundary gate because it resolves the final target before staging content.
    # 函数用途: 验证目标路径越过工作区时拒绝，不创建 session 状态。
    def test_rejects_target_outside_workspace(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)

        result = tool.execute({"action": "begin", "target_path": "../outside.txt"})

        assert result.ok is False
        assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
        assert result.result_envelope["code"] == "PATH_OUTSIDE_WORKSPACE"
        assert not (tmp_path / "outside.txt").exists()
        assert not (workspace / ".agent_file_write_sessions").exists()
