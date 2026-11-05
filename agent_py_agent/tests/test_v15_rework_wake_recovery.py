"""R6：单次迁移读盘失败后的同进程恢复，真实 Client/Gateway/Store/消费者。"""
import ast
import errno
import inspect
import textwrap
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_py_agent.agent.conversation import store_wake_publication as publication
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_v14_r1_task_wake_bootstrap import (
    _fd_count,
    _legacy_root,
    _tool_dispatch,
    _w5_unknown_index,
)
from agent_py_agent.tests.test_v14_w5_background_wakes import (
    assign,
    finish_retirement,
    producer_service,
    runtime_case,
    task_wakes,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    business_wire as business_wire,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import completed


def _unknown_after_normal_turn(case):
    """保留真实已处理发布账，将唯一派生库降为受支持的旧 unknown schema。"""
    host, _client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    earlier = assign(case, "r6-before-migration")
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(earlier).status == "done" and model.calls == 1
    record = next(store.storage.wake_dedupe_dir.glob("*.json"))
    proof = record.read_bytes()
    store.storage.wake_task_index_path.unlink()
    _w5_unknown_index(store.storage)
    return record, proof


def _transient_migration(case, monkeypatch, record):
    """只在一个原发布记录的文件读取边界制造一次 EIO，不替换生产恢复路线。"""
    host = case[0]
    store = host.conversation_store
    read_text, failures = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == record and not failures:
            failures.append(path)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        reopened = ConversationStore(store.storage.root)
    assert failures == [record]
    assert publication._task_index_coverage(reopened.storage) == "unknown"
    assert _fd_count() == before


def _queued_without_wake(case, action):
    host, client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    current = completed(client, "background_agents.get", {"agent_id": thread_id})["agent"]
    if action == "restore":
        parked = completed(client, "background_agents.retire", {"agent_id": thread_id, "disposition": "drain"},
            expected_revision=current["revision"])["agent"]
        assert parked["identity_state"] == "retired"
    else:
        parked = completed(client, "background_agents.suspend", {"agent_id": thread_id},
            expected_revision=current["revision"])["agent"]
    task_id = _tool_dispatch(host, view, "整理一份简短文字结果：R6迁移恢复")
    assert store.session_tasks.require(task_id).status == "queued"
    assert task_wakes(store, task_id) == []
    return task_id, parked["revision"]


def _transition(client, thread_id, action, revision):
    params = {"agent_id": thread_id}
    if action == "retire":
        params["disposition"] = "drain"
    return completed(client, "background_agents." + action, params, expected_revision=revision)


@pytest.mark.parametrize("boundary", ["writable-open", "resume", "restore", "retire"])
def test_transient_migration_recovers_automatically_with_explicit_rebuild_control(business_wire, monkeypatch, boundary):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    # 先建立原服务实例；后续恢复不能靠重新构造宿主或进程清掉内存节流。
    service, actor = producer_service(case)
    record, proof = _unknown_after_normal_turn(case)
    action = "resume" if boundary == "writable-open" else boundary
    task_id, revision = _queued_without_wake(case, action)
    task_before = store.session_tasks.require(task_id)
    _transient_migration(case, monkeypatch, record)
    before = _fd_count()
    if boundary == "writable-open":
        for _ in range(3):
            ConversationStore(store.storage.root)
    _transition(client, thread_id, action, revision)
    automatic = [wake.wake_signal_id for wake in task_wakes(store, task_id)]
    automatic_coverage = publication._task_index_coverage(store.storage)
    service._republish_pending_work(store.threads.require(thread_id), [task_before], actor=actor)
    assert [wake.wake_signal_id for wake in task_wakes(store, task_id)] == automatic
    assert _fd_count() == before
    # 即使自动恢复是红灯，也先证明同一份 canonical 可由显式重建恢复并真正执行。
    store.wakes.rebuild_task_responsibility_index()
    service._republish_pending_work(store.threads.require(thread_id), [task_before], actor=actor)
    live = task_wakes(store, task_id)
    assert len(live) == 1 and record.read_bytes() == proof
    assert model.calls == 1
    scheduler.tick_thread(thread_id)
    recovered = store.session_tasks.require(task_id)
    assert recovered.status == "done" and model.calls == 2 and model.peak == 1
    assert (recovered.sender_thread_id, recovered.target_thread_id, recovered.body_guidance_id) == (
        task_before.sender_thread_id, task_before.target_thread_id, task_before.body_guidance_id)
    if action == "retire":
        finish_retirement(case)
    # 新存储实例再次观察和调度，终态与原handled证明不能倒退或再调模型。
    fresh = ConversationStore(store.storage.root)
    assert fresh.session_tasks.require(task_id).status == "done"
    scheduler.tick_thread(thread_id)
    assert model.calls == 2 and task_wakes(fresh, task_id) == []
    assert automatic_coverage == "complete", "healthy writable recovery kept unknown coverage"
    assert len(automatic) == 1, "explicit rebuild succeeded but automatic recovery stranded the queued task"
    assert automatic == [live[0].wake_signal_id]


def test_complete_responsibility_recovery_keeps_point_access(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_v14_w5_background_wakes import (
        responsibility_raise,
        responsibility_report,
        responsibility_store,
    )

    store = responsibility_store(tmp_path)
    for number in range(80):
        signal = responsibility_raise(store, f"history-{number}", task=f"stask-old-{number}")
        store.wakes.mark_handled(signal.wake_signal_id)
    live = responsibility_raise(store)
    reads, read_text = [], Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("complete point access must not rebuild or scan canonical history")

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(Path, "glob", no_scan)
        patch.setattr(Path, "rglob", no_scan)
        patch.setattr(publication, "_rebuild_task_index_locked", no_scan)
        store.wakes.recover_task_responsibility_index(blocking=False)
        rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [live.wake_signal_id]
    assert reads == [store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key), store.storage.wake_signal_path(live)]
    assert _fd_count() == before
    with closing(publication._task_index_connection(store.storage)) as connection:
        assert connection.execute("SELECT count(*) FROM locators").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["resume", "restore", "retire"])
