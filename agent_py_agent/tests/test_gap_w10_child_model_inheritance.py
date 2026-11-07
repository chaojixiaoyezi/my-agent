"""真实创建、线程存储与工作片验证继承；假后端不触网。"""

import json
import logging
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
from agent_py_agent.agent.agent_core.subagent.model_selection import canonical_subagent_model_scope
from agent_py_agent.agent.conversation.agent_thread import ensure_subagent_thread
from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    inherit_model_profile,
    inherited_model_config,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_scope import (
    model_dependencies_scope,
    prepare_model_dependencies,
    selected_model_scope,
)
from agent_py_agent.tests.test_decision_subagent import prepared  # noqa: F401
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_thread_model_selection import host_with_store


@pytest.fixture
def family(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda kind, cfg: SimpleNamespace(model_name=cfg.model_name))
    host = host_with_store(tmp_path)
    explicit, default, later = [add(host, model_name=name)[0] for name in ("parent", "default", "later")]
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation

    tid = execute_local_model_operation(host, "parent-session", "select", {"profile_id": explicit})["thread_id"]
    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": tid})
    execute_model_profile_operation(host, "set_default", {"profile_id": default})
    return host, tid, explicit, default, later


def child(host, attrs, name="child"):
    task = SimpleNamespace(id=name, agent_thread_id="thread-" + name, attributes=attrs, parent_id="", root_id="", depth=1, owner="alice")
    manager = SimpleNamespace(conversation_store=host.conversation_store, workspace_root=host.conversation_store.storage.root,
                              owner_id="alice", load=lambda run_id: task)
    host.subagents = manager
    return task, ensure_subagent_thread(manager, task)


@pytest.fixture
def scheduler_fd_balance():
    directory = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"

    def count():
        if not os.path.isdir(directory):
            return None
        with os.scandir(directory) as entries:
            return len(list(entries))

    before = count()
    yield
    if before is not None:
        fd_delta = count() - before
        assert fd_delta == 0


@pytest.mark.parametrize("binding", ["unbound", "bound", "distinct"])
def test_create_inherits_canonical_parent_across_default_drift(family, binding):
    host, tid, explicit, default, later = family
    creator = host
    if binding == "distinct":
        creator = Host(host.home_paths.config_dir)
        creator.conversation_store = host.conversation_store
        creator._current_run_params = host._current_run_params
    scope = selected_model_scope(host, thread_id=tid) if binding != "unbound" else nullcontext()
    with scope:
        for index, owner_default in enumerate((default, later)):
            execute_model_profile_operation(host, "set_default", {"profile_id": owner_default})
            attrs = create_task_attributes({}, creator)
            task, thread = child(creator, attrs, f"{binding}-{index}")
            assert attrs["host_model_profile.v1"]["profile_id"] == explicit
            assert thread.model_profile_id == explicit
            assert inherited_model_config(creator, task).model_name == "parent"


def test_missing_label_in_live_binding_does_not_select_default(family):
    host, tid, explicit, _, _ = family
    config = selected_model_config(host, profile_id=explicit)
    config.config_sources = {}
    dependencies = prepare_model_dependencies(host, config, inherited=True)
    with model_dependencies_scope(host, dependencies, thread_id=tid):
        attrs = create_task_attributes({}, host)
    assert attrs["host_model_profile.v1"]["profile_id"] == explicit


def test_mismatch_emits_one_diagnostic_and_keeps_thread_fact(family, caplog):
    host, _, explicit, default, _ = family
    config = selected_model_config(host, profile_id=default)
    dependencies = prepare_model_dependencies(host, config, inherited=True)
    with caplog.at_level(logging.WARNING), model_dependencies_scope(host, dependencies):
        attrs = create_task_attributes({}, host)
        assert not [record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]
        child(host, attrs, "diagnostic-child")
    assert attrs["host_model_profile.v1"]["profile_id"] == explicit
    records = [record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]
    assert len(records) == 1
    assert "only-private-secret" not in records[0].getMessage()


def test_gateway_preflight_binding_supplies_parent_when_task_attrs_absent(family):
    host, tid, explicit, _, _ = family
    host._current_run_params = None
    with selected_model_scope(host, thread_id=tid):
        # 预检绑定仍在但标签缺失，父线程只能由绑定来源快照确定。
        host.config.config_sources = {}
        attrs = create_task_attributes({}, host)
    assert attrs["host_model_profile.v1"]["profile_id"] == explicit


