"""R3-P1-04 回归与边界测试：区分线程读取不确定与真实缺失，并移除 store_io/claim 读取 exists 预检。"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interruptible,
)
from agent_py_agent.agent.conversation import run_claim as run_claim_mod
from agent_py_agent.agent.conversation import store_io
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunClaimHeartbeat,
    conversation_claim_interrupt_name,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_claims import (
    ClaimStore,
    _read_claim_report,
)
from agent_py_agent.agent.conversation.store_threads import _drop_cached_thread


def _fd_count() -> int | None:
    fd_dir = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    return len(os.listdir(fd_dir)) if fd_dir.is_dir() else None


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _setup_native_claim(
    tmp_path: Path,
    *,
    lease_seconds: int = 120,
    interval_seconds: float = 40.0,
):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="r3-owner", owner_id="r3-owner")
    claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "r3-task",
            "reason": "r3-claim-read-boundary",
            "lease_seconds": lease_seconds,
        }
    )
    assert claim is not None
    store.threads.write(thread)
    # 写后索引发表会回填线程缓存；故障注入必须从冷读开始，才能真正覆盖磁盘 EIO。
    _drop_cached_thread(store.storage.thread_path(thread.thread_id))
    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": store,
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": lease_seconds,
            "interval_seconds": interval_seconds,
        }
    )
    return store, thread, claim, heartbeat


def test_single_thread_read_eio_preserves_live_claim_and_renews_next_tick(
    tmp_path: Path, monkeypatch
) -> None:
    fd_before = _fd_count()
    store, thread, claim, heartbeat = _setup_native_claim(
        tmp_path, lease_seconds=120, interval_seconds=0.04
    )
    target = store.storage.thread_path(thread.thread_id)
    claim_path = store.storage.background_claim_path(thread.thread_id)
    initial_claim = json.loads(claim_path.read_text(encoding="utf-8"))
    original_read_text = Path.read_text
    counts = {"errors": 0, "reads": 0}

    def read_once_eio(path: Path, *args, **kwargs):
        if path == target:
            counts["reads"] += 1
            if counts["errors"] == 0:
                counts["errors"] += 1
                raise OSError(errno.EIO, "simulated thread record read error")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_once_eio)
    try:
        with register_interruptible(conversation_claim_interrupt_name(claim["claim_id"])):
            heartbeat.run()
            assert counts["errors"] == 1
            assert not is_interrupted()
            assert heartbeat.is_alive()
            after_first_error = json.loads(claim_path.read_text(encoding="utf-8"))
            assert after_first_error == initial_claim

            assert _wait_until(
                lambda: json.loads(claim_path.read_text(encoding="utf-8"))["heartbeat_at"]
                > initial_claim["heartbeat_at"]
            )
            assert not is_interrupted()
            renewed = json.loads(claim_path.read_text(encoding="utf-8"))
            assert renewed["claim_id"] == claim["claim_id"]
            assert renewed["status"] == "running"
            assert renewed["expires_at"] > initial_claim["expires_at"]
            assert store.threads.load(thread.thread_id) is not None
    finally:
        heartbeat.stop()
        heartbeat.join(3.0)
    assert not heartbeat.is_alive()
    if fd_before is not None:
        assert (_fd_count() - fd_before) == 0


def test_persistent_thread_read_eio_interrupts_only_after_lease_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    fd_before = _fd_count()
    clock = [100.0]
    monkeypatch.setattr(run_claim_mod.time, "monotonic", lambda: clock[0])

    store, thread, claim, heartbeat = _setup_native_claim(
        tmp_path, lease_seconds=5, interval_seconds=40.0
    )
    target = store.storage.thread_path(thread.thread_id)
    claim_path = store.storage.background_claim_path(thread.thread_id)
    before_bytes = claim_path.read_bytes()
    original_read_text = Path.read_text
    attempts = {"count": 0}

    def always_eio(path: Path, *args, **kwargs):
        if path == target:
            attempts["count"] += 1
            raise OSError(errno.EIO, "persistent thread read error")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", always_eio)
    try:
        with register_interruptible(conversation_claim_interrupt_name(claim["claim_id"])):
            # 租约尚未到期：先进入退避重调度（由外部设好截止时间）但不中断。
            heartbeat._lease_deadline = clock[0] + 5.0
            heartbeat.run()
            assert attempts["count"] == 1
            assert not is_interrupted()
            assert heartbeat.is_alive()
            assert claim_path.read_bytes() == before_bytes

            # 越过既有租约截止时间后再次读盘失败：触发中止中断并停机。
            clock[0] += 10.0
            heartbeat.run()
            assert attempts["count"] == 2
            assert is_interrupted()
            assert not heartbeat.is_alive()
            assert claim_path.read_bytes() == before_bytes
    finally:
        heartbeat.stop()
    if fd_before is not None:
        assert (_fd_count() - fd_before) == 0


def test_genuine_missing_thread_interrupts_and_stops_heartbeat(tmp_path: Path) -> None:
    fd_before = _fd_count()
    store, thread, claim, heartbeat = _setup_native_claim(tmp_path)
    claim_path = store.storage.background_claim_path(thread.thread_id)
    before_bytes = claim_path.read_bytes()
    store.storage.thread_path(thread.thread_id).unlink()
    try:
        with register_interruptible(conversation_claim_interrupt_name(claim["claim_id"])):
            heartbeat.run()
            assert is_interrupted()
        assert not heartbeat.is_alive()
        assert claim_path.read_bytes() == before_bytes
    finally:
        heartbeat.stop()
    if fd_before is not None:
        assert (_fd_count() - fd_before) == 0


def test_renew_load_error_report_and_direct_oserror_semantics(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="r3-owner", owner_id="r3-owner")
    claim_path = store.storage.background_claim_path(thread.thread_id)

    report_error = {
        "category": "data_corruption",
        "error_type": "OSError",
        "context": "conversation.thread.read",
        "path": str(store.storage.thread_path(thread.thread_id)),
        "thread_id": thread.thread_id,
    }
    mode = {"value": "report"}
    inner_cause = ValueError("underlying hardware detail")
    direct_exc = PermissionError(errno.EACCES, "direct callback oserror")
    direct_exc.__cause__ = inner_cause

    def controlled_load_report(tid: str):
        assert tid == thread.thread_id
        if mode["value"] == "report":
            return None, report_error
        if mode["value"] == "direct_raise":
            raise direct_exc
        if mode["value"] == "missing":
            return None, None
        return thread, None

    claims = ClaimStore(
        store.storage,
        require_thread=store.threads.require,
        load_thread=store.threads.load,
        load_thread_report=controlled_load_report,
    )
    acquired = claims.acquire(
        {"thread_id": thread.thread_id, "task_id": "t1", "reason": "r3", "lease_seconds": 60}
    )
    assert acquired is not None
    before_bytes = claim_path.read_bytes()

    # 1. 结构化错误报告 -> 抛普通 OSError、消息包含报告信息、不伪造 __cause__、不改写 claim。
    mode["value"] = "report"
    with pytest.raises(OSError) as report_raised:
        claims.renew({"thread_id": thread.thread_id, "claim_id": acquired["claim_id"], "now": 50.0})
    assert type(report_raised.value) is OSError
    assert report_raised.value.__cause__ is None
    assert "conversation.thread.read" in str(report_raised.value)
    assert thread.thread_id in str(report_raised.value)
    assert claim_path.read_bytes() == before_bytes

    # 2. 回调直接抛出 OSError -> 原样透传对象与原 __cause__，不改写 claim。
    mode["value"] = "direct_raise"
    with pytest.raises(PermissionError) as direct_raised:
        claims.renew({"thread_id": thread.thread_id, "claim_id": acquired["claim_id"], "now": 50.0})
    assert direct_raised.value is direct_exc
    assert direct_raised.value.__cause__ is inner_cause
    assert claim_path.read_bytes() == before_bytes

    # 3. 确认缺失 (None, None) -> 保持返回 None，不改写 claim。
    mode["value"] = "missing"
    assert (
        claims.renew({"thread_id": thread.thread_id, "claim_id": acquired["claim_id"], "now": 50.0})
        is None
    )
    assert claim_path.read_bytes() == before_bytes


def test_mismatched_claim_id_and_non_running_claim_controls(tmp_path: Path) -> None:
    store, thread, claim, heartbeat = _setup_native_claim(tmp_path)
    claim_path = store.storage.background_claim_path(thread.thread_id)
    before_running = claim_path.read_bytes()

    # 1. claim_id 不匹配时 renew 返回 None 且不改盘。
    assert (
        store.claims.renew(
            {"thread_id": thread.thread_id, "claim_id": "bgclaim-mismatch", "now": 99.0}
        )
        is None
    )
    assert claim_path.read_bytes() == before_running

    # 2. 心跳发现 claim_id 不匹配时（例如已被新租约替代）立即中断并停止。
    superseded = ConversationRunClaimHeartbeat(
        {
            "store": store,
            "thread_id": thread.thread_id,
            "claim_id": "bgclaim-mismatch",
            "lease_seconds": 30,
            "interval_seconds": 40,
        }
    )
    try:
        with register_interruptible(conversation_claim_interrupt_name("bgclaim-mismatch")):
            superseded.run()
            assert is_interrupted()
        assert not superseded.is_alive()
    finally:
        superseded.stop()

    # 3. 非 running 终态（finished/cancelled）租约调用 renew 返回 None 且保留原终态。
    finished = store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "finished",
            "runtime_facts": {"proof": "kept"},
            "now": 100.0,
        }
    )
    assert finished is not None and finished["status"] == "finished"
    after_finish = claim_path.read_bytes()
    with register_interruptible(conversation_claim_interrupt_name(claim["claim_id"])):
        heartbeat.run()
        assert is_interrupted()
    assert not heartbeat.is_alive()
    assert claim_path.read_bytes() == after_finish


def test_store_io_and_claim_reader_py314_stat_semantics_and_classification(
    tmp_path: Path, monkeypatch
) -> None:
    # 1. 模拟 Python 3.14 Path.exists 吞掉 OSError 返回 False 的语义时，已存在文件仍可读。
    state_path = tmp_path / "record.json"
    state_path.write_text('{"generation": 7}', encoding="utf-8")
    original_exists = Path.exists

    monkeypatch.setattr(
        Path,
        "exists",
        lambda path, *args, **kwargs: False if path == state_path else original_exists(path, *args, **kwargs),
    )
    assert store_io.read_json_object_report(state_path, context="audit.json.read") == (
        {"generation": 7},
        None,
    )

    # 2. 原生 ConversationStore + 缓存失效后单次 Path.stat EIO + Python 3.14 exists 语义。
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="r3-owner", owner_id="r3-owner")
    store.threads.write(thread)
    thread_file = store.storage.thread_path(thread.thread_id)
    _drop_cached_thread(thread_file)
    original_stat = Path.stat
    counts = {"stats": 0, "errors": 0}

    def flaky_stat(path: Path, *args, **kwargs):
        if path == thread_file:
            counts["stats"] += 1
            if counts["stats"] == 2:
                counts["errors"] += 1
                raise OSError(errno.EIO, "simulated thread record stat error")
        return original_stat(path, *args, **kwargs)

    def exists_py314_error_semantics(path: Path, *args, **kwargs):
        if path != thread_file:
            return original_exists(path, *args, **kwargs)
        try:
            path.stat(*args, **kwargs)
        except OSError:
            return False
        return True

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", flaky_stat)
        patched.setattr(Path, "exists", exists_py314_error_semantics)
        loaded, load_error = store.threads.load_report(thread.thread_id)
    assert counts["errors"] == 1
    assert load_error is None
    assert loaded is not None and loaded.thread_id == thread.thread_id

    # 3. 真正缺失文件与父目录保持返回 ({}, None) 且不建目录。
    absent = tmp_path / "absent-parent" / "missing.json"
    assert store_io.read_json_object_report(absent, context="audit.missing") == ({}, None)
    assert not absent.parent.exists()

    # 4. 读错误（EIO / EACCES）、坏 JSON、非法 UTF-8、非对象根均保留 context/path 与错误分类。
    for error_number in (errno.EIO, errno.EACCES):
        original_read = Path.read_text
        with monkeypatch.context() as patched:
            def raise_read(path: Path, *args, err=error_number, **kwargs):
                if path == state_path:
                    raise OSError(err, "simulated record read error")
                return original_read(path, *args, **kwargs)

            patched.setattr(Path, "read_text", raise_read)
            doc, err_report = store_io.read_json_object_report(state_path, context="audit.io.err")
        assert doc == {}
        assert err_report is not None
        assert err_report["context"] == "audit.io.err"
        assert err_report["path"] == str(state_path)

    for raw_bytes, expected_type in (
        (b"{broken", "JSONDecodeError"),
        (b"\xff\xfe", "UnicodeDecodeError"),
        (b"[]", "DataCorruptionError"),
        (b"123", "DataCorruptionError"),
    ):
        state_path.write_bytes(raw_bytes)
        doc, err_report = store_io.read_json_object_report(state_path, context="audit.corrupt")
        assert doc == {}
        assert err_report is not None
        assert err_report["context"] == "audit.corrupt"
        assert err_report["path"] == str(state_path)
        assert err_report["error_type"] == expected_type

    # 5. _read_claim_report 与 ClaimStore.finish 在同类 exists 假阴性与缺失边界保持一致。
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": "t-claim", "reason": "r3"})
    assert claim is not None
    claim_path = store.storage.background_claim_path(thread.thread_id)
    with monkeypatch.context() as patched:
        patched.setattr(
            Path,
            "exists",
            lambda path, *args, **kwargs: False if path == claim_path else original_exists(path, *args, **kwargs),
        )
        claim_doc, claim_err = _read_claim_report(claim_path)
        assert claim_err is None and claim_doc["claim_id"] == claim["claim_id"]
        finished = store.claims.finish(
            {"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "finished"}
        )
        assert finished is not None and finished["status"] == "finished"

    missing_claim = tmp_path / "absent-claims" / "none.json"
    assert _read_claim_report(missing_claim) == ({}, None)
    assert not missing_claim.parent.exists()
    assert store.claims.finish({"thread_id": "no-such-thread", "claim_id": "c-missing"}) is None
    assert not store.storage.background_claim_path("no-such-thread").exists()


def test_direct_claim_store_fallback_interleaving_and_stop_rearm(
    tmp_path: Path, monkeypatch
) -> None:
    fd_before = _fd_count()
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="r3-owner", owner_id="r3-owner")

    # 1. 不传 load_thread_report 的旧构造方式保持向后兼容。
    fallback_claims = ClaimStore(
        store.storage,
        require_thread=lambda _tid: SimpleNamespace(thread_id=thread.thread_id),
        load_thread=lambda _tid: None,
    )
    assert (
        fallback_claims.renew({"thread_id": thread.thread_id, "claim_id": "any", "now": 10.0})
        is None
    )

    # 2. 原生续租遇单次 read EIO 后，与并发 finish/stop 交错保持终态不回退、停机后不重挂。
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "task_id": "t-interleave", "reason": "r3", "lease_seconds": 30}
    )
    assert claim is not None
    store.threads.write(thread)
    thread_file = store.storage.thread_path(thread.thread_id)
    _drop_cached_thread(thread_file)
    original_read = Path.read_text
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow_first_eio(path: Path, *args, **kwargs):
        if path == thread_file:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(3.0)
                raise OSError(errno.EIO, "simulated concurrent thread read error")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_first_eio)
    hb = ConversationRunClaimHeartbeat(
        {
            "store": store,
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": 30,
            "interval_seconds": 0.03,
        }
    )
    hb.start()
    assert entered.wait(3.0)
    finished = store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "cancelled",
            "runtime_facts": {"exit_proof": "recorded"},
            "now": 50.0,
        }
    )
    assert finished is not None and finished["status"] == "cancelled"
    stopper = threading.Thread(target=hb.stop, daemon=True)
    stopper.start()
    time.sleep(0.03)
    assert stopper.is_alive()
    release.set()
    stopper.join(3.0)
    hb.join(3.0)
    assert not stopper.is_alive()
    assert not hb.is_alive()

    # stop 后再次 start 不得重新激活定时器或写盘。
    hb.start()
    assert not hb.is_alive()
    assert hb._pending is None
    latest, err = store.claims.load_report(thread.thread_id)
    assert err is None
    assert latest["status"] == "cancelled"
    assert latest["last_runtime_facts"] == {"exit_proof": "recorded"}
    if fd_before is not None:
        assert (_fd_count() - fd_before) == 0
