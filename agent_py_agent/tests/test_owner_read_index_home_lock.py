# LLM: 所有测试均在 pytest 临时目录运行，使用 threading.Event 同步验证锁分片隔离、同 home 串行、重入及弱引用 GC 回收。
# 模块用途: 验证 OwnerReadIndex 按 owner home 分片的弱引用可重入推进锁行为与生命周期。
from __future__ import annotations

import gc
import json
import threading
import weakref

from agent_py_agent.agent.local_storage import owner_read_index as ori_mod
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import ToolCallRecord

_PARTS = ("data", "telemetry", "tool_calls.jsonl")
_PARTS_SECONDARY = ("data", "telemetry", "tool_calls_secondary.jsonl")


def _write_telemetry_source(home, parts=_PARTS, count=2, prefix="call", owner_id="owner"):
    path = home.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ToolCallRecord(
            call_id=f"{prefix}-{idx}",
            tool_name="read",
            run_id=f"run-{idx}",
            owner_id=owner_id,
            started_at=float(idx + 1),
            ended_at=float(idx + 1),
            duration_ms=float(idx),
        ).to_dict()
        for idx in range(count)
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    return path


def test_different_owner_homes_advance_and_forget_do_not_block_each_other(tmp_path, monkeypatch):
    home_a = tmp_path / "owner_a"
    home_b = tmp_path / "owner_b"
    _write_telemetry_source(home_a, count=2, prefix="a", owner_id="owner-a")
    _write_telemetry_source(home_b, count=3, prefix="b", owner_id="owner-b")

    idx_a = OwnerReadIndex(home_a, owner_id="owner-a")
    idx_b = OwnerReadIndex(home_b, owner_id="owner-b")
    # 先为 home_b 建立既有投影，便于随后验证 forget() 真实删库而不阻塞在 home_a 上。
    initial_b = idx_b.advance("telemetry", _PARTS)
    assert initial_b["status"] == "complete"
    with (home_b.joinpath(*_PARTS)).open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                ToolCallRecord(
                    call_id="b-extra",
                    tool_name="read",
                    run_id="run-extra",
                    owner_id="owner-b",
                    started_at=10.0,
                    ended_at=10.0,
                    duration_ms=1.0,
                ).to_dict()
            )
            + "\n"
        )

    a_entered = threading.Event()
    a_release = threading.Event()
    a_finished = threading.Event()
    original_advance = idx_a._advance
    a_result = {}
    errors = []

    def blocked_advance(*args, **kwargs):
        a_entered.set()
        assert a_release.wait(timeout=5.0), "timed out waiting to release home A"
        return original_advance(*args, **kwargs)

    monkeypatch.setattr(idx_a, "_advance", blocked_advance)

    def run_a():
        try:
            a_result["view"] = idx_a.advance("telemetry", _PARTS)
        except BaseException as exc:  # pragma: no cover - 仅失败时收集
            errors.append(exc)
        finally:
            a_finished.set()

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    try:
        assert a_entered.wait(timeout=5.0)
        view_b = idx_b.advance("telemetry", _PARTS)
        assert view_b["status"] == "complete"
        assert idx_b.query(view_b)["total_count"] == 4
        assert idx_b.forget("telemetry", _PARTS) is True
        assert idx_b.forget("telemetry", _PARTS) is False
        assert not a_finished.is_set()
    finally:
        a_release.set()
        thread_a.join(timeout=5.0)

    assert not thread_a.is_alive()
    assert not errors
    assert a_result["view"]["status"] == "complete"
    assert idx_a.query(a_result["view"])["total_count"] == 2


