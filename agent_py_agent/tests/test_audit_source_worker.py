from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.agent_core.runner.prompts import (
    _build_subagent_runner_prompt,
    subagent_runner_system_prompt,
)
from agent_py_agent.agent.agent_core.runner.timeout_policy import get_task_timeout
from agent_py_agent.agent.agent_core.runner.worker import (
    _reconcile_audit_source_binding_slice,
    _reconcile_audit_source_worker_slice,
)
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    _audit_source_worker_tool_scope_result,
)
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_DEADLINE_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_RUN_PROMPT_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_BINDING_TOOLS,
    AUDIT_SOURCE_BINDINGS_ATTR,
    AUDIT_SOURCE_CONFIG_VERSION_ATTR,
    AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OPEN_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_work_scope_key,
    audit_source_worker_key,
    audit_watch_scope_id,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.audit_requirements import (
    AUDIT_NOTES_MARKER,
    AUDIT_USER_MARKER,
    audit_runtime_requirement_for_task,
    canonical_audit_validated_notes,
    published_audit_requirement,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.workspace_paths import audit_workspace_path
from agent_py_agent.agent.ingestion import source_worker as source_worker_module
from agent_py_agent.agent.ingestion.source_binding import (
    audit_source_config_version,
    normalize_audit_source_bindings,
)
from agent_py_agent.agent.ingestion.source_worker import (
    _sync_source_worker_runtime_context,
    audit_parent_reconcile_state,
    audit_task_has_unreported_findings,
    authorize_source_worker_action,
    ensure_audit_source_worker,
    provision_published_audit_source_workers,
    reconcile_audit_capacity_alert,
    reconcile_audit_finding_outbox,
    reconcile_audit_source_workers,
    source_worker_facts,
    source_worker_lease_fence,
)
from agent_py_agent.agent.ingestion.watch_state import (
    load_state,
    new_state,
    persist_source_binding_revision,
    persist_state,
    state_dir,
    watch_id_for,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
from agent_py_agent.agent.subagents.models import (
    FailureType,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
    TaskStatus,
    VerificationStatus,
)
from agent_py_agent.agent.subagents.runner_result_state import (
    RunnerResultFieldParams,
    apply_runner_result_fields,
)


def _agent(tmp_path: Path, *, audit_id: str = "audit-source-test", max_subagents: int = 20):
    owner_home = tmp_path / "owner"
    manager = SubAgentManager(
        owner_home / "tasks" / audit_id / "work" / "agents",
        workspace_root=tmp_path,
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(
            enable_subagents=True,
            max_subagents=max_subagents,
            task_max_subagents=max_subagents,
            subagent_hierarchy_max_children_per_tool_call=max_subagents,
            access_mode="workspace-write",
            dispatch_supervision_reminder_seconds=0,
            lease_stale_without_heartbeat_seconds=300,
        ),
        home_paths=SimpleNamespace(
            owner_home_dir=str(owner_home),
            owner_id="owner-a",
        ),
        owner_policy=None,
        subagents=manager,
        conversation_store=None,
        tools=SimpleNamespace(specs=lambda: []),
        _current_run_params=SimpleNamespace(
            source="background_main_agent",
            task_id=audit_id,
            request_id=audit_id,
            task_attributes={
                AUDIT_ATTR: True,
                CONVERSATION_REQUEST_ID_ATTR: audit_id,
                "conversation_task_id": audit_id,
            },
        ),
    )

    def related(task_id: str) -> list[str]:
        return [
            task.id for task in manager.list_runs() if task_id in {task.parent_id, task.root_id}
        ]

    agent.subagent_run_ids_for_request = related
    return agent, owner_home


def _watch(owner_home: Path, audit_id: str, index: int, *, run_epoch: int = 0):
    url = f"http://source-{index}.example/events"
    state = new_state(
        owner_home,
        url,
        {"background_harvest": 0},
        watch_id=watch_id_for(
            owner_home,
            url,
            audit_watch_scope_id(audit_id, run_epoch),
        ),
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.audit_run_epoch = run_epoch
    state.audit_objective = "按用户给出的目标研判完整记录"
    state.source_task_goal = f"只负责来源 {url}，按现场已确认的调用方法持续研判"
    persist_state(state)
    return state


def _bind_audit_workspace(
    agent: object,
    owner_home: Path,
    audit_id: str,
    tmp_path: Path,
) -> Path:
    root = audit_workspace_path(owner_home, audit_id)
    root.mkdir(parents=True, exist_ok=True)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": f"{audit_id}-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
            "work_name": audit_id,
            "task_path": str(root),
        }
    )
    agent.conversation_store = store
    return root


def test_named_audit_keeps_watch_checkpoint_scope_but_rotates_worker_attempt_scope() -> None:
    audit_id = "audit-restarted"
    watch_scope_1 = audit_watch_scope_id(audit_id, 1)
    watch_scope_2 = audit_watch_scope_id(audit_id, 2)
    worker_key = audit_source_worker_key(audit_id, "ws-stable")

    assert watch_scope_1 == audit_id
    assert watch_scope_2 == audit_id
    assert audit_source_work_scope_key(worker_key, 1) != audit_source_work_scope_key(
        worker_key,
        2,
    )


def test_active_named_audit_lineage_reports_only_current_run_epoch(tmp_path: Path) -> None:
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    audit_id = "audit-current-epoch-lineage"
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "current-epoch-lineage",
            "channel_user_id": "owner-a",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
            "work_name": "current-epoch-lineage",
            "run_epoch": 2,
        }
    )
    old = agent.subagents.create_run(
        goal="旧轮次来源",
        parent_id=audit_id,
        root_id=audit_id,
        attributes={
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_ATTR: True,
            AUDIT_RUN_EPOCH_ATTR: 1,
        },
    )
    current = agent.subagents.create_run(
        goal="当前轮次来源",
        parent_id=audit_id,
        root_id=audit_id,
        attributes={
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_ATTR: True,
            AUDIT_RUN_EPOCH_ATTR: 2,
        },
    )
    agent.subagents.create_run(
        goal="无关任务",
        parent_id="other-task",
        root_id="other-task",
        attributes={CONVERSATION_REQUEST_ID_ATTR: "other-task"},
    )

    assert agent.subagent_run_ids_for_request(audit_id) == [current.id]
    assert old.id not in agent.subagent_run_ids_for_request(audit_id)


def test_expired_active_parent_remains_authorized_while_durable_backlog_drains(
    monkeypatch,
) -> None:
    link = SimpleNamespace(
        task_id="audit-expired-parent",
        status="active",
        work_kind="audit",
        expires_at=99.0,
    )
    store = SimpleNamespace(
        thread_for_task=lambda _task_id: SimpleNamespace(thread_id="thread-expired"),
        task_links=lambda _thread_id: [link],
    )
    agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr(source_worker_module.time, "time", lambda: 100.0)

    assert audit_parent_reconcile_state(agent, link.task_id) == (True, "active")


def test_direct_audit_leaf_is_least_privilege_before_first_runner_turn(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        prepare_audit_child_creation_scope,
    )

    audit_id = "audit-prebind-create"
    agent, _owner_home = _agent(tmp_path, audit_id=audit_id)
    scoped, error = prepare_audit_child_creation_scope(
        agent,
        {
            "goal": "持续研判一个来源",
            "role": "worker",
            "allowed_tools": [
                "watch_stream",
                "run_command",
                "write_file",
                "web_fetch",
            ],
            "allowed_skills": ["coding"],
            "context_packs": [{"kind": "parent_recent_read", "path": "private.md"}],
            "extra_write_roots": [str(tmp_path)],
            "acceptance_checks": ["write report"],
            "output_refs": ["output/report.md"],
            "attributes": {
                AUDIT_ATTR: True,
                "output_files": ["output/report.md"],
                "skill_snapshot_refs": ["skill://coding"],
            },
        },
    )

    assert error == ""
    assert scoped["allowed_tools"] == list(AUDIT_SOURCE_BINDING_TOOLS)
    assert scoped["allowed_skills"] == []
    assert scoped["context_packs"] == []
    assert scoped["extra_write_roots"] == []
    assert scoped["acceptance_checks"] == []
    assert scoped["role"] == "worker"
    assert scoped["long_running"] is True
    assert scoped["_exact_allowed_tools"] is True
    assert "output_refs" not in scoped
    attrs = scoped["attributes"]
    assert isinstance(attrs, dict)
    assert attrs[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True
    assert attrs[CONVERSATION_REQUEST_ID_ATTR] == audit_id
    assert attrs["dynamic_timeout_seconds"] == 300
    assert "output_files" not in attrs
    assert "skill_snapshot_refs" not in attrs


def test_audit_coordinator_keeps_ordinary_delegate_scope(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        prepare_audit_child_creation_scope,
    )

    agent, _owner_home = _agent(tmp_path, audit_id="audit-coordinator-scope")
    original = {
        "goal": "协调多个来源工作者并汇总结构化发现",
        "role": "coordinator",
        "allowed_tools": ["create_subagents", "inspect_agent_tree"],
    }

    scoped, error = prepare_audit_child_creation_scope(agent, original)

    assert error == ""
    assert scoped is original
    assert AUDIT_SOURCE_BINDING_PENDING_ATTR not in dict(scoped.get("attributes") or {})


@pytest.mark.parametrize("source_count", [1, 3, 5, 10])
def test_published_sources_map_to_one_idempotent_leaf_each(
    tmp_path: Path,
    source_count: int,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolve_create_run,
    )
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        create_run_params,
        prepare_audit_child_creation_scope,
    )

    audit_id = f"audit-published-bindings-{source_count}"
    agent, _owner_home = _agent(
        tmp_path,
        audit_id=audit_id,
        max_subagents=source_count + 2,
    )
    bindings = [
        {
            "source_id": f"source-{index}",
            "url": f"https://source-{index}.example.invalid/events",
            "mode": "cursor",
            "http_request": {
                "method": "GET",
                "cursor_binding": {
                    "location": "query",
                    "name": "position",
                    "initial": 0,
                },
            },
            "source_profile_ref": str(tmp_path / "profiles" / f"source-{index}.md"),
        }
        for index in range(source_count)
    ]
    for binding in bindings:
        profile = Path(str(binding["source_profile_ref"]))
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(
            f"source_id: {binding['source_id']}\n",
            encoding="utf-8",
        )
    agent._current_run_params.task_attributes[AUDIT_SOURCE_BINDINGS_ATTR] = bindings
    first_ids: list[str] = []
    work_scopes: set[str] = set()

    for index, binding in enumerate(bindings):
        raw = {
            "goal": f"持续研判来源 {binding['source_id']}",
            "role": "worker",
            "audit_source_id": binding["source_id"],
        }
        scoped, error = prepare_audit_child_creation_scope(agent, raw)
        assert error == ""
        attrs = dict(scoped["attributes"])
        assert attrs[AUDIT_SOURCE_ID_ATTR] == binding["source_id"]
        assert attrs[AUDIT_SOURCE_OPEN_ATTR] == binding
        assert attrs["work_scope_key"] not in work_scopes
        work_scopes.add(str(attrs["work_scope_key"]))

        run_params = create_run_params(
            agent,
            scoped,
            str(scoped["goal"]),
            list(scoped["allowed_tools"]),
        )
        first = resolve_create_run(agent.subagents, run_params)
        second = resolve_create_run(agent.subagents, run_params)
        assert first.reused is False
        assert second.reused is True
        assert second.task.id == first.task.id
        first_ids.append(first.task.id)

    assert len(agent.subagents.list_runs()) == source_count
    assert len(set(first_ids)) == source_count


