
from __future__ import annotations

"""职责台账 —— 几个月级多层代理值守的"谁盯什么 + 心跳 + 进度",审计性 + 续接依据。

主代理派子代理/孙代理时登记一条 assignment(谁 / 盯哪些源 / 上级 / owner);每个代理周期写心跳+进度。
任何时刻读台账(roster)即知全局:每源谁在盯、哪个代理心跳停(挂)、哪些源没人认领(漏)。挂了的按台账
知道"原盯啥、研判到哪",重新派代理从断点接管 = 续接。配合候选队列 / 分级汇报流两条全量审计流 = 完整可审计。

落盘:每条 assignment 一个 JSON 文件(原子写),心跳/进度各自更新不互相覆盖,并发多代理安全。
"""

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...common.json_io import read_json_object, write_json_file_atomic

# 心跳超过这么久没更新 = 代理卡住/挂了(看门狗据此重派接管)。
_STALL_SECONDS = 180.0
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class Assignment:
    """一条值守职责:某代理负责盯某些源。"""

    assignment_id: str
    agent_id: str
    targets: list[str]  # 盯哪些源(source_id 或 locator)
    role: str = "child"  # child(子代理) | grandchild(孙代理)
    parent_agent_id: str = ""  # 上级代理(主代理 / 子代理)
    owner: str = "local"  # 多主代理隔离:IM 私聊用户身份
    status: str = "active"  # active | stalled | done
    created_at: float = 0.0
    heartbeat_at: float = 0.0
    progress: dict[str, Any] = field(default_factory=dict)  # poll_cursor/candidates_seen/alerts_found...

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assignment":
        return cls(
            assignment_id=str(data.get("assignment_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            targets=[str(t) for t in (data.get("targets") or [])],
            role=str(data.get("role") or "child"),
            parent_agent_id=str(data.get("parent_agent_id") or ""),
            owner=str(data.get("owner") or "local"),
            status=str(data.get("status") or "active"),
            created_at=float(data.get("created_at") or 0.0),
            heartbeat_at=float(data.get("heartbeat_at") or 0.0),
            progress=dict(data.get("progress") or {}),
        )


class DutyRegistry:
    """某 monitor 的职责台账门面(落在 <monitor_root>/assignments/)。"""

    def __init__(self, root: Path):
        self.dir = Path(root) / "assignments"

    def _path(self, assignment_id: str) -> Path:
        safe = _ID_SAFE_RE.sub("-", str(assignment_id)) or "unknown"
        return self.dir / f"{safe}.json"

    def register(self, assignment: Assignment, *, now: float | None = None) -> None:
        """登记/更新一条职责(主代理派活时调)。created_at 首登记时打,心跳同步。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.time() if now is None else now
        if not assignment.created_at:
            assignment.created_at = stamp
        if not assignment.heartbeat_at:
            assignment.heartbeat_at = stamp
        write_json_file_atomic(self._path(assignment.assignment_id), assignment.to_dict())

    def heartbeat(self, assignment_id: str, progress: dict[str, Any] | None = None, *, now: float | None = None) -> bool:
        """代理周期写心跳 + 进度。返回是否成功(台账里没这条则 False)。"""
        existing = read_json_object(self._path(assignment_id))
        if not existing:
            return False
        record = Assignment.from_dict(existing)
        record.heartbeat_at = time.time() if now is None else now
        if record.status == "stalled":
            record.status = "active"  # 心跳回来 → 复活
        if progress:
            record.progress.update(progress)
        write_json_file_atomic(self._path(assignment_id), record.to_dict())
        return True

    def mark_stalled(self, assignment_id: str) -> bool:
        """把某职责标记为 stalled(看门狗发现心跳超时时调;不动心跳,等代理回来 heartbeat 自动复活)。"""
        existing = read_json_object(self._path(assignment_id))
        if not existing:
            return False
        record = Assignment.from_dict(existing)
        record.status = "stalled"
        write_json_file_atomic(self._path(assignment_id), record.to_dict())
        return True

    def get(self, assignment_id: str) -> Assignment | None:
        data = read_json_object(self._path(assignment_id))
        return Assignment.from_dict(data) if data else None

    def all(self) -> list[Assignment]:
        if not self.dir.is_dir():
            return []
        out: list[Assignment] = []
        for path in sorted(self.dir.glob("*.json")):
            data = read_json_object(path)
            if data:
                out.append(Assignment.from_dict(data))
        return out

    def stalled(self, *, now: float | None = None, stall_seconds: float = _STALL_SECONDS) -> list[Assignment]:
        """心跳超时(挂了/卡住)的活跃职责 —— 看门狗据此重派接管。"""
        current = time.time() if now is None else now
        return [
            a
            for a in self.all()
            if a.status == "active" and a.heartbeat_at and (current - a.heartbeat_at) >= stall_seconds
        ]

    def coverage_gaps(self, all_source_ids: list[str]) -> list[str]:
        """没被任何 active 职责覆盖的源(漏检)—— 主代理据此补派。"""
        covered: set[str] = set()
        for a in self.all():
            if a.status != "done":
                covered.update(a.targets)
        return [sid for sid in all_source_ids if sid not in covered]

    def roster(self, all_source_ids: list[str], *, now: float | None = None) -> dict[str, Any]:
        """全局审计视图:总数/活跃/卡住/漏检源,每条职责的代理+心跳年龄+进度。一眼看清谁盯啥、哪挂了、哪漏了。"""
        current = time.time() if now is None else now
        items = self.all()
        stalled_ids = {a.assignment_id for a in self.stalled(now=current)}
        return {
            "total": len(items),
            "active": sum(1 for a in items if a.status == "active" and a.assignment_id not in stalled_ids),
            "stalled": sorted(stalled_ids),
            "coverage_gaps": self.coverage_gaps(all_source_ids),
            "assignments": [
                {
                    "assignment_id": a.assignment_id,
                    "agent_id": a.agent_id,
                    "role": a.role,
                    "targets": a.targets,
                    "owner": a.owner,
                    "status": "stalled" if a.assignment_id in stalled_ids else a.status,
                    "heartbeat_age_seconds": round(current - a.heartbeat_at, 1) if a.heartbeat_at else None,
                    "progress": a.progress,
                }
                for a in items
            ],
        }


__all__ = ["Assignment", "DutyRegistry"]