def test_unfinished_recovery_is_visible_and_same_wire_request_can_retry(business_wire, monkeypatch, action):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, action)
    params = {"agent_id": thread_id, **({"disposition": "drain"} if action == "retire" else {})}
    request = client.prepare("background_agents." + action, params, expected_revision=revision)
    failures, read_text = [], Path.read_text

    def read(path, *args, **kwargs):
        if path == record:
            failures.append(path)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        result = client.send(request)
    assert result["ok"] is False and result["http_status"] == 503, result
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert failures == [record]
    task = store.session_tasks.require(task_id)
    assert task.status == "queued" and store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert task_wakes(store, task_id) == [] and model.calls == 1
    identity = store.threads.require(thread_id).metadata["background_agent"]
    replay = client.send(request)
    assert replay["http_status"] == 200 and replay["state"] == "completed", replay
    assert store.threads.require(thread_id).metadata["background_agent"] == identity
    assert publication._task_index_coverage(store.storage) == "complete"
    assert len(task_wakes(store, task_id)) == 1 and record.read_bytes() == proof
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1
    if action == "retire":
        finish_retirement(case)


def _unknown_store(tmp_path):
    root, live = _legacy_root(tmp_path)
    store = ConversationStore(root, initialize=False)
    _w5_unknown_index(store.storage)
    return store, live


