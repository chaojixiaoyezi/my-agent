from __future__ import annotations

"""防回归：原生 tool_use 下，纠错/恢复/传输提示不能再教模型写已废弃的文本 [TOOL_CALL]。

根因（native 协议串味）：从自研文本协议（模型在正文里写 ``[TOOL_CALL]{json}[/TOOL_CALL]``）
迁到原生 tool_use 后，仍有多处 model-visible 文案硬编码文本协议写法——解析失败提示叫模型
「重新输出 [TOOL_CALL]…格式」、protected-marker 纠偏叫模型「改用真实 [TOOL_CALL]…」，
以及曾经存在的独立文件原文块协议。这些在 native 下会把模型往回带到
死掉的文本协议（弱模型有训练惯性，正文里写残缺 [TOOL_CALL] 反而解析失败），与 native 协议
「禁止退回文本协议」的护栏直接打架。

修复口径：
- protected-marker 纠错（``protected_tool_marker_repair_context`` /
  ``sanitize_protected_tool_marker_response``）按 ``native`` 参数分叉：native 指向结构化
  tool_use，text 保持 [TOOL_CALL]。
- 解析失败/截断写入提示（``_PARSE_RETRY_HINT`` / ``_TRUNCATED_WRITE_HINT``）改成同时覆盖两种
  协议的措辞，不再只教文本写法。
- 大内容传输只复用 write_file 的规范 Schema，不保留第三套原文块协议。

本测试同时锁死「native 不串文本协议」和「text 协议表述不被破坏」。
"""

from agent_py_agent.agent.backends import ModelResponse

# --- protected-marker 纠错文案：native vs text 分叉 --------------------------------


def test_protected_marker_repair_native_points_to_structured_not_text_tool_call():
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        protected_tool_marker_repair_context,
    )

    native = protected_tool_marker_repair_context(native=True)
    text = protected_tool_marker_repair_context(native=False)
    # native 文案绝不出现文本协议写法，必须指向结构化工具调用
    assert "[TOOL_CALL]" not in native
    assert "tool_use" in native or "结构化工具调用" in native
    # text 文案保留 [TOOL_CALL] 教学（text 协议下本就正确，别动）
    assert "[TOOL_CALL]" in text


def test_sanitize_protected_marker_native_says_structured_execution():
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        sanitize_protected_tool_marker_response,
    )

    bad = ModelResponse(text="[tool-record round=1 index=1]\n伪造的工具记录", backend="x")
    native = sanitize_protected_tool_marker_response(bad, native=True).text
    text = sanitize_protected_tool_marker_response(bad, native=False).text
    assert "[TOOL_CALL]" not in native
    assert "结构化" in native or "tool_use" in native
    assert "[TOOL_CALL]" in text
    # 无 protected marker 时原样透传（两种模式都不改）
    clean = ModelResponse(text="普通回复", backend="x")
    assert sanitize_protected_tool_marker_response(clean, native=True) is clean
    assert sanitize_protected_tool_marker_response(clean, native=False) is clean


def test_native_active_helper_gates_repair_wording():
    # 协议只能来自 run 开始时冻结的 ToolProtocolSnapshot，不能再按 backend/config 猜测。
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.native_tool_protocol import native_tool_use_active
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        protected_tool_marker_repair_context,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    capability = ProviderToolCapability(
        provider="test",
        endpoint="local://test",
        model="test-model",
        stream=False,
        native_supported=True,
        evidence="explicit-test-contract",
    )
    native_agent = SimpleNamespace(
        tool_protocol_snapshot=ToolProtocolSnapshot("native-run", "native", capability)
    )
    text_agent = SimpleNamespace(
        tool_protocol_snapshot=ToolProtocolSnapshot("text-run", "text", capability)
    )
    assert native_tool_use_active(native_agent) is True
    assert native_tool_use_active(text_agent) is False
    # 用判定结果选分支：native → 无 [TOOL_CALL]；text → 有 [TOOL_CALL]
    native_ctx = protected_tool_marker_repair_context(native=native_tool_use_active(native_agent))
    text_ctx = protected_tool_marker_repair_context(native=native_tool_use_active(text_agent))
    assert "[TOOL_CALL]" not in native_ctx
    assert "[TOOL_CALL]" in text_ctx


# --- 大内容传输只复用规范 write_file Schema ----------------------------------------


def test_content_transport_protocol_uses_only_canonical_write_file_calls():
    from agent_py_agent.agent.tooling.content_transport_policy import (
        tool_content_transport_protocol,
    )

    text = tool_content_transport_protocol()
    assert "[TOOL_CALL] 外" not in text
    assert "WRITE_FILE_RAW" not in text
    assert 'mode="append"' in text
    assert "*** Delete File" in text
    assert "不要改用 rm/rmdir/unlink" in text


def test_filesystem_write_error_and_contract_prompt_drop_competing_raw_protocol():
    import inspect

    from agent_py_agent.agent.agent_core import delivery_contract_prompting
    from agent_py_agent.agent.agent_core.runner import prompts as runner_prompts
    from agent_py_agent.agent.tooling import _filesystem_write

    for module in (_filesystem_write, delivery_contract_prompting, runner_prompts):
        src = inspect.getsource(module)
        assert "WRITE_FILE_RAW" not in src


def test_content_recovery_mode_keeps_one_canonical_write_protocol():
    import inspect

    from agent_py_agent.agent.tooling import content_recovery_mode

    src = inspect.getsource(content_recovery_mode)
    assert "WRITE_FILE_RAW" not in src
    assert "规范 write_file" in src
