# LLM: 媒体回归以真实导入、投影、编码与回执为依据，不把传输替身当商业模型验收。
# 模块用途: 验证附件边界、字节预算以及未出站图片不得消费的合同。
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


# LLM: 使用真实回环输入、Store、预算投影及出站编码；无真实模型请求，确认按原回执而非调用次数。
# 函数用途: 未送出的新图片保持pending，已送出的输入独立消费且可重领重发。
@pytest.mark.parametrize("encode", [False, True])
def test_evicted_active_media_is_pending_until_encoded(tmp_path, monkeypatch, encode):
    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.conversation.input_media import (
        import_input_media_bytes,
        input_media_root,
    )
    from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, claim, thread, _lane):
        refs = [import_input_media_bytes(data, input_media_root(agent)) for data in (b"image-one", b"image-two")]
        receipts = [send(server, message=f"image-{i}", input_media=[ref])[1] for i, ref in enumerate(refs)]
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        entries = list(params.live_archive_state["_guidance_ack_entries"].values())
        ids = [entry.guidance_id for entry in entries]
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="media-call-1") == 2
        messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
        projected = project_input_media(messages, len(b"image-two"))
        if encode:
            wire = provider_media_messages(projected)
            assert [base64.b64decode(block["source"]["data"]) for row in wire
                    if isinstance(row.get("content"), list) for block in row["content"]
                    if block.get("type") == "image"] == [b"image-two"]
        assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        states = [inputs.reconcile_gateway_input_request(gate.paths, request_id=row["request_id"],
                  conversation_store=agent.conversation_store) for row in receipts]
        # 原Gateway把活动回合内未消费输入显示为active_pending；canonical guidance必须精确恢复pending。
        assert states == ["active_pending", "consumed"]
        receipt = inputs.read_gateway_input_receipt(gate.paths, receipts[0]["request_id"])
        pending, _turn = inputs.gateway_input_guidance_binding(receipt, agent.conversation_store)
        assert pending.status == "pending" and not pending.attempt_id and not pending.submission_id
        state = params.live_archive_state
        assert ids[0] not in state["_guidance_ack_ids"]
        assert ids[0] not in state["_guidance_ack_entries"]
        assert ids[0] not in state["_injected_guidance_ids"]
        assert not any(ids[0] in packet["input_ids"] for packet in params.active_turn_user_inputs)
        assert not any(ids[0] in getattr(row, "input_ids", ()) for row in params.tool_ir_history)
        assert guidance.inject_pending_turn_input(agent, params)
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="media-call-2") == 1
        messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
        provider_media_messages(project_input_media(messages, len(b"image-one")))
        assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        assert inputs.reconcile_gateway_input_request(gate.paths, request_id=receipts[0]["request_id"],
               conversation_store=agent.conversation_store) == "consumed"


# LLM: 保留真实保护线程、提交钩子、provider转换及caller ACK，只替换模型网络结果；不能用同线程helper冒充回传。
# 函数用途: 验证wall guard中淘汰的图片不会被原调用线程消费。
def test_wall_guard_media_ack_uses_outbound_observation(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation import input_media as media
    from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        refs = [media.import_input_media_bytes(data, media.input_media_root(agent)) for data in (b"guard-one", b"guard-two")]
        receipts = [send(server, message=f"guard-image-{i}", input_media=[ref])[1] for i, ref in enumerate(refs)]
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
        media.reset_input_media_delivery()
        caller, sent = threading.get_ident(), []

        # LLM: 仅模拟网络响应；真实线程内校验caller不同，再执行产品预算/编码链。
        # 函数用途: 捕获保护线程实际构造的出站图片字节。
        def generate(_prompt, **kwargs):
            assert threading.get_ident() != caller
            wire = provider_media_messages(project_input_media(kwargs["messages"], len(b"guard-two")))
            sent.extend(base64.b64decode(block["source"]["data"]) for row in wire
                        if isinstance(row.get("content"), list) for block in row["content"] if block.get("type") == "image")
            return ModelResponse(text="ok", backend=backend.name)

        backend = agent.backend
        monkeypatch.setattr(backend, "generate", generate)
        monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *_args: 10.0)
        state = generation._ModelGenerationState(agent=agent, params=params,
            chunk_filter=generation.ToolBoundaryChunkFilter(on_chunk=None), ledger=None, call_id="guard-media-call",
            tool_rounds=0, first_token_timeout_seconds=0, on_chunk=None, retry_sink=None, messages=messages)
        request = generation.ModelGenerateParams(agent=agent, params=params, prompt="look", tool_rounds=0)
        assert generation._generate_through_wall_guard(request, state).text == "ok"
        assert len(sent) == 1 and sent[0] in {b"guard-one", b"guard-two"}
        assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        expected = ["consumed" if data == sent[0] else "active_pending" for data in (b"guard-one", b"guard-two")]
        assert [inputs.reconcile_gateway_input_request(gate.paths, request_id=row["request_id"],
                conversation_store=agent.conversation_store) for row in receipts] == expected
        unsent = expected.index("active_pending")
        receipt = inputs.read_gateway_input_receipt(gate.paths, receipts[unsent]["request_id"])
        pending, _turn = inputs.gateway_input_guidance_binding(receipt, agent.conversation_store)
        assert pending.status == "pending" and not pending.attempt_id and not pending.submission_id