def test_concurrent_recovery_and_nonblocking_contention_close_all_handles(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    reading, release, competing = Event(), Event(), Event()
    read_text, failures, builds = Path.read_text, [], []
    rebuild = publication._rebuild_task_index_locked

    def counted(storage):
        builds.append(storage.root)
        return rebuild(storage)

    def read(path, *args, **kwargs):
        if path == record:
            reading.set()
            assert release.wait(5), "test did not release canonical read"
        return read_text(path, *args, **kwargs)

    def recover(*, competitor=False):
        try:
            if competitor:
                competing.set()
            store.wakes.recover_task_responsibility_index()
        except BaseException as exc:  # noqa: BLE001 - 在线程收口后交回主线程
            failures.append(exc)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(publication, "_rebuild_task_index_locked", counted)
        first, second = Thread(target=recover, daemon=True), Thread(target=lambda: recover(competitor=True), daemon=True)
        first.start()
        try:
            assert reading.wait(5)
            # 先在竞争者尚未打开自己的FD时测非阻塞清理，避免把它的合法在途FD当成本次泄漏。
            busy_fds = _fd_count()
            with pytest.raises(BlockingIOError):
                store.wakes.recover_task_responsibility_index(blocking=False)
            assert _fd_count() == busy_fds
            second.start()
            assert competing.wait(5)
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
    assert not first.is_alive() and not second.is_alive() and not failures, failures
    assert builds == [store.storage.root]
    assert _fd_count() == before
    assert store.wakes.task_responsibility_report(live.thread_id, "stask-live") == (
        [{"wake_signal_id": live.wake_signal_id, "phase": "published"}], None)


@pytest.mark.parametrize("missing", [False, True], ids=["legacy-unknown", "missing-index"])
@pytest.mark.parametrize("interruption", [OSError(errno.EIO, "simulated I/O error"), KeyboardInterrupt()], ids=["EIO", "cancel"])
def test_failed_or_cancelled_migration_releases_retry_and_resources(tmp_path, monkeypatch, missing, interruption):
    store, live = _unknown_store(tmp_path)
    if missing:
        store.storage.wake_task_index_path.unlink()
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    read_text, failures = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == record and not failures:
            failures.append(path)
            raise interruption
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        if isinstance(interruption, OSError):
            # ensure_dirs保留非致命打开合同，读者仍明确unknown。
            ConversationStore(store.storage.root)
        else:
            with pytest.raises(KeyboardInterrupt):
                ConversationStore(store.storage.root)
    assert failures == [record] and publication._task_index_coverage(store.storage) == "unknown"
    assert store.wakes.task_responsibility_report(live.thread_id, "stask-live")[1] is not None
    assert _fd_count() == before
    ConversationStore(store.storage.root)
    assert publication._task_index_coverage(store.storage) == "complete"
    assert _fd_count() == before


def test_readonly_unknown_report_does_not_repair_or_mutate(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    before = {path: path.read_bytes() for path in store.storage.root.rglob("*") if path.is_file()}

    def no_scan(*args, **kwargs):
        raise AssertionError("read-only unknown coverage must not initiate writable recovery")

    fds = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rebuild_task_index_locked", no_scan)
        patch.setattr(Path, "glob", no_scan)
        patch.setattr(Path, "rglob", no_scan)
        readonly = ConversationStore(store.storage.root, initialize=False)
        for _ in range(3):
            rows, error = readonly.wakes.task_responsibility_report(live.thread_id, "stask-live")
            assert rows == [] and error is not None
    assert _fd_count() == fds
    assert {path: path.read_bytes() for path in store.storage.root.rglob("*") if path.is_file()} == before


def test_recovery_rejects_shared_to_exclusive_upgrade(tmp_path):
    store, live = _unknown_store(tmp_path)
    before = _fd_count()
    with store.wakes.task_responsibility_guard(live.thread_id, "stask-live"):
        with pytest.raises(RuntimeError, match="task guard"):
            store.wakes.recover_task_responsibility_index(blocking=False)
    store.wakes.recover_task_responsibility_index()
    assert publication._task_index_coverage(store.storage) == "complete" and _fd_count() == before


def test_corrupt_source_throttle_is_once_per_index_generation(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    proof = record.read_bytes()
    record.write_text("{", encoding="utf-8")
    rebuild, builds = publication._rebuild_task_index_locked, []

    def counted(storage):
        builds.append(storage.root)
        return rebuild(storage)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rebuild_task_index_locked", counted)
        for _ in range(3):
            ConversationStore(store.storage.root)
        assert len(builds) == 1
        for _ in range(2):
            with pytest.raises(DataCorruptionError):
                store.wakes.recover_task_responsibility_index()
        assert len(builds) == 1 and record.read_text() == "{"
        assert store.wakes.task_responsibility_report(live.thread_id, "stask-live")[1] is not None
        # 显式原子重建是新代，不允许旧代的坏账节流阻断下一代健康迁移。
        record.write_bytes(proof)
        store.wakes.rebuild_task_responsibility_index()
        store.storage.wake_task_index_path.unlink()
        _w5_unknown_index(store.storage)
        ConversationStore(store.storage.root)
        assert len(builds) == 3 and publication._task_index_coverage(store.storage) == "complete"
    assert _fd_count() == before


def test_writable_recovery_preserves_original_io_cause(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    read_text = Path.read_text
    error = OSError(errno.EIO, "simulated I/O error")

    def read(path, *args, **kwargs):
        if path == record:
            raise error
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        with pytest.raises(DataCorruptionError) as caught:
            store.wakes.recover_task_responsibility_index()
    assert caught.value.__cause__ is error
    assert _fd_count() == before


@pytest.mark.parametrize("action", ["resume", "restore", "retire"])
def test_recovery_preserves_existing_live_wake_and_executes_once(business_wire, monkeypatch, action):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, action)
    # 原真实队列保留的工作责任；恢复不能凭当前身份revision另发一条。
    live = store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task", "urgency": "normal",
        "dedupe_key": f"session_task:{task_id}:original", "retain_handled": True,
        "metadata": {"session_task_id": task_id}})
    _transient_migration(case, monkeypatch, record)
    _transition(client, thread_id, action, revision)
    assert task_wakes(store, task_id) == [live]
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1
    assert store.storage.wake_handled_dir.joinpath(live.wake_signal_id + ".json").is_file()
    if action == "retire":
        finish_retirement(case)


def recovery_contract_violations(source):
    """只拦语法能判定的危险结构：锁内升级、循环重建、只读修账、未知跳过和缺失finally清理。"""
    tree = ast.parse(textwrap.dedent(source))
    recovery = {"_initialize_task_index", "recover_task_responsibility_index", "rebuild_task_responsibility_index",
        "_rebuild_task_index_locked"}

    def name(call):
        target = call.func
        return target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(target, ast.Name) else ""

    def calls(node):
        return [value for value in ast.walk(node) if isinstance(value, ast.Call)]

    def in_body(statements):
        return [call for statement in statements for call in calls(statement) if name(call) in recovery]

    errors = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp):
            checked = {value.id for value in ast.walk(node.test) if isinstance(value, ast.Name)}
            if {"error", "live"} <= checked and any(isinstance(statement, ast.Continue) for statement in node.body):
                errors.add("unknown_responsibility_silently_skipped")
        if isinstance(node, ast.With):
            shared = any(name(call) in {"task_responsibility_guard", "signal_responsibility_guard"}
                or name(call) == "_task_index_guard" and any(keyword.arg == "exclusive"
                    and isinstance(keyword.value, ast.Constant) and keyword.value.value is False for keyword in call.keywords)
                for item in node.items for call in calls(item.context_expr))
            if shared and in_body(node.body):
                errors.add("recovery_under_shared_guard")
        if isinstance(node, (ast.For, ast.While)) and in_body(node.body):
            errors.add("recovery_inside_point_loop")
        if isinstance(node, ast.FunctionDef) and node.name == "task_responsibility_report" and in_body(node.body):
            errors.add("readonly_report_repairs")
        if isinstance(node, ast.FunctionDef) and node.name == "_initialize_task_index":
            attempts = [value for value in ast.walk(node) if isinstance(value, ast.Try)
                and any(name(call) == "_rebuild_task_index_locked" for statement in value.body for call in calls(statement))]
            if not any(any(name(call) in {"pop", "discard"} and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "_UNKNOWN_INDEX_REBUILDS"
                    for statement in attempt.finalbody for call in calls(statement)) for attempt in attempts):
                errors.add("rebuild_suppression_without_finally_release")
    return errors


