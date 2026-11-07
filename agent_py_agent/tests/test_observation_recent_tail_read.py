"""观测尾读与旧全账选择逐条等价，常规近期查询只读取尾部 ID。"""
from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_observations
from agent_py_agent.agent.conversation.models import ObservationEvent
from agent_py_agent.agent.conversation.store_io import read_jsonl_report
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.runtime_errors import runtime_error_report


@pytest.fixture
def observation_store(tmp_path):
    storage = ConversationStorage(tmp_path / "conversations", initialize=False)
    storage.observations_dir.mkdir(parents=True)
    return store_observations.ObservationStore(
        storage,
        require_thread=lambda thread_id: pytest.fail("读取不应加载线程"),
        update_thread_atomic=lambda *_args: pytest.fail("读取不应修改线程"),
    )


def _row(index, **kwargs):
    return ObservationEvent(
        observation_id=f"obs-{index}",
        thread_id="thread",
        event_type="observation",
        summary=f"观测 {index}\u0085\u2028\u2029",
        observed_at=float(index),
        **kwargs,
    ).to_dict()


def _full_report(store, thread_id="thread", *, limit=20, include_handled=True):
    """内联修复前的全账实现，独立解析事件作为对照。"""
    path = store.storage.observation_path(thread_id)
    if not path.exists():
        return [], []
    report = read_jsonl_report(path, context="conversation.observations.read")
    handled = store._read_handled([str(row.get("observation_id") or "") for row in report.rows])
    events, parse_errors = [], []
    for index, row in enumerate(report.rows, start=1):
        try:
            event = ObservationEvent.from_dict(row)
            handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
            events.append(replace(event, handled_at=handled_at))
        except Exception as exc:
            error = runtime_error_report(exc, context="conversation.observations.parse")
            error["row_index"] = index
            parse_errors.append(error)
    if not include_handled:
        events = [event for event in events if event.handled_at <= 0]
    return (events if limit <= 0 else events[-limit:]), [*report.load_errors, *parse_errors]


@pytest.mark.parametrize("case", range(200))
def test_seeded_dirty_files_match_full_read(observation_store, monkeypatch, case):
    rng = random.Random(20261107 + case)
    long_row = _row(1000)
    long_row["summary"] = "跨块长观测" * 15000
    lines = [
        json.dumps(long_row, ensure_ascii=False),
        "{" + "x" * 70000,
        "{broken",
        "",
        "   \t",
        json.dumps({**_row(1001), "observed_at": "invalid"}),
    ]
    handled = {}
    for index in range(rng.randrange(1, 100)):
        row = _row(index, handled_at=3.0 if rng.randrange(4) == 0 else 0.0)
        if rng.randrange(3) == 0:
            handled[row["observation_id"]] = 100.0 + index
        lines.append(rng.choice([
            json.dumps(row, ensure_ascii=False),
            json.dumps(row, ensure_ascii=False),
            "{truncated",
            "null",
            "[]",
            "",
        ]))
    rng.shuffle(lines)
    newline = "\r\n" if case % 2 else "\n"
    raw = newline.join(lines) + (newline if case % 3 else "")
    path = observation_store.storage.observation_path("thread")
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(
        observation_store, "_read_handled",
        lambda ids: {key: handled[key] for key in ids if key in handled},
    )
    for limit in (1, 5, 20):
        expected, expected_errors = _full_report(observation_store, limit=limit)
        actual, errors = observation_store.recent_report("thread", limit=limit)
        assert [event.to_dict() for event in actual] == [event.to_dict() for event in expected]
        # 有效尾窗已满时只报告该窗口；回退全账时保留原始行号和全部错误。
        if errors or len(actual) < limit:
            assert errors == expected_errors


