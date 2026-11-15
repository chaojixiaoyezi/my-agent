"""W2 回归测试：NW-04 ReadIndex 后台切片续建、NW-09 归档单锁与有界尾部回读、NW-10 损坏向量文件指纹缓存。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bg_exec
from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
    execute_background_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    curator_input_revision,
    freeze_source,
)
from agent_py_agent.agent.memory_archive import storage as archive_storage
from agent_py_agent.agent.memory_archive.models import CompressionSnapshot, RawMemoryEvent
from agent_py_agent.agent.memory_archive.storage import (
    MemoryArchiveError,
    _read_tail_jsonl_text,
    _verify_record_exists,
    append_raw_event,
    append_snapshot,
    raw_event_path_for,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_models import (
    MemoryCuratorConfig,
    MemoryCuratorState,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.owner_wake_discovery import (
    _has_pending_memory_curator_work,
    _memory_read_index_pending,
    curator_work_is_due,
)
from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache
from agent_py_agent.agent.retrieval.vector_store import VectorIdentityError, VectorStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


class _IdleCuratorBackend:
    name = "idle-curator-w2"

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        raise AssertionError("NW-04 ReadIndex maintenance must not call model backend")


def _build_curator_env(tmp_path: Path, *, slice_entries: int = 2):
    owner = tmp_path / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    conversations = ConversationStore(tmp_path / "conversations")
    conversations.threads.get_or_create(
        {
            "canonical_user_id": "owner-w2",
            "channel": "internal",
            "channel_conversation_id": "chat-w2",
            "channel_user_id": "owner-w2",
            "learning_source": ls.user_dialogue_projection(),
            "now": 10.0,
        }
    )
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    long_term = JsonlMemory(
        owner / "memory" / "long_term" / "memory.jsonl",
        ops_path=owner / "memory" / "ops.jsonl",
        candidate_service=candidates,
    )
    lessons = LessonRepository(
        owner / "memory" / "lessons",
        owner / "memory" / "routing" / "INDEX.md",
    )
    hot = HotRuleRepository(owner / "memory-hot.md")
    live_ns = SimpleNamespace(
        memory_curator_enabled=True,
        memory_curator_interval_seconds=10_800,
        memory_curator_turn_threshold=10,
        memory_curator_auto_promotion_policy="conservative_v1",
        memory_lesson_archive_days=15,
        retention_scan_slice_entries=slice_entries,
        retention_scan_slice_bytes=1_048_576,
        retention_scan_slice_ms=100,
    )
    curator = MemoryCuratorService(
        config=MemoryCuratorConfig(
            enabled=True,
            interval_seconds=10_800,
            retention_scan_slice_entries=slice_entries,
        ),
        dependencies=MemoryCuratorDependencies(
            backend=_IdleCuratorBackend(),
            conversation_store=conversations,
            audit_dir=owner / "audit",
            state_store=MemoryCuratorStateStore(owner / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(owner / "memory" / "daily"),
            candidate_service=candidates,
            run_log=CuratorRunLog(owner / "memory" / "curator" / "runs.jsonl"),
            identity=MemoryCuratorIdentity(provider="", model="", owner_id="owner-w2", timezone_name="UTC"),
            formal_memory_source=FormalMemorySource(long_term=long_term, lessons=lessons, hot=hot),
            promotion_callback=lambda cid: None,
            config_provider=lambda: live_ns,
        ),
    )
    return owner, long_term, curator, live_ns


def test_nw04_pending_read_index_wakes_and_advances_across_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, long_term, curator, _live_ns = _build_curator_env(tmp_path, slice_entries=2)
    for idx in range(6):
        long_term.add("user", f"fact entry {idx} " + "a" * 32)
    long_term.migrate_tiers()

    read_index = MemoryReadIndex(long_term.path)
    assert read_index.coverage()["status"] == "missing"
    assert _memory_read_index_pending(owner) is True
    assert _has_pending_memory_curator_work(owner) is True
    rev_before = curator_input_revision(owner)

    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    res1 = curator.run_maintenance_if_due(now=now)
    assert res1["status"] == "partial"
    assert res1["reason"] == "maintenance"
    cov1 = read_index.coverage()
    assert cov1["status"] == "partial"
    assert cov1["indexed_records"] == 2
    assert cov1["total_records"] == 6
    assert _memory_read_index_pending(owner) is True
    assert curator_input_revision(owner) != rev_before

    settled_state = MemoryCuratorState(last_success_at=now.isoformat())
    assert curator_work_is_due(owner, settled_state) is True

    # 即使仍在 interval_seconds 内，pending ReadIndex 也立即放行后续切片直到 complete。
    res2 = curator.run_maintenance_if_due(now=now + timedelta(seconds=1))
    assert res2["status"] == "partial"
    assert read_index.coverage()["indexed_records"] == 4

    res3 = curator.run_maintenance_if_due(now=now + timedelta(seconds=2))
    assert res3["status"] == "succeeded"
    cov_done = read_index.coverage()
    assert cov_done["status"] == "complete"
    assert cov_done["indexed_records"] == 6
    assert _memory_read_index_pending(owner) is False
    assert _has_pending_memory_curator_work(owner) is False

    res_idle = curator.run_maintenance_if_due(now=now + timedelta(seconds=3))
    assert res_idle["status"] == "not_due"
    assert res_idle["reason"] == "interval"

    rev_complete = curator_input_revision(owner)
    # 模拟前台正式写入期间派生读索引临时遇忙失败：正式写提交新代次后由后台发现与维护继续追平。
    orig_rebuild_step = MemoryReadIndex.rebuild_step

    def _busy_rebuild(self: MemoryReadIndex, *, batch_size: int = 1000, admission=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MemoryReadIndex, "rebuild_step", _busy_rebuild)
    long_term.add("user", "subsequent formal fact " + "b" * 32)
    monkeypatch.setattr(MemoryReadIndex, "rebuild_step", orig_rebuild_step)

    assert read_index.coverage()["status"] == "missing"
    assert curator_input_revision(owner) != rev_complete
    assert _memory_read_index_pending(owner) is True
    assert _has_pending_memory_curator_work(owner) is True

    res_rebuilt = curator.run_maintenance_if_due(now=now + timedelta(seconds=4))
    assert res_rebuilt["status"] == "partial"
    assert read_index.coverage()["status"] == "partial"
    assert curator.run_maintenance_if_due(now=now + timedelta(seconds=5))["status"] == "partial"
    assert curator.run_maintenance_if_due(now=now + timedelta(seconds=6))["status"] == "partial"
    res_final = curator.run_maintenance_if_due(now=now + timedelta(seconds=7))
    assert res_final["status"] == "succeeded"
    assert read_index.coverage()["status"] == "complete"
    assert read_index.coverage()["indexed_records"] == 7


def test_nw04_execute_background_request_reports_pending_on_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = ["partial", "succeeded"]

    class _FakeCurator:
        def run_maintenance_if_due(self):
            return {"status": statuses.pop(0)}

    owner = OwnerIdentity.local_main()
    resolved = ensure_owner_home(tmp_path, owner)
    state_path = resolved.home_dir / "memory" / "curator" / "state.json"
    MemoryCuratorStateStore(state_path).request("turn_threshold")
    ref = freeze_source(tmp_path, owner, state_path, "curator")
    monkeypatch.setattr(bg_exec, "background_request_scope", lambda _ctx: nullcontext())

    fake_agent = SimpleNamespace(
        home_paths=SimpleNamespace(
            root=tmp_path,
            owner_provider=owner.provider,
            owner_kind=owner.owner_kind,
            owner_home_dir=resolved.home_dir,
        ),
        config=SimpleNamespace(memory_curator_model_profile=""),
        owner_home=resolved.home_dir,
        memory_curator=_FakeCurator(),
        run_background_turn=lambda *args, **kwargs: "",
    )
    request = {"metadata": {"background_source": ref}}
    first = execute_background_request({"agent": fake_agent, "request": request})
    second = execute_background_request({"agent": fake_agent, "request": request})
    assert first == {
        "source_kind": "curator",
        "source_id": ref["source_id"],
        "status": "partial",
        "run_id": "",
        "outcome": "pending",
    }
    assert second == {
        "source_kind": "curator",
        "source_id": ref["source_id"],
        "status": "succeeded",
        "run_id": "",
        "outcome": "handled",
    }


def test_nw04_read_index_corrupt_or_disabled_guards_and_error_isolation(tmp_path: Path) -> None:
    owner, long_term, curator, _live_ns = _build_curator_env(tmp_path, slice_entries=2)
    long_term.add("user", "fact entry " + "c" * 32)
    long_term.migrate_tiers()

    # 损坏索引返回 unavailable：发现层不得将其视作 missing/partial 以免自旋。
    idx = MemoryReadIndex(long_term.path)
    idx.path.write_bytes(b"corrupt-sqlite-bytes")
    assert idx.coverage()["status"] == "unavailable"
    assert _memory_read_index_pending(owner) is False

    # 删除坏索引并禁用记忆政策：发现层仍返回 False。
    idx.path.unlink()
    (owner / "memory_policy.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert _has_pending_memory_curator_work(owner) is False
    (owner / "memory_policy.json").unlink()

    # 维护路径遇到确定性异常时记录 step_errors，遇到不确定异常时原样上抛。
    def _fail_deterministic(*, batch_size: int):
        raise ValueError("MEMORY_READ_INDEX_UNREADABLE")

    long_term.rebuild_read_index = _fail_deterministic  # type: ignore[method-assign]
    maint_err = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert maint_err["status"] in {"partial", "failed"}
    assert maint_err["errors"]["read_index"] == "MEMORY_READ_INDEX_UNREADABLE"

    def _fail_uncertain(*, batch_size: int):
        raise BoundedCallExitUnconfirmedError("call-w2", "exit_unconfirmed")

    long_term.rebuild_read_index = _fail_uncertain  # type: ignore[method-assign]
    with pytest.raises(BoundedCallExitUnconfirmedError):
        curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 1, tzinfo=timezone.utc))


def test_nw09_concurrent_archive_writers_and_zero_fd_leak(tmp_path: Path) -> None:
    day_ts = "2026-11-15T08:00:00+00:00"
    worker_count = 8
    per_worker = 4
    barrier = threading.Barrier(worker_count)

    def _write_worker(worker_id: int) -> tuple[list[str], list[str]]:
        barrier.wait()
        snap_ids: list[str] = []
        event_ids: list[str] = []
        for step in range(per_worker):
            sid = f"snap-w{worker_id}-{step}"
            eid = f"evt-w{worker_id}-{step}"
            snap_path = append_snapshot(
                tmp_path,
                CompressionSnapshot(
                    snapshot_id=sid,
                    session_id="sess-nw09",
                    compression_id=f"cmp-{worker_id}-{step}",
                    turn_range=[step, step + 1],
                    created_at=day_ts,
                ),
            )
            evt_path = append_raw_event(
                tmp_path,
                RawMemoryEvent(
                    event_id=eid,
                    session_id="sess-nw09",
                    speaker="user",
                    action="message",
                    content_preview=f"worker {worker_id} step {step}",
                    created_at=day_ts,
                ),
            )
            assert snap_path.exists()
            assert evt_path.exists()
            snap_ids.append(sid)
            event_ids.append(eid)
        return snap_ids, event_ids

    fd_before = len(os.listdir("/dev/fd")) if Path("/dev/fd").exists() else None
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(_write_worker, range(worker_count)))
    fd_after = len(os.listdir("/dev/fd")) if Path("/dev/fd").exists() else None
    if fd_before is not None and fd_after is not None:
        assert fd_after - fd_before == 0

    expected_snaps = {sid for snap_ids, _ in results for sid in snap_ids}
    expected_events = {eid for _, event_ids in results for eid in event_ids}
    snap_rows = [
        json.loads(line)["snapshot_id"]
        for line in archive_storage.snapshot_path_for(tmp_path, day_ts).read_text(encoding="utf-8").splitlines()
    ]
    evt_rows = [
        json.loads(line)["event_id"]
        for line in raw_event_path_for(tmp_path, day_ts).read_text(encoding="utf-8").splitlines()
    ]
    assert set(snap_rows) == expected_snaps
    assert set(evt_rows) == expected_events


def test_nw09_start_zero_preserves_row0_and_never_calls_read_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_ts = "2026-11-15T09:00:00+00:00"
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Path.read_text")),
    )

    append_snapshot(
        tmp_path,
        CompressionSnapshot(
            snapshot_id="snap-row0",
            session_id="sess-row0",
            compression_id="cmp-row0",
            turn_range=[0, 1],
            created_at=day_ts,
        ),
    )
    evt_path = append_raw_event(
        tmp_path,
        RawMemoryEvent(
            event_id="evt-row0",
            session_id="sess-row0",
            speaker="user",
            action="message",
            content_preview="first row kept intact",
            created_at=day_ts,
        ),
    )
    append_raw_event(
        tmp_path,
        RawMemoryEvent(
            event_id="evt-row1",
            session_id="sess-row0",
            speaker="assistant",
            action="reply",
            content_preview="second row",
            created_at=day_ts,
        ),
    )

    tail_small = _read_tail_jsonl_text(evt_path, min_bytes=256 * 1024)
    assert tail_small is not None
    lines = tail_small.splitlines()
    assert json.loads(lines[0])["event_id"] == "evt-row0"
    assert json.loads(lines[1])["event_id"] == "evt-row1"

    # 追加超过 256 KiB 的旧行后（start > 0），新追加记录仍在有界尾窗内通过核验，且仍不触发 Path.read_text。
    large_day = "2026-11-15T10:00:00+00:00"
    large_path = raw_event_path_for(tmp_path, large_day)
    large_path.parent.mkdir(parents=True, exist_ok=True)
    padding_entry = json.dumps({"event_id": "pad", "content_preview": "x" * 1024}) + "\n"
    with large_path.open("w", encoding="utf-8") as handle:
        handle.write(padding_entry * 320)

    append_raw_event(
        tmp_path,
        RawMemoryEvent(
            event_id="evt-tail-large",
            session_id="sess-large",
            speaker="user",
            action="message",
            content_preview="multi-byte 中文 tail check",
            created_at=large_day,
        ),
    )


def test_nw09_tail_read_failure_raises_without_full_read_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "events.jsonl"
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Path.read_text")),
    )

    # 空文件或不存在文件均返回 None 并触发 MemoryArchiveError。
    target.write_bytes(b"")
    assert _read_tail_jsonl_text(target, min_bytes=4096) is None
    with pytest.raises(MemoryArchiveError, match="readback failed"):
        _verify_record_exists(target, key="event_id", value="missing", expected={"event_id": "missing"})

    # 非法 UTF-8 尾窗拒绝返回文本并直接报错。
    target.write_bytes(b"\xff\xfe\xfd\n")
    assert _read_tail_jsonl_text(target, min_bytes=4096) is None
    with pytest.raises(MemoryArchiveError, match="readback failed"):
        _verify_record_exists(target, key="event_id", value="bad-utf8", expected={"event_id": "bad-utf8"})

    # 尾窗打开遇 OSError 时直接失败，绝不回落全量读取。
    sample = {"event_id": "evt-io"}
    target.write_text(json.dumps(sample, sort_keys=True) + "\n", encoding="utf-8")
    real_open = Path.open

    def _failing_rb(self: Path, mode: str = "r", *args, **kwargs):
        if self == target and "b" in mode:
            raise OSError("simulated readback open error")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _failing_rb)
    assert _read_tail_jsonl_text(target, min_bytes=4096) is None
    with pytest.raises(MemoryArchiveError, match="readback failed"):
        _verify_record_exists(target, key="event_id", value="evt-io", expected=sample)

    # 与同锁交互验证：locked_json_path 内连续写+回读期间，真实锁串行保护。
    with locked_json_path(target):
        monkeypatch.setattr(Path, "open", real_open)
        _verify_record_exists(target, key="event_id", value="evt-io", expected=sample)


def test_nw10_text_vector_cache_caches_corrupt_stamp_and_retries_only_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "memory_text_vectors.json"
    cache_path.write_text("{corrupt-json", encoding="utf-8")

    cache = TextVectorCache(cache_path)
    load_calls = 0
    orig_load = cache._load

    def _counting_load():
        nonlocal load_calls
        load_calls += 1
        return orig_load()

    cache._load = _counting_load  # type: ignore[method-assign]

    # 初次读取学习当前文件指纹；相同指纹下后续多次读取必须 0 次重载。
    assert cache.get(["k1"]) == {}
    assert load_calls == 1
    assert cache.last_read_error is not None
    for _ in range(5):
        assert cache.get(["k1"]) == {}
        assert cache.keys() == []
    assert load_calls == 1

    # 部分损坏（包含好向量和坏条目的对象）：保留好键且相同指纹 0 次重载，但写路径仍严格拒写。
    partial_doc = json.dumps({"good": [1.0, 2.0], "bad": "not-a-list"})
    cache_path.write_text(partial_doc + "   ", encoding="utf-8")
    stat_before = cache_path.stat()
    os.utime(cache_path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 10_000))
    load_calls = 0
    assert cache.get(["good", "bad"]) == {"good": [1.0, 2.0]}
    assert cache.get(["good"]) == {"good": [1.0, 2.0]}
    assert load_calls == 1
    assert isinstance(cache.last_read_error, ValueError)
    cache.put({"new": [3.0, 4.0]})
    assert isinstance(cache.last_write_error, ValueError)

    # 恢复为合法文件（指纹改变）：下次 get 立即重载并清除错误。
    healthy_doc = json.dumps({"k1": [0.5, 0.5], "k2": [0.25, 0.75]})
    cache_path.write_text(healthy_doc, encoding="utf-8")
    stat_ok = cache_path.stat()
    os.utime(cache_path, ns=(stat_ok.st_atime_ns, stat_ok.st_mtime_ns + 20_000))
    assert cache.get(["k1"]) == {"k1": [0.5, 0.5]}
    assert cache.last_read_error is None

    # 瞬态 OSError 不缓存失败指纹，错误恢复后下一次 get 立刻重试读盘成功。
    cache._read_stamp = None
    fail_io = True
    orig_read_text = Path.read_text

    def _flaky_read_text(self: Path, *args, **kwargs):
        nonlocal fail_io
        if self == cache_path and fail_io:
            raise OSError("simulated transient cache read error")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)
    assert cache.get(["k1"]) == {}
    assert isinstance(cache.last_read_error, OSError)
    assert cache._read_stamp is None
    fail_io = False
    assert cache.get(["k1", "k2"]) == {"k1": [0.5, 0.5], "k2": [0.25, 0.75]}
    assert cache.last_read_error is None


def test_nw10_vector_store_corrupt_snapshot_caches_stamp_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "memory_vectors.json"
    store_path.write_text("{corrupt-snapshot", encoding="utf-8")
    identity = {"provider": "p", "model": "m", "dim": 2}

    store = VectorStore(store_path, identity)
    reload_calls = 0
    orig_reload = store._reload

    def _counting_reload() -> None:
        nonlocal reload_calls
        reload_calls += 1
        orig_reload()

    store._reload = _counting_reload  # type: ignore[method-assign]

    # 损坏文件已有确定 _stamp；所有只读方法在指纹未变时必须 0 次重载。
    for _ in range(3):
        assert store.summary()["vector_count"] is None
        assert store.identity_status() == (False, "VECTOR_STORE_UNREADABLE")
        assert len(store) == 0
        assert store.ids() == []
        assert store.stored_vectors(["entry-1"]) == {}
        with pytest.raises(VectorIdentityError, match="VECTOR_STORE_UNREADABLE"):
            store.search([1.0, 0.0], top_k=1)
    assert reload_calls == 0

    # 写入真实完整快照（改变文件内容、尺寸与 mtime），下一次只读操作立即恢复可用。
    writer = VectorStore(store_path, identity)
    writer.replace_all([("entry-1", [1.0, 0.0], "hello", {})])
    stat_ok = store_path.stat()
    os.utime(store_path, ns=(stat_ok.st_atime_ns, stat_ok.st_mtime_ns + 10_000))
    assert store.summary()["vector_count"] == 1
    assert reload_calls == 1
    assert len(store) == 1
    assert store.ids() == ["entry-1"]
    assert reload_calls == 1

    # 瞬态 OSError 将 _stamp 置空；再次调用时必须重试并在恢复后读出最新内容。
    reload_calls = 0
    fail_io = True
    orig_open = Path.open

    def _flaky_open(self: Path, mode: str = "r", *args, **kwargs):
        if self == store_path and "b" in mode and fail_io:
            raise OSError("simulated transient store open error")
        return orig_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _flaky_open)
    store._stamp = None
    assert store.summary()["vector_count"] is None
    assert store._stamp is None
    assert reload_calls == 1
    assert store.identity_status() == (False, "VECTOR_STORE_UNREADABLE")
    assert reload_calls == 2

    fail_io = False
    assert store.summary()["vector_count"] == 1
    assert store._stamp is not None
    assert reload_calls == 3
    assert store.search([1.0, 0.0], top_k=1)[0].id == "entry-1"
    assert reload_calls == 3
