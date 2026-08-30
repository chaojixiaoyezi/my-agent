from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_transcript import (
    agent_transcript_attempt_has_events,
    agent_transcript_path,
    append_agent_transcript_event,
    begin_agent_transcript_turn,
    compact_agent_transcript_events,
    read_agent_transcript_events,
)


def _agent(tmp_path):
    return SimpleNamespace(conversation_store=SimpleNamespace(root=tmp_path))


def test_agent_transcript_is_exact_incremental_and_bounded(tmp_path, monkeypatch) -> None:
    import agent_py_agent.agent.conversation.agent_transcript as transcript_module

    monkeypatch.setattr(transcript_module, "AGENT_TRANSCRIPT_MAX_EVENTS", 3)
    agent = _agent(tmp_path)
    request_id = begin_agent_transcript_turn(
        agent,
        run_id="child-a",
        attempt_id="attempt-a",
    )
    for index in range(5):
        assert append_agent_transcript_event(
            agent,
            thread_id="thread-child-a",
            task_id="child-a",
            request_id=request_id,
            kind="thinking_started",
            phase="started",
            block_id=f"{request_id}:thinking-{index}",
            payload={"index": index},
        ) == index + 1

    compact_agent_transcript_events(agent, run_id="child-a")
    page = read_agent_transcript_events(agent, run_id="child-a", after=0)
    assert [row["seq"] for row in page["events"]] == [3, 4, 5]
    assert page["cursor"] == 5
    assert read_agent_transcript_events(agent, run_id="child-a", after=4)[
        "events"
    ][0]["seq"] == 5


def test_agent_transcript_rejects_cross_run_or_path_like_identity(tmp_path) -> None:
    agent = _agent(tmp_path)
    request_id = begin_agent_transcript_turn(
        agent,
        run_id="child-a",
        attempt_id="attempt-a",
    )
    assert append_agent_transcript_event(
        agent,
        thread_id="thread-child-a",
        task_id="child-a",
        request_id=request_id.replace("child-a", "child-b"),
        kind="thinking_started",
        phase="started",
        block_id="bg-agent:child-b:attempt-a:thinking",
        payload={},
    ) == 0
    assert not agent_transcript_path(agent, "child-a").exists()
    with pytest.raises(ValueError):
        agent_transcript_path(agent, "../outside")


def test_agent_transcript_attempt_event_check_is_exact(tmp_path) -> None:
    agent = _agent(tmp_path)
    old_request = begin_agent_transcript_turn(
        agent,
        run_id="child-a",
        attempt_id="attempt-old",
    )
    assert append_agent_transcript_event(
        agent,
        thread_id="thread-child-a",
        task_id="child-a",
        request_id=old_request,
        kind="thinking_started",
        phase="started",
        block_id=f"{old_request}:thinking",
        payload={},
    ) == 1

    assert agent_transcript_attempt_has_events(
        agent,
        run_id="child-a",
        attempt_id="attempt-old",
    ) is True
    assert agent_transcript_attempt_has_events(
        agent,
        run_id="child-a",
        attempt_id="attempt-current",
    ) is False
