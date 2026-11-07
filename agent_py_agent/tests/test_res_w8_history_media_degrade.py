"""RES-W8: historical media loss degrades without changing current-turn delivery."""
from __future__ import annotations

import json
import logging
import os
import runpy
import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.guidance import _entry_media_not_sent
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _completed_turn_native_messages,
    _persist_partial_native_turn,
)
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend, _openai_user_messages
from agent_py_agent.agent.backends.responses_wire import message_items
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.conversation import input_media as media
from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_from_metadata,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.conversation.native_history_replay import prepare_native_history_replay
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayAskRunContext,
    GatewayConversationContext,
)
from agent_py_agent.agent.gateway_parts.request_history import (
    append_gateway_conversation_message,
    persist_gateway_partial_result,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.tests.test_cli_run_conversation import _agent
from agent_py_agent.tests.test_native_tool_use_ir_messages_flow import _params


@pytest.fixture(autouse=True)
def delivery_scope():
    media.reset_input_media_delivery()
    yield
    media.reset_input_media_delivery()


def imported(tmp_path, data=b"old-image-bytes", name="old.png"):
    return media.import_input_media_bytes(data, tmp_path / "owner/media/input", name=name)


def history(*refs):
    return [
        {"role": "user", "content": [{"type": "text", "text": "keep-before"},
                                      *media.input_media_blocks(refs),
                                      {"type": "text", "text": "keep-after"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "prior-answer"}]},
        {"role": "user", "content": [{"type": "text", "text": "plain-follow-up"}]},
    ]


def encode(messages, encoder):
    if encoder == "anthropic":
        return media.provider_media_messages(messages)
    if encoder == "openai_chat":
        return [item for row in messages for item in (
            _openai_user_messages(row["content"]) if row["role"] == "user" else [row])]
    return [item for row in messages for item in message_items(row, "fixture-model")]


def placeholder(ref):
    return {"type": "text", "text": f"[历史附件不可用或已清理：{ref.get('name') or ref['sha256']}]"}


@pytest.mark.parametrize("encoder", ("anthropic", "openai_chat", "responses"))
def test_renamed_history_degrades_in_all_encoders_without_spending_budget(tmp_path, caplog, encoder):
    lost = imported(tmp_path)
    kept = imported(tmp_path, data=b"new-image-bytes", name="new.png")
    messages = history(lost, kept)
    original = json.dumps(messages)
    path = Path(lost["path"])
    backup = path.with_suffix(".bak_img")
    path.rename(backup)
    with caplog.at_level(logging.WARNING, logger=media.__name__):
        projected = media.project_input_media(messages, kept["size_bytes"])
    assert projected[0]["content"] == [messages[0]["content"][0], placeholder(lost),
                                        messages[0]["content"][2], messages[0]["content"][3]]
    assert projected[1:] == messages[1:]
    wire = json.dumps(encode(projected, encoder), ensure_ascii=False)
    assert all(text in wire for text in (placeholder(lost)["text"], "keep-before", "keep-after",
                                        "prior-answer", "plain-follow-up"))
    assert media.evicted_input_media_hashes() == frozenset({lost["sha256"]})
    assert media.last_encoded_input_media_hashes() == frozenset({kept["sha256"]})
    assert [record.getMessage() for record in caplog.records] == [
        f"HISTORY_MEDIA_UNAVAILABLE sha256={lost['sha256']} reason=InputMediaError"]
    assert json.dumps(messages) == original
    assert backup.read_bytes() == b"old-image-bytes"


@pytest.mark.parametrize("encoder", ("anthropic", "openai_chat", "responses"))
def test_missing_current_turn_media_keeps_strict_encode_path(tmp_path, caplog, encoder):
    ref = imported(tmp_path)
    messages = history()
    messages[-1]["content"].extend(media.input_media_blocks([ref]))
    Path(ref["path"]).unlink()
    projected = media.project_input_media(messages, ref["size_bytes"])
    assert projected == messages
    with pytest.raises(media.InputMediaError):
        encode(projected, encoder)
    assert media.evicted_input_media_hashes() == frozenset()
    assert not caplog.records


@pytest.mark.parametrize("fault", ("missing", "size", "symlink", "directory", "invalid_identity"))
def test_unavailable_history_is_replaced_in_place_without_mutating_records(tmp_path, caplog, fault):
    ref = imported(tmp_path)
    path = Path(ref["path"])
    if fault == "missing":
        path.unlink()
    elif fault == "size":
        path.write_bytes(b"changed-size")
    elif fault == "symlink":
        backup = path.with_suffix(".saved")
        path.rename(backup)
        path.symlink_to(backup)
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    else:
        ref["sha256"] = "not-a-content-hash"
    messages = history(ref)
    original = json.dumps(messages)
    projected = media.project_input_media(messages, ref["size_bytes"])
    assert projected[0]["content"][1] == placeholder(ref)
    assert projected[1:] == messages[1:]
    assert media.evicted_input_media_hashes() == frozenset({ref["sha256"]})
    assert "HISTORY_MEDIA_UNAVAILABLE" in caplog.text
    encode(projected, "anthropic")
    assert json.dumps(messages) == original


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="mode-000 permission checks require geteuid and non-root")
def test_unreadable_history_degrades_and_logs_a_reason(tmp_path, caplog):
    ref = imported(tmp_path)
    path = Path(ref["path"])
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        assert not os.access(path, os.R_OK)
        projected = media.project_input_media(history(ref), ref["size_bytes"])
        assert projected[0]["content"][1] == placeholder(ref)
        assert f"HISTORY_MEDIA_UNAVAILABLE sha256={ref['sha256']} reason=unreadable" in caplog.text
        assert media.evicted_input_media_hashes() == frozenset({ref["sha256"]})
    finally:
        path.chmod(mode)


