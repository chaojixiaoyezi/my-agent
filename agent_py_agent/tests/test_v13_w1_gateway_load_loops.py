"""W1热循环回归：同owner重建退避、真实入站标记与结构化异常分类。"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.conversation.background_recovery import StorageWaitBackoff
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin
from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable
from agent_py_agent.cli import gateway_lane_retry, gateway_loops


def test_scheduler_enqueue_backoff_survives_agent_rebuild(tmp_path, caplog):
    calls = Mock(side_effect=OwnerQuotaUnavailable("owner usage reconciliation pending; growth refused"))

    def runtime(owner):
        scheduler = _BackgroundSchedulerTickMixin()
        scheduler.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner)))
        scheduler._tick_cheap_checks = lambda _: True
        scheduler.scheduler_service = SimpleNamespace(reconcile_waiting_runs=lambda **_: None, enqueue_ready_runs=calls)
        return scheduler

    first, second = runtime(tmp_path), runtime(tmp_path)
    with caplog.at_level(logging.WARNING):
        first._enqueue_scheduler_runs(now=100)
        second._enqueue_scheduler_runs(now=100.5)
        assert calls.call_count == 1
        second._enqueue_scheduler_runs(now=101)
        assert calls.call_count == 2
    rows = [row for row in caplog.records if "SCHEDULER_ENQUEUE_BACKOFF" in row.message]
    assert len(rows) == 1
    assert rows[0].exc_info is None and "Traceback" not in caplog.text
    assert first._scheduler_storage_wait is second._scheduler_storage_wait
    runtime(tmp_path / "other")._enqueue_scheduler_runs(now=100)
    assert calls.call_count == 3


def test_shared_storage_wait_is_bounded_and_success_resets(monkeypatch):
    monkeypatch.setattr(StorageWaitBackoff, "_SHARED_LIMIT", 2)
    monkeypatch.setattr(StorageWaitBackoff, "_shared", type(StorageWaitBackoff._shared)())
    first = StorageWaitBackoff.shared("first")
    first.failed(100, OSError("busy"))
    assert StorageWaitBackoff.shared("first") is first and not first.ready(100.5)
    StorageWaitBackoff.shared("second")
    StorageWaitBackoff.shared("third")
    assert len(StorageWaitBackoff._shared) == 2
    assert first.succeeded() and first.ready(100.5)


@pytest.mark.parametrize("error_type", [RuntimeRecoveryRequiredError, ConversationCompactError])
def test_waiting_for_user_input_lane_parks_until_new_message(monkeypatch, caplog, error_type):
    clock = [0.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: clock[0])
    retry = gateway_lane_retry.BackgroundLaneRetry()
    marker = ["inbound-1"]
    with caplog.at_level(logging.WARNING):
        error = (error_type("arbitrary non-routing text", code="TEST_COMPACT")
                 if error_type is ConversationCompactError else error_type("arbitrary non-routing text"))
        retry.failed("owner", "thread", error, delay=30,
                     wait_for_inbound_marker=marker[0])
        for now in (30, 300, 3000):
            clock[0] = now
            assert not retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])
        assert retry._failures[("owner", "thread")].waiting_for_user_input
        marker[0] = "inbound-2"
        assert retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])
    assert sum("BACKGROUND_LANE_WAITING_FOR_USER_INPUT" in row.message for row in caplog.records) == 1
    retry.failed("owner", "other", ValueError("恢复核实提示尚未交接"), delay=30)
    clock[0] += 29
    assert not retry.ready("owner", "other", model_ready=lambda: True)
    clock[0] += 1
    assert retry.ready("owner", "other", model_ready=lambda: True)


def test_inbound_marker_uses_user_activity_not_background_updates(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "inbound-test"})
    scheduler = SimpleNamespace(store=store)
    original = gateway_loops._thread_inbound_marker(scheduler, thread.thread_id)
    store.threads.update_summary(thread.thread_id, summary="background summary")
    assert gateway_loops._thread_inbound_marker(scheduler, thread.thread_id) == original
    store.threads.record_user_activity(thread.thread_id, request_id="inbound-new")
    assert gateway_loops._thread_inbound_marker(scheduler, thread.thread_id) != original


def test_thread_tick_captures_marker_before_execution_and_does_not_print_programmer_bug(monkeypatch):
    supervisor = object.__new__(gateway_loops._BackgroundThreadLaneSupervisorMixin)
    supervisor.stop_event = SimpleNamespace(is_set=lambda: False)
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()
    marker = ["before"]
    monkeypatch.setattr(gateway_loops, "_thread_inbound_marker", lambda *_: marker[0])
    printer = Mock()
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error", printer)

    def execute(*_, **__):
        marker[0] = "arrived-during-execution"
        raise RuntimeRecoveryRequiredError("unrelated text")

    assert supervisor._safe_thread_tick(SimpleNamespace(tick_thread=execute), "owner", "thread") == []
    printer.assert_not_called()
    assert supervisor._lane_retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])


def test_unknown_marker_never_unparks_by_time():
    retry = gateway_lane_retry.BackgroundLaneRetry()
    retry.failed("o", "t", RuntimeRecoveryRequiredError("x"), delay=0)
    assert not retry.ready("o", "t", model_ready=lambda: True)
    assert not retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "first-valid")
    assert not retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "first-valid")
    assert retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "new")


"""缓存失效和SIGUSR1安全导出的独立回归，供父级合入负载测试。"""

import ast
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt import sandbox as cache_signal_sb
from agent_py_agent.agent.tooling import plugin_registration as cache_signal_pr
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_process as cache_signal_gp


def cache_signal_spec(root):
    return cache_signal_sb.AttemptSandboxSpec(
        attempt_view=root, staging_root=root, shared_workspace=root, owner_home=root,
        implicit_attempt_write_roots=False,
    )


@pytest.fixture
def cache_signal_empty_cache():
    cache_signal_sb._MacosGitMarkerCache.entries.clear()
    yield
    cache_signal_sb._MacosGitMarkerCache.entries.clear()


def test_git_marker_cache_hits_and_invalidates_on_mtime(tmp_path, monkeypatch, cache_signal_empty_cache):
    root = tmp_path / "root"
    child = root / "child"
    nested = child / "nested"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    clock = [100.0]
    monkeypatch.setattr(cache_signal_sb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original, scans = cache_signal_sb.os.scandir, []

    def cache_signal_scan(path):
        scans.append(Path(path))
        return original(path)

    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_scan)
    spec = cache_signal_spec(root)
    first = cache_signal_sb._macos_git_markers(spec)
    assert nested / ".git" in first and scans
    scans.clear()
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert scans == []
    stamp = nested.stat().st_mtime_ns
    os.utime(nested, ns=(stamp + 1_000_000, stamp + 1_000_000))
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert nested in scans
    scans.clear()
    clock[0] += 5.0
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert scans
    scans.clear()
    new_git = child / ".git"
    new_git.mkdir()
    assert new_git in cache_signal_sb._macos_git_markers(spec)
    assert scans


def test_git_marker_cache_realpath_root_isolation_and_bounds(tmp_path, monkeypatch, cache_signal_empty_cache):
    original, scans = cache_signal_sb.os.scandir, []

    def cache_signal_scan(path):
        scans.append(Path(path))
        return original(path)

    root = tmp_path / "first"
    (root / "a" / "b" / "c" / "d" / ".git").mkdir(parents=True)
    (root / "a" / "b" / "c" / ".git").mkdir()
    (root / "node_modules" / ".git").mkdir(parents=True)
    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_scan)
    markers = cache_signal_sb._macos_git_markers(cache_signal_spec(root))
    assert root / "a" / "b" / "c" / ".git" in markers
    assert root / "a" / "b" / "c" / "d" / ".git" not in markers
    assert root / "node_modules" not in scans
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    scans.clear()
    assert cache_signal_sb._macos_git_markers(cache_signal_spec(alias)) == markers
    assert scans == []
    second = tmp_path / "second"
    second.mkdir()
    cache_signal_sb._macos_git_markers(cache_signal_spec(second))
    assert scans == [second]
    wide = tmp_path / "wide"
    wide.mkdir()
    for index in range(140):
        (wide / f"dir-{index:03}").mkdir()
    scans.clear()
    cache_signal_sb._macos_git_markers(cache_signal_spec(wide))
    assert len(scans) == 128
    for index in range(70):
        another = tmp_path / f"root-{index}"
        another.mkdir()
        cache_signal_sb._macos_git_markers(cache_signal_spec(another))
    assert len(cache_signal_sb._MacosGitMarkerCache.entries) == 64


def test_git_marker_cache_does_not_hide_scan_errors(tmp_path, monkeypatch, cache_signal_empty_cache):
    def cache_signal_denied(path):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_denied)
    with pytest.raises(PermissionError):
        cache_signal_sb._macos_git_markers(cache_signal_spec(tmp_path))
    assert cache_signal_sb._MacosGitMarkerCache.entries == {}


@pytest.mark.parametrize("error_type", [ValueError, FileNotFoundError])
@pytest.mark.parametrize("retry_seconds", [0.0, 30.0])
def test_plugin_registration_value_error_warns_once(tmp_path, monkeypatch, caplog, error_type, retry_seconds):
    owner = SimpleNamespace(home_dir=tmp_path, plugins_dir=tmp_path / "plugins", identity=OwnerIdentity.local_main())
    installation = SimpleNamespace(enabled=True, activation=None, revision="revision",
                                   activation_id="activation", manifest=SimpleNamespace(
                                       plugin_id="plugin", is_content_only=False))
    registry = SimpleNamespace(_construction_params=SimpleNamespace(
        plugin_owner=owner, host_credential_env_names=frozenset()),
        _mcp_clients=[], _mcp_retry_state={}, _mcp_closed=threading.Event())
    attempts = []

    class cache_signal_BrokenClient:
        def __init__(self, *args, **kwargs):
            attempts.append(True)
            raise error_type("synthetic invalid context")

    monkeypatch.setattr(cache_signal_pr, "PluginMCPClient", cache_signal_BrokenClient)
    monkeypatch.setattr(cache_signal_pr, "_discover_plugin_manifests", lambda _: ((installation,), ()))
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENV_RETRY_SECONDS", retry_seconds)
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENVIRONMENTS", cache_signal_pr.OrderedDict())
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENV_WARNED", cache_signal_pr.OrderedDict())
    with caplog.at_level(logging.DEBUG, logger=cache_signal_pr.__name__):
        for _ in range(5):
            cache_signal_pr.synchronize_plugin_clients(registry)
    records = [r for r in caplog.records if r.name == cache_signal_pr.__name__]
    assert len([r for r in records if r.levelno == logging.WARNING]) == 1
    assert len([r for r in records if r.levelno == logging.DEBUG]) == 4
    assert len(attempts) == (1 if retry_seconds else 5)
    assert error_type.__name__ in records[0].getMessage()
    assert "synthetic invalid context" in records[0].getMessage()
    installation.activation_id = "new-activation"
    with caplog.at_level(logging.WARNING, logger=cache_signal_pr.__name__):
        cache_signal_pr.synchronize_plugin_clients(registry)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_sigusr1_handler_only_sets_event():
    tree = ast.parse(Path(cache_signal_gp.__file__).read_text())
    marker = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "mark_stack_dump")
    assert len(marker.body) == 1
    assert ast.unparse(marker.body[0]) == "pending.set()"
    installer = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                     and node.name == "_install_gateway_signal_handlers")
    assert not any(isinstance(node, ast.Call) and ast.unparse(node.func) == "faulthandler.register"
                   for node in ast.walk(installer))


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="需要POSIX SIGUSR1")
def test_sigusr1_python_safe_dump(tmp_path):
    source = r'''
import faulthandler, json, os, signal, sys, threading
from pathlib import Path
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_process as gp
sys.setswitchinterval(0.001)
register_calls = []
faulthandler.register = lambda *a, **k: register_calls.append(a)
dumped = threading.Event()
class StderrObserver:
    has_dump = False
    def fileno(self):
        return sys.__stderr__.fileno()
    def write(self, text):
        if text.startswith("Gateway SIGUSR1 Python thread dump"):
            self.has_dump = True
        return sys.__stderr__.write(text)
    def flush(self):
        sys.__stderr__.flush()
        if self.has_dump:
            dumped.set()
sys.stderr = StderrObserver()
paths = gateway_paths_from_root(Path(sys.argv[1]))
old_handler = signal.getsignal(signal.SIGUSR1)
previous = gp._install_gateway_signal_handlers(paths, {"pid": os.getpid()})
assert signal.getsignal(signal.SIGUSR1) is previous.diagnostic_handler
assert previous.stack_dumper.thread.daemon
stop, ready = threading.Event(), threading.Barrier(51)
def busy():
    ready.wait(timeout=20)
    while not stop.is_set():
        value = Path("synthetic") / "busy" / "path"
        value.parts
workers = [threading.Thread(target=busy, name=f"cache-signal-busy-{i:02}") for i in range(50)]
try:
    for worker in workers:
        worker.start()
    ready.wait(timeout=20)
    os.kill(os.getpid(), signal.SIGUSR1)
    assert dumped.wait(20), "dumper did not flush"
    assert all(worker.is_alive() for worker in workers)
finally:
    stop.set()
    for worker in workers:
        worker.join(5)
    gp._restore_gateway_signal_handlers(previous)
assert signal.getsignal(signal.SIGUSR1) == old_handler
assert not previous.stack_dumper.thread.is_alive()
assert not any(worker.is_alive() for worker in workers)
assert register_calls == []
print(json.dumps({"busy_threads": len(workers), "register_calls": len(register_calls), "restored": True}))
'''
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"busy_threads": 50, "register_calls": 0, "restored": True}
    for index in range(50):
        assert f"Thread cache-signal-busy-{index:02} " in result.stderr
    assert "Gateway SIGUSR1 Python thread dump" in result.stderr
    assert "Segmentation fault" not in result.stderr


# receipt_fragment_merged
"""W1 #53-4 回归片段；仅使用临时Gateway与真实JSON协议，不证明生产负载数字。"""
import ast as receipt_ast
import json as receipt_json
import os as receipt_os
from dataclasses import replace as receipt_replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as receipt_delivery
from agent_py_agent.agent.gateway_parts import runtime_file_retention as receipt_retention
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic as receipt_write
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root as receipt_paths


