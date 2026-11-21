import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_py_agent.tests.test_clfix_curator_daily_finalize_progress import _daily_case


@pytest.mark.parametrize("message_limit,max_chars,preview_size", [(1, 20000, 4), (20, 9000, 950)])
def test_daily_finalize_drains_bounded_audit_pages(tmp_path, message_limit, max_chars, preview_size):
    service, backend, _, _, _ = _daily_case(tmp_path, 0)
    service.config = replace(service.config, batch_message_limit=message_limit, max_input_chars=max_chars)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    root = tmp_path / "audit"
    root.mkdir()
    events = [f"event-{index}" for index in range(8)]
    (root / "2026-11-20.jsonl").write_text("".join(json.dumps({
        "event_id": event, "created_at": now.isoformat(), "preview": "x" * preview_size,
    }) + "\n" for event in events), encoding="utf-8")

    first = service.run(reason="daily_finalize", now=now, scheduled=True)

    assert first.status == "succeeded"
    assert 0 < first.processed_audit_events < len(events)
    assert service.state_store.load().last_daily_finalize_date == ""
    for _ in range(len(events)):
        if service.state_store.load().last_daily_finalize_date:
            break
        assert service.run(reason="daily_finalize", now=now, scheduled=True).status == "succeeded"
    assert [event for batch in backend.seen for event in batch] == events
    assert service.state_store.load().last_daily_finalize_date == now.date().isoformat()


def test_later_message_cannot_settle_an_incomplete_filtered_audit_page(tmp_path):
    from datetime import timedelta

    from agent_py_agent.tests.test_conveyor_m64_curator_catchup_and_consumption import _append_input

    service, backend, store, thread, _ = _daily_case(tmp_path, 0)
    service.config = replace(service.config, batch_message_limit=1)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    _append_input(store, thread, now + timedelta(days=1), 1)
    root = tmp_path / "audit"
    root.mkdir()
    (root / "2026-11-20.jsonl").write_text("".join(json.dumps({
        "event_id": event, "created_at": when.isoformat(),
    }) + "\n" for event, when in (("old", now - timedelta(hours=1)), ("eligible", now))), encoding="utf-8")

    outcome = service.run(reason="daily_finalize", now=now, scheduled=True)

    assert outcome.status == "succeeded"
    state = service.state_store.load()
    assert state.last_processed_audit_event_id == "old"
    assert state.last_daily_finalize_date == ""
    assert not state.per_thread_cursors
    for _ in range(2):
        assert service.run(reason="daily_finalize", now=now, scheduled=True).status == "succeeded"
    assert backend.seen == [["eligible"]]
    assert service.state_store.load().last_daily_finalize_date == now.date().isoformat()
