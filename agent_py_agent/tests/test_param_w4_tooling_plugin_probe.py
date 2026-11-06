"""PARAM-W4 消费点合同：无网络、无模型调用、无真实插件进程，只使用临时文件。

来源：plan-param-w4.md 与硬编码清单 2.2、3.1；仅验证可选宿主配置的消费者。
范围：响应/解压字节、请求字符、MCP 连接、展示缓存、探测预算和 AgentConfig 默认。
复现：在工作树根以隔离 HOME 执行 python3 -m pytest 本文件 -q --tb=short。
配置声明、注册和非白名单装配入口由集成切片负责；这里不声称已完成生产接线。
"""
from __future__ import annotations

import asyncio
import gzip
import io
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent import plugin_activation_ref as activation
from agent_py_agent.agent import plugin_runtime
from agent_py_agent.agent.conversation import input_media, input_media_images
from agent_py_agent.agent.session_lock import service as session_lock
from agent_py_agent.agent.settings import reasoning_probe as probe
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.defaults import context_window_or_default
from agent_py_agent.agent.tooling import web_fetch_runtime as web
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.web_http_helpers import MAX_BODY_CHARS, scalar_text
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.cli.chat_parts import history, tui_model_add, tui_subscription_models


# LLM: 只构造请求值对象；URL 不会被连接，配置是宿主注入的独立测试对象。
# 函数用途: 创建响应体上限的公共测试请求。
def _raw_request(**options):
    return web.FetchRawRequest("web_fetch", "https://example.test/", "GET", {}, None, 3, **options)


# LLM: 传输仅用内存字节，保留真实逐跳流程、解压和超限拒绝链。
# 函数用途: 在抓取入口读取伪响应，记录实际 read 长度。
def _fetch(monkeypatch, request, body, *, encoding="", status=200):
    reader = io.BytesIO(body)
    response = SimpleNamespace(status=status, headers={"Content-Encoding": encoding},
                               read=MagicMock(side_effect=reader.read))
    monkeypatch.setattr(web, "_send_pinned", lambda _hop: (MagicMock(), response))
    result = web.fetch_raw_response(request, format_http_error=lambda *args: None,
                                    resolve_pin=lambda _url: web.PinResult("203.0.113.1", None))
    return result, response.read


# LLM: 改配置必须触及真实读取预算，不只检查 helper 返回值。
# 函数用途: 验证响应体配置在相等边界放行、超一字节拒绝。
@pytest.mark.parametrize("size,ok", [(7, True), (8, False)])
def test_response_limit_reaches_transport(monkeypatch, size, ok):
    request = _raw_request(config=SimpleNamespace(web_response_body_max_bytes=7))
    result, read = _fetch(monkeypatch, request, b"x" * size)
    read.assert_called_once_with(8)
    assert isinstance(result, web.RawResponseParts) is ok
    if not ok:
        assert result.error_code == "ARTIFACT_TOO_LARGE"


# LLM: 压缩体预算和解压后预算不得脱钩，仍使用原解压器而非一次性解压。
# 函数用途: 验证配置限制同时拦住解压炸弹。
def test_response_limit_applies_after_decompression(monkeypatch):
    request = _raw_request(config=SimpleNamespace(web_response_body_max_bytes=100))
    result, read = _fetch(monkeypatch, request, gzip.compress(b"x" * 101), encoding="gzip")
    read.assert_called_once_with(101)
    assert result.error_code == "ARTIFACT_TOO_LARGE"


# LLM: 不传配置保持旧上限；显式请求上限优先，重定向 replace 不丢冻结预算。
# 函数用途: 检查兼容默认、调大配置以及调用者显式覆盖。
def test_response_limit_defaults_and_explicit_override():
    assert _raw_request().max_bytes == 1_000_000
    assert _raw_request(config=SimpleNamespace()).max_bytes == 1_000_000
    configured = _raw_request(config=SimpleNamespace(web_response_body_max_bytes=2_000_000))
    assert configured.max_bytes == replace(configured, timeout=1).max_bytes == 2_000_000
    assert _raw_request(max_bytes=3, config=SimpleNamespace(web_response_body_max_bytes=7)).max_bytes == 3


