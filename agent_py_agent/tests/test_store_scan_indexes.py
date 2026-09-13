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
from agent_py_agent.agent.conversation import store as store_module
from agent_py_agent.agent.conversation.store import (
    _SCAN_INDEX_MAX_RECORDS_PER_FILE,
    _SCAN_INDEX_SCHEMA,
)


# LLM: 快照函数把三类查询的返回压成可比对的纯数据(信号 + load_error 集合),三态一致性
# 断言只依赖这些快照;改动查询语义时这里必须同步,否则验收会失去意义。
# 函数用途: 取一次唤醒查询的可比对快照(信号字典 + 结构化错误列表)。
def _wake_snapshot(
    store: ConversationStore, *, limit: int = 0, include_normal: bool = True
) -> tuple[list[dict], list[dict]]:
    signals, errors = store.pending_wake_signals_report(
        limit=limit, include_normal=include_normal
    )
    return [item.to_dict() for item in signals], list(errors)


# 函数用途: 取一次观察查询的可比对快照。
def _observation_snapshot(store: ConversationStore, *, limit: int = 0) -> list[dict]:
    return [item.to_dict() for item in store.unhandled_observations_requiring_main(limit=limit)]


# 函数用途: 取一次策略查询的可比对快照(策略字典 + 结构化错误列表)。
def _policy_snapshot(
    store: ConversationStore, *, enabled_only: bool = False
) -> tuple[list[dict], list[dict]]:
    policies, errors = store.list_progress_policies_report(enabled_only=enabled_only)
    return [item.to_dict() for item in policies], list(errors)


# 函数用途: 取一次到期策略查询的可比对快照。
def _due_snapshot(store: ConversationStore, *, now: float = 10_000.0) -> list[dict]:
    return [item.to_dict() for item in store.due_progress_policies_report(now=now)[0]]


def _thread(store: ConversationStore, suffix: str = "1"):
    return store.get_or_create_thread(
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
        index = store._scan_index(name)  # noqa: SLF001 - 验收需要直接操作索引状态
        index.max_entries = 0
        index.clear()


# LLM: 有界性验收需要把上限压到人能数清的规模(默认 8192 条无法在测试里造出来),因此统一
# 通过这个 helper 打开热 store 并设定 wake 两个队列的条目上限;条目上界是唯一被改的东西,
# 查询语义与生产默认完全一致。
# 函数用途: 打开一个把唤醒队列索引条目上限压到 max_entries 的热 store。
def _bounded_wake_store(root: Path, max_entries: int) -> ConversationStore:
    store = ConversationStore(root)
    for kind in ("urgent", "normal"):
        store._scan_index(f"wake:{kind}").max_entries = max_entries  # noqa: SLF001
    return store


# 函数用途: 读一个索引的 entries/records/evictions/prunes 诊断快照(有界性断言的统一入口)。
def _index_stats(store: ConversationStore, name: str) -> dict:
    return store._scan_index(name).stats()  # noqa: SLF001 - 验收需要直接读索引统计


# LLM: 四种损坏形态分别对应"索引文件版本不符/索引被清空/条目指纹过期/条目主键被污染",
# 覆盖索引可能失效的全部现实入口;每种都必须回退权威读取并给出完全一致的结果。
# 函数用途: 按模式损坏指定索引,返回被损坏的索引对象。
def _corrupt_index(store: ConversationStore, name: str, mode: str):
    index = store._scan_index(name)  # noqa: SLF001 - 验收需要直接操作索引状态
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
        for item in disabled._scan_index("wake:urgent").warnings  # noqa: SLF001
    )

    # 热索引: 复用已缓存投影。
    warm = ConversationStore(root)
    assert _wake_snapshot(warm) == baseline_all
    before_warnings = list(warm._scan_index("wake:urgent").warnings)  # noqa: SLF001
    assert _wake_snapshot(warm) == baseline_all
    assert _wake_snapshot(warm, limit=3) == baseline_capped
    assert _wake_snapshot(warm, include_normal=False) == baseline_urgent
    # 健康索引不该产生任何"不可用"告警。
    assert warm._scan_index("wake:urgent").warnings == before_warnings == []  # noqa: SLF001
    stats = warm._scan_index("wake:urgent").stats()  # noqa: SLF001
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
            warnings = broken._scan_index("wake:urgent").warnings  # noqa: SLF001
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

    policies, errors = warm.list_progress_policies_report(enabled_only=True)
    assert errors
    errors[0]["message"] = "被消费方改写的策略错误"
    repeated, repeated_errors = warm.list_progress_policies_report(enabled_only=True)
    assert repeated_errors == ConversationStore(root).list_progress_policies_report(
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
        for item in disabled._scan_index("observations").warnings  # noqa: SLF001
    )

    warm = ConversationStore(root)
    assert _observation_snapshot(warm) == baseline_all
    assert _observation_snapshot(warm) == baseline_all
    assert _observation_snapshot(warm, limit=2) == baseline_capped
    assert warm._scan_index("observations").warnings == []  # noqa: SLF001

    for mode in CORRUPTION_MODES:
        broken = ConversationStore(root)
        assert _observation_snapshot(broken) == baseline_all
        _corrupt_index(broken, "observations", mode)
        assert _observation_snapshot(broken) == baseline_all, mode
        assert _observation_snapshot(broken, limit=2) == baseline_capped, mode

    assert _tree_signature(root) == cold_before


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
    # 旧实现每个分片都要重读一次 observation_handled.json(夹具里 5 个分片 = 5 次);
    # 现在整轮只读 1 次,干净分片在热索引下不再读取。带脏行的 thread-dirty.jsonl 必须
    # 每轮回权威读取(脏快照不入索引),这是"索引绝不固化解析失败"的直接证据。
    assert reads == ["observation_handled.json", "thread-dirty.jsonl"], reads


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
    assert warm._scan_index("progress_policies").warnings == []  # noqa: SLF001

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

    signal = store.raise_wake_signal(
        {"thread_id": thread.thread_id, "reason": "urgent_child_event", "now": 20.0}
    )
    assert [item.wake_signal_id for item in store.pending_wake_signals(limit=0)] == [
        signal.wake_signal_id
    ]
    # 原地元数据更新(原子替换)必须被看见,不能被索引挡住。
    cached = store.cache_pending_wake_delivery(signal.wake_signal_id, {"prompt": "owner payload"})
    assert cached is not None
    assert cached.metadata["owner_delivery"] == {"prompt": "owner payload"}
    assert store.pending_wake_signals(limit=0)[0].metadata["owner_delivery"] == {
        "prompt": "owner payload"
    }
    store.mark_wake_signal_handled(signal.wake_signal_id, now=30.0)
    assert store.pending_wake_signals(limit=0) == []

    policy = store.set_progress_policy(
        {"thread_id": thread.thread_id, "interval_seconds": 300, "now": 1_000.0}
    )
    assert policy.policy_id in [
        item.policy_id for item in store.list_progress_policies(enabled_only=True)
    ]
    store.mark_progress_failed(
        policy.policy_id, now=1_100.0, backoff_seconds=60.0, failure_count=3
    )
    assert policy.policy_id not in [
        item.policy_id for item in store.list_progress_policies(enabled_only=True)
    ]
    assert policy.policy_id in [item.policy_id for item in store.list_progress_policies()]

    observation = store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "child_agent_event",
            "summary": "需要主代理判断",
            "requires_main_agent": True,
            "now": 40.0,
        }
    )
    assert observation.observation_id in [
        item.observation_id for item in store.unhandled_observations_requiring_main(limit=0)
    ]
    store.mark_observations_handled([observation.observation_id], now=50.0)
    assert observation.observation_id not in [
        item.observation_id for item in store.unhandled_observations_requiring_main(limit=0)
    ]