def test_concurrent_work_scope_creation_is_one_logical_run(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolve_create_run,
    )
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, _owner_home = _agent(tmp_path, audit_id="audit-create-race")
    params = CreateRunParams(
        goal="one durable source worker",
        thought="typed create race",
        plan=["work"],
        parent_id="audit-create-race",
        root_id="audit-create-race",
        role="worker",
        attributes={"work_scope_key": "audit-source-scope:race"},
    )
    barrier = Barrier(8)

    def create_once():
        barrier.wait()
        return resolve_create_run(agent.subagents, params)

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolutions = list(pool.map(lambda _index: create_once(), range(8)))

    assert len(agent.subagents.list_runs()) == 1
    assert len({item.task.id for item in resolutions}) == 1
    assert sum(not item.reused for item in resolutions) == 1
    assert sum(item.reused for item in resolutions) == 7


def test_reconcile_retires_duplicate_active_source_workers(tmp_path: Path) -> None:
    from agent_py_agent.agent.ingestion.source_worker import (
        _canonical_worker_task,
    )

    audit_id = "audit-duplicate-workers"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    first = agent.subagents.load(str(created["run_id"]))
    duplicates = [first]
    for index in range(2):
        duplicate = agent.subagents.create_run(
            goal=first.goal,
            thought=f"legacy race {index}",
            plan=list(first.plan),
            agent_name=first.agent_name,
            role=first.role,
            parent_id=first.parent_id,
            root_id=first.root_id,
            allowed_tools=list(first.allowed_tools),
            attributes=dict(first.attributes),
        )
        duplicate.status = TaskStatus.RUNNING.value
        duplicate.last_progress_at = time.time() + index
        agent.subagents.save(duplicate)
        duplicates.append(duplicate)

    canonical, retired = _canonical_worker_task(
        agent,
        str(first.attributes[AUDIT_SOURCE_WORKER_KEY_ATTR]),
        retire_duplicates=True,
    )

    persisted = [agent.subagents.load(task.id) for task in duplicates]
    active = [
        task
        for task in persisted
        if task.status
        in {
            TaskStatus.PLANNING.value,
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.PAUSED.value,
        }
    ]
    assert canonical is not None
    assert retired == 2
    assert [task.id for task in active] == [canonical.id]
    assert sum(task.status == TaskStatus.CANCELLED.value for task in persisted) == 2


@pytest.mark.parametrize("source_count", [1, 3, 5, 10])
def test_root_runtime_provisions_each_published_source_through_create_subagents(
    tmp_path: Path,
    monkeypatch,
    source_count: int,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = f"audit-host-provision-{source_count}"
    agent, _owner_home = _agent(
        tmp_path,
        audit_id=audit_id,
        max_subagents=source_count + 2,
    )
    bindings: list[dict[str, object]] = []
    for index in range(source_count):
        profile = tmp_path / "audit" / f"source-{index}.md"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(f"source_id: source-{index}\n", encoding="utf-8")
        bindings.append(
            {
                "source_id": f"source-{index}",
                "url": f"https://source-{index}.example.invalid/events",
                "mode": "cursor",
                "http_request": {
                    "method": "GET",
                    "cursor_binding": {
                        "location": "query",
                        "name": "position",
                        "initial": 0,
                    },
                },
                "source_profile_ref": str(profile),
            }
        )
    agent._current_run_params.task_attributes[AUDIT_SOURCE_BINDINGS_ATTR] = bindings

    first = provision_published_audit_source_workers(agent)
    second = provision_published_audit_source_workers(agent)
    tasks = agent.subagents.list_runs()

    assert first["ok"] is True
    assert first["required"] == source_count
    assert first["ready"] == source_count
    assert {item["state"] for item in first["sources"]} == {"created"}
    assert second["ok"] is True
    assert second["ready"] == source_count
    assert {item["state"] for item in second["sources"]} == {"reused"}
    assert len(tasks) == source_count
    assert {task.attributes[AUDIT_SOURCE_ID_ATTR] for task in tasks} == {
        f"source-{index}" for index in range(source_count)
    }
    assert all(task.attributes[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True for task in tasks)
    assert all(task.allowed_tools == list(AUDIT_SOURCE_BINDING_TOOLS) for task in tasks)
    task_by_source = {str(task.attributes[AUDIT_SOURCE_ID_ATTR]): task for task in tasks}
    assert all(
        task_by_source[f"source-{index}"].context_manifest.required_read_paths
        == [str(tmp_path / "audit" / f"source-{index}.md")]
        for index in range(source_count)
    )


def test_next_audit_epoch_gets_a_fresh_runner_task_for_the_same_logical_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-worker-next-epoch"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    profile = tmp_path / "audit" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    binding = {
        "source_id": "source-a",
        "url": "https://source-a.example.invalid/events",
        "mode": "cursor",
        "http_request": {
            "method": "GET",
            "cursor_binding": {
                "location": "query",
                "name": "position",
                "initial": 0,
            },
        },
        "source_profile_ref": str(profile),
    }
    attrs = agent._current_run_params.task_attributes
    attrs[AUDIT_SOURCE_BINDINGS_ATTR] = [binding]
    attrs[AUDIT_RUN_EPOCH_ATTR] = 1
    first = provision_published_audit_source_workers(agent)
    assert first["sources"], first
    first_task = agent.subagents.load(str(first["sources"][0]["run_id"]))
    first_scope = str(first_task.attributes.get("work_scope_key") or "")
    first_task.status = TaskStatus.DONE.value
    first_task.verification_status = VerificationStatus.VERIFIED.value
    agent.subagents.save(first_task)

    attrs[AUDIT_RUN_EPOCH_ATTR] = 2
    second = provision_published_audit_source_workers(agent)
    second_task = agent.subagents.load(str(second["sources"][0]["run_id"]))
    stable_watch = watch_id_for(
        owner_home,
        str(binding["url"]),
        audit_watch_scope_id(audit_id, 2),
    )

    assert first["sources"][0]["state"] == "created"
    assert second["sources"][0]["state"] == "created"
    assert second_task.id != first_task.id
    assert str(second_task.attributes.get("work_scope_key") or "") != first_scope
    assert audit_source_worker_key(audit_id, stable_watch) == audit_source_worker_key(
        audit_id,
        watch_id_for(owner_home, str(binding["url"]), audit_watch_scope_id(audit_id, 1)),
    )


def test_published_source_leaf_requires_an_exact_known_source_id(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        prepare_audit_child_creation_scope,
    )

    agent, _owner_home = _agent(tmp_path, audit_id="audit-exact-source-id")
    profile = tmp_path / "profiles" / "known-source.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: known-source\n", encoding="utf-8")
    agent._current_run_params.task_attributes[AUDIT_SOURCE_BINDINGS_ATTR] = [
        {
            "source_id": "known-source",
            "url": "https://known.example.invalid/events",
            "mode": "cursor",
            "http_request": {
                "method": "GET",
                "cursor_binding": {
                    "location": "query",
                    "name": "cursor",
                    "initial": 0,
                },
            },
            "source_profile_ref": str(profile),
        }
    ]

    _missing, missing_error = prepare_audit_child_creation_scope(
        agent,
        {"goal": "缺少来源编号", "role": "worker"},
    )
    _unknown, unknown_error = prepare_audit_child_creation_scope(
        agent,
        {
            "goal": "使用未知来源编号",
            "role": "worker",
            "audit_source_id": "unknown-source",
        },
    )

    assert "必须用 audit_source_id" in missing_error
    assert "不属于当前 Audit" in unknown_error
    assert agent.subagents.list_runs() == []


