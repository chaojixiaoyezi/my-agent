"""协作登记和待回应索引的真实规模、恢复及兼容回归。"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.collaboration.models import AgentCapability, CollaborationCase
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: 读取当前测试进程的内核累计物理写字节；不以 JSON 长度、文件增长或工具调用次数冒充写入量。
# 函数用途: 在 macOS/Linux 规模测试中观察真实进程写入，无法观察的平台明确拒绝该测量。
def _disk_written():
    if sys.platform == "darwin":
        class Usage(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_ubyte * 16), ("fields", ctypes.c_uint64 * 16),
                        ("read_bytes", ctypes.c_uint64), ("written_bytes", ctypes.c_uint64)]
        usage = Usage()
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        result = library.proc_pid_rusage(os.getpid(), 2, ctypes.byref(usage))
        assert result == 0, ctypes.get_errno()
        return int(usage.written_bytes)
    if sys.platform.startswith("linux"):
        values = dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
        return int(values["write_bytes"])
    raise AssertionError("physical write observation is not available on this platform")


# LLM: 旧文件刻意超过原 1 MiB 门槛，先测普通登记再测真实 manager 创建，不绕过 base.py 的登记调用。
# 函数用途: 验证 5k 次登记、重启、同 ID 覆盖及每次样本物理写入上界，不改旧名册原件。
def test_large_roster_registration_persists_every_agent_and_creation(tmp_path):
    root = tmp_path / "collaboration"
    root.mkdir()
    legacy = {f"legacy-{number}": AgentCapability(agent_id=f"legacy-{number}", metadata={"note": "x" * 400}).to_dict()
              for number in range(3000)}
    path = root / "agent_capabilities.json"
    original = json.dumps(legacy).encode()
    assert len(original) > 1024 * 1024
    path.write_bytes(original)
    store = CollaborationStore(root)
    observations = []
    for number in range(5000):
        observed = number in (0, 999, 1999, 2999, 3999, 4999)
        before = _disk_written() if observed else 0
        store.register_agent(AgentCapability(agent_id=f"agent-{number}", capabilities=("review",)))
        if observed:
            observations.append(_disk_written() - before)
    print("registration_disk_write_bytes=" + json.dumps(observations))
    assert max(observations) <= 512 * 1024, observations
    capabilities, error = store.agent_capabilities_report()
    assert error is None
    assert {item.agent_id for item in capabilities} == set(legacy) | {f"agent-{number}" for number in range(5000)}
    store.register_agent(AgentCapability(agent_id="agent-0", role="updated"))
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path, collaboration_store=store)
    child = manager.create_run(goal="真实创建登记")
    restarted = CollaborationStore(root)
    rows, error = restarted.agent_capabilities_report()
    assert error is None and child.id in {item.agent_id for item in rows}
    assert next(item for item in rows if item.agent_id == "agent-0").role == "updated"
    assert path.read_bytes() == original


# LLM: 历史是原 case JSON 格式，允许初始化迁移；观察窗口只覆盖后续真实 pending 入口，不测替代查询器。
# 函数用途: 在封闭历史增长时证明目标点查无全树 glob，返回不变且连接不漏 FD。
def test_pending_target_lookup_ignores_closed_history(tmp_path, monkeypatch):
    observations = []
    for history in (100, 1000):
        root = tmp_path / str(history)
        cases = root / "cases"
        cases.mkdir(parents=True)
        for number in range(history):
            case = CollaborationCase(case_id=f"closed-{number}", thread_id="thread", status="closed")
            (cases / f"{case.case_id}.json").write_text(json.dumps(case.to_dict()))
        # 本测试测迁移后的热查询，不要求后台线程在固定10秒内完成冷历史。
        # 仅把原生产维护任务调度到当前线程，实际分页、规范读取与索引写入不替换。
        with monkeypatch.context() as setup:
            setup.setattr(CollaborationStore, "_start_history_maintenance", CollaborationStore._maintain_history)
            store = CollaborationStore(root)
        migration = store._history_maintenance_report
        assert not migration["errors"] and migration["complete"] is True, migration
        assert migration["remaining"] == 0
        case = store.open_case({"thread_id": "thread", "title": "当前工作"})
        request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["target"], "question": "核对"})
        scans = []
        original_glob = Path.glob

        def observe(path, pattern):
            if path == cases:
                scans.append(pattern)
            return original_glob(path, pattern)

        with monkeypatch.context() as observed:
            observed.setattr(Path, "glob", observe)
            before = len(os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"))
            for _ in range(10):
                rows, errors = store.pending_requests_for_agent_report(agent_id="target", agent_name="alias")
                assert not errors and [item["request_id"] for item in rows] == [request.request_id]
            after = len(os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"))
        observations.append({"history": history, "scans": scans, "fd_delta": after - before})
    print("pending_scale_f26=" + json.dumps(observations))
    assert all(not item["scans"] and item["fd_delta"] == 0 for item in observations), observations


# LLM: pytest 故障回归；仅破坏临时 canonical 文件，覆盖所有本地写入口。
# 函数用途: 保证后一次写入不能把先前损坏的记录冒充为健康数据。
@pytest.mark.parametrize("writer", ["request", "evidence", "case", "request_status"])
def test_native_write_cannot_stamp_over_preexisting_bad_canonical_row(tmp_path, writer):
    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["target"]})
    source = store._evidence_path(case.case_id) if writer == "evidence" else store._requests_path(case.case_id)
    with source.open("a") as stream:
        stream.write("not-valid-json\n")
    if writer == "request":
        store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["target"]})
    elif writer == "evidence":
        store.submit_evidence({"case_id": case.case_id, "request_id": request.request_id, "source_agent_id": "other"})
    elif writer == "case":
        store.update_case_status(case.case_id, status="open")
    else:
        store.update_request_status({"case_id": case.case_id, "request_id": request.request_id, "status": "completed"})
    rows, errors = store.pending_requests_for_agent_report(agent_id="target")
    assert rows and errors, (rows, errors)
    assert "not-valid-json" in source.read_text()


# LLM: pytest 迁移隔离回归；外 case 记录只存在于临时输入，不能进入有效投影。
# 函数用途: 检查错放的请求和证据不会污染另一个正常 case。
@pytest.mark.parametrize("kind", ["request", "evidence"])
def test_migration_rejects_cross_case_rows_without_polluting_healthy_case(tmp_path, kind):
    from dataclasses import replace

    store = CollaborationStore(tmp_path / "collaboration")
    case_a = store.open_case({"thread_id": "a"})
    case_b = store.open_case({"thread_id": "b"})
    own = store.request_collaboration({"case_id": case_a.case_id, "target_agent_ids": ["a"]})
    request_b = store.request_collaboration({"case_id": case_b.case_id, "question": "healthy", "target_agent_ids": ["b"]})
    if kind == "request":
        foreign = replace(request_b, question="polluted", target_agent_ids=("intruder",)).to_dict()
        path = store._requests_path(case_a.case_id)
    else:
        foreign = {"case_id": case_b.case_id, "request_id": request_b.request_id,
                   "source_agent_id": "b", "evidence_id": "foreign"}
        path = store._evidence_path(case_a.case_id)
    with path.open("a") as stream:
        stream.write(json.dumps(foreign) + "\n")
    store.history_index.mark_dirty(case_a.case_id, published=True)
    rows, errors = store.pending_requests_for_agent_report(agent_id="b")
    assert [(row["request_id"], row["question"]) for row in rows] == [(request_b.request_id, "healthy")]
    assert errors and any(error.get("case_id") == case_a.case_id for error in errors)
    rows_a, _ = store.pending_requests_for_agent_report(agent_id="a")
    assert [row["request_id"] for row in rows_a] == [own.request_id]
    with pytest.raises(ValueError, match="case"):
        store.history_index.commit_event(case_a.case_id, store._history_signature(case_a.case_id), request=request_b)


# LLM: pytest 名册生命周期回归；删除的只是临时来源，不删除历史责任。
# 函数用途: 检查退场身份仍能找到自己尚未完成的工作。
def test_roster_source_removal_keeps_historical_identity_and_open_responsibility(tmp_path):
    root = tmp_path / "collaboration"
    root.mkdir()
    path = root / "agent_capabilities.json"
    path.write_text(json.dumps({"a": {"agent_id": "a", "status": "available", "role": "old"}}))
    store = CollaborationStore(root)
    assert store.agent_capabilities_report()[0][0].agent_id == "a"
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a"]})
    path.write_text("{}")
    capabilities, error = store.agent_capabilities_report()
    assert error is None and [item.agent_id for item in capabilities] == ["a"]
    matches, error = store.match_agents_report()
    assert not matches and error is None
    rows, errors = store.pending_requests_for_agent_report(agent_id="a")
    assert not errors and [row["request_id"] for row in rows] == [request.request_id]
    path.write_text(json.dumps({"a": {"agent_id": "a", "status": "available", "role": "reappeared"}}))
    capabilities, error = store.agent_capabilities_report()
    assert error is None and capabilities[0].role == "reappeared"
    assert [item.agent_id for item in store.match_agents()] == ["a"]


# LLM: pytest 流解析回归；用固定小块输入验证完整数字边界，不改变解析契约。
# 函数用途: 保证跨块数字不会被错误截成已经读完的短数字。
@pytest.mark.parametrize("number,chunk_size", [("1e+20", 10), ("1.25", 10), ("-1e-2", 11)])
def test_streamed_legacy_number_token_crossing_chunk_is_not_a_complete_prefix(number, chunk_size):
    import io

    from agent_py_agent.agent.collaboration.history_index import _legacy_values

    class SplitStream(io.StringIO):
        def read(self, size=-1):
            return super().read(min(size, chunk_size))

    text = '{"skip":' + number + ',"valid":{"agent_id":"ok"}}'
    expected = json.loads(text)
    assert list(_legacy_values(SplitStream(text), 1024)) == [expected["skip"], expected["valid"]]


# LLM: pytest 修复通知回归；外部修复先完成，再精确失效，覆盖当前实例和重启实例。
# 函数用途: 检查已修好的原始记录可以恢复读取，而不是永久留在坏缓存里。
@pytest.mark.parametrize("mode", ["case", "evidence", "targets"])
@pytest.mark.parametrize("restart", [False, True])
def test_completed_canonical_repair_notifies_exact_invalidation_and_resumes(tmp_path, monkeypatch, mode, restart):
    from dataclasses import replace

    root = tmp_path / "collaboration"
    store = CollaborationStore(root)
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a"]})
    target = "a"
    if mode == "case":
        store.update_case_status(case.case_id, status="closed")
        store._case_path(case.case_id).write_text(json.dumps(case.to_dict()))
    elif mode == "evidence":
        store.submit_evidence({"case_id": case.case_id, "request_id": request.request_id, "source_agent_id": "a"})
        store._evidence_path(case.case_id).write_text("")
    else:
        target = "b"
        restored = replace(request, target_agent_ids=("b",), status="rerouted")
        store._requests_path(case.case_id).write_text(json.dumps(restored.to_dict()) + "\n")
    # 外部修复已在停写约束下完成；通知必须耐久，不用全树发现恢复对象。
    monkeypatch.setattr(store, "_start_history_maintenance", lambda: None)
    store.invalidate_history_case(case.case_id)
    assert store.history_index.dirty_cases() == [case.case_id]
    if restart:
        live = CollaborationStore(root)
        assert live._history_worker is not None
        live._history_worker.join(10)
        assert not live._history_worker.is_alive()
        assert not live.history_index.dirty_cases()
    else:
        live = store
    rows, errors = live.pending_requests_for_agent_report(agent_id=target)
    assert not errors and [row["request_id"] for row in rows] == [request.request_id]


# LLM: pytest 双次提交故障回归；只在临时存储注入 SQL 失败，后续写入必须补齐旧事件。
# 函数用途: 检查后来成功的提交不会掩盖前一次尚未同步的请求或证据。
@pytest.mark.parametrize("failed_event", ["request", "evidence"])
def test_later_success_repairs_all_previous_failed_canonical_commit(tmp_path, monkeypatch, failed_event):
    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({"thread_id": "thread"})
    initial = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a"]})

    def fail_commit(*args, **kwargs):
        raise OSError("after canonical append")

    with monkeypatch.context() as fault:
        fault.setattr(store.history_index, "commit_event", fail_commit)
        with pytest.raises(OSError, match="after canonical"):
            if failed_event == "request":
                store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a"]})
            else:
                store.submit_evidence({"case_id": case.case_id, "request_id": initial.request_id, "source_agent_id": "a"})
    store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a"]})
    responded = {item.request_id for item in store.case_evidence(case.case_id) if item.source_agent_id == "a"}
    expected = {item.request_id for item in store.case_requests(case.case_id)} - responded
    for live in (store, CollaborationStore(store.root)):
        rows, errors = live.pending_requests_for_agent_report(agent_id="a")
        assert not errors and len(rows) == len(expected) and {row["request_id"] for row in rows} == expected


# LLM: pytest 并发锁回归；临时线程用受控事件排序，不以时间猜测锁是否有效。
# 函数用途: 检查 case 首次发表与恢复不会互相踩掉数据。
def test_first_case_publication_holds_original_lock_against_recovery(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    store = CollaborationStore(tmp_path / "collaboration")
    entered, release, recovery_attempt = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    observed = {}
    original_guard, original_dirty = store._case_update_lock, store.history_index.mark_dirty

    @contextmanager
    def tracked_guard(case_id):
        if getattr(local, "role", "") == "recovery":
            recovery_attempt.set()
        with original_guard(case_id):
            local.held = True
            try:
                yield
            finally:
                local.held = False

    def pause_after_dirty(case_id, **kwargs):
        original_dirty(case_id, **kwargs)
        observed.update(case_id=case_id, held=getattr(local, "held", False))
        entered.set()
        assert release.wait(10)

    def fail_commit(*args, **kwargs):
        raise OSError("after first canonical")

    def recover():
        local.role = "recovery"
        store._sync_history_case(observed["case_id"])

    with monkeypatch.context() as fault:
        fault.setattr(store, "_case_update_lock", tracked_guard)
        fault.setattr(store.history_index, "mark_dirty", pause_after_dirty)
        fault.setattr(store.history_index, "commit_event", fail_commit)
        with ThreadPoolExecutor(max_workers=2) as pool:
            creating = pool.submit(store.open_case, {"thread_id": "thread"})
            try:
                assert entered.wait(10)
                assert observed["held"], "first publication bypassed the existing case lock"
                recovering = pool.submit(recover)
                assert recovery_attempt.wait(10)
                assert store.history_index.dirty_cases() == [observed["case_id"]]
            finally:
                release.set()
            with pytest.raises(OSError, match="first canonical"):
                creating.result(timeout=10)
            recovering.result(timeout=10)
    assert not store.history_index.dirty_cases()
    request = store.request_collaboration({"case_id": observed["case_id"], "target_agent_ids": ["a"]})
    rows, errors = CollaborationStore(store.root).pending_requests_for_agent_report(agent_id="a")
    assert not errors and [row["request_id"] for row in rows] == [request.request_id]


# LLM: pytest 事务回归；临时数据库内注入失败，身份、活跃状态、特性必须同进同退。
# 函数用途: 检查注册失败后不会只留下半份 agent 名册。
def test_native_agent_upsert_failure_rolls_back_identity_active_and_features(tmp_path):
    import sqlite3

    store = CollaborationStore(tmp_path / "collaboration")
    original = AgentCapability(agent_id="native", role="old", capabilities=("old",))
    store.register_agent(original)
    with store.history_index.connect() as db:
        db.execute("CREATE TRIGGER reject_feature BEFORE INSERT ON agent_features "
                   "WHEN NEW.agent_id='native' BEGIN SELECT RAISE(ABORT,'injected feature failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected feature"):
        store.register_agent(AgentCapability(agent_id="native", role="changed", capabilities=("new",)))
    rows, error = store.agent_capabilities_report()
    assert error is None and rows == [original]
    assert store.match_agents(required_capabilities=["old"]) == [original]
    assert not store.match_agents(required_capabilities=["new"])


# LLM: pytest 迁移重启回归；保留临时检查点，native 行优先规则不能被旧来源覆盖。
# 函数用途: 检查迁移中断后能续上，并且重复身份仍选新格式记录。
def test_legacy_checkpoint_restart_and_duplicate_identity_native_precedence(tmp_path, monkeypatch):
    root = tmp_path / "collaboration"
    root.mkdir()
    path = root / "agent_capabilities.json"
    text = json.dumps({str(i): {"agent_id": f"dup-{i % 5}", "role": str(i)} for i in range(70)})
    path.write_text(text)
    store = CollaborationStore(root)
    commit = store.history_index._roster_page

    def interrupt_after_checkpoint(page, offset):
        commit(page, offset)
        if offset == 64:
            raise OSError("interrupted after committed page")

    monkeypatch.setattr(store.history_index, "_roster_page", interrupt_after_checkpoint)
    rows, error = store.agent_capabilities_report()
    assert not rows and error
    with store.history_index.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='roster_offset'").fetchone() == ("64",)
    store.register_agent(AgentCapability(agent_id="dup-0", role="native"))
    restored = CollaborationStore(root)
    rows, error = restored.agent_capabilities_report()
    expected = {f"dup-{i % 5}": str(i) for i in range(70)}
    expected["dup-0"] = "native"
    assert error is None and {item.agent_id: item.role for item in rows} == expected
    assert path.read_text() == text


# LLM: pytest 热集合成本回归；从正式 pending 入口记录 SQL 步数及连接复用，不只测输出存在。
# 函数用途: 检查无关 case 变多时，查自己的待办仍是固定成本。
@pytest.mark.parametrize("unrelated_status", ["closed", "open"])
def test_pending_official_entry_sql_work_stays_bounded_and_reuses_target_connection(tmp_path, monkeypatch, unrelated_status):
    from contextlib import contextmanager

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["a", "alias", "b"]})
    original = store.history_index.connect
    measurements = []
    previous = 0
    for history in (100, 5000):
        # 此处是 SQL 查询计划夹具；真实关闭 JSON 账本规模另由上面的公开入口测试覆盖。
        with original() as db:
            db.executemany("INSERT INTO cases VALUES (?,?,?,?)", [(f"closed-{i}", "{}", unrelated_status, i) for i in range(previous, history)])
            db.executemany("INSERT INTO inventory(case_id,done) VALUES (?,1)", [(f"closed-{i}",) for i in range(previous, history)])
            db.executemany("INSERT INTO pending VALUES (?,?,?,?,?,?,?)",
                           [("other", f"closed-{i}", f"request-{i}", "normal", i, i, i) for i in range(previous, history)])
        steps, target_connections = [0], set()

        @contextmanager
        def observed_connection():
            with original() as db:
                marker = object()

                def progress():
                    steps[0] += 1
                    return 0

                def trace(sql):
                    if "FROM pending p" in sql:
                        target_connections.add(marker)

                db.set_progress_handler(progress, 1)
                db.set_trace_callback(trace)
                try:
                    yield db
                finally:
                    db.set_progress_handler(None, 0)
                    db.set_trace_callback(None)

        with monkeypatch.context() as instrument:
            instrument.setattr(store.history_index, "connect", observed_connection)
            rows, errors = store.pending_requests_for_agent_report(agent_id="a", agent_name="alias", agent_role="b")
        assert not errors and [row["request_id"] for row in rows] == [request.request_id]
        assert len(target_connections) == 1
        measurements.append(steps[0])
        previous = history
    print("pending_sql_vm_steps", unrelated_status, measurements)
    assert measurements[1] <= measurements[0] + 600


# LLM: pytest 待办语义回归；临时 case 覆盖别名、响应、状态、排序、截断与重开。
# 函数用途: 保证索引变快后，谁该看到哪些待办的原有规则不变。
def test_pending_alias_responses_statuses_order_limit_and_reopen(tmp_path):
    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({"thread_id": "thread", "now": 10})
    requests = {}
    for offset, status in enumerate(("requested", "completed", "blocked", "declined")):
        requests[status] = store.request_collaboration({"case_id": case.case_id, "now": 20 + offset,
                                                       "status": status, "target_agent_ids": ["a", "alias"],
                                                       "priority": "high" if status == "completed" else "normal"})
        if status != "requested":
            requests[status] = store.update_request_status({"case_id": case.case_id, "request_id": requests[status].request_id, "status": status})
    rows, errors = store.pending_requests_for_agent_report(agent_id="a", agent_name="alias", limit=0)
    assert not errors and [row["request_id"] for row in rows] == [requests["completed"].request_id, requests["requested"].request_id]
    assert store.pending_requests_for_agent(agent_id="a", limit=1)[0]["request_id"] == requests["completed"].request_id
    store.submit_evidence({"case_id": case.case_id, "request_id": requests["completed"].request_id, "source_agent_id": "alias"})
    rows = store.pending_requests_for_agent(agent_id="a", agent_name="alias", limit=0)
    assert [row["request_id"] for row in rows] == [requests["requested"].request_id]
    store.update_case_status(case.case_id, status="closed")
    assert not store.pending_requests_for_agent(agent_id="a", agent_name="alias")
    store.update_case_status(case.case_id, status="open")
    store.update_request_status({"case_id": case.case_id, "request_id": requests["requested"].request_id, "status": "rerouted", "target_agent_ids": ["b"]})
    assert not store.pending_requests_for_agent(agent_id="a", agent_name="alias")
    assert [row["request_id"] for row in store.pending_requests_for_agent(agent_id="b")] == [requests["requested"].request_id]


# LLM: pytest 私有文件边界回归；只链接临时文件，夹具被宿主拒绝时明确跳过而不伪造通过。
# 函数用途: 检查索引拒绝链接，并保持链接指向的文件原样不动。
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_private_history_index_rejects_links_without_touching_external_file(tmp_path, link):
    import errno
    import os
    import stat

    store = CollaborationStore(tmp_path / "collaboration")
    path = store.history_index.path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    external = tmp_path / "untouched"
    external.write_bytes(b"sentinel")
    path.unlink()
    if link == "symlink":
        path.symlink_to(external)
    else:
        try:
            os.link(external, path)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.ENOTSUP}:
                pytest.skip("host denied real hardlink fixture before index entry; real hardlinks UNVERIFIED")
            raise
    with pytest.raises((OSError, ValueError)):
        store.register_agent(AgentCapability(agent_id="forbidden"))
    assert external.read_bytes() == b"sentinel"


# LLM: pytest 元数据分支回归；模拟 nlink 仅证明开库前守卫，不替代真实硬链接测试。
# 函数用途: 检查可疑链接在 SQLite 打开之前就被拦住。
def test_history_metadata_hardlink_guard_runs_before_sqlite_open(tmp_path, monkeypatch):
    import os
    import sqlite3

    store = CollaborationStore(tmp_path / "collaboration")
    path = store.history_index.path
    before, original = path.read_bytes(), Path.lstat
    opened = []

    def metadata(candidate, *args, **kwargs):
        value = original(candidate, *args, **kwargs)
        if candidate == path:
            fields = list(value)
            fields[3] = 2
            return os.stat_result(fields)
        return value

    def forbidden_open(*args, **kwargs):
        opened.append(True)
        raise AssertionError("SQLite opened before hardlink rejection")

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(sqlite3, "connect", forbidden_open)
    with pytest.raises(OSError, match="single-link"):
        store.register_agent(AgentCapability(agent_id="forbidden"))
    assert not opened and path.read_bytes() == before


# LLM: pytest 同 ID 跨 case 回归；错误归属必须在最后快照折叠前过滤，覆盖重启。
# 函数用途: 保证别处的同名请求不能挤掉当前 case 的有效请求。
@pytest.mark.parametrize("restart", [False, True])
def test_foreign_same_request_id_does_not_replace_last_owned_snapshot(tmp_path, restart):
    from dataclasses import replace

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["old"]})
    request = store.update_request_status({"case_id": case.case_id, "request_id": request.request_id,
                                           "status": "rerouted", "target_agent_ids": ["new"]})
    foreign = replace(request, case_id="foreign-case", target_agent_ids=("foreign",))
    with store._requests_path(case.case_id).open("a") as stream:
        stream.write(json.dumps(foreign.to_dict()) + "\n")
    store.invalidate_history_case(case.case_id)
    if restart:
        store = CollaborationStore(store.root)
    rows, errors = store.pending_requests_for_agent_report(agent_id="new")
    assert [row["request_id"] for row in rows] == [request.request_id]
    assert errors and any(error.get("claimed_case_id") == "foreign-case" for error in errors)
    assert not store.pending_requests_for_agent(agent_id="old")
    assert not store.pending_requests_for_agent(agent_id="foreign")


# LLM: pytest 旧来源删除回归；只退役旧来源活跃投影，native 和历史责任必须保留。
# 函数用途: 检查整个旧名册消失后不会继续显示过期的活跃 agent。
@pytest.mark.parametrize("restart", [False, True])
def test_deleted_legacy_source_retires_only_legacy_active_projection(tmp_path, restart):
    root = tmp_path / "collaboration"
    root.mkdir()
    path = root / "agent_capabilities.json"
    legacy = AgentCapability(agent_id="legacy", capabilities=("research",))
    path.write_text(json.dumps({legacy.agent_id: legacy.to_dict()}))
    store = CollaborationStore(root)
    native = AgentCapability(agent_id="native", capabilities=("research",))
    store.register_agent(native)
    assert {row.agent_id for row in store.match_agents(required_capabilities=["research"])} == {"legacy", "native"}
    case = store.open_case({"thread_id": "thread"})
    request = store.request_collaboration({"case_id": case.case_id, "target_agent_ids": ["legacy"]})
    path.unlink()
    if restart:
        store = CollaborationStore(root)
    assert [row.agent_id for row in store.match_agents(required_capabilities=["research"])] == ["native"]
    rows, error = store.agent_capabilities_report()
    assert error is None and {row.agent_id for row in rows} == {"legacy", "native"}
    assert [row["request_id"] for row in store.pending_requests_for_agent(agent_id="legacy")] == [request.request_id]
