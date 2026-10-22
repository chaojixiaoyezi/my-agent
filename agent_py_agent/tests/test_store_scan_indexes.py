from __future__ import annotations

"""读取侧增量索引的安全验收:有索引 / 无索引 / 索引损坏三态结果必须逐字一致。

夹具规模刻意小于性能实测脚本(/tmp 里那份),这里只证明**语义不变**与**有界性**:
唤醒队列、观察分片、进度策略三类台账各自构造"正常 + 脏 + 边界"记录,然后把
"冷 store(全量扫描) / 硬关索引 / 热索引 / 四种索引损坏"六种读取方式的结果逐字对比。
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import store_index as store_module
from agent_py_agent.agent.conversation.store_index import (
    _SCAN_INDEX_MAX_RECORDS_PER_FILE_COUNT,
    _SCAN_INDEX_SCHEMA,
)


# LLM: 快照函数把三类查询的返回压成可比对的纯数据(信号 + load_error 集合),三态一致性
# 断言只依赖这些快照;改动查询语义时这里必须同步,否则验收会失去意义。
# 函数用途: 取一次唤醒查询的可比对快照(信号字典 + 结构化错误列表)。
def _wake_snapshot(
    store: ConversationStore, *, limit: int = 0, include_normal: bool = True
) -> tuple[list[dict], list[dict]]:
    signals, errors = store.wakes.pending_report(
        limit=limit, include_normal=include_normal
    )
    return [item.to_dict() for item in signals], list(errors)


# 函数用途: 取一次观察查询的可比对快照。
def _observation_snapshot(store: ConversationStore, *, limit: int = 0) -> list[dict]:
    return [item.to_dict() for item in store.observations.unhandled_requiring_main(limit=limit)]


# 函数用途: 取一次策略查询的可比对快照(策略字典 + 结构化错误列表)。
def _policy_snapshot(
    store: ConversationStore, *, enabled_only: bool = False
) -> tuple[list[dict], list[dict]]:
    policies, errors = store.progress.list_report(enabled_only=enabled_only)
    return [item.to_dict() for item in policies], list(errors)


# 函数用途: 取一次到期策略查询的可比对快照。
def _due_snapshot(store: ConversationStore, *, now: float = 10_000.0) -> list[dict]:
    return [item.to_dict() for item in store.progress.due_report(now=now)[0]]


def _thread(store: ConversationStore, suffix: str = "1"):
    return store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": f"thread-{suffix}",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )


# LLM: 台账夹具直接按磁盘格式落文件(而不是全走 store API),这样能造出 store 正常写入
# 不会产生的脏状态:非 pending 残留、坏 JSON、空文件、同名目录、主键与文件名不一致。
# 函数用途: 写一条唤醒信号文件,可指定状态、时间、以及故意错位的负载主键。
def _seed_wake(
    root: Path,
    kind: str,
    signal_id: str,
    *,
    status: str = "pending",
    created_at: float = 1.0,
    payload_id: str | None = None,
    thread_id: str = "thread-1",
) -> Path:
    payload = {
        "wake_signal_id": signal_id if payload_id is None else payload_id,
        "thread_id": thread_id,
        "observation_id": "",
        "urgency": kind,
        "severity": "warning",
        "reason": "agent_event",
        "source_agent_id": "child-1",
        "parent_agent_id": "main",
        "root_task_id": "task-1",
        "summary": f"signal {signal_id}",
        "evidence_refs": [],
        "created_at": created_at,
        "handled_at": created_at if status != "pending" else 0.0,
        "status": status,
        "dedupe_key": "",
        "metadata": {"seed": signal_id},
    }
    path = root / "wake_queue" / kind / f"{signal_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


# 函数用途: 写一个观察分片(JSONL),行内容由调用方给出。
def _seed_observations(root: Path, thread_id: str, rows: list[dict]) -> Path:
    path = root / "observations" / f"{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


# 函数用途: 造一行观察事件负载。
def _observation_row(
    observation_id: str,
    *,
    thread_id: str = "thread-1",
    observed_at: float = 1.0,
    handled_at: float = 0.0,
    requires_main_agent: bool = True,
    requires_llm_report: bool = False,
) -> dict:
    return {
        "observation_id": observation_id,
        "thread_id": thread_id,
        "event_type": "child_agent_event",
        "summary": f"observation {observation_id}",
        "urgency": "normal",
        "severity": "info",
        "source_agent_id": "child-1",
        "parent_agent_id": "main",
        "root_task_id": "task-1",
        "evidence_refs": [],
        "requires_main_agent": requires_main_agent,
        "requires_llm_report": requires_llm_report,
        "observed_at": observed_at,
        "handled_at": handled_at,
        "wake_signal_id": "",
        "metadata": {"seed": observation_id},
    }


# 函数用途: 写一条进度策略文件,可指定 enabled/到期时间/故意错位的负载主键。
def _seed_policy(
    root: Path,
    policy_id: str,
    *,
    enabled: bool = True,
    next_due_at: float = 2.0,
    payload_id: str | None = None,
    thread_id: str = "thread-1",
) -> Path:
    payload = {
        "policy_id": policy_id if payload_id is None else payload_id,
        "thread_id": thread_id,
        "task_id": "task-1",
        "interval_seconds": 300,
        "next_due_at": next_due_at,
        "route_channel": "internal",
        "route_target": "",
        "enabled": enabled,
        "last_report_at": 0.0,
        "report_on_blocked": True,
        "report_on_completion": True,
        "metadata": {"seed": policy_id},
    }
    path = root / "progress_policies" / f"{policy_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


# 函数用途: 造齐三类台账的混合夹具(正常 + 脏 + 边界),返回会话根目录。
def _seed_all_ledgers(root: Path) -> Path:
    thread = _thread(ConversationStore(root), "seed")
    for index in range(12):
        _seed_wake(
            root,
            "urgent" if index % 2 else "normal",
            f"wake-{index:03d}",
            created_at=100.0 + index,
            thread_id=thread.thread_id,
        )
    _seed_wake(root, "urgent", "wake-handled-left", status="handled", created_at=1.0)
    _seed_wake(root, "normal", "wake-odd-id", payload_id="not-the-file-name", created_at=2.0)
    (root / "wake_queue" / "urgent" / "wake-broken.json").write_text("{not json", encoding="utf-8")
    (root / "wake_queue" / "normal" / "wake-empty.json").write_text("", encoding="utf-8")
    (root / "wake_queue" / "normal" / "wake-list.json").write_text("[]", encoding="utf-8")
    (root / "wake_queue" / "normal" / "wake-subdir.json").mkdir(parents=True, exist_ok=True)

    _seed_observations(
        root,
        "thread-a",
        [
            _observation_row(f"obs-a-{index:03d}", thread_id="thread-a", observed_at=200.0 + index)
            for index in range(6)
        ],
    )
    _seed_observations(
        root,
        "thread-b",
        [
            _observation_row("obs-b-handled", thread_id="thread-b", observed_at=150.0, handled_at=151.0),
            _observation_row("obs-b-plain", thread_id="thread-b", observed_at=160.0, requires_main_agent=False),
            _observation_row("obs-b-llm", thread_id="thread-b", observed_at=170.0, requires_main_agent=False, requires_llm_report=True),
        ],
    )
    _seed_observations(root, "thread-empty", [])
    (root / "observations" / "thread-dirty.jsonl").write_text(
        '{"observation_id": "obs-dirty-1", "thread_id": "thread-dirty", "event_type": "x", '
        '"summary": "ok", "observed_at": 180.0, "requires_main_agent": true}\n'
        "{broken line\n",
        encoding="utf-8",
    )
    handled_path = root / "observation_handled.json"
    handled_path.write_text(
        json.dumps({"obs-a-000": 190.0}, sort_keys=True), encoding="utf-8"
    )

    for index in range(6):
        _seed_policy(
            root,
            f"policy-{index:03d}",
            enabled=index % 3 != 0,
            next_due_at=300.0 + index,
        )
    _seed_policy(root, "policy-disabled-old", enabled=False, next_due_at=1.0)
    _seed_policy(root, "policy-odd-id", payload_id="not-the-file-name", next_due_at=2.0)
    (root / "progress_policies" / "policy-broken.json").write_text("}{", encoding="utf-8")
    return root


# LLM: "无索引"必须是真的全量扫描:max_entries=0 会让 _scan_index_usable 直接判不可用
# 并记结构化告警,查询退回逐文件权威读取;这与"新建 store 冷启动"是两条独立证据。
# 函数用途: 把指定台账的索引容量压到 0,模拟索引完全不可用。
def _disable_index(store: ConversationStore, *names: str) -> None:
    for name in names:
        index = store.storage.indexes.get(name)  # noqa: SLF001 - 验收需要直接操作索引状态
        index.max_entries = 0
        index.clear()


# LLM: 有界性验收需要把上限压到人能数清的规模(默认 8192 条无法在测试里造出来),因此统一
# 通过这个 helper 打开热 store 并设定 wake 两个队列的条目上限;条目上界是唯一被改的东西,
# 查询语义与生产默认完全一致。
# 函数用途: 打开一个把唤醒队列索引条目上限压到 max_entries 的热 store。
def _bounded_wake_store(root: Path, max_entries: int) -> ConversationStore:
    store = ConversationStore(root)
    for kind in ("urgent", "normal"):
        store.storage.indexes.get(f"wake:{kind}").max_entries = max_entries  # noqa: SLF001
    return store


# 函数用途: 读一个索引的 entries/records/evictions/prunes 诊断快照(有界性断言的统一入口)。
def _index_stats(store: ConversationStore, name: str) -> dict:
    return store.storage.indexes.get(name).stats()  # noqa: SLF001 - 验收需要直接读索引统计


# LLM: 四种损坏形态分别对应"索引文件版本不符/索引被清空/条目指纹过期/条目主键被污染",
# 覆盖索引可能失效的全部现实入口;每种都必须回退权威读取并给出完全一致的结果。
# 函数用途: 按模式损坏指定索引,返回被损坏的索引对象。
def _corrupt_index(store: ConversationStore, name: str, mode: str):
    index = store.storage.indexes.get(name)  # noqa: SLF001 - 验收需要直接操作索引状态
    if mode == "schema":
        index.schema = "tampered.schema.v0"
        return index
    if mode == "cleared":
        index.clear()
        return index
    if mode == "fingerprint":
        for key, entry in list(index._entries.items()):  # noqa: SLF001
            index._entries[key] = replace(entry, fingerprint=(1, 2, 3, 4))  # noqa: SLF001
        return index
    if mode == "identity":
        for key, entry in list(index._entries.items()):  # noqa: SLF001
            if not entry.ok or not isinstance(entry.payload, dict):
                continue
            poisoned = dict(entry.payload)
            for field in ("wake_signal_id", "policy_id"):
                if field in poisoned:
                    poisoned[field] = f"tampered-{key}"
            index._entries[key] = replace(entry, payload=poisoned)  # noqa: SLF001
        return index
    raise AssertionError(f"unknown corruption mode: {mode}")


# 函数用途: 列出目录树里所有台账文件的名字与大小,用于证明查询不写不删历史数据。
def _tree_signature(root: Path) -> list[tuple[str, int]]:
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    )


CORRUPTION_MODES = ("schema", "cleared", "fingerprint", "identity")


def test_wake_report_matches_across_index_states(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    cold_before = _tree_signature(root)

    baseline_store = ConversationStore(root)
    baseline_all = _wake_snapshot(baseline_store)
    baseline_capped = _wake_snapshot(baseline_store, limit=3)
    baseline_urgent = _wake_snapshot(baseline_store, include_normal=False)

    # 冷 store: 每次调用都是全量扫描(索引不可复用)。
    for _ in range(2):
        assert _wake_snapshot(ConversationStore(root)) == baseline_all

    # 硬关索引: 容量 0 → 明确回退全量扫描 + 结构化告警。
    disabled = ConversationStore(root)
    _disable_index(disabled, "wake:urgent", "wake:normal")
    assert _wake_snapshot(disabled) == baseline_all
    assert _wake_snapshot(disabled, limit=3) == baseline_capped
    assert _wake_snapshot(disabled, include_normal=False) == baseline_urgent
    assert any(
        item["reason"] == "dir_too_large"
        for item in disabled.storage.indexes.get("wake:urgent").warnings  # noqa: SLF001
    )

    # 热索引: 复用已缓存投影。
    warm = ConversationStore(root)
    assert _wake_snapshot(warm) == baseline_all
    before_warnings = list(warm.storage.indexes.get("wake:urgent").warnings)  # noqa: SLF001
    assert _wake_snapshot(warm) == baseline_all
    assert _wake_snapshot(warm, limit=3) == baseline_capped
    assert _wake_snapshot(warm, include_normal=False) == baseline_urgent
    # 健康索引不该产生任何"不可用"告警。
    assert warm.storage.indexes.get("wake:urgent").warnings == before_warnings == []  # noqa: SLF001
    stats = warm.storage.indexes.get("wake:urgent").stats()  # noqa: SLF001
    assert stats["hits"] >= 2
    assert stats["index"] == "wake:urgent"

    # 四种损坏: 结果与冷扫描逐字一致。
    for mode in CORRUPTION_MODES:
        broken = ConversationStore(root)
        assert _wake_snapshot(broken) == baseline_all
        _corrupt_index(broken, "wake:urgent", mode)
        _corrupt_index(broken, "wake:normal", mode)
        assert _wake_snapshot(broken) == baseline_all, mode
        assert _wake_snapshot(broken, limit=3) == baseline_capped, mode
        assert _wake_snapshot(broken, include_normal=False) == baseline_urgent, mode
        if mode in {"schema", "identity"}:
            warnings = broken.storage.indexes.get("wake:urgent").warnings  # noqa: SLF001
            assert warnings, mode
            assert all(item["schema"] == "conversation.scan_index.warning.v1" for item in warnings)
            assert all(item["index"] == "wake:urgent" for item in warnings)

    assert _tree_signature(root) == cold_before


def test_wake_load_errors_survive_every_index_state(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    baseline_errors = _wake_snapshot(ConversationStore(root))[1]
    assert baseline_errors, "夹具必须包含坏文件,否则该用例没有意义"

    warm = ConversationStore(root)
    for _ in range(3):
        assert _wake_snapshot(warm)[1] == baseline_errors
    for mode in CORRUPTION_MODES:
        broken = ConversationStore(root)
        _wake_snapshot(broken)
        _corrupt_index(broken, "wake:urgent", mode)
        _corrupt_index(broken, "wake:normal", mode)
        assert _wake_snapshot(broken)[1] == baseline_errors, mode


def test_returned_load_errors_are_private_copies(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    warm = ConversationStore(root)
    baseline = _wake_snapshot(warm)[1]
    assert baseline

    # 消费方拿到 load_error 后原地改写自己的那份,不得污染索引里的投影。
    baseline[0]["message"] = "被消费方改写的错误文案"
    baseline[0]["reason_tampered"] = True
    assert _wake_snapshot(warm)[1] == _wake_snapshot(ConversationStore(root))[1]

    policies, errors = warm.progress.list_report(enabled_only=True)
    assert errors
    errors[0]["message"] = "被消费方改写的策略错误"
    repeated, repeated_errors = warm.progress.list_report(enabled_only=True)
    assert repeated_errors == ConversationStore(root).progress.list_report(
        enabled_only=True
    )[1]


def test_wake_index_sees_in_place_edits_and_new_files(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    warm = ConversationStore(root)
    assert _wake_snapshot(warm) == _wake_snapshot(ConversationStore(root))

    # 1) 新增文件
    _seed_wake(root, "urgent", "wake-new", created_at=400.0)
    assert _wake_snapshot(warm) == _wake_snapshot(ConversationStore(root))
    # 2) 原地改写已有文件(状态翻转 / 时间变化): 原子替换会换指纹,必须被看见
    _seed_wake(root, "urgent", "wake-001", created_at=7.0)
    assert _wake_snapshot(warm) == _wake_snapshot(ConversationStore(root))
    # 3) 删除文件
    (root / "wake_queue" / "urgent" / "wake-001.json").unlink()
    assert _wake_snapshot(warm) == _wake_snapshot(ConversationStore(root))
    # 4) 把一条 handled 残留改回 pending
    _seed_wake(root, "urgent", "wake-handled-left", created_at=9.0, status="pending")
    indexed = _wake_snapshot(warm)
    assert indexed == _wake_snapshot(ConversationStore(root))
    assert any(item["wake_signal_id"] == "wake-handled-left" for item in indexed[0])


def test_observation_report_matches_across_index_states(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    cold_before = _tree_signature(root)

    baseline_store = ConversationStore(root)
    baseline_all = _observation_snapshot(baseline_store)
    baseline_capped = _observation_snapshot(baseline_store, limit=2)
    assert len(baseline_all) >= 5

    for _ in range(2):
        assert _observation_snapshot(ConversationStore(root)) == baseline_all

    disabled = ConversationStore(root)
    _disable_index(disabled, "observations")
    assert _observation_snapshot(disabled) == baseline_all
    assert _observation_snapshot(disabled, limit=2) == baseline_capped
    assert any(
        item["reason"] == "dir_too_large"
        for item in disabled.storage.indexes.get("observations").warnings  # noqa: SLF001
    )

    warm = ConversationStore(root)
    assert _observation_snapshot(warm) == baseline_all
    assert _observation_snapshot(warm) == baseline_all
    assert _observation_snapshot(warm, limit=2) == baseline_capped
    assert warm.storage.indexes.get("observations").warnings == []  # noqa: SLF001

    for mode in CORRUPTION_MODES:
        broken = ConversationStore(root)
        assert _observation_snapshot(broken) == baseline_all
        _corrupt_index(broken, "observations", mode)
        assert _observation_snapshot(broken) == baseline_all, mode
        assert _observation_snapshot(broken, limit=2) == baseline_capped, mode

    assert _tree_signature(root) == cold_before


# LLM: 热handled文件也应按指纹命中，但坏观察分片仍逐次回读；这是更严格的零重复JSON读取合同。
# 函数用途: 核对原handled优化升级后只重读脏分片，不削弱ScanIndex指标。
def test_observation_hot_path_reads_handled_map_once(tmp_path, monkeypatch) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    warm = ConversationStore(root)
    _observation_snapshot(warm)

    reads: list[str] = []
    original = Path.read_text

    def counting(self: Path, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    _observation_snapshot(warm)
    # 未变化的handled映射也不再读JSON；脏快照依然不入索引，每轮回权威读取。
    assert reads == ["thread-dirty.jsonl"], reads


def test_progress_policy_report_matches_across_index_states(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    cold_before = _tree_signature(root)

    baseline_store = ConversationStore(root)
    baseline_enabled = _policy_snapshot(baseline_store, enabled_only=True)
    baseline_all = _policy_snapshot(baseline_store)
    baseline_due = _due_snapshot(baseline_store)
    assert baseline_enabled[1], "夹具必须包含坏策略文件"

    for _ in range(2):
        assert _policy_snapshot(ConversationStore(root), enabled_only=True) == baseline_enabled

    disabled = ConversationStore(root)
    _disable_index(disabled, "progress_policies")
    assert _policy_snapshot(disabled, enabled_only=True) == baseline_enabled
    assert _policy_snapshot(disabled) == baseline_all
    assert _due_snapshot(disabled) == baseline_due

    warm = ConversationStore(root)
    assert _policy_snapshot(warm, enabled_only=True) == baseline_enabled
    assert _policy_snapshot(warm, enabled_only=True) == baseline_enabled
    assert _policy_snapshot(warm) == baseline_all
    assert _due_snapshot(warm) == baseline_due
    assert warm.storage.indexes.get("progress_policies").warnings == []  # noqa: SLF001

    for mode in CORRUPTION_MODES:
        broken = ConversationStore(root)
        assert _policy_snapshot(broken, enabled_only=True) == baseline_enabled
        _corrupt_index(broken, "progress_policies", mode)
        assert _policy_snapshot(broken, enabled_only=True) == baseline_enabled, mode
        assert _policy_snapshot(broken) == baseline_all, mode
        assert _due_snapshot(broken) == baseline_due, mode

    assert _tree_signature(root) == cold_before


def test_policy_index_sees_status_and_due_changes(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    warm = ConversationStore(root)
    assert _policy_snapshot(warm, enabled_only=True) == _policy_snapshot(
        ConversationStore(root), enabled_only=True
    )

    _seed_policy(root, "policy-000", enabled=True, next_due_at=5.0)
    assert _policy_snapshot(warm, enabled_only=True) == _policy_snapshot(
        ConversationStore(root), enabled_only=True
    )
    assert _due_snapshot(warm) == _due_snapshot(ConversationStore(root))


def test_wake_and_policy_writes_are_visible_through_warm_index(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)

    signal = store.wakes.raise_signal(
        {"thread_id": thread.thread_id, "reason": "urgent_child_event", "now": 20.0}
    )
    assert [item.wake_signal_id for item in store.wakes.pending(limit=0)] == [
        signal.wake_signal_id
    ]
    # 原地元数据更新(原子替换)必须被看见,不能被索引挡住。
    cached = store.wakes.cache_delivery(signal.wake_signal_id, {"prompt": "owner payload"})
    assert cached is not None
    assert cached.metadata["owner_delivery"] == {"prompt": "owner payload"}
    assert store.wakes.pending(limit=0)[0].metadata["owner_delivery"] == {
        "prompt": "owner payload"
    }
    store.wakes.mark_handled(signal.wake_signal_id, now=30.0)
    assert store.wakes.pending(limit=0) == []

    policy = store.progress.create(
        {"thread_id": thread.thread_id, "interval_seconds": 300, "now": 1_000.0}
    )
    assert policy.policy_id in [
        item.policy_id for item in store.progress.list(enabled_only=True)
    ]
    store.progress.mark_failed(
        policy.policy_id, now=1_100.0, backoff_seconds=60.0, failure_count=3
    )
    assert policy.policy_id not in [
        item.policy_id for item in store.progress.list(enabled_only=True)
    ]
    assert policy.policy_id in [item.policy_id for item in store.progress.list()]

    observation = store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "child_agent_event",
            "summary": "需要主代理判断",
            "requires_main_agent": True,
            "now": 40.0,
        }
    )
    assert observation.observation_id in [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)
    ]
    store.observations.mark_handled([observation.observation_id], now=50.0)
    assert observation.observation_id not in [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)
    ]


def test_scan_index_stays_bounded_and_reports_structured_warnings(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    store = ConversationStore(root)

    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    index.max_entries = 4
    assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
    assert index.stats()["entries"] == 0
    warning = index.warnings[0]
    assert warning["schema"] == "conversation.scan_index.warning.v1"
    assert warning["reason"] == "dir_too_large"
    assert warning["entries"] > warning["limit"] == 4
    # 告警按 (reason,key) 去重: 反复回退不会刷屏。
    for _ in range(5):
        _wake_snapshot(store)
    assert len(index.warnings) == 1

    budget = ConversationStore(root)
    budget_index = budget.storage.indexes.get("observations")  # noqa: SLF001
    assert budget_index.max_entries >= 1
    _seed_observations(
        root,
        "thread-huge",
        [
            _observation_row(f"obs-huge-{position:05d}", thread_id="thread-huge")
            for position in range(_SCAN_INDEX_MAX_RECORDS_PER_FILE_COUNT + 10)
        ],
    )
    assert _observation_snapshot(budget) == _observation_snapshot(ConversationStore(root))
    assert "thread-huge.jsonl" not in budget_index._entries  # noqa: SLF001
    assert [item["reason"] for item in budget_index.warnings] == [
        "entry_record_budget_exceeded"
    ]
    assert budget_index.stats()["records"] <= budget_index.stats()["max_records"]


def test_scan_index_entries_budget_holds_under_name_rotation(tmp_path) -> None:
    """文件轮换/删除: 连续创建 N>max_entries 个不同名文件并穿插删除。"""
    root = tmp_path / "conversations"
    limit = 4
    store = _bounded_wake_store(root, limit)
    queue = store.storage.wake_queue_dir / "urgent"

    live: dict[str, Path] = {}
    for position in range(12):
        name = f"wake-rot-{position:02d}"
        live[name] = _seed_wake(root, "urgent", name, created_at=100.0 + position)
        # 每轮删掉最早的一个:磁盘上同时存在的文件数始终远小于累计名字数。
        if position >= 3:
            live.pop(f"wake-rot-{position - 3:02d}").unlink()
        assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
        assert _index_stats(store, "wake:urgent")["entries"] <= limit, position

    # 被删名字不得驻留:条目集合恰好等于磁盘上真实存在的文件名集合。
    assert {path.name for path in queue.glob("*.json")} == {
        f"{name}.json" for name in live
    }
    retained = set(store.storage.indexes.get("wake:urgent")._entries)  # noqa: SLF001
    assert retained == {path.name for path in queue.glob("*.json")}
    assert retained.isdisjoint({f"wake-rot-{position:02d}.json" for position in range(9)})

    # 收尾:全部删光后热索引必须回到空,且查询与冷 store 逐字一致。
    for path in list(live.values()):
        path.unlink()
    assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root)) == ([], [])
    final = _index_stats(store, "wake:urgent")
    assert final["entries"] == 0
    assert final["prunes"] > 0


def test_scan_index_entries_budget_when_live_set_stays_small(tmp_path) -> None:
    """磁盘同时存在的文件数(1) < 上限,累计出现过的名字(63) >> 上限。"""
    root = tmp_path / "conversations"
    limit = 4
    store = _bounded_wake_store(root, limit)

    for position in range(63):
        name = f"wake-churn-{position:03d}"
        path = _seed_wake(root, "urgent", name, created_at=1.0 + position)
        assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
        path.unlink()
        assert _index_stats(store, "wake:urgent")["entries"] <= limit, position

    survivor = _seed_wake(root, "urgent", "wake-churn-final", created_at=999.0)
    hot = _wake_snapshot(store)
    assert hot == _wake_snapshot(ConversationStore(root))
    assert [item["wake_signal_id"] for item in hot[0]] == ["wake-churn-final"]
    assert hot[1] == []
    stats = _index_stats(store, "wake:urgent")
    assert stats["entries"] <= limit
    assert set(store.storage.indexes.get("wake:urgent")._entries) == {  # noqa: SLF001
        survivor.name
    }


def test_scan_index_lru_evicts_oldest_when_one_scan_exceeds_limit(tmp_path) -> None:
    """累计名字超过上限: 单轮写入超过上限时按 LRU 淘汰,查询结果不受影响。"""
    root = tmp_path / "conversations"
    limit = 4
    store = ConversationStore(root)
    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    index.max_entries = limit

    # (1) 纯索引层: 单轮写入 6 条(dict 负载,记录数恒为 0)必须仍受条目预算约束。
    for position in range(6):
        index.store(
            Path(f"/probe/wake_queue/urgent/entry-{position:02d}.json"),
            (1, position + 1, position, position),
            ok=True,
            payload={"wake_signal_id": f"entry-{position:02d}"},
        )
    assert index.stats()["entries"] == limit
    assert index.stats()["evictions"] == 2
    assert set(index._entries) == {f"entry-{position:02d}.json" for position in range(2, 6)}

    # (2) 真实查询路径: 同一轮里出现 6 个文件时,目录规模本身超上限 → 整体回退全量扫描,
    # 既不写入也不淘汰,但结果必须与冷 store 逐字一致(上界只影响耗时)。
    names = [f"wake-burst-{position:02d}" for position in range(6)]
    for position, name in enumerate(names):
        _seed_wake(root, "urgent", name, created_at=1.0 + position)
    index.clear()
    hot = _wake_snapshot(store)
    assert hot == _wake_snapshot(ConversationStore(root))
    assert [item["wake_signal_id"] for item in hot[0]] == names
    assert hot[1] == []
    assert index.stats()["entries"] == 0
    assert any(item["reason"] == "dir_too_large" for item in index.warnings)
    for name in names:
        (root / "wake_queue" / "urgent" / f"{name}.json").unlink()

    # (3) 单轮内扫描超过上限个文件: 目录规模本身超上限 → 整体回退全量扫描(不写入也不淘汰),
    # 条目数不许越界,结果仍与冷 store 逐字一致;条目维度的 LRU 语义由 (1) 直接对索引断言。
    steady = [f"wake-steady-{position:02d}" for position in range(6)]
    for position, name in enumerate(steady[:4]):
        _seed_wake(root, "urgent", name, created_at=200.0 + position)
        assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
    assert index.stats()["entries"] == limit, index.stats()
    for offset, name in enumerate(steady[4:], start=4):
        _seed_wake(root, "urgent", name, created_at=200.0 + offset)
        assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
        # 目录里已有 5/6 个文件 > 上限 4: 本轮整体不可用,条目数不许越界。
        assert index.stats()["entries"] <= limit, index.stats()
    assert index.stats()["records"] == 0

    # (4) 长时间轮换: 磁盘上同时只有 2 个文件(始终小于上限),累计名字远多于上限,
    # 条目数必须稳定贴在上限而不是随累计名字数增长。
    for name in steady:
        (root / "wake_queue" / "urgent" / f"{name}.json").unlink()
    _wake_snapshot(store)
    for position in range(20):
        name = f"wake-churn-{position:02d}"
        _seed_wake(root, "urgent", name, created_at=400.0 + position)
        assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root)), position
        if position >= 2:
            (root / "wake_queue" / "urgent" / f"wake-churn-{position - 2:02d}.json").unlink()
        stats = index.stats()
        assert stats["entries"] <= limit, (position, stats)
        assert stats["records"] == 0
    assert index.stats()["prunes"] > 0


def test_scan_index_does_not_prune_when_directory_is_unreadable(
    tmp_path, monkeypatch
) -> None:
    """不误删: 目录短暂不可读时不得清空有效条目,查询必须回退全量扫描且结果一致。"""
    root = tmp_path / "conversations"
    store = _bounded_wake_store(root, 4)
    for position in range(3):
        _seed_wake(root, "urgent", f"wake-kept-{position}", created_at=1.0 + position)
    baseline = _wake_snapshot(store)
    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    before = set(index._entries)
    assert before == {f"wake-kept-{position}.json" for position in range(3)}

    # 模拟"目录暂时读不到"(权限抖动/IO 故障),区别于"目录为空"。CPython 的 os.scandir 是
    # C 槽位、改不动,所以这里直接对目标目录让生产用的 listing 助手返回 None(它只在
    # OSError 分支返回 None),从而确定性地走"不可列出"状态机。
    real_match_names = store_module._scan_dir_match_names  # noqa: SLF001
    real_glob = Path.glob
    blocked = store.storage.wake_queue_dir / "urgent"

    def unreadable_match_names(directory, pattern):
        if Path(directory) == blocked:
            return None
        return real_match_names(directory, pattern)

    def broken_glob(self, pattern, *args, **kwargs):
        if self == blocked:
            raise PermissionError("模拟目录暂时不可读")
        return real_glob(self, pattern, *args, **kwargs)

    with monkeypatch.context() as patch:
        # 情形一: 目录列不出来,但旧路径(glob)仍能枚举 → 结果与基线逐字一致。
        patch.setattr(store_module, "_scan_dir_match_names", unreadable_match_names)
        snapshot = _wake_snapshot(store)
        assert snapshot == baseline
        assert set(index._entries) == before, "目录不可读时不得清掉任何有效条目"
        assert index.stats()["prunes"] == 0
        # 情形二: 目录连枚举都失败 → 干净的"空结果",绝不能是半截结果;索引仍不被清。
        patch.setattr(store_module.Path, "glob", broken_glob)
        again = _wake_snapshot(store)
        assert again == ([], [])
        stats = index.stats()
        assert set(index._entries) == before
        assert stats["prunes"] == 0

    assert stats["dir_unreadable"] >= 2
    # 降级原因可观测,且按 (reason,key) 去重,不刷屏。
    assert [item["reason"] for item in index.warnings] == ["dir_unreadable"]
    assert stats["entries"] <= stats["max_entries"]

    # 目录恢复可读后,热索引必须回到与冷 store 逐字一致的结果。
    assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root)) == baseline


def test_scan_index_enumeration_matches_glob_for_non_regular_entries(tmp_path) -> None:
    """枚举成员口径必须与 Path.glob 逐字相同: 非普通文件也算成员,不许被静默过滤掉。"""
    root = tmp_path / "conversations"
    queue = root / "wake_queue" / "urgent"
    queue.mkdir(parents=True, exist_ok=True)
    _seed_wake(root, "urgent", "wake-live", created_at=1.0)
    (queue / "wake-old.json.bak").write_text("{}", encoding="utf-8")
    (queue / ".wake-hidden.json").write_text("{}", encoding="utf-8")
    (queue / "wake-中文.json").write_text("{}", encoding="utf-8")
    (queue / "wake-dir.json").mkdir(parents=True, exist_ok=True)
    (queue / "wake-upper.JSON").write_text("{}", encoding="utf-8")
    ghosts: list[str] = []
    try:
        os.symlink(queue / "never-existed.json", queue / "wake-ghost.json")
        ghosts.append("wake-ghost.json")
    except OSError:  # pragma: no cover - 平台不允许建符号链接时只跳过这一项
        pass

    # (1) 成员集合与 Path.glob 逐字相同。非普通条目(FIFO 等)单独放在一个 store 永远不会
    # 读到的目录里: 读 FIFO 会阻塞(既有行为,与本索引无关),不能拿它去喂真实查询。
    parity = tmp_path / "parity"
    parity.mkdir()
    (parity / "a.json").write_text("{}", encoding="utf-8")
    (parity / "b.jsonl").write_text("{}", encoding="utf-8")
    (parity / "c.json.bak").write_text("{}", encoding="utf-8")
    (parity / "UPPER.JSON").write_text("{}", encoding="utf-8")
    (parity / "sub.json").mkdir()
    try:
        os.symlink(parity / "never-existed.json", parity / "broken.json")
    except OSError:  # pragma: no cover - 同上
        pass
    if hasattr(os, "mkfifo"):
        try:
            os.mkfifo(parity / "fifo.json")
        except OSError:  # pragma: no cover - 同上
            pass
    for pattern in ("*.json", "*.json*", "*.jsonl", "*.JSON"):
        expected = {path.name for path in parity.glob(pattern)}
        assert store_module._scan_dir_match_names(parity, pattern) == expected, pattern  # noqa: SLF001
        assert store_module._scan_dir_match_names(queue, pattern) == {  # noqa: SLF001
            path.name for path in queue.glob(pattern)
        }, pattern

    # (2) 安全语义: 断链/不可读成员必须照旧产出结构化 load_error。runtime/guidance 消费方
    # 以"有 load_error 就不消费"作硬门,索引不许把这类文件静默跳过(那等于绕过硬门)。
    store = ConversationStore(root)
    hot = _wake_snapshot(store)
    cold = _wake_snapshot(ConversationStore(root))
    assert hot == cold
    assert "wake-live" in [item["wake_signal_id"] for item in hot[0]]
    failed = {Path(item["path"]).name for item in hot[1]}
    assert failed >= set(ghosts)
    assert failed >= {"wake-dir.json"}
    # 再来一轮(热索引已建立)必须仍然看得见这些失败文件。
    assert _wake_snapshot(store) == hot


def test_scan_index_dir_too_large_uses_the_query_pattern(tmp_path) -> None:
    """上界口径必须与被扫描的文件集合同源: 不匹配查询模式的文件不得抬高 dir_too_large。"""
    root = tmp_path / "conversations"
    limit = 4
    store = _bounded_wake_store(root, limit)
    queue = store.storage.wake_queue_dir / "urgent"

    for position in range(limit - 1):
        _seed_wake(root, "urgent", f"wake-live-{position}", created_at=1.0 + position)
    # 目录里另有 4 个不匹配 *.json 的邻居: 它们不是本轮要扫的目标,不得计入上界。
    for position in range(4):
        (queue / f"wake-noise-{position}.json.bak").write_text("{}", encoding="utf-8")
    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    assert _wake_snapshot(store) == _wake_snapshot(ConversationStore(root))
    assert index.stats()["entries"] == limit - 1
    assert [item["reason"] for item in index.warnings] == []

    # 真正匹配的文件数超过上界时,照旧整体回退全量扫描并留结构化告警。
    for position in range(2):
        _seed_wake(root, "urgent", f"wake-extra-{position}", created_at=50.0 + position)
    hot = _wake_snapshot(store)
    assert hot == _wake_snapshot(ConversationStore(root))
    assert len(hot[0]) == limit + 1
    assert [item["reason"] for item in index.warnings] == ["dir_too_large"]


def test_scan_index_schema_constant_is_self_consistent(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    assert index.schema == _SCAN_INDEX_SCHEMA
    assert index.stats()["schema"] == _SCAN_INDEX_SCHEMA


def test_ordered_scan_keeps_path_sort_order_for_tricky_names(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    queue = store.storage.wake_queue_dir / "urgent"
    queue.mkdir(parents=True, exist_ok=True)
    for name in (
        "wake-0002.json",
        "wake-10.json",
        "Wake-1.json",
        "wake.extra.json",
        ".wake-hidden.json",
        "唤醒-中文.json",
        "wake-0001.json",
    ):
        (queue / name).write_text("{}", encoding="utf-8")
    # 目录名也匹配 pattern: 旧实现的 glob 会把它当成一条记录(读取时报错),顺序也必须一致。
    (queue / "zz-dir.json").mkdir(parents=True, exist_ok=True)

    index = store.storage.indexes.get("wake:urgent")  # noqa: SLF001
    expected = [path.name for path in sorted(queue.glob("*.json"))]
    first = index.ordered_paths(queue, "*.json")
    assert [path.name for path in first] == expected

    # 文件集合不变 → 直接复用同一份排列(这是省掉 pathlib 比较的证据,不改变顺序语义)。
    second = index.ordered_paths(queue, "*.json")
    assert second is first

    # 集合变化 → 重新按 Path 排序,结果与直接 sorted() 一致。
    (queue / "wake-0000.json").write_text("{}", encoding="utf-8")
    third = index.ordered_paths(queue, "*.json")
    assert [path.name for path in third] == [
        path.name for path in sorted(queue.glob("*.json"))
    ]


def test_concurrent_observation_writers_do_not_lose_or_duplicate(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)
    total = 120
    stop = threading.Event()
    seen: list[list[str]] = []
    errors: list[BaseException] = []

    def writer(offset: int) -> None:
        try:
            for position in range(offset, total, 4):
                store.observations.append(
                    {
                        "thread_id": thread.thread_id,
                        "event_type": "child_agent_event",
                        "summary": f"row {position}",
                        "requires_main_agent": True,
                        "now": 100.0 + position,
                    }
                )
        except BaseException as exc:  # pragma: no cover - 失败要在主线程里报出来
            errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            ids = [
                item.observation_id
                for item in store.observations.unhandled_requiring_main(limit=0)
            ]
            seen.append(ids)

    with ThreadPoolExecutor(max_workers=5) as pool:
        readers = [pool.submit(reader) for _ in range(2)]
        writers = [pool.submit(writer, offset) for offset in range(4)]
        for future in writers:
            future.result()
        stop.set()
        for future in readers:
            future.result()

    assert not errors
    for ids in seen:
        assert len(ids) == len(set(ids)), "并发读返回了重复观察事件"
    final = [item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)]
    assert len(final) == len(set(final)) == total, "并发写入后丢失或重复了观察事件"
    assert final == [
        item.observation_id
        for item in ConversationStore(root).observations.unhandled_requiring_main(limit=0)
    ]


def test_concurrent_wake_writers_do_not_lose_or_duplicate(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)
    total = 60
    stop = threading.Event()
    seen: list[list[str]] = []
    errors: list[BaseException] = []

    def writer(offset: int, kind: str) -> None:
        try:
            for position in range(offset, total, 3):
                store.wakes.raise_signal(
                    {
                        "thread_id": thread.thread_id,
                        "reason": "agent_event",
                        "urgency": kind,
                        "summary": f"signal {position}",
                        "dedupe_key": f"dedupe-{position}",
                        "now": 200.0 + position,
                    }
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            signals, load_errors = store.wakes.pending_report(limit=0)
            assert not load_errors
            seen.append([item.wake_signal_id for item in signals])

    with ThreadPoolExecutor(max_workers=4) as pool:
        readers = [pool.submit(reader)]
        writers = [
            pool.submit(writer, 0, "urgent"),
            pool.submit(writer, 1, "normal"),
            pool.submit(writer, 2, "urgent"),
        ]
        for future in writers:
            future.result()
        stop.set()
        for future in readers:
            future.result()

    assert not errors
    for ids in seen:
        assert len(ids) == len(set(ids))
    final, load_errors = store.wakes.pending_report(limit=0)
    assert not load_errors
    ids = [item.wake_signal_id for item in final]
    assert len(ids) == len(set(ids)) == total


def test_concurrent_policy_writers_do_not_lose_or_duplicate(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)
    total = 40
    stop = threading.Event()
    seen: list[int] = []
    errors: list[BaseException] = []

    def writer(offset: int) -> None:
        try:
            for position in range(offset, total, 3):
                store.progress.create(
                    {
                        "thread_id": thread.thread_id,
                        "interval_seconds": 300,
                        "now": 300.0 + position,
                    }
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            policies, load_errors = store.progress.list_report()
            assert not load_errors
            ids = [item.policy_id for item in policies]
            assert len(ids) == len(set(ids))
            seen.append(len(ids))

    with ThreadPoolExecutor(max_workers=4) as pool:
        readers = [pool.submit(reader)]
        writers = [pool.submit(writer, offset) for offset in range(3)]
        for future in writers:
            future.result()
        stop.set()
        for future in readers:
            future.result()

    assert not errors
    assert seen
    policies, load_errors = store.progress.list_report()
    assert not load_errors
    ids = [item.policy_id for item in policies]
    assert len(ids) == len(set(ids)) == total


def test_repeated_hot_queries_are_stable_and_ordered(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    store = ConversationStore(root)
    first = _wake_snapshot(store)
    for _ in range(5):
        assert _wake_snapshot(store) == first
    urgencies = [item["urgency"] for item in first[0]]
    created = [item["created_at"] for item in first[0]]
    assert urgencies == sorted(urgencies, key=lambda value: 0 if value == "urgent" else 1)
    urgent_created = [value for value, kind in zip(created, urgencies, strict=True) if kind == "urgent"]
    normal_created = [value for value, kind in zip(created, urgencies, strict=True) if kind == "normal"]
    assert urgent_created == sorted(urgent_created)
    assert normal_created == sorted(normal_created)

    observations = _observation_snapshot(store)
    assert [item["observed_at"] for item in observations] == sorted(
        item["observed_at"] for item in observations
    )
    assert [item["policy_id"] for item in _policy_snapshot(store, enabled_only=True)[0]] == [
        item["policy_id"]
        for item in sorted(
            _policy_snapshot(store, enabled_only=True)[0], key=lambda entry: entry["next_due_at"]
        )
    ]
    assert pytest.approx(_due_snapshot(store)[0]["next_due_at"]) == _due_snapshot(store)[0][
        "next_due_at"
    ]


def test_task_link_fingerprint_cache_and_wake_model_cache_avoid_repeated_reads(
    tmp_path, monkeypatch
) -> None:
    from agent_py_agent.agent.conversation.models import WakeSignal

    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)
    link = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-cache-001",
            "title": "cache check",
            "now": 100.0,
        }
    )
    assert link.status == "active"
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": link.task_id,
            "reason": "subagent_runner_finished",
            "urgency": "normal",
            "now": 101.0,
        }
    )
    # 首次暖机读取填充指纹缓存与 WakeSignal 模型缓存。
    links_first, errors_first = store.tasks.list_report(thread.thread_id)
    assert not errors_first and [item.task_id for item in links_first] == ["task-cache-001"]
    wakes_first, wake_errors_first = store.wakes.pending_report(limit=0)
    assert not wake_errors_first and [item.wake_signal_id for item in wakes_first] == [
        signal.wake_signal_id
    ]

    task_path = store.storage.task_path("task-cache-001")
    original_read_text = Path.read_text
    task_reads: list[str] = []

    def counting_read_text(self: Path, *args, **kwargs):
        if self == task_path:
            task_reads.append(self.name)
        return original_read_text(self, *args, **kwargs)

    original_from_dict = WakeSignal.from_dict
    wake_model_calls: list[str] = []

    def counting_from_dict(data):
        wake_model_calls.append(str(data.get("wake_signal_id") or ""))
        return original_from_dict(data)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    monkeypatch.setattr(WakeSignal, "from_dict", counting_from_dict)

    links_second, errors_second = store.tasks.list_report(thread.thread_id)
    assert not errors_second and [item.task_id for item in links_second] == ["task-cache-001"]
    assert task_reads == []

    wakes_second, wake_errors_second = store.wakes.pending_report(limit=0)
    assert not wake_errors_second and [item.wake_signal_id for item in wakes_second] == [
        signal.wake_signal_id
    ]
    assert wake_model_calls == []

    # 状态更新后指纹变化，必须立即读到新状态。
    updated = store.tasks.update_status(
        {"task_id": "task-cache-001", "status": "completed", "expected_status": "active"}
    )
    assert updated is not None and updated.status == "completed"
    links_third, errors_third = store.tasks.list_report(thread.thread_id)
    assert not errors_third and [item.status for item in links_third] == ["completed"]


def test_task_link_and_wake_cache_isolate_in_place_dict_mutations(tmp_path) -> None:
    import json

    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = _thread(store)
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-mut-001",
            "title": "mutation isolation",
            "now": 100.0,
        }
    )
    task_path = store.storage.task_path("task-mut-001")
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["host_channel_actor"] = {
        "owner_id": thread.owner_id,
        "source_thread_id": thread.thread_id,
        "actor_key": "original",
    }
    payload["effective_source_bindings"] = [{"source_id": "src-1"}]
    task_path.write_text(json.dumps(payload), encoding="utf-8")

    first_link, err1 = store.tasks.load_report("task-mut-001")
    assert err1 is None and first_link is not None
    assert first_link.host_channel_actor is not None
    first_link.host_channel_actor["actor_key"] = "mutated"
    first_link.effective_source_bindings[0]["source_id"] = "mutated-src"

    second_link, err2 = store.tasks.load_report("task-mut-001")
    assert err2 is None and second_link is not None
    assert second_link.host_channel_actor == {
        "owner_id": thread.owner_id,
        "source_thread_id": thread.thread_id,
        "actor_key": "original",
    }
    assert second_link.effective_source_bindings == ({"source_id": "src-1"},)

    sig = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-mut-001",
            "reason": "subagent_runner_finished",
            "urgency": "normal",
            "metadata": {"k": "original"},
            "now": 101.0,
        }
    )
    first_wakes, werr1 = store.wakes.pending_report(limit=0)
    assert not werr1 and len(first_wakes) == 1
    first_wakes[0].metadata["k"] = "mutated"

    second_wakes, werr2 = store.wakes.pending_report(limit=0)
    assert not werr2 and len(second_wakes) == 1
    assert second_wakes[0].wake_signal_id == sig.wake_signal_id
    assert second_wakes[0].metadata == {"k": "original"}


def test_reclaim_dead_running_runs_passes_all_tasks_for_host_pid_veto(monkeypatch) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep

    stalled_running = SimpleNamespace(
        id="run-stalled",
        status="RUNNING",
        heartbeat_at=1.0,
        updated_at=1.0,
        attributes={
            "audit_source_worker": True,
            "audit_source_watch_id": "w-1",
            "audit_source_owner_home": "/tmp/owner",
            "runner_session": {
                "status": "running",
                "worker_pid": 424242,
                "in_process": False,
                "heartbeat_at": 1.0,
            },
        },
    )
    pending_sibling = SimpleNamespace(
        id="run-sibling",
        status="PENDING",
        heartbeat_at=9999999999.0,
        updated_at=9999999999.0,
        attributes={
            "audit_source_worker": True,
            "audit_source_watch_id": "w-2",
            "audit_source_owner_home": "/tmp/owner",
            "runner_session": {
                "status": "running",
                "worker_pid": 424242,
                "in_process": False,
                "heartbeat_at": 9999999999.0,
            },
        },
    )
    captured_host_pids: list[set[int]] = []
    monkeypatch.setattr(
        "agent_py_agent.agent.subagents.process_control.is_pid_alive",
        lambda pid: pid == 424242,
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "_terminate_fully_stalled_source_hosts",
        lambda pids: captured_host_pids.append(set(pids)) or {},
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "conversation_lifecycle_decisions",
        lambda _agent, _tasks: {},
    )
    agent = SimpleNamespace(
        subagents=SimpleNamespace(list_runs=lambda: [stalled_running, pending_sibling])
    )
    capability_auto_sweep._reclaim_dead_running_runs(agent)
    assert captured_host_pids == [set()]


# LLM: 包装真实from_dict及原转换器，只计数不模拟业务结果；ScanIndex读口保持原调用。
# 函数用途: 为planner记忆化测试记录一次真实建模调用。
def _planner_model_spy(monkeypatch, target, name):
    original = getattr(target, name)
    calls = []

    # LLM: 原返回和异常不变，不把spy调用当作业务成功。
    # 函数用途: 收集建模参数并透传真实函数。
    def count(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, count)
    return calls


# LLM: handled变化必须立即使分片事件memo失效，零处理时间仍沿原handled_at<=0语义；返回metadata不可污染原负载。
# 函数用途: 核对观察模型只建一次、确认即时可见及ScanIndex逐轮命中指标。
def test_planner_memo_observations_and_handled_updates(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.models import ObservationEvent

    store = ConversationStore(tmp_path / 'conversations')
    rows = [_observation_row('live'), _observation_row('zero'), _observation_row('self-handled', handled_at=5)]
    _seed_observations(store.storage.root, 'thread-1', rows)
    store.storage.observation_handled_path.write_text('{"zero": 0}', encoding='utf-8')
    calls = _planner_model_spy(monkeypatch, ObservationEvent, 'from_dict')
    first = store.observations.unhandled_requiring_main(limit=0)
    assert [item.observation_id for item in first] == ['live', 'zero']
    assert len(calls) == 3
    first[0].metadata['seed'] = 'caller'
    assert store.observations.unhandled_requiring_main(limit=0)[0].metadata == {'seed': 'live'}
    assert len(calls) == 3
    assert _index_stats(store, 'observations')['hits'] == 1
    store.observations.mark_handled(['live'], now=10)
    assert [item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)] == ['zero']
    assert len(calls) == 6
    assert _index_stats(store, 'observations')['hits'] == 2
    _seed_observations(store.storage.root, 'thread-1', [*rows, _observation_row('new')])
    assert [item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)] == ['zero', 'new']
    assert len(calls) == 10


# LLM: 缺失/错误handled文件不得复用上次处理代次；读取窗口内变化也不可发布成稳定缓存。
# 函数用途: 检查处理标记被删除、损坏和恢复时沿原空映射语义即时失效。
def test_planner_memo_handled_missing_corrupt_and_restored(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    _seed_observations(store.storage.root, 'thread-1', [_observation_row('event')])
    store.observations.mark_handled(['event'], now=10)
    assert store.observations.unhandled_requiring_main() == []
    for text in ['{broken', '{}', '{"event": 10}']:
        store.storage.observation_handled_path.write_text(text, encoding='utf-8')
        expected = [] if text == '{"event": 10}' else ['event']
        assert [item.observation_id for item in store.observations.unhandled_requiring_main()] == expected
    store.storage.observation_handled_path.unlink()
    assert [item.observation_id for item in store.observations.unhandled_requiring_main()] == ['event']


# LLM: 现有wake全局模型缓存不能代替本次对象转换器memo；两类ScanIndex每拍read仍须原样计数。
# 函数用途: 验证策略/wake转换器热拍不再调用，外部更新、错误与返回字典隔离不变。
@pytest.mark.parametrize('kind', ['policy', 'wake'])
def test_planner_memo_models_preserve_metrics_and_errors(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.conversation import store_progress, store_wakes

    store = ConversationStore(tmp_path / 'conversations')
    if kind == 'policy':
        path = _seed_policy(store.storage.root, 'policy-one')
        target, helper, index_name = store_progress, '_progress_policy_from_payload', 'progress_policies'
        query = store.progress.list_report
        numeric_field = 'next_due_at'
    else:
        path = _seed_wake(store.storage.root, 'urgent', 'wake-one')
        target, helper, index_name = store_wakes, '_wake_signal_from_payload', 'wake:urgent'
        query = store.wakes.pending_report
        numeric_field = 'created_at'
    calls = _planner_model_spy(monkeypatch, target, helper)
    first, errors = query()
    assert not errors and len(first) == 1 and len(calls) == 1
    first[0].metadata['seed'] = 'caller'
    again, errors = query()
    assert not errors and again[0].metadata['seed'] != 'caller'
    assert len(calls) == 1
    assert _index_stats(store, index_name)['hits'] == 1
    data = json.loads(path.read_text())
    data[numeric_field] = 'not-a-number'
    path.write_text(json.dumps(data), encoding='utf-8')
    failures = [query()[1] for _ in range(2)]
    assert failures[0] and failures[0] == failures[1]
    assert len(calls) == 3
    data[numeric_field] = 50
    path.write_text(json.dumps(data), encoding='utf-8')
    assert len(query()[0]) == 1
    assert len(query()[0]) == 1
    assert len(calls) == 4
    _disable_index(store, index_name)
    query()
    query()
    assert len(calls) == 6


# LLM: 输入缓存只能省JSON读取，current/armed/status判据仍每次执行，不缓存任务授权或等待决定。
# 函数用途: 构造真实输入文件及最小task refs，记录读盘次数和current检查次数。
@pytest.fixture
def planner_input_case(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import input_dependencies as module

    store = ConversationStore(tmp_path / 'conversations')
    link = SimpleNamespace(input_dependency_refs=('input-one',))
    store.tasks.load_report = lambda task_id: (link, None)
    agent = SimpleNamespace(conversation_store=store)
    path = module.input_dependency_path(agent, 'input-one')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"status":"pending","armed":true,"metadata":{"nested":[1]}}', encoding='utf-8')
    reads = _planner_model_spy(monkeypatch, module, 'read_json_object_report')
    current = []

    # LLM: 只替代与此缓存无关的owner/子任务核验资源，实际pending读取及其记录仍走产品入口。
    # 函数用途: 检查每拍拿到的metadata独立，并动态模拟原任务已结束。
    def is_current(agent, row):
        assert row['metadata']['nested'] == [1]
        row['metadata']['nested'].append('caller')
        current.append(row)
        return len(current) <= 2

    monkeypatch.setattr(module, 'input_dependency_current', is_current)
    return SimpleNamespace(module=module, agent=agent, path=path, link=link, reads=reads, current=current)


# LLM: 连续热拍仍须核验current；缓存输入payload必须深拷贝，不让嵌套scope/metadata成为共享状态。
# 函数用途: 核对输入只读一次但每拍重新检查任务活动性。
def test_planner_memo_input_reads_once_but_checks_current_each_time(planner_input_case):
    case = planner_input_case
    assert case.module.task_has_waiting_input(case.agent, 'task-one')
    assert case.module.task_has_waiting_input(case.agent, 'task-one')
    assert not case.module.task_has_waiting_input(case.agent, 'task-one')
    assert len(case.current) == 3 and len(case.reads) == 1


# LLM: 真实改写/坏JSON/缺失文件不能由旧输入快照掩盖，不缓存空值或失败。
# 函数用途: 核对输入完成、损坏与删除立即退出等待判定。
def test_planner_memo_input_invalidation(planner_input_case):
    case = planner_input_case
    assert case.module.task_has_waiting_input(case.agent, 'task-one')
    for text in ['{"status":"ready","armed":true}', '{broken', '{}']:
        case.path.write_text(text, encoding='utf-8')
        assert not case.module.task_has_waiting_input(case.agent, 'task-one')
    case.path.unlink()
    assert not case.module.task_has_waiting_input(case.agent, 'task-one')


# LLM: 协作快照的entities/metadata均可变，不能只replace浅拷贝；另一个Store须共享文件命中而非状态决定。
# 函数用途: 验证case热读不再解析JSON，状态改写及目录增删仍即时可见。
def test_planner_memo_collaboration_case_reads_and_isolation(tmp_path, monkeypatch):
    from agent_py_agent.agent.collaboration.store import CollaborationCaseStore

    store = CollaborationCaseStore(tmp_path / 'collaboration')
    case = store.open_case({'thread_id': 'thread-one', 'entities': {'rows': [1]}, 'metadata': {'rows': [2]}})
    reads = []
    original = Path.read_text

    # LLM: 只计数case JSON文件，透传真实reader的锁/解析及错误。
    # 函数用途: 记录协作目录的物理读取，不干涉其它账本。
    def read(path, *args, **kwargs):
        if path.parent == store.cases_dir:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    first = store.list_cases(status='open')[0]
    first.entities['rows'].append('caller')
    first.metadata['rows'].append('caller')
    again = CollaborationCaseStore(store.root).list_cases(status='open')[0]
    assert again.entities == {'rows': [1]} and again.metadata == {'rows': [2]}
    assert len(reads) == 1
    path = store._case_path(case.case_id)
    data = json.loads(original(path))
    data['status'] = 'closed'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert store.list_cases(status='open') == []
    assert len(reads) == 2
    path.unlink()
    assert store.list_cases() == []
    path.write_text('{broken', encoding='utf-8')
    assert store.list_cases() == []
    path.write_text(json.dumps(data), encoding='utf-8')
    assert store.list_cases()[0].status == 'closed'


# LLM: 只追踪产品openat获得的真实FD，保留真实nofollow/dir_fd；失败的红灯测试也回收泄漏，不能污染后续用例。
# 函数用途: 为目录FD测试记录当前仍持有的中间句柄，并在结束时清理自有资源。
@pytest.fixture
def planner_directory_fds(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.runtime_db import operations

    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    active = set()
    original_open, original_close = os.open, os.close

    # LLM: openat仍由内核执行；每段必须带nofollow和directory，不允许修泄漏时放松路径隔离。
    # 函数用途: 记录实际打开的子目录FD。
    def opened(path, flags, *args, **kwargs):
        if kwargs.get('dir_fd') is not None:
            assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
        fd = original_open(path, flags, *args, **kwargs)
        if kwargs.get('dir_fd') is not None:
            active.add(fd)
        return fd

    # LLM: 只在真实close成功后移除记录；根FD由fixture所有，不属于产品函数。
    # 函数用途: 跟踪产品回收过的FD。
    def closed(fd):
        original_close(fd)
        active.discard(fd)

    with monkeypatch.context() as patch:
        patch.setattr(operations.os, 'open', opened)
        patch.setattr(operations.os, 'close', closed)
        try:
            yield SimpleNamespace(module=operations, root_fd=root_fd, active=active, root=tmp_path)
        finally:
            for fd in list(active):
                original_close(fd)
            original_close(root_fd)


# LLM: 走真实多级openat，成功只向调用方移交末级父FD，根FD必须仍有效。
# 函数用途: 检查已有和自动创建的三层目录都不泄漏中间FD。
@pytest.mark.parametrize('create_dirs', [False, True])
def test_planner_memo_parent_fd_success_closes_intermediates(planner_directory_fds, create_dirs):
    from pathlib import PurePosixPath

    case = planner_directory_fds
    if not create_dirs:
        (case.root / 'a' / 'b' / 'c').mkdir(parents=True)
    parent, leaf = case.module._resolve_parent_fd(
        case.root_fd, PurePosixPath('a/b/c/file.json').as_posix(), create_dirs=create_dirs,
    )
    assert leaf == 'file.json' and case.active == {parent}
    assert os.fstat(case.root_fd).st_ino == case.root.stat().st_ino
    assert os.fstat(parent).st_ino == (case.root / 'a' / 'b' / 'c').stat().st_ino
    os.close(parent)
    assert case.active == set()
    assert case.module._resolve_parent_fd(case.root_fd, 'file.json', create_dirs=False) == (case.root_fd, 'file.json')


# LLM: 打开后续目录失败、nofollow拒绝和异步中断都不能漏掉已打开的父FD，也不能关闭借入的根。
# 函数用途: 验证多级路径三类异常分支的FD所有权。
@pytest.mark.parametrize('failure', ['missing', 'symlink', 'interrupt'])
def test_planner_memo_parent_fd_failure_closes_intermediates(planner_directory_fds, monkeypatch, failure):
    case = planner_directory_fds
    (case.root / 'a' / 'b').mkdir(parents=True)
    if failure == 'symlink':
        (case.root / 'elsewhere').mkdir()
        (case.root / 'a' / 'b' / 'c').symlink_to(case.root / 'elsewhere', target_is_directory=True)
    if failure == 'interrupt':
        original = case.module.os.open

        # LLM: 在第三次open之前确定性中断，前两层仍由真实内核打开，不把stub计数当FD回收。
        # 函数用途: 注入非普通Exception中断以核对资源清理。
        def interrupted(path, *args, **kwargs):
            if path == 'c':
                raise KeyboardInterrupt('directory probe interrupted')
            return original(path, *args, **kwargs)

    error = KeyboardInterrupt if failure == 'interrupt' else (OSError, case.module.RuntimeConflictError)
    with monkeypatch.context() as patch:
        if failure == 'interrupt':
            patch.setattr(case.module.os, 'open', interrupted)
        with pytest.raises(error):
            case.module._resolve_parent_fd(case.root_fd, 'a/b/c/file.json', create_dirs=False)
    assert case.active == set()
    assert os.fstat(case.root_fd).st_ino == case.root.stat().st_ino


# LLM: 任务明确要求handled_fp非空才能复用；缺失不能与权限/解析失败混成一个可缓存空版本。
# 函数用途: 锁住无处理文件时保守重建、建文件后即时失效并恢复热命中的边界。
def test_planner_memo_missing_handled_keeps_authoritative_fallback(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.models import ObservationEvent

    store = ConversationStore(tmp_path / 'conversations')
    _seed_observations(store.storage.root, 'thread-1', [_observation_row('event')])
    calls = _planner_model_spy(monkeypatch, ObservationEvent, 'from_dict')
    for _ in range(2):
        assert [item.observation_id for item in store.observations.unhandled_requiring_main()] == ['event']
    assert len(calls) == 2
    assert store.observations._handled_cache_fp is None
    store.observations.mark_handled(['event'], now=10)
    assert store.observations.unhandled_requiring_main() == []
    assert store.observations.unhandled_requiring_main() == []
    assert len(calls) == 3


# LLM: 读前后版本不同时不能把旧handled映射关联到新版本，避免后续热拍错误地保留已处理事件。
# 函数用途: 用真实原读取器加窗口内改写验证处理回执只发布稳定快照。
def test_planner_memo_handled_changed_during_read_is_not_cached(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_observations as module

    store = ConversationStore(tmp_path / 'conversations')
    _seed_observations(store.storage.root, 'thread-1', [_observation_row('event')])
    path = store.storage.observation_handled_path
    path.write_text('{}', encoding='utf-8')
    original = module.read_json_file_report
    reads = []

    # LLM: 在真实读取结束后、产品取第二次指纹前改写文件，保留原返回而制造真实版本窗口。
    # 函数用途: 确定性模拟同时到来的处理确认。
    def changing(target):
        report = original(target)
        reads.append(target)
        if len(reads) == 1:
            target.write_text('{"event":10}', encoding='utf-8')
        return report

    monkeypatch.setattr(module, 'read_json_file_report', changing)
    assert len(store.observations.unhandled_requiring_main()) == 1
    assert store.observations._handled_cache_fp is None
    assert store.observations.unhandled_requiring_main() == []
    assert store.observations.unhandled_requiring_main() == []
    assert len(reads) == 2


# LLM: 每种对象memo单独缩小预算验证边界，不改变生产索引预算；保留真实文件轮换和真实查询入口。
# 函数用途: 验证观察、策略和唤醒对象memo不会随历史路径无限增长。
@pytest.mark.parametrize('kind', ['observation', 'policy', 'wake'])
def test_planner_memo_model_caches_are_bounded(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.conversation import store_observations, store_progress, store_wakes

    store = ConversationStore(tmp_path / 'conversations')
    store.storage.observation_handled_path.write_text('{}', encoding='utf-8')
    specs = {
        'observation': (store_observations, '_SHARD_MAIN_EVENTS_CACHE_MAX_ENTRIES_COUNT', store.observations, '_shard_main_events_cache'),
        'policy': (store_progress, '_POLICY_OBJ_CACHE_MAX_ENTRIES_COUNT', store.progress, '_policy_obj_cache'),
        'wake': (store_wakes, '_WAKE_OBJ_CACHE_MAX_ENTRIES_COUNT', store.wakes, '_wake_obj_cache'),
    }
    module, limit_name, domain, cache_name = specs[kind]
    monkeypatch.setattr(module, limit_name, 2)
    cache = getattr(domain, cache_name)
    previous = None
    for position in range(4):
        if previous is not None:
            previous.unlink()
        name = f'{kind}-{position}'
        if kind == 'observation':
            previous = _seed_observations(store.storage.root, name, [_observation_row(name, thread_id=name)])
            assert len(domain.unhandled_requiring_main()) == 1
        elif kind == 'policy':
            previous = _seed_policy(store.storage.root, name)
            assert len(domain.list_report()[0]) == 1
        else:
            previous = _seed_wake(store.storage.root, 'urgent', name)
            assert len(domain.pending_report()[0]) == 1
        assert str(previous) in cache
        assert len(cache) <= 2
    assert len(cache) == 2


# LLM: 缩小输入LRU上限但不替代真实reader；refs轮换后旧key必须可淘汰，current仍由原测试探针每次检查。
# 函数用途: 验证输入文件缓存的条目上界。
def test_planner_memo_input_cache_is_bounded(planner_input_case, monkeypatch):
    case = planner_input_case
    monkeypatch.setattr(case.module, '_INPUT_DEP_RECORD_CACHE_MAX_ENTRIES_COUNT', 2)
    text = case.path.read_text()
    for position in range(4):
        input_id = f'input-{position}'
        path = case.module.input_dependency_path(case.agent, input_id)
        path.write_text(text, encoding='utf-8')
        case.link.input_dependency_refs = (input_id,)
        case.module.task_has_waiting_input(case.agent, 'task-one')
        assert len(case.module._INPUT_DEP_RECORD_CACHE) <= 2
    assert len(case.module._INPUT_DEP_RECORD_CACHE) == 2


# LLM: 协作缓存按全模块预算淘汰，不能因每秒重建Store累积文件版本；仍走真实open_case/list_cases。
# 函数用途: 验证跨Store协作对象缓存的硬上界。
def test_planner_memo_case_cache_is_bounded(tmp_path, monkeypatch):
    from agent_py_agent.agent.collaboration import store as module

    monkeypatch.setattr(module, '_CASE_FILE_CACHE_MAX_ENTRIES_COUNT', 2)
    store = module.CollaborationCaseStore(tmp_path / 'collaboration')
    for position in range(4):
        store.open_case({'thread_id': 'thread-one', 'title': str(position)})
    assert len(store.list_cases()) == 4
    assert len(module._CASE_FILE_CACHE) == 2