@pytest.mark.parametrize("source_count", [1, 3, 5, 7, 9, 10, 13])
def test_one_exact_worker_per_watch_is_idempotent(
    tmp_path: Path,
    monkeypatch,
    source_count: int,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = f"audit-cardinality-{source_count}"
    agent, owner_home = _agent(
        tmp_path,
        audit_id=audit_id,
        max_subagents=source_count + 2,
    )
    states = [_watch(owner_home, audit_id, index) for index in range(source_count)]

    first = [ensure_audit_source_worker(agent, state) for state in states]
    second = [ensure_audit_source_worker(agent, state) for state in states]
    tasks = agent.subagents.list_runs()

    assert all(row["ok"] is True for row in first)
    assert all(row["ok"] is True for row in second)
    assert len(tasks) == source_count
    assert len({task.attributes[AUDIT_SOURCE_WORKER_KEY_ATTR] for task in tasks}) == source_count
    assert all(task.allowed_tools == ["watch_stream", "record_finding"] for task in tasks)
    assert all("schedule_child_subagents" not in task.allowed_tools for task in tasks)
    assert all(task.attributes.get("_exact_allowed_tools") is None for task in tasks)
    assert all(task.attributes["dynamic_timeout_seconds"] == 300 for task in tasks)
    assert all(get_task_timeout(task, 0.0, agent.config) == 300 for task in tasks)

    tasks_by_watch = {str(task.attributes[AUDIT_SOURCE_WATCH_ID_ATTR]): task for task in tasks}
    leases: list[dict[str, object]] = []
    for index, state in enumerate(states):
        task = tasks_by_watch[state.watch_id]
        task.status = TaskStatus.RUNNING.value
        task.runner_active_attempt_id = f"attempt-{index}"
        agent.subagents.save(task)
        previous = set_current_subagent_context(
            agent,
            run_id=task.id,
            attempt_id=task.runner_active_attempt_id,
            task_attributes=task.attributes,
        )
        try:
            leases.append(authorize_source_worker_action(agent, state, now=100.0))
        finally:
            restore_current_subagent_context(agent, previous)

    assert all(lease["ok"] is True for lease in leases)
    assert len({str(lease["run_id"]) for lease in leases}) == source_count
    assert len(list(state_dir(owner_home).glob("*.worker-lease.json"))) == source_count


def test_capacity_shortage_is_visible_and_does_not_share_a_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-capacity"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id, max_subagents=2)
    states = [_watch(owner_home, audit_id, index) for index in range(5)]

    rows = [ensure_audit_source_worker(agent, state) for state in states]

    assert len(agent.subagents.list_runs()) == 2
    assert [row["ok"] for row in rows] == [True, True, False, False, False]
    assert all(row["state"] == "waiting_for_capacity" for row in rows[2:])


def test_expired_settled_watch_does_not_create_worker(tmp_path: Path) -> None:
    audit_id = "audit-expired-settled"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.opened_at = 1.0
    state.watch_window_seconds = 1
    state.window_finalized_at = 2.0
    persist_state(state)

    result = ensure_audit_source_worker(agent, state)

    assert result["ok"] is True
    assert result["state"] == "complete"
    assert result["created"] is False
    assert agent.subagents.list_runs() == []


def test_expired_unfinalized_window_keeps_worker_recoverable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-expired-needs-final-read"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.opened_at = 1.0
    state.watch_window_seconds = 1
    state.window_finalized_at = 0.0
    persist_state(state)

    result = ensure_audit_source_worker(agent, state)

    assert result["ok"] is True
    assert result["state"] == "waiting_for_worker"
    assert len(agent.subagents.list_runs()) == 1


def test_missing_source_assignment_fails_closed_before_worker_creation(
    tmp_path: Path,
) -> None:
    audit_id = "audit-missing-source-assignment"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    url = "http://missing-source-goal.example/events"
    state = new_state(
        owner_home,
        url,
        {"background_harvest": 0},
        watch_id=watch_id_for(owner_home, url, audit_id),
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.audit_objective = "持续研判已确认来源"
    persist_state(state)

    result = ensure_audit_source_worker(agent, state)

    assert result["ok"] is False
    assert result["state"] == "degraded"
    assert result["error_code"] == "AUDIT_SOURCE_TASK_CONTEXT_UNAVAILABLE"
    assert agent.subagents.list_runs() == []


def test_source_assignment_survives_reload_and_is_used_by_replacement_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-assignment-reload"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    original = _watch(owner_home, audit_id, 1)

    reloaded = load_state(owner_home, original.watch_id)
    assert reloaded is not None
    assert reloaded.source_task_goal == original.source_task_goal

    created = ensure_audit_source_worker(agent, reloaded)
    task = agent.subagents.load(str(created["run_id"]))

    assert original.source_task_goal in task.goal
    assert original.audit_objective not in original.source_task_goal
    assert created["worker_key"] == audit_source_worker_key(
        audit_id,
        original.watch_id,
    )


def test_existing_audit_child_is_adopted_as_the_exact_source_worker(
    tmp_path: Path,
) -> None:
    audit_id = "audit-adopt-child"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.audit_run_prompt = "本轮要求必须在绑定后的新工作片执行。"
    persist_state(state)
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        "conversation_task_id": audit_id,
        "output_files": ["work/child_outputs/setup.md"],
        "system_default_output_ref": True,
    }
    task = agent.subagents.create_run(
        goal="consume one source",
        thought="open then monitor",
        plan=["open", "consume"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=list(AUDIT_SOURCE_BINDING_TOOLS),
        attributes=attrs,
    )
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-adopt"
    task.context_packs = [
        {
            "kind": "parent_recent_read",
            "path": "docs/sources/other-source.md",
            "summary": "另一个来源的说明不应进入本来源工作者上下文",
        }
    ]
    task.context_manifest.hint_read_paths = ["docs/sources/other-source.md"]
    agent.subagents.save(task)
    runtime_attrs = dict(attrs)
    previous = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-adopt",
        task_attributes=runtime_attrs,
    )
    try:
        result = ensure_audit_source_worker(agent, state)
        from agent_py_agent.agent.ingestion.watch_tool import (
            _source_worker_forbidden_action,
        )

        assert runtime_attrs[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True
        assert AUDIT_SOURCE_WATCH_ID_ATTR not in runtime_attrs
        assert _source_worker_forbidden_action(agent, "pull") is True
        assert _source_worker_forbidden_action(agent, "verdict") is True
    finally:
        restore_current_subagent_context(agent, previous)

    persisted = agent.subagents.load(task.id)
    assert result["adopted"] is True
    assert result["context_refresh_required"] is True
    assert result["run_id"] == task.id
    assert len(agent.subagents.list_runs()) == 1
    assert persisted.attributes[AUDIT_SOURCE_WORKER_ATTR] is True
    assert persisted.attributes[AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR] == "attempt-adopt"
    assert AUDIT_SOURCE_BINDING_PENDING_ATTR not in persisted.attributes
    assert persisted.attributes[AUDIT_SOURCE_WORKER_KEY_ATTR] == audit_source_worker_key(
        audit_id,
        state.watch_id,
    )
    assert persisted.allowed_tools == [
        "watch_stream",
        "record_finding",
    ]
    assert "write_file" not in persisted.allowed_tools
    assert "run_command" not in persisted.allowed_tools
    assert "output_files" not in persisted.attributes
    assert persisted.context_packs == []
    assert persisted.context_manifest.hint_read_paths == []
    refreshed = agent.subagents.runner_context.build_execution_context(task.id)
    runtime_profile = refreshed.context_bundle["runtime_profile"]
    assert runtime_profile["kind"] == "audit_source_worker"
    assert runtime_profile[AUDIT_RUN_PROMPT_ATTR] == state.audit_run_prompt


def test_source_binding_transition_does_not_publish_stale_opening_turn_summary(
    tmp_path: Path,
) -> None:
    audit_id = "audit-binding-transition-summary"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    task = SubAgentTask(
        id="worker-binding-transition",
        goal="open source",
        thought="bind then refresh",
        plan=["open"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        runner_active_attempt_id="attempt-opening",
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                state.watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
            AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR: "attempt-opening",
        },
    )
    result_meta = {
        "ok": True,
        "message": "",
        "response": "旧工具范围下误以为需要协调者代替消费",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(found=True, ok=True, status="DONE"),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.PENDING.value
    assert task.result == ""
    assert AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR not in task.attributes


def test_source_binding_transition_keeps_later_recovery_attempt_summary(
    tmp_path: Path,
) -> None:
    audit_id = "audit-binding-transition-recovery"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    task = SubAgentTask(
        id="worker-binding-recovery",
        goal="consume source",
        thought="resume bound worker",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        runner_active_attempt_id="attempt-recovery",
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                state.watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
            AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR: "attempt-opening",
        },
    )
    result_meta = {
        "ok": True,
        "message": "",
        "response": "恢复轮已经处理一批记录",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(found=True, ok=True, status="DONE"),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.PENDING.value
    assert task.result == "恢复轮已经处理一批记录"
    assert AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR not in task.attributes


def test_generic_audit_child_is_not_adopted_as_a_source_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-generic-child-not-adopted"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        "conversation_task_id": audit_id,
    }
    generic = agent.subagents.create_run(
        goal="ordinary investigation",
        thought="inspect",
        plan=["inspect"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=["run_command", "write_file"],
        attributes=attrs,
    )
    generic.status = TaskStatus.RUNNING.value
    generic.runner_active_attempt_id = "attempt-generic"
    agent.subagents.save(generic)
    previous = set_current_subagent_context(
        agent,
        run_id=generic.id,
        attempt_id="attempt-generic",
        task_attributes=dict(attrs),
    )
    try:
        result = ensure_audit_source_worker(agent, state)
    finally:
        restore_current_subagent_context(agent, previous)

    assert result["created"] is True
    assert result.get("adopted") is not True
    assert result["run_id"] != generic.id
    assert len(agent.subagents.list_runs()) == 2
    assert agent.subagents.load(generic.id).attributes.get(AUDIT_SOURCE_WORKER_ATTR) is not True


def test_pending_source_binding_scope_denies_broad_tools_before_first_open(
    tmp_path: Path,
) -> None:
    audit_id = "audit-binding-tool-gate"
    agent, _owner_home = _agent(tmp_path, audit_id=audit_id)
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
    }
    previous = set_current_subagent_context(
        agent,
        run_id="worker-binding",
        attempt_id="attempt-binding",
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.ingestion.watch_tool import (
            _source_worker_forbidden_action,
        )

        assert (
            _audit_source_worker_tool_scope_result(
                agent,
                {"tool": "watch_stream"},
            )
            is None
        )
        denied = _audit_source_worker_tool_scope_result(
            agent,
            {"tool": "run_command"},
        )
        assert _source_worker_forbidden_action(agent, "open") is False
        assert _source_worker_forbidden_action(agent, "sample") is False
        assert _source_worker_forbidden_action(agent, "pull") is True
        assert _source_worker_forbidden_action(agent, "verdict") is True
        assert _source_worker_forbidden_action(agent, "close") is True
    finally:
        restore_current_subagent_context(agent, previous)

    assert denied is not None
    assert denied.error_code == "TOOL_NOT_ALLOWED"


def test_adopted_source_worker_runtime_gate_denies_the_old_broad_tools(
    tmp_path: Path,
) -> None:
    audit_id = "audit-adopt-tool-gate"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            audit_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
    }
    previous = set_current_subagent_context(
        _agent_value,
        run_id="worker-adopted",
        attempt_id="attempt-a",
        task_attributes=attrs,
    )
    try:
        assert (
            _audit_source_worker_tool_scope_result(
                _agent_value,
                {"tool": "watch_stream"},
            )
            is None
        )
        denied = _audit_source_worker_tool_scope_result(
            _agent_value,
            {"tool": "raise_event"},
        )
    finally:
        restore_current_subagent_context(_agent_value, previous)
    assert denied is not None
    assert denied.error_code == "TOOL_NOT_ALLOWED"


def test_restart_reconcile_skips_terminal_named_audit_with_pending_watch(
    tmp_path: Path,
) -> None:
    audit_id = "audit-terminal-parent"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    _watch(owner_home, audit_id, 1)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "terminal-audit-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "completed",
            "work_kind": "audit",
            "work_name": "terminal-audit",
        }
    )
    agent.conversation_store = store

    summary = reconcile_audit_source_workers(agent)

    assert summary["checked"] == 1
    assert summary["skipped"] == 1
    assert summary["degraded"] == 0
    assert summary["workers"][0]["state"] == "not_required"
    assert summary["workers"][0]["parent_state"] == "inactive"
    assert agent.subagents.list_runs() == []
    persisted = load_state(owner_home, summary["workers"][0]["watch_id"])
    assert persisted is not None
    assert persisted.closed is True
    assert persisted.close_reason == "audit_parent_inactive"


def test_terminal_parent_closes_watch_and_cancels_existing_source_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-terminal-parent-with-worker"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    run_id = str(created["run_id"])
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "terminal-worker-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "completed",
            "work_kind": "audit",
            "work_name": "terminal-with-worker",
        }
    )
    agent.conversation_store = store

    summary = reconcile_audit_source_workers(agent)
    persisted_watch = load_state(owner_home, state.watch_id)
    task = agent.subagents.load(run_id)

    assert summary["workers"][0]["closed"] is True
    assert summary["workers"][0]["worker_cancelled"] is True
    assert persisted_watch is not None and persisted_watch.closed is True
    assert persisted_watch.close_reason == "audit_parent_inactive"
    assert task.status == TaskStatus.CANCELLED.value
    assert task.failure_type == FailureType.CANCELLED.value


