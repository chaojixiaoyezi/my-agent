"""T1 候选队列轮转单测:已研判归档+截断、游标平移、未研判不丢不错位。"""

from agent.tooling.log_ops.store import LogOpsStore, compact_candidates


def test_compact_keeps_unread_archives_judged_resets_cursor(tmp_path):
    store = LogOpsStore(tmp_path, "m1")
    store.ensure_dirs()
    store.append_candidates([{"id": i, "raw": f"evt{i}"} for i in range(100)])
    store.write_poll_cursor(60)  # 前 60 已研判
    result = compact_candidates(store)

    assert result == {"pruned": 60, "kept": 40}
    assert store.count_candidates() == 40       # 只留未研判
    assert store.read_poll_cursor() == 0        # 游标重置
    remaining = store.read_candidates(offset=0, limit=100)
    assert len(remaining) == 40
    assert remaining[0]["id"] == 60 and remaining[-1]["id"] == 99  # 未研判一条不丢
    archive = tmp_path / "m1" / "candidates.archive.jsonl"
    assert archive.exists() and sum(1 for _ in archive.open()) == 60  # 已研判归档


def test_compact_noop_when_cursor_zero(tmp_path):
    store = LogOpsStore(tmp_path, "m2")
    store.ensure_dirs()
    store.append_candidates([{"id": i} for i in range(10)])
    assert compact_candidates(store) == {"pruned": 0, "kept": 10}
    assert store.count_candidates() == 10  # 没研判过不动


def test_poll_continuity_after_compact(tmp_path):
    """轮转后继续 poll + daemon 又 append:从未研判处接着读,不重读已研判、不丢新候选。"""
    store = LogOpsStore(tmp_path, "m3")
    store.ensure_dirs()
    store.append_candidates([{"id": i} for i in range(50)])
    store.write_poll_cursor(30)
    compact_candidates(store)  # 留 [30,50)=20,cursor=0

    batch = store.read_candidates(offset=store.read_poll_cursor(), limit=10)
    assert [c["id"] for c in batch] == list(range(30, 40))  # 从原 30 接着读

    store.write_poll_cursor(10)                          # poll 消费了 10
    store.append_candidates([{"id": i} for i in range(50, 55)])  # daemon 新增 5
    batch2 = store.read_candidates(offset=store.read_poll_cursor(), limit=20)
    assert [c["id"] for c in batch2] == list(range(40, 55))  # 不重读不丢,游标对齐
