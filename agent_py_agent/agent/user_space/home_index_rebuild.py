# LLM: Home index rebuild repairs lightweight maps from owner-home bodies without changing task truth.
# 模块用途: 从 owner home 的 task/run/agent 正文重建全局索引；默认只预览，显式 apply 才写索引。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home_indexes import (
    AgentIndexRef,
    RunIndexRef,
    TaskIndexRef,
    register_agent_ref,
    register_owner_ref,
    register_run_ref,
    register_task_ref,
)
from .home_layout import MyAgentHomePaths
from .owner_resolver import OwnerHomeResult, OwnerIdentity, resolve_owner_home


# LLM: HomeIndexRebuildResult is a refs-only maintenance result for CLI and tests.
# 类用途: 保存重建索引的预览/执行结果，不携带任务正文或产物内容。
@dataclass(frozen=True)
class HomeIndexRebuildResult:
    applied: bool
    owners: tuple[OwnerHomeResult, ...]
    task_refs: tuple[TaskIndexRef, ...]
    run_refs: tuple[RunIndexRef, ...]
    agent_refs: tuple[AgentIndexRef, ...]

    # LLM: to_dict serializes rebuild results without leaking task bodies.
    # 函数用途: 转成 CLI/doctor 可输出的 refs-only JSON 字典。
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
        }


# LLM: rebuild_home_indexes scans owner homes and optionally appends fresh index refs.
# 函数用途: 根据当前磁盘正文重建 owner/task/run/agent 轻量索引；不会删除旧索引行。
def rebuild_home_indexes(home: MyAgentHomePaths, *, apply: bool = False) -> HomeIndexRebuildResult:
    owners = tuple(_owner_records(home))
    task_refs: list[TaskIndexRef] = []
    run_refs: list[RunIndexRef] = []
    agent_refs: list[AgentIndexRef] = []
    for owner in owners:
        task_refs.extend(_task_refs_for_owner(owner))
        run_refs.extend(_run_refs_for_owner(owner))
        agent_refs.extend(_agent_refs_for_owner(owner))
    result = HomeIndexRebuildResult(
        applied=bool(apply),
        owners=owners,
        task_refs=tuple(task_refs),
        run_refs=tuple(run_refs),
        agent_refs=tuple(agent_refs),
    )
    if apply:
        for owner in owners:
            register_owner_ref(home, owner)
        for ref in task_refs:
            register_task_ref(home, ref)
        for ref in run_refs:
            register_run_ref(home, ref)
        for ref in agent_refs:
            register_agent_ref(home, ref)
    return result


# LLM: _owner_records discovers concrete owner homes from the V2 directory tree.
# 函数用途: 枚举 local owner、provider user 和 provider group，生成统一 owner 记录。
def _owner_records(home: MyAgentHomePaths) -> list[OwnerHomeResult]:
    records: list[OwnerHomeResult] = []
    records.extend(_local_owner_records(home))
    providers = home.owners_dir / "providers"
    if providers.exists():
        for provider_dir in sorted(path for path in providers.iterdir() if path.is_dir()):
            records.extend(_provider_owner_records(provider_dir))
    return records


# LLM: _local_owner_records expands local owner folders into owner records.
# 函数用途: 从 owners/local/* 推导本机 owner 身份和 home 路径。
def _local_owner_records(home: MyAgentHomePaths) -> list[OwnerHomeResult]:
    if not home.local_owners_dir.exists():
        return []
    records = []
    for owner_dir in sorted(path for path in home.local_owners_dir.iterdir() if path.is_dir()):
        owner_name = owner_dir.name
        identity = OwnerIdentity.local_main() if owner_name == "main" else OwnerIdentity(provider="local", owner_kind=owner_name, owner_id=owner_name)
        records.append(resolve_owner_home(home.root, identity))
    return records


# LLM: _provider_owner_records expands provider user/group folders into owner records.
# 函数用途: 从 owners/providers/<provider>/users|groups 下推导 owner 身份和 home 路径。
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


