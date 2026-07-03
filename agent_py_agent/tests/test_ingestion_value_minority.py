"""§9 漏斗 A/B 钉子:少数派取值抬升通道 + 追平流尾不收工。

测试方独立复测实锤(同 harness 换种子 1/30):
- 漏斗 A:真目标与诱饵【结构完全相同,只差结果端一个取值】→ 签名稀有度把两者一并
  处置;且稀有形状诱饵成群,单一名额池按序号平手把真目标挤进 overflow(7307 候选仅
  7/30 真目标)。修:字面取值字段的 (路径,取值) 窗口稀有 → 独立车道抬升,纯计数零语义。
- 漏斗 B:读游标追平 spool 写游标 ≠ 盯守完成;窗口未满不许收工。
"""

from __future__ import annotations

from agent.ingestion.config import IngestTuning, tuning_from_params
from agent.ingestion.engine import StreamDigestEngine, _is_literal_value_token
from agent.ingestion.watch_payloads import _attach_keep_watching_note, _candidate_row


def _tuning(**overrides) -> IngestTuning:
    base = {
        "window_seconds": 300,
        "bucket_seconds": 30,
        "rare_threshold": 3,
        "max_candidates_per_pull": 8,
        "value_rare_threshold": 3,
        "value_min_support": 32,
    }
    base.update(overrides)
    return IngestTuning(**base)


def _steady_decoys(start: int, count: int, *, established: bool = False) -> list[tuple[int, dict]]:
    # 高频同形状事件:user 高基数(会折叠成 s:*),结果端 established 取值区分真假。
    return [
        (start + i, {"kind": "login", "user": f"user-{(start + i) % 97}", "established": established})
        for i in range(count)
    ]


def test_minority_value_escalates_when_signature_is_common():
    """漏斗 A 核心:目标与海量诱饵共签名(user 折叠),只差 established 取值 → 取值通道抬升。"""
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    batch = _steady_decoys(400, 200)
    batch.insert(150, (9999, {"kind": "login", "user": "user-7", "established": True}))
    digest = engine.process(batch, now=1010.0)
    hits = [c for c in digest.candidates if c.event.get("established") is True]
    assert len(hits) == 1, [c.event for c in digest.candidates]
    hit = hits[0]
    assert hit.reason == "minority_field_value"
    assert hit.value_path == "established"
    assert hit.value_token == "b:T"
    assert hit.value_window_count <= 3
    assert hit.field_window_count >= 32


def test_value_lane_not_crowded_out_by_rare_shape_decoy_sea():
    """漏斗 A 挤出算术:每批都有一群稀有形状诱饵(计数全 1 平手占满形状名额),
    真目标(少数派取值)必须走独立车道,不被挤进 overflow。"""
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8, max_candidates_per_pull=4))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    batch = _steady_decoys(400, 100)
    # 一群"触发端像目标"的稀有形状诱饵:每个形状都独一无二,序号都在目标之前。
    for j in range(12):
        batch.insert(j, (500 + j, {"kind": f"weird-{j}", "probe": {"stage": j}, "established": False}))
    batch.append((9999, {"kind": "login", "user": "user-3", "established": True}))
    digest = engine.process(batch, now=1010.0)
    hits = [c for c in digest.candidates if c.event.get("established") is True]
    assert len(hits) == 1, "真目标被稀有形状诱饵挤出了候选批"
    assert hits[0].reason == "minority_field_value"


def test_majority_value_still_suppressed():
    """诱饵的多数派取值(established=false)不因取值通道泛滥:照旧被压。"""
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    digest = engine.process(_steady_decoys(400, 200), now=1010.0)
    assert digest.candidates == []
    assert digest.suppressed_total == 200


def test_value_channel_off_by_zero_threshold():
    """通道关闭(阈值 0)→ 不产生 minority_field_value 候选(本例签名恰好稀有,
    仍可经形状车道抬升——关闭取值通道只关取值通道,不改形状语义)。"""
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8, value_rare_threshold=0))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    batch = _steady_decoys(400, 100)
    batch.append((9999, {"kind": "login", "user": "user-3", "established": True}))
    digest = engine.process(batch, now=1010.0)
    assert [c for c in digest.candidates if c.reason == "minority_field_value"] == []