def test_same_owner_home_concurrent_advance_and_forget_serialize(tmp_path, monkeypatch):
    home_a = tmp_path / "owner_a"
    _write_telemetry_source(home_a, count=3, prefix="same", owner_id="owner-a")

    idx_1 = OwnerReadIndex(home_a, owner_id="owner-a")
    idx_2 = OwnerReadIndex(home_a, owner_id="owner-a")
    idx_3 = OwnerReadIndex(home_a, owner_id="owner-a")

    t1_entered = threading.Event()
    t1_release = threading.Event()
    t1_exited = threading.Event()
    t2_acquired_ref = threading.Event()
    t2_entered = threading.Event()
    t3_acquired_ref = threading.Event()
    t3_finished = threading.Event()

    orig_1 = idx_1._advance
    orig_2 = idx_2._advance
    orig_get_lock = ori_mod._home_advance_lock
    events: list[str] = []
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    t2_tid: list[int] = []
    t3_tid: list[int] = []

    def spy_home_lock(home):
        lock = orig_get_lock(home)
        tid = threading.get_ident()
        if t2_tid and tid == t2_tid[0]:
            t2_acquired_ref.set()
        elif t3_tid and tid == t3_tid[0]:
            t3_acquired_ref.set()
        return lock

    monkeypatch.setattr(ori_mod, "_home_advance_lock", spy_home_lock)

    def wrapped_1(*args, **kwargs):
        events.append("t1_enter")
        t1_entered.set()
        assert t1_release.wait(timeout=5.0), "timed out waiting to release t1"
        try:
            return orig_1(*args, **kwargs)
        finally:
            events.append("t1_exit")
            t1_exited.set()

    def wrapped_2(*args, **kwargs):
        assert t1_exited.is_set(), "t2 entered _advance while t1 still held per-home lock"
        events.append("t2_enter")
        t2_entered.set()
        return orig_2(*args, **kwargs)

    monkeypatch.setattr(idx_1, "_advance", wrapped_1)
    monkeypatch.setattr(idx_2, "_advance", wrapped_2)

    def run_1():
        try:
            results["v1"] = idx_1.advance("telemetry", _PARTS)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def run_2():
        try:
            t2_tid.append(threading.get_ident())
            results["v2"] = idx_2.advance("telemetry", _PARTS)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def run_3():
        try:
            t3_tid.append(threading.get_ident())
            results["f3"] = idx_3.forget("telemetry", _PARTS)
            results["f3_after_t1"] = t1_exited.is_set()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            t3_finished.set()

    th1 = threading.Thread(target=run_1)
    th2 = threading.Thread(target=run_2)
    th3 = threading.Thread(target=run_3)

    th1.start()
    try:
        assert t1_entered.wait(timeout=5.0)
        th2.start()
        th3.start()
        assert t2_acquired_ref.wait(timeout=5.0)
        assert t3_acquired_ref.wait(timeout=5.0)
        assert not t2_entered.wait(timeout=0.05)
        assert not t3_finished.is_set()
        assert str(idx_1._home) in ori_mod._PROCESS_ADVANCE_LOCKS
    finally:
        t1_release.set()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)
        th3.join(timeout=5.0)

    assert not th1.is_alive() and not th2.is_alive() and not th3.is_alive()
    assert not errors
    assert t2_entered.is_set()
    assert t3_finished.is_set()
    assert results["f3_after_t1"] is True
    assert results["v1"]["status"] == "complete"
    assert results["v2"]["status"] == "complete"
    assert results["f3"] is True
    assert events.index("t1_exit") < events.index("t2_enter")


def test_same_owner_home_reentrancy_and_memo_lock_order(tmp_path):
    home = tmp_path / "owner_reentrant"
    _write_telemetry_source(home, parts=_PARTS, count=2, prefix="primary", owner_id="owner-r")
    _write_telemetry_source(home, parts=_PARTS_SECONDARY, count=1, prefix="secondary", owner_id="owner-r")

    idx = OwnerReadIndex(home, owner_id="owner-r")
    nested_outcomes: list[tuple[str, bool]] = []

    def reentrant_validator(item):
        assert item["schema"] == "tool_call_telemetry.v1"
        if not nested_outcomes:
            nested = OwnerReadIndex(home, owner_id="owner-r")
            sub_view = nested.advance("telemetry", _PARTS_SECONDARY)
            forgotten = nested.forget("telemetry", _PARTS_SECONDARY)
            with ori_mod._PROCESS_MEMO_LOCK:
                assert isinstance(ori_mod._PROCESS_MEMOS, dict)
            nested_outcomes.append((sub_view["status"], forgotten))

    view = idx.advance("telemetry", _PARTS, validator=reentrant_validator)
    assert view["status"] == "complete"
    assert idx.query(view)["total_count"] == 2
    assert nested_outcomes == [("complete", True)]

    lock = ori_mod._home_advance_lock(idx._home)
    with lock:
        same_lock = ori_mod._home_advance_lock(idx._home)
        assert same_lock is lock
        with same_lock:
            with ori_mod._PROCESS_MEMO_LOCK:
                assert str(idx._home) in ori_mod._PROCESS_ADVANCE_LOCKS


