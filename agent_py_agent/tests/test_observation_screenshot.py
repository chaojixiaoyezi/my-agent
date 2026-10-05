"""J16 第 9 节（vision1）：能看图的主模型随观察结果附一张缩小截图。

链路：适配器 observe 生成缩略 PNG → MCP image 内容块（encode_call_result）→ 宿主提取并按档案模态
落盘为 owner 附件引用（extract_observation_screenshot）→ ToolResult.metadata → record_tool_call_ir 登记
→ _native_provider_messages 注入一次并消费；不写 IR / transcript。没声明时不落盘、不注入。
"""
from __future__ import annotations

import base64
import json
import os
import stat
import struct
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_call_archive_record import extract_observation_screenshot
from agent_py_agent.agent.agent_core.tool_ir_history import (
    OBSERVATION_SCREENSHOT_PENDING_KEY,
    record_tool_call_ir,
)
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.agent.tooling.screen_observation import (
    OBSERVATION_SCREENSHOT_KEY,
    OBSERVATION_SCREENSHOT_MAX_BYTES,
    OBSERVATION_SCREENSHOT_MAX_WIDTH_PX,
    observation_screenshot,
)
from agent_py_agent.agent.tooling.screen_region_digest import PixelBuffer
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    make_test_protocol_snapshot,
)

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fea735cb420000000049454e44ae426082"
)


def _fake_agent(root: Path, modalities: list[str]):
    return SimpleNamespace(
        config=SimpleNamespace(model_input_modalities=list(modalities)),
        home_paths=SimpleNamespace(owner_home_dir=str(root)),
    )


def _outcome_with_image(png_b64: str) -> ToolHandlerOutcome:
    payload = {
        "result": '{"window": "win:b:1"}',
        "content": [
            {"type": "text", "text": '{"window": "win:b:1"}'},
            {"type": "image", "data": png_b64, "mimeType": "image/png"},
        ],
        "structuredContent": {"window": "win:b:1", "generation": "b-1-1"},
    }
    return ToolHandlerOutcome("observe_window", True, json.dumps(payload, ensure_ascii=False),
                              result_envelope={"observation": {"observation_id": "obs-vision"}})


def _native_params():
    return SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-vision", source_protocol="native"),
        tool_ir_history=[],
        tool_context=[],
        live_archive_state={},
        provider_history_messages=[],
    )


def _record_observation(params, ref: dict, call_id: str = "toolu_obs") -> None:
    call = canonical_history_call("observe_window", {"window": ""}, call_id=call_id)
    result = ToolResult.succeeded(call, facts=ToolSuccessFacts(metadata={"observation_screenshot": ref}))
    record_tool_call_ir(params, tool_rounds=1, call=call, result=result)


def _has_image(messages) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") == "image"
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
    )


def test_declared_image_model_gets_screenshot_saved_and_injected(tmp_path):
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")

    stripped, ref = extract_observation_screenshot(agent, _outcome_with_image(png_b64))
    assert ref and ref["sha256"] and ref["media_type"] == "image/png" and ref["size_bytes"] == len(TINY_PNG)
    stored = tmp_path / "media" / "input" / ref["sha256"]
    assert stored.is_file() and stored.read_bytes() == TINY_PNG, "落盘到 owner 私有附件根，内容寻址"
    assert '"image"' not in stripped.output and png_b64 not in stripped.output, "base64 不进工具结果正文"

    params = _native_params()
    _record_observation(params, ref)
    assert params.live_archive_state[OBSERVATION_SCREENSHOT_PENDING_KEY]["sha256"] == ref["sha256"]
    first = _native_provider_messages(agent, params)
    tail = first[-1]
    assert tail["role"] == "user" and tail["content"][0]["type"] == "text"
    image_block = tail["content"][-1]
    assert image_block["type"] == "image" and image_block["source"]["type"] == "local_file"
    assert image_block["source"]["sha256"] == ref["sha256"]