# LLM: 缺省或无效宿主配置不得变成无限预算；正整数不接受 bool、字符串或浮点数。
# 函数用途: 检查响应字节预算的兼容失败值。
@pytest.mark.parametrize("value", [None, 0, -1, True, "7", 7.5])
def test_invalid_response_limit_preserves_default(value):
    assert _raw_request(config=SimpleNamespace(web_response_body_max_bytes=value)).max_bytes == 1_000_000


# LLM: 字符预算与 UTF-8 字节数独立；显式 max_chars 仍是调用方的准确上限。
# 函数用途: 验证配置进入正文校验，且不改变一般标量校验。
def test_request_body_character_limit_and_explicit_override():
    config = SimpleNamespace(web_request_body_max_chars=3)
    assert scalar_text("中文🙂", name="body", max_chars=None, config=config) == "中文🙂"
    with pytest.raises(ValueError):
        scalar_text("中文🙂多", name="body", max_chars=None, config=config)
    assert scalar_text("1234", name="body", max_chars=4, config=config) == "1234"
    assert scalar_text("", name="body", max_chars=None, config=config, allow_empty=True) == ""
    with pytest.raises(ValueError):
        scalar_text({}, name="body", max_chars=None, config=config)


# LLM: 缺省仍为百万字符，调大配置不得被旧字面量再次截住。
# 函数用途: 检查正文预算缺省、无效值和增大后的边界。
@pytest.mark.parametrize("value", [None, 0, -1, False, "3", 3.0])
def test_request_body_default_and_invalid_config(value):
    assert scalar_text("x" * MAX_BODY_CHARS, name="body", max_chars=None) == "x" * MAX_BODY_CHARS
    with pytest.raises(ValueError):
        scalar_text("x" * (MAX_BODY_CHARS + 1), name="body", max_chars=None,
                    config=SimpleNamespace(web_request_body_max_chars=value))
    assert len(scalar_text("x" * (MAX_BODY_CHARS + 1), name="body", max_chars=None,
                           config=SimpleNamespace(web_request_body_max_chars=MAX_BODY_CHARS + 1))) == MAX_BODY_CHARS + 1


# LLM: 兼容常量必须直接等于配置权威，显式调用参数与原来的数量语义不变。
# 函数用途: 检查媒体、图片、闲置锁和历史兜底取自 AgentConfig。
def test_compatibility_defaults_match_agent_config():
    assert AgentConfig.input_media_max_bytes == input_media.DEFAULT_MEDIA_BYTES
    assert AgentConfig.input_media_max_files == input_media.DEFAULT_MEDIA_FILES
    assert AgentConfig.input_image_max_pixels == input_media_images.DEFAULT_IMAGE_PIXELS
    assert AgentConfig.feishu_personal_idle_lock_seconds == session_lock.DEFAULT_IDLE_SECONDS
    assert AgentConfig.chat_history_max_turns == history.MAX_HISTORY_TURNS
    assert input_media.validate_input_media(None) == ()
    assert session_lock.UnlockService(SimpleNamespace(), idle_limit_seconds=61)._idle_limit_seconds == 61
    assert history.chat_history_max_turns(SimpleNamespace(chat_history_max_turns=3)) == 3
    turns = []
    for i in range(3):
        history.append_conversation_turn(turns, threading.Lock(), history.ConversationTurn(str(i), "a"), max_turns=2)
    assert turns == [("1", "a"), ("2", "a")]


# LLM: 测试替换的只是对话框回调，实际构造 TextArea 并读回返回字段，不把显示默认当作用户保存。
# 函数用途: 在手动添加界面检查默认窗口和用户覆盖。
@pytest.mark.parametrize("override", [None, "65536"])
def test_manual_model_window_uses_canonical_default(monkeypatch, override):
    monkeypatch.setattr(AgentConfig, "model_context_window_tokens", 196608)
    seen = []

    # LLM: HSplit 把 TextArea 转成 Window；读写其真实 Buffer，不替换控件或缩小返回值断言。
    # 函数用途: 在真实控件缓冲区中模拟输入，核对默认和用户覆盖。
    async def dialog(_app, _title, body, _buttons, **_kwargs):
        name, window = body.children[2].content.buffer, body.children[4].content.buffer
        seen.append(window.text)
        name.text = "manual-model"
        if override:
            window.text = override
        return True

    monkeypatch.setattr(tui_model_add, "_dialog", dialog)
    form = tui_model_add._ConnectionForm(None, None, "test", "openai_compatible")
    rows = asyncio.run(form._manual({"ok": True}))
    assert seen == ["196608"]
    assert rows[0]["model_context_window_tokens"] == (override or "196608")