def test_explicit_model_and_detached_creation_keep_existing_resolution(family):
    host, _, _, default, _ = family
    attrs = create_task_attributes({"model": "default"}, host)
    assert attrs["host_model_profile.v1"]["profile_id"] == default
    with pytest.raises(ModelProfileError):
        create_task_attributes({"model": "not-configured"}, host)
    host._current_run_params = None
    attrs = {}
    inherit_model_profile(attrs, host)
    assert attrs == {"host_model_profile.v1": {"profile_id": "default"}}


@pytest.mark.parametrize("source", ["inherited", "explicit", "automatic"])
def test_in_process_child_uses_canonical_profile_and_restores_parent(family, source):
    host, tid, explicit, default, _ = family
    task, thread = canonical_child(host, default, explicit, source, source)
    with selected_model_scope(host, thread_id=tid):
        parent_config = host.config
        with canonical_subagent_model_scope(host, task.id):
            assert host.config.model_name == "default"
            assert host.backend.model_name == "default"
            assert host.config is not parent_config
        assert host.config is parent_config


@pytest.mark.parametrize("source", ["inherited", "explicit", "automatic"])
def test_worker_process_resolves_canonical_child_not_stale_spawn_ref(family, source):
    host, _, explicit, default, _ = family
    task, thread = canonical_child(host, explicit, default, source, "worker-" + source)
    # 新解释器没有父 ContextVar；经真实 worker builder 和目录解析叶构造最终 worker。
    script = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.agent_core.runner import worker as runner
from agent_py_agent.tests.test_model_profiles import Host
from agent_py_agent.agent.conversation.store import ConversationStore
host = Host(Path(sys.argv[1]))
host.conversation_store = ConversationStore(Path(sys.argv[2]))
task = SimpleNamespace(attributes=json.loads(sys.argv[4]), agent_thread_id=sys.argv[3])
host.subagents = SimpleNamespace(owner_id="alice", workspace_root=Path(sys.argv[2]))
runner._worker_configuration_context = lambda *_: host
runner.authorize_operation = lambda *_: task
class Worker:
    def __init__(self, config, root, **kwargs):
        self.config = config
        self.subagents = host.subagents
params = runner.RunSubagentWorkerParams(host.config, Path(sys.argv[2]), "child", "", True, 4, False, "")
worker = runner._build_worker_agent(Worker, params)
print(json.dumps({"model": worker.config.model_name, "origin": runner.__file__}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(host.home_paths.config_dir),
                             str(host.conversation_store.storage.root), thread.thread_id, json.dumps(task.attributes)],
                            capture_output=True, text=True, timeout=20,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
                            cwd=Path(__file__).resolve().parents[2])
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["model"] == "parent"
    assert Path(observed["origin"]).resolve().is_relative_to(Path(__file__).resolve().parents[2])


def canonical_child(host, profile_id, stale_profile_id, source, name):
    task, thread = child(host, {"host_model_profile.v1": {"profile_id": profile_id}}, name)
    if source != "inherited":
        host.conversation_store.threads.update_atomic(thread.thread_id, lambda row: replace(
            row, model_selection_revision=row.model_selection_revision + 1, model_selection_source=source,
            model_selection_last_explicit_revision=(row.model_selection_revision + 1 if source == "explicit" else 0),
        ))
    # 恢复时旧 task 引用不能覆盖已经存在的 canonical child 选择。
    task.attributes = {"host_model_profile.v1": {"profile_id": stale_profile_id}}
    return task, thread