def test_ast_recovery_guards_reject_semantic_counterexamples():
    source = "\n".join(textwrap.dedent(inspect.getsource(value)) for value in (
        publication._initialize_task_index, publication.task_responsibility_report,
        BackgroundAgentService._republish_pending_work))
    assert recovery_contract_violations(source) == set()
    invalid_upgrade = """
        def producer(store, tasks):
            for task in tasks:
                with store.wakes.task_responsibility_guard('thread', task):
                    store.wakes.recover_task_responsibility_index()
    """
    invalid_read = """
        def task_responsibility_report(storage):
            rebuild_task_responsibility_index(storage)
    """
    invalid_cleanup = """
        def _initialize_task_index(storage):
            _UNKNOWN_INDEX_REBUILDS.add(storage.root)
            try:
                _rebuild_task_index_locked(storage)
            except OSError:
                return
    """
    invalid_skip = """
        def producer(tasks):
            for task in tasks:
                live, error = task_responsibility_report(task)
                if error or live:
                    continue
    """
    assert recovery_contract_violations(invalid_upgrade) == {"recovery_under_shared_guard", "recovery_inside_point_loop"}
    assert recovery_contract_violations(invalid_read) == {"readonly_report_repairs"}
    assert recovery_contract_violations(invalid_cleanup) == {"rebuild_suppression_without_finally_release"}
    assert recovery_contract_violations(invalid_skip) == {"unknown_responsibility_silently_skipped"}