def test_unnamed_missing_history_uses_the_full_hash(tmp_path):
    ref = imported(tmp_path)
    ref["name"] = ""
    Path(ref["path"]).unlink()
    projected = media.project_input_media(history(ref), ref["size_bytes"])
    assert projected[0]["content"][1] == {"type": "text", "text": f"[历史附件不可用或已清理：{ref['sha256']}]"}


def test_text_follow_up_after_over_budget_history_uses_budget_placeholder(tmp_path, caplog):
    ref = imported(tmp_path)
    messages = history(ref)
    original = json.dumps(messages)
    projected = media.project_input_media(messages, 1)
    assert projected[0]["content"][1] == {"type": "text", "text": (
        f"[历史附件已归档，本轮媒体预算未加载：{ref['name']}；"
        f"原件引用：{ref['path']}。需要视觉重读时请重新添加附件。]")}
    assert projected[1:] == messages[1:]
    assert media.evicted_input_media_hashes() == frozenset({ref["sha256"]})
    assert not caplog.records
    assert json.dumps(messages) == original


def test_current_turn_guidance_keeps_release_observation_after_budget_eviction(tmp_path):
    first = imported(tmp_path, b"first-image", "first.png")
    second = imported(tmp_path, b"last-image", "last.png")
    messages = history()
    messages[-1]["content"].extend(media.input_media_blocks([first]))
    messages.extend([{"role": "tool", "content": "tool-result"},
                     {"role": "user", "content": media.input_media_blocks([second])}])
    delivery = media.reset_input_media_delivery()
    projected = media.project_input_media(messages, second["size_bytes"])
    media.provider_media_messages(projected)
    assert projected[-2] == messages[-2]
    assert _entry_media_not_sent(SimpleNamespace(metadata={"input_media": [first]}), delivery)
    assert not _entry_media_not_sent(SimpleNamespace(metadata={"input_media": [second]}), delivery)
    assert media.evicted_input_media_hashes(delivery) == frozenset({first["sha256"]})


