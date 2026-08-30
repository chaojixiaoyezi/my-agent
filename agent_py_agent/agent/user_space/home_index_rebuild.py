
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .home_indexes import (
    AgentIndexRef,
    RunIndexRef,
    TaskIndexRef,
    replace_home_index_snapshots,
)
from .home_layout import MyAgentHomePaths
from .owner_resolver import OwnerHomeResult, OwnerIdentity, resolve_owner_home


@dataclass(frozen=True)
class HomeIndexRebuildResult:
    applied: bool
    owners: tuple[OwnerHomeResult, ...]
    task_refs: tuple[TaskIndexRef, ...]
    run_refs: tuple[RunIndexRef, ...]
    agent_refs: tuple[AgentIndexRef, ...]
    load_errors: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "owner_count": len(self.owners),
            "task_count": len(self.task_refs),
            "run_count": len(self.run_refs),
            "agent_count": len(self.agent_refs),
            "owners": [
                {
                    "owner_id": owner.owner_id,
                    "provider": owner.identity.provider,
                    "owner_kind": owner.identity.owner_kind,
                    "owner_home": str(owner.home_dir),
                }
                for owner in self.owners
            ],
            "tasks": [_task_ref_dict(ref) for ref in self.task_refs],
            "runs": [_run_ref_dict(ref) for ref in self.run_refs],
            "agents": [_agent_ref_dict(ref) for ref in self.agent_refs],
            "load_errors": list(self.load_errors),
        }


# LLM: Dry-run only scans canonical owner homes. Apply atomically replaces the
# disposable global projections instead of appending another copy of every ref.
# 函数用途: 从真实用户目录重建全局索引；应用时会把膨胀旧索引压成当前状态快照。
def rebuild_home_indexes(home: MyAgentHomePaths, *, apply: bool = False) -> HomeIndexRebuildResult:
    owners = tuple(_owner_records(home))
    task_refs: list[TaskIndexRef] = []
    run_refs: list[RunIndexRef] = []
    agent_refs: list[AgentIndexRef] = []
    load_errors: list[dict[str, object]] = []
    for owner in owners:
        task_states = _task_state_reports_for_owner(owner)
        task_refs.extend(_task_refs_for_owner(owner, task_states))
        run_refs.extend(_run_refs_for_owner(owner, task_states))
        agent_report = _agent_refs_for_owner(owner)
        agent_refs.extend(agent_report.refs)
        load_errors.extend(error for _path, _payload, error in task_states if error is not None)
        load_errors.extend(agent_report.load_errors)
    result = HomeIndexRebuildResult(
        applied=bool(apply),
        owners=owners,
        task_refs=tuple(task_refs),
        run_refs=tuple(run_refs),
        agent_refs=tuple(agent_refs),
        load_errors=tuple(load_errors),
    )
    if apply:
        replace_home_index_snapshots(
            home,
            owners=owners,
            task_refs=tuple(task_refs),
            run_refs=tuple(run_refs),
            agent_refs=tuple(agent_refs),
        )
    return result


def _owner_records(home: MyAgentHomePaths) -> list[OwnerHomeResult]:
    records: list[OwnerHomeResult] = []
    records.extend(_local_owner_records(home))
    providers = home.owners_dir / "providers"
    if providers.exists():
        for provider_dir in sorted(path for path in providers.iterdir() if path.is_dir()):
            records.extend(_provider_owner_records(provider_dir))
    return records


def _local_owner_records(home: MyAgentHomePaths) -> list[OwnerHomeResult]:
    if not home.local_owners_dir.exists():
        return []
    records = []
    for owner_dir in sorted(path for path in home.local_owners_dir.iterdir() if path.is_dir()):
        owner_name = owner_dir.name
        identity = OwnerIdentity.local_main() if owner_name == "main" else OwnerIdentity(provider="local", owner_kind=owner_name, owner_id=owner_name)
        records.append(resolve_owner_home(home.root, identity))
    return records


def _provider_owner_records(provider_dir: Path) -> list[OwnerHomeResult]:
    records: list[OwnerHomeResult] = []
    root = provider_dir.parents[2]
    provider = provider_dir.name
    for kind_dir, owner_kind in ((provider_dir / "users", "user"), (provider_dir / "groups", "group")):
        if not kind_dir.exists():
            continue
        for owner_dir in sorted(path for path in kind_dir.iterdir() if path.is_dir()):
            identity = (
                OwnerIdentity.provider_user(provider, owner_dir.name)
                if owner_kind == "user"
                else OwnerIdentity.provider_group(provider, owner_dir.name)
            )
            records.append(resolve_owner_home(root, identity))
    return records