def test_failed_rebuild_between_recovery_and_task_guard_is_not_silent_success(business_wire, monkeypatch):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, "resume")
    store.wakes.recover_task_responsibility_index()
    request = client.prepare("background_agents.resume", {"agent_id": thread_id}, expected_revision=revision)
    reading, checked, release = Event(), Event(), Event()
    read_text, coverage = Path.read_text, publication._task_index_coverage
    rebuild_errors, results, failures = [], [], []

    def read(path, *args, **kwargs):
        if path == record and not reading.is_set():
            reading.set()
            assert release.wait(5)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    def observed(storage, **kwargs):
        value = coverage(storage, **kwargs)
        if storage.root == store.storage.root and value == "complete" and reading.is_set():
            checked.set()
        return value

    def rebuild():
        try:
            store.wakes.rebuild_task_responsibility_index()
        except DataCorruptionError as exc:
            rebuild_errors.append(exc)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def resume():
        try:
            results.append(client.send(request))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(publication, "_task_index_coverage", observed)
        writer, producer = Thread(target=rebuild, daemon=True), Thread(target=resume, daemon=True)
        writer.start()
        try:
            assert reading.wait(5)
            producer.start()
            assert checked.wait(5), "producer did not read old complete coverage while rebuild held index EX"
        finally:
            release.set()
            writer.join(5)
            if producer.ident is not None:
                producer.join(5)
    assert not writer.is_alive() and not producer.is_alive() and not failures, failures
    assert len(rebuild_errors) == 1 and isinstance(rebuild_errors[0].__cause__, OSError)
    assert len(results) == 1 and results[0]["ok"] is False and results[0]["http_status"] == 503, results
    assert store.session_tasks.require(task_id).status == "queued" and task_wakes(store, task_id) == []
    replay = client.send(request)
    assert replay["ok"] is True and replay["state"] == "completed", replay
    assert len(task_wakes(store, task_id)) == 1
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_transition_during_recovery_never_revives_task(business_wire, monkeypatch, terminal):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, "resume")
    reading, release = Event(), Event()
    read_text, failures, results = Path.read_text, [], []

    def read(path, *args, **kwargs):
        if path == record and not reading.is_set():
            reading.set()
            assert release.wait(5)
        return read_text(path, *args, **kwargs)

    def resume():
        try:
            results.append(_transition(client, thread_id, "resume", revision))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        worker = Thread(target=resume, daemon=True)
        worker.start()
        try:
            assert reading.wait(5)
            store.session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
            ended = store.session_tasks.advance(task_id, SessionTaskUpdate(status=terminal))
        finally:
            release.set()
            worker.join(5)
    assert not worker.is_alive() and not failures and len(results) == 1, failures
    assert store.session_tasks.require(task_id).to_dict() == ended.to_dict()
    assert task_wakes(store, task_id) == []
    fresh = ConversationStore(store.storage.root)
    scheduler.tick_thread(thread_id)
    assert fresh.session_tasks.require(task_id).to_dict() == ended.to_dict() and model.calls == 1