def test_deleted_media_directory_is_recreated_and_old_reference_degrades(tmp_path):
    old = imported(tmp_path)
    root = Path(old["path"]).parent
    shutil.rmtree(root.parent)
    new = media.import_input_media_bytes(b"new-upload", root, name="new.png")
    assert Path(new["path"]).read_bytes() == b"new-upload"
    assert root.is_dir() and root.parent.is_dir()
    messages = history(old)
    messages[-1]["content"].extend(media.input_media_blocks([new]))
    projected = media.project_input_media(messages, new["size_bytes"])
    assert projected[0]["content"][1] == placeholder(old)
    encode(projected, "anthropic")
    assert media.evicted_input_media_hashes() == frozenset({old["sha256"]})
    assert media.last_encoded_input_media_hashes() == frozenset({new["sha256"]})


def test_same_length_history_replacement_still_fails_at_encode_time(tmp_path):
    ref = imported(tmp_path)
    Path(ref["path"]).write_bytes(b"x" * ref["size_bytes"])
    projected = media.project_input_media(history(ref), ref["size_bytes"])
    assert projected[0]["content"][1]["type"] == "image"
    with pytest.raises(media.InputMediaError, match="附件内容已变化"):
        encode(projected, "anthropic")


@pytest.mark.parametrize("size", ([1], {"bad": 1}, "not-an-int", None, True))
def test_malformed_history_size_degrades_through_the_original_validator(tmp_path, caplog, size):
    ref = imported(tmp_path)
    ref["size_bytes"] = size
    messages = history(ref)
    original = json.dumps(messages)
    projected = media.project_input_media(messages, media.DEFAULT_MEDIA_BYTES)
    assert projected[0]["content"][1] == placeholder(ref)
    assert media.evicted_input_media_hashes() == frozenset({ref["sha256"]})
    assert f"HISTORY_MEDIA_UNAVAILABLE sha256={ref['sha256']} reason=InputMediaError" in caplog.text
    assert json.dumps(messages) == original


def test_no_assistant_means_all_user_rows_stay_current(tmp_path, caplog):
    ref = imported(tmp_path)
    Path(ref["path"]).unlink()
    messages = [row for row in history(ref) if row["role"] != "assistant"]
    projected = media.project_input_media(messages, ref["size_bytes"])
    assert projected == messages
    assert media.evicted_input_media_hashes() == frozenset()
    assert not caplog.records
    with pytest.raises(media.InputMediaError):
        encode(projected, "anthropic")


def test_only_rows_after_the_last_assistant_stay_current(tmp_path):
    old = imported(tmp_path)
    middle = imported(tmp_path, b"middle-image", "middle.png")
    current = imported(tmp_path, b"current-image", "current.png")
    messages = history(old)[:2] + history(middle)[:2] + [
        {"role": "tool", "content": "tool-result"},
        {"role": "user", "content": media.input_media_blocks([current])}]
    for ref in (old, middle, current):
        Path(ref["path"]).unlink()
    projected = media.project_input_media(messages, current["size_bytes"])
    assert projected[0]["content"][1] == placeholder(old)
    assert projected[2]["content"][1] == placeholder(middle)
    assert projected[4:] == messages[4:]
    assert media.evicted_input_media_hashes() == frozenset({old["sha256"], middle["sha256"]})
    with pytest.raises(media.InputMediaError):
        encode(projected, "anthropic")