def receipt_seed(paths, key='input-a', state='consumed', updated_at=1):
    prepared = {'id': key, 'kind': 'ask', 'goal': 'bounded input',
                'metadata': {'client_input_digest': 'digest-' + key}}
    row = receipt_delivery.GatewayInputReceipt(key, 'digest-' + key, key, state, prepared,
        receipt_delivery._prepared_payload_digest(prepared), key, updated_at=updated_at)
    receipt_write(receipt_delivery.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def receipt_outbox(paths, key, *, entries=None, updated_at=1):
    path = paths.root / 'client_outbox' / (key + '.json')
    receipt_write(path, {'schema_version': 'tui_active_input_outbox.v3',
                         'entries': {} if entries is None else entries, 'updated_at': updated_at})
    return path


def receipt_drain(paths, directory, **kwargs):
    results = []
    for _ in range(100):
        report = receipt_retention.archive_terminal_slice(paths, directory=directory, before=100, max_ms=1000, **kwargs)
        results.append(report)
        if report['complete']:
            return results
    pytest.fail('retention failed to make cross-slice progress')


def test_poison_receipt_quarantined_once(tmp_path, caplog):
    paths = receipt_paths(tmp_path / 'gateway')
    path = paths.root / 'input_receipts' / 'broken.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    results = []
    for tick in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        results.append(summary)
        if tick >= 3:
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)
    assert results[0]['errors'] == results[1]['errors'] == 1
    assert all(row['quarantined'] == 1 and row['errors'] == 0 for row in results[3:])
    assert sum('GATEWAY_INPUT_RECEIPT_QUARANTINED' in row.message for row in caplog.records) == 1
    assert path.read_text() == '{broken'
    # 改正文而不改目录mtime；同名变代必须解除隔离，不能等目录重新扫描。
    directory_stamp = path.parent.stat().st_mtime_ns
    row = receipt_seed(paths, 'fixed')
    prepared = {**row.prepared_request, 'id': 'broken'}
    path.write_text(receipt_json.dumps(receipt_replace(row, request_id='broken',
        prepared_request=prepared, prepared_payload_digest=receipt_delivery._prepared_payload_digest(prepared)).to_dict()))
    receipt_os.utime(path.parent, ns=(directory_stamp, directory_stamp))
    recovered = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    assert recovered['errors'] == 0 and recovered['consumed'] == 2 and recovered['quarantined'] == 0