# LLM: 旧worker保留原Context及观察对象，新调用独占新对象；同步等真实线程结束，不靠sleep猜时序。
# 函数用途: 验证迟到旧调用不覆盖新调用结果，单次观察也不残留上一投影。
def test_late_media_worker_cannot_overwrite_new_call(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from agent_py_agent.agent.conversation import input_media as media

    old_ref = imported(tmp_path, data=b"old-media")
    new_ref = imported(tmp_path, data=b"new-media")
    old_delivery = media.reset_input_media_delivery()
    old_context = copy_context()
    new_delivery = media.reset_input_media_delivery()
    assert old_delivery is not new_delivery
    old_messages = [{"role": "user", "content": input_media_blocks([old_ref])}]
    new_messages = [{"role": "user", "content": input_media_blocks([new_ref])}]
    provider_media_messages(project_input_media(new_messages, new_ref["size_bytes"]))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(old_context.run, provider_media_messages, old_messages).result(timeout=5)
    assert media.last_encoded_input_media_hashes(old_delivery) == frozenset({old_ref["sha256"]})
    assert media.last_encoded_input_media_hashes(new_delivery) == frozenset({new_ref["sha256"]})
    assert media.last_encoded_input_media_hashes() == frozenset({new_ref["sha256"]})
    project_input_media([], new_ref["size_bytes"])
    assert media.last_encoded_input_media_hashes(new_delivery) is None
    assert not media.evicted_input_media_hashes(new_delivery)


# LLM: 先经真实插话提交及ACK/失败恢复，再执行两个真实Compact候选入口；候选必须复制观察事实而非原生锁或活动引用。
# 函数用途: 防止纯文本或图片插话遗留的瞬态媒体句柄破坏Compact，并校验候选隔离。
@pytest.mark.parametrize("with_media", [False, True], ids=["text", "image"])
@pytest.mark.parametrize("settlement", ["ack", "failed"])
@pytest.mark.parametrize("projection", ["history", "active_tools"])
def test_guidance_media_observation_survives_compact(tmp_path, monkeypatch, with_media, settlement, projection):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.compact_active_projection import (
        replace_recovery_active_tools,
    )
    from agent_py_agent.agent.agent_core.compact_request_recovery import replace_recovery_history
    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.conversation import input_media as media
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput
    from agent_py_agent.tests.test_compact_active_projection import _fixture
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, _b, _g, _c, _r, server, claim, thread, _lane):
        options = {"input_media": [media.import_input_media_bytes(b"compact-image", media.input_media_root(agent))]} if with_media else {}
        assert send(server, message="compact-guidance", **options)[0] == 202
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="compact-media-call") == 1
        if with_media:
            provider_media_messages(AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history))
        if settlement == "ack":
            assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        else:
            assert guidance.restore_turn_input_after_failed_provider_call(agent, params, provider_call_id="compact-media-call") == 1
        delivery = params.live_archive_state["_guidance_media_delivery"]
        encoded = media.last_encoded_input_media_hashes(delivery)
        context, compact_params, frozen, retained = _fixture()
        compact_params.live_archive_state.update(params.live_archive_state)
        if projection == "active_tools":
            candidate, _prepared = replace_recovery_active_tools(compact_params, frozen,
                compact_context=context, retained_records=retained, max_chars=2000)
        else:
            prompt = PromptRenderInput(system_prompt="", memory_text="", owner_scope="", dynamic="", workspace_context="",
                injection_fragments=("原注入",), tool_catalog="", recommendations="", execution_facts="",
                user_prompt="继续", task_and_transcript="继续", native_tool_use=True)
            candidate, _prepared = replace_recovery_history(compact_params, replace(frozen, prompt_input=prompt),
                history_seed=SimpleNamespace(messages=(), canonical_messages=()), injection="新注入",
                injection_index=0, compact_context=context)
        snapshot = candidate.live_archive_state["_guidance_media_delivery"]
        assert snapshot is not delivery and snapshot.lock is not delivery.lock
        assert media.last_encoded_input_media_hashes(snapshot) == encoded
        provider_media_messages([])
        assert media.last_encoded_input_media_hashes(snapshot) == encoded