def test_restart_reconcile_creates_worker_for_exact_active_named_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-active-parent"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    _watch(owner_home, audit_id, 1)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "active-audit-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
            "work_name": "active-audit",
        }
    )
    agent.conversation_store = store

    summary = reconcile_audit_source_workers(agent)

    assert summary["checked"] == 1
    assert summary["waiting"] == 1
    assert summary.get("skipped", 0) == 0
    assert len(agent.subagents.list_runs()) == 1


def test_restart_reconcile_keeps_replacement_in_parent_audit_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-active-parent-workspace"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    _watch(owner_home, audit_id, 1)
    task_root = audit_workspace_path(owner_home, audit_id)
    task_root.mkdir(parents=True)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "active-audit-workspace-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
            "work_name": "active-audit-workspace",
            "task_path": str(task_root),
        }
    )
    agent.conversation_store = store

    summary = reconcile_audit_source_workers(agent)

    assert summary["waiting"] == 1
    task = agent.subagents.list_runs()[0]
    assert task.attributes["run_workspace"] == {
        "task_root": str(task_root),
        "work_dir": str(task_root / "work"),
        "output_dir": str(task_root / "output"),
    }
    assert task.task_workspace_dir == str(task_root)
    assert task.agent_run_workspace_dir == str(task_root / "work" / "agents" / task.id)


def test_restart_reconcile_migrates_idle_legacy_worker_to_parent_audit_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-migrate-legacy-worker"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    first = ensure_audit_source_worker(agent, state)
    original = agent.subagents.load(str(first["run_id"]))
    original_workspace = original.task_workspace_dir
    task_root = audit_workspace_path(owner_home, audit_id)
    task_root.mkdir(parents=True)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "legacy-worker-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
            "work_name": "legacy-worker",
            "task_path": str(task_root),
        }
    )
    agent.conversation_store = store

    summary = reconcile_audit_source_workers(agent)
    migrated = agent.subagents.load(original.id)

    assert summary["waiting"] == 1
    assert len(agent.subagents.list_runs()) == 1
    assert migrated.id == original.id
    assert migrated.task_workspace_dir != original_workspace
    assert migrated.attributes["run_workspace"]["task_root"] == str(task_root)
    assert migrated.task_workspace_dir == str(task_root)
    assert migrated.agent_run_workspace_dir == str(task_root / "work" / "agents" / migrated.id)


def test_worker_created_during_named_clear_is_immediately_cancelled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    audit_id = "audit-create-clear-race"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)

    def close_while_starting(_agent, _tasks, _params):
        state.closed = True
        persist_state(state)
        return {"status": "not_needed", "run_ids": []}

    monkeypatch.setattr(lifecycle, "auto_start_tasks", close_while_starting)

    result = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(result["run_id"]))

    assert result["state"] == "cancelled"
    assert result["error_code"] == "AUDIT_SOURCE_WATCH_CLOSED"
    assert task.status == TaskStatus.CANCELLED.value
    assert task.failure_type == FailureType.CANCELLED.value
    assert task.attributes["cancel_subagents"]["reason"] == (
        "audit_watch_closed_during_worker_create"
    )


def test_source_worker_context_uses_exact_read_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-read-scope"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.source_profile_ref = "docs/sources/waf.md#v3"
    state.document_refs = [
        "docs/sources/waf-fields.xlsx#sha256:abc",
        "artifact://source-profile/opaque",
    ]
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    boundary = agent.subagents.runner_context._build_write_boundary(task)

    assert task.allowed_tools == [
        "watch_stream",
        "record_finding",
        "read_file",
        "read_artifact",
    ]
    assert task.context_manifest.required_read_paths == [
        "docs/sources/waf.md",
        "docs/sources/waf-fields.xlsx",
    ]
    assert boundary["read_scope_mode"] == "exact"
    assert boundary["artifact_read_scope_mode"] == "current_run"
    assert boundary["artifact_read_root"] == str(task.agent_run_workspace_dir)
    assert str(task.agent_run_workspace_dir) in boundary["allowed_read_roots"]
    assert "docs/sources/waf.md" in boundary["allowed_read_roots"]
    assert "artifact://source-profile/opaque" not in boundary["allowed_read_roots"]


def test_new_source_worker_does_not_inherit_sibling_goal_file_hints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-no-sibling-hints"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    own_ref = tmp_path / "docs" / "sources" / "auth.md"
    sibling_ref = tmp_path / "docs" / "sources" / "web.md"
    own_ref.parent.mkdir(parents=True, exist_ok=True)
    own_ref.write_text("auth fields", encoding="utf-8")
    sibling_ref.write_text("web fields", encoding="utf-8")
    state.source_profile_ref = str(own_ref)
    state.audit_objective = f"监控认证来源，另一个独立来源说明位于 {sibling_ref}，不要混合处理。"
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))

    assert task.context_manifest.required_read_paths == [str(own_ref)]
    assert task.context_manifest.hint_read_paths == []
    boundary = agent.subagents.runner_context._build_write_boundary(task)
    assert str(own_ref) in boundary["allowed_read_roots"]
    assert str(sibling_ref) not in boundary["allowed_read_roots"]


def test_source_worker_uses_focused_shared_runner_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-focused-prompt"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    audit_root = _bind_audit_workspace(agent, owner_home, audit_id, tmp_path)
    state = _watch(owner_home, audit_id, 1)
    source_profile = audit_root / "work" / "sources" / "waf.md"
    source_profile.parent.mkdir(parents=True, exist_ok=True)
    source_profile_text = "source_id=source-1\n只按本来源字段判断，兄弟来源不可见。"
    source_profile.write_text(source_profile_text, encoding="utf-8")
    state.source_profile_ref = f"{source_profile}#v3"
    state.document_refs = ["docs/sources/waf-fields.xlsx#sha256:abc"]
    state.audit_run_prompt = "本轮先故意提交 unsure，再用 review 更正为 hit。"
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    context = agent.subagents.runner_context.build_execution_context(str(created["run_id"]))
    prompt = _build_subagent_runner_prompt(context)
    system_prompt = subagent_runner_system_prompt(context)

    runtime_profile = context.context_bundle["runtime_profile"]
    assert runtime_profile["kind"] == "audit_source_worker"
    assert AUDIT_OBJECTIVE_ATTR not in runtime_profile
    assert runtime_profile["audit_run_prompt"] == state.audit_run_prompt
    assert runtime_profile["source_id"] == state.source_id
    assert runtime_profile["watch_id"] == state.watch_id
    assert runtime_profile["worker_key"] == audit_source_worker_key(audit_id, state.watch_id)
    assert runtime_profile["source_profile_ref"] == f"{source_profile}#v3"
    assert runtime_profile["document_refs"] == ["docs/sources/waf-fields.xlsx#sha256:abc"]
    assert runtime_profile["source_config_version"] == state.source_config_version
    assert runtime_profile["source_profile"]["inline"] is True
    assert runtime_profile["source_profile"]["complete"] is True
    assert runtime_profile["source_profile"]["text"] == source_profile_text
    assert len(runtime_profile["source_profile"]["sha256"]) == 64
    assert "# Audit Source Worker Turn" in prompt
    assert str(source_profile) in prompt
    assert prompt.count(state.audit_objective) == 0
    assert prompt.count(json.dumps(source_profile_text, ensure_ascii=False)) == 1
    assert prompt.count(state.audit_run_prompt) == 1
    assert prompt.count(state.source_task_goal) == 1
    assert "legacy sample values" not in prompt
    assert prompt.count(json.dumps(context.goal, ensure_ascii=False)) == 1
    assert "checkpoint.json" not in prompt
    assert "summary.md" not in prompt
    assert "recovery_refs" not in prompt
    assert "runner_instruction" not in prompt
    assert "长 CSS/JS/HTML" not in prompt
    assert "evidence_packets" not in prompt
    assert "capability_request" not in prompt
    assert len(prompt) < 8_000
    assert "专属 Audit 来源工作者" in system_prompt
    assert "watch_stream(action=pull)" in prompt
    assert "不要为了重复确认机械状态先调用 status" in prompt
    assert "不要在每个工作片机械重读" in prompt
    assert "watch_stream pull.batch_context" in prompt
    assert "watch_stream status.batch_context" not in prompt
    assert "通常省略 target_records" in prompt
    assert '"default_target_records": "omit"' in prompt
    assert "习惯整数当默认批量" in prompt
    assert "verdict_token" in prompt
    assert "不接受批量默认值" in prompt
    assert "遗漏行保持待判" in prompt
    assert "只返回这些欠账" in prompt
    assert "delivery_ref 必须作为" in prompt
    assert "绝不能放进 verdicts" in prompt
    assert "数组顺序本身不作为身份" in prompt
    assert "当前批首次判断不要复制 ack_id" in prompt
    assert "三项身份" not in prompt
    assert "建系统/写代码类交付" not in system_prompt


def test_source_worker_projects_operational_notes_without_sibling_prepare_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-bounded-objective"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    audit_root = _bind_audit_workspace(agent, owner_home, audit_id, tmp_path)
    state = _watch(owner_home, audit_id, 1)
    profile = audit_root / "work" / "sources" / "current.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id=source-1\n当前来源规则：只检查 verified。", encoding="utf-8")
    state.source_profile_ref = str(profile)
    state.audit_objective = published_audit_requirement(
        validated_notes="当前全局要求：逐条研判并保留引用。",
        user_prepare_history=(
            "第一路来源的原始要求。\n\n--- 后续 prepare ---\n\n"
            "绝不能发给当前工作者的兄弟来源原始要求。"
        ),
    )
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    # Simulate a worker persisted with an older full host wrapper.  The
    # final context projection must still stay bounded without rewriting the
    # historical task record first.
    task.attributes[AUDIT_OBJECTIVE_ATTR] = state.audit_objective
    agent.subagents.save(task)
    context = agent.subagents.runner_context.build_execution_context(task.id)
    prompt = _build_subagent_runner_prompt(context)

    runtime_profile = context.context_bundle["runtime_profile"]
    assert AUDIT_OBJECTIVE_ATTR not in runtime_profile
    assert runtime_profile["source_profile"]["text"] == (
        "source_id=source-1\n当前来源规则：只检查 verified。"
    )
    assert AUDIT_USER_MARKER not in prompt
    assert "兄弟来源原始要求" not in prompt
    assert "当前全局要求：逐条研判并保留引用。" not in prompt
    assert "当前来源规则：只检查 verified。" in prompt


