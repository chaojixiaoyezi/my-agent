"""有界事件删除的积压由原分页游标逐拍排空，不等待下一次全库轮转。"""
import time

import pytest

from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.user_space import owner_maintenance as om


@pytest.mark.parametrize("event_count", [40, 39])
def test_capped_event_steps_keep_store_pending_until_backlog_drained(tmp_path, monkeypatch, event_count):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 10)
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_MAX_DELETE", 5)
    store = LocalStore(tmp_path / "local_store" / "local.db")
    payload = {"padding": "x" * (128 * 1024)}
    for _ in range(event_count):
        store.record_event("backlog", payload=payload)
    assert store.stats()["event_count"] == event_count
    # 活动 JSONL 不被分步裁剪，足够大时每拍都沿真实有界 SQL 路径继续。
    assert store.events_path.stat().st_size > 4 * 1024 * 1024
    now, cursor, deleted = time.time(), None, 0
    reference = str(store.db_path.relative_to(tmp_path))

    for tick in range(20):
        selected, position = om._maintenance_store_page([store.db_path], tmp_path, cursor)
        assert selected == [store.db_path]
        result = om._maintain_event_store(store.root, now=now)
        assert result["executed"] is True
        if tick == 0:
            assert result["deleted_db"] == 5
            assert result["partial"] is True
        assert 0 <= result["deleted_db"] <= 5
        deleted += result["deleted_db"]
        page = om._maintenance_store_position(selected, tmp_path, [result], position)
        if result.get("partial"):
            assert result["selected"] == 5
            assert page["partial"] is True
            assert position["pending"] == [reference]
            cursor = page["cursor"]
        else:
            assert result["selected"] < 5
            assert not page.get("partial")
            assert position["pending"] == []
            break
    else:
        pytest.fail("events backlog did not drain within twenty bounded steps")

    assert deleted == event_count - 10
    assert store.stats()["event_count"] == 10
