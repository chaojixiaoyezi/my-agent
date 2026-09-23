from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.openai_chat import _openai_user_messages
from agent_py_agent.agent.backends.responses_wire import message_items
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.conversation.input_media import (
    InputMediaError,
    import_input_media,
    input_media_blocks,
    project_input_media,
    provider_media_messages,
    validate_input_media,
)
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_envelope,
    canonical_native_messages_from_metadata,
)
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
from agent_py_agent.cli.chat_parts.tui_interaction import TuiDraft, TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_media import TuiMediaRef, draft_media, pasted_media_paths


# LLM: 这是二进制媒体合同测试数据，不是模型替身或真实模型验收结果。
# 函数用途: 建立一个本地媒体文件并经产品导入函数生成 ref。
def imported(tmp_path: Path, suffix: str = ".png", data: bytes = b"test-media") -> dict:
    source = tmp_path / f"sample with spaces{suffix}"
    source.write_bytes(data)
    return import_input_media(source, tmp_path / "owner" / "media" / "input")


@pytest.mark.parametrize("suffix", [".png", ".mp4"])
def test_ir_and_restart_keep_only_refs_but_provider_gets_exact_bytes(tmp_path, suffix):
    ref = imported(tmp_path, suffix, b"unique-media-bytes")
    messages = AnthropicMessageAdapter().to_provider_messages([UserTurn("描述附件", media=(ref,))])
    envelope = canonical_native_messages_envelope(messages)
    restored = canonical_native_messages_from_metadata({"canonical_native_messages": envelope})
    encoded_history = json.dumps(restored)
    assert "local_file" in encoded_history and "base64" not in encoded_history
    provider = provider_media_messages(list(restored))
    source = provider[0]["content"][1]["source"]
    assert base64.b64decode(source["data"]) == b"unique-media-bytes"
    assert json.dumps(restored) == encoded_history
    assert Path(ref["path"]).read_bytes() == b"unique-media-bytes"


def test_owner_boundary_and_symlink_are_rejected(tmp_path):
    ref = imported(tmp_path)
    with pytest.raises(InputMediaError):
        validate_input_media([ref], root=tmp_path / "different-owner")
    path = Path(ref["path"])
    saved = path.with_suffix(".backup")
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(InputMediaError):
        validate_input_media([ref], root=path.parent)


def test_changed_bytes_rejected_even_when_length_is_same(tmp_path):
    ref = imported(tmp_path)
    Path(ref["path"]).write_bytes(b"x" * ref["size_bytes"])
    with pytest.raises(InputMediaError):
        provider_media_messages([{"role": "user", "content": input_media_blocks([ref])}])


def test_count_total_bytes_and_import_limit_are_enforced(tmp_path):
    ref = imported(tmp_path)
    with pytest.raises(InputMediaError):
        validate_input_media([ref, ref], max_bytes=ref["size_bytes"])
    with pytest.raises(InputMediaError):
        validate_input_media([ref, ref], max_files=1)
    with pytest.raises(InputMediaError):
        import_input_media(tmp_path / "sample with spaces.png", tmp_path / "staging", max_bytes=1)
    assert not (tmp_path / "staging").exists()


def test_long_media_history_budget_preserves_latest_and_archive_authority(tmp_path):
    ref = imported(tmp_path)
    messages = [{"role": "user", "content": input_media_blocks([ref])} for _ in range(100)]
    original = json.dumps(messages)
    projected = project_input_media(messages, ref["size_bytes"])
    assert sum(row["content"][0]["type"] == "image" for row in projected) == 1
    assert projected[-1]["content"][0]["type"] == "image"
    assert "历史附件已归档" in projected[0]["content"][0]["text"]
    assert json.dumps(messages) == original
    with pytest.raises(InputMediaError):
        project_input_media(messages, 1)


def test_stash_restores_refs_and_deleting_marker_removes_attachment(tmp_path):
    ref = imported(tmp_path)
    state = TuiInteractionState()
    state.register_media(TuiMediaRef("[附件 1]", ref))
    draft = state.capture_draft("[附件 1] 看看", 0)
    state.toggle_stash(draft)
    state.install_draft(TuiDraft("", 0))
    assert not state.capture_draft("", 0).media_refs
    restored = state.toggle_stash(TuiDraft("", 0))
    assert draft_media(restored, restored.text)[0] == "看看"
    assert draft_media(restored, restored.text)[1][0]["sha256"] == ref["sha256"]
    assert not draft_media(restored, "看看")[1]
    assert not draft_media(TuiDraft("[附件 1]", 0), "[附件 1]")[1]


def test_pasted_path_requires_whole_block_of_existing_media(tmp_path):
    source = tmp_path / "sample with spaces.png"
    source.write_bytes(b"media")
    assert pasted_media_paths(f'"{source}"') == (source,)
    assert pasted_media_paths(source.as_uri()) == (source,)
    assert pasted_media_paths(f"请看 {source}") == ()
    assert pasted_media_paths(str(tmp_path / "missing.png")) == ()


def test_media_execution_options_round_trip(tmp_path):
    ref = imported(tmp_path)
    options = GatewayAskExecutionOptions(input_media=(ref,))
    assert GatewayAskExecutionOptions.from_payload(options.to_payload()) == options


def test_openai_and_responses_do_not_silently_drop_images(tmp_path):
    ref = imported(tmp_path)
    blocks = [{"type": "text", "text": "图片是什么"}, *input_media_blocks([ref])]
    chat = _openai_user_messages(blocks)
    assert chat[0]["content"][1]["type"] == "image_url"
    assert base64.b64decode(chat[0]["content"][1]["image_url"]["url"].split(",")[1]) == b"test-media"
    response = message_items({"role": "user", "content": blocks}, "vision-model")
    assert response[-1]["content"][0]["type"] == "input_image"
