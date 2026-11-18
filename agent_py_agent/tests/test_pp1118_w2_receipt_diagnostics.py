"""W2 回归与诊断测试：回执逐字段错误码、SQLite 持久隔离与非阻塞可让渡对账。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery_pending_index import (
    RecoveryPendingIndex,
    projection_file_stamp,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_steer_delivery_recovery import _background_input


def _clear_receipt_caches() -> None:
    with delivery._ReceiptFailureCache.lock:
        delivery._ReceiptFailureCache.entries.clear()
    with delivery._CONSUMED_RECEIPT_STAMP_LOCK:
        delivery._CONSUMED_RECEIPT_STAMP_CACHE.clear()
    with delivery._RECEIPT_DIR_CACHE_LOCK:
        delivery._RECEIPT_DIR_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolated_receipt_caches():
    _clear_receipt_caches()
    try:
        yield
    finally:
        _clear_receipt_caches()


def _valid_receipt_dict(request_id: str = "gwreq-msg-w2", *, state: str = "consumed") -> dict:
    owner = {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    prepared = {
        "id": request_id,
        "request_id": request_id,
        "kind": "ask",
        "goal": "diagnostic test input",
        "user_id": "local-agent",
        REQUEST_OWNER_KEY: dict(owner),
        "owner_id": "main",
        "metadata": {
            "client_input_digest": f"digest-{request_id}",
            REQUEST_OWNER_KEY: dict(owner),
        },
    }
    receipt = delivery.GatewayInputReceipt(
        request_id=request_id,
        client_input_digest=f"digest-{request_id}",
        client_message_id=f"msg-{request_id}",
        state=state,
        prepared_request=prepared,
        prepared_payload_digest=delivery._prepared_payload_digest(prepared),
        guidance_dedupe_key=f"dedupe-{request_id}",
        updated_at=100.0,
    )
    return receipt.to_dict()


@pytest.mark.parametrize(
    "case_name,expected_code,expected_field,expected_state",
    [
        ("schema_version", "INPUT_RECEIPT_SCHEMA_UNSUPPORTED", "schema_version", "consumed"),
        ("state", "INPUT_RECEIPT_STATE_INVALID", "state", ""),
        ("request_id", "INPUT_RECEIPT_REQUEST_ID_MISSING", "request_id", "consumed"),
        ("client_input_digest", "INPUT_RECEIPT_CLIENT_DIGEST_MISSING", "client_input_digest", "consumed"),
        ("prepared_request.id", "INPUT_RECEIPT_PREPARED_ID_MISMATCH", "prepared_request.id", "consumed"),
        (
            "metadata_digest",
            "INPUT_RECEIPT_CLIENT_DIGEST_MISMATCH",
            "prepared_request.metadata.client_input_digest",
            "consumed",
        ),
        ("payload_digest", "INPUT_RECEIPT_PAYLOAD_DIGEST_MISMATCH", "prepared_payload_digest", "consumed"),
        ("owner_mismatch", "INPUT_RECEIPT_OWNER_BINDING_MISMATCH", "prepared_request.host_request_owner.v1", "consumed"),
        ("owner_invalid", "INPUT_RECEIPT_OWNER_BINDING_INVALID", "prepared_request.host_request_owner.v1", "consumed"),
        ("transport_non_v3", "INPUT_RECEIPT_TRANSPORT_REQUIRES_V3", "transport", "consumed"),
        ("updated_at", "INPUT_RECEIPT_TIMESTAMP_INVALID", "updated_at", "consumed"),
        ("legacy_schema", "INPUT_RECEIPT_LEGACY_SCHEMA", "schema_version", "consumed"),
    ],
)
def test_blank_or_invalid_receipt_field_yields_distinct_diagnostics(
    tmp_path: Path, case_name: str, expected_code: str, expected_field: str, expected_state: str
) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    request_id = "gwreq-msg-matrix"
    payload = _valid_receipt_dict(request_id, state="consumed")
    if case_name == "schema_version":
        payload["schema_version"] = ""
    elif case_name == "state":
        payload["state"] = ""
    elif case_name == "request_id":
        payload["request_id"] = ""
    elif case_name == "client_input_digest":
        payload["client_input_digest"] = ""
    elif case_name == "prepared_request.id":
        payload["prepared_request"]["id"] = ""
    elif case_name == "metadata_digest":
        payload["prepared_request"]["metadata"]["client_input_digest"] = ""
    elif case_name == "payload_digest":
        payload["prepared_payload_digest"] = ""
    elif case_name == "owner_mismatch":
        payload["prepared_request"][REQUEST_OWNER_KEY] = {
            "provider": "feishu",
            "owner_kind": "user",
            "owner_id": "other-user",
        }
    elif case_name == "owner_invalid":
        payload["prepared_request"][REQUEST_OWNER_KEY] = {"provider": "local"}
    elif case_name == "transport_non_v3":
        payload["transport"] = {"unexpected": True}
    elif case_name == "updated_at":
        payload["updated_at"] = "not-a-timestamp"
    elif case_name == "legacy_schema":
        payload["schema_version"] = "gateway_input_receipt.v1"

    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, request_id), payload)

    with pytest.raises(DataCorruptionError) as caught:
        delivery.read_gateway_input_receipt(paths, request_id)
    assert type(caught.value) is DataCorruptionError
    assert caught.value.reason_code == expected_code
    assert caught.value.field == expected_field
    assert caught.value.receipt_state == expected_state


def test_empty_and_unreadable_receipt_files_have_structured_diagnostics(tmp_path: Path) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    empty_path = delivery.gateway_input_receipt_path(paths, "gwreq-empty")
    empty_path.parent.mkdir(parents=True, exist_ok=True)
    empty_path.write_text("{}", encoding="utf-8")
    with pytest.raises(DataCorruptionError) as empty_exc:
        delivery.read_gateway_input_receipt(paths, "gwreq-empty")
    assert empty_exc.value.reason_code == "INPUT_RECEIPT_EMPTY"
    assert empty_exc.value.field == "<file>"

    bad_path = delivery.gateway_input_receipt_path(paths, "gwreq-bad-json")
    bad_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(DataCorruptionError) as bad_exc:
        delivery.read_gateway_input_receipt(paths, "gwreq-bad-json")
    assert bad_exc.value.reason_code == "INPUT_RECEIPT_UNREADABLE"
    assert bad_exc.value.field == "<json>"


@pytest.mark.parametrize(
    "state,expected_next",
    [
        ("consumed", "already_consumed_no_replay"),
        ("terminal_unknown", "check_message_lifecycle_before_any_replay"),
        ("queued", "inspect_receipt_fields_read_only"),
    ],
)
def test_reconcile_summary_last_error_and_unsettled_error_include_diagnostics(
    tmp_path: Path, state: str, expected_next: str
) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    request_id = "gwreq-msg-detail"
    payload = _valid_receipt_dict(request_id, state=state)
    payload["prepared_request"]["id"] = ""
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, request_id), payload)

    summary = delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    assert summary["errors"] == 1
    assert summary["last_error"]["reason_code"] == "INPUT_RECEIPT_PREPARED_ID_MISMATCH"
    assert summary["last_error"]["field"] == "prepared_request.id"
    assert summary["last_error"]["receipt_state"] == state
    assert summary["last_error"]["effect"] == "not_started"
    assert summary["last_error"]["next_step"] == expected_next

    with pytest.raises(delivery.GatewayInputReconcileUnsettledError) as unsettled:
        delivery.raise_if_input_reconcile_unsettled(summary)
    assert f"last={request_id}:DataCorruptionError:INPUT_RECEIPT_PREPARED_ID_MISMATCH" in str(unsettled.value)


def test_durable_quarantine_survives_restart_and_avoids_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    bad_a = _valid_receipt_dict("gwreq-bad-a", state="consumed")
    bad_a["prepared_request"]["id"] = ""
    bad_b = _valid_receipt_dict("gwreq-bad-b", state="consumed")
    bad_b["prepared_payload_digest"] = "wrong-digest"
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, "gwreq-bad-a"), bad_a)
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, "gwreq-bad-b"), bad_b)

    for tick in range(3):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
        if tick < 2:
            assert summary["errors"] == 2
            assert summary["quarantined"] == 0
        else:
            # 同一拍持久化后不得把刚落盘的隔离条目重复计数。
            assert summary["errors"] == 0
            assert summary["quarantined"] == 2

    quarantine_logs = [row for row in caplog.records if "GATEWAY_INPUT_RECEIPT_QUARANTINED" in row.message]
    assert len(quarantine_logs) == 2
    assert any(
        "reason_code=INPUT_RECEIPT_PREPARED_ID_MISMATCH" in row.message
        and "field=prepared_request.id" in row.message
        and "receipt_state=consumed" in row.message
        and "effect=not_started" in row.message
        and "next_step=already_consumed_no_replay" in row.message
        for row in quarantine_logs
    )

    # 模拟进程重启：清空进程内缓存后再次对账，不应再打开或重读已隔离回执，也不应再打隔离警告。
    _clear_receipt_caches()
    caplog.clear()
    read_calls: list[str] = []
    original_read = delivery.read_gateway_input_receipt

    def spy_read(arg_paths, request_id, **kwargs):
        read_calls.append(request_id)
        return original_read(arg_paths, request_id, **kwargs)

    monkeypatch.setattr(delivery, "read_gateway_input_receipt", spy_read)
    tick4 = delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    assert read_calls == []
    assert tick4["errors"] == 0
    assert tick4["quarantined"] == 2
    assert not any("GATEWAY_INPUT_RECEIPT_QUARANTINED" in row.message for row in caplog.records)
    index = RecoveryPendingIndex(paths.root)
    assert index.quarantined_count("receipt") == 2
    assert [key for key, _ver in index.quarantined_page("receipt")] == ["gwreq-bad-a", "gwreq-bad-b"]


def test_in_place_edit_without_dir_mtime_change_unquarantines_and_settles(tmp_path: Path) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    request_id = "gwreq-edit-inplace"
    broken = _valid_receipt_dict(request_id, state="consumed")
    broken["client_input_digest"] = ""
    path = delivery.gateway_input_receipt_path(paths, request_id)
    write_json_file_atomic(path, broken)

    for _ in range(3):
        delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    assert RecoveryPendingIndex(paths.root).quarantined_count("receipt") == 1

    # 模拟重启并原地修复文件内容，同时锁定父目录 mtime 不变。
    _clear_receipt_caches()
    dir_mtime_ns = path.parent.stat().st_mtime_ns
    fixed = _valid_receipt_dict(request_id, state="consumed")
    path.write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
    os.utime(path.parent, ns=(dir_mtime_ns, dir_mtime_ns))

    recovered = delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    assert recovered["errors"] == 0
    assert recovered["consumed"] == 1
    assert recovered["quarantined"] == 0
    assert RecoveryPendingIndex(paths.root).quarantined_count("receipt") == 0


def test_quarantine_persist_failure_falls_back_to_in_process_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    broken = _valid_receipt_dict("gwreq-db-fail", state="consumed")
    broken["request_id"] = ""
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, "gwreq-db-fail"), broken)

    def fail_quarantine(*_args, **_kwargs):
        raise OSError("projection temporarily unavailable")

    monkeypatch.setattr(RecoveryPendingIndex, "quarantine", fail_quarantine)
    for _ in range(2):
        delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    summary = delivery.reconcile_gateway_input_receipts(paths, object(), quarantine_after_failures=3)
    assert summary["errors"] == 0
    assert summary["quarantined"] == 1
    assert any("GATEWAY_INPUT_RECEIPT_QUARANTINE_NOT_PERSISTED" in row.message for row in caplog.records)


def test_recovery_pending_index_rollback_safety_and_counter_transitions(tmp_path: Path) -> None:
    index = RecoveryPendingIndex(tmp_path)
    receipt_path = tmp_path / "input_receipts" / "item-1.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text("{}", encoding="utf-8")
    stamp = projection_file_stamp(receipt_path)

    version = index.begin("receipt", "item-1")
    # 版本不匹配 CAS 必须失败，不改变计数。
    assert index.quarantine("receipt", "item-1", version + 1, stamp) is False
    assert index.quarantined_count("receipt") == 0

    assert index.quarantine("receipt", "item-1", version, stamp) is True
    assert index.quarantined_count("receipt") == 1
    assert index.quarantined_page("receipt") == (("item-1", version),)

    # 回滚安全：schema version 保持 1，旧二进制的 pending=1 查询与 caught_up 均看不到 pending=2 行。
    assert index.page("receipt") == ()
    assert index.caught_up("receipt") is True
    with index.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT request_id,version FROM entries WHERE kind='receipt' AND pending=1").fetchall() == []

    # 再次隔离幂等失败，计数不变。
    assert index.quarantine("receipt", "item-1", version, stamp) is False
    assert index.quarantined_count("receipt") == 1

    # wake / begin / acknowledge / forget 从隔离态移出时均同步递减计数。
    index.wake("receipt", "item-1", version)
    assert index.quarantined_count("receipt") == 0
    assert index.page("receipt") == (("item-1", version),)

    assert index.quarantine("receipt", "item-1", version, stamp) is True
    assert index.quarantined_count("receipt") == 1
    assert index.acknowledge("receipt", "item-1", version, stamp) is True
    assert index.quarantined_count("receipt") == 0

    version2 = index.begin("receipt", "item-1")
    assert index.quarantine("receipt", "item-1", version2, stamp) is True
    assert index.quarantined_count("receipt") == 1
    version3 = index.begin("receipt", "item-1")
    assert index.quarantined_count("receipt") == 0
    assert index.quarantine("receipt", "item-1", version3, stamp) is True
    index.forget("receipt", "item-1", version3)
    assert index.quarantined_count("receipt") == 0
    # 终态 kind 绝不因回执隔离而写入 pending=2。
    assert index.quarantined_count("terminal") == 0


def test_should_yield_processes_one_receipt_commits_cursor_and_resumes_next(tmp_path: Path) -> None:
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for idx in range(3):
        req_id = f"gwreq-yield-{idx}"
        write_json_file_atomic(
            delivery.gateway_input_receipt_path(paths, req_id),
            _valid_receipt_dict(req_id, state="consumed"),
        )

    first = delivery.reconcile_gateway_input_receipts(paths, object(), should_yield=lambda: True)
    assert first["checked"] == 1
    assert first["consumed"] == 1
    assert first["yielded"] is True
    cursor = json.loads((paths.root / "input_receipts/state/reconcile_cursor.json").read_text(encoding="utf-8"))
    assert cursor["last_name"] == "gwreq-yield-0.json"

    second = delivery.reconcile_gateway_input_receipts(paths, object(), should_yield=lambda: False)
    assert second["checked"] == 2
    assert second["consumed"] == 2
    assert second["yielded"] is False
    cursor2 = json.loads((paths.root / "input_receipts/state/reconcile_cursor.json").read_text(encoding="utf-8"))
    assert cursor2["last_name"] == "gwreq-yield-2.json"

    empty = delivery.reconcile_gateway_input_receipts(paths, object(), should_yield=lambda: True)
    assert empty["checked"] == 0
    assert empty["yielded"] is False


def test_periodic_reconcile_skips_busy_target_locks_without_blocking_or_error(tmp_path: Path) -> None:
    agent, paths, request_id = _background_input(tmp_path, "busy-task")
    store = agent.conversation_store
    held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_turn_guard() -> None:
        try:
            with store.guidance.ledger.turn_guard("busy-task"):
                held.set()
                release.wait(10)
        except BaseException as exc:  # pragma: no cover - 仅失败诊断
            errors.append(exc)

    worker = threading.Thread(target=hold_turn_guard)
    worker.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        busy_summary = delivery.reconcile_gateway_input_receipts(paths, agent)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert busy_summary["deferred_busy"] == 1
        assert busy_summary["errors"] == 0
        assert busy_summary["quarantined"] == 0
        with delivery._ReceiptFailureCache.lock:
            assert not delivery._ReceiptFailureCache.entries
        assert [key for key, _ in RecoveryPendingIndex(paths.root).page("receipt")] == [request_id]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors

    store.tasks.update_status({"task_id": "busy-task", "status": "completed"})
    settled = delivery.reconcile_gateway_input_receipts(paths, agent)
    assert settled["deferred_busy"] == 0
    assert settled["errors"] == 0
    assert settled["queued"] == 1
    assert delivery.read_gateway_input_receipt(paths, request_id).state == "queued"


@pytest.mark.parametrize("lock_kind", ["gateway_turn_transition", "task_transition"])
def test_reconcile_bound_input_busy_skip_uses_nonblocking_target_locks(
    tmp_path: Path, lock_kind: str
) -> None:
    agent, paths, request_id = _background_input(tmp_path, "target-busy-task")
    store = agent.conversation_store
    receipt = delivery.read_gateway_input_receipt(paths, request_id)
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        ctx = (
            delivery.gateway_input_transition(paths, "target-busy-task")
            if lock_kind == "gateway_turn_transition"
            else store.tasks.transition_guard("target-busy-task")
        )
        with ctx:
            held.set()
            release.wait(10)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(BlockingIOError):
            delivery._reconcile_bound_input(paths, store, receipt, busy_skip=True)
        assert time.monotonic() - started < 1.0

        with pytest.raises(BlockingIOError):
            delivery._reconcile_unsettled_receipt(paths, agent, receipt, {"terminal_unknown": 0}, busy_skip=True)

        summary = delivery.reconcile_gateway_input_receipts(paths, agent)
        assert summary["deferred_busy"] == 1
        assert summary["errors"] == 0
        assert summary["quarantined"] == 0
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()

    # 默认 busy_skip=False（收口/恢复调用方）保持阻塞语义：锁短时释放后直接完成对账而不抛错。
    held2 = threading.Event()
    release2 = threading.Event()

    def hold_briefly() -> None:
        with store.guidance.ledger.turn_guard("target-busy-task"):
            held2.set()
            release2.wait(5)

    worker2 = threading.Thread(target=hold_briefly)
    worker2.start()
    assert held2.wait(5)
    releaser = threading.Timer(0.05, release2.set)
    releaser.start()
    try:
        state = delivery._reconcile_bound_input(paths, store, receipt)
        assert state == "active_pending"
    finally:
        releaser.cancel()
        release2.set()
        worker2.join(5)
    assert not worker2.is_alive()


def test_should_yield_after_deferred_busy_preserves_cursor_progress(tmp_path: Path) -> None:
    agent, paths, _request_id = _background_input(tmp_path, "busy-yield-task")
    write_json_file_atomic(
        delivery.gateway_input_receipt_path(paths, "gwreq-msg-z-healthy"),
        _valid_receipt_dict("gwreq-msg-z-healthy", state="consumed"),
    )
    store = agent.conversation_store
    held = threading.Event()
    release = threading.Event()

    def hold_turn_guard() -> None:
        with store.guidance.ledger.turn_guard("busy-yield-task"):
            held.set()
            release.wait(10)

    worker = threading.Thread(target=hold_turn_guard)
    worker.start()
    assert held.wait(5)
    try:
        first = delivery.reconcile_gateway_input_receipts(paths, agent, should_yield=lambda: True)
        assert first["deferred_busy"] == 1
        assert first["consumed"] == 0
        assert first["yielded"] is True
        cursor = json.loads((paths.root / "input_receipts/state/reconcile_cursor.json").read_text(encoding="utf-8"))
        assert cursor["last_name"] == "gwreq-msg-steer-d.json"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()

    second = delivery.reconcile_gateway_input_receipts(paths, agent, should_yield=lambda: False)
    assert second["consumed"] == 1
    assert second["yielded"] is False
    assert second["errors"] == 0
