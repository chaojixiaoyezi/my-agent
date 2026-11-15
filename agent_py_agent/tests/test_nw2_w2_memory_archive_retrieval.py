"""W2 回归测试：NW-04 ReadIndex 后台切片续建、NW-09 归档单锁与有界尾部回读、NW-10 损坏向量文件指纹缓存。"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_wake_discovery
from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bg_exec
from agent_py_agent.agent.gateway_parts import multiprocess_mode
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    CoordinatorAdmissionScope,
    PersistentAdmission,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
    execute_background_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    curator_input_revision,
    enqueue_source,
    freeze_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    bind_admission_takeover,
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import (
    GatewayClaim,
    claim_request,
    ensure_gateway_folders,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _iter_pending_requests,
    _process_claimed_gateway_request_path,
    _request_deferred_until_later,
    dispatch_pending_requests,
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
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.owner_wake_discovery import (
    _has_pending_memory_curator_work,
    _memory_read_index_pending,
    curator_work_is_due,
)
from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache
from agent_py_agent.agent.retrieval.vector_store import VectorIdentityError, VectorStore
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
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


def test_nw2r1_deterministic_read_index_failure_backs_off_due_and_soft_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, long_term, curator, _live_ns = _build_curator_env(tmp_path, slice_entries=2)
    for idx in range(3):
        long_term.add("user", f"fact entry nw2r1 {idx} " + "a" * 32)
    long_term.migrate_tiers()
    read_index = MemoryReadIndex(long_term.path)
    assert _memory_read_index_pending(owner) is True

    def _fail_owner_mismatch(*, batch_size: int):
        raise ValueError("MEMORY_INDEX_OWNER_MISMATCH")

    now = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)
    base_mono = time.monotonic()
    long_term.rebuild_read_index = _fail_owner_mismatch  # type: ignore[method-assign]
    err_res = curator.run_maintenance_if_due(now=now)
    assert err_res["errors"]["read_index"] == "MEMORY_INDEX_OWNER_MISMATCH"

    assert _memory_read_index_pending(owner) is False
    assert owner_wake_discovery.memory_read_index_blocked(owner) is True
    assert _has_pending_memory_curator_work(owner) is False
    settled_state = MemoryCuratorState(
        last_success_at=now.isoformat(),
        last_daily_finalize_date=datetime.now().astimezone().date().isoformat(),
    )
    assert curator_work_is_due(owner, settled_state) is False

    monkeypatch.setattr(owner_wake_discovery.time, "monotonic", lambda: base_mono + 10_801.0)
    assert _memory_read_index_pending(owner) is True

    del long_term.rebuild_read_index
    resumed = curator.run_maintenance_if_due(now=now + timedelta(seconds=10_801))
    assert resumed["status"] == "partial"
    assert read_index.coverage()["indexed_records"] > 0
    assert owner_wake_discovery.memory_read_index_blocked(owner) is False

    def _fail_busy(*, batch_size: int):
        raise sqlite3.OperationalError("database is locked")

    long_term.rebuild_read_index = _fail_busy  # type: ignore[method-assign]
    busy_res = curator.run_maintenance_if_due(now=now + timedelta(seconds=10_802))
    assert busy_res["errors"]["read_index"] == "CURATOR_MAINTENANCE_OPERATIONALERROR"
    assert owner_wake_discovery.memory_read_index_blocked(owner) is False
    assert _memory_read_index_pending(owner) is True


def _attach_owner_quota(agent: SimpleAgent, quota: OwnerQuotaEnforcer) -> None:
    agent.owner_quota = quota
    agent.memory.quota_enforcer = quota
    agent.persona_repository.quota_enforcer = quota
    curator = getattr(agent, "memory_curator", None)
    if curator is not None:
        curator.candidate_service.quota_enforcer = quota
        curator.daily_store.quota_enforcer = quota
        curator.formal_memory_source.long_term.quota_enforcer = quota


@contextmanager
def _open_coordinator_gate(agent: SimpleAgent, c_run_id: str, *, worker_count: int = 2):
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(worker_count, c_run_id))
    with coordinator_guard(paths.root, gate.count):
        controller = prepare_coordinator_takeover(paths, gate.count, gate.c_run_id, config=agent.config)
        bind_admission_takeover(gate, controller)
        control = GatewayControlPlane(
            agent.config,
            agent.home_paths.root,
            paths,
            gate.count,
            gate.c_run_id,
            agent_root=agent.root,
        )
        GatewayDrainRuntime(control).retain_admission(gate)
        yield paths, gate


def _admit_and_claim_request(
    gate: PersistentAdmission,
    paths,
    req_path: Path,
    owner: OwnerIdentity,
) -> GatewayClaim:
    req_doc = read_json_file(req_path)
    decision = gate.admit(
        req_path,
        (owner, session_scope_for_request(req_doc)),
        AdmissionLimits(8, 8),
    )
    assert decision["status"] == "admitted"
    claim = claim_request(
        paths,
        req_path,
        coordinator_snapshot=freeze_coordinator_claim(gate, decision),
    )
    assert claim is not None
    return claim


def test_nw2r1_execute_background_request_stops_requeue_when_read_index_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    maint_responses = [
        {"status": "partial", "errors": {"lessons": "X"}},
        {"status": "partial"},
        {"status": "partial", "errors": {"read_index": "OWNER_QUOTA_EXCEEDED"}},
        {"status": "failed", "errors": {"read_index": "MEMORY_INDEX_OWNER_MISMATCH"}},
        {"status": "failed", "errors": {"read_index": "OWNER_QUOTA_EXCEEDED", "lessons": "X"}},
    ]

    class _FakeCurator:
        def run_maintenance_if_due(self):
            return maint_responses.pop(0)

        def run_if_due(self):
            return SimpleNamespace(status="not_due", run_id="")

    owner = OwnerIdentity.local_main()
    resolved = ensure_owner_home(tmp_path / "unit", owner)
    state_path = resolved.home_dir / "memory" / "curator" / "state.json"
    MemoryCuratorStateStore(state_path).request("turn_threshold")
    ref = freeze_source(tmp_path / "unit", owner, state_path, "curator")
    monkeypatch.setattr(bg_exec, "background_request_scope", lambda _ctx: nullcontext())

    fake_agent = SimpleNamespace(
        home_paths=SimpleNamespace(
            root=tmp_path / "unit",
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
    assert first["outcome"] == "handled"

    second = execute_background_request({"agent": fake_agent, "request": request})
    assert second["outcome"] == "pending"

    owner_wake_discovery.note_memory_read_index_blocked(resolved.home_dir, 60)
    third = execute_background_request({"agent": fake_agent, "request": request})
    assert third["outcome"] == "pending"

    fourth = execute_background_request({"agent": fake_agent, "request": request})
    assert fourth["outcome"] == "pending"

    fifth = execute_background_request({"agent": fake_agent, "request": request})
    assert fifth["outcome"] == "handled"

    monkeypatch.setattr(bg_exec, "_auxiliary_model_unavailable", lambda *_args, **_kwargs: False)
    sixth = execute_background_request({"agent": fake_agent, "request": request})
    assert sixth["outcome"] == "pending"

    owner_wake_discovery.clear_memory_read_index_blocked(resolved.home_dir)
    seventh = execute_background_request({"agent": fake_agent, "request": request})
    assert seventh["outcome"] == "pending"

    monkeypatch.setattr(multiprocess_mode, "_WORKER_PARTITION", None)
    mp_agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(tmp_path / "mp-home"),
            model_backend="echo",
            max_tokens=65536,
            gateway_worker_process_count=2,
            memory_curator_enabled=True,
            memory_curator_interval_seconds=75,
            enable_self_learning=False,
        ),
        tmp_path / "mp-ws",
    )
    mp_state = mp_agent.home_paths.owner_home_dir / "memory" / "curator" / "state.json"
    MemoryCuratorStateStore(mp_state).request("turn_threshold")
    mp_ref = freeze_source(mp_agent.home_paths.root, owner, mp_state, "curator")
    pending_result = {
        "background_result": {
            "source_kind": "curator",
            "source_id": mp_ref["source_id"],
            "outcome": "pending",
        }
    }

    with _open_coordinator_gate(mp_agent, "c-run-nw2r1-requeue") as (paths, gate):
        req_path, created = enqueue_source(paths, mp_agent.home_paths.root, mp_ref)
        assert created is True

        owner_wake_discovery.note_memory_read_index_blocked(mp_agent.home_paths.owner_home_dir, 60)
        mp_agent.memory_curator = SimpleNamespace(
            _effective_config=lambda: SimpleNamespace(interval_seconds=180)
        )
        claim_eff = _admit_and_claim_request(gate, paths, req_path, owner)
        with coordinator_execution_scope(gate, claim_eff, mp_agent):
            t_eff = time.time()
            assert bg_exec.requeue_pending_background_request(pending_result, mp_agent) is True
        doc_eff = read_json_file(paths.inbox / req_path.name)
        assert doc_eff["not_before_at"] >= t_eff + 180.0

        mp_agent.memory_curator = SimpleNamespace(config=SimpleNamespace(interval_seconds=90))
        claim_cfg = _admit_and_claim_request(gate, paths, req_path, owner)
        with coordinator_execution_scope(gate, claim_cfg, mp_agent):
            t_cfg = time.time()
            assert bg_exec.requeue_pending_background_request(pending_result, mp_agent) is True
        doc_cfg = read_json_file(paths.inbox / req_path.name)
        assert doc_cfg["not_before_at"] >= t_cfg + 90.0

        mp_agent.memory_curator = SimpleNamespace(config=None)
        claim_agent_cfg = _admit_and_claim_request(gate, paths, req_path, owner)
        with coordinator_execution_scope(gate, claim_agent_cfg, mp_agent):
            t_agent = time.time()
            assert bg_exec.requeue_pending_background_request(pending_result, mp_agent) is True
        doc_agent = read_json_file(paths.inbox / req_path.name)
        assert doc_agent["not_before_at"] >= t_agent + 75.0

        # Clearing blocked state (as in transient sqlite3.OperationalError) keeps fast 1.0s retry.
        owner_wake_discovery.clear_memory_read_index_blocked(mp_agent.home_paths.owner_home_dir)
        claim_fast = _admit_and_claim_request(gate, paths, req_path, owner)
        with coordinator_execution_scope(gate, claim_fast, mp_agent):
            t_fast = time.time()
            assert bg_exec.requeue_pending_background_request(pending_result, mp_agent) is True
        doc_fast = read_json_file(paths.inbox / req_path.name)
        assert t_fast <= doc_fast["not_before_at"] < t_fast + 10.0


def test_nw2r2_read_index_eio_requeue_recovers_after_backoff_without_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(multiprocess_mode, "_WORKER_PARTITION", None)
    cfg = AgentConfig(
        my_agent_home=str(tmp_path / "home"),
        model_backend="echo",
        max_tokens=65536,
        gateway_worker_process_count=2,
        memory_curator_enabled=True,
        memory_curator_interval_seconds=120,
        retention_scan_slice_entries=1000,
        enable_self_learning=False,
    )
    agent = SimpleAgent(cfg, tmp_path / "ws")
    owner = OwnerIdentity.local_main()
    owner_home = agent.home_paths.owner_home_dir
    quota = OwnerQuotaEnforcer(owner_home, max_bytes=100 * 1024 * 1024, policy_available=True)
    _attach_owner_quota(agent, quota)
    assert agent.memory_curator._effective_config().interval_seconds == 120
    assert agent.memory.path == agent.memory_curator.formal_memory_source.long_term.path

    lines = [
        MemoryRecord(
            role="user",
            content=(
                "zxqvtarget0852 unique archival completion entry"
                if idx == 852
                else f"distractor archive record {idx:04d} stable context entry"
            ),
            kind="fact",
            entry_id=f"memory-{idx:04d}",
            created_at=float(1000 + idx),
            updated_at=float(1000 + idx),
        ).to_json()
        for idx in range(1501)
    ]
    agent.memory_curator.formal_memory_source.long_term.path.parent.mkdir(parents=True, exist_ok=True)
    agent.memory_curator.formal_memory_source.long_term.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    agent.memory_curator.formal_memory_source.long_term.migrate_tiers()

    read_index = MemoryReadIndex(agent.memory_curator.formal_memory_source.long_term.path)
    assert not read_index.path.exists()
    state_path = owner_home / "memory" / "curator" / "state.json"
    MemoryCuratorStateStore(state_path).request("turn_threshold")
    assert _memory_read_index_pending(owner_home) is True

    wall_base = time.time()
    mono_base = time.monotonic()
    clock_offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: wall_base + clock_offset[0])
    monkeypatch.setattr(time, "monotonic", lambda: mono_base + clock_offset[0])

    real_connect = MemoryReadIndex._connect
    trigger_open_eio = [False]

    def _guarded_connect(self: MemoryReadIndex, *, write: bool = False):
        if write and trigger_open_eio[0] and self.path == read_index.path:
            trigger_open_eio[0] = False
            raise OSError(errno.EIO, "simulated index open io error")
        return real_connect(self, write=write)

    monkeypatch.setattr(MemoryReadIndex, "_connect", _guarded_connect)

    # Phase 1: Slice 1 completes 1000/1501 records cleanly and leaves 501 records pending.
    with _open_coordinator_gate(agent, "c-run-nw2r2") as (paths, gate):
        ref_1 = freeze_source(agent.home_paths.root, owner, state_path, "curator")
        req_1_path, created_1 = enqueue_source(paths, agent.home_paths.root, ref_1)
        assert created_1 is True
        claim_1 = _admit_and_claim_request(gate, paths, req_1_path, owner)
        with coordinator_execution_scope(gate, claim_1, agent):
            assert (
                _process_claimed_gateway_request_path(
                    agent, paths, claim_1.path, claim_1.lease_owner, claim=claim_1
                )
                is True
            )
        gate.release_settled()

        assert read_index.coverage()["indexed_records"] == 1000
        assert read_index.coverage()["total_records"] == 1501
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is False
        assert _memory_read_index_pending(owner_home) is True

        pre_records, pre_errors = agent.memory_curator.formal_memory_source.long_term.search_report(
            "zxqvtarget0852", top_k=5
        )
        assert "memory-0852" not in [record.entry_id for record in pre_records]
        assert any(err.get("error_type") == "MEMORY_READ_INDEX_PENDING" for err in pre_errors)

        # Phase 2: Freeze slice 2 request, simulate single write-open EIO before SQLite rewrite.
        ref_2 = freeze_source(agent.home_paths.root, owner, state_path, "curator")
        req_2_path, created_2 = enqueue_source(paths, agent.home_paths.root, ref_2)
        assert created_2 is True
        req_2_id = req_2_path.stem
        rev_before_eio = curator_input_revision(agent.home_paths.root)

        trigger_open_eio[0] = True
        claim_2 = _admit_and_claim_request(gate, paths, req_2_path, owner)
        before_eio_ts = time.time()
        with coordinator_execution_scope(gate, claim_2, agent):
            assert (
                _process_claimed_gateway_request_path(
                    agent, paths, claim_2.path, claim_2.lease_owner, claim=claim_2
                )
                is True
            )

        assert not (paths.terminal / f"{req_2_id}.json").exists()
        assert not claim_2.path.exists()
        requeued_path = paths.inbox / f"{req_2_id}.json"
        assert requeued_path.exists()
        requeued_doc = read_json_file(requeued_path)
        assert requeued_doc["id"] == req_2_id
        assert requeued_doc["status"] == "pending"
        assert requeued_doc["not_before_at"] >= before_eio_ts + 120.0
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is True
        assert read_index.coverage()["indexed_records"] == 1000
        assert curator_input_revision(agent.home_paths.root) == rev_before_eio

        # Unrelated ready request stays claimable while deferred read_index request waits.
        thread = agent.conversation_store.threads.get_or_create(
            {
                "canonical_user_id": "local-agent",
                "channel": "chat",
                "channel_conversation_id": "unrelated-ready-wake",
                "channel_user_id": "local-agent",
            }
        )
        wake = agent.conversation_store.wakes.raise_signal(
            {"thread_id": thread.thread_id, "reason": "unrelated-ready"}
        )
        other_ref = freeze_source(
            agent.home_paths.root,
            owner,
            agent.conversation_store.storage.wake_signal_path(wake),
            "wake",
        )
        other_path, other_created = enqueue_source(paths, agent.home_paths.root, other_ref)
        assert other_created is True
        submitted_ready: list[GatewayClaim] = []
        dispatched = dispatch_pending_requests(
            paths,
            AdmissionLimits(8, 8),
            lambda c, _u, _r: submitted_ready.append(c),
            admission_gate=gate,
        )
        assert dispatched == 1
        assert [c.request_id for c in submitted_ready] == [other_path.stem]
        assert requeued_path.exists()
        with coordinator_execution_scope(gate, submitted_ready[0], agent):
            assert (
                _process_claimed_gateway_request_path(
                    agent,
                    paths,
                    submitted_ready[0].path,
                    submitted_ready[0].lease_owner,
                    claim=submitted_ready[0],
                )
                is True
            )
        gate.release_settled()

        # Phase 3a: Before not_before_at, persisted not_before_at on disk blocks claiming even if in-memory backoff clears.
        clock_offset[0] = 60.0
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is True
        owner_wake_discovery.clear_memory_read_index_blocked(owner_home)
        pending_entries = _iter_pending_requests(paths)
        assert len(pending_entries) == 1
        assert _request_deferred_until_later(pending_entries[0]) is True
        mid_claims: list[GatewayClaim] = []
        assert (
            dispatch_pending_requests(
                paths,
                AdmissionLimits(8, 8),
                lambda c, _u, _r: mid_claims.append(c),
                admission_gate=gate,
            )
            == 0
        )
        assert mid_claims == []
        # Re-arm remaining monotonic backoff (60s left) so expiry at +130s is driven by real monotonic clock.
        owner_wake_discovery.note_memory_read_index_blocked(owner_home, 60.0)
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is True

        # Phase 3b: Advance past both not_before_at and monotonic deadline with ZERO formal source change.
        trigger_open_eio[0] = False
        clock_offset[0] = 130.0
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is False
        assert curator_work_is_due(owner_home, agent.memory_curator.state_store.load()) is True
        assert curator_input_revision(agent.home_paths.root) == rev_before_eio

        ref_retry = freeze_source(agent.home_paths.root, owner, state_path, "curator")
        assert ref_retry == ref_2
        path_retry, created_retry = enqueue_source(paths, agent.home_paths.root, ref_retry)
        assert path_retry.stem == req_2_id and created_retry is False

        final_claims: list[GatewayClaim] = []
        assert (
            dispatch_pending_requests(
                paths,
                AdmissionLimits(8, 8),
                lambda c, _u, _r: final_claims.append(c),
                admission_gate=gate,
            )
            == 1
        )
        assert [c.request_id for c in final_claims] == [req_2_id]
        claim_final = final_claims[0]
        with coordinator_execution_scope(gate, claim_final, agent):
            assert (
                _process_claimed_gateway_request_path(
                    agent,
                    paths,
                    claim_final.path,
                    claim_final.lease_owner,
                    claim=claim_final,
                )
                is True
            )
        gate.release_settled()

        assert (paths.terminal / f"{req_2_id}.json").exists()
        assert not (paths.inbox / f"{req_2_id}.json").exists()

    assert read_index.coverage()["indexed_records"] == 1501
    assert read_index.coverage()["total_records"] == 1501
    records, errors = agent.memory_curator.formal_memory_source.long_term.search_report(
        "zxqvtarget0852", top_k=5
    )
    assert records and records[0].entry_id == "memory-0852"
    assert errors == []


def test_nw2r2_read_index_sqlite_locked_keeps_fast_retry_and_unconfirmed_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(multiprocess_mode, "_WORKER_PARTITION", None)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(tmp_path / "home"),
            model_backend="echo",
            max_tokens=65536,
            gateway_worker_process_count=2,
            memory_curator_enabled=True,
            memory_curator_interval_seconds=120,
            enable_self_learning=False,
        ),
        tmp_path / "ws",
    )
    owner = OwnerIdentity.local_main()
    owner_home = agent.home_paths.owner_home_dir
    _attach_owner_quota(
        agent,
        OwnerQuotaEnforcer(owner_home, max_bytes=100 * 1024 * 1024, policy_available=True),
    )

    lines = [
        MemoryRecord(
            role="user",
            content=f"transient lock entry {idx:03d}",
            kind="fact",
            entry_id=f"lock-{idx:03d}",
            created_at=float(2000 + idx),
            updated_at=float(2000 + idx),
        ).to_json()
        for idx in range(5)
    ]
    agent.memory_curator.formal_memory_source.long_term.path.parent.mkdir(parents=True, exist_ok=True)
    agent.memory_curator.formal_memory_source.long_term.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    agent.memory_curator.formal_memory_source.long_term.migrate_tiers()
    state_path = owner_home / "memory" / "curator" / "state.json"
    MemoryCuratorStateStore(state_path).request("turn_threshold")

    def _raise_locked(*, batch_size: int):
        raise sqlite3.OperationalError("database is locked")

    agent.memory_curator.formal_memory_source.long_term.rebuild_read_index = _raise_locked  # type: ignore[method-assign]

    with _open_coordinator_gate(agent, "c-run-sqlite-locked") as (paths, gate):
        ref = freeze_source(agent.home_paths.root, owner, state_path, "curator")
        req_path, created = enqueue_source(paths, agent.home_paths.root, ref)
        assert created is True
        claim = _admit_and_claim_request(gate, paths, req_path, owner)
        before_locked = time.time()
        with coordinator_execution_scope(gate, claim, agent):
            assert (
                _process_claimed_gateway_request_path(
                    agent, paths, claim.path, claim.lease_owner, claim=claim
                )
                is True
            )
        assert owner_wake_discovery.memory_read_index_blocked(owner_home) is False
        assert not (paths.terminal / req_path.name).exists()
        requeued_locked = read_json_file(paths.inbox / req_path.name)
        assert before_locked <= requeued_locked["not_before_at"] < before_locked + 10.0

        def _raise_unconfirmed(*, batch_size: int):
            raise BoundedCallExitUnconfirmedError("call-read-index", "timeout")

        agent.memory_curator.formal_memory_source.long_term.rebuild_read_index = _raise_unconfirmed  # type: ignore[method-assign]
        claim_unconfirmed = _admit_and_claim_request(gate, paths, req_path, owner)
        with (
            coordinator_execution_scope(gate, claim_unconfirmed, agent),
            pytest.raises(BoundedCallExitUnconfirmedError),
        ):
            execute_background_request(
                {
                    "agent": agent,
                    "request_path": claim_unconfirmed.path,
                    "request_id": claim_unconfirmed.request_id,
                    "request": read_json_file(claim_unconfirmed.path),
                }
            )

        with (
            coordinator_execution_scope(gate, claim_unconfirmed, agent),
            pytest.raises(BoundedCallExitUnconfirmedError),
        ):
            _process_claimed_gateway_request_path(
                agent,
                paths,
                claim_unconfirmed.path,
                claim_unconfirmed.lease_owner,
                claim=claim_unconfirmed,
            )
        assert claim_unconfirmed.path.exists()
        assert not (paths.inbox / req_path.name).exists()
        assert not (paths.terminal / req_path.name).exists()