def test_active_audit_revision_syncs_into_next_source_worker_slice(
    tmp_path: Path,
) -> None:
    audit_id = "audit-source-runtime-update"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-before-revision"
    agent.subagents.save(task)
    new_goal = published_audit_requirement(
        validated_notes="下一批采用新的共享要求。",
        user_prepare_history="完整用户历史只保存在命名 Audit。",
    )
    store = SimpleNamespace(
        load_task_link=lambda selected: SimpleNamespace(
            task_id=selected,
            work_kind="audit",
            status="active",
            goal=new_goal,
            run_prompt="本次运行增加复核要求。",
        )
    )
    agent.conversation_store = store

    assert audit_runtime_requirement_for_task(store, audit_id) == ("下一批采用新的共享要求。")
    assert _sync_source_worker_runtime_context(agent, state, task) is True
    updated = agent.subagents.load(task.id)
    assert updated.attributes[AUDIT_OBJECTIVE_ATTR] == "下一批采用新的共享要求。"
    assert updated.attributes[AUDIT_RUN_PROMPT_ATTR] == "本次运行增加复核要求。"
    assert updated.attributes[AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR] == (
        "attempt-before-revision"
    )
    assert state.audit_run_prompt == "本次运行增加复核要求。"
    persisted = load_state(owner_home, state.watch_id)
    assert persisted is not None
    assert persisted.audit_run_prompt == "本次运行增加复核要求。"


def test_active_audit_transport_revision_preserves_progress_and_updates_next_slice(
    tmp_path: Path,
) -> None:
    audit_id = "audit-source-transport-update"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1, run_epoch=4)
    state.source_mode = "adapter"
    state.source_profile_ref = "work/sources/window-old.md"
    state.document_refs = ["work/sources/window-old-fields.md"]
    state.source_envelope = {
        "mode": "adapter",
        "record_boundary": "adapter_records",
        "request": {"method": "GET"},
        "adapter": {"path": "work/window_adapter.py", "sha256": "a" * 64},
        "valid": True,
        "continuation_verified": True,
        "observed_record_count": 7,
    }
    state.source_config_version = audit_source_config_version(
        source_url=state.source_url,
        source_mode=state.source_mode,
        source_envelope=state.source_envelope,
        poll_query_seconds=state.tuning.poll_query_seconds,
    )
    state.cursor = 321
    state.source_checkpoint = {"next_from_ms": 123456, "pending_more": False}
    state.spool_seq = 29
    state.spool_generation = 3
    state.last_error = "old adapter hash"
    state.last_error_code = "SOURCE_ADAPTER_INVALID"
    persist_state(state)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    previous_run_id = task.id
    previous_progress = (
        state.cursor,
        dict(state.source_checkpoint),
        state.spool_seq,
        state.spool_generation,
        state.audit_run_epoch,
    )
    binding = dict(
        normalize_audit_source_bindings(
            [
                {
                    "source_id": state.source_id,
                    "source_profile_ref": "work/sources/window-current.md",
                    "document_refs": ["work/sources/window-current-fields.md"],
                    "url": state.source_url,
                    "mode": "adapter",
                    "http_request": {"method": "GET"},
                    "source_adapter": {
                        "path": "work/window_adapter.py",
                        "sha256": "b" * 64,
                    },
                }
            ]
        )[0]
    )
    agent.conversation_store = SimpleNamespace(
        load_task_link=lambda selected: SimpleNamespace(
            task_id=selected,
            work_kind="audit",
            status="active",
            goal=published_audit_requirement(
                validated_notes="下一批使用修正后的时间窗适配器。",
                user_prepare_history="历史要求留在命名 Audit。",
            ),
            run_prompt="继续同一轮监控。",
            effective_source_bindings=(binding,),
        )
    )

    assert _sync_source_worker_runtime_context(agent, state, task) is True

    updated = agent.subagents.load(previous_run_id)
    assert updated.id == previous_run_id
    assert updated.attributes[AUDIT_SOURCE_CONFIG_VERSION_ATTR] == state.source_config_version
    assert updated.attributes["audit_source_profile_ref"] == (
        "work/sources/window-current.md"
    )
    assert updated.context_manifest.required_read_paths == [
        "work/sources/window-current.md",
        "work/sources/window-current-fields.md",
    ]
    assert state.source_envelope["adapter"]["sha256"] == "b" * 64
    assert state.last_error == ""
    assert state.last_error_code == ""
    assert (
        state.cursor,
        state.source_checkpoint,
        state.spool_seq,
        state.spool_generation,
        state.audit_run_epoch,
    ) == previous_progress
    persisted = load_state(owner_home, state.watch_id)
    assert persisted is not None
    assert persisted.source_envelope["adapter"]["sha256"] == "b" * 64
    assert persisted.source_checkpoint == {"next_from_ms": 123456, "pending_more": False}


def test_source_worker_projects_json_escaped_host_wrapper_without_sibling_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-escaped-objective"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.source_profile_ref = "docs/sources/current.md"
    wrapped = published_audit_requirement(
        validated_notes="当前全局要求：逐条研判并保留引用。",
        user_prepare_history="第一路要求。\n\n--- 后续 prepare ---\n\n兄弟来源要求。",
    )
    state.audit_objective = wrapped.replace("\n", r"\n")
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.attributes[AUDIT_OBJECTIVE_ATTR] = state.audit_objective
    agent.subagents.save(task)
    context = agent.subagents.runner_context.build_execution_context(task.id)
    prompt = _build_subagent_runner_prompt(context)

    runtime_profile = context.context_bundle["runtime_profile"]
    assert AUDIT_OBJECTIVE_ATTR not in runtime_profile
    assert AUDIT_USER_MARKER not in prompt
    assert "兄弟来源要求" not in prompt
    assert "当前全局要求：逐条研判并保留引用。" not in prompt


@pytest.mark.parametrize("escaped", [False, True])
def test_canonical_audit_notes_unwraps_nested_partial_host_wrapper(
    escaped: bool,
) -> None:
    nested = published_audit_requirement(
        validated_notes="旧的共享说明。\n\n运行中的补充要求。",
        user_prepare_history="不能进入工作者提示的完整用户历史。",
    )
    nested = nested.removeprefix("# Audit 生效上下文\n")
    wrapped = published_audit_requirement(
        validated_notes=nested,
        user_prepare_history="外层保存的完整用户历史。",
    )
    if escaped:
        wrapped = wrapped.replace("\n", r"\n")

    notes = canonical_audit_validated_notes(wrapped)

    assert notes == "旧的共享说明。\n\n运行中的补充要求。"
    assert AUDIT_NOTES_MARKER not in notes
    assert AUDIT_USER_MARKER not in notes
    assert "用户历史" not in notes


@pytest.mark.parametrize("escaped", [False, True])
def test_canonical_audit_notes_keeps_tail_after_nested_host_footer(
    escaped: bool,
) -> None:
    old = published_audit_requirement(
        validated_notes="旧的共享说明。",
        user_prepare_history="不能进入工作者提示的旧用户历史。",
    )
    nested_with_update = old.removeprefix("# Audit 生效上下文\n") + "\n\n运行中的补充要求。"
    wrapped = published_audit_requirement(
        validated_notes=nested_with_update,
        user_prepare_history="外层保存的完整用户历史。",
    )
    if escaped:
        wrapped = wrapped.replace("\n", r"\n")

    notes = canonical_audit_validated_notes(wrapped)

    assert notes == "旧的共享说明。\n\n运行中的补充要求。"
    assert AUDIT_NOTES_MARKER not in notes
    assert AUDIT_USER_MARKER not in notes
    assert "用户历史" not in notes


def test_source_worker_without_explicit_refs_has_no_file_tools_or_read_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-no-read-surface"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)

    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    context = agent.subagents.runner_context.build_execution_context(task.id)
    prompt = _build_subagent_runner_prompt(context)

    assert task.allowed_tools == ["watch_stream", "record_finding"]
    runtime_profile = context.context_bundle["runtime_profile"]
    assert AUDIT_OBJECTIVE_ATTR not in runtime_profile
    assert runtime_profile["source_profile"] == {
        "ref": "",
        "inline": False,
        "complete": False,
        "reason": "missing_ref",
    }
    assert "read_file" not in prompt
    assert "read_artifact" not in prompt
    assert "明确引用读取" not in prompt
    assert "Tool Content Transport Protocol" not in prompt


def test_source_worker_never_silently_truncates_large_source_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-source-large-profile"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    audit_root = _bind_audit_workspace(agent, owner_home, audit_id, tmp_path)
    state = _watch(owner_home, audit_id, 1)
    profile = audit_root / "work" / "sources" / "large.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id=source-1\n" + "完整规则。" * 3_000, encoding="utf-8")
    state.source_profile_ref = str(profile)
    persist_state(state)

    created = ensure_audit_source_worker(agent, state)
    context = agent.subagents.runner_context.build_execution_context(str(created["run_id"]))
    prompt = _build_subagent_runner_prompt(context)
    projection = context.context_bundle["runtime_profile"]["source_profile"]

    assert projection["inline"] is False
    assert projection["complete"] is False
    assert projection["reason"] == "profile_too_large"
    assert "text" not in projection
    assert "完整规则。完整规则。" not in prompt
    assert str(profile) in prompt


def test_recovery_projects_persisted_finding_outbox_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-finding-recovery"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-recovery-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "recovery",
        }
    )
    agent.conversation_store = None
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    ledger = Path(task.agent_run_findings_jsonl)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "version": 2,
                "id": "af-recovery",
                "revision": 1,
                "audit_finding": True,
                "owner_id": "owner-a",
                "audit_id": audit_id,
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "claim": "落盘后进程崩溃的 finding",
                "source_refs": [
                    f"audit://{state.watch_id}/candidate/1:0",
                ],
                "evidence_records": [
                    {
                        "source_ref": f"audit://{state.watch_id}/candidate/1:0",
                        "ack_id": "1:0",
                        "inline": True,
                        "raw_complete": True,
                        "raw_event": {"reading_id": "READ-42", "kind": "sensor"},
                    }
                ],
                "stage": "initial",
                "report_status": "pending",
                "requires_llm_report": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    unavailable = reconcile_audit_finding_outbox(agent, task.id)
    agent.conversation_store = store
    recovered = reconcile_audit_finding_outbox(agent, task.id)
    duplicate = reconcile_audit_finding_outbox(agent, task.id)

    assert unavailable["state"] == "pending"
    assert recovered["state"] == "caught_up", recovered
    assert recovered["published"] == 1
    assert duplicate["published"] == 0
    signals = store.pending_wake_signals()
    assert len(signals) == 1
    assert signals[0].metadata["finding_id"] == "af-recovery"
    assert signals[0].metadata["requires_llm_report"] is True
    assert signals[0].metadata["report_scope"] == "incremental"
    assert signals[0].metadata["evidence_records"][0]["raw_event"]["reading_id"] == "READ-42"
    assert audit_task_has_unreported_findings(agent, audit_id) is True

    # A process crash or an old buggy sibling acknowledgement may remove the
    # queue row without a sent delivery receipt. The durable finding outbox
    # must recreate that exact event instead of declaring it delivered.
    first_signal_id = signals[0].wake_signal_id
    store.mark_wake_signal_handled(first_signal_id)
    repaired = reconcile_audit_finding_outbox(agent, task.id)
    replayed = store.pending_wake_signals()
    assert repaired["owner_delivery_replay"]["requeued"] == 1
    assert len(replayed) == 1
    assert replayed[0].wake_signal_id != first_signal_id
    assert replayed[0].metadata["finding_id"] == "af-recovery"


