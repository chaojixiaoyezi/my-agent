# LLM: Wave5使用合成进程/通道及临时SQLite/请求目录，不发信号、不启动插件服务或真实IM投递。
# 模块用途: 锁定缓存、冷预检、故障告警、阶段账及三类安全结清边界。
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata as dm
from agent_py_agent.agent.tooling import plugin_registration as plugins


# LLM: 每次隔离缓存并保留原查询注入点，合成PID不触碰真实进程。
# 函数用途: 提供可控的Darwin出生观察和ps调用计数。
@pytest.fixture
def darwin_observer(monkeypatch):
    dm.clear_process_start_time_cache()
    monkeypatch.setattr(dm.sys, "platform", "darwin")
    pid = [100]
    births = {200: "1000:000001"}
    calls = []
    monkeypatch.setattr(dm.os, "getpid", lambda: pid[0])
    monkeypatch.setattr(dm, "_darwin_proc_birth_token", lambda target: births.get(target))

    # LLM: 不调用真实ps，输出差异仅表示不同观察次序。
    # 函数用途: 计数并返回合成的非空旧口径值。
    def query(target):
        calls.append(target)
        return f"Mon Oct 26 06:00:00 2026 {len(calls)}"

    monkeypatch.setattr(dm, "_query_ps_start_time", query)
    yield pid, births, calls
    dm.clear_process_start_time_cache()


# LLM: 合成PID变化模拟fork边界，无结果不能进入永久自身缓存。
# 函数用途: 验证自身成功值复用、PID换代失效与失败不缓存。
def test_b5_self_cache_is_fork_safe_and_only_keeps_success(darwin_observer, monkeypatch):
    pid, _, calls = darwin_observer
    first = dm._get_process_start_time(100)
    assert dm._get_process_start_time(100) == first
    assert calls == [100]
    pid[0] = 101
    assert dm._get_process_start_time(101) != first
    assert calls == [100, 101]
    dm.clear_process_start_time_cache()
    monkeypatch.setattr(dm, "_query_ps_start_time", lambda target: None)
    assert dm._get_process_start_time(101) is None
    assert dm._SELF_PROCESS_START_TIME == {}


# LLM: PID相同但kernel出生版本不同必须重新观察ps，不能比较两种源字符串。
# 函数用途: 验证外部进程缓存按准确出生代次失效。
def test_b5_kernel_birth_invalidates_cached_legacy_value(darwin_observer):
    _, births, calls = darwin_observer
    before = dm._get_process_start_time(200)
    assert dm._get_process_start_time(200) == before
    assert calls == [200]
    births[200] = "1000:000002"
    assert dm._get_process_start_time(200) != before
    assert calls == [200, 200]


# LLM: 确认ESRCH后不再派生ps，合成观察不访问操作系统进程。
# 函数用途: 核对已死PID的零ps路径。
def test_b5_dead_native_observation_does_not_spawn_ps(darwin_observer, monkeypatch):
    # LLM: 仅抛受控死亡观察，不发信号。
    # 函数用途: 模拟原生ESRCH结果。
    def dead(target):
        raise ProcessLookupError()

    monkeypatch.setattr(dm, "_darwin_proc_birth_token", dead)
    assert dm._get_process_start_time(200) is None
    assert darwin_observer[2] == []


# LLM: 展示TTL命中不能代替身份核验的新观察；不做真实信号调用。
# 函数用途: 验证同PID短TTL也不能授权旧身份。
def test_b5_ttl_is_never_identity_authority(darwin_observer, monkeypatch):
    _, births, calls = darwin_observer
    births.clear()
    token = dm._FRESH_PROCESS_OBSERVATION.set(False)  # 明确模拟只读缓存观察，不是授权读口。
    try:
        first = dm._get_process_start_time(200)
        assert dm._get_process_start_time(200) == first
        assert calls == [200]
        monkeypatch.setattr(dm, "process_host_id", lambda: "test-host")
        monkeypatch.setattr(dm.os, "kill", lambda *_: None)
        monkeypatch.setattr(dm, "_process_start_observation", lambda pid, recorded=None: dm._get_process_start_time(pid))
        assert dm.process_identity_is_live({"pid": 200, "host_id": "test-host", "start_time": first}) is False
        assert calls == [200, 200]
    finally:
        dm._FRESH_PROCESS_OBSERVATION.reset(token)