def test_model_without_image_declaration_gets_no_screenshot(tmp_path):
    agent = _fake_agent(tmp_path, ["text"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")

    stripped, ref = extract_observation_screenshot(agent, _outcome_with_image(png_b64))
    assert ref is None and not (tmp_path / "media").exists(), "没声明时既不落盘也不登记"
    assert '"image"' not in stripped.output, "图片块照样剥离，base64 不进任何正文"

    params = _native_params()
    messages = _native_provider_messages(agent, params)
    assert not _has_image(messages)


# 函数用途: 没有观察核验事实的工具（任意 MCP 工具返回图片）结果原样保留：不剥离、不落盘、不登记（3a 终审收紧）。
def test_non_observation_tool_images_are_left_untouched(tmp_path):
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    outcome = replace(_outcome_with_image(png_b64), result_envelope={})

    kept, ref = extract_observation_screenshot(agent, outcome)
    assert kept is outcome and ref is None, "非观察工具的图片不能被当成观察截图"
    assert not (tmp_path / "media").exists()


# 函数用途: 观察记录被宿主核验拒绝时，图片块照样剥离但不落盘、不登记（截图来自未核验的观察）。
def test_rejected_observation_strips_image_without_storing(tmp_path):
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    outcome = replace(_outcome_with_image(png_b64), result_envelope={"observation_rejected": "OBSERVATION_STALE"})

    stripped, ref = extract_observation_screenshot(agent, outcome)
    assert ref is None and not (tmp_path / "media").exists()
    assert '"image"' not in stripped.output and png_b64 not in stripped.output


def test_observation_screenshot_respects_pixel_and_byte_caps():
    big = PixelBuffer(800, 400, os.urandom(800 * 400 * 3))
    encoded = observation_screenshot(big)
    assert encoded, "高熵大图应在逐级缩小后落在字节上限内"
    png = base64.b64decode(encoded)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= OBSERVATION_SCREENSHOT_MAX_BYTES
    width, height = struct.unpack(">II", png[16:24])
    assert width <= OBSERVATION_SCREENSHOT_MAX_WIDTH_PX and height < big.height, "缩过"
    assert width < big.width, "字节超限时继续缩小"

    small = PixelBuffer(40, 30, bytes(40 * 30 * 3))
    png_small = base64.b64decode(observation_screenshot(small))
    assert struct.unpack(">II", png_small[16:24]) == (40, 30), "小图不缩放"


def test_screenshot_never_enters_ir_or_persistent_history_and_injects_once(tmp_path):
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    _, ref = extract_observation_screenshot(agent, _outcome_with_image(png_b64))
    params = _native_params()
    _record_observation(params, ref)

    persisted = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    assert not _has_image(persisted), "IR 本身（持久化投影的源头）不含图片块"

    first = _native_provider_messages(agent, params)
    assert _has_image(first)
    second = _native_provider_messages(agent, params)
    assert not _has_image(second), "截图只给当次模型请求，注入即消费"


def test_encode_call_result_moves_screenshot_into_image_block(monkeypatch):
    fake = types.ModuleType("mcp")

    class _FakeType:
        def __init__(self, **fields):
            self.fields = fields

    fake.types = SimpleNamespace(
        TextContent=_FakeType,
        ImageContent=_FakeType,
        CallToolResult=_FakeType,
        ServerResult=lambda result: result,
    )
    monkeypatch.setitem(sys.modules, "mcp", fake)

    from agent_py_agent.agent.tooling import computer_use_observation_tools as glue

    body = {"window": "win:b:1"}
    structured = {"window": "win:b:1", OBSERVATION_SCREENSHOT_KEY: "cG5n"}
    encoded = glue.encode_call_result(body, structured, False)
    blocks = encoded.fields["content"]
    assert [block.fields.get("type") for block in blocks] == ["text", "image"]
    assert blocks[1].fields["data"] == "cG5n"
    assert blocks[1].fields["mimeType"] == "image/png"
    assert OBSERVATION_SCREENSHOT_KEY not in encoded.fields["structuredContent"], "截图键不留在 structuredContent"
    assert OBSERVATION_SCREENSHOT_KEY not in json.dumps(body, ensure_ascii=False)


def test_observe_body_excludes_screenshot_key():
    from agent_py_agent.agent.tooling import computer_use_observation_tools as glue

    class _Observer:
        supports_ui_candidates = False

        def observe(self, window=None, *, want_screenshot=False):
            return {"window": "win:b:1", OBSERVATION_SCREENSHOT_KEY: "cG5n",
                    "my_agent_observation": {"schema": "plugin_observation.v1"}}

    body, structured, is_error = glue.call_observation_tool(_Observer(), "observe_window", {}, None)
    assert not is_error and OBSERVATION_SCREENSHOT_KEY not in body
    assert structured[OBSERVATION_SCREENSHOT_KEY] == "cG5n", "截图留给接管层编码，不进模型可见正文"


def test_stored_screenshot_file_and_dirs_are_private(tmp_path):
    """落盘文件 0600、目录 0700（隐私边界；vision1r 初审补）。"""
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    _, ref = extract_observation_screenshot(agent, _outcome_with_image(png_b64))
    assert ref and ref["sha256"]
    stored = Path(ref["path"])
    assert stored.is_file() and stored.read_bytes() == TINY_PNG
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    for parent in (tmp_path / "media", tmp_path / "media" / "input"):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_latest_observation_overwrites_pending_screenshot(tmp_path):
    """连续两次观察只注入最新一张（pending 覆盖语义；vision1r 初审补）。"""
    agent = _fake_agent(tmp_path, ["text", "image"])
    params = _native_params()
    first = {"sha256": "1" * 64, "media_type": "image/png", "size_bytes": 1, "name": "first"}
    second = {"sha256": "2" * 64, "media_type": "image/png", "size_bytes": 1, "name": "second"}
    _record_observation(params, first, call_id="toolu_obs_1")
    _record_observation(params, second, call_id="toolu_obs_2")
    assert params.live_archive_state[OBSERVATION_SCREENSHOT_PENDING_KEY]["sha256"] == "2" * 64
    messages = _native_provider_messages(agent, params)
    assert messages[-1]["content"][-1]["source"]["sha256"] == "2" * 64


def test_pending_screenshot_does_not_leak_across_params(tmp_path):
    """pending 只挂在产生它的运行参数上，别的 params（别的线程/会话）不被注入（vision1r 初审补）。"""
    agent = _fake_agent(tmp_path, ["text", "image"])
    owner_params, other_params = _native_params(), _native_params()
    _record_observation(
        owner_params,
        {"sha256": "3" * 64, "media_type": "image/png", "size_bytes": 1, "name": "shot"},
        call_id="toolu_obs_3",
    )
    assert not _has_image(_native_provider_messages(agent, other_params))
    assert _has_image(_native_provider_messages(agent, owner_params))


# 函数用途: 没声明 image 时走真实 MCP 渲染路（_normalize_call_result）——正文与改动前逐字节相同、无占位、无 base64（vision2）。
def test_undeclared_model_body_has_no_placeholder_and_matches_pre_change_baseline(tmp_path):
    from agent_py_agent.agent.tooling import mcp_client
    from agent_py_agent.agent.tooling.output_projection import project_tool_output_body

    agent = _fake_agent(tmp_path, ["text"])
    text_block = {"type": "text", "text": '{"window": "win:b:1"}'}

    def project(content_blocks):
        normalized = mcp_client._normalize_call_result(
            {"content": content_blocks, "isError": False, "structuredContent": {"window": "win:b:1"}})
        raw = json.dumps({"result": normalized["content"], "content": normalized["content_blocks"],
                          "structuredContent": normalized["structuredContent"]}, ensure_ascii=False)
        stripped, ref = extract_observation_screenshot(agent, ToolHandlerOutcome("observe_window", True, raw))
        body = project_tool_output_body(tool="observe_window", output=stripped.output, trust="runtime", redaction="default")
        return raw, stripped, ref, body

    raw, stripped, ref, body = project([text_block])
    assert ref is None and '"image"' not in stripped.output
    assert "[image content" not in body and "mimeType=" not in body, "没声明时正文不出现图片占位"
    baseline = project_tool_output_body(tool="observe_window", output=raw, trust="runtime", redaction="default")
    assert body == baseline, "无图结果剥离无副作用：模型可见正文与改动前逐字节相同"
    _, _, _, body_with_image = project([text_block, {"type": "image", "data": "cG5n", "mimeType": "image/png"}])
    assert "[image content" in body_with_image and body_with_image != body, "对照：旧适配器形态仍带占位（用例有鉴别力）"


# 函数用途: 落盘时模型支持图、注入时已换到不支持图的模型：不注入、pending 丢弃（vision2 换模型边界）。
def test_switching_to_a_model_without_image_drops_pending_screenshot(tmp_path):
    agent_image = _fake_agent(tmp_path, ["text", "image"])
    agent_text = _fake_agent(tmp_path, ["text"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    _, ref = extract_observation_screenshot(agent_image, _outcome_with_image(png_b64))
    assert ref and ref["sha256"]
    params = _native_params()
    _record_observation(params, ref)
    assert OBSERVATION_SCREENSHOT_PENDING_KEY in params.live_archive_state

    messages = _native_provider_messages(agent_text, params)
    assert not _has_image(messages), "换到不支持图的模型后不再注入"
    assert OBSERVATION_SCREENSHOT_PENDING_KEY not in params.live_archive_state, "pending 已被消费丢弃"


# 函数用途: 请求组装即消费：这一枪失败/被取消后重试（再次组装）不重复注入，也不串到下一轮（vision2）。
def test_interrupted_request_consumes_pending_and_retry_does_not_reinject(tmp_path):
    agent = _fake_agent(tmp_path, ["text", "image"])
    png_b64 = base64.b64encode(TINY_PNG).decode("ascii")
    _, ref = extract_observation_screenshot(agent, _outcome_with_image(png_b64))
    params = _native_params()
    _record_observation(params, ref)

    first = _native_provider_messages(agent, params)  # 第一次组装（随后该请求失败/被取消）
    assert _has_image(first)
    retry = _native_provider_messages(agent, params)  # 失败重试或下一轮的组装
    assert not _has_image(retry), "失败重试/下一轮不再注入"
    assert OBSERVATION_SCREENSHOT_PENDING_KEY not in params.live_archive_state


# 函数用途: 执行器按内部参数把宿主的结构化意愿注入 __observation_screenshot（模板同 __process_completion_target，vision2）。
def test_handler_arguments_injects_screenshot_wish_from_trusted_context():
    from agent_py_agent.agent.tooling.executor import _handler_arguments

    def payload_for(trusted, declared=("__observation_screenshot",), model_args=None):
        request = SimpleNamespace(trusted_run_context=trusted)
        call = SimpleNamespace(arguments={"window": "main", **(model_args or {})}, call_id="call-1", operation_id="")
        runtime = SimpleNamespace(runtime_policy=SimpleNamespace(
            input_policy=SimpleNamespace(internal_parameters=tuple(declared))))
        return _handler_arguments(request, call, runtime)

    assert payload_for({"observation_screenshot": True})["__observation_screenshot"] is True
    assert payload_for({"observation_screenshot": False})["__observation_screenshot"] is False
    assert payload_for({})["__observation_screenshot"] is False, "缺省从严"
    assert payload_for({"observation_screenshot": "yes"})["__observation_screenshot"] is False, "非布尔不认"
    assert "__observation_screenshot" not in payload_for({"observation_screenshot": True}, declared=()), "未声明不注入"
    # 模型在 arguments 里塞同名字段不得生效：宿主注入必须无条件覆盖（vision2r 初审补；此前 setdefault 变异存活）
    assert payload_for({"observation_screenshot": False}, model_args={"__observation_screenshot": True})[
        "__observation_screenshot"] is False
    assert payload_for({"observation_screenshot": True}, model_args={"__observation_screenshot": False})[
        "__observation_screenshot"] is True