def test_finding_evidence_projection_keeps_complete_record_or_exact_ref() -> None:
    from agent_py_agent.agent.agent_core.runtime.record_finding_tool import (
        _audit_evidence_record,
    )

    source_ref = "audit://ws-1234567890/candidate/7:0"
    complete = _audit_evidence_record(
        {
            "ok": True,
            "source_id": "sensor-a",
            "watch_id": "ws-1234567890",
            "source_ref": source_ref,
            "ack_id": "7:0",
            "raw_complete": True,
            "raw_event": {"reading_id": "SENSOR-0007", "value": 42},
            "event_sha256": "abc123",
            "event_bytes": 48,
        },
        source_ref=source_ref,
        ack_id="7:0",
    )
    oversized = _audit_evidence_record(
        {
            "ok": True,
            "source_id": "sensor-a",
            "watch_id": "ws-1234567890",
            "source_ref": source_ref,
            "ack_id": "7:0",
            "raw_complete": True,
            "raw_event": {"payload": "x" * 12_100},
            "event_sha256": "def456",
            "event_bytes": 12_100,
        },
        source_ref=source_ref,
        ack_id="7:0",
    )

    assert complete["inline"] is True
    assert complete["raw_event"]["reading_id"] == "SENSOR-0007"
    assert oversized["inline"] is False
    assert oversized["reason"] == "source_record_exceeds_inline_budget"
    assert "raw_event" not in oversized
    assert oversized["source_ref"] == source_ref


def test_finding_delivery_receipt_uses_verdict_ref_not_supplementary_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import harvester

    audit_id = "audit-finding-mixed-evidence"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-mixed-evidence-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "mixed-evidence",
        }
    )
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    agent.conversation_store = store
    canonical = f"audit://{state.watch_id}/candidate/1601:0"
    supporting = "SOAK-C-000001600"
    ledger = Path(task.agent_run_findings_jsonl)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "version": 2,
                "id": "disk-node-49-offset1600",
                "revision": 1,
                "audit_finding": True,
                "owner_id": "owner-a",
                "audit_id": audit_id,
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "claim": "节点磁盘故障确认",
                "source_refs": [canonical, supporting],
                "evidence_refs": [canonical, supporting],
                "evidence_verdicts": [
                    {
                        "source_ref": canonical,
                        "ack_id": "1601:0",
                        "verdict": "hit",
                        "score": 95,
                    }
                ],
                "stage": "initial",
                "report_status": "pending",
                "requires_llm_report": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    checked: list[tuple[str, ...]] = []

    def already_sent(_owner_home, refs):
        checked.append(tuple(refs))
        return tuple(refs) == (canonical,)

    monkeypatch.setattr(harvester, "audit_source_refs_reported", already_sent)

    assert audit_task_has_unreported_findings(agent, audit_id) is False
    replay = source_worker_module._requeue_unreported_finding_deliveries(
        agent,
        task,
        ledger,
    )
    assert replay == {"ok": True, "state": "caught_up", "requeued": 0}
    assert checked == [(canonical,), (canonical,)]
    assert store.pending_wake_signals() == []


def test_audit_terminal_receipt_check_reads_each_watch_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import harvester

    audit_id = "audit-terminal-receipt-index"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    refs = [
        f"audit://{state.watch_id}/candidate/1701:0",
        f"audit://{state.watch_id}/candidate/1702:0",
    ]
    rows = []
    for index, source_ref in enumerate(refs, start=1):
        rows.append(
            {
                "version": 2,
                "id": f"finding-{index}",
                "revision": 1,
                "audit_finding": True,
                "owner_id": "owner-a",
                "audit_id": audit_id,
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "claim": f"事件 {index}",
                "evidence_verdicts": [
                    {
                        "source_ref": source_ref,
                        "ack_id": source_ref.rsplit("/", 1)[-1],
                        "verdict": "hit",
                        "score": 95,
                    }
                ],
                "stage": "initial",
                "report_status": "pending",
                "requires_llm_report": True,
            }
        )
    ledger = Path(task.agent_run_findings_jsonl)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    checked: list[tuple[str, ...]] = []

    def all_sent(_owner_home, source_refs):
        checked.append(tuple(source_refs))
        return True

    monkeypatch.setattr(harvester, "audit_source_refs_reported", all_sent)

    assert audit_task_has_unreported_findings(agent, audit_id) is False
    assert checked == [tuple(refs)]