# LLM: 原scoped_locks直接调用_get读口也必须新观察，不能只保护统一liveness包装器。
# 函数用途: 验证缺kernel token时默认直接读口不会以TTL参与锁接管判定。
def test_b5_scoped_lock_direct_observer_never_uses_ttl(darwin_observer, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    _, births, calls = darwin_observer
    births.clear()
    first = dm._get_process_start_time(200)
    monkeypatch.setattr(scoped_locks, "is_pid_alive", lambda _: True)
    assert scoped_locks._lock_process_stale({"pid": 200, "start_time": first}) is True
    assert calls == [200, 200]


# LLM: ps前后原生出生代改变时不能把跨代观察缓存到任一代。
# 函数用途: 验证PID复用竞态失败关闭。
def test_b5_observation_race_does_not_cache_wrong_birth(darwin_observer, monkeypatch):
    tokens = iter(["1000:000001", "1000:000002"])
    monkeypatch.setattr(dm, "_darwin_proc_birth_token", lambda target: next(tokens))
    assert dm._get_process_start_time(200) is None
    assert dm._PROCESS_START_TIME_CACHE == {}


# LLM: 保留真实stat版本判断，假Store只计解码次数，不替代激活决策。
# 函数用途: 验证安装表不变复用、变更重读。
def test_b6_snapshot_reuses_parse_and_reloads_changed_installation(tmp_path, monkeypatch):
    path = tmp_path / "installations.json"
    path.write_text("{}")
    owner = SimpleNamespace(plugins_dir=tmp_path)
    rows = [("first",)]
    calls = []

    # LLM: 假Store无文件写，返回受控投影模拟原解码结果。
    # 类用途: 观察安装表缓存是否避开重复解码。
    class Store:
        # LLM: 不构造真实插件服务。
        # 函数用途: 接受同步调用的owner参数。
        def __init__(self, owner):
            pass

        # LLM: 仅返回测试投影，物理stat仍由真实代码检查。
        # 函数用途: 记录一次安装表解码调用。
        def snapshot(self):
            calls.append(True)
            return rows[0]

    plugins._INSTALLATION_SNAPSHOTS.clear()
    monkeypatch.setattr(plugins, "PluginInstallStore", Store)
    assert plugins._discover_plugin_manifests(owner)[0] == ("first",)
    assert plugins._discover_plugin_manifests(owner)[0] == ("first",)
    assert len(calls) == 1
    rows[0] = ("disabled",)
    path.write_text('{"revision":2}')
    assert plugins._discover_plugin_manifests(owner)[0] == ("disabled",)
    assert len(calls) == 2
    plugins._INSTALLATION_SNAPSHOTS.clear()


# LLM: 环境目录及激活身份变化均须失效，负缓存日志按代去重且有期限。
# 函数用途: 验证缺环境重试边界和单次告警。
def test_b6_negative_cache_tracks_environment_and_activation(tmp_path, monkeypatch, caplog):
    owner = SimpleNamespace(home_dir=tmp_path, plugins_dir=tmp_path)
    install = SimpleNamespace(manifest=SimpleNamespace(plugin_id="test-plugin"), revision=1,
                              activation_id="activation-one",
                              activation=SimpleNamespace(plan=SimpleNamespace(environment_ref="env-one")))
    first = plugins._plugin_environment_key(owner, install, (1, 2))
    assert first[-1] == (False,)
    plugins._MISSING_ENVIRONMENTS.clear()
    plugins._MISSING_ENV_WARNED.clear()
    monkeypatch.setattr(plugins.time, "monotonic", lambda: 100.0)
    plugins._remember_missing_environment(first)
    plugins._remember_missing_environment(first)
    assert plugins._MISSING_ENVIRONMENTS[first] == 130.0
    assert len(caplog.records) == 1
    (tmp_path / "environments" / "env-one").mkdir(parents=True)
    restored = plugins._plugin_environment_key(owner, install, (1, 2))
    assert restored != first and restored not in plugins._MISSING_ENVIRONMENTS
    install.activation_id = "activation-two"
    assert plugins._plugin_environment_key(owner, install, (1, 2)) != restored
    plugins._MISSING_ENVIRONMENTS.clear()
    plugins._MISSING_ENV_WARNED.clear()


# LLM: 使用临时 owner 和原策略/state 解析器，不构造完整 Agent；坏输入必须 unknown。
# 函数用途: 核对空态、紧急触发、配置关闭与损坏的预检结果。
def test_b6_cold_precheck_empty_due_disabled_unknown(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli import gateway_loops as loops

    owner = OwnerIdentity.provider_user("test", "cold")
    home = loops.resolve_owner_home(tmp_path, owner).home_dir
    config = SimpleNamespace(memory_curator_enabled=True, enable_self_learning=False, timezone="")
    supervisor = SimpleNamespace(_base_agent=SimpleNamespace(config=config, home_paths=SimpleNamespace(root=tmp_path)))
    assert loops._cold_curator_precheck(supervisor, owner) == ("not_due", False)
    (home / "memory" / "curator").mkdir(parents=True)
    (home / "memory" / "curator" / "state.json").write_text('{"pending_reasons":["admin"]}')
    assert loops._cold_curator_precheck(supervisor, owner) == ("due", True)
    (home / "memory_policy.json").write_text('{"enabled":false}')
    assert loops._cold_curator_precheck(supervisor, owner) == ("not_due", False)
    (home / "memory_policy.json").write_text('broken')
    assert loops._cold_curator_precheck(supervisor, owner) == ("unknown", False)


# LLM: 保留真实候选循环，假池只观测构造次数；unknown 不能缓存成无任务。
# 函数用途: 核对单拍两次冷构建上限和下拍公平游标。
def test_b6_cold_build_budget_and_cursor(monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops

    built = []
    pool = SimpleNamespace(peek=lambda owner: None, get=lambda owner, **kw: built.append(owner) or object())
    supervisor = SimpleNamespace(_curator_candidate_cursor=0)
    monkeypatch.setattr(loops, "_cold_curator_precheck", lambda *args: ("unknown", False))
    monkeypatch.setattr(loops, "_curator_candidate_is_admitted", lambda *args, **kwargs: False)
    candidates = [("owner", str(n)) for n in range(5)]
    index = loops._submit_due_curators(supervisor, candidates, 5, set(), pool)
    assert built == ["0", "1"] and index == 2
    supervisor._curator_candidate_cursor = index
    loops._submit_due_curators(supervisor, candidates, 5, set(), pool)
    assert built == ["0", "1", "2", "3"]


# LLM: 受控时钟仅替代计时，不替代真实冷候选分支。
# 函数用途: 验证保护期抑制普通冷构建，却允许紧急恢复。
def test_c2_startup_suppresses_cold_not_urgent(monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops

    monkeypatch.setattr(loops.time, "monotonic", lambda: 100.0)
    calls = []
    pool = SimpleNamespace(peek=lambda _: None, get=lambda owner, **kw: calls.append(owner) or object())
    supervisor = SimpleNamespace(_startup_started_at=0, _curator_cold_builds=0)
    monkeypatch.setattr(loops, "_cold_curator_precheck", lambda *args: ("due", False))
    assert loops._resolve_curator_candidate("owner", "cold", pool, supervisor=supervisor)[0] is None
    assert calls == []
    monkeypatch.setattr(loops, "_cold_curator_precheck", lambda *args: ("due", True))
    assert loops._resolve_curator_candidate("owner", "urgent", pool, supervisor=supervisor)[0] is not None
    assert calls == ["urgent"]


# LLM: 安全清理先于保护期返回，retention 替身被调用即失败。
# 函数用途: 验证启动保护不推迟安全恢复。
def test_c2_retention_grace_preserves_safety_recovery(monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops

    controller = object.__new__(loops._GatewayOwnerMaintenanceController)
    controller._startup_started_at = 0.0
    controller._base_agent = object()
    calls = []
    monkeypatch.setattr(loops.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(loops, "_cleanup_loaded_runtime_safety", lambda agent: calls.append("safety"))
    monkeypatch.setattr(loops, "run_owner_retention_if_due", lambda *a, **k: pytest.fail("retention during grace"))
    assert controller.tick()["scanned"] == 0
    assert calls == ["safety"]


# LLM: 仅构造临时 processing 请求，保留真实T/JSON存储链及不可变metadata。
# 函数用途: 为阶段账准备明确的 transport attempt/epoch。
@pytest.fixture
def stage_request(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders

    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    path = paths.processing / "req.json"
    payload = {"id": "req", "execution_attempt_id": "transport-one", "lease_epoch": 1,
               "status": "processing", "created_at": 1.0, "prompt": "hi", "metadata": {"signed": "unchanged"}}
    path.write_text(json.dumps(payload))
    return path, payload


# LLM: 真实原子更新；旧worker不能写新lease，遥测不能改变业务指纹。
# 函数用途: 核对阶段耗时、不可变内容与代际CAS。
def test_c2_stages_durations_fingerprint_and_stale_epoch(stage_request):
    from agent_py_agent.agent.gateway_parts.io import gateway_request_fingerprint
    from agent_py_agent.agent.gateway_parts.request_binding import (
        record_gateway_request_stage as record,
    )

    path, payload = stage_request
    fingerprint = gateway_request_fingerprint(payload, "req")
    record(path, payload, "dispatched_at", at=2.0)
    record(path, payload, "owner_agent_ready_at", at=3.0)
    record(path, payload, "lock_wait_started_at", at=4.0)
    result = record(path, payload, "lock_acquired_at", at=5.0)
    assert [result[key] for key in ("queue_wait_ms", "agent_init_ms", "lock_wait_ms")] == [1000.0] * 3
    current = json.loads(path.read_text())
    assert gateway_request_fingerprint(current, "req") == fingerprint
    current["lease_epoch"] = 2
    current["execution_attempt_id"] = "transport-two"
    path.write_text(json.dumps(current))
    assert record(path, payload, "attempt_started_at", at=6.0) == {}
    assert json.loads(path.read_text())["execution_attempt_id"] == "transport-two"


# LLM: 没有旧受理时标时不可拿当前dispatch时刻冒充accepted，不伪造零排队。
# 函数用途: 验证历史缺失时间点明确为未知，而不是造出完整链。
def test_c2_legacy_missing_accepted_time_remains_unknown(stage_request):
    from agent_py_agent.agent.gateway_parts.request_binding import record_gateway_request_stage

    path, payload = stage_request
    payload.pop("created_at")
    path.write_text(json.dumps(payload))
    stages = record_gateway_request_stage(path, payload, "dispatched_at", at=2.0)
    assert stages["accepted_at"] is None and "queue_wait_ms" not in stages


# LLM: 实际仓储创建完整链，阶段字典不代替执行身份。
# 函数用途: 核对阶段账落入真实attempt并绑定原请求和epoch。
def test_c2_actual_attempt_binds_request_and_lease(tmp_path):
    from agent_py_agent.agent.runtime_db.repository import (
        RuntimeRepository,
        request_stage_timing_scope,
    )

    repo = RuntimeRepository(tmp_path / "runtime.db")
    stages = {"request_id": "req", "execution_attempt_id": "transport-one", "lease_epoch": 7,
              "accepted_at": 1.0, "dispatched_at": 2.0, "owner_agent_ready_at": 3.0, "lock_acquired_at": 4.0}
    with request_stage_timing_scope(stages):
        result = repo.record_run_creation(owner_id="local/main", run_id="run-timing", role="main")
    meta = json.loads(repo.get_attempt(result["attempt_id"])["metadata_json"])["request_stage_timing"]
    assert meta["attempt_id"] == result["attempt_id"] and meta["lease_epoch"] == 7
    assert meta["request_id"] == "req" and meta["attempt_started_at"] >= meta["lock_acquired_at"]


# LLM: 仅写临时监护状态并控制墙钟，不启动服务或外呼。
# 函数用途: 为持久告警测试准备服务代次和预算。
@pytest.fixture
def fault_supervisor(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import supervisor as module

    paths = SimpleNamespace(state=tmp_path / "gateway.json", supervisor_state=tmp_path / "supervisor.json")
    paths.state.write_text(json.dumps({"process_identity": {"pid": 10, "start_time": "generation-one"}}))
    supervisor = SimpleNamespace(_paths=paths, _restart_count=5, max_restart_attempts=5)
    clock = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    return module, supervisor, clock


# LLM: 假投递只返回受控成功，去重使用真实状态文件，不冒充IM成功。
# 函数用途: 核对跨对象持久去重、慢探针与恢复覆盖。
def test_c1_persistent_fault_cooldown_and_recovery(fault_supervisor, monkeypatch):
    module, supervisor, clock = fault_supervisor
    deliveries = []
    monkeypatch.setattr(module, "_deliver_supervisor_admin_alert", lambda _, fields: deliveries.append(fields["fault_status"]) or True)
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert deliveries == ["restart_budget_exhausted"]
    stored = json.loads(supervisor._paths.supervisor_state.read_text())
    assert stored["alert_notified"] is True and stored["restart_attempts"] == 5
    # 新监护对象复用同一磁盘冷却，不能重新刷同代告警。
    other = SimpleNamespace(**vars(supervisor))
    module._record_supervisor_fault(other, "restart_budget_exhausted")
    module._record_supervisor_fault(other, "slow_probe_failed")
    module._record_supervisor_fault(other, "gateway_recovered", recovered=True)
    module._record_supervisor_fault(other, "gateway_recovered", recovered=True)
    assert deliveries == ["restart_budget_exhausted", "slow_probe_failed", "recovered"]
    assert supervisor._restart_count == 5


# LLM: 投递异常不得改变预算或误记送达，冷却保留原持久事实。
# 函数用途: 核对失败与五分钟重试节流。
def test_c1_delivery_failure_is_not_notified_and_retries(fault_supervisor, monkeypatch):
    module, supervisor, clock = fault_supervisor
    calls = []

    # LLM: 仅模拟通道异常，不触碰外部网络。
    # 函数用途: 记录一次投递调用失败，供冷却断言。
    def fail(*args):
        calls.append(True)
        raise OSError("unconfigured")

    monkeypatch.setattr(module, "_deliver_supervisor_admin_alert", fail)
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    clock[0] += 299
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert len(calls) == 1
    assert json.loads(supervisor._paths.supervisor_state.read_text())["alert_notified"] is False
    clock[0] += 1
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert len(calls) == 2 and supervisor._restart_count == 5


# LLM: 只在 pytest 临时 SQLite 库种合成记录，不访问真实 owner 运行库。
# 函数用途: 构造可验证三类结清边界的旧格式运行。
def _runtime_record(tmp_path, *, orphan=False, unknown=False):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.tests.test_runtime_db_recover_stale import _seed_run

    repo = RuntimeRepository(tmp_path / "runtime.db")
    run = _seed_run(repo, status="unknown" if unknown else "created", metadata={},
                    attempt_status="unknown" if unknown else "running")
    with repo._runtime_connection() as conn:
        conn.execute("UPDATE agent_runs SET created_at=1, updated_at=1 WHERE agent_run_id=?", (run,))
        conn.execute("UPDATE agent_attempts SET started_at=1 WHERE agent_run_id=?", (run,))
        if orphan:
            conn.execute("DELETE FROM agent_attempts WHERE agent_run_id=?", (run,))
            conn.execute("UPDATE agent_runs SET current_attempt_id='' WHERE agent_run_id=?", (run,))
        conn.commit()
    return repo, run


# LLM: 真实SQLite事务与审计；非空悬空指针不能取消。
# 函数用途: 区分未开始孤儿和损坏指针，并核对幂等。
def test_c3_orphan_cancel_and_corrupt_pointer_preserved(tmp_path):
    repo, run = _runtime_record(tmp_path, orphan=True)
    assert repo.unidentified_stale_attempts()[0]["attempt_status"] == "never_created"
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=4000) == [run]
    assert repo.get_agent_run(run)["status"] == "cancelled"
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=4001) == []
    with repo._runtime_connection() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM runtime_events WHERE event_type='agent_run.operational_settlement'").fetchone()[0])
        assert payload["recovery_reason"] == "orphan_created_no_attempt"
        conn.execute("UPDATE agent_runs SET status='created', current_attempt_id='missing-row' WHERE agent_run_id=?", (run,))
        conn.commit()
    assert repo.unidentified_stale_attempts()[0]["attempt_status"] == "missing_attempt_row"
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=4002) == []


# LLM: 运维确认不等于恢复；原create_attempt恢复门仍须拒绝。
# 函数用途: 核对unknown确认归档后不授予重放。
def test_c3_unknown_ack_preserves_business_status_and_replay_gate(tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRecoveryRequiredError

    repo, run = _runtime_record(tmp_path, unknown=True)
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=90000) == [run]
    assert repo.get_agent_run(run)["status"] == "unknown"
    assert repo.current_attempt(run)["status"] == "unknown"
    assert repo.unidentified_stale_attempts() == []
    meta = json.loads(repo.current_attempt(run)["metadata_json"])
    assert meta["operator_settled_at"] == 90000 and "effect_disposition" not in meta
    with pytest.raises(RuntimeRecoveryRequiredError):
        repo.create_attempt(run)


# LLM: 不以无PID或年龄证明副作用未发生。
# 函数用途: 核对24小时门槛及unknown业务审计保留。
def test_c3_auto_thresholds_and_unknown_without_pid_not_noop(tmp_path):
    repo, run = _runtime_record(tmp_path, unknown=True)
    assert repo.cleanup_stale_runtime(now=3600)["settled"] == []
    assert repo.cleanup_stale_runtime(now=90000)["settled"] == [run]
    assert repo.current_attempt(run)["status"] == "unknown"


# LLM: 真实锁行和删除CAS，仅替代进程观察；过期不能代替死亡证明。
# 函数用途: 验证活锁保留、死锁仅一次回收。
def test_c3_expired_live_lock_retained_dead_lock_cas_released(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db import repository as module

    repo, run = _runtime_record(tmp_path, unknown=True)
    attempt = repo.current_attempt(run)
    with repo._runtime_connection() as conn:
        conn.execute("INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,pid,start_token,attempt_id,"
            "attempt_generation,workspace_epoch,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,1,1,1)",
            ("lock-one", "scope:test", "holder", os.getpid(), "birth", attempt["attempt_id"]))
        conn.commit()
    monkeypatch.setattr(module, "holder_is_alive", lambda *args: True)
    assert repo.cleanup_stale_resource_locks(now=90000) == []
    monkeypatch.setattr(module, "holder_is_alive", lambda *args: False)
    assert repo.cleanup_stale_resource_locks(now=90000) == ["lock-one"]
    assert repo.cleanup_stale_resource_locks(now=90000) == []


# LLM: 使用假客户端但调用真实同步链；负缓存不得跳过撤销 stop 的确认门。
# 函数用途: 验证缺环境只构造一次、恢复后重试，撤销仍等旧连接确认关闭。
def test_b6_negative_hit_does_not_bypass_revocation(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    plugins._MISSING_ENVIRONMENTS.clear()
    plugins._MISSING_ENV_WARNED.clear()
    owner = SimpleNamespace(home_dir=tmp_path, plugins_dir=tmp_path / "plugins", identity=OwnerIdentity.local_main())
    install = SimpleNamespace(activation=SimpleNamespace(plan=SimpleNamespace(environment_ref="env")),
        manifest=SimpleNamespace(plugin_id="demo", is_content_only=False), enabled=True, revision="rev", activation_id="act")
    rows = [install]
    calls = []
    closed = [False]

    # LLM: 只模拟客户构建/关闭结果，不替代真实同步的激活、负缓存与撤销判断。
    # 类用途: 提供可观测的缺目录失败与关闭确认。
    class Client:
        # LLM: 测试只读临时目录；构建计数验证真实负缓存的重试预算。
        # 函数用途: 缺环境报 FileNotFoundError，目录恢复后接受真实同步。
        def __init__(self, *args, **kwargs):
            calls.append("construct")
            if not (owner.plugins_dir / "environments" / "env").exists():
                raise FileNotFoundError("missing env")
            self.activation_ref = SimpleNamespace(scope=SimpleNamespace(activation_id="act"))

        # LLM: 模拟真实的前代资源收敛检查成功，不启动任何进程。
        # 函数用途: 允许同步链登记测试客户端。
        def require_settled_previous_resources(self):
            return None

        # LLM: 返回不同 confirmed 值检查原 stop 门没有被缓存跳过。
        # 函数用途: 记录关闭尝试，交回可控确认结果。
        def stop(self):
            calls.append("stop")
            return SimpleNamespace(confirmed=closed[0])

    monkeypatch.setattr(plugins, "PluginMCPClient", Client)
    monkeypatch.setattr(plugins, "_discover_plugin_manifests", lambda owner: (tuple(rows), (1,)))
    registry = SimpleNamespace(_construction_params=SimpleNamespace(plugin_owner=owner, host_credential_env_names=()),
        _mcp_clients=[], _mcp_retry_state={}, _mcp_closed=SimpleNamespace(is_set=lambda: False))
    plugins.synchronize_plugin_clients(registry)
    plugins.synchronize_plugin_clients(registry)
    assert calls == ["construct"]
    (owner.plugins_dir / "environments" / "env").mkdir(parents=True)
    plugins.synchronize_plugin_clients(registry)
    assert calls == ["construct", "construct"] and len(registry._mcp_clients) == 1
    rows.clear()
    plugins.synchronize_plugin_clients(registry)
    assert len(registry._mcp_clients) == 1
    closed[0] = True
    plugins.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [] and calls[-2:] == ["stop", "stop"]
    plugins._MISSING_ENVIRONMENTS.clear()
    plugins._MISSING_ENV_WARNED.clear()


# LLM: 不运行真实进程；通过原启动等待入口核对递进 sleep，退出仍沿原 poll 检查。
# 函数用途: 验证启动轮询从50ms递增，而非固定半秒。
def test_c1_progressive_start_poll(monkeypatch):
    from agent_py_agent.agent.gateway_parts import supervisor as module

    ticks = iter([None, None, 1])
    sleeps = []
    supervisor = SimpleNamespace(_is_gateway_healthy=lambda **kw: False, _gateway_pid=None, _log_error=lambda _: None)
    process = SimpleNamespace(poll=lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert module._wait_for_gateway_start(supervisor, process) is None
    assert sleeps == [0.05, 0.1]


# LLM: 直接调用显式CLI控制，不代替权限入口；即使unknown被确认，工具副作用审计不改变。
# 函数用途: 验证--settle无交互确认，并保留运维清理的结构化结果。
def test_c3_cli_settle_does_not_prompt(tmp_path, monkeypatch, capsys):
    from agent_py_agent.cli import home_runtime_commands as commands

    repo, run = _runtime_record(tmp_path, orphan=True)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    monkeypatch.setattr(commands, "make_agent", lambda args: agent)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unexpected interactive confirmation"))
    args = SimpleNamespace(settle=True, older_than_days=0, json=True)
    assert commands.cmd_runtime_stale_attempts(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["settled_agent_run_ids"] == [run] and result["remaining"] == []


# LLM: 独审反例：运维确认不能改写已经取消的 run 或补造 unknown 的结束时刻。
# 函数用途: 锁定历史 unknown 确认仅写 metadata/event 的边界。
def test_review_unknown_ack_preserves_existing_run_terminal(tmp_path):
    repo, run = _runtime_record(tmp_path, unknown=True)
    with repo._runtime_connection() as conn:
        conn.execute("UPDATE agent_runs SET status='cancelled' WHERE agent_run_id=?", (run,))
        conn.commit()
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=90000) == [run]
    assert repo.get_agent_run(run)["status"] == "cancelled"
    assert repo.current_attempt(run)["ended_at"] == 0


# LLM: 独审反例：同代再次故障仍须更新真实状态，通知去重仅影响发送。
# 函数用途: 验证恢复后再触顶不遗留 recovered，也不覆盖最后失败为恢复时刻。
def test_review_fault_state_separate_from_notification_dedup(fault_supervisor, monkeypatch):
    module, supervisor, clock = fault_supervisor
    monkeypatch.setattr(module, "_deliver_supervisor_admin_alert", lambda *args: True)
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    clock[0] = 200.0
    module._record_supervisor_fault(supervisor, "gateway_recovered", recovered=True)
    state = json.loads(supervisor._paths.supervisor_state.read_text())
    assert state["last_failure_reason"] == "restart_budget_exhausted" and state["last_failure_at"] == 100
    clock[0] = 300.0
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert json.loads(supervisor._paths.supervisor_state.read_text())["fault_status"] == "restart_budget_exhausted"


# LLM: 独审反例：前256条不能清的记录不得永远占住扫描头，测试使用临时真实SQLite。
# 函数用途: 验证自动结清按稳定游标继续扫描，列表不截断合法孤儿。
def test_review_stale_scan_advances_past_live_head(tmp_path):
    from agent_py_agent.tests.test_runtime_db_recover_stale import _seed_run

    repo, run = _runtime_record(tmp_path, orphan=True)
    with repo._runtime_connection() as conn:
        conn.execute("UPDATE agent_runs SET created_at=2 WHERE agent_run_id=?", (run,))
        conn.commit()
    for _ in range(256):
        other = _seed_run(repo, status="created", metadata={"runner_pid": os.getpid()})
        with repo._runtime_connection() as conn:
            conn.execute("UPDATE agent_attempts SET started_at=1 WHERE agent_run_id=?", (other,))
            conn.commit()
    assert any(item["agent_run_id"] == run for item in repo.unidentified_stale_attempts())
    repo.settle_unidentified_attempts(older_than_seconds=3600, now=90000, automatic=True)
    repo.settle_unidentified_attempts(older_than_seconds=3600, now=90000, automatic=True)
    assert repo.get_agent_run(run)["status"] == "cancelled"


# LLM: MUTATING/DIRTY及原因属于业务审计，不随死锁回收或历史确认重写。
# 函数用途: 验证清锁完整保留未知操作及脏资源记录。
@pytest.mark.parametrize("state", ["MUTATING", "DIRTY"])
def test_review_lock_cleanup_preserves_resource_audit(tmp_path, monkeypatch, state):
    from agent_py_agent.agent.runtime_db import repository as module

    repo, run = _runtime_record(tmp_path, unknown=True)
    attempt = repo.current_attempt(run)["attempt_id"]
    with repo._runtime_connection() as conn:
        conn.execute("INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,"
                     "tool_operation_generation,operation_type,canonical_scope,status,created_at,updated_at) "
                     "VALUES('op',?,?,1,1,'write','scope','UNKNOWN',1,1)", (run, attempt))
        conn.execute("INSERT INTO resource_mutations(mutation_id,canonical_scope,state,dirty_reason,attempt_id,updated_at) "
                     "VALUES('mutation','scope',?,'original evidence',?,1)", (state, attempt))
        conn.execute("INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,pid,start_token,attempt_id,"
                     "attempt_generation,workspace_epoch,lease_expires_at,created_at,updated_at) "
                     "VALUES('lock','scope','holder',99,'birth',?,1,1,1,1,1)", (attempt,))
        before = dict(conn.execute("SELECT * FROM resource_mutations").fetchone())
        conn.commit()
    monkeypatch.setattr(module, "holder_is_alive", lambda *args: False)
    assert repo.cleanup_stale_resource_locks(now=90000) == ["lock"]
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=90000) == [run]
    with repo._runtime_connection() as conn:
        assert dict(conn.execute("SELECT * FROM resource_mutations").fetchone()) == before
        assert conn.execute("SELECT status FROM tool_operations").fetchone()[0] == "UNKNOWN"


# LLM: 稳定游标必须越过活holder的固定头部；实际CAS仍由原仓储执行。
# 函数用途: 防止第一批256把活锁饿死后续死锁回收。
def test_review_lock_scan_advances_past_live_head(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db import repository as module

    repo, _ = _runtime_record(tmp_path, unknown=True)
    with repo._runtime_connection() as conn:
        conn.executemany("INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,pid,start_token,attempt_id,"
            "attempt_generation,workspace_epoch,lease_expires_at,created_at,updated_at) VALUES(?,?,'holder',?,'birth','',1,1,?,1,1)",
            [(f"lock-{n:03}", f"scope-{n}", 1 if n < 256 else 2, 1 if n < 256 else 2) for n in range(257)])
        conn.commit()
    monkeypatch.setattr(module, "holder_is_alive", lambda pid, *args: pid == 1)
    assert repo.cleanup_stale_resource_locks(now=90000, bounded=True) == []
    assert repo.cleanup_stale_resource_locks(now=90000, bounded=True) == ["lock-256"]