@pytest.fixture(params=[InterruptedError, ValueError], ids=["interrupted", "error"])
def persisted_user_only_turn(tmp_path, request):
    """用真实部分回合生产者与 Gateway 终态写入生成空正文载体。"""
    agent, params = _agent(tmp_path), _params()
    refs = tuple(media.import_input_media_bytes(data, media.input_media_root(agent), name=name)
                 for data, name in ((b"old-image-bytes", "old.png"), (b"kept-image-bytes", "kept.png")))
    params.tool_ir_history.append(UserTurn("historical-user", media=refs))
    native = _completed_turn_native_messages(params, None)
    assert [row["role"] for row in native] == ["user"]
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat"})
    conversation = GatewayConversationContext(thread_id=thread.thread_id)
    context = GatewayAskRunContext(agent=agent, request={"metadata": {"channel": "chat"}},
        request_id=params.request_id, request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json", on_chunk=None)
    assert append_gateway_conversation_message(agent, context.request, conversation,
        request_id=context.request_id, role="user", content="historical-user")
    results = []

    def persist(result):
        results.append(result)
        persist_gateway_partial_result(context, conversation, result)

    _persist_partial_native_turn(persist, params, request.param("ended before assistant output"))
    assert len(results) == 1
    result = results[0]
    assert result.canonical_native_messages == native
    assert result.runtime_status == ("cancelled" if request.param is InterruptedError else "failed")
    rows = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[-1].content == ""
    assert rows[-1].metadata["turn_end_reason"] == result.turn_end_reason
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    return SimpleNamespace(agent=agent, thread=thread, rows=rows, path=path, native=native,
        refs=refs, raw=path.read_bytes(), metadata=json.dumps(rows[-1].metadata), result=result)


def replay_rows(turn, kind):
    if kind == "ordinary":
        return turn.rows
    return select_message_snapshot(turn.agent.conversation_store.messages, turn.thread.thread_id)


def chat_payload(messages):
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://example.test/v1", api_key="", model_name="fixture-model", stream_enabled=False))
    return backend.project_generate_payload(CacheStructuredPrompt("fixture-system"), messages=messages)


@pytest.fixture
def replay_resources(persisted_user_only_turn, monkeypatch):
    """同时核对真实 FD 数和本次转录读取句柄，含提前关闭与异常退出。"""
    turn, opened = persisted_user_only_turn, []
    original_open = Path.open
    fd_dir = next((path for path in (Path("/dev/fd"), Path("/proc/self/fd")) if path.is_dir()), None)
    before = len(os.listdir(fd_dir)) if fd_dir is not None else None

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == turn.path:
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)
    yield
    assert all(handle.closed for handle in opened)
    if fd_dir is not None:
        assert len(os.listdir(fd_dir)) - before == 0


@pytest.mark.parametrize("kind", ["ordinary", "snapshot"])
def test_ended_user_only_history_allows_text_follow_up(persisted_user_only_turn, replay_resources, kind):
    turn = persisted_user_only_turn
    lost, kept = turn.refs
    Path(lost["path"]).unlink()
    delivery = media.reset_input_media_delivery()
    media.provider_media_block(media.input_media_blocks([kept])[0])
    observed = (delivery.evicted, delivery.encoded)
    replay = prepare_native_history_replay(replay_rows(turn, kind))
    projected = list(replay)
    assert media._MEDIA_DELIVERY.get() is delivery
    assert (delivery.evicted, delivery.encoded) == observed
    current = AnthropicMessageAdapter().to_provider_messages([UserTurn("plain-follow-up")])
    payload = chat_payload(projected + current)
    assert [row["role"] for row in payload["messages"]] == ["system", "user", "user"]
    assert payload["messages"][0] == {"role": "system", "content": "fixture-system"}
    assert payload["messages"][-1] == {"role": "user", "content": "plain-follow-up"}
    assert projected == [{"role": "user", "content": [turn.native[0]["content"][0],
                                                           placeholder(lost), turn.native[0]["content"][2]]}]
    wire = json.dumps(payload, ensure_ascii=False)
    assert placeholder(lost)["text"] in wire and "plain-follow-up" in wire
    assert "data:image/png;base64,a2VwdC1pbWFnZS1ieXRlcw==" in wire
    assert media.last_encoded_input_media_hashes(delivery) == frozenset({kept["sha256"]})
    assert tuple(projected) == provider_history_messages_from_rows(turn.rows) == tuple(replay)
    assert list(canonical_native_messages_from_metadata(turn.rows[-1].metadata)) == turn.native
    assert turn.path.read_bytes() == turn.raw
    assert json.dumps(turn.rows[-1].metadata) == turn.metadata
    assert Path(kept["path"]).read_bytes() == b"kept-image-bytes"
    projected[0]["content"][0]["text"] = "caller-only edit"
    assert list(replay)[0]["content"][0] == turn.native[0]["content"][0]


