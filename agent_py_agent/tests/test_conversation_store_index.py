"""G4 回归：真实扫描只枚举一次，派生缓存不改变统计或可变数据隔离。"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import (
    ConversationStore,
    store_index,
    store_observations,
    store_progress,
)
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.models import ObservationEvent, ProgressPolicy, WakeSignal


# LLM: 所有业务夹具写入 pytest 临时目录，扫描与读取仍走真实产品入口。
# 函数用途: 创建一条带可选嵌套元数据的唤醒、观察或策略及其查询入口。
def _ledger_case(tmp_path, kind, metadata=None):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "user", "channel": "internal",
        "channel_conversation_id": "conv", "channel_user_id": "user", "now": 1,
    })
    request = {"thread_id": thread.thread_id, "metadata": metadata or {}, "now": 2}
    if kind == "wake":
        item = store.wakes.raise_signal(request)
        return store, store.wakes.pending_report, store.storage.wake_signal_path(item), "wake:urgent"
    if kind == "observation":
        store.observations.append({**request, "requires_main_agent": True})
        store.storage.observation_handled_path.write_text("{}", encoding="utf-8")
        return store, store.observations.unhandled_requiring_main, store.storage.observation_path(thread.thread_id), "observations"
    item = store.progress.create({**request, "interval_seconds": 10})
    return store, store.progress.list_report, store.storage.policy_path(item.policy_id), "progress_policies"


# LLM: 包装真实 scandir，不替代枚举结果；计数严格按目标目录归属。
# 函数用途: 记录产品发起的目录枚举调用。
def _scan_spy(monkeypatch):
    calls = []
    original = store_index.os.scandir

    # LLM: 每次仍执行真实文件系统枚举并传播原异常。
    # 函数用途: 统计 scandir 调用并透传返回值。
    def scan(directory):
        calls.append(Path(directory))
        return original(directory)

    monkeypatch.setattr(store_index.os, "scandir", scan)
    return calls


# LLM: 三个真实调用方都必须复用 ordered_paths 的同轮 listing，冷热轮次均受约束。
# 函数用途: 验证每轮每个账本目录只 scandir 一次且命中统计不变。
@pytest.mark.parametrize("kind", ["wake", "observation", "policy"])
def test_each_ledger_scan_enumerates_each_directory_once(tmp_path, monkeypatch, kind):
    store, query, path, name = _ledger_case(tmp_path, kind)
    calls = _scan_spy(monkeypatch)
    first = query()
    assert calls.count(path.parent) == 1
    calls.clear()
    assert query() == first
    assert calls.count(path.parent) == 1
    stats = store.storage.indexes.get(name).stats()
    assert (stats["misses"], stats["hits"], stats["stale"], stats["prunes"]) == (1, 1, 0, 0)


# LLM: 一次性 listing 不能变成长缓存；独立 usable 即使目录 mtime 被恢复也必须重新枚举。
# 函数用途: 验证消费后直接 usable 看见新增成员并重新判断容量。
def test_listing_is_consumed_once_and_direct_usable_scans_fresh(tmp_path, monkeypatch):
    index = store_index.ScanIndex("test", max_entries=1)
    indexes = store_index.ScanIndexes()
    (tmp_path / "one.json").write_text("{}")
    calls = _scan_spy(monkeypatch)
    assert len(index.ordered_paths(tmp_path, "*.json")) == 1
    assert indexes.usable(index, key="test", directory=tmp_path, pattern="*.json")
    assert len(calls) == 1
    before = tmp_path.stat()
    (tmp_path / "two.json").write_text("{}")
    os.utime(tmp_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not indexes.usable(index, key="test", directory=tmp_path, pattern="*.json")
    assert len(calls) == 2
    assert index.warnings[-1]["reason"] == "dir_too_large"


# LLM: 共享索引的另一个线程未做 ordered_paths，不能借用本线程的待消费 listing。
# 函数用途: 用真实线程交错验证独立 usable 必须自行枚举目录。
def test_direct_usable_in_another_thread_does_not_borrow_listing(tmp_path, monkeypatch):
    index = store_index.ScanIndex("test")
    indexes = store_index.ScanIndexes()
    index.ordered_paths(tmp_path, "*.json")
    calls = _scan_spy(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(indexes.usable, index, key="test", directory=tmp_path, pattern="*.json").result(timeout=10)
    assert calls == [tmp_path]


# LLM: clear 是当前版本的失效接口；目录、模式或成员版本不匹配都必须丢弃一次性 listing。
# 函数用途: 验证失效和跨目录/模式/版本场景仍执行新的枚举。
@pytest.mark.parametrize("change", ["clear", "directory", "pattern", "membership", "schema"])
def test_listing_reuse_requires_same_validated_scan(tmp_path, monkeypatch, change):
    index = store_index.ScanIndex("test")
    indexes = store_index.ScanIndexes()
    index.ordered_paths(tmp_path, "*.json")
    directory, pattern = tmp_path, "*.json"
    if change == "clear":
        index.clear()
    elif change == "directory":
        directory = tmp_path / "other"
        directory.mkdir()
    elif change == "pattern":
        pattern = "*.jsonl"
    elif change == "membership":
        (tmp_path / "new.json").write_text("{}")
    else:
        index.schema = "invalid"
        assert not indexes.usable(index, key="test", directory=directory, pattern=pattern)
        index.schema = store_index._SCAN_INDEX_SCHEMA
    calls = _scan_spy(monkeypatch)
    assert indexes.usable(index, key="test", directory=directory, pattern=pattern)
    assert len(calls) == 1


# LLM: 目录不可读不能复用上次成功 listing；保留条目和原 dir_unreadable/prunes 语义。
# 函数用途: 验证枚举失败清除一次性结果并留可观测降级。
def test_unreadable_listing_never_reuses_previous_success(tmp_path, monkeypatch):
    index = store_index.ScanIndex("test")
    index.ordered_paths(tmp_path, "*.json")
    monkeypatch.setattr(store_index, "_scan_dir_match_names", lambda *_args: None)
    assert index.ordered_paths(tmp_path, "*.json") == []
    assert not store_index.ScanIndexes().usable(index, key="test", directory=tmp_path, pattern="*.json")
    assert index.stats()["dir_unreadable"] == 1
    assert index.stats()["prunes"] == 0


# LLM: 当前版本 memo 已按 payload 身份复用，不应为实现优化而重新增加指纹系统调用。
# 函数用途: 验证三类热查询每个目标文件仅执行原 read 的一次指纹校验。
@pytest.mark.parametrize("kind", ["wake", "observation", "policy"])
def test_warm_ledger_reads_do_not_repeat_file_fingerprint(tmp_path, monkeypatch, kind):
    _store, query, path, _name = _ledger_case(tmp_path, kind)
    expected = query()
    calls = []
    original = store_index._file_fingerprint

    # LLM: 保持真实物理文件指纹语义，仅记录目标路径。
    # 函数用途: 统计一次热读的文件校验次数。
    def fingerprint(target):
        calls.append(target)
        return original(target)

    monkeypatch.setattr(store_index, "_file_fingerprint", fingerprint)
    assert query() == expected
    assert calls.count(path) == 1


# LLM: 空字典无需 deepcopy，但返回字典仍必须各自独立；非空嵌套字段保持原深复制合同。
# 函数用途: 验证三类 memo 冷热读不复制空 metadata，也不泄露可变嵌套对象。
@pytest.mark.parametrize("kind", ["wake", "observation", "policy"])
@pytest.mark.parametrize("metadata", [{}, {"nested": [1]}])
def test_metadata_cloning_skips_empty_and_keeps_private_copies(tmp_path, monkeypatch, kind, metadata):
    _store, query, path, _name = _ledger_case(tmp_path, kind, metadata)
    empty_copies = []
    original = store_progress.copy.deepcopy

    # LLM: 复制非空值仍走真实 deepcopy，不改变被测对象或其返回值。
    # 函数用途: 记录不必要的空字典深复制。
    def deepcopy(value, *args, **kwargs):
        if value == {}:
            empty_copies.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(store_progress.copy, "deepcopy", deepcopy)
    first = query()
    item = first[0] if kind == "observation" else first[0][0]
    if metadata:
        item.metadata["nested"].append("caller")
    else:
        item.metadata["caller"] = True
    again = query()
    repeated = again[0] if kind == "observation" else again[0][0]
    assert repeated.metadata == metadata
    assert repeated.metadata is not item.metadata
    assert not empty_copies
    assert json.loads(path.read_text())["metadata"] == metadata


# LLM: 无待处理事件的分片应直接返回共享空 tuple，外层 events 列表仍沿原公共接口返回。
# 函数用途: 验证空观察快照冷拍与热拍均不构造克隆列表。
def test_empty_observation_snapshot_skips_cloning_on_cold_and_warm_reads(tmp_path, monkeypatch):
    store, query, path, _name = _ledger_case(tmp_path, "observation")
    row = json.loads(path.read_text())
    row["requires_main_agent"] = False
    path.write_text(json.dumps(row) + "\n")
    calls = []
    original = store_observations._clone_main_events

    # LLM: 仍透传真实克隆函数，断言只限制空快照的冗余分配。
    # 函数用途: 记录观察克隆入口调用。
    def clone(events):
        calls.append(events)
        return original(events)

    monkeypatch.setattr(store_observations, "_clone_main_events", clone)
    assert query() == query() == []
    assert calls == []
    assert store.storage.indexes.get("observations").stats()["hits"] == 1


# LLM: 替身仅提供发现阶段依赖；准入过滤仍调用原 append 逻辑，发现过程不能消费任何账本。
# 函数用途: 构造可观测来源调用顺序的后台就绪扫描器。
def _ready_case(monkeypatch, wakes=(), observations=(), policies=()):
    calls = []

    # LLM: 记录发现来源，不修改任何模型或调度状态。
    # 函数用途: 返回某个只读来源的夹具内容。
    def source(name, values):
        calls.append(name)
        return values

    store = SimpleNamespace(
        wakes=SimpleNamespace(pending=lambda **_kw: source("wakes", wakes)),
        observations=SimpleNamespace(unhandled_requiring_main=lambda **_kw: source("observations", observations)),
        progress=SimpleNamespace(list_report=lambda **_kw: source("policies", (policies, ()))),
    )
    scheduler = SimpleNamespace(
        store=store, runtime=SimpleNamespace(agent=None),
        _supply_backoff=SimpleNamespace(should_attempt=lambda thread, _now: thread != "cooldown"),
        _recovery_guard=SimpleNamespace(block_for_task=lambda task: object() if task == "blocked" else None),
    )
    monkeypatch.setattr(runtime_module, "_wake_waits_this_tick", lambda _s, signal, _now: signal.thread_id == "waiting")
    monkeypatch.setattr(runtime_module, "_observation_waits_for_linked_wake", lambda _s, event: event.thread_id == "linked")
    monkeypatch.setattr(runtime_module, "_runnable_due_policies", lambda _s, items, **_kw: (source("runnable", items), ()))
    return scheduler, calls


# LLM: 满额判断必须基于实际准入数，不是来源行数；首条等待/冷却/恢复阻塞不能耗尽配额。
# 函数用途: 验证唤醒填满后不再扫描观察、策略或计算 runnable。
@pytest.mark.parametrize("limit", [1, 2])
def test_ready_wakes_stop_lower_priority_scans_after_admission(monkeypatch, limit):
    wakes = [WakeSignal(f"wake-{i}", thread, root_task_id=task) for i, (thread, task) in enumerate([
        ("", ""), ("waiting", ""), ("cooldown", ""), ("blocked-thread", "blocked"),
        ("one", ""), ("one", ""), ("two", ""), ("three", ""),
    ])]
    scheduler, calls = _ready_case(monkeypatch, wakes=wakes)
    assert runtime_module._ready_background_thread_ids(scheduler, current=10, limit=limit) == ("one", "two")[:limit]
    assert calls == ["wakes"]


# LLM: 观察补满前仍须过滤链接等待和重复线程，补满后不得枚举策略。
# 函数用途: 验证观察填满配额后跳过 progress 列表与 runnable 计算。
def test_ready_observations_stop_progress_scan_after_admission(monkeypatch):
    events = [ObservationEvent(f"obs-{i}", thread, "event", "summary") for i, thread in enumerate(["one", "linked", "cooldown", "two", "three"])]
    scheduler, calls = _ready_case(monkeypatch, wakes=[WakeSignal("wake", "one")], observations=events)
    assert runtime_module._ready_background_thread_ids(scheduler, current=10, limit=2) == ("one", "two")
    assert calls == ["wakes", "observations"]


# LLM: 非正 limit 保持不限额合同，三种来源依原优先级合并且仅调度到期策略。
# 函数用途: 验证无限额或尚未满额时仍扫描全部来源并维持去重和 due 过滤。
@pytest.mark.parametrize("limit", [0, -1, 4])
def test_ready_scan_keeps_all_sources_when_not_full(monkeypatch, limit):
    policies = [ProgressPolicy("due", "three", "", 10, 5), ProgressPolicy("future", "future", "", 10, 15)]
    scheduler, calls = _ready_case(monkeypatch, wakes=[WakeSignal("wake", "one")], observations=[ObservationEvent("obs", "two", "event", "summary")], policies=policies)
    assert runtime_module._ready_background_thread_ids(scheduler, current=10, limit=limit) == ("one", "two", "three")
    assert calls == ["wakes", "observations", "policies", "runnable"]
