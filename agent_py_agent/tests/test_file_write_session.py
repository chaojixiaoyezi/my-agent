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
        assert append_1.result_envelope["staging_contract"]["fact_source"] == "preview_and_chunks"
        assert append_1.result_envelope["staging_contract"]["commit_action"] == "finish"
        assert append_1.result_envelope["staging_contract"]["preview_materialized_after_append"] is True
        assert append_1.result_envelope["staging_contract"]["temp_path_materialized_on_finish"] is False
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

    # LLM: Staged previews make long writes recoverable even when a model times out before finish.
    # 函数用途: 验证每次 append 后都会组装可读取的 preview，并给出 finish/continue 的结构化下一步。
    def test_append_materializes_recoverable_preview_and_next_actions(self, tmp_path: Path):
        from agent_py_agent.agent.tooling.file_write_session_inspection import (
            open_file_write_sessions,
        )

        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=32)
        session_id = _begin(tool)

        append_0 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "hello "})
        append_1 = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 1, "content": "world"})

        preview_path = Path(append_1.result_envelope["preview_path"])
        assert append_0.ok is True
        assert append_1.ok is True
        assert preview_path.read_text(encoding="utf-8") == "hello world"
        assert append_1.result_envelope["preview_materialized"] is True
        assert append_1.result_envelope["next_chunk_index"] == 2
        assert append_1.result_envelope["finish_tool_call"] == {
            "tool": "file_write_session",
            "action": "finish",
            "session_id": session_id,
        }
        assert append_1.result_envelope["continue_tool_call"] == {
            "tool": "file_write_session",
            "action": "append",
            "session_id": session_id,
            "chunk_index": 2,
        }
        sessions = open_file_write_sessions(workspace, limit=5)
        assert sessions[0]["preview_path"] == str(preview_path)
        assert sessions[0]["preview_materialized"] is True
        assert sessions[0]["finish_tool_call"] == append_1.result_envelope["finish_tool_call"]
        assert append_1.result_envelope["staging_contract"]["fact_source"] == "preview_and_chunks"
        assert append_1.result_envelope["staging_contract"]["preview_materialized_after_append"] is True

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

    # LLM: Structured checkpoints must not commit invalid JSON because downstream recovery depends on parseable files.
    # 函数用途: 验证 .json 目标在 finish 前会做结构化校验；坏 JSON 要返回机器错误并保留 open session 继续修复。
    def test_finish_rejects_invalid_json_target_and_keeps_session_open(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=128)
        session_id = _begin(tool, target_path="out/data.json")

        append = tool.execute(
            {
                "action": "append",
                "session_id": session_id,
                "chunk_index": 0,
                "content": '[{"name":"demo"}',
            }
        )
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert append.ok is True
        assert finish.ok is False
        assert finish.result_envelope["code"] == "STRUCTURED_FILE_INVALID"
        assert finish.result_envelope["format"] == "json"
        assert finish.result_envelope["session_id"] == session_id
        assert not (workspace / "out" / "data.json").exists()

        repaired = tool.execute(
            {
                "action": "append",
                "session_id": session_id,
                "chunk_index": 1,
                "content": "]",
            }
        )
        repaired_finish = tool.execute({"action": "finish", "session_id": session_id})

        assert repaired.ok is True
        assert repaired_finish.ok is True
        assert json.loads((workspace / "out" / "data.json").read_text(encoding="utf-8")) == [
            {"name": "demo"}
        ]


# LLM: Structured-target validation tests stay in their own class to keep write-session contracts readable.
# 类用途: 覆盖 JSON checkpoint 提交前的结构校验和 open-session 保留行为。
class TestFileWriteSessionToolStructuredValidation:
    # LLM: Workbook-style JSON checkpoints must not commit duplicate sheet identities.
    # 函数用途: 验证 sheets/rows 这类通用表格 JSON 在提交前会检查机器结构，避免重复 sheet 进入下游 builder。
    def test_finish_rejects_duplicate_json_sheet_names_and_keeps_session_open(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=2048)
        session_id = _begin(tool, target_path="out/source_data.json")
        duplicate_sheet_payload = json.dumps(
            {
                "sheets": [
                    {"name": "week-1", "columns": ["project"], "rows": [{"project": "a"}]},
                    {"name": "week-1", "columns": ["project"], "rows": [{"project": "b"}]},
                ]
            },
            ensure_ascii=False,
        )

        append = tool.execute(
            {
                "action": "append",
                "session_id": session_id,
                "chunk_index": 0,
                "content": duplicate_sheet_payload,
            }
        )
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert append.ok is True
        assert finish.ok is False
        assert finish.result_envelope["code"] == "STAGED_JSON_DUPLICATE_SHEET_NAMES"
        assert finish.result_envelope["recommended_action"] == "repair_structured_checkpoint_json"
        assert not (workspace / "out" / "source_data.json").exists()

    # LLM: Abort must clean staged state and prevent accidental final writes.
    # 函数用途: 验证 abort 删除 session 临时目录，后续 finish 失败且目标文件不存在。
    def test_abort_removes_session_state(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)
        append = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abc"})
        manifest_path = Path(append.result_envelope["manifest_path"])

        abort = tool.execute({"action": "abort", "session_id": session_id, "discard_chunks": True})
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert append.ok is True
        assert abort.ok is True
        assert abort.result_envelope["status"] == "aborted"
        assert not manifest_path.parent.exists()
        assert finish.ok is False
        assert finish.result_envelope["code"] == "SESSION_NOT_FOUND"
        assert not (workspace / "out" / "large.txt").exists()

    # LLM: Abort must not discard already staged chunks unless the caller explicitly asks.
    # 函数用途: 验证已有 chunk 的 session 默认不能 abort，避免真实模型误删续跑进度。
    def test_abort_rejects_nonempty_session_without_discard_flag(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace)
        session_id = _begin(tool)
        append = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abc"})

        abort = tool.execute({"action": "abort", "session_id": session_id})

        assert append.ok is True
        assert abort.ok is False
        assert abort.result_envelope["code"] == "SESSION_HAS_CHUNKS"
        assert abort.result_envelope["session_id"] == session_id
        assert abort.result_envelope["next_chunk_index"] == 1

    # LLM: The session tool accepts fuzzy model chunk sizing by splitting payloads into bounded chunks.
    # 函数用途: 验证超过单 chunk 上限时自动拆分，避免真实模型因为块大小估算不准而卡住。
    def test_accepts_oversized_chunk_by_auto_splitting(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=4)
        session_id = _begin(tool)

        result = tool.execute({"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abcde"})

        assert result.ok is True
        assert result.result_envelope["auto_split"] is True
        assert result.result_envelope["max_chunk_chars"] == 4

    # LLM: Real models may recover from invalid inline writes by appending with a target path first.
    # 函数用途: 验证 append 带 target_path 时可自动创建 session，避免 begin/append 顺序稍错就卡死。
    def test_append_with_target_path_auto_starts_missing_session(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=16)

        append = tool.execute(
            {
                "action": "append",
                "session_id": "homepage-v1",
                "target_path": "out/index.html",
                "chunk_index": 0,
                "content": "<!doctype html>",
            }
        )
        finish = tool.execute({"action": "finish", "session_id": "homepage-v1"})

        assert append.ok is True
        assert append.result_envelope["auto_started"] is True
        assert append.result_envelope["target_path"]["display"] == "out/index.html"
        assert finish.ok is True
        assert (workspace / "out" / "index.html").read_text(encoding="utf-8") == "<!doctype html>"