def test_value_counts_survive_snapshot_restore():
    """取值窗口账随引擎快照持久(跨进程收割/重启不清零)。"""
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    snapshot = engine.snapshot(now=1001.0)
    revived = StreamDigestEngine(_tuning(low_cardinality_limit=8))
    revived.restore(snapshot, now=1002.0)
    batch = [(9999, {"kind": "login", "user": "user-3", "established": True})]
    digest = revived.process(batch, now=1003.0)
    hits = [c for c in digest.candidates if c.reason == "minority_field_value"]
    assert len(hits) == 1, "restore 后字段样本量归零会让取值通道失灵"


def test_literal_value_token_predicate():
    assert _is_literal_value_token("b:T")
    assert _is_literal_value_token("s:captured")
    assert _is_literal_value_token("n:503")
    assert _is_literal_value_token("null")
    assert not _is_literal_value_token("s:*")
    assert not _is_literal_value_token("n:mono")
    assert not _is_literal_value_token("n:e2")
    assert not _is_literal_value_token("t:list")


def test_tuning_accepts_value_channel_params():
    tuning = tuning_from_params({"value_rare_threshold": "5", "value_min_support": "100",
                                 "value_max_candidates_per_pull": "12"})
    assert tuning.value_rare_threshold == 5
    assert tuning.value_min_support == 100
    assert tuning.value_max_candidates_per_pull == 12


def test_candidate_row_projects_minority_triage():
    engine = StreamDigestEngine(_tuning(low_cardinality_limit=8))
    engine.process(_steady_decoys(0, 400), now=1000.0)
    batch = [(9999, {"kind": "login", "user": "user-3", "established": True})]
    digest = engine.process(batch, now=1003.0)
    row = _candidate_row(digest.candidates[0])
    assert row["triage"]["reason"] == "minority_field_value"
    assert row["triage"]["minority_value"]["path"] == "established"


# ---- 漏斗 B:追平流尾 ≠ 盯守结束 ----


def test_keep_watching_note_when_window_incomplete():
    payload = {"watch": {"window_complete": False, "remaining_seconds": 1200.0}}
    _attach_keep_watching_note(payload)
    assert payload["keep_watching"] is True
    assert "1200" in payload["keep_watching_note"]


def test_no_keep_watching_note_when_window_complete_or_unwindowed():
    done = {"watch": {"window_complete": True, "remaining_seconds": 0.0}}
    _attach_keep_watching_note(done)
    assert "keep_watching" not in done
    unwindowed = {"watch": {"closed": False}}
    _attach_keep_watching_note(unwindowed)
    assert "keep_watching" not in unwindowed


# ---- 出站授权钉扎:收割线程跨调用长命,不能随调用窗口失去授权 ----
# 真机实锤(fleet5):allowed_private_hosts 每次工具调用临时热注入、返回即还原为空,
# 收割线程在调用窗口之外的拉流全被 NETWORK_PRIVATE_HOST_BLOCKED 拦——六路间歇断粮,
# 其一读游标冻结掉出源滚动缓冲=真丢数据。


def test_harvester_fetch_pins_grants_beyond_call_window():
    from types import SimpleNamespace

    from agent.ingestion.watch_tool import WatchStreamTool

    tool = WatchStreamTool(SimpleNamespace())
    tool.allowed_private_hosts = ("192.168.1.5",)  # 模拟调用层热注入在场
    pinned = tool._harvester_fetch()
    tool.allowed_private_hosts = ()  # 模拟调用返回后还原
    # 钉住的闭包仍带授权:安全闸判定用钉扎值,不读实例现值
    pin = tool._resolve_pin_with("http://192.168.1.5:8901/pull", ("192.168.1.5",), None)
    assert pin.error is None, "钉扎授权应放行点名的内网源"
    blocked = tool._resolve_pin_with("http://192.168.1.5:8901/pull", (), None)
    assert blocked.error is not None, "无授权仍必须被出站闸拦(闸本身不放松)"
    assert callable(pinned)


def test_harvester_fetch_respects_instance_override():
    from types import SimpleNamespace

    from agent.ingestion.watch_tool import WatchStreamTool

    tool = WatchStreamTool(SimpleNamespace())
    sentinel = lambda url: (True, {"items": []}, "")  # noqa: E731 - 测试替身
    tool._fetch_json = sentinel
    assert tool._harvester_fetch() is sentinel
