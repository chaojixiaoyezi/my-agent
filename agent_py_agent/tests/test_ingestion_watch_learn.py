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


class _StatusSource(_FakeSource):
    """低基数 status 源(§11.2 盯守窗口频次校验用):ok 高频常态、fail 稀疏、defect 从不出现。"""

    def feed_status(self, count: int) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            self.events.append({"seq": seq, "status": "fail" if seq % 10 == 0 else "ok"})


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


def test_configure_rejects_sample_high_frequency_target(owner_home):
    """§7.1 拒错闸:sample 缓存取值分布后,把样本高频取值(≈常态)配成 target 即拒
    (精确值经首记号聚合匹配整句、contains 子串同拒);样本没出现的记号照常放行。"""
    source = _FakeSource()
    source.feed_messagey(400)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    wid = opened["watch_id"]
    _payload(tool.execute({"action": "sample", "watch_id": wid, "sample_count": 300}))
    cached = ws.load_state(Path(owner_home), wid).last_sample_digest  # 分布已随快照落盘
    assert cached["fields"]["note"]["events"] == 300

    rejected = tool.execute(
        {"action": "configure", "watch_id": wid, "spec": {"result_field": "note", "target_values": ["pass"]}}
    )
    assert not rejected.ok and rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "常态" in rejected.output and "normal_value" in rejected.output
    rejected_contains = tool.execute(
        {"action": "configure", "watch_id": wid, "spec": {"result_field": "note", "target_value_contains": ["rework"]}}
    )
    assert not rejected_contains.ok
    # 样本里 0 次的记号配 target → 放行;normal_* 判据不受频次校验
    assert tool.execute(
        {"action": "configure", "watch_id": wid, "spec": {"result_field": "note", "target_values": ["defect"]}}
    ).ok
    assert tool.execute({"action": "configure", "watch_id": wid, "spec": _SPEC}).ok


def test_configure_target_without_sample_evidence_not_rejected(owner_home):
    """没 sample 过=没有频次证据 → 不拒(任务/源信封明确点名 target 的合法场景)。"""
    source = _FakeSource()
    source.feed_messagey(100)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    result = tool.execute(
        {"action": "configure", "watch_id": opened["watch_id"], "spec": {"result_field": "note", "target_values": ["pass"]}}
    )
    assert result.ok, result.output