@dataclass(frozen=True)
class _AgentRefsReport:
    refs: list[AgentIndexRef]
    load_errors: list[dict[str, object]]


def _task_state_reports_for_owner(owner: OwnerHomeResult) -> list[tuple[Path, dict[str, Any], dict[str, object] | None]]:
    return [_task_state_report(path) for path in _task_state_paths(owner.home_dir)]


def _task_state_report(path: Path) -> tuple[Path, dict[str, Any], dict[str, object] | None]:
    state_path, state, state_error = _read_json_report(path, context="home_index_rebuild.task_state")
    _workspace_path, workspace, workspace_error = _read_json_report(
        path.parent / "run_workspace.json",
        context="home_index_rebuild.run_workspace",
    )
    payload = _state_with_workspace_identity(state, workspace)
    return state_path, payload, state_error or workspace_error


def _state_with_workspace_identity(state: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state) if isinstance(state, dict) else {}
    if not isinstance(workspace, dict):
        return payload
    for key in ("request_id", "run_id", "task_id", "task_title", "prompt_fingerprint", "owner_id", "owner_home", "task_name", "source"):
        if not payload.get(key) and workspace.get(key):
            payload[key] = workspace[key]
    return payload


def _task_refs_for_owner(
    owner: OwnerHomeResult,
    state_reports: list[tuple[Path, dict[str, Any], dict[str, object] | None]],
) -> list[TaskIndexRef]:
    refs: list[TaskIndexRef] = []
    for state_path, payload, load_error in state_reports:
        task_id = str(payload.get("task_id") or state_path.parents[1].name)
        refs.append(
            TaskIndexRef(
                owner_id=owner.owner_id,
                task_id=task_id,
                task_path=state_path.parents[1],
                status="UNKNOWN" if load_error else str(payload.get("status") or "active"),
                title=str(payload.get("task_name") or task_id),
            )
        )
    return refs


def _run_refs_for_owner(
    owner: OwnerHomeResult,
    state_reports: list[tuple[Path, dict[str, Any], dict[str, object] | None]],
) -> list[RunIndexRef]:
    refs: list[RunIndexRef] = []
    for state_path, payload, load_error in state_reports:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        task_id = str(payload.get("task_id") or state_path.parents[1].name)
        refs.append(
            RunIndexRef(
                owner_id=owner.owner_id,
                run_id=run_id,
                task_id=task_id,
                run_path=state_path.parent,
                status="UNKNOWN" if load_error else str(payload.get("status") or "active"),
            )
        )
    return refs


def _agent_refs_for_owner(owner: OwnerHomeResult) -> _AgentRefsReport:
    refs: list[AgentIndexRef] = []
    load_errors: list[dict[str, object]] = []
    agents_root = owner.home_dir / "agents"
    for state_path in sorted(agents_root.glob("*/state.json")):
        _path, payload, load_error = _read_json_report(state_path, context="home_index_rebuild.agent_state")
        if load_error:
            load_errors.append(load_error)
        agent_id = str(payload.get("id") or payload.get("agent_id") or state_path.parent.name)
        task_id = str(payload.get("task_id") or "")
        refs.append(
            AgentIndexRef(
                owner_id=owner.owner_id,
                agent_id=agent_id,
                task_id=task_id,
                run_path=state_path.parent,
                status="UNKNOWN" if load_error else str(payload.get("status") or "active"),
            )
        )
    return _AgentRefsReport(refs, load_errors)


def _task_state_paths(owner_home: Path) -> list[Path]:
    return sorted((owner_home / "tasks").glob("*/*/work/state.json"))


def _read_json_report(path: Path, *, context: str) -> tuple[Path, dict[str, Any], dict[str, object] | None]:
    report = read_json_object_report(path, context=context)
    return path, report.payload, report.load_error


def _task_ref_dict(ref: TaskIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "task_id": ref.task_id,
        "task_path": str(ref.task_path),
        "status": ref.status,
        "title": ref.title,
    }


def _run_ref_dict(ref: RunIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "run_id": ref.run_id,
        "task_id": ref.task_id,
        "run_path": str(ref.run_path),
        "status": ref.status,
    }


def _agent_ref_dict(ref: AgentIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "agent_id": ref.agent_id,
        "task_id": ref.task_id,
        "run_path": str(ref.run_path),
        "status": ref.status,
    }


__all__ = ["HomeIndexRebuildResult", "rebuild_home_indexes"]
