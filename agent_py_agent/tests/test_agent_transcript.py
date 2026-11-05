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
    return SimpleNamespace(conversation_store=SimpleNamespace(storage=SimpleNamespace(root=tmp_path)))


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


def test_w8_retire_rechecks_age_and_preserves_transition_sidecar(tmp_path):
    import os

    from agent_py_agent.agent.conversation.agent_transcript import retire_agent_transcript_file

    path = tmp_path / "child.jsonl"
    path.write_bytes(b"old")
    os.utime(path, (100, 100))
    assert retire_agent_transcript_file(path, now=200, retention_seconds=101) == 0
    assert path.read_bytes() == b"old"
    assert retire_agent_transcript_file(path, now=200, retention_seconds=100) == 3
    assert not path.exists() and (tmp_path / ".child.jsonl.transition.lock").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_w8_retire_never_unlinks_nonexclusive_stream(tmp_path, kind):
    import os

    from agent_py_agent.agent.conversation.agent_transcript import retire_agent_transcript_file

    target = tmp_path / "original"
    target.write_bytes(b"keep")
    path = tmp_path / "child.jsonl"
    path.symlink_to(target) if kind == "symlink" else os.link(target, path)
    assert retire_agent_transcript_file(path, now=10**12, retention_seconds=0) == 0
    assert path.exists() and target.read_bytes() == b"keep"


def test_w8_multilink_stat_is_rejected_before_unlink(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.conversation import agent_transcript

    # 受控stat反例只验证分支，不替代上面的真实硬链接用例；真实用例保留且不skip。
    path = tmp_path / "child.jsonl"
    path.write_bytes(b"keep")
    original = os.stat

    def stat_at(name, *args, **kwargs):
        info = original(name, *args, **kwargs)
        if name == path.name and kwargs.get("dir_fd") is not None:
            fields = list(info)
            fields[3] = 2
            return os.stat_result(fields)
        return info

    # 能力表按函数对象判断；包装stat时保留本机原有dir_fd/NOFOLLOW支持，不放宽被测保护。
    assert original in os.supports_dir_fd and original in os.supports_follow_symlinks
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {stat_at})
    monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks | {stat_at})
    monkeypatch.setattr(agent_transcript.os, "stat", stat_at)
    assert agent_transcript.retire_agent_transcript_file(path, now=10**12, retention_seconds=0) == 0
    assert path.read_bytes() == b"keep"
