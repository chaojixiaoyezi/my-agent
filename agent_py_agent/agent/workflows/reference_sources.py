"""WorkflowReferenceSources.v1 (BIZ-012, V08-01..07)

Authoritative bounded read-only reference reader checking:
1. Scheduler rules / jobs (kind: 'schedule') -> REFERENCED_BY_SCHEDULE
2. Nested subworkflow steps (kind: 'workflow') -> REFERENCED_BY_SUBWORKFLOW
3. Business Space bindings / governance policies (kind: 'space') -> REFERENCED_BY_SPACE
4. Non-settled WorkflowRunStore runs (kind: 'workflow_run') -> ACTIVE_WORKFLOW_RUN
5. Open SessionTask records (kind: 'session_task') -> OPEN_SESSION_TASK

Provides WorkflowReferenceReader, build_workflow_reference_reader, and
WorkflowManagementService for full recycle / restore / orphan maintenance lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any

from ..assets.workflow_store import (
    BUSINESS_WORKFLOW_METADATA,
    WorkflowStore,
    normalize_workflow_asset_id,
)
from ..common.cache_freshness import cache_entry_trustworthy
from ..common.nofollow_fs import _supports_dir_fd, read_bytes_beneath, read_text_beneath
from ..conversation.business_spaces import BusinessSpaceService
from .business_scope import (
    WorkflowPolicyRefusal,
    WorkflowScopeService,
    workflow_state,
)
from .run_list_index import RunListIndex
from .run_store import WorkflowRunStore, _decode_run
from .run_store import has_open_obligation as workflow_has_open_obligation

_max_blockers_page = 100
_active_run_statuses = (
    "starting",
    "queued",
    "running",
    "waiting_for_agent",
    "waiting_human",
    "paused",
    "paused_for_resume",
    "cancelling",
)
_session_task_state_keys = frozenset({"epoch", "walk", "done", "position"})
_max_open_ref_backfill_batches = 32


def _space_record(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return vars(value)


def _workflow_reference(value, target, *, typed=False):
    # 通用metadata/package来源不能被当成工作流；显式workflow字段可接原裸ID。
    return (isinstance(value, str) and (typed or value.startswith('workflow:'))
            and normalize_workflow_asset_id(value) == target)


def _explicit_workflow_reference(value, target):
    if not isinstance(value, dict):
        return False
    if any(_workflow_reference(value.get(key), target, typed=True) for key in ('workflow_id', 'workflow_ref')):
        return True
    execution = value.get('execution_target')
    if isinstance(execution, dict) and execution.get('kind') == 'workflow':
        if _workflow_reference(execution.get('asset_id'), target, typed=True):
            return True
    context = value.get('business_context')
    if isinstance(context, dict):
        if any(_workflow_reference(context.get(key), target, typed=True) for key in ('workflow_id', 'workflow_ref')):
            return True
        refs = context.get('selected_method_refs', ())
        if isinstance(refs, (list, tuple)) and any(_workflow_reference(ref, target) for ref in refs):
            return True
    return False


def _scheduler_reference_row(job):
    """只投影引用扫描读取的字段：身份、可解析性、删除态与有效执行目标；运行计数与时间戳不进入代次。"""
    if not isinstance(job, dict):
        return None
    targets = [job.get("execution_target")]
    targets.extend(rule.get("execution_target") for rule in job.get("rules", [])
                   if isinstance(rule, dict) and rule.get("execution_target") is not None)
    return {"job_id": str(job.get("job_id") or job.get("id") or ""),
            "deleted": job.get("status") == "deleted", "targets": targets}


def _space_reference_row(space_id, space):
    # binding_state 沿用绑定检查的原读取口径（字典读 state，对象读属性），state 沿用空间自身的口径。
    binding_state = space.get("state") if isinstance(space, dict) else getattr(space, "state", "active")
    record = space if isinstance(space, dict) else _space_record(space)
    if not isinstance(record, dict):
        return {"id": space_id, "parsed": False, "binding_state": binding_state}
    return {
        "id": space_id, "parsed": True, "binding_state": binding_state,
        "state": record.get("state") or record.get("status") or "active",
        "mounts": record.get("mounts", []),
        "workflows": (record.get("workflows", []) or record.get("bound_workflows", [])
                      or record.get("workflow_ids", []) or []),
        "governance": record.get("governance_policies") or record.get("governance") or record.get("policies"),
    }


def _binding_reference_row(binding):
    if (not isinstance(binding, dict) or binding.get("state") not in ("bound", "unbound")
            or not isinstance(binding.get("space_ids"), list)
            or any(not isinstance(sid, str) or not sid for sid in binding["space_ids"])
            or any(key in binding and not isinstance(binding[key], str) for key in ("asset_id", "ref"))):
        return None
    return {"unbound": binding.get("state") == "unbound", "asset_id": binding.get("asset_id", ""),
            "ref": binding.get("ref", ""), "space_ids": binding.get("space_ids", [])}


def _subscription_reference_row(identifier, row):
    if not isinstance(row, dict):
        return {"id": identifier, "parsed": False}
    return {"id": identifier, "parsed": True, "state": row.get("state"), "policy_ref": row.get("policy_ref")}


def _reference_generation(snapshot):
    return hashlib.sha256(json.dumps(
        ["reference_projection.v1", snapshot], ensure_ascii=True, default=_space_record,
    ).encode()).hexdigest()


def _business_projection(catalog):
    if not isinstance(catalog, dict):
        raise ValueError("business reference catalog is invalid")
    spaces, bindings, subscriptions = (catalog.get(name, {}) for name in ("spaces", "bindings", "subscriptions"))
    if any(not isinstance(collection, dict) for collection in (spaces, bindings, subscriptions)):
        raise ValueError("business reference collection is invalid")
    space_rows = [_space_reference_row(key, spaces[key]) for key in sorted(spaces, key=str)]
    binding_rows = [_binding_reference_row(bindings[key]) for key in sorted(bindings, key=str)]
    subscription_rows = [_subscription_reference_row(key, subscriptions[key]) for key in sorted(subscriptions, key=str)]
    return space_rows, binding_rows, subscription_rows


def _prepare_owner_business_projection(service, identity):
    catalog = service._read(allow_legacy_references=True)
    if identity == (None, None):
        raw = read_bytes_beneath(service.owner_root, ("business", "catalog.json"))
        if raw is not None:
            catalog = json.loads(raw)
    rows = _business_projection(catalog)
    states = {row["id"]: row["binding_state"] for row in rows[0]}
    if service.reference_source_identity() != identity:
        raise BlockingIOError("business reference source changed during projection")
    return rows, states, _reference_generation(rows)


@lru_cache
def _cached_owner_business_projection(owner_home, owner_id, identity):
    # Retain reference fields only, with the source identity as the cache key.
    return _prepare_owner_business_projection(BusinessSpaceService(owner_home, owner_id=owner_id), identity)


def _owner_business_projection(service):
    identity = service.reference_source_identity()
    if any(item is not None for item in identity) and all(
            item is None or (item[2] is not None and cache_entry_trustworthy(item[0][3])) for item in identity):
        result = _cached_owner_business_projection(service.owner_root, service.owner_id, identity)
        if service.reference_source_identity() != identity:
            raise BlockingIOError("business reference source changed before paging")
        return result
    return _prepare_owner_business_projection(service, identity)


def _coverage_blocker(blockers, target, reason):
    blockers.append({'ref': target, 'kind': 'coverage', 'reason_code': reason})


def _needs_open_ref_backfill(probe_row: Mapping[str, Any]) -> bool:
    return not bool(probe_row.get("open_ref_complete")) or bool(probe_row.get("legacy_ids"))


def _extract_owner_home(host_or_owner_home: Any) -> Path | None:
    if host_or_owner_home is None:
        return None
    if isinstance(host_or_owner_home, (str, Path)):
        return Path(host_or_owner_home).resolve()
    paths = getattr(host_or_owner_home, "home_paths", None)
    if paths is not None:
        raw = getattr(paths, "owner_home_dir", None) or getattr(paths, "owner_home", None)
        if raw is not None:
            return Path(raw).resolve()
    for attr in ("owner_home_dir", "owner_home", "owner_root", "root"):
        raw = getattr(host_or_owner_home, attr, None)
        if raw is not None:
            return Path(raw).resolve()
    return None


def _session_task_refs(items, target_id):
    from ..conversation.session_tasks import has_open_obligation

    return [
        str(getattr(task, "task_id", "") or task)
        for task in items
        if _explicit_workflow_reference(getattr(task, "metadata", None), target_id)
        and has_open_obligation({"status": getattr(task, "status", None), "metadata": task.metadata})
    ]


def _session_task_blockers(blockers, task_ids):
    blockers.extend({"ref": task_id, "kind": "session_task", "reason_code": "OPEN_SESSION_TASK"}
                    for task_id in task_ids)


def _session_task_slice(task_store, target_id, blockers, budget):
    """宿主无Owner目录或目录版本时无法持久续点：只读一片预算，首个未结引用即停，未取尽即部分覆盖。"""
    cursor = None
    while True:
        limit = max(1, min(100, budget.max_entries - budget.entries))
        # 原分页合同只在有游标时传cursor；保留旧测试替身只接收limit的调用形状。
        page = task_store.list_page(limit=limit) if cursor is None else task_store.list_page(limit=limit, cursor=cursor)
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return False
        open_ids = _session_task_refs(items, target_id)
        _session_task_blockers(blockers, open_ids)
        following = page.get("next_cursor")
        # 读错误、覆盖不明、游标循环或预算耗尽都属"未知"，不能当作"没有引用"放行。
        if page.get("load_errors") or page.get("coverage", "complete") != "complete":
            return False
        if open_ids or not following:
            return not following
        budget.charge(entries=max(1, len(items)))
        if following == cursor or budget.entries >= budget.max_entries:
            return False
        cursor = following


def _session_task_generation(identity, epoch):
    return hashlib.sha256(json.dumps(["session_task_reference.v2", identity, epoch]).encode()).hexdigest()


def _session_task_state(previous, identity):
    """Accept only bounded continuations from this canonical directory and publication epoch."""
    try:
        state = json.loads(previous[1]) if previous else None
    except (TypeError, ValueError, RecursionError):
        return None
    if (isinstance(state, dict) and set(state) == _session_task_state_keys
            and isinstance(state["epoch"], str) and len(state["epoch"]) == 32
            and previous[0] == _session_task_generation(identity, state["epoch"])
            and (state["walk"] is None or isinstance(state["walk"], dict))
            and type(state["done"]) is bool and (not state["done"] or state["walk"] is None)
            and type(state["position"]) is int and state["position"] >= 0):
        return state
    return None


class WorkflowReferenceReader:
    """Authoritative bounded read-only workflow reference reader."""

    def __init__(
        self,
        host_or_owner_home: Any = None,
        *,
        workflow_store: Any = None,
        run_store: Any = None,
        space_store: Any = None,
        scheduler_repo: Any = None,
        session_task_store: Any = None,
        owner_id: str | None = None,
    ) -> None:
        self.host = host_or_owner_home if not isinstance(host_or_owner_home, (str, Path)) else None
        self.owner_home = _extract_owner_home(host_or_owner_home)
        self.owner_id = owner_id or getattr(getattr(self.host, "home_paths", None), "owner_id", None) or "local/main"

        # 1. WorkflowStore
        self.workflow_store = workflow_store
        if self.workflow_store is None and self.host is not None:
            self.workflow_store = getattr(self.host, "workflow_store", None)
            if self.workflow_store is None:
                workflows = getattr(self.host, "workflows", None)
                if workflows is not None and hasattr(workflows, "store"):
                    self.workflow_store = workflows.store
        if self.workflow_store is None and self.owner_home is not None:
            self.workflow_store = WorkflowStore(self.owner_home)

        # 2. WorkflowRunStore
        self.run_store = run_store
        if self.run_store is None and self.host is not None:
            self.run_store = getattr(self.host, "workflow_run_store", None) or getattr(self.host, "run_store", None)
            if self.run_store is None and hasattr(self.host, "engine") and hasattr(self.host.engine, "store"):
                self.run_store = self.host.engine.store
        if self.run_store is None and self.owner_home is not None:
            self.run_store = WorkflowRunStore(self.owner_home)

        # 3. SpaceStore / BusinessSpaceService
        self.space_store = space_store
        if self.space_store is None and self.host is not None:
            self.space_store = (
                getattr(self.host, "space_service", None)
                or getattr(self.host, "spaces", None)
                or getattr(self.host, "space_store", None)
            )

        # 4. SchedulerRepository
        self.scheduler_repo = scheduler_repo
        if self.scheduler_repo is None and self.host is not None:
            self.scheduler_repo = getattr(self.host, "scheduler_repository", None) or getattr(self.host, "scheduler", None)
        if self.scheduler_repo is None and self.owner_home is not None:
            for sub in ("data/scheduler", "scheduler"):
                candidate = self.owner_home / sub
                if candidate.exists() and any(candidate.iterdir()):
                    try:
                        from ..scheduler.repository import SchedulerRepository

                        self.scheduler_repo = SchedulerRepository(
                            candidate, owner_provider="local", owner_kind="main", owner_id="local/main"
                        )
                        break
                    except Exception:
                        pass

        # 5. SessionTaskStore
        self.session_task_store = session_task_store
        if self.session_task_store is None and self.host is not None:
            self.session_task_store = getattr(self.host, "session_task_store", None)
            if self.session_task_store is None and hasattr(self.host, "conversation_store"):
                self.session_task_store = getattr(self.host.conversation_store, "session_tasks", None)
        if self.session_task_store is None and self.owner_home is not None:
            conv_dir = self.owner_home / "conversation"
            if conv_dir.exists() and (conv_dir / "session_tasks").exists():
                try:
                    from ..conversation.session_tasks import SessionTaskStore
                    from ..conversation.store_layout import ConversationStorage

                    storage = ConversationStorage(conv_dir, initialize=False)
                    self.session_task_store = SessionTaskStore(storage)
                except Exception:
                    pass
            elif (self.owner_home / "session_tasks").exists():
                try:
                    from ..conversation.session_tasks import SessionTaskStore

                    self.session_task_store = SessionTaskStore(self.owner_home)
                except Exception:
                    pass

    def read_references(self, workflow_id: str) -> dict[str, Any]:
        target_id = normalize_workflow_asset_id(workflow_id)
        if not isinstance(target_id, str) or not target_id:
            return {"coverage": "complete", "blockers": []}

        blockers: list[dict[str, str]] = []
        coverage = "complete"
        generations: dict[str, str] = {}

        # 1. Scheduler rules / jobs
        ok_sched = self._check_scheduler(target_id, blockers, generations)
        if not ok_sched:
            coverage = "unknown"
            _coverage_blocker(blockers, target_id, "SCHEDULER_REFERENCE_SOURCE_INCOMPLETE")

        # 2. Nested subworkflow steps
        ok_sub = self._check_subworkflows(target_id, blockers)
        if not ok_sub:
            coverage = "unknown"

        # 3. Business space bindings / governance policies
        ok_space = self._check_spaces(target_id, blockers, generations)
        if not ok_space:
            coverage = "unknown"
            _coverage_blocker(blockers, target_id, "BUSINESS_REFERENCE_SOURCE_INCOMPLETE")

        # 4. Non-settled WorkflowRunStore runs
        ok_runs = self._check_runs(target_id, blockers)
        if not ok_runs:
            coverage = "unknown"
            _coverage_blocker(blockers, target_id, "WORKFLOW_RUN_REFERENCE_SOURCE_INCOMPLETE")

        # 5. Open SessionTask records
        ok_tasks = self._check_session_tasks(target_id, blockers)
        if not ok_tasks:
            coverage = "unknown"
            _coverage_blocker(blockers, target_id, "SESSION_TASK_REFERENCE_SOURCE_INCOMPLETE")

        # Deduplicate blockers preserving order
        unique_blockers: list[dict[str, str]] = []
        seen = set()
        for b in blockers:
            key = (b["ref"], b["kind"], b["reason_code"])
            if key not in seen:
                seen.add(key)
                unique_blockers.append({"ref": b["ref"], "kind": b["kind"], "reason_code": b["reason_code"]})

        if len(unique_blockers) > _max_blockers_page:
            coverage = 'unknown'
            unique_blockers.insert(0, {'ref': target_id, 'kind': 'coverage',
                'reason_code': 'WORKFLOW_REFERENCE_BLOCKERS_TRUNCATED'})
        result = {"coverage": coverage, "blockers": unique_blockers[:_max_blockers_page]}
        if generations:
            result["coverage_generation"] = generations
        return result

    def _source_page(self, source, target_id, snapshot, size, blockers, generations, scan, *, generation=None):
        """跨调用续扫同一份按身份排序的引用语义投影；成员、可解析性、删除态或有效目标变化才使旧覆盖与阻断项失效。"""
        if size <= _max_blockers_page:
            return scan(0, _max_blockers_page, blockers)
        if self.owner_home is None:
            scan(0, _max_blockers_page, blockers)
            return False

        generation = generation or _reference_generation(snapshot)
        generations[source] = generation
        index = RunListIndex(self.owner_home)
        source_id = f"{source}:{target_id}"
        previous = index.reference_cursor(source_id)
        try:
            state = json.loads(previous[1]) if previous and previous[0] == generation else {}
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        start = state.get('offset', 0)
        prior = state.get('blockers', [])
        valid = state.get('valid', True)
        if (type(start) is not int or not 0 <= start <= size or type(valid) is not bool
                or not isinstance(prior, list) or len(prior) > _max_blockers_page + 1
                or any(not isinstance(row, dict) or any(not isinstance(row.get(key), str)
                       for key in ('kind', 'ref', 'reason_code')) for row in prior)):
            start, prior, valid = 0, [], True
        if start == size:
            blockers.extend(prior)
            return valid

        found = []
        end = min(start + _max_blockers_page, size)
        valid = scan(start, end, found) and valid
        # 保留多一项供原输出截断逻辑识别覆盖不全，避免分页回执无界累加。
        combined = list({(row['kind'], row['ref'], row['reason_code']): row
                         for row in [*prior, *found]}.values())[:_max_blockers_page + 1]
        blockers.extend(combined)
        cursor = json.dumps({'offset': end, 'blockers': combined, 'valid': valid}, ensure_ascii=True)
        committed = index.advance_reference_cursor(
            source_id, previous=previous, generation=generation, cursor=cursor, now=time.time(),
        )
        return committed and end == size and valid

    def _check_scheduler(self, target_id: str, blockers: list[dict[str, str]], generations) -> bool:
        # 宿主先于首个调度装配时不能把当时的空目录永久缓存为空引用。
        if self.scheduler_repo is None and self.owner_home is not None:
            try:
                from ..scheduler.repository import SchedulerRepository

                for sub in ('data/scheduler', 'scheduler'):
                    candidate = self.owner_home / sub
                    if candidate.exists() and any(candidate.iterdir()):
                        self.scheduler_repo = SchedulerRepository(
                            candidate, owner_provider='local', owner_kind='main', owner_id='local/main',
                        )
                        break
            except (OSError, ValueError, TypeError):
                return False
        if self.scheduler_repo is None:
            return True
        try:
            listed = self.scheduler_repo.list_jobs(include_deleted=False)
            # 原repository合同为(jobs, errors)；形状不符或errors非空都属覆盖未知。
            if not isinstance(listed, tuple) or len(listed) != 2:
                return False
            jobs, errors = listed
            # 按稳定身份分页并只保留引用语义字段；普通执行完成改写计数、时间戳或下次执行顺序不会让续扫归零。
            rows = sorted((_scheduler_reference_row(job) for job in jobs or []),
                          key=lambda row: "" if row is None else row["job_id"])

            def scan(start, end, found):
                return self._scheduler_page(rows, target_id, found, start, end)

            if errors:
                scan(0, _max_blockers_page, blockers)
                return False
            return self._source_page('scheduler', target_id, rows, len(rows), blockers, generations, scan)
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return False
        except Exception:
            return False

    @staticmethod
    def _scheduler_page(rows, target_id, blockers, start, end):
        for row in rows[start:end]:
            if row is None or row["deleted"]:
                continue
            job_id = row["job_id"]
            referenced = False
            for t in row["targets"]:
                if not t:
                    continue
                if isinstance(t, dict):
                    asset_id = normalize_workflow_asset_id(
                        str(t.get("asset_id") or t.get("workflow_id") or t.get("workflow_ref") or "")
                    )
                    kind = str(t.get("kind") or "")
                    if asset_id == target_id and kind == "workflow":
                        referenced = True
                        break
                elif isinstance(t, str):
                    if _workflow_reference(t, target_id):
                        referenced = True
                        break
            if referenced and job_id:
                blockers.append({"ref": job_id, "kind": "schedule", "reason_code": "REFERENCED_BY_SCHEDULE"})
        return True

    def _check_subworkflows(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        try:
            if self.owner_home is not None:
                from .reference_index import read_parents

                diagnostics = {}
                complete, parents = read_parents(self.workflow_store, target_id,
                    limit=_max_blockers_page, diagnostics=diagnostics)
                if not complete:
                    blockers.append({'ref': target_id, 'kind': 'coverage',
                        'reason_code': diagnostics.get('reason_code', 'WORKFLOW_REFERENCE_INDEX_STALE')})
                blockers.extend({"ref": parent, "kind": "workflow", "reason_code": "REFERENCED_BY_SUBWORKFLOW"}
                                for parent in parents)
                return complete
            elif self.workflow_store is not None:
                # Fallback for custom mocks without owner_home
                records = getattr(self.workflow_store, "workflows", None) or getattr(self.workflow_store, "_records", None)
                if isinstance(records, dict):
                    for parent_id, wf in islice(records.items(), _max_blockers_page):
                        if normalize_workflow_asset_id(parent_id) == target_id:
                            continue
                        steps = getattr(wf, "steps", []) if not isinstance(wf, dict) else wf.get("steps", [])
                        is_ref = False
                        for step in steps:
                            sub_ref = normalize_workflow_asset_id(
                                getattr(step, "subworkflow_ref", "") if not isinstance(step, dict) else step.get("subworkflow_ref", "")
                            )
                            if sub_ref == target_id:
                                is_ref = True
                                break
                        if is_ref:
                            blockers.append({"ref": parent_id, "kind": "workflow", "reason_code": "REFERENCED_BY_SUBWORKFLOW"})
                    return len(records) <= _max_blockers_page
            return True
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return False
        except Exception:
            return False

    def _check_spaces(self, target_id: str, blockers: list[dict[str, str]], generations) -> bool:
        try:
            generation = None
            if type(self.space_store) is BusinessSpaceService:
                snapshot, states, generation = _owner_business_projection(self.space_store)
            elif self.space_store is None and self.owner_home is not None:
                service = BusinessSpaceService(self.owner_home, owner_id=self.owner_id)
                snapshot, states, generation = _owner_business_projection(service)
            elif self.space_store is not None:
                catalog = None
                if hasattr(self.space_store, "_read"):
                    catalog = self.space_store._read()
                elif isinstance(self.space_store, dict):
                    catalog = self.space_store
                elif hasattr(self.space_store, "catalog"):
                    catalog = self.space_store.catalog
                elif hasattr(self.space_store, "list_spaces"):
                    spaces_list = self.space_store.list_spaces()
                    catalog = {"spaces": {getattr(s, "space_id", str(s)): s for s in spaces_list}}
                if catalog is None:
                    return True
                snapshot = _business_projection(catalog)
                states = {row["id"]: row["binding_state"] for row in snapshot[0]}
            else:
                return True

            def scan(start, end, found):
                return self._space_page(snapshot, states, target_id, found, start, end)

            return self._source_page('business', target_id, snapshot, max(map(len, snapshot)),
                                     blockers, generations, scan, generation=generation)
        except (OSError, ValueError, KeyError):
            return False
        except Exception:
            return False

    @staticmethod
    def _space_page(snapshot, space_states, target_id, blockers, start, end):
        space_rows, binding_rows, subscription_rows = snapshot
        complete = True
        for space in space_rows[start:end]:
            space_id = space["id"]
            if not space["parsed"]:
                complete = False
                continue
            if space["state"] in ("archived", "deleted"):
                continue
            is_ref = False
            # mounts
            for mount in space["mounts"]:
                if isinstance(mount, dict):
                    if any(
                        normalize_workflow_asset_id(mount.get(k, "")) == target_id
                        for k in ("workflow_id", "workflow_ref")
                    ) or any(_workflow_reference(mount.get(k), target_id) for k in ("package_ref", "ref", "asset_id")):
                        is_ref = True
                        break
                elif _workflow_reference(mount, target_id):
                    is_ref = True
                    break
            # bound workflows
            if not is_ref:
                for w in space["workflows"]:
                    if normalize_workflow_asset_id(str(w)) == target_id:
                        is_ref = True
                        break
            # governance policies
            if not is_ref:
                gov = space["governance"]
                if gov:
                    if (_explicit_workflow_reference(gov, target_id)
                            or _workflow_reference(gov, target_id)):
                        is_ref = True
            if is_ref:
                blockers.append({"ref": str(space_id), "kind": "space", "reason_code": "REFERENCED_BY_SPACE"})

        # bindings
        if binding_rows:
            for binding in binding_rows[start:end]:
                if binding is None:
                    complete = False
                    continue
                if binding["unbound"]:
                    continue
                b_asset = normalize_workflow_asset_id(binding["asset_id"])
                b_ref = normalize_workflow_asset_id(binding["ref"])
                if target_id in (b_asset, b_ref):
                    for sid in binding["space_ids"]:
                        sp_state = space_states.get(sid)
                        if sp_state not in ("archived", "deleted"):
                            blockers.append(
                                {"ref": str(sid), "kind": "space", "reason_code": "REFERENCED_BY_SPACE"}
                            )
        for row in subscription_rows[start:end]:
            identifier = row['id']
            if not row['parsed']:
                complete = False
                continue
            if row['state'] not in ('active', 'paused', 'removed'):
                complete = False
            if row['state'] in ('active', 'paused') and _workflow_reference(row['policy_ref'], target_id):
                blockers.append({'ref': str(identifier), 'kind': 'space',
                                 'reason_code': 'REFERENCED_BY_SPACE'})
        return complete

    def _check_runs(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        complete = True
        try:
            if self.owner_home is not None:
                runs_root = self.owner_home / "data" / "workflow_runs"
                index_path = self.owner_home / "data" / "workflow_run_index.sqlite3"
                found_run_ids: set[str] = set()
                index_queried_ok = False
                if index_path.exists():
                    try:
                        from .run_list_index import RunListIndex

                        idx = RunListIndex(self.owner_home)
                        probe = idx.open_reference_probe(
                            target_id, active_statuses=_active_run_statuses, limit=_max_blockers_page
                        )
                        if _needs_open_ref_backfill(probe):
                            backfill_store = WorkflowRunStore(self.owner_home)
                            for _ in range(_max_open_ref_backfill_batches):
                                if not _needs_open_ref_backfill(probe):
                                    break
                                # 只运行无锁 backfill（推进 open_ref_scan 与 legacy），绝不在读口/资产锁内 replay(pending) 抢 run_store._lock。
                                idx.backfill(backfill_store)
                                next_probe = idx.open_reference_probe(
                                    target_id, active_statuses=_active_run_statuses, limit=_max_blockers_page
                                )
                                if (
                                    bool(next_probe["open_ref_complete"]) == bool(probe["open_ref_complete"])
                                    and bool(probe["legacy_ids"])
                                    and len(next_probe["legacy_ids"]) >= len(probe["legacy_ids"])
                                    and next_probe["legacy_ids"] == probe["legacy_ids"]
                                ):
                                    probe = next_probe
                                    break
                                probe = next_probe
                        pending_ids = probe["pending_ids"]
                        legacy_ids = probe["legacy_ids"]
                        complete = (
                            bool(probe["open_ref_complete"])
                            and not pending_ids
                            and not legacy_ids
                            and not probe["truncated"]
                        )
                        candidates = list(dict.fromkeys([*probe["candidate_ids"], *pending_ids, *legacy_ids]))
                        retries = 2 if _supports_dir_fd() else 0
                        for r_id in candidates[: _max_blockers_page * 2]:
                            raw_bytes = read_bytes_beneath(
                                self.owner_home, ("data", "workflow_runs", r_id, "run.json"), snapshot_retries=retries
                            )
                            if raw_bytes is None:
                                if r_id in pending_ids:
                                    complete = False
                                continue
                            data = json.loads(raw_bytes.decode("utf-8"))
                            record = _decode_run(data)
                            if record.workflow_run_id != r_id:
                                complete = False
                                continue
                            if normalize_workflow_asset_id(record.workflow_id) == target_id and workflow_has_open_obligation(data):
                                found_run_ids.add(r_id)
                        index_queried_ok = True
                    except Exception:
                        index_queried_ok = False
                        complete = False
                if not index_queried_ok and runs_root.exists() and runs_root.is_dir():
                    scanned = 0
                    max_scan = _max_blockers_page
                    retries = 2 if _supports_dir_fd() else 0
                    for entry in runs_root.iterdir():
                        if scanned >= max_scan:
                            complete = False
                            break
                        scanned += 1
                        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                            continue
                        if entry.name in found_run_ids:
                            continue
                        raw_bytes = read_bytes_beneath(
                            self.owner_home, ("data", "workflow_runs", entry.name, "run.json"), snapshot_retries=retries
                        )
                        if raw_bytes is None:
                            complete = False
                            continue
                        data = json.loads(raw_bytes.decode("utf-8"))
                        record = _decode_run(data)
                        if record.workflow_run_id != entry.name:
                            complete = False
                            continue
                        if normalize_workflow_asset_id(record.workflow_id) == target_id and workflow_has_open_obligation(data):
                            found_run_ids.add(entry.name)
                if len(found_run_ids) > _max_blockers_page:
                    complete = False
                for r_id in sorted(found_run_ids)[:_max_blockers_page]:
                    blockers.append({"ref": str(r_id), "kind": "workflow_run", "reason_code": "ACTIVE_WORKFLOW_RUN"})
            elif self.run_store is not None:
                # Fallback for mock run_store
                for st in _active_run_statuses:
                    for q_id in (target_id, f"workflow:{target_id}"):
                        runs = self.run_store.list_runs(workflow_id=q_id, status=st, limit=_max_blockers_page + 1)
                        complete = complete and len(runs) <= _max_blockers_page
                        for r in runs[:_max_blockers_page]:
                            run_id = getattr(r, "workflow_run_id", None) or getattr(r, "run_id", None) or str(r)
                            if run_id:
                                blockers.append(
                                    {"ref": str(run_id), "kind": "workflow_run", "reason_code": "ACTIVE_WORKFLOW_RUN"}
                                )
            return complete
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return False
        except Exception:
            return False

    def _check_session_tasks(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        task_store = self.session_task_store or getattr(getattr(self.host, "conversation_store", None), "session_tasks", None)
        if task_store is None and self.owner_home is not None:
            conv_dir = self.owner_home / "conversation"
            if conv_dir.exists() and (conv_dir / "session_tasks").exists():
                try:
                    from ..conversation.session_tasks import SessionTaskStore
                    from ..conversation.store_layout import ConversationStorage

                    storage = ConversationStorage(conv_dir, initialize=False)
                    task_store = SessionTaskStore(storage)
                except (OSError, ValueError, KeyError):
                    return False
            elif (self.owner_home / "session_tasks").exists():
                try:
                    from ..conversation.session_tasks import SessionTaskStore

                    task_store = SessionTaskStore(self.owner_home)
                except (OSError, ValueError, KeyError):
                    return False

        if task_store is None:
            return True

        try:
            from ..memory_store.retention_scan_budget import RetentionScanBudget

            # 每次调用只按已加载预算的条目维度推进；耗尽即报告部分覆盖并留下续点。
            budget = RetentionScanBudget.default(getattr(self.host, "config", None))
            if self.owner_home is None or not callable(getattr(task_store, "obligation_change_page", None)):
                return _session_task_slice(task_store, target_id, blockers, budget)
            return self._session_task_coverage(task_store, target_id, blockers, budget)
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return False
        except Exception:
            return False

    def _session_task_coverage(self, task_store, target_id, blockers, budget):
        """Cover the canonical baseline and subsequent publications through a verified boundary."""
        version = task_store.obligation_source_version()
        if version is None:
            return True
        identity = list(version[:2])
        index = RunListIndex(self.owner_home)
        source_id = f"session_tasks:{target_id}"
        previous = (index.reference_cursor(source_id)
                    if (self.owner_home / "data" / "workflow_run_index.sqlite3").exists() else None)
        state = _session_task_state(previous, identity)
        # Reserve at least one entry for the canonical walk while it is still in progress.
        reserve = int(state is None or not state["done"])
        limit = min(_max_blockers_page, max(0, budget.max_entries - reserve))
        epoch, head, changes = task_store.obligation_change_page(
            state["epoch"] if state else None, state["position"] if state else 0, limit=limit,
        )
        if state is None or state["epoch"] != epoch or state["position"] > head:
            state = {"epoch": epoch, "walk": None, "done": False, "position": head}
        elif state["done"] and state["position"] == head:
            return task_store.obligation_source_version() == version

        def commit(complete):
            generation = _session_task_generation(identity, epoch)
            cursor = json.dumps(state, ensure_ascii=True, separators=(",", ":"))
            if previous != (generation, cursor) and (previous is not None or not complete):
                if not index.advance_reference_cursor(
                    source_id, previous=previous, generation=generation, cursor=cursor, now=time.time(),
                ):
                    return False
            return complete and task_store.obligation_source_version() == version

        blockers_seen = []
        for position, path in changes:
            budget.charge(entries=1)
            if not isinstance(path, str) or not path.endswith(".json"):
                return commit(False)
            task = task_store.load(path.removesuffix(".json"))
            if task is None or task.task_id != path.removesuffix(".json"):
                return commit(False)
            blockers_seen.extend(_session_task_refs([task], target_id))
            if not blockers_seen:
                state["position"] = position
        if not blockers_seen and limit and len(changes) < limit:
            state["position"] = head
        if blockers_seen:
            _session_task_blockers(blockers, blockers_seen)
            last_change = bool(changes and position == head)
            return commit(state["done"] and last_change)

        while not state["done"] and budget.entries < budget.max_entries:
            page = task_store.list_page(limit=min(_max_blockers_page, budget.max_entries - budget.entries),
                                        cursor=state["walk"], read_only=True)
            items = page.get("items") if isinstance(page, dict) else None
            if (not isinstance(items, list) or page.get("load_errors")
                    or page.get("coverage", "complete") != "complete"):
                return commit(False)
            following = page.get("next_cursor")
            if following is not None and following == state["walk"]:
                return commit(False)
            open_ids = _session_task_refs(items, target_id)
            if open_ids:
                _session_task_blockers(blockers, open_ids)
                return commit(following is None and state["position"] == head)
            budget.charge(entries=max(1, page.get("read_count") or len(items)))
            state["walk"], state["done"] = following, following is None
        return commit(state["done"] and state["position"] == head)

    def __call__(
        self,
        target_or_kind: str | None = None,
        target_id: str | None = None,
        *,
        workflow_id: str | None = None,
        ref_id: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        if (
            target_or_kind is None
            and target_id is None
            and workflow_id is None
            and ref_id is None
            and ref is None
        ):
            return {"coverage": "complete", "blockers": []}
        if target_id is not None and target_or_kind in ("workflow", "workflows"):
            resolved_id = str(target_id)
        elif target_or_kind is not None:
            resolved_id = str(target_or_kind)
        elif workflow_id is not None:
            resolved_id = str(workflow_id)
        elif ref_id is not None:
            resolved_id = str(ref_id)
        elif ref is not None:
            resolved_id = str(ref)
        elif target_id is not None:
            resolved_id = str(target_id)
        else:
            return {"coverage": "complete", "blockers": []}
        return self.read_references(resolved_id)


def build_workflow_reference_reader(
    host_or_owner_home: Any,
    *,
    workflow_store: Any = None,
    run_store: Any = None,
    space_store: Any = None,
    scheduler_repo: Any = None,
    session_task_store: Any = None,
    owner_id: str | None = None,
) -> Callable[[str], dict[str, Any]]:
    """Factory creating authoritative WorkflowReferenceReader callable."""
    return WorkflowReferenceReader(
        host_or_owner_home,
        workflow_store=workflow_store,
        run_store=run_store,
        space_store=space_store,
        scheduler_repo=scheduler_repo,
        session_task_store=session_task_store,
        owner_id=owner_id,
    )


class WorkflowManagementService(WorkflowScopeService):
    """WorkflowManagementService (BIZ-012, V08-01..07)

    Full lifecycle management for workflows (recycle, recycled.list, restore, cleanup).
    """

    def __init__(
        self,
        owner_home: Any,
        *,
        owner_id: str = "local/main",
        authorize: Any = None,
        reference_reader: Any = None,
        clock: Any = None,
        context_provider: Any = None,
        agent_steps: Any = None,
        governance: Any = None,
        engine_factory: Any = None,
    ) -> None:
        if reference_reader is None:
            reference_reader = build_workflow_reference_reader(owner_home, owner_id=owner_id)
        if authorize is None:
            def _default_authorize(actor: Any, action: str, ref: str) -> bool:
                return True

            authorize = _default_authorize
        super().__init__(
            owner_home,
            owner_id=owner_id,
            authorize=authorize,
            reference_reader=reference_reader,
            clock=clock,
            context_provider=context_provider,
            agent_steps=agent_steps,
            governance=governance,
            engine_factory=engine_factory,
        )

    def execute(self, action: str, request: dict, *, actor: Any, expected_revision: int | None = None, idempotency_key: str = "") -> dict:
        if action == "workflows.recycled.list":
            req = dict(request)
            req["lifecycle"] = "recycled"
            return self._list(req, actor)
        if action == "workflows.recycle":
            ref = request.get("workflow_ref", "")
            self._authorize(actor, action, ref)
            definition = self._get(ref)
            from .orphan_maintenance import cleanup_status

            status = cleanup_status(self, definition)
            if status["blockers"]:
                first_code = status["blockers"][0]["reason_code"]
                if any(b.get("reason_code") == "REFERENCE_COVERAGE_UNKNOWN" for b in status["blockers"]):
                    raise WorkflowPolicyRefusal(
                        "REFERENCE_COVERAGE_UNKNOWN",
                        details={
                            "blocker_reason_code": "REFERENCE_COVERAGE_UNKNOWN",
                            "blocker_count": len(status["blockers"]),
                            "blockers": status["blockers"],
                        },
                    )
                raise WorkflowPolicyRefusal(
                    "WORKFLOW_CLEANUP_BLOCKED",
                    details={
                        "blocker_reason_code": first_code,
                        "blocker_count": len(status["blockers"]),
                        "blockers": status["blockers"],
                    },
                )
        return super().execute(action, request, actor=actor, expected_revision=expected_revision, idempotency_key=idempotency_key)


__all__ = [
    "WorkflowReferenceReader",
    "build_workflow_reference_reader",
    "WorkflowManagementService",
    "normalize_workflow_asset_id",
]