def test_configure_rejects_window_high_frequency_target_without_sample(owner_home):
    """§11.2:没 sample 过、但历轮 pull 的盯守窗口已显示某取值是常态高频 → (重)configure
    把它配成 target 照样被结构拦下(治"配反→洪泛误报→重 configure 仍配同一个"的复发环)。"""
    source = _StatusSource()
    source.feed_status(300)
    tool = _tool(owner_home, source)
    wid = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))["watch_id"]
    # 先配常态判据 + pull 若干轮把盯守窗口喂热(ok≈90% 常态高频)。
    _payload(tool.execute({"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "normal_values": ["ok", "fail"]}}))
    for _ in range(8):
        _payload(tool.execute({"action": "pull", "watch_id": wid}))
    # 不 sample、直接把常态 ok 配成 target → 盯守窗口频次校验拦下(样本证据这路是空的)。
    rejected = tool.execute({"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "target_values": ["ok"]}})
    assert not rejected.ok and rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "盯守窗口" in rejected.output and "normal_value" in rejected.output


def test_configure_window_guard_never_hurts_sparse_or_cold_start(owner_home):
    """§11.2 回归护栏:真稀疏目标绝不误伤。① 窗口没热身过(零 pull)配 target → 放行;
    ② 窗口热了、但配的是窗口里稀有/从没出现的取值(真目标)→ 放行。宁可漏拦不误杀。"""
    source = _StatusSource()
    source.feed_status(300)
    tool = _tool(owner_home, source)
    wid = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))["watch_id"]
    # ① 冷启动:没 pull 过 → 窗口无证据 → 点名 target 放行(第一枪不硬拦,靠重配兜)。
    assert tool.execute({"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "target_values": ["ok"]}}).ok
    for _ in range(8):
        _payload(tool.execute({"action": "pull", "watch_id": wid}))
    # ② 稀疏真目标(窗口里从没出现的 defect)→ 放行(不被窗口频次校验误杀)。
    assert tool.execute({"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "target_values": ["defect"]}}).ok


def test_sample_digest_survives_restart_and_still_rejects(owner_home):
    """sample 分布随 watch 持久化:重启/补岗后 configure 频次校验照样生效。"""
    source = _FakeSource()
    source.feed_messagey(300)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "sample", "watch_id": opened["watch_id"], "sample_count": 200}))

    ws.registry = ws.WatchRegistry()  # 模拟重启
    tool2 = _tool(owner_home, source)
    reopened = _payload(tool2.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert reopened["resumed_existing_watch"] is True
    rejected = tool2.execute(
        {"action": "configure", "watch_id": reopened["watch_id"], "spec": {"result_field": "note", "target_values": ["pass"]}}
    )
    assert not rejected.ok and rejected.error_code == "TOOL_INVALID_ARGUMENTS"


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


def test_blind_inverted_configure_flood_cut_by_runtime_immunity(owner_home):
    """P2 u-2hb 全形态端到端:第一枪全盲配反(零 sample+冷窗口,两道 configure 闸按
    "无证据不定罪"如实放行)→ 旧行为引擎照判据整批抬常态、模型照报(2h 26 误报);
    现在运行时配反免疫在支持度热身后按常态压组断源,pull payload 结构化告警指引重配。"""
    source = _StatusSource()
    source.feed_status(300)
    tool = _tool(owner_home, source)
    wid = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))["watch_id"]

    # 双盲区入口如实存在:不 sample、窗口没热(还没 pull 过)→ 配反闸无证据放行。
    blind = tool.execute(
        {"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "target_values": ["ok"]}}
    )
    assert blind.ok

    first = _payload(tool.execute({"action": "pull", "watch_id": wid}))
    first_lifted = sum(1 for row in first["candidates"] if row["triage"]["reason"] == "spec_target_value")
    # 300 条里 ok≈90%:支持度(64)热身一过,免疫接管——本批抬升只剩热身期漏进车道的零头。
    assert first_lifted <= 8
    assert first.get("spec_target_common_suppressed"), first.keys()
    alert = first["spec_target_common_suppressed"][0]
    assert alert["path"] == "status" and alert["value"] == "ok"
    assert "配反" in first.get("spec_target_common_note", "")
    assert first["engine_totals"]["spec_target_suppressed"] > 100  # 洪泛在引擎侧被成批压掉

    # 续流再拉:免疫稳定在岗,配反 target 一条不再抬,告警持续在场直到模型重配。
    source.feed_status(100)
    second = _payload(tool.execute({"action": "pull", "watch_id": wid}))
    assert not [row for row in second["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert second.get("spec_target_common_suppressed")

    # 出口(护栏):按告警指引重配为真·稀疏目标 defect(窗口 0 次,configure 双保险放行)
    # → 命中照常逐条抬升、零告警——免疫只掐"≈常态"的取值,稀疏目标一根汗毛不动。
    fixed = tool.execute(
        {"action": "configure", "watch_id": wid, "spec": {"result_field": "status", "target_values": ["defect"]}}
    )
    assert fixed.ok
    source.feed_status(100)
    base = len(source.events)
    source.events.append({"seq": base, "status": "defect"})
    source.events.append({"seq": base + 1, "status": "defect"})
    third = _payload(tool.execute({"action": "pull", "watch_id": wid}))
    hits = [row for row in third["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert len(hits) == 2  # 两条 defect 全抬升
    assert not third.get("spec_target_common_suppressed")