@pytest.mark.parametrize('error', [OSError('temporary'), BlockingIOError('busy'), PermissionError('temporarily denied')])
def test_receipt_transient_io_never_quarantines(tmp_path, monkeypatch, error):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    def receipt_fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(receipt_delivery, 'read_gateway_input_receipt', receipt_fail)
    for _ in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary['errors'] == 1 and summary['quarantined'] == 0
        with pytest.raises(receipt_delivery.GatewayInputReconcileUnsettledError):
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)


def test_receipt_failure_streak_resets_and_cache_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'one.json'
    path.write_text('{}')
    stamp = receipt_delivery._receipt_failure_stamp(path)
    cache = receipt_delivery._ReceiptFailureCache
    monkeypatch.setattr(cache, 'max_entries', 2)
    with cache.lock:
        cache.entries.clear()
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'OSError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    for index in range(4):
        other = tmp_path / f'{index}.json'
        other.write_text('{}')
        receipt_delivery._record_receipt_failure(other, receipt_delivery._receipt_failure_stamp(other), 'ValueError', 1)
    assert len(cache.entries) <= 2


def test_unchanged_dir_skips_scan_and_pending_retries(tmp_path, monkeypatch):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    calls, scan = [], receipt_delivery.os.scandir
    def receipt_scan(path):
        calls.append(str(path))
        return scan(path)
    monkeypatch.setattr(receipt_delivery.os, 'scandir', receipt_scan)
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    calls.clear()
    assert receipt_delivery.reconcile_gateway_input_receipts(paths, object())['consumed'] == 1
    assert not calls
    # 已消费整圈缓存不能掩蔽原地修改，也不能掩蔽pending的后续重试。
    path = receipt_delivery.gateway_input_receipt_path(paths, 'input-a')
    row = receipt_delivery.read_gateway_input_receipt(paths, 'input-a')
    payload = receipt_replace(row, state='active_pending').to_dict()
    directory_stamp = path.parent.stat().st_mtime_ns
    path.write_text(receipt_json.dumps(payload))
    assert path.parent.stat().st_mtime_ns == directory_stamp
    attempts = []
    def receipt_pending(*args):
        attempts.append(1)
        args[-1]['active_pending'] = args[-1].get('active_pending', 0) + 1
    monkeypatch.setattr(receipt_delivery, '_reconcile_unsettled_receipt', receipt_pending)
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    assert attempts == [1, 1] and not calls