# LLM: _task_refs_for_owner reads task workspace states as the authority for task refs.
# 函数用途: 从 owner/tasks 下的 work/state.json 生成 task 索引引用。
def _task_refs_for_owner(owner: OwnerHomeResult) -> list[TaskIndexRef]:
    refs: list[TaskIndexRef] = []
    for state_path in _task_state_paths(owner.home_dir):
        payload = _read_json(state_path)
        task_id = str(payload.get("task_id") or state_path.parents[1].name)
        refs.append(
            TaskIndexRef(
                owner_id=owner.owner_id,
                task_id=task_id,
                task_path=state_path.parents[1],
                status=str(payload.get("status") or "active"),
                title=str(payload.get("task_name") or task_id),
            )
        )
    return refs


# LLM: _run_refs_for_owner reads task workspace states as the authority for run refs.
# 函数用途: 从 owner/tasks 下的 work/state.json 生成 run 索引引用。
def _run_refs_for_owner(owner: OwnerHomeResult) -> list[RunIndexRef]:
    refs: list[RunIndexRef] = []
    for state_path in _task_state_paths(owner.home_dir):
        payload = _read_json(state_path)
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
                status=str(payload.get("status") or "active"),
            )
        )
    return refs


# LLM: _agent_refs_for_owner reads owner agent projections as agent index truth.
# 函数用途: 从 owner_home/agents/*/state.json 生成 agent 索引引用。
def _agent_refs_for_owner(owner: OwnerHomeResult) -> list[AgentIndexRef]:
    refs: list[AgentIndexRef] = []
    agents_root = owner.home_dir / "agents"
    for state_path in sorted(agents_root.glob("*/state.json")):
        payload = _read_json(state_path)
        agent_id = str(payload.get("id") or payload.get("agent_id") or state_path.parent.name)
        task_id = str(payload.get("task_id") or "")
        refs.append(
            AgentIndexRef(
                owner_id=owner.owner_id,
                agent_id=agent_id,
                task_id=task_id,
                run_path=state_path.parent,
                status=str(payload.get("status") or "active"),
            )
        )
    return refs


# LLM: _task_state_paths finds V2 task workspace state files only.
# 函数用途: 列出 owner_home/tasks/<date>/<task>/work/state.json 文件。
def _task_state_paths(owner_home: Path) -> list[Path]:
    return sorted((owner_home / "tasks").glob("*/*/work/state.json"))


# LLM: _read_json tolerates broken maintenance inputs during rebuild previews.
# 函数用途: 读取 JSON 对象，缺失、损坏或非对象时返回空字典。
def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _task_ref_dict keeps rebuild JSON output stable without exposing dataclasses.
# 函数用途: 把 task 索引引用转成 CLI 可序列化字典。
def _task_ref_dict(ref: TaskIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "task_id": ref.task_id,
        "task_path": str(ref.task_path),
        "status": ref.status,
        "title": ref.title,
    }


# LLM: _run_ref_dict keeps rebuild JSON output stable without exposing dataclasses.
# 函数用途: 把 run 索引引用转成 CLI 可序列化字典。
def _run_ref_dict(ref: RunIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "run_id": ref.run_id,
        "task_id": ref.task_id,
        "run_path": str(ref.run_path),
        "status": ref.status,
    }


# LLM: _agent_ref_dict keeps rebuild JSON output stable without exposing dataclasses.
# 函数用途: 把 agent 索引引用转成 CLI 可序列化字典。
def _agent_ref_dict(ref: AgentIndexRef) -> dict[str, object]:
    return {
        "owner_id": ref.owner_id,
        "agent_id": ref.agent_id,
        "task_id": ref.task_id,
        "run_path": str(ref.run_path),
        "status": ref.status,
    }


__all__ = ["HomeIndexRebuildResult", "rebuild_home_indexes"]