@pytest.mark.parametrize("identity", ["scoped", "legacy"])
@pytest.mark.parametrize("source", ["inherited", "explicit", "automatic"])
@pytest.mark.parametrize("binding", ["unbound", "scheduler_fixed", "scheduler_inherit"])
def test_recursive_spawn_uses_registered_direct_parent_not_root_link(family, identity, source, binding):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.settings.model_scope import model_selection_snapshot

    host, root_tid, root_profile, parent_profile, _ = family
    parent, parent_thread = canonical_child(host, parent_profile, root_profile, source, "recursive-parent")
    # 根会话链接只拥有工作目录；即使暂存线程字段或模型引用过时，也不能覆盖直接父线程。
    host._current_run_params.task_attributes["agent_thread_id"] = root_tid
    scope = nullcontext()
    if binding != "unbound":
        config = selected_model_config(host, profile_id=root_profile)
        snapshot = replace(model_selection_snapshot(host.conversation_store.threads.require(root_tid), config),
                           selection_source=binding, selection_thread_id=root_tid)
        dependencies = replace(prepare_model_dependencies(host, config, inherited=True), selection=snapshot)
        scope = model_dependencies_scope(host, dependencies)
    previous = None
    if identity == "scoped":
        previous = set_current_subagent_context(host, run_id=parent.id, task_attributes=host._current_run_params.task_attributes)
    else:
        host._current_subagent_run_id = parent.id
    try:
        with scope:
            attrs = create_task_attributes({}, host)
        assert attrs["host_model_profile.v1"] == {"profile_id": parent_profile}
        assert host.conversation_store.threads.require(parent_thread.thread_id).model_profile_id == parent_profile
    finally:
        if previous is not None:
            restore_current_subagent_context(host, previous)
        else:
            del host._current_subagent_run_id


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("decision_enabled", [False, True])
def test_real_create_records_label_mismatch_once(request, monkeypatch, caplog, nested, decision_enabled):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.tests.test_decision_subagent import advice, created, provider

    creation_family = request.getfixturevalue("prepared")
    host, (label_profile, _), _ = creation_family
    from agent_py_agent.agent.agent_core.orchestration import decision_subagent

    original_prepare = decision_subagent._prepare

    def checked_prepare(*args, **kwargs):
        try:
            return original_prepare(*args, **kwargs)
        except Exception:
            import traceback

            pytest.fail(traceback.format_exc())

    monkeypatch.setattr(decision_subagent, "_prepare", checked_prepare)
    if not decision_enabled:
        from agent_py_agent.tests.test_decision_settings import patch

        patch(host, {"enabled": False})
    if nested:
        parent = host.subagents.create_run(goal="检查父任务", thought="分工", plan=["检查"],
            allowed_tools=["create_subagents"], attributes={"host_model_profile.v1": {"profile_id": "default"}})
        host._current_subagent_run_id = parent.id
        host._current_run_params.run_id = parent.id
    seen = provider(monkeypatch, creation_family, {"0": label_profile})
    host.config.config_sources = {"model_name": {"source": "owner_model_profile", "profile_id": label_profile}}
    with caplog.at_level(logging.WARNING):
        tasks = created(host, CreateSubagentsTool(host).execute({"items": [{"goal": "检查继承", "agent_name": "inherit-check"}]}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert type(tasks[0].attributes["host_model_profile.v1"]) is dict
    assert "host_model_inheritance_diagnostic.v1" not in tasks[0].attributes
    assert len(seen) == int(decision_enabled)
    if decision_enabled:
        assert advice(host, tasks[0])["status"] == "pending"
    records = [record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]
    assert len(records) == 1
    # 恢复已存在的线程不能重发；另一次真实创建仍须独立发一条，不能跨操作缓存。
    ensure_subagent_thread(host.subagents, tasks[0])
    assert len([record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]) == 1
    with caplog.at_level(logging.WARNING):
        created(host, CreateSubagentsTool(host).execute({"items": [{"goal": "检查另一个任务", "agent_name": "inherit-again"}]}))
    assert len([record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]) == 2


def test_detached_scheduler_fixed_binding_keeps_frozen_profile(family, caplog):
    from agent_py_agent.agent.settings.model_scope import model_selection_snapshot

    host, tid, _, _, later = family
    host._current_run_params = None
    config = selected_model_config(host, profile_id=later)
    snapshot = replace(model_selection_snapshot(host.conversation_store.threads.require(tid), config),
                       selection_source="scheduler_fixed", scheduler_run_id="scheduled-run")
    dependencies = replace(prepare_model_dependencies(host, config, inherited=True), selection=snapshot)
    with caplog.at_level(logging.WARNING), model_dependencies_scope(host, dependencies):
        attrs = create_task_attributes({}, host)
        task, thread = child(host, attrs, "scheduler-child")
    assert attrs["host_model_profile.v1"] == {"profile_id": later}
    assert thread.model_profile_id == later
    assert inherited_model_config(host, task).model_name == "later"
    assert not [record for record in caplog.records if "MODEL_INHERITANCE_LABEL_MISMATCH" in record.getMessage()]


@pytest.mark.parametrize("mode", ["isolated", "followup"])
@pytest.mark.parametrize("policy", ["fixed", "inherit"])
def test_scheduled_main_runparams_inherit_policy_without_initializing_execution_thread(
    family, tmp_path, scheduler_fd_balance, mode, policy,
):
    from agent_py_agent.agent.conversation.runtime import _run_params, _run_request
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.scheduler.execution import scheduled_model_scope
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRepository,
    )
    from agent_py_agent.agent.scheduler.service import SchedulerService
    from agent_py_agent.agent.settings.model_scope import current_model_selection

    host, origin_id, origin_profile, owner_default, fixed_profile = family
    store = host.conversation_store
    repository = SchedulerRepository(tmp_path / "scheduler", owner_provider="local", owner_kind="user", owner_id="alice")
    host.scheduler_repository = repository
    service = SchedulerService(repository, conversation_store=store)
    execution = {"mode": mode, "model_policy": policy, "profile_id": fixed_profile if policy == "fixed" else ""}
    job = repository.create_job(SchedulerJobCreateRequest(
        name="检查定时继承", prompt="检查自己的资料", thread_id=origin_id, execution=execution,
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"}, now=900,
    ))[0]
    wake_ids = service.enqueue_ready_runs(now=1000)
    assert len(wake_ids) == 1
    signal = store.wakes.pending_one(wake_ids[0])
    claim = service.claim_wake(signal, now=1000).claim
    assert claim is not None
    run = repository.get_active_run(signal.root_task_id)
    assert run["job_id"] == job["job_id"] and run["execution"] == execution
    execution_thread = store.threads.require(run["thread_id"])
    if mode == "isolated":
        assert execution_thread.thread_id != origin_id and execution_thread.model_profile_id == ""
    else:
        assert execution_thread.thread_id == origin_id and execution_thread.model_profile_id == origin_profile
    request = _run_request({"thread_id": signal.thread_id, "task_id": signal.root_task_id,
                            "reason": signal.reason, "wake_signal": signal.to_dict(), "now": 1000})
    expected = fixed_profile if policy == "fixed" else origin_profile
    assert expected != owner_default
    original_params = host._current_run_params
    with selected_model_scope(host, thread_id=origin_id):
        outer_config, outer_selection = host.config, current_model_selection(host)
        try:
            with scheduled_model_scope(host, execution_thread, request):
                selection = current_model_selection(host)
                assert selection.selection_source == "scheduler_" + policy
                assert selection.profile_id == expected
                host._current_run_params = _run_params(signal.thread_id, request, host, thread=execution_thread)
                assert host._current_run_params.task_attributes["conversation_thread_id"] == signal.thread_id
                assert host._current_run_params.task_id == run["run_id"]
                attrs = create_task_attributes({}, host)
                task, thread = child(host, attrs, "scheduled-child")
                assert attrs["host_model_profile.v1"] == {"profile_id": expected}
                assert thread.model_profile_id == expected
                assert inherited_model_config(host, task).model_name == ("later" if policy == "fixed" else "parent")
                explicit_attrs = create_task_attributes({"model": "default"}, host)
                assert explicit_attrs["host_model_profile.v1"] == {"profile_id": owner_default}
                persisted = ConversationStore(store.storage.root)
                assert persisted.threads.require(thread.thread_id).model_profile_id == expected
                persisted_execution = persisted.threads.require(execution_thread.thread_id)
                assert persisted_execution.model_profile_id == execution_thread.model_profile_id
                assert persisted_execution.model_selection_revision == execution_thread.model_selection_revision
                assert persisted_execution.model_selection_source == execution_thread.model_selection_source
        finally:
            host._current_run_params = original_params
            assert service.release(claim, now=1001)
        assert host.config is outer_config and current_model_selection(host) is outer_selection
    assert current_model_selection(host) is None