def test_scan_index_stays_bounded_and_reports_structured_warnings(tmp_path) -> None:
    root = _seed_all_ledgers(tmp_path / "conversations")
    store = ConversationStore(root)

    index = store._scan_index("wake:urgent")  # noqa: SLF001
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
    budget_index = budget._scan_index("observations")  # noqa: SLF001
    assert budget_index.max_entries >= 1
    _seed_observations(
        root,
        "thread-huge",
        [
            _observation_row(f"obs-huge-{position:05d}", thread_id="thread-huge")
            for position in range(_SCAN_INDEX_MAX_RECORDS_PER_FILE + 10)
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
    queue = store.wake_queue_dir / "urgent"

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
    retained = set(store._scan_index("wake:urgent")._entries)  # noqa: SLF001
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
    assert set(store._scan_index("wake:urgent")._entries) == {  # noqa: SLF001
        survivor.name
    }


def test_scan_index_lru_evicts_oldest_when_one_scan_exceeds_limit(tmp_path) -> None:
    """累计名字超过上限: 单轮写入超过上限时按 LRU 淘汰,查询结果不受影响。"""
    root = tmp_path / "conversations"
    limit = 4
    store = ConversationStore(root)
    index = store._scan_index("wake:urgent")  # noqa: SLF001
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
    index = store._scan_index("wake:urgent")  # noqa: SLF001
    before = set(index._entries)
    assert before == {f"wake-kept-{position}.json" for position in range(3)}

    # 模拟"目录暂时读不到"(权限抖动/IO 故障),区别于"目录为空"。CPython 的 os.scandir 是
    # C 槽位、改不动,所以这里直接对目标目录让生产用的 listing 助手返回 None(它只在
    # OSError 分支返回 None),从而确定性地走"不可列出"状态机。
    real_match_names = store_module._scan_dir_match_names  # noqa: SLF001
    real_glob = Path.glob
    blocked = store.wake_queue_dir / "urgent"

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
    queue = store.wake_queue_dir / "urgent"

    for position in range(limit - 1):
        _seed_wake(root, "urgent", f"wake-live-{position}", created_at=1.0 + position)
    # 目录里另有 4 个不匹配 *.json 的邻居: 它们不是本轮要扫的目标,不得计入上界。
    for position in range(4):
        (queue / f"wake-noise-{position}.json.bak").write_text("{}", encoding="utf-8")
    index = store._scan_index("wake:urgent")  # noqa: SLF001
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
    index = store._scan_index("wake:urgent")  # noqa: SLF001
    assert index.schema == _SCAN_INDEX_SCHEMA
    assert index.stats()["schema"] == _SCAN_INDEX_SCHEMA


def test_ordered_scan_keeps_path_sort_order_for_tricky_names(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    queue = store.wake_queue_dir / "urgent"
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

    index = store._scan_index("wake:urgent")  # noqa: SLF001
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
                store.append_observation(
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
                for item in store.unhandled_observations_requiring_main(limit=0)
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
    final = [item.observation_id for item in store.unhandled_observations_requiring_main(limit=0)]
    assert len(final) == len(set(final)) == total, "并发写入后丢失或重复了观察事件"
    assert final == [
        item.observation_id
        for item in ConversationStore(root).unhandled_observations_requiring_main(limit=0)
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
                store.raise_wake_signal(
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
            signals, load_errors = store.pending_wake_signals_report(limit=0)
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
    final, load_errors = store.pending_wake_signals_report(limit=0)
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
                store.set_progress_policy(
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
            policies, load_errors = store.list_progress_policies_report()
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
    policies, load_errors = store.list_progress_policies_report()
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