@pytest.mark.parametrize('directory', ['input_receipts', 'client_outbox'])
def test_retention_moves_only_terminal_old_entries(tmp_path, directory):
    paths = receipt_paths(tmp_path / 'gateway')
    if directory == 'input_receipts':
        receipt_seed(paths, 'old')
        receipt_seed(paths, 'young', updated_at=1000)
        receipt_seed(paths, 'pending', state='active_pending')
    else:
        receipt_outbox(paths, 'old')
        receipt_outbox(paths, 'young', updated_at=1000)
        receipt_outbox(paths, 'pending', entries={'message-a': {'request_id': 'pending'}})
    folder = paths.root / directory
    lock = folder / 'orphan.json.lock'
    lock.write_text('keep')
    reports = receipt_drain(paths, directory, max_entries=1)
    assert sum(row['archived_entries'] for row in reports) == 1
    assert all(row['scanned_entries'] <= 1 for row in reports)
    assert reports[0]['complete'] is False
    assert (paths.root / 'archive' / directory / '197001' / 'old.json').is_file()
    if directory == 'input_receipts':
        # 未迁移读者仍需热件：核冷备份与原件逐字节相同，不恢复危险删除来满足旧期望。
        assert (folder / 'old.json').read_bytes() == (paths.root / 'archive' / directory / '197001' / 'old.json').read_bytes()
        assert all(row['reader_cutover_required'] is True for row in reports)
        assert all(row['deferred_reason'] == 'input_receipt_reader_cutover_required' for row in reports)
    else:
        assert not (folder / 'old.json').exists()
    assert (folder / 'young.json').exists() and (folder / 'pending.json').exists()
    assert lock.read_text() == 'keep' and not any(row['errors'] for row in reports)
    if directory == 'input_receipts':
        row = receipt_delivery.read_gateway_input_receipt(paths, 'old')
        assert row.state == 'consumed'
        reused, created = receipt_delivery.load_or_prepare_gateway_input_locked(paths, request_id='old',
            client_input_digest=row.client_input_digest, client_message_id=row.client_message_id,
            guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request)
        assert not created and reused == row