# LLM: 服务商已知值优先，未知项用统一兜底；两者不能被本机当前会话窗口替换。
# 函数用途: 验证勾选界面的未知窗口来自权威默认。
@pytest.mark.parametrize("default_window", [None, 196608])
def test_subscription_window_preserves_known_values(monkeypatch, default_window):
    monkeypatch.setattr(AgentConfig, "model_context_window_tokens", default_window)
    expected = str(context_window_or_default(AgentConfig))

    # LLM: 读取已布局控件的同一个 Buffer，不能只断言传给 TextArea 的构造参数。
    # 函数用途: 核对订阅勾选表单的未知窗口默认。
    async def dialog(_app, _title, body, _buttons, **_kwargs):
        assert body.children[3].content.buffer.text == expected
        return ["known", "unknown"]

    monkeypatch.setattr(tui_subscription_models, "_dialog", dialog)
    rows = asyncio.run(tui_subscription_models.choose_models(None, [
        {"model_name": "known", "model_context_window_tokens": 65536}, {"model_name": "unknown"}], "测试"))
    assert [row["model_context_window_tokens"] for row in rows] == [65536, expected]


# LLM: 构造插件不启动传输；保存原 MCP 构造器的调用结果，不能递归调用被替换的类。
# 函数用途: 检查可选配置确实进入能力包 MCP 客户端且不改变普通插件默认。
@pytest.mark.parametrize("configured,expected", [
    (None, 10.0), (2.5, 2.5), (0, 10.0), (-1, 10.0), (False, 10.0),
    ("2.5", 10.0), (float("inf"), 10.0), (float("nan"), 10.0),
])
def test_plugin_pack_connect_timeout_is_bound_at_construction(tmp_path, monkeypatch, configured, expected):
    from agent_py_agent.tests.test_cpack_w3_lazy_activation import staged_pack

    _store, entry, owner = staged_pack(tmp_path)
    monkeypatch.setattr(plugin_runtime, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    monkeypatch.setattr(plugin_runtime, "sandboxed_plugin_argv", lambda argv, spec: list(argv))
    config = SimpleNamespace(plugin_pack_mcp_connect_timeout_seconds=configured, plugin_activation_memo_max_entries=2)
    client = plugin_runtime.PluginMCPClient(owner, entry, config=config)
    try:
        assert client.lazy and not client.is_running()
        assert client.config.connect_timeout == expected
        assert type(client.config.connect_timeout) is float
    finally:
        client.stop()


# LLM: 实际安装表与时戳来自临时目录，缓存只可加速 available，require 必须再次读权威。
# 函数用途: 验证两条配置缓存的 FIFO 淘汰与撤销后拒绝。
def test_activation_memo_config_bounds_fifo_without_authorizing_send(tmp_path, monkeypatch):
    import os
    import time

    from agent_py_agent.tests.test_plugin_activation import revocation
    from agent_py_agent.tests.test_plugin_activation_ref import plugin_reference

    monkeypatch.setattr(activation, "_ACTIVATION_AVAILABILITY_MEMO", activation.OrderedDict())
    config = SimpleNamespace(plugin_activation_memo_max_entries=1)
    references = []
    for index in range(2):
        store, entry, _ref, owner = plugin_reference(tmp_path / str(index), phase="active")
        path = owner.plugins_dir / "installations.json"
        old = time.time_ns() - 10_000_000_000
        os.utime(path, ns=(old, old))
        ref = activation.PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, entry.activation_id, config=config)
        assert ref.available()
        references.append((store, entry, ref, owner))
    assert len(activation._ACTIVATION_AVAILABILITY_MEMO) == 1
    assert next(iter(activation._ACTIVATION_AVAILABILITY_MEMO))[0] == str(references[-1][3].root)
    store, entry, ref, _owner = references[-1]
    assert set(ref.to_payload()) == {"root", "identity", "scope"}
    store.change_activation(revocation(entry))
    with pytest.raises(ValueError):
        ref.require()
    assert not ref.available()


