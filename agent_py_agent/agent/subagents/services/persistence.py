from __future__ import annotations

"""LLM: persistence service for SubAgentManager task records.

给人看的解释：
子代理工单的读取、扫描和保存集中在这里。SubAgentManager 继续提供原方法名，
但具体持久化逻辑不再塞在 base mixin 里。
"""

import json
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from ..models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    ContextManifest,
    QualityContract,
    SubAgentTask,
    TakeoverRecord,
    VerificationEvidence,
)
from ..utils import _apply_missing_paths


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


def _normalize_quality_contract(value: object) -> QualityContract:
    if isinstance(value, QualityContract):
        return value
    if not isinstance(value, dict):
        return QualityContract()
    payload = {key: value[key] for key in _field_names(QualityContract) if key in value}
    for key in [
        "failure_conditions",
        "forbidden_delivery",
        "must_check",
        "sampling_plan",
        "evidence_required",
        "allowed_degradation",
    ]:
        payload[key] = _string_list_value(payload.get(key))
    payload["cannot_self_accept"] = bool(payload.get("cannot_self_accept", True))
    payload["parent_final_gate"] = bool(payload.get("parent_final_gate", True))
    return QualityContract(**payload)


def _normalize_context_manifest(value: object) -> ContextManifest:
    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class SubAgentPersistenceService:
    """Read and write SubAgentTask records for the manager facade."""

    def __init__(self, manager: Any):
        self.manager = manager

    @property
    def workspace(self) -> Path:
        return self.manager.workspace

    def load(self, run_id: str) -> SubAgentTask:
        """Load one subagent task from disk."""

        path = self.workspace / run_id / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        data = {key: value for key, value in data.items() if key in _field_names(SubAgentTask)}
        data["capability_requests"] = [
            CapabilityRequest(**item) for item in data.get("capability_requests", []) if isinstance(item, dict)
        ]
        data["capability_grants"] = [
            CapabilityGrant(**item) for item in data.get("capability_grants", []) if isinstance(item, dict)
        ]
        data["capability_gaps"] = [
            CapabilityGap(**item) for item in data.get("capability_gaps", []) if isinstance(item, dict)
        ]
        data["evidence"] = [VerificationEvidence(**item) for item in data.get("evidence", []) if isinstance(item, dict)]
        data["takeover_records"] = [
            TakeoverRecord(**item) for item in data.get("takeover_records", []) if isinstance(item, dict)
        ]
        data["channel_checks"] = [
            ChannelProbeCheck(**item) for item in data.get("channel_checks", []) if isinstance(item, dict)
        ]
        data["quality_contract"] = _normalize_quality_contract(data.get("quality_contract"))
        data["context_manifest"] = _normalize_context_manifest(data.get("context_manifest"))
        data["context_packs"] = _normalize_context_packs(data.get("context_packs"))
        return SubAgentTask(**data)

    def list_runs(self) -> list[SubAgentTask]:
        """Scan the workspace for subagent task records."""

        runs: list[SubAgentTask] = []
        for task_file in sorted(self.workspace.glob("*/task.json")):
            try:
                runs.append(self.load(task_file.parent.name))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return runs

    def save(self, task: SubAgentTask) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        task_dir = Path(task.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        self.manager._ensure_work_order_files(task)
        task.updated_at = task.updated_at or time.time()
        payload = json.dumps(asdict(task), ensure_ascii=False, indent=2)
        (task_dir / "task.json").write_text(payload, encoding="utf-8")
        (task_dir / "run.json").write_text(payload, encoding="utf-8")
        (task_dir / "thought.md").write_text(self._render_thought_markdown(task), encoding="utf-8")
        self.manager._index_task(task)
        if self.manager.local_store:
            self.manager.local_store.task_registry.register_task(
                task_id=task.id,
                session_id=task.root_id,
                user_id=task.owner or "",
                status=task.status,
                goal=task.goal,
            )

    @staticmethod
    def _render_thought_markdown(task: SubAgentTask) -> str:
        return (
            "# Thought\n\n"
            f"{task.thought}\n\n"
            "## Plan\n"
            + "\n".join(f"- {item}" for item in task.plan)
            + "\n\n"
            "## Capability Boundary\n"
            f"- Agent: {task.agent_name}\n"
            f"- Role: {task.role}\n"
            f"- Owner: {task.owner or 'none'}\n"
            f"- Supervisor: {task.supervisor or 'none'}\n"
            f"- Final owner: {task.final_owner or 'none'}\n"
            f"- Parent: {task.parent_id or 'none'}\n"
            f"- Depth: {task.depth}\n"
            f"- Allowed skills: {', '.join(task.allowed_skills) or 'none'}\n"
            f"- Allowed tools: {', '.join(task.allowed_tools) or 'none'}\n\n"
            "## Write Boundary\n"
            f"- Task dir: {task.task_dir}\n"
            f"- Allowed write roots: {', '.join(task.allowed_write_roots) or 'none'}\n"
            f"- Forbidden write roots: {', '.join(task.forbidden_write_roots) or 'none'}\n\n"
            "## Acceptance Checks\n"
            + "\n".join(f"- {item}" for item in task.acceptance_checks or ["未设置"])
            + "\n\n"
            "## Evidence\n"
            + "\n".join(f"- [{item.kind}] {item.summary}" for item in task.evidence or [])
            + ("\n" if task.evidence else "- 暂无\n")
        )
