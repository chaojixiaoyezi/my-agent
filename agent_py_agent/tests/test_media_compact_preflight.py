"""媒体会话越过压缩点：压缩链不可用时 preflight 只守窗口硬上限，越窗时强制恢复给出结构化非文本原因。"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.input_media import (
    import_input_media,
    input_media_blocks,
    input_media_root,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
# canonical 原生历史里的图片是 owner 附件目录的 local_file 引用（发送前才展开 base64）；preflight 不读文件，假 sha 即可。
IMAGE = {"type": "image", "source": {"type": "local_file", "path": "/owner/attachments/" + "f" * 64, "sha256": "f" * 64,
                                     "media_type": "image/png", "size_bytes": 70, "name": "pixel.png"}}
WINDOW = 60_000


# LLM: 与原 preflight 测试同形：真实配置与策略，只替换文字估算；媒体只以原生历史里的图片块表达。
# 函数用途: 构造一次 preflight 请求，按参数决定是否带图片、是否持久保存。
def _preflight(monkeypatch, *, media: bool, save: bool, tokens: int, policy: str = "off"):
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, memory_compact_auto_trigger_percent=50, compact_media_policy=policy),
        backend=SimpleNamespace(context_window_tokens=1_000, name="fake"),
    )
    history = [{"role": "user", "content": [{"type": "text", "text": "看图"}, *([IMAGE] if media else [])]}]
    params = SimpleNamespace(
        context_scope="default", save=save, live_archive_state={}, tool_ir_history=(),
        provider_history_messages=history,
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
    )
    monkeypatch.setattr("agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
                        lambda _value: tokens)
    return preflight_context_pressure_response(SimpleNamespace(agent=agent, params=params, prompt="继续", tool_rounds=0))


# (是否带图, save, 估算 token, off 下是否拦截, auto 下是否拦截)
@pytest.mark.parametrize("case", [
    (False, True, 499, False, False), (False, True, 500, True, True),   # 纯文字：原压缩点 50% 不变
    (True, True, 500, False, True), (True, True, 999, False, True),     # 媒体：off 下压缩链不可用不拦；auto 下走归档引用，按压缩点拦
    (True, True, 1_000, True, True),                                    # 媒体：窗口硬上限两种策略都拦
    (False, False, 999, False, False), (False, False, 1_000, True, True),  # save=false 纯文字：原窗口门不变
    (True, False, 999, False, False), (True, False, 1_000, True, True),    # save=false 媒体：与纯文字相同
])
@pytest.mark.parametrize("policy", ["off", "auto"])
def test_preflight_threshold_follows_compact_capacity(monkeypatch, case, policy):
    media, save, tokens, fires_off, fires_auto = case
    fires = fires_off if policy == "off" else fires_auto
    response = _preflight(monkeypatch, media=media, save=save, tokens=tokens, policy=policy)
    assert (response is not None) is fires
    if fires:
        assert response.runtime_status == "context_overflow"
        assert ("compact_capacity=non_text" in response.text) is (media and policy == "off")


# LLM: 只经原 Store 写入已完成回合；图片经产品导入到 owner 附件目录，原生信封与真实会话同形。
# 函数用途: 按用例写入一轮问答（带图与否）和若干长文字行，返回写入的消息 ID。
def _seed(agent, thread_id: str, case: tuple, tmp_path) -> list[str]:
    with_image, rows = case[0], case[1]
    source = tmp_path / "one-pixel.png"
    source.write_bytes(PNG)
    image = input_media_blocks([import_input_media(source, input_media_root(agent))])
    store = agent.conversation_store
    native = [{"role": "user", "content": [{"type": "text", "text": "看图"}, *(image if with_image else [])]},
              {"role": "assistant", "content": [{"type": "text", "text": "图片是红色"}]}]
    rows_in = [("user", "看图", None), ("assistant", "图片是红色", native)]
    rows_in += [("user" if index % 2 == 0 else "assistant", f"原文{index}:" + "x" * 6_000, None) for index in range(rows)]
    ids = []
    for index, (role, content, envelope) in enumerate(rows_in):
        metadata = {"conversation_request_id": "prior-media" if index < 2 else f"prior-{(index - 2) // 2}"}
        if envelope is not None:
            metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(envelope)
        ids.append(store.messages.append({"thread_id": thread_id, "role": role, "content": content,
                                          "metadata": metadata, "now": 10.0}).message_id)
    return ids


# 函数用途: 统计一次实际出站请求里的图片块数。
def _image_blocks(wire) -> int:
    return json.dumps(wire["messages"]).count('"type": "image"')


# (是否带图, 长文字行数, off 下预期, auto 下预期)；本矩阵明确声明不支持图片，故保留旧存档选源断言。
@pytest.mark.parametrize("case", [
    (True, 6, "sent", "sent"),            # 压缩点以下：原样带图发送
    (True, 16, "sent", "compacted"),      # 越过压缩点、低于窗口：off 带图发送不压缩；auto 归档引用后压缩再发送
    (True, 32, "refused", "compacted"),   # 越过窗口：off 结构化非文本拒绝；auto 强制恢复走归档引用后发送
    (False, 16, "compacted", "compacted"),  # 纯文字对照：原自动压缩后发送
])
@pytest.mark.parametrize("policy", ["off", "auto"])
# LLM: 纯文字能力声明是回退的结构事实；原落盘/业务/来源断言保留，并锁住回退原因。
# 函数用途: 验证不支持媒体的摘要后端走旧存档选源，原始记录和归档结果都不丢。
def test_gateway_media_history_between_trigger_and_window(tmp_path, monkeypatch, case, policy):
    outcome = case[2] if policy == "off" else case[3]
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent-home"))
    fixture = actual_request(tmp_path, mode="disabled", original_window=WINDOW)
    agent = fixture.agent
    agent.config.compact_media_policy = policy
    # 声明纯文字模态：本测试的假 HTTP 后端不看图，避免片 B 的视觉探针多出请求，业务调用计数保持 1。
    agent.config.model_input_modalities = ["text"]
    agent.config.model_context_window_tokens = WINDOW
    agent.backend.context_window_tokens = WINDOW
    agent.config.memory_compact_auto_trigger_percent = 50
    agent.config.max_tokens = 1024
    ids = _seed(agent, fixture.thread_id, case, tmp_path)
    business, _ = fake_http(monkeypatch, fixture)
    summaries = []

    # LLM: 这里只捕获结构上不支持图片的旧存档选源请求，不把这一断言外推到冻结主前缀。
    # 函数用途: 核对文字回退请求的媒体引用和模型回复。
    def summarize(request):
        # 声明不支持图片，故此次失去前缀缓存，摘要请求只看到归档引用。
        payload = json.dumps(request.messages or [], ensure_ascii=False) + str(request.prompt)
        summaries.append(("附件引用" in payload, "local_file" in payload))
        return ModelResponse(text="已完整核对原始材料。", backend="fake")

    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", summarize)
    if outcome == "refused":
        with pytest.raises(ConversationCompactError) as failure:
            request_execution._run_gateway_ask(fixture.context)
        assert failure.value.code == "COMPACT_REQUEST_NON_TEXT"
        # 用户看到的原因须点明是图片等非文本内容，不能落回通用的压缩失败文案。
        message = gateway_client_error_message(failure.value.error_code)
        assert "图片" in message and message != gateway_client_error_message("COMPACT_REQUEST_PROJECTION_UNKNOWN")
    else:
        assert request_execution._run_gateway_ask(fixture.context).response == "资料整理完成。"
    thread = agent.conversation_store.threads.require(fixture.thread_id)
    stored = [row.message_id for row in agent.conversation_store.messages.recent(fixture.thread_id, limit=0)]
    assert stored[:len(ids)] == ids, "原始记录保持原序，不因拒绝或跳过而丢行"
    if outcome == "compacted":
        assert thread.compact_generation == 1 and summaries and len(business) == 1
        assert _image_blocks(business[0][0]) == 0
        media = case[0]
        assert all(saw_ref is media and not saw_local for saw_ref, saw_local in summaries), summaries
        checkpoint = committed_compact_checkpoint_chain(agent, thread)[-1]
        if media:
            assert checkpoint["media_policy_reason"] == "COMPACT_PREFIX_MEDIA_UNSUPPORTED"
            assert checkpoint["media_blocks_archived"] == 1 and checkpoint["media_refs"]
        return
    assert thread.compact_generation == 0 and not summaries
    assert len(business) == (0 if outcome == "refused" else 1)
    if outcome == "sent":
        assert _image_blocks(business[0][0]) == 1


@pytest.mark.parametrize("policy", ["auto", "archived_refs"])
@pytest.mark.parametrize("modalities", [["text", "image"], None])
# LLM: 只替换HTTP响应，比较实际摘要HTTP与同次冻结主投影；媒体归档只约束checkpoint和后续业务展示。
# 函数用途: 证明已支持或未声明不支持的媒体原样进入连续前缀，压缩后仍按引用归档。
def test_frozen_media_prefix_is_sent_unchanged_then_archived(tmp_path, monkeypatch, policy, modalities):
    from agent_py_agent.tests.test_transcript_compact_prefix import _blocks, _capture_frozen_wires

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent-home"))
    fixture = actual_request(tmp_path, mode="disabled", original_window=WINDOW)
    agent = fixture.agent
    agent.config.compact_media_policy = policy
    agent.config.model_input_modalities = modalities
    agent.config.memory_compact_auto_trigger_percent = 50
    agent.config.max_tokens = 1024
    ids = _seed(agent, fixture.thread_id, (True, 16), tmp_path)
    frozen = _capture_frozen_wires(monkeypatch, agent)
    wires, _ = fake_http(monkeypatch, fixture)
    assert request_execution._run_gateway_ask(fixture.context).response == "资料整理完成。"
    assert len(wires) == 2, "只有一次摘要和一次业务HTTP，不另发视觉探针或看图小请求"
    summary_wire, business_wire = wires[0][0], wires[1][0]
    source = _blocks(summary_wire)[:-1]
    assert len(frozen) == 1 and source and source == _blocks(frozen[0])[:len(source)]
    assert _image_blocks(summary_wire) == 1 and _image_blocks(business_wire) == 0
    thread = agent.conversation_store.threads.require(fixture.thread_id)
    checkpoint = committed_compact_checkpoint_chain(agent, thread)[-1]
    assert checkpoint["media_blocks_archived"] == 1 and checkpoint["media_blocks_summarized"] == 0
    assert checkpoint["media_refs"] and checkpoint.get("media_policy_reason", "") != "COMPACT_PREFIX_MEDIA_UNSUPPORTED"
    assert all(sha in json.dumps(summary_wire, ensure_ascii=False) for sha in checkpoint["media_refs"])
    assert "were replaced by" not in str(summary_wire), "冻结前缀摘要指令不能谎称媒体已从请求移除"
    assert "local_file" not in thread.summary
    stored = [row.message_id for row in agent.conversation_store.messages.recent(fixture.thread_id, limit=0)]
    assert stored[:len(ids)] == ids


# LLM: 两种路径只在测试中剥离前缀优化，真实媒体导入/预算/归档/CAS 均执行；同量历史和同窗口不得增加请求。
# 函数用途: 比较原来源与 F6 在超窗含图历史上的调用数、完整覆盖与图片事实。
@pytest.mark.parametrize("policy", ["auto", "archived_refs"])
def test_supported_media_prefix_over_window_recovers_with_original_source(tmp_path, monkeypatch, policy):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import compact_request_recovery
    from agent_py_agent.agent.conversation import compact

    results = []
    for freeze, name in ((False, "main"), (True, "f6xx")):
        with monkeypatch.context() as patch:
            root = tmp_path / name
            patch.setenv("MY_AGENT_HOME", str(root / "my-agent-home"))
            fixture = actual_request(root, mode="disabled", original_window=WINDOW)
            agent = fixture.agent
            assert agent.home_paths.root.resolve() == (root / "my-agent-home").resolve()
            agent.config.compact_media_policy = policy
            agent.config.model_input_modalities = ["text", "image"]
            agent.config.memory_compact_auto_trigger_percent = 50
            ids = _seed(agent, fixture.thread_id, (True, 32), root)
            if not freeze:
                original_surface = compact_request_recovery._transcript_summary_surface

                def without_prefix(*args):
                    surface, tools = original_surface(*args)
                    return replace(surface, transcript_prefix=None), tools

                patch.setattr(compact_request_recovery, "_transcript_summary_surface", without_prefix)
                original_resolved = compact._resolved_provider_surface
                patch.setattr(compact, "_resolved_provider_surface", lambda request: replace(
                    original_resolved(request), transcript_prefix=None))
            wires, _ = fake_http(patch, fixture)
            assert request_execution._run_gateway_ask(fixture.context).response == "资料整理完成。"
            thread = agent.conversation_store.threads.require(fixture.thread_id)
            checkpoints = committed_compact_checkpoint_chain(agent, thread)
            assert thread.compact_generation == 1 and len(checkpoints) == 1
            checkpoint = checkpoints[0]
            assert checkpoint["source_message_ids"] == ids
            assert checkpoint["media_blocks_archived"] + checkpoint["media_blocks_summarized"] == 1
            assert checkpoint["media_refs"] and _image_blocks(wires[-1][0]) == 0
            if policy == "archived_refs":
                assert all(_image_blocks(wire[0]) == 0 for wire in wires)
            stored = [row.message_id for row in agent.conversation_store.messages.recent(fixture.thread_id, limit=0)]
            assert stored[:len(ids)] == ids
            results.append((len(wires), {key: checkpoint[key] for key in (
                "media_refs", "media_blocks_archived", "media_blocks_summarized")}))
    assert results[0] == results[1], "容量预检不得比原媒体分段路径多发一次失败请求或改变媒体事实"
    print("media_prefix_fallback_calls", policy, results)