def test_recovery_projects_inline_verdict_finding_after_crash_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-inline-finding-recovery"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-inline-recovery-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "inline-recovery",
        }
    )
    state = _watch(owner_home, audit_id, 1)
    state.audit_run_epoch = 7
    persist_state(state)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    verdict_path = state_dir(owner_home) / f"{state.watch_id}.verdicts.ndjson"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    source_ref = f"audit://{state.watch_id}/candidate/1:0"
    verdict_path.write_text(
        json.dumps(
            {
                "ack_id": "1:0",
                "verdict": "hit",
                "score": 97,
                "score_range": {"min": 0, "max": 100},
                "dimensions": [],
                "note": "结果字段与用户条件共同支持命中",
                "stage": "initial",
                "acknowledged": True,
                "owner_id": "owner-a",
                "audit_id": audit_id,
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "source_ref": source_ref,
                "audit_run_epoch": 7,
                "ingest_run_epoch": 6,
                "processing_run_id": task.id,
                "processing_attempt_id": "attempt-before-crash",
                "finding": {
                    "claim": "崩溃前已经随判定持久化的发现",
                    "kind": "hit",
                    "urgency": "urgent",
                    "requires_llm_report": True,
                    # The model may suggest an adjacent row. Inline findings
                    # are authoritative only for their owning verdict row.
                    "evidence_refs": [f"audit://{state.watch_id}/candidate/2:0"],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    agent.conversation_store = store

    recovered = reconcile_audit_finding_outbox(agent, task.id)
    duplicate = reconcile_audit_finding_outbox(agent, task.id)

    assert recovered["state"] == "caught_up", recovered
    assert recovered["inline_projection"]["projected"] == 1
    assert recovered["published"] == 1
    assert duplicate["inline_projection"]["projected"] == 0
    assert duplicate["published"] == 0
    finding_rows = [
        json.loads(line)
        for line in Path(task.agent_run_findings_jsonl).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(finding_rows) == 1
    assert finding_rows[0]["source"] == "watch_stream.verdict"
    assert finding_rows[0]["source_refs"] == [source_ref]
    assert finding_rows[0]["audit_run_epoch"] == 7
    assert finding_rows[0]["ingest_run_epoch"] == 6
    signals = store.pending_wake_signals()
    assert len(signals) == 1
    assert signals[0].metadata["delivery_evidence_refs"] == [source_ref]
    assert signals[0].metadata["audit_run_epoch"] == 7
    assert signals[0].metadata["ingest_run_epoch"] == 6


def test_nonreport_finding_is_durable_without_waking_owner_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.ingestion.source_worker import (
        _publish_audit_finding_event,
    )

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-finding-no-report"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-no-report-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "no-report",
        }
    )
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    agent.conversation_store = store

    published = _publish_audit_finding_event(
        agent,
        task,
        {
            "id": "af-no-report",
            "revision": 1,
            "owner_id": "owner-a",
            "source_id": state.source_id,
            "watch_id": state.watch_id,
            "claim": "低置信记录，仅保留审计",
            "source_refs": [f"audit://{state.watch_id}/candidate/1:0"],
            "stage": "initial",
            "report_status": "pending",
            "requires_llm_report": False,
        },
    )

    assert published["ok"] is True
    assert published["report_requested"] is False
    assert published["wake_signal_id"] == ""
    assert store.pending_wake_signals() == []
    observations = store.recent_observations(thread.thread_id)
    assert len(observations) == 1
    assert observations[0].requires_main_agent is False
    assert observations[0].requires_llm_report is False


def test_lease_takeover_rejects_stale_attempt(
    tmp_path: Path,
) -> None:
    audit_id = "audit-lease"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
    }
    task = agent.subagents.create_run(
        goal="consume",
        thought="judge",
        plan=["pull"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=["watch_stream"],
        attributes=attrs,
    )
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-a"
    agent.subagents.save(task)

    previous = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-a",
        task_attributes=attrs,
    )
    try:
        first = authorize_source_worker_action(agent, state, now=100.0)
    finally:
        restore_current_subagent_context(agent, previous)
    assert first["ok"] is True

    task = agent.subagents.load(task.id)
    task.runner_active_attempt_id = "attempt-b"
    agent.subagents.save(task)
    current = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-b",
        task_attributes=attrs,
    )
    try:
        held = authorize_source_worker_action(agent, state, now=101.0)
        lease_path = state_dir(owner_home) / f"{state.watch_id}.worker-lease.json"
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        payload["expires_at"] = 100.0
        lease_path.write_text(json.dumps(payload), encoding="utf-8")
        takeover = authorize_source_worker_action(agent, state, now=102.0)
    finally:
        restore_current_subagent_context(agent, current)
    assert held["error_code"] == "AUDIT_SOURCE_LEASE_HELD"
    assert takeover["ok"] is True
    assert takeover["lease_epoch"] == 2

    with source_worker_lease_fence(state, first, now=103.0) as fenced_old:
        assert fenced_old["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    with source_worker_lease_fence(state, takeover, now=103.0) as fenced_current:
        assert fenced_current["ok"] is True

    stale = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-a",
        task_attributes=attrs,
    )
    try:
        denied = authorize_source_worker_action(agent, state, now=103.0)
    finally:
        restore_current_subagent_context(agent, stale)
    assert denied["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"


def test_old_source_slice_may_finish_claimed_batch_but_cannot_claim_next_batch(
    tmp_path: Path,
) -> None:
    audit_id = "audit-config-slice-fence"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.source_config_version = "sha256:" + "a" * 64
    persist_state(state)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        AUDIT_SOURCE_CONFIG_VERSION_ATTR: state.source_config_version,
    }
    task = agent.subagents.create_run(
        goal="consume one claimed batch",
        thought="judge",
        plan=["pull", "verdict"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=["watch_stream"],
        attributes=attrs,
    )
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-a"
    agent.subagents.save(task)
    previous = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-a",
        task_attributes=attrs,
    )
    try:
        claimed_batch_authority = authorize_source_worker_action(
            agent,
            state,
            now=100.0,
        )
        assert claimed_batch_authority["ok"] is True
        state.source_config_version = "sha256:" + "b" * 64
        persist_source_binding_revision(state)
        next_batch = authorize_source_worker_action(
            agent,
            state,
            now=101.0,
            require_current_config=True,
        )
    finally:
        restore_current_subagent_context(agent, previous)

    assert next_batch["error_code"] == "AUDIT_SOURCE_CONTEXT_REFRESH_REQUIRED"
    with source_worker_lease_fence(
        state,
        claimed_batch_authority,
        now=101.0,
    ) as finish_claimed:
        assert finish_claimed["ok"] is True
    with source_worker_lease_fence(
        state,
        claimed_batch_authority,
        now=101.0,
        require_current_config=True,
    ) as claim_next:
        assert claim_next["error_code"] == "AUDIT_SOURCE_CONTEXT_REFRESH_REQUIRED"


def test_lease_acquire_rechecks_attempt_inside_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audit_id = "audit-lease-race"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
    }
    task = agent.subagents.create_run(
        goal="consume",
        thought="judge",
        plan=["pull"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=["watch_stream"],
        attributes=attrs,
    )
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-old"
    agent.subagents.save(task)

    original_validate = source_worker_module._validate_current_worker_task
    validations = 0

    def _replace_attempt_between_checks(*args, **kwargs):
        nonlocal validations
        validations += 1
        if validations == 2:
            replacement = agent.subagents.load(task.id)
            replacement.runner_active_attempt_id = "attempt-new"
            agent.subagents.save(replacement)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        source_worker_module,
        "_validate_current_worker_task",
        _replace_attempt_between_checks,
    )
    previous = set_current_subagent_context(
        agent,
        run_id=task.id,
        attempt_id="attempt-old",
        task_attributes=attrs,
    )
    try:
        denied = authorize_source_worker_action(agent, state, now=100.0)
    finally:
        restore_current_subagent_context(agent, previous)

    assert validations == 2
    assert denied["ok"] is False
    assert denied["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert not (state_dir(owner_home) / f"{state.watch_id}.worker-lease.json").exists()


def test_previous_named_audit_epoch_cannot_consume_reopened_stable_watch(
    tmp_path: Path,
) -> None:
    audit_id = "audit-old-epoch-fenced"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1, run_epoch=2)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    old_attrs = {
        AUDIT_ATTR: True,
        AUDIT_RUN_EPOCH_ATTR: 1,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "work_scope_key": audit_source_work_scope_key(worker_key, 1),
    }
    old_task = agent.subagents.create_run(
        goal="旧轮次迟到来源工作者",
        thought="consume",
        plan=["pull"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=["watch_stream"],
        attributes=old_attrs,
    )
    old_task.status = TaskStatus.RUNNING.value
    old_task.runner_active_attempt_id = "old-attempt"
    agent.subagents.save(old_task)

    previous = set_current_subagent_context(
        agent,
        run_id=old_task.id,
        attempt_id="old-attempt",
        task_attributes=old_attrs,
    )
    try:
        denied = authorize_source_worker_action(agent, state, now=100.0)
    finally:
        restore_current_subagent_context(agent, previous)

    assert denied["ok"] is False
    assert denied["error_code"] == "AUDIT_SOURCE_BINDING_MISMATCH"
    assert source_worker_facts(agent, state)["run_id"] == ""
    assert not (state_dir(owner_home) / f"{state.watch_id}.worker-lease.json").exists()


@pytest.mark.parametrize("reported_status", ["DONE", "PENDING", "BLOCKED"])
def test_source_worker_batch_end_stays_pending_while_watch_is_open(
    tmp_path: Path,
    reported_status: str,
) -> None:
    audit_id = "audit-result"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    task = SubAgentTask(
        id="worker-result",
        goal="consume",
        thought="judge",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        runner_active_attempt_id="attempt-current",
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
            "background_start": {
                "launch_id": "shared-launch",
                "status": "running",
                "pid": 4242,
            },
        },
    )
    lease_path = state_dir(owner_home) / f"{state.watch_id}.worker-lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "worker_key": worker_key,
                "run_id": task.id,
                "attempt_id": "attempt-current",
                "expires_at": 999.0,
            }
        ),
        encoding="utf-8",
    )
    result_meta = {
        "ok": True,
        "message": "",
        "response": "one batch complete",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(found=True, ok=True, status=reported_status),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.PENDING.value
    assert task.verification_status == VerificationStatus.UNVERIFIED.value
    assert task.failure_type == FailureType.INCOMPLETE_DELIVERABLES.value
    assert task.ended_at == 0.0
    assert task.attributes["background_start"]["status"] == "reclaimed"
    assert task.attributes["audit_source_recovery"]["consecutive_stalls"] == 0
    assert not lease_path.exists()


@pytest.mark.parametrize("reported_status", ["DONE", "PENDING", "BLOCKED"])
def test_source_worker_late_result_stays_cancelled_after_watch_clear(
    tmp_path: Path,
    reported_status: str,
) -> None:
    audit_id = "audit-result-cleared"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.closed = True
    state.closed_at = 100.0
    state.close_reason = "named_audit_clear"
    persist_state(state)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    task = SubAgentTask(
        id="worker-result-cleared",
        goal="consume",
        thought="judge",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        runner_active_attempt_id="attempt-current",
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        },
    )
    result_meta = {
        "ok": True,
        "message": "",
        "response": "late generated result",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(
                found=True,
                ok=True,
                status=reported_status,
            ),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.CANCELLED.value
    assert task.verification_status == VerificationStatus.UNVERIFIED.value
    assert task.failure_type == FailureType.CANCELLED.value
    assert task.blockers == []
    assert task.ended_at == 123.0
    assert task.runner_active_attempt_id == ""
    assert task.result == "Audit source was closed; late runner result ignored"
    assert result_meta == {
        "ok": False,
        "message": "Audit source was closed; late runner result ignored",
        "response": "late generated result",
        "dry_run": False,
    }


def test_closed_source_watch_fences_stale_runner_start_at_canonical_save(
    tmp_path: Path,
) -> None:
    audit_id = "audit-start-after-clear"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    task = agent.subagents.create_run(
        goal="consume",
        thought="judge",
        plan=["pull"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                state.watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        },
    )
    stale = agent.subagents.load(task.id)
    state.closed = True
    state.closed_at = 100.0
    state.close_reason = "named_audit_clear"
    persist_state(state)
    stale.status = TaskStatus.RUNNING.value
    stale.runner_active_attempt_id = "attempt-selected-before-clear"

    agent.subagents.save(stale)
    persisted = agent.subagents.load(task.id)

    assert stale.status == TaskStatus.CANCELLED.value
    assert persisted.status == TaskStatus.CANCELLED.value
    assert persisted.failure_type == FailureType.CANCELLED.value
    assert persisted.runner_active_attempt_id == ""
    assert "attempt-selected-before-clear" in persisted.runner_abandoned_attempt_ids
    with pytest.raises(RuntimeError, match="terminal run"):
        agent.subagents.lifecycle.prepare_runner_attempt(task.id)


def test_source_worker_batch_end_closes_irrelevant_capability_request(
    tmp_path: Path,
) -> None:
    audit_id = "audit-result-capability"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    request = CapabilityRequest(
        id="cap-write-report",
        from_run_id="worker-result-capability",
        problem="write a generic final report",
        needed_capability="write_file",
        requested_tools=["write_file"],
    )
    task = SubAgentTask(
        id="worker-result-capability",
        goal="consume",
        thought="judge",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        capability_requests=[request],
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        },
    )
    result_meta = {
        "ok": True,
        "message": "",
        "response": "one batch complete",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="PENDING",
                capability_requests=[{"requested_tools": ["write_file"]}],
            ),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.PENDING.value
    assert task.capability_requests[0].status == "CLOSED"
    assert task.blockers == []


def test_source_worker_malformed_batch_closeout_stays_pending_with_error_visible(
    tmp_path: Path,
) -> None:
    audit_id = "audit-result-parse-error"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    task = SubAgentTask(
        id="worker-result-parse-error",
        goal="consume",
        thought="judge",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                state.watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        },
    )
    result_meta = {
        "ok": False,
        "message": "runner did not produce a valid result block",
        "response": "batch verdicts were already persisted",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(
                found=True,
                ok=False,
                parse_error="missing required status",
            ),
            now=123.0,
        )
    )

    assert task.status == TaskStatus.PENDING.value
    assert task.verification_status == VerificationStatus.UNVERIFIED.value
    assert task.failure_type == FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value
    assert task.ended_at == 0.0
    assert task.runner_last_error.endswith(
        "structured output parse failed: missing required status"
    )
    assert result_meta["ok"] is False


def test_settled_source_worker_ignores_report_capability_and_finishes(
    tmp_path: Path,
) -> None:
    audit_id = "audit-settled-capability"
    _agent_value, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    state.opened_at = 1.0
    state.watch_window_seconds = 1
    state.window_finalized_at = 2.0
    persist_state(state)
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    request = CapabilityRequest(
        id="cap-write-settled-report",
        from_run_id="worker-settled-capability",
        problem="write a generic final report",
        needed_capability="write_file",
        requested_tools=["write_file"],
    )
    task = SubAgentTask(
        id="worker-settled-capability",
        goal="consume",
        thought="judge",
        plan=["pull"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        capability_requests=[request],
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        },
    )
    result_meta = {
        "ok": False,
        "message": "waiting for report capability",
        "response": "all durable verdicts were already committed",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": "",
            },
            parsed=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="BLOCKED",
                capability_requests=[{"requested_tools": ["write_file"]}],
            ),
            now=123.0,
        )
    )

    assert result_meta["ok"] is True
    assert result_meta["message"] == "Audit source window complete and backlog settled"
    assert task.status == TaskStatus.DONE.value
    assert task.verification_status == VerificationStatus.VERIFIED.value
    assert task.failure_type == ""
    assert task.blockers == []
    assert task.capability_requests[0].status == "CLOSED"
    assert task.progress == 1.0


