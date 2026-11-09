"""Cat2-R5：坏行可降级，文件级错误和全坏历史仍拒绝后台续接。"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_history_seed import (
    BackgroundHistoryUnavailableError,
    background_conversation_history_seed,
    prepare_background_history_or_raise,
)
from agent_py_agent.agent.conversation.history_seed import seed_text_messages
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_history_unavailable_traceback_has_bounded_safe_structured_sources():
    errors = [{"context": "subagents.load_root_index", "error_type": "SubagentRunIndexUnavailable",
               "category": "runtime_error", "error_code": "INDEX_PARTIAL", "sqlite_error_code": 11,
               "message": "private user content", "path": "/private/secret",
               "scope_resolution": "/private/secret"} for _ in range(17)]
    error = BackgroundHistoryUnavailableError("unreadable", load_errors=errors, detail="private detail")
    summary = json.loads(error.__notes__[0].split(": ", 1)[1])
    assert str(error) == "unreadable" and error.load_errors == errors and error.detail == "private detail"
    assert summary["load_error_count"] == 17 and summary["sources_truncated"] is True
    assert len(summary["sources"]) == 16
    assert summary["sources"][0] == {
        "context": "subagents.load_root_index", "error_type": "SubagentRunIndexUnavailable",
        "category": "runtime_error", "error_code": "INDEX_PARTIAL", "sqlite_error_code": 11}
    assert "private" not in error.__notes__[0]
    invalid = BackgroundHistoryUnavailableError("unreadable", load_errors=[{
        "context": "x" * 129, "error_code": True, "sqlite_error_code": True, "category": "正文"}])
    assert json.loads(invalid.__notes__[0].split(": ", 1)[1])["sources"] == [{}]


@pytest.fixture
def case(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   auto_save_memory=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "cat2-r5", "channel": "chat", "cwd": str(tmp_path),
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir),
    })
    expected = tuple(("user" if index % 2 == 0 else "assistant", f"preserved message {index}") for index in range(6))
    for role, content in expected:
        store.messages.append({"thread_id": thread.thread_id, "role": role, "content": content})
    request = SimpleNamespace(thread_id=thread.thread_id, task_id="", reason="scheduled_progress_report",
                              wake_signal=None, conversation_turn_id="bg-turn")
    return SimpleNamespace(agent=agent, store=store, thread=thread, request=request, expected=expected)


def _source_state(case):
    paths = (case.store.storage.message_path(case.thread.thread_id),
             case.store.storage.observation_path(case.thread.thread_id),
             case.store.storage.thread_path(case.thread.thread_id))
    return {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) if path.exists() else None
            for path in paths}


@pytest.mark.parametrize("damage", ["mid_line", "torn_tail", "torn_observation", "all_bad", "oserror"])
def test_background_seed_degrades_only_skippable_rows(case, monkeypatch, caplog, damage):
    path = case.store.storage.message_path(case.thread.thread_id)
    if damage == "mid_line":
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join([*lines[:3], b'{"broken json line\n', *lines[3:]]))
    elif damage == "torn_tail":
        with path.open("ab") as handle:
            handle.write(b'{"private_torn_tail":')
    elif damage == "torn_observation":
        case.store.storage.observation_path(case.thread.thread_id).write_bytes(b"{torn observation\n")
    elif damage == "all_bad":
        path.write_bytes(b"{bad\n{bad2\n")
    else:
        def unreadable(*_args, **_kwargs):
            raise OSError("simulated read error")
        monkeypatch.setattr(case.store.messages, "recent_report", unreadable)
    before = _source_state(case)

    # 同一损坏连续读取两次：降级不依赖修复原文件，失败也不得消费错误证据。
    for _ in range(2):
        caplog.clear()
        if damage in {"all_bad", "oserror"}:
            result = background_conversation_history_seed(case.agent, case.store, case.thread, case.request)
            assert result.status == "unreadable" and result.seed is None and result.load_errors
            with pytest.raises(BackgroundHistoryUnavailableError) as raised:
                prepare_background_history_or_raise(case.agent, case.store, case.thread, case.request)
            assert raised.value.error_code == "BACKGROUND_HISTORY_UNAVAILABLE" and raised.value.load_errors
        else:
            with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.conversation.background_history_seed"):
                result = prepare_background_history_or_raise(case.agent, case.store, case.thread, case.request)
                assert result.status == "ready"
                assert seed_text_messages(result.seed) == case.expected
            warnings = [record for record in caplog.records
                        if record.name == "agent_py_agent.agent.conversation.background_history_seed"
                        and record.getMessage().startswith("JSONL_ROW_SKIPPED ")]
            assert len(warnings) == 1
            assert f"thread={case.thread.thread_id}" in warnings[0].getMessage()
            assert "private_torn_tail" not in warnings[0].getMessage()
            assert all(content not in warnings[0].getMessage() for _role, content in case.expected)
        assert _source_state(case) == before


def test_file_level_report_blocks_even_when_readable_messages_remain(case, monkeypatch):
    rows, errors = case.store.messages.recent_report(case.thread.thread_id, limit=20)
    assert rows and not errors
    report = {"context": "conversation.messages.read", "error_type": "OSError", "message": "simulated file read error"}
    monkeypatch.setattr(case.store.messages, "recent_report", lambda *_args, **_kwargs: (rows, [report]))
    before = _source_state(case)
    for _ in range(2):
        result = background_conversation_history_seed(case.agent, case.store, case.thread, case.request)
        assert result.status == "unreadable" and result.seed is None
        assert report in result.load_errors
        assert _source_state(case) == before
