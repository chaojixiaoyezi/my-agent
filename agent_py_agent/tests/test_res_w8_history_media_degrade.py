"""RES-W8: historical media loss degrades without changing current-turn delivery."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.guidance import _entry_media_not_sent
from agent_py_agent.agent.backends.openai_chat import _openai_user_messages
from agent_py_agent.agent.backends.responses_wire import message_items
from agent_py_agent.agent.conversation import input_media as media


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
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
