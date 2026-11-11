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

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..assets.workflow_store import (
    BUSINESS_WORKFLOW_METADATA,
    WorkflowStore,
    normalize_workflow_asset_id,
)
from ..common.nofollow_fs import read_text_beneath
from .business_scope import (
    WorkflowPolicyRefusal,
    WorkflowScopeService,
    workflow_state,
)
from .run_store import WorkflowRunStore

_max_blockers_page = 100
_active_run_statuses = ("running", "queued", "waiting_for_agent", "paused_for_resume")
_open_session_task_statuses = ("queued", "pending", "running", "accepted")


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
    ) -> None:
        self.host = host_or_owner_home if not isinstance(host_or_owner_home, (str, Path)) else None
        self.owner_home = _extract_owner_home(host_or_owner_home)

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

        # 1. Scheduler rules / jobs
        ok_sched = self._check_scheduler(target_id, blockers)
        if not ok_sched:
            coverage = "unknown"

        # 2. Nested subworkflow steps
        ok_sub = self._check_subworkflows(target_id, blockers)
        if not ok_sub:
            coverage = "unknown"

        # 3. Business space bindings / governance policies
        ok_space = self._check_spaces(target_id, blockers)
        if not ok_space:
            coverage = "unknown"

        # 4. Non-settled WorkflowRunStore runs
        ok_runs = self._check_runs(target_id, blockers)
        if not ok_runs:
            coverage = "unknown"

        # 5. Open SessionTask records
        ok_tasks = self._check_session_tasks(target_id, blockers)
        if not ok_tasks:
            coverage = "unknown"

        # Deduplicate blockers preserving order
        unique_blockers: list[dict[str, str]] = []
        seen = set()
        for b in blockers:
            key = (b["ref"], b["kind"], b["reason_code"])
            if key not in seen:
                seen.add(key)
                unique_blockers.append({"ref": b["ref"], "kind": b["kind"], "reason_code": b["reason_code"]})

        return {"coverage": coverage, "blockers": unique_blockers[:_max_blockers_page]}

    def _check_scheduler(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        if self.scheduler_repo is None:
            return True
        try:
            jobs, _ = self.scheduler_repo.list_jobs(include_deleted=False)
            for job in jobs:
                if not isinstance(job, dict) or job.get("status") == "deleted":
                    continue
                job_id = str(job.get("job_id") or job.get("id") or "")
                targets = [job.get("execution_target")]
                for rule in job.get("rules", []):
                    if isinstance(rule, dict) and rule.get("execution_target") is not None:
                        targets.append(rule.get("execution_target"))
                referenced = False
                for t in targets:
                    if not t:
                        continue
                    if isinstance(t, dict):
                        asset_id = normalize_workflow_asset_id(
                            str(t.get("asset_id") or t.get("workflow_id") or t.get("workflow_ref") or "")
                        )
                        kind = str(t.get("kind") or "")
                        if asset_id == target_id and (kind in ("workflow", "") or not kind):
                            referenced = True
                            break
                    elif isinstance(t, str):
                        if normalize_workflow_asset_id(t) == target_id:
                            referenced = True
                            break
                if referenced and job_id:
                    blockers.append({"ref": job_id, "kind": "schedule", "reason_code": "REFERENCED_BY_SCHEDULE"})
            return True
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return False
        except Exception:
            return False

    def _check_subworkflows(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        try:
            wf_dir = self.owner_home / "workflows" if self.owner_home is not None else None
            if wf_dir and wf_dir.is_dir():
                for entry in wf_dir.iterdir():
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    parent_id = entry.name
                    if normalize_workflow_asset_id(parent_id) == target_id:
                        continue
                    raw = read_text_beneath(self.owner_home, ("workflows", parent_id, "workflow.json"))
                    if raw is None:
                        continue
                    data = json.loads(raw)
                    metadata = data.get("metadata") or data.get("descriptor", {}).get("metadata", {}) or {}
                    state = metadata.get(BUSINESS_WORKFLOW_METADATA, {}) or metadata.get("business_scope_state", {})
                    if state.get("lifecycle") == "recycled":
                        continue
                    is_ref = False
                    for step in data.get("steps", []):
                        if not isinstance(step, dict):
                            continue
                        mode = step.get("mode", "")
                        sub_ref = normalize_workflow_asset_id(step.get("subworkflow_ref", ""))
                        wf_ref = normalize_workflow_asset_id(step.get("workflow_id", ""))
                        cfg = step.get("config", {}) or {}
                        cfg_wf = normalize_workflow_asset_id(cfg.get("workflow_id", ""))
                        cfg_sub = normalize_workflow_asset_id(cfg.get("subworkflow_id", ""))
                        if (
                            sub_ref == target_id
                            or wf_ref == target_id
                            or cfg_wf == target_id
                            or cfg_sub == target_id
                            or (
                                mode == "subworkflow"
                                and step.get("subworkflow_ref") in (target_id, f"workflow:{target_id}")
                            )
                        ):
                            is_ref = True
                            break
                    if is_ref:
                        blockers.append({"ref": parent_id, "kind": "workflow", "reason_code": "REFERENCED_BY_SUBWORKFLOW"})
            elif self.workflow_store is not None:
                # Fallback for custom mocks without owner_home
                records = getattr(self.workflow_store, "workflows", None) or getattr(self.workflow_store, "_records", None)
                if isinstance(records, dict):
                    for parent_id, wf in records.items():
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
            return True
        except (OSError, ValueError, KeyError):
            return False
        except Exception:
            return False

    def _check_spaces(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        catalog = None
        try:
            if self.space_store is not None:
                if hasattr(self.space_store, "_read"):
                    catalog = self.space_store._read()
                elif isinstance(self.space_store, dict):
                    catalog = self.space_store
                elif hasattr(self.space_store, "catalog"):
                    catalog = self.space_store.catalog
                elif hasattr(self.space_store, "list_spaces"):
                    spaces_list = self.space_store.list_spaces()
                    catalog = {"spaces": {getattr(s, "space_id", str(s)): s for s in spaces_list}}
            elif self.owner_home is not None:
                for parts in (("data", "business", "catalog.json"), ("business", "catalog.json")):
                    cat_file = self.owner_home.joinpath(*parts)
                    if cat_file.exists():
                        raw = cat_file.read_text(encoding="utf-8")
                        catalog = json.loads(raw)
                        break

            if catalog is None:
                return True

            if not isinstance(catalog, dict):
                return False

            spaces = catalog.get("spaces", {})
            if not isinstance(spaces, dict):
                return False

            for space_id, space in spaces.items():
                if not isinstance(space, dict):
                    space = getattr(space, "to_dict", lambda: dict(space))() if hasattr(space, "to_dict") else vars(space)
                if not isinstance(space, dict):
                    continue
                state = space.get("state") or space.get("status") or "active"
                if state in ("archived", "deleted"):
                    continue
                is_ref = False
                # mounts
                for mount in space.get("mounts", []):
                    if isinstance(mount, dict):
                        if any(
                            normalize_workflow_asset_id(mount.get(k, "")) == target_id
                            for k in ("package_ref", "asset_id", "workflow_id", "ref")
                        ):
                            is_ref = True
                            break
                    elif isinstance(mount, str) and normalize_workflow_asset_id(mount) == target_id:
                        is_ref = True
                        break
                # bound workflows
                if not is_ref:
                    bound_flows = (
                        space.get("workflows", [])
                        or space.get("bound_workflows", [])
                        or space.get("workflow_ids", [])
                        or []
                    )
                    for w in bound_flows:
                        if normalize_workflow_asset_id(str(w)) == target_id:
                            is_ref = True
                            break
                # governance policies
                if not is_ref:
                    gov = space.get("governance_policies") or space.get("governance") or space.get("policies")
                    if gov:
                        gov_str = json.dumps(gov) if not isinstance(gov, str) else gov
                        if target_id in gov_str:
                            is_ref = True
                if is_ref:
                    blockers.append({"ref": str(space_id), "kind": "space", "reason_code": "REFERENCED_BY_SPACE"})

            # bindings
            bindings = catalog.get("bindings", {})
            if isinstance(bindings, dict):
                for binding_id, binding in bindings.items():
                    if not isinstance(binding, dict):
                        continue
                    if binding.get("state") == "unbound":
                        continue
                    b_asset = normalize_workflow_asset_id(binding.get("asset_id", ""))
                    b_ref = normalize_workflow_asset_id(binding.get("ref", ""))
                    if target_id in (b_asset, b_ref):
                        for sid in binding.get("space_ids", []):
                            sp = spaces.get(sid, {})
                            sp_state = (
                                sp.get("state")
                                if isinstance(sp, dict)
                                else getattr(sp, "state", "active")
                            )
                            if sp_state not in ("archived", "deleted"):
                                blockers.append(
                                    {"ref": str(sid), "kind": "space", "reason_code": "REFERENCED_BY_SPACE"}
                                )
            return True
        except (OSError, ValueError, KeyError):
            return False
        except Exception:
            return False

    def _check_runs(self, target_id: str, blockers: list[dict[str, str]]) -> bool:
        try:
            if self.owner_home is not None:
                runs_root = self.owner_home / "data" / "workflow_runs"
                index_path = self.owner_home / "data" / "workflow_run_index.sqlite3"
                found_run_ids = set()
                index_queried_ok = False
                if index_path.exists():
                    try:
                        from .run_list_index import RunListIndex

                        idx = RunListIndex(self.owner_home)
                        for st in _active_run_statuses:
                            for q_id in (target_id, f"workflow:{target_id}"):
                                for r_id, _ in idx.page(q_id, st, limit=10):
                                    found_run_ids.add(str(r_id))
                        index_queried_ok = True
                    except Exception:
                        index_queried_ok = False
                if not index_queried_ok and runs_root.exists() and runs_root.is_dir():
                    scanned = 0
                    max_scan = 100
                    for entry in runs_root.iterdir():
                        if scanned >= max_scan:
                            break
                        if not entry.is_dir() or entry.name.startswith("."):
                            continue
                        scanned += 1
                        if entry.name in found_run_ids:
                            continue
                        raw = read_text_beneath(self.owner_home, ("data", "workflow_runs", entry.name, "run.json"))
                        if raw is None:
                            continue
                        data = json.loads(raw)
                        wf_id = normalize_workflow_asset_id(data.get("workflow_id", ""))
                        st = data.get("status", "")
                        if wf_id == target_id and st in _active_run_statuses:
                            found_run_ids.add(entry.name)
                for r_id in sorted(found_run_ids):
                    blockers.append({"ref": str(r_id), "kind": "workflow_run", "reason_code": "ACTIVE_WORKFLOW_RUN"})
            elif self.run_store is not None:
                # Fallback for mock run_store
                for st in _active_run_statuses:
                    for q_id in (target_id, f"workflow:{target_id}"):
                        runs = self.run_store.list_runs(workflow_id=q_id, status=st, limit=10)
                        for r in runs:
                            run_id = getattr(r, "workflow_run_id", None) or getattr(r, "run_id", None) or str(r)
                            if run_id:
                                blockers.append(
                                    {"ref": str(run_id), "kind": "workflow_run", "reason_code": "ACTIVE_WORKFLOW_RUN"}
                                )
            return True
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
            page = task_store.list_page(limit=100)
            items = page.get("items", []) if isinstance(page, dict) else []
            for task in items:
                t_status = getattr(task, "status", "")
                if t_status not in _open_session_task_statuses:
                    continue
                t_id = getattr(task, "task_id", "") or str(task)
                dedupe_key = str(getattr(task, "dedupe_key", "") or "")
                metadata = getattr(task, "metadata", {}) or {}
                bc = metadata.get("business_context") if isinstance(metadata, dict) else {}

                is_ref = False
                if target_id in dedupe_key:
                    is_ref = True
                elif isinstance(metadata, dict):
                    meta_wf = normalize_workflow_asset_id(
                        str(metadata.get("workflow_id") or metadata.get("workflow_ref") or "")
                    )
                    bc_wf = (
                        normalize_workflow_asset_id(str(bc.get("workflow_id") or bc.get("workflow_ref") or ""))
                        if isinstance(bc, dict)
                        else ""
                    )
                    if target_id in (meta_wf, bc_wf):
                        is_ref = True
                    else:
                        try:
                            meta_str = json.dumps(metadata)
                            if target_id in meta_str:
                                is_ref = True
                        except Exception:
                            pass
                if is_ref and t_id:
                    blockers.append({"ref": str(t_id), "kind": "session_task", "reason_code": "OPEN_SESSION_TASK"})
            return True
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return False
        except Exception:
            return False

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if not args and not kwargs:
            return {"coverage": "complete", "blockers": []}
        if len(args) == 1 and isinstance(args[0], str):
            workflow_id = args[0]
        elif len(args) >= 2 and args[0] in ("workflow", "workflows"):
            workflow_id = args[1]
        elif "workflow_id" in kwargs:
            workflow_id = kwargs["workflow_id"]
        elif "ref_id" in kwargs:
            workflow_id = kwargs["ref_id"]
        elif "ref" in kwargs:
            workflow_id = kwargs["ref"]
        else:
            workflow_id = str(args[0])
        return self.read_references(workflow_id)


def build_workflow_reference_reader(
    host_or_owner_home: Any,
    *,
    workflow_store: Any = None,
    run_store: Any = None,
    space_store: Any = None,
    scheduler_repo: Any = None,
    session_task_store: Any = None,
) -> Callable[[str], dict[str, Any]]:
    """Factory creating authoritative WorkflowReferenceReader callable."""
    return WorkflowReferenceReader(
        host_or_owner_home,
        workflow_store=workflow_store,
        run_store=run_store,
        space_store=space_store,
        scheduler_repo=scheduler_repo,
        session_task_store=session_task_store,
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
        **kwargs: Any,
    ) -> None:
        if reference_reader is None:
            reference_reader = build_workflow_reference_reader(owner_home)
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
            **kwargs,
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
