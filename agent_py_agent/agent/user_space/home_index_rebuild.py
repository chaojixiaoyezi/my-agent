
from __future__ import annotations

import json
import os
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
from .local_user_store import LocalUserStore
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
    source_paths = (home.global_index_owners_jsonl, home.global_index_active_tasks_jsonl,
                    home.global_index_active_runs_jsonl, home.global_index_active_agents_jsonl)
    source_stamps = {str(path): _index_source_stamp(path) for path in source_paths}
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
        applied=bool(apply and not load_errors),
        owners=owners,
        task_refs=tuple(task_refs),
        run_refs=tuple(run_refs),
        agent_refs=tuple(agent_refs),
        load_errors=tuple(load_errors),
    )
    if apply and not load_errors:
        replace_home_index_snapshots(
            home,
            owners=owners,
            task_refs=tuple(task_refs),
            run_refs=tuple(run_refs),
            agent_refs=tuple(agent_refs),
            expected_source_stamps=source_stamps,
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


# LLM: 只枚举已有本地目录；普通用户用原Store的local/user路径投影，不经旧provider回退、不物化账号。
# 函数用途: 以真实local用户目录与规范身份重建owner索引。
def _local_owner_records(home: MyAgentHomePaths) -> list[OwnerHomeResult]:
    if not home.local_owners_dir.exists():
        return []
    records = []
    for owner_dir in sorted(path for path in home.local_owners_dir.iterdir() if path.is_dir()):
        owner_name = owner_dir.name
        owner = (resolve_owner_home(home.root, OwnerIdentity.local_main()) if owner_name == "main"
                 else LocalUserStore(home.root).owner_home(owner_name))
        records.append(owner)
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


# LLM: 索引是可重建投影；新 runs 与存量 tasks 的宿主 state.json 都保留，用户项目内容不用于判断状态。
# 函数用途: 重建运行索引时同时读取当前归档和升级前的记录，不搬动用户文件。
def _task_state_paths(owner_home: Path) -> list[Path]:
    return sorted(
        path for root in ("runs", "tasks")
        for path in (owner_home / root).glob("*/*/work/state.json")
    )


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


def reconcile_home_index_page(home, *, owner_id: str, apply: bool = False,
                              cursor: str = "", limit: int | None = None,
                              config=None, budget=None, commit_transition=None) -> dict[str, object]:
    """按原索引字节游标校正位置引用；未覆盖、坏状态均不构成删除证明。"""
    import time
    from dataclasses import replace

    from .home_indexes import publish_workspace_refs, workspace_index_statuses
    from .storage_lanes import StorageLaneBudget

    if apply:
        from .home_index_compact import _require_coordinator_writer
        _require_coordinator_writer()

    result = dict(owner_id=owner_id, items=0, bytes=0, repaired=0, protected=0,
                  unknown=0, errors=[], next_cursor="", partial=True, effect_outcome="not_started")
    parts = Path(owner_id).parts
    if (not owner_id or Path(owner_id).is_absolute() or '..' in parts
            or not ((len(parts) == 2 and parts[0] == 'local')
                    or (len(parts) == 4 and parts[0] == 'providers' and parts[2] in ('users', 'groups')))):
        result['errors'] = [{'code': 'INVALID_OWNER_ID'}]
        return result
    owner_root = home.root / 'owners' / owner_id
    scoped = replace(home, owner_id=owner_id)
    if budget is None:
        budget = StorageLaneBudget(
            getattr(config, 'retention_scan_slice_entries', 128),
            getattr(config, 'retention_scan_slice_bytes', 8388608),
            time.monotonic() + getattr(config, 'retention_scan_slice_ms', 25) / 1000,
        )
    cap = min(limit or budget.max_items or 128, budget.max_items or 128)
    try:
        progress = json.loads(cursor) if cursor else {'owner': owner_id, 'phase': 0, 'offset': 0}
        if progress.get('owner') != owner_id:
            raise ValueError('owner_cursor_mismatch')
        phase = int(progress['phase'])
        offset = int(progress['offset'])
        if phase not in (0, 1) or offset < 0:
            raise ValueError('invalid_cursor_boundary')
    except (ValueError, TypeError, KeyError):
        result['errors'] = [{'code': 'INVALID_INDEX_CURSOR'}]
        return result
    paths = (home.global_index_active_tasks_jsonl, home.global_index_active_runs_jsonl)
    # 预算只在本页已前进至少一行后生效；否则时间片在首行前耗尽会让同一游标永远原样返回。
    def spent():
        return bool(result['items']) and budget.exhausted(result['items'], result['bytes'])

    while phase < len(paths) and result['items'] < cap and not spent():
        path = paths[phase]
        try:
            with path.open('rb') as source:
                stamp = source.fileno()
                import os
                st = os.fstat(stamp)
                identity = [st.st_dev, st.st_ino]
                saved_end = progress.get('end')
                if (progress.get('identity') and progress['identity'] != identity) or st.st_size < offset or (
                    saved_end is not None and st.st_size < int(saved_end)
                ):
                    if len(result['errors']) < 16:
                        result['errors'].append({'code': 'INDEX_SOURCE_CHANGED'})
                    offset = 0
                    progress = {'owner': owner_id, 'phase': phase, 'offset': 0}
                end = int(progress.get('end', st.st_size))
                source.seek(offset)
                while offset < end and result['items'] < cap and not spent():
                    remaining = (budget.max_bytes - result['bytes']) if budget.max_bytes is not None else 8388608
                    read_limit = max(1, min(remaining, end - offset))
                    raw = source.readline(read_limit)
                    result['bytes'] += len(raw)
                    if not raw.endswith(b'\n') and len(raw) < read_limit:
                        if len(result['errors']) < 16:
                            result['errors'].append({'code': 'INDEX_SOURCE_CHANGED'})
                        new_size = os.fstat(stamp).st_size
                        end = len(raw) if offset == 0 and new_size >= end else new_size
                        offset = 0
                        progress = {'owner': owner_id, 'phase': phase, 'offset': 0}
                        source.seek(0)
                        continue
                    if progress.get('discard'):
                        # 上页遇到超整页预算的单行：只丢弃到下一个换行，不解析、不当删除证明。
                        offset += len(raw)
                        result['items'] += 1
                        if raw.endswith(b'\n'):
                            progress = {key: value for key, value in progress.items() if key != 'discard'}
                        continue
                    if not raw or not raw.endswith(b'\n'):
                        if raw and offset + len(raw) >= end:
                            # 冻结 end 处没有换行的尾行（撕裂写或写入中）：越过它，留给下一轮新游标重读。
                            code, offset = 'INDEX_TAIL_INCOMPLETE', end
                        elif result['items'] == 0:
                            # 单行超过整页字节预算：跨页丢弃到换行，保证游标前进。
                            code, offset = 'INDEX_ROW_EXCEEDS_SLICE', offset + len(raw)
                            progress = dict(progress, discard=True)
                        else:
                            break  # 本页剩余字节不够读完下一行；下页从同一偏移继续，不算错误。
                        result['items'] += 1
                        result['unknown'] += 1
                        if len(result['errors']) < 16:
                            result['errors'].append({'code': code, 'phase': phase, 'offset': offset})
                        continue
                    offset = source.tell()
                    result['items'] += 1
                    try:
                        row = json.loads(raw)
                        if row.get('owner_id') != owner_id:
                            continue
                        root = Path(row['task_path']) if phase == 0 else Path(row['run_path']).parent
                        root.resolve().relative_to(owner_root.resolve())
                        state_path = root / 'work' / 'state.json'
                        state_path.resolve().relative_to(owner_root.resolve())
                        # 单一状态文件也纳入同拍字节预算，不把巨型损坏记录读入内存。
                        remaining = (budget.max_bytes - result['bytes']) if budget.max_bytes is not None else 8388608
                        with state_path.open('rb') as state_source:
                            if os.fstat(state_source.fileno()).st_size > remaining:
                                raise ValueError('canonical_record_exceeds_budget')
                            state_raw = state_source.read(max(0, remaining))
                        result['bytes'] += min(len(state_raw), max(0, remaining))
                        if len(state_raw) > remaining:
                            raise ValueError('canonical_record_exceeds_budget')
                        state = json.loads(state_raw)
                        if state.get('task_id') != row.get('task_id'):
                            raise ValueError('canonical_task_identity_mismatch')
                        run_id = str(state.get('primary_run_id') or state.get('run_id') or '')
                        if phase == 1 and row.get('run_id') != run_id:
                            result['protected'] += 1
                            continue
                        statuses = workspace_index_statuses(state, run_id)
                        desired = statuses[phase]
                        if desired == 'unknown' or not run_id:
                            raise ValueError('canonical_status_or_run_unknown')
                        if str(row.get('status') or '').lower() == desired:
                            continue
                        result['repaired'] += 1
                        if apply:
                            if budget.max_bytes is not None and result['bytes'] + len(state_raw) > budget.max_bytes:
                                result['unknown'] += 1
                                continue
                            result['bytes'] += len(state_raw)
                            def operation():
                                return publish_workspace_refs(scoped, task_id=state['task_id'], run_id=run_id, root=root, title=str(state.get('task_name') or row.get('title') or state['task_id']), max_bytes=len(state_raw))
                            prior_effect = result['effect_outcome']
                            result['effect_outcome'] = 'unknown'
                            receipt = commit_transition(operation) if commit_transition else operation()
                            if receipt.get('status') == 'published':
                                result['effect_outcome'] = 'applied'
                            else:
                                result['effect_outcome'] = prior_effect
                                result['protected'] += 1
                    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                        result['unknown'] += 1
                        if len(result['errors']) < 16:
                            result['errors'].append({'code': 'CANONICAL_INDEX_SOURCE_UNAVAILABLE', 'reason': type(exc).__name__})
                # 推进游标或阶段前用已持有的 FD 再 fstat：缓冲已预取整页时同 inode 截短不会碰到真实 EOF。
                # 截短或身份不符走原 INDEX_SOURCE_CHANGED，回偏移 0 重新打开幂等重读（不读旧缓冲）；完成只凭这份同身份证明。
                if _held_source_changed(stamp, identity, end):
                    if len(result['errors']) < 16:
                        result['errors'].append({'code': 'INDEX_SOURCE_CHANGED'})
                    offset = 0
                    progress = {'owner': owner_id, 'phase': phase, 'offset': 0}
                    continue
                progress = dict(owner=owner_id, phase=phase, offset=offset, identity=identity, end=end,
                                **({'discard': True} if progress.get('discard') else {}))
                if offset < end:
                    break
        except FileNotFoundError:
            result['unknown'] += 1
            if len(result['errors']) < 16:
                result['errors'].append({'code': 'INDEX_SOURCE_MISSING'})
        except OSError as exc:
            result['errors'].append({'code': 'INDEX_SOURCE_UNREADABLE', 'reason': type(exc).__name__})
            break
        phase += 1
        offset = 0
        progress = dict(owner=owner_id, phase=phase, offset=0)
    result['partial'] = phase < len(paths) or bool(result['unknown']) or bool(result['errors'])
    result['next_cursor'] = json.dumps(progress, separators=(',', ':')) if phase < len(paths) else ''
    return result


def maintain_home_indexes_lane(request) -> dict:
    """协调器沿原提交门处理当前 owner；不枚举其它 owner 或启动 Agent。"""
    if request.kind != 'global_indexes' or request.coordinator is not True:
        return dict(cursor=request.cursor, complete=False, items=0, bytes=0,
                    deferred_reason='coordinator_required')
    if 'compact' in request.cursor:
        from .home_index_compact import compact_index_page
        phase = int(request.cursor.get('file', 0))
        paths = (request.home.global_index_active_tasks_jsonl, request.home.global_index_active_runs_jsonl)
        if phase not in (0, 1):
            return dict(cursor={}, complete=False, items=0, bytes=0,
                        errors=[{'code': 'INVALID_COMPACT_FILE_CURSOR'}])
        receipt = request.run_write(lambda: compact_index_page(
            paths[phase], cursor=request.cursor.get('compact') or {}, budget=request.budget,
        ))
        if receipt['complete']:
            receipt['complete'] = phase == 1
            receipt['cursor'] = {} if phase == 1 else {'compact': {}, 'file': 1}
        else:
            receipt['cursor'] = {'compact': receipt['cursor'], 'file': phase}
        return receipt
    report = reconcile_home_index_page(
        request.home, owner_id=request.home.owner_id, apply=True,
        cursor=str(request.cursor.get('page') or ''), budget=request.budget,
        commit_transition=request.run_write,
    )
    return dict(cursor={'page': report['next_cursor']} if report['next_cursor'] else {'compact': {}, 'file': 0},
                complete=False, items=report['items'], bytes=report['bytes'],
                protected=report['protected'], errors=report['errors'])


def _index_source_stamp(path):
    try:
        st = path.stat(follow_symlinks=False)
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


# LLM: 只读已持有 FD 的 fstat，不重新按路径打开；身份须等于本页记入游标的 (st_dev, st_ino)，实际长度不得小于冻结 end。
# 函数用途: 判断本页索引源在推进前是否已被截短或身份不符，供完成证明绑定文件身份。
def _held_source_changed(fd: int, identity: list[int], end: int) -> bool:
    st = os.fstat(fd)
    return [st.st_dev, st.st_ino] != identity or st.st_size < end
