from __future__ import annotations

import pytest

from agent_py_agent.cli.chat_parts.tui_events import (
    TuiEvent,
    TuiEventJournal,
    TuiEventSequencer,
)


def _event(seq: int, *, event_id: str = "evt", stream_id: str = "stream") -> TuiEvent:
    return TuiEvent(
        event_id=f"{event_id}-{seq}",
        seq=seq,
        stream_id=stream_id,
        block_id="block",
        kind="assistant_delta",
        phase="delta",
        payload={"text": "x"},
    )


def test_event_rejects_invalid_envelope() -> None:
    with pytest.raises(ValueError, match="requires"):
        TuiEvent("", 1, "stream", "block", "kind", "started")
    with pytest.raises(ValueError, match="seq"):
        TuiEvent("event", 0, "stream", "block", "kind", "started")
    with pytest.raises(ValueError, match="phase"):
        TuiEvent("event", 1, "stream", "block", "kind", "invented")


def test_sequencer_allocates_monotonic_events_and_scope() -> None:
    sequencer = TuiEventSequencer(
        "gateway:req-1",
        session_id="session-1",
        request_id="req-1",
        clock=lambda: 42.0,
    )
    first = sequencer.emit("turn_started", "started", "turn:req-1")
    second = sequencer.emit("assistant_started", "started", "assistant:req-1")
    assert (first.seq, second.seq) == (1, 2)
    assert second.stream_id == "gateway:req-1"
    assert second.session_id == "session-1"
    assert second.request_id == "req-1"
    assert second.created_at == 42.0
    assert first.event_id != second.event_id


def test_sequencer_allows_explicit_per_event_request_scope() -> None:
    sequencer = TuiEventSequencer("session", session_id="session-1")
    event = sequencer.emit(
        "turn_started",
        "started",
        "turn:2",
        request_id="request-2",
        turn_id="request-2",
    )
    assert event.session_id == "session-1"
    assert event.request_id == "request-2"
    assert event.turn_id == "request-2"


def test_journal_deduplicates_and_rejects_conflicting_identity() -> None:
    journal = TuiEventJournal(max_events=2)
    first = _event(1)
    assert journal.append(first).status == "accepted"
    assert journal.append(first).status == "duplicate"
    conflict = TuiEvent(
        event_id=first.event_id,
        seq=2,
        stream_id="stream",
        block_id="block",
        kind="assistant_delta",
        phase="delta",
        payload={"text": "different"},
    )
    result = journal.append(conflict)
    assert result.status == "rejected"
    assert result.reason == "event_id_conflict"


def test_journal_rejects_unseen_out_of_order_and_retains_cursor() -> None:
    journal = TuiEventJournal(max_events=2)
    assert journal.append(_event(2)).accepted
    result = journal.append(_event(1, event_id="late"))
    assert result.status == "rejected"
    assert result.reason == "out_of_order_seq"
    assert journal.cursors() == {"stream": 2}


def test_journal_bounds_replay_window_without_losing_stream_cursor() -> None:
    journal = TuiEventJournal(max_events=2, max_seen_ids=3)
    for seq in range(1, 5):
        assert journal.append(_event(seq)).accepted
    assert [event.seq for event in journal.snapshot()] == [3, 4]
    assert journal.cursors()["stream"] == 4