@pytest.mark.parametrize("method", ["recent_report", "recent"])
def test_recent_5000_rows_never_reads_full_ledger(observation_store, monkeypatch, method):
    path = observation_store.storage.observation_path("thread")
    path.write_text("".join(json.dumps(_row(index)) + "\n" for index in range(5000)))
    requested_ids = []

    def handled(ids):
        requested_ids.append(list(ids))
        return {"obs-4999": 9000.0}

    monkeypatch.setattr(observation_store, "_read_handled", handled)
    monkeypatch.setattr(
        store_observations, "read_jsonl_report",
        lambda *_args, **_kwargs: pytest.fail("近期读取不得加载 5000 行全账"),
    )
    before = path.read_bytes()
    result = getattr(observation_store, method)("thread", limit=20)
    events, errors = result if method == "recent_report" else (result, [])
    expected_ids = [f"obs-{index}" for index in range(4980, 5000)]
    assert [event.observation_id for event in events] == expected_ids
    assert requested_ids == [expected_ids]
    assert events[-1].handled_at == 9000.0
    assert errors == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("limit", [1, 5, 20])
@pytest.mark.parametrize("suffix", [
    "{broken\n", "null\n", '{"observed_at":"invalid"}\n',
    "{" + "x" * 70000 + "\n", "\n" * 70000,
], ids=["bad-json", "non-object", "bad-event", "long-bad-line", "blank-block"])
def test_short_tail_falls_back_with_original_errors(observation_store, monkeypatch, limit, suffix):
    path = observation_store.storage.observation_path("thread")
    path.write_text("".join(json.dumps(_row(index)) + "\n" for index in range(40)) + suffix)
    monkeypatch.setattr(observation_store, "_read_handled", lambda ids: {})
    expected = _full_report(observation_store, limit=limit)
    reads = []

    def full_read(path, *, context):
        reads.append((path, context))
        return read_jsonl_report(path, context=context)

    monkeypatch.setattr(store_observations, "read_jsonl_report", full_read)
    assert observation_store.recent_report("thread", limit=limit) == expected
    assert reads == [(path, "conversation.observations.read")]


@pytest.mark.parametrize(("limit", "include_handled"), [
    (0, True), (-1, True), (1, False), (20, False), (0, False), (-1, False),
])
def test_unlimited_and_unhandled_branches_keep_full_read(
    observation_store, monkeypatch, limit, include_handled,
):
    path = observation_store.storage.observation_path("thread")
    path.write_text("{broken\n" + "".join(
        json.dumps(_row(index, handled_at=5.0 if index % 2 else 0.0)) + "\n"
        for index in range(40)
    ))
    monkeypatch.setattr(observation_store, "_read_handled", lambda ids: {"obs-38": 10.0})
    monkeypatch.setattr(
        store_observations, "read_jsonl_tail_report",
        lambda *_args, **_kwargs: pytest.fail("全量或未处理筛选分支不得尾读"),
    )
    assert observation_store.recent_report(
        "thread", limit=limit, include_handled=include_handled,
    ) == _full_report(observation_store, limit=limit, include_handled=include_handled)


def test_missing_file_does_not_read_ledgers(observation_store, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("文件缺失时不得读取观察或处理账本")

    monkeypatch.setattr(store_observations, "read_jsonl_report", unexpected)
    monkeypatch.setattr(store_observations, "read_jsonl_tail_report", unexpected)
    monkeypatch.setattr(observation_store, "_read_handled", unexpected)
    assert observation_store.recent_report("missing") == ([], [])


def test_tail_applies_persisted_handled_times_after_reopening(tmp_path):
    store = ConversationStore(tmp_path / "owner")
    thread = store.threads.get_or_create({
        "canonical_user_id": "user", "channel": "internal",
        "channel_conversation_id": "conversation", "channel_user_id": "user", "now": 1,
    })
    events = [store.observations.append({
        "thread_id": thread.thread_id, "summary": str(index), "now": index + 2,
    }) for index in range(30)]
    store.observations.mark_handled([events[0].observation_id, events[-1].observation_id], now=100)
    reopened = ConversationStore(store.storage.root).observations
    recent, errors = reopened.recent_report(thread.thread_id, limit=20)
    assert recent == [*events[-20:-1], replace(events[-1], handled_at=100)]
    assert errors == []
