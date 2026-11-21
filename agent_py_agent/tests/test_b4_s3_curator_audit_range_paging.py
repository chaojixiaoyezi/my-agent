import json
from dataclasses import replace
from datetime import datetime, timezone

from agent_py_agent.agent.memory_store.curator_models import CURATOR_REPLAY_BREAKER_FAILURE_COUNT
from agent_py_agent.tests.test_clfix_curator_daily_finalize_progress import _daily_case


def test_detached_audit_prefix_advances_under_tighter_budget(tmp_path):
    service, backend, _, _, _ = _daily_case(tmp_path, 0)
    service.config = replace(service.config, batch_message_limit=10, max_input_chars=20000)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    root = tmp_path / "audit"
    root.mkdir()
    path = root / "2026-11-20.jsonl"
    events = ["z-source", "a-source", "f-source", "b-source", "c-source"]
    lines = [json.dumps({"event_id": event, "created_at": now.isoformat(), "preview": "x" * 950}) + "\n"
             for event in [*events, "newer-than-range"]]
    path.write_text("".join(lines[:-1]), encoding="utf-8")
    backend.fails = lambda _: True
    for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT):
        assert service.run(reason="interval", now=now, scheduled=True).failure_code == "CURATOR_SCHEMA_INVALID"
    detached, = service.state_store.load().pending_local_dates
    assert detached["source_range"]["audit"] == events[-1]

    with path.open("a", encoding="utf-8") as handle:
        handle.write(lines[-1])
    service.config = replace(service.config, max_input_chars=9000)
    backend.fails = lambda _: False
    backend.seen.clear()
    for _ in events:
        state = service.state_store.load()
        assert state.last_processed_audit_event_id == events[-1]
        if not state.pending_local_dates:
            break
        row, = state.pending_local_dates
        outcome = service.run(reason="interval", now=now, scheduled=True, date_replay=row)
        assert outcome.status == "succeeded", outcome
    assert [event for batch in backend.seen for event in batch] == events
    assert service.state_store.load().pending_local_dates == []
    assert service.state_store.load().last_processed_audit_event_id == events[-1]


def test_replaced_frozen_endpoint_remains_unresolved(tmp_path):
    service, backend, _, _, _ = _daily_case(tmp_path, 0)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    root = tmp_path / "audit"
    root.mkdir()
    path = root / "2026-11-20.jsonl"
    path.write_text(json.dumps({"event_id": "frozen-end", "created_at": now.isoformat()}) + "\n",
                    encoding="utf-8")
    backend.fails = lambda _: True
    for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT):
        assert service.run(reason="interval", now=now, scheduled=True).failure_code == "CURATOR_SCHEMA_INVALID"
    state = service.state_store.load()
    detached, = state.pending_local_dates
    replacement = root / "replacement.tmp"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    backend.fails = lambda _: False
    backend.seen.clear()

    result = service.run(reason="interval", now=now, scheduled=True, date_replay=detached)

    assert result.failure_code == "CURATOR_INPUT_AUDIT_RANGE_ENDPOINT_UNRESOLVED"
    assert backend.seen == []
    after = service.state_store.load()
    assert after.last_processed_audit_event_id == state.last_processed_audit_event_id
    assert after.pending_local_dates[0]["cursor"] == detached["cursor"]