def test_receipt_retention_byte_budget_active_file_and_symlink(tmp_path):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths, 'old')
    receipt_seed(paths, 'active')
    receipt_write(paths.processing / 'active.json', {'id': 'active'})
    folder = paths.root / 'input_receipts'
    (folder / 'link.json').symlink_to(folder / 'old.json')
    report = receipt_retention.archive_terminal_slice(paths, directory='input_receipts', before=100,
        max_bytes=1, max_ms=1000)
    assert not report['complete'] and report['archived_entries'] == 0
    reports = receipt_drain(paths, 'input_receipts', max_entries=1)
    assert sum(row['archived_entries'] for row in reports) == 1
    assert (folder / 'active.json').exists() and (folder / 'link.json').is_symlink()
    with pytest.raises(ValueError, match='not_enabled'):
        receipt_retention.archive_terminal_slice(paths, directory='control_operations', before=100)


def test_receipt_retention_publish_failure_keeps_original(tmp_path, monkeypatch):
    paths = receipt_paths(tmp_path / 'gateway')
    row = receipt_seed(paths)
    def receipt_failed_write(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(receipt_retention, 'write_bytes_atomic_beneath', receipt_failed_write)
    reports = receipt_drain(paths, 'input_receipts')
    assert any(report['errors'] for report in reports)
    assert receipt_delivery.read_gateway_input_receipt(paths, row.request_id) == row
    assert receipt_delivery.gateway_input_receipt_path(paths, row.request_id).exists()


def test_receipt_scan_context_closed_and_lru_bounded(tmp_path, monkeypatch):
    # 缓存跨片拥有目录上下文；完成或逐出时必须关闭，不允许每拍新增一个未管理句柄。
    original, closed = receipt_retention.os.scandir, []
    class ReceiptScan:
        def __init__(self, path):
            self.inner = original(path)
        def __enter__(self):
            return self.inner
        def __exit__(self, *args):
            self.inner.close()
            closed.append(1)
    with receipt_retention._RuntimeScans.lock:
        for key in list(receipt_retention._RuntimeScans.entries):
            receipt_retention._close_scan(key)
    monkeypatch.setattr(receipt_retention.os, 'scandir', ReceiptScan)
    monkeypatch.setattr(receipt_retention._RuntimeScans, 'max_directories', 2)
    for index in range(4):
        paths = receipt_paths(tmp_path / str(index))
        receipt_outbox(paths, 'pending', entries={'a': {}})
        receipt_retention.archive_terminal_slice(paths, directory='client_outbox', before=100, max_entries=1, max_ms=1000)
    assert len(receipt_retention._RuntimeScans.entries) == 2 and len(closed) == 2
    for key in list(receipt_retention._RuntimeScans.entries):
        receipt_retention._close_scan(key)
    assert len(closed) == 4


def test_receipt_process_list_preserves_order_and_reports_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_store as receipt_sessions
    from agent_py_agent.tests.test_process_session_store import _reservation as receipt_reservation
    store = receipt_sessions.ProcessSessionStore(tmp_path / 'sessions')
    for key in ('bg-z', 'bg-a', 'bg-m'):
        store.write(receipt_reservation(key))
    records, errors = store.list_records()
    assert [row['session_id'] for row in records] == ['bg-a', 'bg-m', 'bg-z'] and not errors
    calls, original = [], receipt_sessions.os.scandir
    class ReceiptScan:
        def __init__(self, path):
            self.inner = original(path)
        def __enter__(self):
            return self.inner
        def __exit__(self, *args):
            self.inner.close()
            calls.append('closed')
    monkeypatch.setattr(receipt_sessions.os, 'scandir', ReceiptScan)
    records, errors = store.list_records(max_entries=1)
    assert records == [] and len(errors) == 1 and 'process_session_scan_budget_exceeded' in errors[0]['message']
    assert calls == ['closed']
    records, errors = store.list_records(max_ms=0.000001)
    assert not records and errors


def test_receipt_maintenance_safe_gates_and_cross_slice_progress(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home as receipt_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_retention_if_due as receipt_maintain,
    )
    from agent_py_agent.agent.user_space.runtime_paths import (
        runtime_paths_for_agent as receipt_runtime_paths,
    )
    home = receipt_home(tmp_path / 'home')
    paths = receipt_paths(receipt_runtime_paths(None, home.owner_home_dir, home)['gateway_workspace'])
    receipt_seed(paths)
    original_input = receipt_delivery.gateway_input_receipt_path(paths, 'input-a').read_bytes()
    receipt_outbox(paths, 'empty')
    for tick in range(20):
        receipt_maintain(home, now=2000000 + tick, include_global_indexes=False,
                        quota_max_entries=4096, quota_max_ms=1000)
        state = receipt_json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        if not state.get('maintenance_partial'):
            break
    assert not state['maintenance_partial']
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').exists()
    assert (paths.root / 'archive/client_outbox/197001/empty.json').exists()
    assert receipt_delivery.gateway_input_receipt_path(paths, 'input-a').read_bytes() == original_input
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == original_input
    assert not (paths.root / 'client_outbox/empty.json').exists()


def test_receipt_retention_legal_hold_preserves_runtime_files(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home as receipt_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_retention_if_due as receipt_maintain,
    )
    from agent_py_agent.agent.user_space.runtime_paths import (
        runtime_paths_for_agent as receipt_runtime_paths,
    )
    home = receipt_home(tmp_path / 'home')
    paths = receipt_paths(receipt_runtime_paths(None, home.owner_home_dir, home)['gateway_workspace'])
    receipt_seed(paths)
    receipt_outbox(paths, 'empty')
    policy = receipt_json.loads(home.owner_retention_json.read_text())
    policy['legal_hold'] = True
    home.owner_retention_json.write_text(receipt_json.dumps(policy))
    receipt_maintain(home, now=2000000, include_global_indexes=False, quota_max_entries=4096, quota_max_ms=1000)
    assert receipt_delivery.gateway_input_receipt_path(paths, 'input-a').exists()
    assert (paths.root / 'client_outbox/empty.json').exists()
    assert not (paths.root / 'archive/runtime-files.sqlite3').exists()


def test_receipt_hot_scans_use_context_managers_static():
    import inspect as receipt_inspect

    from agent_py_agent.agent.tooling import process_session_store as receipt_sessions
    for function in (receipt_retention._directory_entries, receipt_delivery._cached_receipt_candidates,
                     receipt_sessions.ProcessSessionTransaction.list_records):
        import textwrap as receipt_textwrap
        tree = receipt_ast.parse(receipt_textwrap.dedent(receipt_inspect.getsource(function)))
        assert any(isinstance(node, receipt_ast.With) and any(
            isinstance(item.context_expr, receipt_ast.Call) and isinstance(item.context_expr.func, receipt_ast.Attribute)
            and item.context_expr.func.attr == 'scandir' for item in node.items) for node in receipt_ast.walk(tree))


def test_receipt_persisted_terminal_unknown_failure_is_quarantined(tmp_path, monkeypatch, caplog):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths, state='terminal_unknown')
    def receipt_unsettled(paths, agent, path, summary):
        summary['checked'] += 1
        summary['terminal_unknown'] += 1
        summary['terminal_unknown_errors'] += 1
        summary['last_terminal_unknown_error'] = {'request_id': path.stem, 'error_type': 'DataCorruptionError'}
    monkeypatch.setattr(receipt_delivery, '_reconcile_receipt_path', receipt_unsettled)
    for tick in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        if tick >= 3:
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)
            assert summary['quarantined'] == 1
    assert sum('GATEWAY_INPUT_RECEIPT_QUARANTINED' in row.message for row in caplog.records) == 1


