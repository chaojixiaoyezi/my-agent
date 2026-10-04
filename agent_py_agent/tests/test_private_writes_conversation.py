"""pwf：会话与用量组宿主数据私有写入用例。

验证范围：agent_transcript 追加/重写、store_wakes/store_wake_publication 的观察账追加。
统一在 umask 0o022 下断言：新文件 0600、新目录 0700；预置 0644 文件写一次后收紧到 0600；
已有内容逐字节保留（只在尾部追加）。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.agent_transcript import (
    agent_transcript_path,
    append_agent_transcript_event,
)
from agent_py_agent.agent.conversation.background_transcript import (
    BACKGROUND_TRANSCRIPT_EVENT_KINDS,
    BACKGROUND_TRANSCRIPT_EVENT_PHASES,
    BACKGROUND_TRANSCRIPT_SCHEMA,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _transcript_agent(root: Path):
    storage = SimpleNamespace(root=root)
    store = SimpleNamespace(storage=storage)
    return SimpleNamespace(conversation_store=store)


def _append_transcript(agent, run_id: str, *, payload: dict) -> None:
    request_id = f"bg-agent:{run_id}:attempt-1"
    append_agent_transcript_event(
        agent,
        thread_id="thread-1",
        task_id=run_id,
        request_id=request_id,
        kind=sorted(BACKGROUND_TRANSCRIPT_EVENT_KINDS)[0],
        phase=sorted(BACKGROUND_TRANSCRIPT_EVENT_PHASES)[0],
        block_id=f"{request_id}:block-1",
        payload=payload,
    )


def test_agent_transcript_append_is_private_and_byte_stable(tmp_path: Path) -> None:
    agent = _transcript_agent(tmp_path / "conv")
    run_id = "run-1"
    request_id = f"bg-agent:{run_id}:attempt-1"
    block_id = f"{request_id}:block-1"
    path = agent_transcript_path(agent, run_id)

    # 预置一个 0644 的旧账本行：写一次后应被收紧到 0600，且旧内容逐字节保留。
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = b'{"seq": 1}\n'
    path.write_bytes(seed)
    os.chmod(path, 0o644)

    _append_transcript(agent, run_id, payload={"text": "hello"})

    expected = json.dumps(
        {
            "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
            "seq": 2,
            "thread_id": "thread-1",
            "task_id": run_id,
            "request_id": request_id,
            "kind": sorted(BACKGROUND_TRANSCRIPT_EVENT_KINDS)[0],
            "phase": sorted(BACKGROUND_TRANSCRIPT_EVENT_PHASES)[0],
            "block_id": block_id,
            "payload": {"text": "hello"},
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    assert path.read_bytes() == seed + expected
    assert _mode(path) == 0o600
    # pdp 2026-10-03：目录是测试先按 umask 建的（0755），私有写不再替调用方收紧。
    assert _mode(path.parent) == 0o755


def test_agent_transcript_compact_rewrite_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.conversation.agent_transcript import compact_agent_transcript_events

    agent = _transcript_agent(tmp_path / "conv")
    run_id = "run-2"
    path = agent_transcript_path(agent, run_id)
    _append_transcript(agent, run_id, payload={"text": "a"})
    before = path.read_bytes()
    os.chmod(path, 0o644)

    compact_agent_transcript_events(agent, run_id=run_id)

    assert path.read_bytes() == before
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700


def _observation_request(thread_id: str, *, now: float) -> dict:
    return {
        "thread_id": thread_id,
        "event_type": "subagent_runner_finished",
        "summary": "子任务已完成。",
        "source_agent_id": "child-1",
        "root_task_id": "task-1",
        "requires_main_agent": True,
        "now": now,
    }


def _wake_request(thread_id: str, *, now: float) -> dict:
    return {
        "thread_id": thread_id,
        "reason": "subagent_runner_finished",
        "root_task_id": "task-1",
        "dedupe_key": "child-1:DONE",
        "metadata": {"task_id": "child-1", "status": "DONE"},
        "now": now,
    }


def test_wake_observation_ledger_is_private_and_byte_stable(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    path = store.storage.observation_path(thread.thread_id)

    observation, _signal = store.wakes.append_observation(
        _observation_request(thread.thread_id, now=20.0),
        _wake_request(thread.thread_id, now=20.1),
    )

    expected = json.dumps(observation.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    assert path.read_bytes() == expected
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700

    # 预置宽权限后写第二行：文件收紧到 0600，旧行逐字节保留。
    os.chmod(path, 0o644)
    before = path.read_bytes()
    store.wakes.append_observation(
        _observation_request(thread.thread_id, now=21.0),
        {**_wake_request(thread.thread_id, now=21.1), "dedupe_key": "child-1:DONE-2"},
    )
    assert path.read_bytes().startswith(before)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