def _exercise_busy_home_lock(busy_home, monkeypatch):
    _write_telemetry_source(busy_home, count=1, prefix="busy", owner_id="busy")
    idx_1 = OwnerReadIndex(busy_home, owner_id="busy")
    idx_2 = OwnerReadIndex(busy_home, owner_id="busy")

    t1_entered = threading.Event()
    t1_release = threading.Event()
    t2_entered = threading.Event()
    t2_release = threading.Event()
    t2_got_lock = threading.Event()

    orig_1 = idx_1._advance
    orig_2 = idx_2._advance
    orig_get_lock = ori_mod._home_advance_lock
    observed_ids: list[int] = []
    t2_tid: list[int] = []
    lock_ref = None

    def spy_home_lock(home):
        lock = orig_get_lock(home)
        if t2_tid and threading.get_ident() == t2_tid[0]:
            t2_got_lock.set()
        return lock

    monkeypatch.setattr(ori_mod, "_home_advance_lock", spy_home_lock)

    def wrap_1(*args, **kwargs):
        observed_ids.append(id(ori_mod._PROCESS_ADVANCE_LOCKS[str(idx_1._home)]))
        t1_entered.set()
        assert t1_release.wait(timeout=5.0)
        return orig_1(*args, **kwargs)

    def wrap_2(*args, **kwargs):
        observed_ids.append(id(ori_mod._PROCESS_ADVANCE_LOCKS[str(idx_2._home)]))
        t2_entered.set()
        assert t2_release.wait(timeout=5.0)
        return orig_2(*args, **kwargs)

    monkeypatch.setattr(idx_1, "_advance", wrap_1)
    monkeypatch.setattr(idx_2, "_advance", wrap_2)

    th1 = threading.Thread(target=lambda: idx_1.advance("telemetry", _PARTS))
    th2 = threading.Thread(
        target=lambda: (t2_tid.append(threading.get_ident()), idx_2.advance("telemetry", _PARTS))
    )

    th1.start()
    try:
        assert t1_entered.wait(timeout=5.0)
        lock_ref = weakref.ref(ori_mod._PROCESS_ADVANCE_LOCKS[str(idx_1._home)])
        th2.start()
        assert t2_got_lock.wait(timeout=5.0)
        t1_release.set()
        th1.join(timeout=5.0)
        assert t2_entered.wait(timeout=5.0)
        gc.collect()
        assert lock_ref() is not None
        assert len(ori_mod._PROCESS_ADVANCE_LOCKS) == 1
        assert observed_ids[0] == observed_ids[1]
    finally:
        t1_release.set()
        t2_release.set()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)
    return lock_ref


def test_weak_lock_registry_shrinks_to_zero_after_gc(tmp_path, monkeypatch):
    gc.collect()
    assert len(ori_mod._PROCESS_ADVANCE_LOCKS) == 0

    homes = [tmp_path / f"owner_{i}" for i in range(4)]
    for i, home in enumerate(homes):
        _write_telemetry_source(home, count=1, prefix=f"h{i}", owner_id=f"owner-{i}")
        idx = OwnerReadIndex(home, owner_id=f"owner-{i}")
        assert idx.advance("telemetry", _PARTS)["status"] == "complete"
        assert idx.forget("telemetry", _PARTS) is True

    gc.collect()
    assert len(ori_mod._PROCESS_ADVANCE_LOCKS) == 0

    # 持有者退出后、仍有等待/后续线程持本地强引用期间，弱表必须保活同一个 RLock 实例直至全部释放。
    lock_ref = _exercise_busy_home_lock(tmp_path / "owner_busy", monkeypatch)
    gc.collect()
    assert lock_ref() is None
    assert len(ori_mod._PROCESS_ADVANCE_LOCKS) == 0