# LLM: 预热同样受容量控制，无效配置只能回到兼容默认，不能把缓存当授权存储。
# 函数用途: 检查预热入口读取宿主配置并淘汰最先插入的条目。
def test_activation_prewarm_accepts_config(tmp_path, monkeypatch):
    owner = resolve_owner_home(tmp_path)
    monkeypatch.setattr(activation, "_ACTIVATION_AVAILABILITY_MEMO", activation.OrderedDict())
    monkeypatch.setattr(activation.os, "stat", lambda *args, **kwargs: SimpleNamespace(st_mtime_ns=1))
    monkeypatch.setattr(activation, "cache_stat_signature", lambda _stat: ("stamp",))
    monkeypatch.setattr(activation, "cache_entry_trustworthy", lambda _mtime: True)
    rows = tuple(SimpleNamespace(enabled=True, manifest=SimpleNamespace(plugin_id=str(index)), activation_id=str(index))
                 for index in range(3))
    activation.remember_available_installations(owner, rows, ("stamp",),
                                                config=SimpleNamespace(plugin_activation_memo_max_entries=2))
    assert [key[2] for key in activation._ACTIVATION_AVAILABILITY_MEMO] == ["1", "2"]


# LLM: 假后端只捕获结构化配置与发送选项；完全不执行真实模型、凭据或记录写入。
# 函数用途: 经过完整样本采集链验证超时和 token 上限，同时保留模型档案约束。
@pytest.mark.parametrize("token_cap,profile_cap,timeout,expected", [(123, 1000, 2.5, 123), (4000, 97, 3.0, 97)])
def test_probe_config_reaches_backend_and_request(monkeypatch, token_cap, profile_cap, timeout, expected):
    config = AgentConfig(max_tokens=profile_cap, model_context_window_tokens=128000)
    config.reasoning_probe_max_output_tokens = token_cap
    config.reasoning_probe_timeout_seconds = timeout
    sent, backend_configs = [], []

    # LLM: 真实 generate 调用仍发生，返回内容不参与判定，只使用 usage。
    # 函数用途: 捕获发送给后端的推理选项并返回最小结构化用量。
    def generate(_prompt, *, request_options):
        sent.append(request_options)
        return SimpleNamespace(usage={"output_tokens": 11})

    # LLM: 闭包只捕获当前请求配置，不持久化也不构造真实 HTTP 后端。
    # 函数用途: 记录采集阶段交给后端的超时配置。
    def factory(_name, backend_config):
        backend_configs.append(backend_config)
        return SimpleNamespace(generate=generate)

    monkeypatch.setattr(probe, "get_backend", factory)
    monkeypatch.setattr(probe, "_save_record", lambda *args: None)
    job = probe._ProbeJob(SimpleNamespace(), probe.ProbeTarget("test", config), "manual", False, "w4")
    try:
        samples = probe._collect_samples(job)
    finally:
        probe._STARTED.pop(job.key, None)
    assert len(samples) == len(sent) == probe._TOTAL_REQUESTS
    assert backend_configs[0].request_timeout == timeout
    assert backend_configs[0].model_reasoning_control == "effort"
    assert [options.reasoning_effort for options in sent] == list(probe.PROBE_LEVELS) * probe.PROBE_ROUND_COUNT
    assert {options.max_output_tokens for options in sent} == {expected}
    assert config.request_timeout == AgentConfig.request_timeout


# LLM: 回执与发送使用同一个上限计算入口，不以文案反推调用是否成功。
# 函数用途: 检查启动回执的 token 预算与实际配置一致，后台任务在测试里不启动。
def test_probe_receipt_uses_configured_cap(monkeypatch):
    config = AgentConfig(max_tokens=1000)
    config.reasoning_probe_max_output_tokens = 123
    target = probe.ProbeTarget("w4", config)
    agent = SimpleNamespace(home_paths=SimpleNamespace())
    monkeypatch.setattr(probe, "probe_records_path", lambda _home: "w4-test")
    monkeypatch.setattr(probe, "_save_record", lambda *args: None)
    monkeypatch.setattr(probe, "clear_host_notices", lambda *args: None)
    monkeypatch.setattr(probe, "_spawn", lambda _target: None)
    try:
        receipt = probe._start(agent, target, "manual")
        assert "每次输出上限 123 token" in receipt
        assert f"最多约 {probe._TOTAL_REQUESTS * 123} token" in receipt
    finally:
        probe._release("w4-test::w4")