# LLM: Stable id and path-boundary tests are kept in a separate class so the contract suite stays under code-size limits.
# 类用途: 覆盖 session_id 幂等、目标冲突、自动拆分和路径边界等补充场景。
class TestFileWriteSessionToolIdentityAndGuards:

    # LLM: Model-chosen session ids act as idempotency keys, matching 会话运行时 stable operation ids.
    # 函数用途: 验证 begin 接受调用方提供的 session_id；重复 begin 同目标返回同一 open session，避免随机 id 让后续 append 丢失。
    def test_begin_accepts_stable_session_id_and_reuses_same_target(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=16)

        first = tool.execute({
            "action": "begin",
            "session_id": "homepage-001",
            "target_path": "out/index.html",
        })
        second = tool.execute({
            "action": "begin",
            "session_id": "homepage-001",
            "target_path": "out/index.html",
        })
        append = tool.execute({
            "action": "append",
            "session_id": "homepage-001",
            "chunk_index": 0,
            "content": "<!doctype html>",
        })

        assert first.ok is True
        assert second.ok is True
        assert first.result_envelope["session_id"] == "homepage-001"
        assert second.result_envelope["duplicate"] is True
        assert append.ok is True

    # LLM: Stable session ids are operation keys, so one id cannot silently switch targets.
    # 函数用途: 验证相同 session_id 指向不同 target_path 时返回结构化冲突，避免写错文件。
    def test_begin_rejects_same_session_id_for_different_target(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=16)

        first = tool.execute({
            "action": "begin",
            "session_id": "homepage-001",
            "target_path": "out/index.html",
        })
        conflict = tool.execute({
            "action": "begin",
            "session_id": "homepage-001",
            "target_path": "out/other.html",
        })

        assert first.ok is True
        assert conflict.ok is False
        assert conflict.result_envelope["code"] == "SESSION_TARGET_CONFLICT"
        assert conflict.result_envelope["session_id"] == "homepage-001"

    # LLM: A target can have only one open write session, regardless of model-chosen ids.
    # 函数用途: 验证同一目标文件已有 open session 时，新 session_id 的 begin 会返回已有 session 供续写。
    def test_begin_rejects_new_session_when_target_already_has_open_session(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=16)

        first = tool.execute({
            "action": "begin",
            "session_id": "homepage-001",
            "target_path": "out/index.html",
        })
        append = tool.execute({
            "action": "append",
            "session_id": "homepage-001",
            "chunk_index": 0,
            "content": "<!doctype html>",
        })
        second = tool.execute({
            "action": "begin",
            "session_id": "homepage-002",
            "target_path": "out/index.html",
        })

        assert first.ok is True
        assert append.ok is True
        assert second.ok is False
        assert second.result_envelope["code"] == "TARGET_HAS_OPEN_SESSION"
        assert second.result_envelope["recommended_session_id"] == "homepage-001"
        assert second.result_envelope["next_chunk_index"] == 1

    # LLM: Oversized append payloads should become multiple chunks because model output limits are fuzzy.
    # 函数用途: 验证 append 超过 chunk 上限时自动拆分成连续 chunk，而不是直接失败。
    def test_append_auto_splits_oversized_content(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        tool = _tool(workspace, max_chunk_chars=4)
        session_id = _begin(tool)

        result = tool.execute(
            {"action": "append", "session_id": session_id, "chunk_index": 0, "content": "abcdefghijkl"}
        )
        finish = tool.execute({"action": "finish", "session_id": session_id})

        assert result.ok is True
        assert result.result_envelope["auto_split"] is True
        assert result.result_envelope["chunk_index"] == 0
        assert result.result_envelope["received_chunks"] == [0, 1, 2]
        assert finish.ok is True
        assert (workspace / "out" / "large.txt").read_text(encoding="utf-8") == "abcdefghijkl"

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