def test_receipt_json_report_io_failure_does_not_become_poison(tmp_path, monkeypatch):
    from types import SimpleNamespace as ReceiptNamespace
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    original = receipt_delivery.read_json_file_report
    def receipt_io_report(path, **options):
        if path.name == 'input-a.json':
            return ReceiptNamespace(payload={}, lock_busy=False,
                load_error={'category': 'io', 'error_type': 'OSError'})
        return original(path, **options)
    monkeypatch.setattr(receipt_delivery, 'read_json_file_report', receipt_io_report)
    for _ in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary['errors'] == 1 and summary['quarantined'] == 0


def test_receipt_plugin_owned_input_stays_for_unmigrated_reader(tmp_path):
    paths = receipt_paths(tmp_path / 'gateway')
    row = receipt_seed(paths)
    prepared = {**row.prepared_request, 'metadata': {
        **row.prepared_request['metadata'], 'plugin_provenance': {'installation_ref': 'test-installation'}}}
    row = receipt_replace(row, prepared_request=prepared,
        prepared_payload_digest=receipt_delivery._prepared_payload_digest(prepared))
    receipt_write(receipt_delivery.gateway_input_receipt_path(paths, row.request_id), row.to_dict())
    assert sum(item['archived_entries'] for item in receipt_drain(paths, 'input_receipts')) == 0
    assert receipt_delivery.gateway_input_receipt_path(paths, row.request_id).exists()

