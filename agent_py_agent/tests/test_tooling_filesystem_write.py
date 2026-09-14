"""通用文件写入工具测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import filesystem_access_options
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    write_file_content_parameter_detail,
)


def test_write_file_writes_text_and_creates_parent_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "deep/nested/file.txt", "content": "hello"})

    assert result.ok
    assert (workspace / "deep/nested/file.txt").read_text(encoding="utf-8") == "hello"
    assert result.result_envelope["display"] == {
        "kind": "write",
        "path": "deep/nested/file.txt",
        "mode": "overwrite",
        "bytes": 5,
        "binary": False,
        "total_lines": 1,
        "lines": ["hello"],
        "hidden_lines": 0,
    }


def test_write_file_overwrites_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "existing.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "existing.txt", "content": "new"})

    assert result.ok
    assert target.read_text(encoding="utf-8") == "new"
    display = result.result_envelope["display"]
    assert display["kind"] == "diff"
    assert display["path"] == "existing.txt"
    assert display["lines_added"] == 1
    assert display["lines_removed"] == 1


def test_write_file_appends_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.md"
    target.write_text("# Report\n", encoding="utf-8")
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "report.md", "mode": "append", "content": "\n## Section\nbody\n"})

    assert result.ok
    assert "已追加文件" in result.output
    assert target.read_text(encoding="utf-8") == "# Report\n\n## Section\nbody\n"


def test_write_file_rejects_unknown_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "report.md", "mode": "continue", "content": "body"})

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "overwrite 或 append" in result.output


def test_write_file_rejects_write_mode_with_repair_hint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "report.md", "mode": "write", "content": "body"})

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "新建或覆盖文件时省略 mode" in result.output
    assert "mode=\"overwrite\"" in result.output


def test_write_file_writes_binary_base64(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)
    payload = b"\x00\x01binary\xff"

    result = tool.execute(
        {
            "path": "artifacts/blob.bin",
            "data_base64": base64.b64encode(payload).decode("ascii"),
        }
    )

    assert result.ok
    assert (workspace / "artifacts/blob.bin").read_bytes() == payload


def test_write_file_requires_exactly_one_payload(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    missing = tool.execute({"path": "out.txt"})
    duplicate = tool.execute({"path": "out.txt", "content": "x", "data_base64": "eA=="})

    assert not missing.ok
    assert not duplicate.ok
    assert "二选一" in missing.output
    assert "二选一" in duplicate.output


def test_write_file_ignores_empty_optional_data_base64_when_content_is_present(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "out.txt", "content": "hello", "data_base64": ""})

    assert result.ok
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_allows_non_dangerous_external_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "../outside.txt", "content": "bad"})

    assert result.ok
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "bad"


def test_write_file_blocks_configured_dangerous_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    danger = tmp_path / "danger"
    workspace.mkdir()
    danger.mkdir()
    tool = WriteFileTool(
        workspace,
        options=WriteFileToolOptions(access_options=filesystem_access_options(path_dangerous_roots=[str(danger)])),
    )

    result = tool.execute({"path": str(danger / "secret.txt"), "content": "bad"})

    assert not result.ok
    assert "危险目录" in result.output


def test_write_file_blocks_direct_task_progress_ledger_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute(
        {
            "path": ".my_agent/home/owners/local/main/memory_archive/task_progress/run-1/progress.json",
            "content": "{}",
        }
    )

    assert not result.ok
    assert result.error_code == "SYSTEM_LEDGER_WRITE_BLOCKED"
    assert "请使用 task_progress 工具" in result.output
    assert not (
        workspace
        / ".my_agent/home/owners/local/main/memory_archive/task_progress/run-1/progress.json"
    ).exists()


def test_write_file_allows_user_report_named_progress_json_outside_system_ledger(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "lab_outputs/progress.json", "content": "{}"})

    assert result.ok
    assert (workspace / "lab_outputs/progress.json").read_text(encoding="utf-8") == "{}"


def test_write_file_content_detail_uses_transport_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    detail = tool.model_spec.input_schema["properties"]["content"]["description"]
    assert detail == write_file_content_parameter_detail()
    assert str(MAX_INLINE_WRITE_CONTENT_CHARS) in detail


def test_write_file_accepts_long_inline_content_with_transport_hint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "site/style.css"
    tool = WriteFileTool(workspace)

    result = tool.execute(
        {
            "path": "site/style.css",
            "content": "A" * (MAX_INLINE_WRITE_CONTENT_CHARS + 1),
        }
    )

    assert result.ok
    assert "inline content 超过推荐值" in result.output
    assert 'mode="append"' in result.output
    assert "WRITE_FILE_RAW" not in result.output
    assert target.read_text(encoding="utf-8") == "A" * (MAX_INLINE_WRITE_CONTENT_CHARS + 1)


def test_write_file_soft_warns_when_writing_final_text_into_reference_root(tmp_path: Path) -> None:
    """有明确目标产物时，写进参考目录应当场软提醒，但不能阻断写入。"""
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    fact_dir = workspace / "memory_archive/runtime_facts/req-1"
    fact_dir.mkdir(parents=True)
    (fact_dir / "task.json").write_text(
        json.dumps(
            {
                "run_intent": {
                    "reference_roots": {"items": [str(reference)]},
                    "desired_outputs": {"items": ["outputs/final-report.md"]},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = WriteFileTool(workspace, workspace_roots=[workspace, reference])

    result = tool.execute({"path": str(reference / "notes/report.md"), "content": "hello"})

    assert result.ok
    assert (reference / "notes/report.md").read_text(encoding="utf-8") == "hello"
    assert "软提醒" in result.output
    assert "outputs/final-report.md" in result.output


def test_write_file_soft_warns_from_owner_home_runtime_facts(tmp_path: Path) -> None:
    """owner-home runtime facts 也要能驱动参考目录软提示，避免只看旧 workspace 根。"""
    workspace = tmp_path / "workspace"
    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    fact_dir = owner_home / "memory_archive/runtime_facts/req-1"
    fact_dir.mkdir(parents=True)
    (fact_dir / "task.json").write_text(
        json.dumps(
            {
                "run_intent": {
                    "reference_roots": {"items": [str(reference)]},
                    "desired_outputs": {"items": ["outputs/final-report.md"]},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = WriteFileTool(
        workspace,
        workspace_roots=[workspace, reference],
        options=WriteFileToolOptions(runtime_fact_roots=[owner_home]),
    )

    result = tool.execute({"path": str(reference / "notes/report.md"), "content": "hello"})

    assert result.ok
    assert "软提醒" in result.output
    assert "outputs/final-report.md" in result.output


def test_write_file_does_not_warn_without_desired_output(tmp_path: Path) -> None:
    """没有明确目标产物时，系统不能凭参考路径猜测并提示模型写错位置。"""
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    fact_dir = workspace / "memory_archive/runtime_facts/req-1"
    fact_dir.mkdir(parents=True)
    (fact_dir / "task.json").write_text(
        json.dumps(
            {
                "run_intent": {
                    "reference_roots": {"items": [str(reference)]},
                    "desired_outputs": {"items": []},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = WriteFileTool(workspace, workspace_roots=[workspace, reference])

    result = tool.execute({"path": str(reference / "notes/report.md"), "content": "hello"})

    assert result.ok
    assert "软提醒" not in result.output


def test_write_file_reports_corrupt_run_intent_facts(tmp_path: Path) -> None:
    """运行意图账本坏了不能静默退化成“没有目标路径/参考目录”。"""
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    fact_dir = workspace / "memory_archive/runtime_facts/req-1"
    fact_dir.mkdir(parents=True)
    (fact_dir / "task.json").write_text("{not-json", encoding="utf-8")
    tool = WriteFileTool(workspace, workspace_roots=[workspace, reference])

    result = tool.execute({"path": str(reference / "notes/report.md"), "content": "hello"})

    assert result.ok
    assert "运行意图账本读取失败" in result.output
    assert "run_intent.runtime_fact.read" in result.output


def test_apply_patch_add_update_delete_and_move(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ApplyPatchTool(workspace)

    add = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Add File: notes.txt\n"
                "+hello\n"
                "+world\n"
                "*** End Patch\n"
            )
        }
    )
    update = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: notes.txt\n"
                "*** Move to: moved.txt\n"
                "-hello\n"
                "+hi\n"
                " world\n"
                "*** End Patch\n"
            )
        }
    )
    delete = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Delete File: moved.txt\n"
                "*** End Patch\n"
            )
        }
    )

    assert add.ok
    assert update.ok
    assert delete.ok
    assert add.result_envelope["display"]["kind"] == "diff"
    assert add.result_envelope["display"]["lines_added"] == 2
    assert update.result_envelope["display"]["path"] == "moved.txt"
    assert update.result_envelope["display"]["lines_added"] == 1
    assert update.result_envelope["display"]["lines_removed"] == 1
    assert delete.result_envelope["display"]["lines_removed"] == 2
    assert not (workspace / "notes.txt").exists()
    assert not (workspace / "moved.txt").exists()


def test_apply_patch_returns_bounded_multi_file_diff_display(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("before\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace)

    result = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: old.txt\n"
                "-before\n"
                "+after\n"
                "*** Add File: new.txt\n"
                "+created\n"
                "*** End Patch\n"
            )
        }
    )

    assert result.ok
    display = result.result_envelope["display"]
    assert display["kind"] == "patch"
    assert display["hidden_files"] == 0
    assert [item["path"] for item in display["files"]] == ["old.txt", "new.txt"]
    assert display["files"][0]["lines_removed"] == 1
    assert display["files"][1]["lines_added"] == 1


def test_apply_patch_rejects_unmatched_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace)

    result = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: notes.txt\n"
                "-missing\n"
                "+new\n"
                "*** End Patch\n"
            )
        }
    )

    assert not result.ok
    assert "上下文未命中" in result.output
    assert "未找到唯一的以下补丁原始行" in result.output
    assert "missing" in result.output
    assert "edit_file" in result.output


def test_apply_patch_rejects_empty_update_like_sample_a(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace)

    result = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: notes.txt\n"
                "*** End Patch\n"
            )
        }
    )

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "不能为空" in result.output
    assert "末尾追加示例" in result.output
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_patch_header_error_shows_exact_spacing_and_complete_example(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = ApplyPatchTool(workspace)

    result = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File:notes.txt\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            )
        }
    )

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "冒号后保留一个空格" in result.output
    assert "*** Update File: notes.txt" in result.output
    assert "*** Begin Patch" in result.output


def test_apply_patch_unprefixed_update_line_explains_all_prefixes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("old\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace)

    result = tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: notes.txt\n"
                "old\n"
                "*** End Patch\n"
            )
        }
    )

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "一个真实空格" in result.output
    assert "-old\\n+new" in result.output
    assert "不要直接写没有前缀" in result.output


# LLM: GUIDE-01(2026-08-15 轻量运行时 对照真机): owner 模式写外部路径被拦时, 错误消息必须带
# 具体可用写入位置(workspace_roots + owner home), 模型无需探索即知可写位置, 防止
# "声称写了但实际无产物"的假完成。同模型在无限制的 轻量运行时 上直接成功, 差异在系统引导。
# 函数用途: 验证 owner scope 拦截消息包含可用写入位置。
def test_write_file_owner_block_message_lists_usable_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    owner_home = tmp_path / "owner-home"
    workspace.mkdir()
    owner_home.mkdir()
    tool = WriteFileTool(
        workspace,
        options=WriteFileToolOptions(
            access_options=filesystem_access_options(
                owner_scope_root=str(owner_home),
                path_dangerous_roots=[],
            )
        ),
    )

    result = tool.execute({"path": str(tmp_path / "outside.txt"), "content": "bad"})

    assert not result.ok
    # 消息必须包含可用的 owner home 路径（模型据此知道往哪写）
    assert str(owner_home) in result.output
    assert "可用的写入位置" in result.output
