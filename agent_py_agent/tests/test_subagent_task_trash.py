from __future__ import annotations

"""LLM: verifies task-local trash moves replace direct deletion.

给人看的解释：
这些测试确保子代理只能把授权目录内的文件移到任务 trash，并留下 manifest。
"""

import json
from pathlib import Path

from agent_py_agent.agent.subagents.task_trash import (
    TaskTrashMoveRequest,
    ensure_task_trash,
    move_to_task_trash,
)


def test_task_trash_moves_file_and_writes_manifest(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "draft.txt"
    source.write_text("old", encoding="utf-8")

    result = move_to_task_trash(
        TaskTrashMoveRequest(
            task_dir=task_dir,
            source_path="draft.txt",
            reason="replace outdated draft",
            actor_run_id="run-1",
        )
    )

    assert result.moved is True
    assert not source.exists()
    assert Path(result.destination).exists()
    manifest_lines = Path(result.manifest_ref).read_text(encoding="utf-8").splitlines()
    record = json.loads(manifest_lines[-1])
    assert record["source"] == str(source.resolve())
    assert record["reason"] == "replace outdated draft"
    assert record["actor_run_id"] == "run-1"


def test_task_trash_blocks_source_outside_task(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    result = move_to_task_trash(TaskTrashMoveRequest(task_dir=task_dir, source_path=outside))

    assert result.moved is False
    assert result.blockers == ["source_outside_allowed_roots"]
    assert outside.exists()


def test_task_trash_recreates_missing_trash_dir(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    trash = ensure_task_trash(task_dir)
    (trash / "manifest.jsonl").unlink()
    trash.rmdir()

    recreated = ensure_task_trash(task_dir)

    assert recreated.exists()
    assert (recreated / "manifest.jsonl").exists()


def test_task_trash_blocks_trash_self_move(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    trash = ensure_task_trash(task_dir)
    existing = trash / "old.txt"
    existing.write_text("already trash", encoding="utf-8")

    result = move_to_task_trash(TaskTrashMoveRequest(task_dir=task_dir, source_path=existing))

    assert result.moved is False
    assert result.blockers == ["source_already_in_trash"]