@pytest.mark.parametrize(
    "failure_type",
    [
        FailureType.STATUS_BLOCKED.value,
        FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value,
    ],
)
def test_reconcile_requeues_generic_blocked_source_worker(
    tmp_path: Path,
    monkeypatch,
    failure_type: str,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-generic-blocked"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = failure_type
    task.blockers = ["runner completed one bounded batch"]
    task.capability_requests = [
        CapabilityRequest(
            id="cap-report",
            from_run_id=task.id,
            problem="write report",
            needed_capability="write_file",
            requested_tools=["write_file"],
        )
    ]
    agent.subagents.save(task)

    ensured = ensure_audit_source_worker(agent, state)

    persisted = agent.subagents.load(task.id)
    assert ensured["state"] == "waiting_for_worker"
    assert ensured["run_id"] == task.id
    assert persisted.status == TaskStatus.PENDING.value
    assert persisted.failure_type == FailureType.INCOMPLETE_DELIVERABLES.value
    assert persisted.blockers == []
    assert persisted.capability_requests[0].status == "CLOSED"
    assert len(agent.subagents.list_runs()) == 1


@pytest.mark.parametrize(
    "failure_type",
    [
        FailureType.PROVIDER_TIMEOUT.value,
        FailureType.TRANSIENT_ERROR.value,
    ],
)
def test_provider_supply_failure_waits_then_requeues_same_source_worker(
    tmp_path: Path,
    monkeypatch,
    failure_type: str,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.ingestion import source_worker

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    monkeypatch.setattr(
        source_worker,
        "jittered_backoff",
        lambda *_args, **_kwargs: 30.0,
    )
    audit_id = "audit-provider-recovery"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = failure_type
    task.runner_attempts = 1
    task.runner_last_attempt_at = time.time()
    task.runner_last_error = "typed provider failure"
    agent.subagents.save(task)

    first = ensure_audit_source_worker(agent, state)
    repeated = ensure_audit_source_worker(agent, state)

    blocked = agent.subagents.load(task.id)
    recovery = blocked.attributes["audit_source_recovery"]
    reason = (
        "provider_timeout"
        if failure_type == FailureType.PROVIDER_TIMEOUT.value
        else "provider_transient"
    )
    assert first["state"] == "waiting_for_retry"
    assert repeated["state"] == "waiting_for_retry"
    assert first["run_id"] == task.id
    assert repeated["retry_not_before"] == first["retry_not_before"]
    assert blocked.status == TaskStatus.BLOCKED.value
    assert recovery[f"{reason}_count"] == 1
    assert recovery["consecutive_provider_failures"] == 1
    assert source_worker_facts(agent, state)["state"] == "recovering"

    recovery["provider_retry_not_before"] = time.time() - 1.0
    blocked.attributes["audit_source_recovery"] = recovery
    agent.subagents.save(blocked)
    resumed = ensure_audit_source_worker(agent, state)

    persisted = agent.subagents.load(task.id)
    assert resumed["state"] == "waiting_for_worker"
    assert resumed["run_id"] == task.id
    assert resumed["recovery_reason"] == failure_type
    assert persisted.status == TaskStatus.PENDING.value
    assert persisted.failure_type == ""
    assert len(agent.subagents.list_runs()) == 1


def test_non_retryable_source_worker_failure_remains_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-permission-blocked"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = FailureType.PERMISSION_BLOCKED.value
    agent.subagents.save(task)

    result = ensure_audit_source_worker(agent, state)

    persisted = agent.subagents.load(task.id)
    assert result["state"] == "degraded"
    assert result["ok"] is False
    assert persisted.status == TaskStatus.BLOCKED.value
    assert persisted.failure_type == FailureType.PERMISSION_BLOCKED.value


def test_quota_exhausted_source_worker_waits_for_explicit_operator_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-quota-blocked"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    agent.subagents.save(task)

    first = ensure_audit_source_worker(agent, state)
    second = ensure_audit_source_worker(agent, state)
    persisted = agent.subagents.load(task.id)
    facts = source_worker_facts(agent, state)

    assert first["state"] == "awaiting_operator"
    assert second["state"] == "awaiting_operator"
    assert first["error_code"] == "PROVIDER_QUOTA_EXHAUSTED"
    assert persisted.status == TaskStatus.BLOCKED.value
    assert persisted.failure_type == FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    assert facts["state"] == "awaiting_operator"
    assert facts["failure_type"] == FailureType.PROVIDER_QUOTA_EXHAUSTED.value

    from agent_py_agent.agent.ingestion.source_worker import (
        resume_quota_blocked_source_workers,
    )

    assert resume_quota_blocked_source_workers(agent, audit_id) == 1
    resumed = agent.subagents.load(task.id)
    assert resumed.status == TaskStatus.PENDING.value
    assert resumed.failure_type == FailureType.INCOMPLETE_DELIVERABLES.value
    assert resumed.attributes["audit_source_recovery"]["operator_resume_count"] == 1
    assert resume_quota_blocked_source_workers(agent, audit_id) == 0


def test_capacity_alert_publishes_edge_and_recovery_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import harvester

    audit_id = "audit-capacity-alert"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    store = ConversationStore(tmp_path / "conversations")
    agent.conversation_store = store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "capacity-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续监测容量",
            "status": "active",
            "work_kind": "audit",
            "work_name": "容量巡检",
        }
    )
    states = [_watch(owner_home, audit_id, index) for index in range(1, 11)]
    source_workers = [
        (state, f"run-capacity-{index}") for index, state in enumerate(states, start=1)
    ]
    active_by_watch = {
        state.watch_id: {
            "pending": 1000 + index,
            "oldest_pending_age_seconds": 900.0 + index,
            "ingest_records_per_second": 5.0,
            "processing_throughput": {"records_per_second": 1.0},
            "processing_latency": {
                "p95_seconds": 800.0 + index,
                "sample_count": 10,
            },
            "capacity_alert": {
                "active": True,
                "reasons": ["backlog_threshold"],
            },
        }
        for index, state in enumerate(states, start=1)
    }
    monkeypatch.setattr(
        harvester,
        "audit_capacity_facts",
        lambda state, **_kwargs: active_by_watch[state.watch_id],
    )

    first = reconcile_audit_capacity_alert(
        agent,
        source_workers,
        now=1000.0,
    )
    duplicate = reconcile_audit_capacity_alert(
        agent,
        source_workers,
        now=1001.0,
    )

    assert first["published"] == 1
    assert first["source_count"] == 10
    assert first["alert_source_count"] == 10
    assert duplicate["published"] == 0
    assert len(store.pending_wake_signals()) == 1
    wake = store.pending_wake_signals()[0]
    assert wake.metadata["schema_version"] == "audit-capacity-event.v2"
    assert wake.metadata["source_count"] == 10
    assert wake.metadata["active_worker_count"] == 10
    assert wake.metadata["alert_source_count"] == 10
    assert wake.metadata["pending"] == sum(range(1001, 1011))
    assert len(wake.metadata["sources"]) == 10
    cursor_path = (
        audit_workspace_path(owner_home, audit_id) / "work" / "runtime" / "capacity-alert.json"
    )
    assert cursor_path.exists()
    assert not any(state_dir(owner_home).glob("*.capacity-alert.json"))

    recovered_by_watch = {
        watch_id: {
            **facts,
            "pending": 0,
            "capacity_alert": {"active": False, "reasons": []},
        }
        for watch_id, facts in active_by_watch.items()
    }
    monkeypatch.setattr(
        harvester,
        "audit_capacity_facts",
        lambda state, **_kwargs: recovered_by_watch[state.watch_id],
    )
    recovery = reconcile_audit_capacity_alert(
        agent,
        source_workers,
        now=1002.0,
    )

    assert recovery["published"] == 1
    assert len(store.pending_wake_signals()) == 2
    recovery_wake = store.pending_wake_signals()[1]
    assert recovery_wake.metadata["capacity_state"] == "recovered"
    assert recovery_wake.metadata["pending"] == 0


def test_hung_source_worker_slice_requeues_same_task_without_duplicate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-hung-worker"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = ""
    agent.subagents.save(task)
    timeout_result = SubAgentRunnerResult(
        run_id=task.id,
        dry_run=False,
        ok=False,
        status=TaskStatus.TIMEOUT.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        message="runner timed out",
    )

    reconciled = _reconcile_audit_source_worker_slice(
        agent,
        task,
        timeout_result,
    )

    persisted = agent.subagents.load(task.id)
    assert reconciled.status == TaskStatus.PENDING.value
    assert persisted.status == TaskStatus.PENDING.value
    assert persisted.attributes["audit_source_recovery"]["runner_timeout_count"] == 1
    assert persisted.attributes["audit_source_recovery"]["consecutive_stalls"] == 1
    facts = source_worker_facts(agent, state)
    assert facts["state"] == "recovering"
    assert facts["recovery"]["last_reason"] == "runner_timeout"
    ensured = ensure_audit_source_worker(agent, state)
    assert ensured["run_id"] == task.id
    assert len(agent.subagents.list_runs()) == 1


def test_hung_source_binding_slice_requeues_the_same_run(
    tmp_path: Path,
) -> None:
    audit_id = "audit-hung-binding"
    agent, _owner_home = _agent(tmp_path, audit_id=audit_id)
    task = agent.subagents.create_run(
        goal="open one source",
        thought="bind",
        plan=["open"],
        parent_id=audit_id,
        root_id=audit_id,
        role="worker",
        allowed_tools=list(AUDIT_SOURCE_BINDING_TOOLS),
        attributes={
            AUDIT_ATTR: True,
            AUDIT_DEADLINE_ATTR: time.time() + 600,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
        },
    )
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = "attempt-binding-stalled"
    agent.subagents.save(task)
    timeout_result = SubAgentRunnerResult(
        run_id=task.id,
        dry_run=False,
        ok=False,
        status=TaskStatus.TIMEOUT.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        message="runner timed out before source open",
    )

    reconciled = _reconcile_audit_source_binding_slice(
        agent,
        task,
        timeout_result,
    )

    assert reconciled is timeout_result
    assert reconciled.status == TaskStatus.PENDING.value
    persisted = agent.subagents.load(task.id)
    assert persisted.status == TaskStatus.PENDING.value
    assert persisted.runner_active_attempt_id == ""
    assert persisted.ended_at == 0.0
    assert persisted.attributes[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True
    assert persisted.attributes["audit_source_recovery"]["runner_timeout_count"] == 1
    assert (
        persisted.attributes["audit_source_recovery"]["last_abandoned_attempt_id"]
        == "attempt-binding-stalled"
    )
    assert len(agent.subagents.list_runs()) == 1


def test_progressing_source_worker_slice_is_rotation_not_hang(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params: {"status": "not_needed", "run_ids": []},
    )
    audit_id = "audit-progressing-worker"
    agent, owner_home = _agent(tmp_path, audit_id=audit_id)
    state = _watch(owner_home, audit_id, 1)
    created = ensure_audit_source_worker(agent, state)
    task = agent.subagents.load(str(created["run_id"]))
    task.status = TaskStatus.RUNNING.value
    task.runner_active_attempt_id = ""
    task.last_progress_at = time.time() - 1
    agent.subagents.save(task)
    timeout_result = SubAgentRunnerResult(
        run_id=task.id,
        dry_run=False,
        ok=False,
        status=TaskStatus.TIMEOUT.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        message="runner slice ended",
    )

    reconciled = _reconcile_audit_source_worker_slice(
        agent,
        task,
        timeout_result,
        timeout_seconds=300,
    )

    persisted = agent.subagents.load(task.id)
    assert reconciled.status == TaskStatus.PENDING.value
    assert persisted.status == TaskStatus.PENDING.value
    recovery = persisted.attributes["audit_source_recovery"]
    assert recovery["slice_rotation_count"] == 1
    assert recovery["consecutive_stalls"] == 0
    assert "runner_timeout_count" not in recovery
    assert source_worker_facts(agent, state)["state"] == "waiting_for_worker"
