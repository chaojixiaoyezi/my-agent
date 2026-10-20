# LLM: 临时home、真实原锁与纯fake身份观察；不创建Agent/服务/模型/native进程，不代替实际迁移验收。
# 模块用途: 贯通正常Count0停止到原C publisher精确迁移，保历史与未来输入。
import base64
import copy
import json
import os
import time
from argparse import Namespace
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread, ThreadTaskLink
from agent_py_agent.agent.gateway_parts import standalone_inventory as inventory
from agent_py_agent.agent.gateway_parts import standalone_migration as migration
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.cli import common, gateway_process
from agent_py_agent.cli.gateway_control_plane import build_control_plane
from agent_py_agent.cli.gateway_coordinated_run import (
    _cold_gateway_paths,
    publish_coordinated_state,
)
from agent_py_agent.tests.test_process_session_store import _running


# LLM: 只在tmp夹具写原canonical格式；生产被测reader不可调用会恢复redo/刷新的Store。
# 函数用途: 留历史、已结native、future输入，随后以支持版原writer保存真实停止来源。
def stopped_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_path = tmp_path / "agent.yaml"
    def configure(count):
        config_path.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_worker_process_count: {count}\n')
        args = Namespace(config=config_path, workspace_root="")
        return args, common.agent_configuration(args)[0]
    args, config = configure(0)
    paths = _cold_gateway_paths(args)
    owner = resolve_owner_home(home)
    owner.home_dir.mkdir(parents=True)
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    ids = repo.record_run_creation(owner_id=owner.owner_id, conversation_task_id="task-old", thread_id="thread-old", run_id="run-old")
    assert repo.settle_agent_attempt(agent_run_id=ids["agent_run_id"], attempt_id=ids["attempt_id"])["settled"]
    # 原TaskRun/AgentRun保持created，attempt已结；future输入仍有原task/run/thread refs。
    task_id = ids["task_id"]
    repo.upsert_wake(root_task_id=task_id, root_run_id="run-old", root_thread_id="thread-old", next_due_at=time.time()+86400)
    roots = inventory._conversation_roots(home, owner, config, {})
    root = next(root for root in roots if "workspaces" in root.parts)
    link = ThreadTaskLink("thread-old", task_id, "history", input_dependency_refs=("input-old",))
    thread = ConversationThread("thread-old", "local-user", owner_id=owner.owner_id, owner_home=str(owner.home_dir))
    for directory, name, row in (("tasks", task_id, link.to_dict()), ("threads", "thread-old", thread.to_dict())):
        target=root/directory/(name+".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(row))
    row={"schema_version":"task_input.v1", "input_id":"input-old", "provider":"test", "owner":asdict(owner.identity),
         "scope":{"agent_kind":"main", "run_id":"run-old", "root_task_id":task_id, "thread_id":"thread-old"},
         "status":"pending", "armed":True, "expires_at":time.time()+86400}
    target=root/"session_tasks"/"inputs"/(sha256(b"input-old").hexdigest()+".json")
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(row))
    native = _running("bg-history", status="exited", finished_at=3.0, exit_code=0, stop_requested=True,
        access_scope={"owner_id":owner.owner_id, "conversation_id":"thread-old", "owner_home":str(owner.home_dir)},
        execution_scope={"owner_home":str(owner.home_dir), "thread_id":"thread-old", "root_task_id":task_id,
                         "run_id":"run-old", "attempt_id":ids["attempt_id"]},
        termination={"cleanup":{"confirmed":True, "instances":[{"method":"not_running", "confirmed":True,
                    "return_code":0, "observed_processes":3, "unresolved_pids":[]}]}})
    native_root=process_session_store_root(owner.home_dir, owner.home_dir)
    ProcessSessionStore(native_root).write(native)
    monkeypatch.setattr(inventory, "_process_instance_terminated", lambda *_: True)
    monkeypatch.setattr(migration, "process_identity_is_live", lambda *_: False)
    identity={"host_id":"fixture-host", "pid":os.getpid(), "start_time":"fixture-birth"}
    context=SimpleNamespace(paths=paths, config=config, config_path=config_path, process_identity=identity,
        process_started_at=1.0, coordinator_worker_count=0, control_plane=None)
    cleanup={"drain_complete":True,"alive_threads":[],"interrupted_model_calls":0,
             "host_shutdown_executors":0,"surviving_background_sessions":[]}
    with standalone_lifecycle_guard(paths.root) as held:
        monkeypatch.setattr(gateway_process, "log_gateway_event", lambda *_: None)
        gateway_process._record_gateway_service_termination(paths, None, os.getpid(),
            gateway_process._GatewayServiceTermination("stopped", "planned_stop", "fixture", "", 0), context=context)
        fact=record_standalone_stop(context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    args, config=configure(2)
    return SimpleNamespace(home=home, paths=paths, args=args, config=config, owner=owner, repo=repo, ids=ids,
        input=target, native_root=native_root, context=context, old_raw=paths.state.read_bytes(), configure=configure)


def test_normal_standalone_history_native_and_future_input_migrate(tmp_path, monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    before=case.input.read_bytes()
    initial=inventory.standalone_inventory(case.home,case.config,case.paths)
    with coordinator_guard(case.paths.root,2):
        controller=prepare_coordinator_takeover(case.paths,2,"new-generation",config=case.config,config_path=case.args.config)
        assert not controller._all_quarantined
        factory=Mock()
        control=build_control_plane(case.args,case.paths,controller,factory)
        context=SimpleNamespace(control_plane=control,paths=case.paths,coordinator_worker_count=2)
        publish_coordinated_state(context,{"status":"starting","pid":os.getpid()})
        state=json.loads(case.paths.state.read_text())
        assert state["c_run_id"]=="new-generation" and state["worker_count"]==2
        archive=json.loads((case.paths.root/state["standalone_migration_ref"]).read_text())
        assert base64.b64decode(archive["source_raw_base64"])==case.old_raw
        assert archive["state"]=="prepared" and "closure" not in archive
        launched=[]
        assert controller.launch_owner_writer(owner_digest(case.owner.identity), lambda: launched.append("owner"))["status"]=="started"
        assert controller.launch_worker(0, lambda: launched.append("worker"))["status"]=="started"
        assert launched==["owner","worker"]
        factory.assert_not_called()
        assert case.input.read_bytes()==before
        assert inventory.standalone_inventory(case.home,case.config,case.paths)==initial
    assert not (case.paths.root/"generations").exists()


def _prepare(case, generation="new-generation"):
    return prepare_coordinator_takeover(case.paths,2,generation,config=case.config,config_path=case.args.config)


@pytest.mark.parametrize("live", [True,None])
def test_alive_unknown_old_instance_cannot_start(tmp_path,monkeypatch,live):
    case=stopped_home(tmp_path,monkeypatch)
    monkeypatch.setattr(migration,"process_identity_is_live",lambda *_:live)
    with coordinator_guard(case.paths.root,2), pytest.raises(migration.StandaloneMigrationRejected,match="previous_instance"):
        _prepare(case)
    assert case.paths.state.read_bytes()==case.old_raw


@pytest.mark.parametrize("damage", ["missing_birth","wrong_home","blocker","mixed_mp","native_unknown","native_alive","redo","orphan_native","runtime_busy","unknown_operation","processing","unsafe_input"])
def test_unsafe_or_unsettled_source_never_migrates(tmp_path,monkeypatch,damage):
    case=stopped_home(tmp_path,monkeypatch)
    raw=case.paths.state.read_bytes()
    state=json.loads(raw)
    if damage=="missing_birth":
        del state["process_identity"]["start_time"]
        state["standalone_stop"]["process_identity"]=state["process_identity"]
    elif damage=="wrong_home":
        state["standalone_stop"]["home"]=str(tmp_path/"other")
    elif damage=="blocker":
        state["standalone_stop"]["blockers"]=[{"reason":"not_confirmed","ref":"native"}]
    elif damage=="mixed_mp":
        worker=case.paths.root/"workers"/"w0.json"
        worker.parent.mkdir(parents=True)
        worker.write_text(json.dumps({"c_run_id":"old-mp"}))
    elif damage=="native_unknown":
        path=case.native_root/"bg-history.json"
        row=json.loads(path.read_text())
        row["status"]="unknown"
        path.write_text(json.dumps(row))
    elif damage=="native_alive":
        monkeypatch.setattr(inventory,"_process_instance_terminated",lambda *_:False)
    elif damage=="redo":
        (case.native_root/".process-sessions.redo.json").write_text("{}")
    elif damage=="orphan_native":
        (case.native_root.parent/"unbound-owner").mkdir()  # 准确未绑定digest目录，不能被owner枚举漏掉。
    elif damage=="runtime_busy":
        with case.repo._runtime_connection() as conn:
            conn.execute("UPDATE agent_attempts SET status='running',ended_at=0")
            conn.commit()
    elif damage=="unknown_operation":
        with case.repo._runtime_connection() as conn:
            conn.execute("INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,tool_operation_generation,operation_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("unknown-op",case.ids["agent_run_id"],case.ids["attempt_id"],1,1,"file_write","UNKNOWN",1,1))
            conn.commit()
    elif damage=="processing":
        case.paths.processing.mkdir(parents=True)
        (case.paths.processing/"gwreq-unsettled.json").write_text("{}")
    elif damage=="unsafe_input":
        saved=case.input.read_bytes()
        case.input.unlink()
        target=tmp_path/"outside.json"
        target.write_bytes(saved)
        case.input.symlink_to(target)
    if damage in {"missing_birth","wrong_home","blocker"}:
        write_json_file_atomic(case.paths.state,state)
        raw=case.paths.state.read_bytes()
    with coordinator_guard(case.paths.root,2), pytest.raises(migration.StandaloneMigrationRejected):
        _prepare(case)
    assert case.paths.state.read_bytes()==raw


@pytest.mark.parametrize("change", ["state","config","input","lease_origin","source_clone","source_cleared","source_duck"])
def test_source_changes_before_original_first_publish_are_rejected(tmp_path,monkeypatch,change):
    case=stopped_home(tmp_path,monkeypatch)
    with coordinator_guard(case.paths.root,2):
        controller=_prepare(case)
        control=build_control_plane(case.args,case.paths,controller,Mock())
        context=SimpleNamespace(control_plane=control,paths=case.paths,coordinator_worker_count=2)
        if change=="state":
            state=json.loads(case.old_raw)
            state["later_change"]=True
            write_json_file_atomic(case.paths.state,state)
        elif change=="config":
            case.args.config.write_text(case.args.config.read_text()+"# changed\n")
        elif change=="input":
            row=json.loads(case.input.read_text())
            row["armed"]=False
            case.input.write_text(json.dumps(row))
        elif change=="lease_origin":
            controller._standalone_origin=copy.copy(controller._standalone_origin)
        elif change=="source_clone":
            controller._standalone_migration=copy.copy(controller._standalone_migration)
        elif change=="source_cleared":
            controller._standalone_migration=None
        else:
            controller._standalone_migration=SimpleNamespace(_committed=True,reference="fake.json")
        before=case.paths.state.read_bytes()
        with pytest.raises(migration.StandaloneMigrationRejected):
            publish_coordinated_state(context,{"status":"starting","pid":os.getpid()})
        assert case.paths.state.read_bytes()==before


def test_prepared_preimage_is_not_authority_and_restarts_with_fresh_gate(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    original=migration.update_json_file_atomic
    with coordinator_guard(case.paths.root,2):
        controller=_prepare(case)
        control=build_control_plane(case.args,case.paths,controller,Mock())
        context=SimpleNamespace(control_plane=control,paths=case.paths,coordinator_worker_count=2)
        monkeypatch.setattr(migration,"update_json_file_atomic",Mock(side_effect=OSError("fake interruption")))
        with pytest.raises(OSError):
            publish_coordinated_state(context,{"status":"starting","pid":os.getpid()})
        assert not controller._standalone_migration._committed
    assert case.paths.state.read_bytes()==case.old_raw
    archives=list((case.paths.root/"standalone_migrations").glob("*.json"))
    assert len(archives)==1
    monkeypatch.setattr(migration,"update_json_file_atomic",original)
    monkeypatch.setattr(migration,"process_identity_is_live",lambda *_:None)
    with coordinator_guard(case.paths.root,2),pytest.raises(migration.StandaloneMigrationRejected):
        _prepare(case)
    monkeypatch.setattr(migration,"process_identity_is_live",lambda *_:False)
    case.paths=gateway_paths_from_root(case.paths.root)
    with coordinator_guard(case.paths.root,2):
        controller=_prepare(case)
        control=build_control_plane(case.args,case.paths,controller,Mock())
        publish_coordinated_state(SimpleNamespace(control_plane=control,paths=case.paths,coordinator_worker_count=2),
                                  {"status":"starting","pid":os.getpid()})
        assert controller._standalone_migration._committed


def test_successor_read_is_non_mutating_and_missing_sqlite_facility_refuses(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    def files():
        return {str(path.relative_to(case.home)):(path.stat().st_mode,sha256(path.read_bytes()).hexdigest())
                for path in case.home.rglob("*") if path.is_file()}
    before=files()
    inventory.standalone_inventory(case.home,case.config,case.paths)
    assert files()==before
    shm=runtime_db_path(case.owner.home_dir).with_name("runtime.db-shm")
    assert shm.exists()
    shm.unlink()
    before=files()
    with coordinator_guard(case.paths.root,2),pytest.raises(migration.StandaloneMigrationRejected,match="facilities_missing"):
        _prepare(case)
    assert files()==before and not shm.exists()


def test_stop_writer_keeps_unknown_inventory_as_blocker(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    case.configure(0)
    monkeypatch.setattr(inventory,"_process_instance_terminated",lambda *_:False)
    context=case.context
    context.config=common.agent_configuration(case.args)[0]
    with standalone_lifecycle_guard(case.paths.root) as held:
        result=record_standalone_stop(context,held,{"drain_complete":True,"alive_threads":[],
             "interrupted_model_calls":0,"host_shutdown_executors":0,"surviving_background_sessions":[]})
    assert result["standalone_stop"]["sources"] is None
    assert result["standalone_stop"]["blockers"]==[{"reason":"native_instance_alive_or_unknown","ref":"bg-history"}]
    assert "physical_quiet" not in result["standalone_stop"]


def test_original_state_cas_rejects_change_after_preimage_is_saved(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    original=migration.update_json_file_atomic
    def conflict(path,update,**kwargs):
        row=json.loads(path.read_text())
        row["concurrent_revision"]=1
        write_json_file_atomic(path,row)
        return original(path,update,**kwargs)
    with coordinator_guard(case.paths.root,2):
        controller=_prepare(case)
        control=build_control_plane(case.args,case.paths,controller,Mock())
        monkeypatch.setattr(migration,"update_json_file_atomic",conflict)
        with pytest.raises(migration.StandaloneMigrationRejected,match="source_or_configuration_changed"):
            publish_coordinated_state(SimpleNamespace(control_plane=control,paths=case.paths,coordinator_worker_count=2),
                                      {"status":"starting","pid":os.getpid()})
        assert not controller._standalone_migration._committed
        assert json.loads(case.paths.state.read_text())["concurrent_revision"]==1
        assert "c_run_id" not in json.loads(case.paths.state.read_text())


def test_supported_stop_json_without_actual_variant_does_not_authorize(tmp_path,monkeypatch):
    case=stopped_home(tmp_path,monkeypatch)
    launched=[]
    with coordinator_guard(case.paths.root,2):
        controller=prepare_coordinator_takeover(case.paths,2,"no-captured-variant")
        assert controller._all_quarantined
        assert controller.launch_owner_writer(owner_digest(case.owner.identity),lambda:launched.append(1))["status"]!="started"
        assert launched==[]
        assert case.paths.state.read_bytes()==case.old_raw
