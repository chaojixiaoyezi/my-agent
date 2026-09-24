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


# (是否带图, 长文字行数, off 下预期, auto 下预期)
@pytest.mark.parametrize("case", [
    (True, 6, "sent", "sent"),            # 压缩点以下：原样带图发送
    (True, 16, "sent", "compacted"),      # 越过压缩点、低于窗口：off 带图发送不压缩；auto 归档引用后压缩再发送
    (True, 32, "refused", "compacted"),   # 越过窗口：off 结构化非文本拒绝；auto 强制恢复走归档引用后发送
    (False, 16, "compacted", "compacted"),  # 纯文字对照：原自动压缩后发送
])
@pytest.mark.parametrize("policy", ["off", "auto"])
def test_gateway_media_history_between_trigger_and_window(tmp_path, monkeypatch, case, policy):
    outcome = case[2] if policy == "off" else case[3]
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent-home"))
    fixture = actual_request(tmp_path, mode="disabled", original_window=WINDOW)
    agent = fixture.agent
    agent.config.compact_media_policy = policy
    agent.config.model_context_window_tokens = WINDOW
    agent.backend.context_window_tokens = WINDOW
    agent.config.memory_compact_auto_trigger_percent = 50
    agent.config.max_tokens = 1024
    ids = _seed(agent, fixture.thread_id, case, tmp_path)
    business, _ = fake_http(monkeypatch, fixture)
    summaries = []

    def summarize(request):
        # 摘要请求只能看到归档引用文字块，不能带 local_file 媒体引用或附件路径
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
        return
    assert thread.compact_generation == 0 and not summaries
    assert len(business) == (0 if outcome == "refused" else 1)
    if outcome == "sent":
        assert _image_blocks(business[0][0]) == 1
