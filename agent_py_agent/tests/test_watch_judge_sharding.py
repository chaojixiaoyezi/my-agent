"""判读并发/横向扩(P1 头号):一路 spool 被 K 个判读工按记录取模分片并行消费。

钉死的契约:
1. 分片划分不重不漏:K 个判读工各认领 spool_seq%K==index 的记录,合起来=全体、无交叠;
2. 每个分片独立在途/ack:一个分片换人重投不影响别的分片;
3. 全局未判账合计:candidates_unjudged 按全分片 ack 合计(过载/唤醒兜底看整路而非单片);
4. 动态工数:recommended_judge_workers 随积压涨、随判完落,钳到 max_judge_workers;
5. shard_count=1 与旧单消费者路逐值等价(既有 test_watch_spool_takeover 已覆盖,这里再钉一条)。

铁律:分片是纯结构入参(按 spool_seq 取模),不碰候选真假——每个判读工照样把自己分片的
候选逐条递给模型判。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.wake_backstop import lane_unjudged_backlog
from agent.ingestion.watch_state import list_states, new_state, persist_state


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int) -> None:
        base = len(self.events)
        for index in range(count):
            self.events.append({"seq": base + index, "kind": "beat", "note": f"n{base + index}"})

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
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


def _harvested_state(owner_home: Path, source: _FakeSource, count: int, chunk: int = 4):
    """喂 count 条进真收割管线,候选落 spool。content_mode(冷启动直通)每条都成候选;
    full_read_per_pull=chunk 把 spool 记录切小 → 多条记录,分片按 spool_seq 取模才分得开。"""
    state = new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": chunk})
    persist_state(state)
    source.feed(count)
    assert hv._harvest_cycle(state, source.handle)
    return state


def _stream_positions(records: list[dict]) -> set[int]:
    return {row["stream_pos"] for record in records for row in (record.get("candidates") or [])}


def _drain_shard(state, shard_index: int, shard_count: int, consumer: str) -> set[int]:
    """一个判读工把自己分片判到清空(每轮 ack 上一批),返回它判过的 stream_pos 集合。"""
    seen: set[int] = set()
    for _ in range(200):
        records, _bl = hv.read_spool_records(
            state, max_candidates=8, consumer=consumer, shard_index=shard_index, shard_count=shard_count
        )
        if not records:
            break
        seen |= _stream_positions(records)
    return seen


def test_shards_partition_records_disjointly_and_cover_all(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 40, chunk=2)  # ~20 条记录,分得开
    assert state.spool_seq >= 4
    k = 3
    shards = [_drain_shard(state, i, k, consumer=f"w{i}") for i in range(k)]
    # 不重叠:任意两分片交集为空
    for a in range(k):
        for b in range(a + 1, k):
            assert shards[a].isdisjoint(shards[b]), f"shard {a} 与 {b} 有交叠(双判)"
    # 全覆盖:并集 = 全体候选(引擎抬进 spool 的每条都恰好被一个判读工判过)
    all_positions = _drain_shard(state, 0, 1, consumer="solo-check")  # 单工全量口径做对照
    # solo-check 会把全体重投一遍(它是新消费者、独立游标),用它拿全集
    union = set().union(*shards)
    assert union == all_positions, "分片并集 ≠ 全体候选(有漏判)"


def test_each_shard_acks_independently(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 24, chunk=2)
    k = 2
    # 两个分片各取一批(挂在途未 ack)
    r0, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0", shard_index=0, shard_count=k)
    r1, _ = hv.read_spool_records(state, max_candidates=4, consumer="w1", shard_index=1, shard_count=k)
    assert r0 and r1
    c0 = hv.read_spool_cursor(state, 0, k)
    c1 = hv.read_spool_cursor(state, 1, k)
    assert c0["inflight"]["consumer"] == "w0"
    assert c1["inflight"]["consumer"] == "w1"
    # w0 再来取(ack 掉自己上一批),不该动 w1 的在途
    hv.read_spool_records(state, max_candidates=4, consumer="w0", shard_index=0, shard_count=k)
    assert hv.acked_candidates(hv.read_spool_cursor(state, 0, k)) > 0
    assert hv.acked_candidates(hv.read_spool_cursor(state, 1, k)) == 0  # w1 还没 ack


def test_global_unjudged_backlog_sums_across_shards(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 24, chunk=2)
    written = int(state.totals["spool_candidates"])
    assert written == 24
    k = 3
    # 每个分片取一批但都不 ack(全挂在途)→ 全局未判 = 全体(交付≠判完)
    for i in range(k):
        hv.read_spool_records(state, max_candidates=100, consumer=f"w{i}", shard_index=i, shard_count=k)
    persist_state(state)
    lane = list_states(owner_home)[0]
    # 唤醒兜底的未判账必须看得见全体(不因分了片就漏算)
    assert lane_unjudged_backlog(owner_home, lane) == written
    # 各分片都 ack(再取一次)→ 全局未判归零
    for i in range(k):
        hv.read_spool_records(state, max_candidates=100, consumer=f"w{i}", shard_index=i, shard_count=k)
    assert lane_unjudged_backlog(owner_home, list_states(owner_home)[0]) == 0


def test_recommended_workers_scales_with_backlog_and_caps(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 200, chunk=4)  # 深积压
    # judge_quota 默认 = max(max_candidates_per_pull=8, full_read_per_pull)。这里关了直通 → 8。
    quota = hv.judge_quota(state.tuning)
    written = int(state.totals["spool_candidates"])
    expected = min(int(getattr(state.tuning, "max_judge_workers")), (written + quota - 1) // quota)
    assert hv.recommended_judge_workers(state) == expected
    assert expected > 1  # 200 条深积压确实推荐多工
    # 判空后回落到 1(单工够了)
    _drain_shard(state, 0, 1, consumer="solo")
    assert hv.recommended_judge_workers(state) == 1


def test_shard_takeover_redelivers_within_shard(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 24, chunk=2)
    k = 2
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0", shard_index=0, shard_count=k)
    first_pos = _stream_positions(first)
    assert first_pos
    # w0 死了,继任者 w0b 接管【同一分片】:原样重投,不推进交付游标
    redelivered, backlog = hv.read_spool_records(
        state, max_candidates=4, consumer="w0b", shard_index=0, shard_count=k
    )
    assert _stream_positions(redelivered) == first_pos
    assert backlog["redelivered_candidates"] == len(first_pos)


def test_shard_count_one_is_identical_to_base_cursor(owner_home):
    """shard_count=1 用基座游标(与既有单消费者路同一把锁),不写分片 sidecar。"""
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 6, chunk=2)
    hv.read_spool_records(state, max_candidates=100, consumer="run-a", shard_index=0, shard_count=1)
    base = ws.state_dir(owner_home) / f"{state.watch_id}.read.json"
    assert base.exists()  # 基座游标(默认路径)
    assert not hv._shard_sidecar_glob(owner_home, state.watch_id)  # 没有分片 sidecar
