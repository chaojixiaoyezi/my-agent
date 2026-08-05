from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.structured_output import (
    StructuredBatchRequest,
    collect_structured_batches,
    json_objects_from_text,
)


def test_json_objects_from_text_ignores_chatter_and_incomplete_braces() -> None:
    text = '说明 {not-json} 前缀 {"value": 1} 后缀 {"value": 2} 尾巴 {"cut":'

    assert json_objects_from_text(text) == [{"value": 1}, {"value": 2}]


def test_collect_structured_batches_retries_only_missing_rows() -> None:
    calls: list[list[str]] = []

    def generate(rows):
        keys = [row["id"] for row in rows]
        calls.append(keys)
        emitted = keys if len(calls) > 1 else keys[:-1]
        return SimpleNamespace(text=json.dumps({"items": [{"id": key} for key in emitted]}))

    def parse(text, expected):
        payload = json.loads(text)
        return {row["id"]: row for row in payload["items"] if row["id"] in expected}

    report = collect_structured_batches(
        StructuredBatchRequest(
            rows=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
            key_of=lambda row: row["id"],
            generate=generate,
            parse=parse,
        )
    )

    assert set(report.values) == {"a", "b", "c"}
    assert calls == [["a", "b", "c"], ["c"]]
    assert report.unresolved_keys == ()


def test_collect_structured_batches_splits_by_count_or_weight_without_dropping() -> None:
    calls: list[list[str]] = []

    def generate(rows):
        keys = [row["id"] for row in rows]
        calls.append(keys)
        return SimpleNamespace(
            text=json.dumps({"items": [{"id": key} for key in keys]})
        )

    def parse(text, expected):
        payload = json.loads(text)
        return {
            row["id"]: row
            for row in payload["items"]
            if row["id"] in expected
        }

    rows = [
        {"id": "a", "weight": 3},
        {"id": "b", "weight": 3},
        {"id": "c", "weight": 8},
        {"id": "d", "weight": 2},
    ]
    report = collect_structured_batches(
        StructuredBatchRequest(
            rows=rows,
            key_of=lambda row: row["id"],
            generate=generate,
            parse=parse,
            max_batch_items=3,
            max_batch_weight=6,
            weight_of=lambda row: row["weight"],
        )
    )

    assert set(report.values) == {"a", "b", "c", "d"}
    # a+b reaches the weight watermark.  c is overweight but is still emitted
    # intact as one row; d follows in its own batch.
    assert calls == [["a", "b"], ["c"], ["d"]]


def test_collect_structured_batches_runs_bounded_top_level_chunks_in_parallel() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def generate(rows):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return SimpleNamespace(
            text=json.dumps({"items": [{"id": row["id"]} for row in rows]})
        )

    def parse(text, expected):
        payload = json.loads(text)
        return {
            row["id"]: row
            for row in payload["items"]
            if row["id"] in expected
        }

    report = collect_structured_batches(
        StructuredBatchRequest(
            rows=[{"id": str(index)} for index in range(6)],
            key_of=lambda row: row["id"],
            generate=generate,
            parse=parse,
            max_batch_items=2,
            max_workers=3,
        )
    )

    assert list(report.values) == [str(index) for index in range(6)]
    assert report.calls == 3
    assert report.unresolved_keys == ()
    assert 2 <= peak <= 3


def test_collect_structured_batches_splits_only_opted_in_size_errors() -> None:
    calls: list[list[str]] = []

    def generate(rows):
        keys = [row["id"] for row in rows]
        calls.append(keys)
        if len(rows) > 2:
            raise RuntimeError("output too large")
        return SimpleNamespace(
            text=json.dumps({"items": [{"id": row["id"]} for row in rows]})
        )

    report = collect_structured_batches(
        StructuredBatchRequest(
            rows=[{"id": str(index)} for index in range(4)],
            key_of=lambda row: row["id"],
            generate=generate,
            parse=lambda text, expected: {
                row["id"]: row
                for row in json.loads(text)["items"]
                if row["id"] in expected
            },
            max_batch_items=4,
            should_split_exception=lambda exc: str(exc) == "output too large",
        )
    )

    assert calls == [["0", "1", "2", "3"], ["0", "1"], ["2", "3"]]
    assert list(report.values) == ["0", "1", "2", "3"]
    assert report.calls == 3
    assert report.split_retries == 1
    assert report.unresolved_keys == ()


def test_collect_structured_batches_does_not_swallow_unrelated_exception() -> None:
    with pytest.raises(RuntimeError, match="provider unavailable"):
        collect_structured_batches(
            StructuredBatchRequest(
                rows=[{"id": "a"}],
                key_of=lambda row: row["id"],
                generate=lambda _rows: (_ for _ in ()).throw(
                    RuntimeError("provider unavailable")
                ),
                parse=lambda _text, _expected: {},
                should_split_exception=lambda exc: "too large" in str(exc),
            )
        )