def test_anonymous_ended_envelope_uses_the_same_history_projection(persisted_user_only_turn):
    turn = persisted_user_only_turn
    lost, _kept = turn.refs
    Path(lost["path"]).unlink()
    metadata = {key: value for key, value in turn.rows[-1].metadata.items()
                if key not in {"conversation_request_id", "gateway_request_id"}}
    row = replace(turn.rows[-1], metadata=metadata)
    projected = provider_history_messages_from_rows([row])
    assert projected[0]["content"][1] == placeholder(lost)
    assert chat_payload(list(projected))["messages"]
    assert list(canonical_native_messages_from_metadata(metadata)) == turn.native
    assert turn.path.read_bytes() == turn.raw


@pytest.mark.parametrize("kind", ["ordinary", "snapshot"])
def test_ended_envelope_keeps_same_length_integrity_rejection(persisted_user_only_turn, replay_resources, kind):
    turn = persisted_user_only_turn
    lost, _kept = turn.refs
    Path(lost["path"]).write_bytes(b"x" * lost["size_bytes"])
    projected = list(prepare_native_history_replay(replay_rows(turn, kind)))
    assert projected == turn.native
    with pytest.raises(media.InputMediaError, match="附件内容已变化") as error:
        chat_payload(projected)
    assert error.value.error_code == "INPUT_MEDIA_INVALID"
    assert error.value.__cause__ is None
    assert turn.path.read_bytes() == turn.raw


@pytest.mark.parametrize("kind", ["ordinary", "snapshot"])
@pytest.mark.parametrize("ending", ["close", "exception"])
def test_ended_envelope_replay_closes_resources_on_early_exit(
        persisted_user_only_turn, replay_resources, kind, ending):
    replay = prepare_native_history_replay(replay_rows(persisted_user_only_turn, kind))
    if ending == "exception":
        with pytest.raises(RuntimeError, match="consumer stopped"), closing(iter(replay)) as iterator:
            assert next(iterator) == persisted_user_only_turn.native[0]
            raise RuntimeError("consumer stopped")
    else:
        with closing(iter(replay)) as iterator:
            assert next(iterator) == persisted_user_only_turn.native[0]


@pytest.mark.parametrize("missing_index", [0, 1])
def test_multiple_current_user_turns_still_reject_missing_media(tmp_path, missing_index):
    lost = imported(tmp_path)
    kept = imported(tmp_path, b"current-image", "current.png")
    turns = [UserTurn("initial"), UserTurn("steer"), UserTurn("later", media=(kept,))]
    turns[missing_index] = replace(turns[missing_index], media=(lost,))
    messages = AnthropicMessageAdapter().to_provider_messages(turns)
    original = json.dumps(messages)
    Path(lost["path"]).unlink()
    with pytest.raises(media.InputMediaError) as error:
        chat_payload(messages)
    assert error.value.error_code == "INPUT_MEDIA_INVALID"
    assert error.value.__cause__ is None
    assert json.dumps(messages) == original
    assert media.evicted_input_media_hashes() == frozenset()


def test_module_collects_without_optional_geteuid(monkeypatch):
    monkeypatch.delattr(os, "geteuid", raising=False)
    loaded = runpy.run_path(__file__)
    collected = {name: value for name, value in loaded.items() if name.startswith("test_")}
    assert set(collected) == {name for name in globals() if name.startswith("test_")}
    permission = "test_unreadable_history_degrades_and_logs_a_reason"
    for name, function in collected.items():
        skips = [mark for mark in getattr(function, "pytestmark", ()) if mark.name == "skipif"]
        if name == permission:
            assert len(skips) == 1 and skips[0].args == (True,)
        else:
            assert not skips
