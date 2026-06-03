"""通用文件写入工具测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from agent_py_agent.agent.tooling.content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    write_file_content_parameter_detail,
)
from agent_py_agent.agent.tooling.filesystem_write import (
    ApplyPatchTool,
    WriteFileTool,
    WriteFileToolOptions,
)


def test_write_file_writes_text_and_creates_parent_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "deep/nested/file.txt", "content": "hello"})

    assert result.ok
    assert (workspace / "deep/nested/file.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_overwrites_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "existing.txt"
    target.write_text("old", encoding="utf-8")
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "existing.txt", "content": "new"})

    assert result.ok
    assert target.read_text(encoding="utf-8") == "new"


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


def test_write_file_allows_non_dangerous_external_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    result = tool.execute({"path": "../outside.txt", "content": "bad"})

    assert result.ok
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "bad"


def test_write_file_blocks_configured_dangerous_root(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.filesystem import filesystem_access_options

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


def test_write_file_content_detail_uses_transport_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = WriteFileTool(workspace)

    assert tool.spec.parameter_details["content"] == write_file_content_parameter_detail()
    assert str(MAX_INLINE_WRITE_CONTENT_CHARS) in tool.spec.parameter_details["content"]


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
    assert "WRITE_FILE_RAW" in result.output
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
    assert not (workspace / "notes.txt").exists()
    assert not (workspace / "moved.txt").exists()


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
