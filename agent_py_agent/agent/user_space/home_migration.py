# LLM: Home migration copies legacy home data into owner home without deleting old files.
# 模块用途: 生成并执行 V1 -> V2 owner home 迁移计划；只复制，不覆盖，不清理。

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths


@dataclass(frozen=True)
class HomeMigrationAction:
    action: str
    source: Path
    target: Path
    status: str = "planned"

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "source": str(self.source),
            "target": str(self.target),
            "status": self.status,
        }


@dataclass(frozen=True)
class HomeMigrationResult:
    applied: bool
    actions: tuple[HomeMigrationAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "actions": [action.to_dict() for action in self.actions]}


def plan_home_migration(home: MyAgentHomePaths) -> HomeMigrationResult:
    return HomeMigrationResult(applied=False, actions=tuple(_legacy_copy_actions(home)))


def apply_home_migration(home: MyAgentHomePaths) -> HomeMigrationResult:
    applied: list[HomeMigrationAction] = []
    for action in _legacy_copy_actions(home):
        status = _apply_copy_action(action)
        applied.append(HomeMigrationAction(action=action.action, source=action.source, target=action.target, status=status))
    return HomeMigrationResult(applied=True, actions=tuple(applied))


def _legacy_copy_actions(home: MyAgentHomePaths) -> list[HomeMigrationAction]:
    return [
        *_file_copy_actions("copy_daily_memory", home.memory_daily_dir, home.owner_memory_daily_dir, "*.jsonl"),
        *_file_copy_actions("copy_raw_memory", home.memory_raw_dir, home.owner_memory_raw_dir, "*.jsonl"),
        *_task_workspace_copy_actions(home.workspace_tasks_dir, home.owner_tasks_dir),
    ]


def _file_copy_actions(action: str, source_dir: Path, target_dir: Path, pattern: str) -> list[HomeMigrationAction]:
    if not source_dir.exists():
        return []
    return [
        HomeMigrationAction(action=action, source=source, target=target_dir / source.name)
        for source in sorted(source_dir.glob(pattern))
        if source.is_file()
    ]


def _task_workspace_copy_actions(source_root: Path, target_root: Path) -> list[HomeMigrationAction]:
    if not source_root.exists():
        return []
    actions: list[HomeMigrationAction] = []
    for state in sorted(source_root.glob("*/*/state.json")):
        task_dir = state.parent
        relative = task_dir.relative_to(source_root)
        actions.append(HomeMigrationAction(action="copy_task_workspace", source=task_dir, target=target_root / relative))
    return actions


def _apply_copy_action(action: HomeMigrationAction) -> str:
    if action.target.exists():
        return "skipped_existing"
    action.target.parent.mkdir(parents=True, exist_ok=True)
    if action.source.is_dir():
        shutil.copytree(action.source, action.target)
        return "copied"
    shutil.copy2(action.source, action.target)
    return "copied"


__all__ = ["HomeMigrationAction", "HomeMigrationResult", "apply_home_migration", "plan_home_migration"]
