"""watch_stream 学判据层:sample 字段分布 / configure 灌 spec / 持久化回环 / 非法 spec。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.ingestion import watch_state as ws
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed_messagey(self, count: int, target_at: int = -1) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            token = "defect" if seq == target_at else ("pass" if seq % 4 else "rework")
            self.events.append({"seq": seq, "junk": f"u-{seq:08d}", "note": f"{token} ref={seq:010d}"})

    def handle(self, url: str) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, source: _FakeSource) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool

def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


_SPEC = {"result_field": "note", "normal_value_contains": ["pass", "rework"], "ignore_fields": ["junk"]}


def test_sample_returns_raw_events_and_counted_field_digest(owner_home):
    source = _FakeSource()
    source.feed_messagey(400)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert "先 action=sample" in opened["guidance"]  # 未配 spec 的 open 引导学判据

    sampled = _payload(tool.execute({"action": "sample", "watch_id": opened["watch_id"], "sample_count": 300}))
    assert sampled["sampled_events"] == 300
    assert sampled["raw_events"] and sampled["current_spec"] is None
    digest = sampled["field_digest"]
    # 高基数噪声字段的结构信号:distinct 顶到跟踪帽;低基数字段列出 top 取值
    assert str(digest["junk"]["distinct_values"]).endswith("+")
    assert digest["seq"]["events"] == 300
    # 游标未被取样动过
    status = _payload(tool.execute({"action": "status", "watch_id": opened["watch_id"]}))
    assert status["coverage"]["cursor"] == 0


def test_configure_applies_spec_and_pull_uses_spec_lane(owner_home):
    source = _FakeSource()
    source.feed_messagey(300)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    configured = _payload(tool.execute({"action": "configure", "watch_id": opened["watch_id"], "spec": _SPEC}))
    assert configured["spec"]["result_field"] == "note"

    source.feed_messagey(200, target_at=390)
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    assert pulled["source_spec_configured"] is True
    spec_rows = [row for row in pulled["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert len(spec_rows) == 1
    assert spec_rows[0]["triage"]["spec_match"]["mode"] == "outside_normal"
    assert "defect" in spec_rows[0]["triage"]["spec_match"]["value"]
    assert pulled["engine_totals"]["escalated_spec_target"] == 1

    status = _payload(tool.execute({"action": "status", "watch_id": opened["watch_id"]}))
    assert status["source_spec"]["result_field"] == "note"


def test_spec_survives_process_restart(owner_home):
    source = _FakeSource()
    source.feed_messagey(200)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "configure", "watch_id": opened["watch_id"], "spec": _SPEC}))

    ws.registry = ws.WatchRegistry()  # 模拟重启
    source.feed_messagey(150, target_at=260)
    tool2 = _tool(owner_home, source)
    reopened = _payload(tool2.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert reopened["resumed_existing_watch"] is True
    assert reopened["source_spec"]["result_field"] == "note"  # spec 随盘复活
    pulled = _payload(tool2.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    spec_rows = [row for row in pulled["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert [row["stream_pos"] for row in spec_rows] == [260]


def test_configure_rejects_invalid_spec_with_reason(owner_home):
    source = _FakeSource()
    source.feed_messagey(50)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    result = tool.execute({"action": "configure", "watch_id": opened["watch_id"], "spec": {"result_field": "note"}})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "至少一种" in result.output
    # JSON 字符串形式的 spec 也接受(模型常把对象串化)
    ok_result = tool.execute(
        {"action": "configure", "watch_id": opened["watch_id"], "spec": json.dumps(_SPEC, ensure_ascii=False)}
    )
    assert ok_result.ok, ok_result.output


def test_audit_records_spec_lane_positions(owner_home):
    source = _FakeSource()
    source.feed_messagey(120, target_at=60)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "configure", "watch_id": opened["watch_id"], "spec": _SPEC}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    audit_path = ws.state_dir(Path(owner_home)) / f"{opened['watch_id']}.audit.ndjson"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert any(60 in row.get("escalated_spec_pos", []) for row in rows)


if __name__ == "__main__":
    import unittest

    unittest.main()
