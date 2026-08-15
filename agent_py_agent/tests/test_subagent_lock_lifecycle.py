"""锁生命周期钉子(任务完成力底座 P3-1,实锤来源 R5a:13 子代理 23 次
WRITE_FORBIDDEN,锁住的正是各自的声明交付目标)。

钉死三层契约(全部经真实 SubAgentManager save/load 落盘验证):
1. 自锁矛盾剔除:任务自己的声明交付目标(精确文件/目录覆盖)不得留在它自己的
   locked_files——save 权威口统一剔除并结构化留痕(locked_files_sanitized)。
2. 合法锁保留:与自身交付目标无关的锁(如兄弟任务的产物、外部敏感文件)原样保留。
3. 锁变更账本:locked_files 的每次增删都进 locked_files_changes(环形上限),
   "运行中途谁动了锁"从此有据可查(R5a 取证盲区的针对修复)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.output_alignment import (
    LOCKED_FILES_CHANGES_ATTR,
    LOCKED_FILES_SANITIZED_ATTR,
    delivery_root,
)

pytestmark = pytest.mark.integration


def _manager(tmp_path: Path) -> SubAgentManager:
    return SubAgentManager(workspace=tmp_path / "ws")


def _task_with_outputs(manager: SubAgentManager, outputs: list[str]):
    return manager.create_run(
        params=CreateRunParams(
            goal="锁生命周期钉子",
            thought="t",
            plan=["p1"],
            role="worker",
            attributes={"output_files": outputs},
        )
    )


def test_self_locked_delivery_file_is_sanitized_on_save(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    task = _task_with_outputs(manager, ["backend/app.py"])
    target = str(Path(delivery_root(task)) / "backend" / "app.py")

    task.locked_files = [target]  # R5a 形态:锁住自己要交付的文件
    manager.save(task)

    reloaded = manager.load(task.id)
    assert reloaded.locked_files == [], "自锁交付目标必须在落盘前被剔除"
    ledger = reloaded.attributes[LOCKED_FILES_SANITIZED_ATTR]
    assert ledger[-1]["removed"] == [target]
    assert ledger[-1]["code"] == "OUTPUT_TARGET_LOCKED"


def test_directory_lock_covering_delivery_root_is_sanitized(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    task = _task_with_outputs(manager, ["report.md"])

    task.locked_files = [delivery_root(task)]  # 锁整个交付目录(R5a "output 目录被锁"形态)
    manager.save(task)

    reloaded = manager.load(task.id)
    assert reloaded.locked_files == []
    assert reloaded.attributes[LOCKED_FILES_SANITIZED_ATTR][-1]["removed_count"] == 1


def test_unrelated_locks_are_preserved(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    task = _task_with_outputs(manager, ["mine/result.md"])
    sibling_output = str(tmp_path / "other-task" / "output" / "theirs.md")
    sensitive = str(tmp_path / "secrets" / "key.pem")

    task.locked_files = [sibling_output, sensitive]
    manager.save(task)

    reloaded = manager.load(task.id)
    assert sorted(reloaded.locked_files) == sorted([sibling_output, sensitive]), (
        "与自身交付目标无关的锁(兄弟产物/敏感文件)必须原样保留"
    )
    assert LOCKED_FILES_SANITIZED_ATTR not in reloaded.attributes


def test_lock_changes_are_journaled_across_saves(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    task = _task_with_outputs(manager, ["out.md"])
    external = str(tmp_path / "frozen" / "data.csv")

    task = manager.load(task.id)
    task.locked_files = [external]
    manager.save(task)  # 加锁一次

    task = manager.load(task.id)
    task.locked_files = []
    manager.save(task)  # 解锁一次

    reloaded = manager.load(task.id)
    changes = reloaded.attributes[LOCKED_FILES_CHANGES_ATTR]
    assert any(external in entry["added"] for entry in changes), "加锁必须入账"
    assert any(external in entry["removed"] for entry in changes), "解锁必须入账"


def test_takeover_passed_self_lock_is_also_sanitized(tmp_path: Path) -> None:
    """takeover 透传的锁(R5a 疑似来源链)同样过 save 权威口被剔。"""
    manager = _manager(tmp_path)
    task = _task_with_outputs(manager, ["final/report.md"])
    target = str(Path(delivery_root(task)) / "final" / "report.md")

    manager.record_takeover(task.id, take_over_by="parent", reason="重派", locked_files=[target])

    reloaded = manager.load(task.id)
    assert target not in reloaded.locked_files, "takeover 透传的自锁同样必须被剔"
    assert reloaded.attributes[LOCKED_FILES_SANITIZED_ATTR][-1]["removed"] == [target]