# LLM: 独立进程先改权威默认再导入消费者，避免 reload 替换异常类和上下文对象污染其他测试。
# 函数用途: 反证兼容常量及函数默认确实由 AgentConfig 派生，而不是数值恰好相等。
def test_fallback_defaults_follow_changed_authority_in_fresh_process():
    code = '''
from agent_py_agent.agent.settings.config import AgentConfig
AgentConfig.input_media_max_bytes = 12345
AgentConfig.input_media_max_files = 3
AgentConfig.input_image_max_pixels = 67890
AgentConfig.feishu_personal_idle_lock_seconds = 456
AgentConfig.chat_history_max_turns = 7
from agent_py_agent.agent.conversation import input_media as media, input_media_images as images
from agent_py_agent.agent.session_lock import service
from agent_py_agent.cli.chat_parts import history
assert media.DEFAULT_MEDIA_BYTES == media.import_input_media.__kwdefaults__["max_bytes"] == 12345
assert media.validate_input_media.__kwdefaults__["max_files"] == 3
assert images.DEFAULT_IMAGE_PIXELS == images.inspect_input_image.__kwdefaults__["max_pixels"] == 67890
assert service.UnlockService.__init__.__kwdefaults__["idle_limit_seconds"] == 456
assert history.build_history_context.__kwdefaults__["max_turns"] == 7
assert history.append_conversation_turn.__kwdefaults__["max_turns"] == 7
print("authority-defaults-ok")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "authority-defaults-ok"


# LLM: 非正整数或非整数不是无限缓存；未传配置沿兼容默认，身份载荷不受容量影响。
# 函数用途: 覆盖缓存配置边界、无配置调用和跨进程载荷兼容。
@pytest.mark.parametrize("value", [None, 0, -1, True, "2", 2.5])
def test_activation_memo_invalid_values_keep_default(tmp_path, value):
    owner = resolve_owner_home(tmp_path)
    reference = activation.PluginActivationRef.from_owner(
        owner, "test", "a" * 64, config=SimpleNamespace(plugin_activation_memo_max_entries=value))
    restored = activation.PluginActivationRef.from_payload(reference.to_payload())
    assert reference._memo_limit == activation.ACTIVATION_AVAILABILITY_MEMO_MAX_ENTRIES
    assert restored == reference
    assert set(reference.to_payload()) == {"root", "identity", "scope"}


# LLM: 构造使用真实安装表与固定激活，不启动 Python/插件进程；仅隔离与本测试无关的老格式启动策略解析。
# 函数用途: 验证普通插件收到能力包专用超时配置时仍保持原 MCP 默认。
def test_non_pack_plugin_keeps_original_mcp_connect_timeout(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_activation_ref import plugin_reference

    _store, entry, _reference, owner = plugin_reference(tmp_path, phase="active")
    assert not entry.manifest.has_mcp_runtime
    environment = owner.plugins_dir / "environments" / entry.activation.plan.environment_ref
    (environment / "python" / "bin").mkdir(parents=True)
    monkeypatch.setattr(plugin_runtime, "legacy_launch_policy", lambda *_args: None)
    config = SimpleNamespace(plugin_pack_mcp_connect_timeout_seconds=2.5)
    client = plugin_runtime.PluginMCPClient(owner, entry, config=config)
    try:
        expected = plugin_runtime.MCPServerConfig(name="ordinary", command="unused").connect_timeout
        assert not client.lazy and not client.is_running()
        assert client.config.connect_timeout == expected
    finally:
        client.stop()


# LLM: 缺省探测仍受原档案输出上限限制，非法配置不得传 NaN、bool 或零到真实后端。
# 函数用途: 验证探测输出和超时的兼容兜底及非法值处理。
@pytest.mark.parametrize("value", [None, 0, -1, True, "8", 8.5, float("inf"), float("nan")])
def test_probe_invalid_config_preserves_bounded_defaults(value):
    config = AgentConfig(max_tokens=123, model_context_window_tokens=128000)
    config.reasoning_probe_max_output_tokens = value
    config.reasoning_probe_timeout_seconds = value
    assert probe._probe_output_cap(config) == 123
    expected_timeout = value if type(value) is float and value == 8.5 else 180.0
    assert probe._probe_timeout(config) == expected_timeout