# retention_rework_fragment_merged
"""归档返工反例：丢索引失败关闭、未迁移读者保热件；供父级合并。"""

from agent_py_agent.agent.gateway_parts import input_delivery_service as rework_inputs
from agent_py_agent.agent.gateway_parts import runtime_file_retention as rework_retention
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic as rework_write
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root as rework_paths
from agent_py_agent.agent.runtime_errors import DataCorruptionError as ReworkCorruption


def rework_seed(paths, key='input-a', *, state='consumed', metadata=None):
    prepared = {'id': key, 'kind': 'ask', 'goal': 'original complete request', 'user_id': 'user-a',
                'conversation': {'channel_conversation_id': 'conversation-a'},
                'metadata': {'client_input_digest': 'digest-' + key, 'channel': 'chat', **(metadata or {})}}
    row = rework_inputs.GatewayInputReceipt(key, 'digest-' + key, 'message-a', state, prepared,
        rework_inputs._prepared_payload_digest(prepared), 'original-guidance-key', updated_at=1)
    rework_write(rework_inputs.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def rework_outbox(paths, key, *, entries=None):
    path = paths.root / 'client_outbox' / (key + '.json')
    rework_write(path, {'schema_version': 'tui_active_input_outbox.v3',
                        'entries': {} if entries is None else entries, 'updated_at': 1})
    return path


def rework_archive(paths, directory):
    reports = []
    for _ in range(100):
        report = rework_retention.archive_terminal_slice(paths, directory=directory, before=100,
            max_entries=1, max_ms=1000)
        reports.append(report)
        if report['complete']:
            return reports
    pytest.fail('bounded retention failed to finish')


def rework_prepare(paths, row, prepare=None):
    return rework_inputs.load_or_prepare_gateway_input_locked(paths, request_id=row.request_id,
        client_input_digest=row.client_input_digest, client_message_id=row.client_message_id,
        guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request,
        prepare_request=prepare)


@pytest.mark.parametrize('legacy_without_marker', [False, True])
def test_rework_missing_index_never_reprepares_consumed_input(tmp_path, legacy_without_marker):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    assert not any(r['errors'] for r in rework_archive(paths, 'input_receipts'))
    cold = paths.root / 'archive/input_receipts/197001/input-a.json'
    assert json.loads(cold.read_text()) == row.to_dict()
    # 模拟已由旧版本删掉热件的存量；不是让新生产路径执行危险删除。
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink(missing_ok=True)
    marker = paths.root / 'archive/runtime-files-published.json'
    if legacy_without_marker:
        marker.unlink(missing_ok=True)
    (paths.root / 'archive/runtime-files.sqlite3').unlink()
    prepare = Mock(side_effect=AssertionError('must not prepare a second input'))
    with pytest.raises(ReworkCorruption):
        rework_prepare(paths, row, prepare)
    prepare.assert_not_called()
    assert not hot.exists() and not (paths.root / 'archive/runtime-files.sqlite3').exists()
    assert json.loads(cold.read_text()) == row.to_dict()


def test_rework_writer_cannot_recreate_lost_authority_index(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    rework_outbox(paths, 'first')
    assert not any(r['errors'] for r in rework_archive(paths, 'client_outbox'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    index.unlink()
    second = rework_outbox(paths, 'second')
    before = second.read_bytes()
    reports = rework_archive(paths, 'client_outbox')
    assert any(r['errors'] for r in reports)
    assert second.read_bytes() == before and not index.exists()


def test_rework_input_copy_retains_original_and_original_prepare_identity(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    before = hot.read_bytes()
    reports = rework_archive(paths, 'input_receipts')
    assert hot.exists(), 'unmigrated readers still require the original hot receipt'
    assert hot.read_bytes() == before
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == before
    assert any(r.get('deferred_reason') == 'input_receipt_reader_cutover_required' for r in reports)
    assert not any(r['errors'] for r in reports)
    reused, created = rework_prepare(paths, row)
    assert not created and reused == row
    with pytest.raises(ValueError):
        rework_inputs.load_or_prepare_gateway_input_locked(paths, request_id=row.request_id,
            client_input_digest='another input', client_message_id=row.client_message_id,
            guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request)


def test_rework_late_control_still_observes_hot_receipt_without_ordinary_metadata(tmp_path):
    from agent_py_agent.agent.gateway_parts import control_service as controls
    from agent_py_agent.agent.gateway_parts.http_handlers import http_input_identity

    paths = rework_paths(tmp_path / 'gateway')
    scope = controls.GatewayControlScope('user-a', 'chat', 'conversation-a', metadata={'message_id': 'message-a'})
    key, _ = http_input_identity(user_id=scope.user_id, channel=scope.channel,
        conversation_id=scope.conversation_id, message_id='message-a')
    rework_seed(paths, key)
    store = SimpleNamespace(guidance=SimpleNamespace(append_once=Mock()))
    before = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    rework_archive(paths, 'input_receipts')
    after = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    store.guidance.append_once.assert_not_called()
    assert before.error_code == after.error_code == 'INPUT_IDENTITY_CONFLICT'


def test_rework_production_maintenance_retains_input_but_archives_empty_outbox(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = rework_paths(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    original = hot.read_bytes()
    outbox = rework_outbox(paths, 'empty')
    pending = rework_outbox(paths, 'pending', entries={'message': {'request_id': 'pending'}})
    for tick in range(20):
        run_owner_retention_if_due(home, now=2000000 + tick, include_global_indexes=False,
            quota_max_entries=4096, quota_max_ms=1000)
        state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        if not state.get('maintenance_partial'):
            break
    assert not state['maintenance_partial']
    assert hot.exists(), 'production maintenance may not delete receipts needed by control'
    assert hot.read_bytes() == original and pending.exists() and not outbox.exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == original
    assert (paths.root / 'archive/client_outbox/197001/empty.json').exists()


def test_rework_cold_read_disappearing_between_lookup_and_read_is_not_fresh(tmp_path, monkeypatch):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    rework_archive(paths, 'input_receipts')
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink(missing_ok=True)
    original_read = rework_inputs.read_json_file_report

    def vanished(path, **kwargs):
        if 'archive' in path.parts:
            return SimpleNamespace(payload={}, load_error=None, lock_busy=False)
        return original_read(path, **kwargs)

    monkeypatch.setattr(rework_inputs, 'read_json_file_report', vanished)
    with pytest.raises(ReworkCorruption):
        rework_prepare(paths, row)
    assert not hot.exists()


def test_rework_publication_marker_alone_prevents_missing_index_becoming_absence(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    rework_outbox(paths, 'old')
    rework_archive(paths, 'client_outbox')
    marker = paths.root / 'archive/runtime-files-published.json'
    assert json.loads(marker.read_text())['schema_version'] == 'gateway_runtime_files_published.v1'
    cold = paths.root / 'archive/client_outbox/197001/old.json'
    cold.unlink()
    cold.parent.rmdir()
    cold.parent.parent.rmdir()
    (paths.root / 'archive/runtime-files.sqlite3').unlink()
    with pytest.raises(ReworkCorruption):
        rework_retention.lookup_runtime_file(paths, 'input_receipts', 'unseen.json')


def test_rework_genuinely_new_gateway_can_prepare(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    assert rework_retention.lookup_runtime_file(paths, 'input_receipts', 'first.json') is None
    row = rework_seed(paths, 'first')
    rework_inputs.gateway_input_receipt_path(paths, 'first').unlink()
    created_row, created = rework_prepare(paths, row)
    assert created and created_row.state == 'pending'
    assert created_row.prepared_request == row.prepared_request


@pytest.mark.parametrize('stage', ['marker', 'bytes', 'index'])
def test_rework_publish_failure_never_deletes_source(tmp_path, monkeypatch, stage):
    paths = rework_paths(tmp_path / 'gateway')
    path = rework_outbox(paths, 'old')
    original = path.read_bytes()
    write = rework_retention.write_bytes_atomic_beneath
    index = rework_retention._index

    def broken_write(root, parts, raw):
        if (stage == 'marker' and parts[-1] == 'runtime-files-published.json'
                or stage == 'bytes' and parts[-1] == 'old.json'):
            raise OSError('injected publication failure')
        return write(root, parts, raw)

    def broken_index(paths, *, writable=False):
        if stage == 'index' and writable:
            raise OSError('injected index failure')
        return index(paths, writable=writable)

    monkeypatch.setattr(rework_retention, 'write_bytes_atomic_beneath', broken_write)
    monkeypatch.setattr(rework_retention, '_index', broken_index)
    reports = rework_archive(paths, 'client_outbox')
    assert any(r['errors'] for r in reports) and path.read_bytes() == original


def test_rework_repeated_input_copy_is_idempotent_and_budgeted(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    report = rework_retention.archive_terminal_slice(paths, directory='input_receipts', before=100,
        max_bytes=1, max_ms=1000)
    assert not report['complete'] and report['bytes'] == 0 and hot.exists()
    first = rework_archive(paths, 'input_receipts')
    assert sum(r['archived_entries'] for r in first) == 1
    before = hot.read_bytes()
    again = rework_archive(paths, 'input_receipts')
    assert sum(r['archived_entries'] for r in again) == 0
    assert all(r['scanned_entries'] <= 1 for r in again)
    assert hot.read_bytes() == before


def test_rework_static_input_retention_returns_before_any_unlink():
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(rework_retention._archive_one)))
    body = tree.body[0].body[0].body
    input_return = next(i for i, node in enumerate(body) if isinstance(node, ast.If)
        and ast.unparse(node.test) == "directory == 'input_receipts'"
        and any(isinstance(child, ast.Return) for child in node.body))
    unlink = next(i for i, node in enumerate(body) if any(isinstance(child, ast.Call)
        and ast.unparse(child.func) == 'os.unlink' for child in ast.walk(node)))
    assert input_return < unlink
