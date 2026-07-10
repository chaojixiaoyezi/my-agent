from __future__ import annotations

import json
from types import SimpleNamespace

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
