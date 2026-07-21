from __future__ import annotations

"""Step 2 集成测试：native 工具历史 → IR → 出站 Anthropic messages。

覆盖治本红线区的端到端数据流（不依赖真实模型/网络）：
1. 真实 ``_record_tool_call`` 路径在 native 下并行产 IR（与 tool_context 文本共存）；
2. 真实 provider tool_use id 透传：archive 用入站 id 覆盖合成 id，IR 用同一真实 id；
3. ``_native_provider_messages`` 把多轮 IR 翻成正确的 assistant(tool_use)/
   user(tool_result) 序列（真实 id 配对、连续结果合并、跨轮不串合）；
4. backend.generate(messages=...) 走结构化对话并保留原始 user prompt；text 协议零改动；
5. builder native 旁路：prompt 不再折入 [tool-record] 文本；
6. 子代理收口结果在 native 下落进 IR 历史。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.base import AnthropicCompatibleBackend, BackendOptions
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import ToolExecutionResult

# --- minimal native agent + params (no archive/network side effects) ---------


def _native_agent(root: Path, *, protocol: str = "native", backend: str = "anthropic_compatible"):
    """A stub agent whose config/backend make native_tool_use_active(...) == True."""
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,  # disable live archive writes for a clean unit
            tool_output_externalize_min_chars=10_000_000,  # keep results inline
            tool_output_preview_chars=160,
        ),
        root=root,
        tools=SimpleNamespace(),
    )


def _params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="分析两个文件并汇总",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="# Tools\n- read_file",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        save=False,
        delivery_contract={},
    )


def _record(agent, params, *, tool_rounds, idx, payload, result):
    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params,
            tool_rounds=tool_rounds,
            idx=idx,
            payload=payload,
            result=result,
        ),
    )


def _ok(tool: str, output: str) -> ToolExecutionResult:
    return ToolExecutionResult(tool, True, output)


def _err(tool: str, output: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(tool, False, output, error_code=code)


# --- 1+2+3: multi-round IR through the real _record_tool_call path ------------


def test_multi_round_native_history_produces_paired_merged_messages(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()

    # round 1: two read_file calls with real provider tool_use ids (toolu_*)
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "read_file", "call_id": "toolu_aaa", "path": "a.md"},
        result=_ok("read_file", "BODY-A"),
    )
    _record(
        agent, params, tool_rounds=1, idx=2,
        payload={"tool": "read_file", "call_id": "toolu_bbb", "path": "b.md"},
        result=_ok("read_file", "BODY-B"),
    )
    # round 2: one write_file call
    _record(
        agent, params, tool_rounds=2, idx=1,
        payload={"tool": "write_file", "call_id": "toolu_ccc", "path": "out.md"},
        result=_ok("write_file", "WROTE"),
    )

    messages = _native_provider_messages(agent, params)

    # assistant(2 tool_use) / user(2 tool_result merged) / assistant(1 tool_use) / user(1 tool_result)
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "user"]
    # round 1 assistant carries both tool_use blocks with the REAL provider ids
    assert [b["type"] for b in messages[0]["content"]] == ["tool_use", "tool_use"]
    assert [b["id"] for b in messages[0]["content"]] == ["toolu_aaa", "toolu_bbb"]
    assert [b["name"] for b in messages[0]["content"]] == ["read_file", "read_file"]
    # input is structured (no json string), control keys stripped
    assert messages[0]["content"][0]["input"] == {"path": "a.md"}
    # round 1 results merged into ONE user message, tool_use_id pairs the call id
    assert [b["tool_use_id"] for b in messages[1]["content"]] == ["toolu_aaa", "toolu_bbb"]
    assert messages[1]["content"][0]["content"].startswith("[tool=read_file; status=ok]")
    assert messages[1]["content"][0]["is_error"] is False
    # round 2 is a separate assistant/user pair — does NOT cross-merge with round 1
    assert messages[2]["content"][0]["id"] == "toolu_ccc"
    assert messages[3]["content"][0]["tool_use_id"] == "toolu_ccc"


def test_error_result_marks_is_error_true_in_messages(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "web_fetch", "call_id": "toolu_e", "url": "http://x"},
        result=_err("web_fetch", '{"error_code":"WEB_FETCH_FAILED"}', "WEB_FETCH_FAILED"),
    )

    messages = _native_provider_messages(agent, params)

    result_block = messages[1]["content"][0]
    assert result_block["tool_use_id"] == "toolu_e"
    assert result_block["is_error"] is True


def test_native_history_replays_provider_thinking_blocks_on_next_tool_round(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import open_assistant_turn_ir

    agent = _native_agent(tmp_path)
    params = _params()
    open_assistant_turn_ir(
        params,
        tool_rounds=1,
        response_text="我先读取。",
        response_content_blocks=[
            {"type": "thinking", "thinking": "先检查", "signature": "sig-1"},
            {"type": "text", "text": "我先读取。"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        ],
    )
    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        payload={
            "tool": "read_file",
            "call_id": "toolu_1",
            "path": "README.md",
        },
        result=_ok("read_file", "BODY"),
    )

    messages = _native_provider_messages(agent, params)

    assert messages[0]["content"][:2] == [
        {"type": "thinking", "thinking": "先检查", "signature": "sig-1"},
        {"type": "text", "text": "我先读取。"},
    ]
    assert messages[0]["content"][2]["id"] == "toolu_1"
    assert messages[1]["content"][0]["tool_use_id"] == "toolu_1"


def test_native_record_coexists_with_text_tool_context(tmp_path):
    # Gray-dual-track: IR history AND the legacy text tool_context both get populated.
    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "read_file", "call_id": "toolu_z", "path": "z.md"},
        result=_ok("read_file", "ZBODY"),
    )

    assert params.tool_ir_history, "native must populate IR history"
    assert any("[tool-record" in str(item) for item in params.tool_context), "text track must still be written"


# --- real id stamping back onto the result ------------------------------------


def test_archive_stamps_real_provider_id_onto_result_call_id(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    result = _ok("read_file", "B")
    assert result.call_id == ""  # starts synthetic-free
    _record(
        agent, params, tool_rounds=3, idx=2,
        payload={"tool": "read_file", "call_id": "toolu_real", "path": "p"},
        result=result,
    )
    # archive_tool_call_record must overwrite synthetic "3-2" with the real id.
    assert result.call_id == "toolu_real"
    # and the archive record carries the same real id.
    assert params.archive_tool_calls[-1]["call_id"] == "toolu_real"


# --- text protocol stays synthetic (gray-line: text path untouched) -----------


def test_text_protocol_keeps_synthetic_call_id_and_no_ir(tmp_path):
    agent = _native_agent(tmp_path, protocol="text")
    params = _params()
    result = _ok("read_file", "B")
    _record(
        agent, params, tool_rounds=3, idx=2,
        payload={"tool": "read_file", "call_id": "toolu_should_be_ignored", "path": "p"},
        result=result,
    )
    # text protocol: synthetic round-idx id, IR history stays empty.
    assert params.archive_tool_calls[-1]["call_id"] == "3-2"
    assert params.tool_ir_history == []
    assert _native_provider_messages(agent, params) is None


def test_openai_backend_uses_the_same_native_ir_history(tmp_path):
    agent = _native_agent(tmp_path, backend="openai_compatible")
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "read_file", "call_id": "toolu_x", "path": "p"},
        result=_ok("read_file", "B"),
    )
    assert len(params.tool_ir_history) == 2
    assert params.tool_ir_history[0].tool_calls[0].name == "read_file"
    assert params.tool_ir_history[1].tool_call_id == "toolu_x"
    assert params.archive_tool_calls[-1]["call_id"] == "toolu_x"


# --- 4: backend.generate(messages=...) wiring ---------------------------------


def test_anthropic_backend_keeps_initial_user_prompt_before_native_history():
    backend = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://api.example.com",
            api_key="k",
            model_name="claude-x",
            request_timeout=60,
            max_tokens=512,
            temperature=0.2,
            stream_enabled=False,
        )
    )
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "done"}]}

    backend.request_json = fake_request_json
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "A", "is_error": False}]},
    ]

    resp = backend.generate("SYSTEM+TASK PROMPT", messages=msgs)

    assert resp.text == "done"
    # The first request sent prompt as a user turn; every continuation preserves that identity.
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "SYSTEM+TASK PROMPT"},
        *msgs,
    ]
    assert "system" not in captured["payload"]


def test_anthropic_backend_without_messages_keeps_single_user_prompt():
    backend = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://api.example.com",
            api_key="k",
            model_name="claude-x",
            request_timeout=60,
            max_tokens=512,
            temperature=0.2,
            stream_enabled=False,
        )
    )
    captured: dict[str, object] = {}
    backend.request_json = lambda path, payload, headers: (captured.__setitem__("payload", payload) or {"content": [{"type": "text", "text": "ok"}]})

    backend.generate("PLAIN PROMPT")

    # text protocol path unchanged: one user message, no system field.
    assert captured["payload"]["messages"] == [{"role": "user", "content": "PLAIN PROMPT"}]
    assert "system" not in captured["payload"]


# --- 5: builder native bypass -------------------------------------------------


def _builder(tmp_path) -> PromptBuilder:
    config = AgentConfig()
    config.system_prompt = "SYS"
    return PromptBuilder(config, Path(tmp_path))


def test_builder_native_drops_tool_record_text(tmp_path):
    builder = _builder(tmp_path)
    tool_context = ['[tool-record round=1 index=1]\n{"tool":"read_file"}\n[tool-output-record round=1 index=1]\nSECRET-BODY']

    native_prompt = builder.build(
        "TASK",
        [],
        tools=ToolSections(tool_context=tool_context, native_tool_use=True),
    )
    text_prompt = builder.build(
        "TASK",
        [],
        tools=ToolSections(tool_context=tool_context, native_tool_use=False),
    )

    # native: the [tool-record] text body must NOT be folded into the prompt.
    assert "SECRET-BODY" not in native_prompt
    assert "tool-record" not in native_prompt
    # but the task is still present.
    assert "TASK" in native_prompt
    # text protocol: the transcript IS folded (unchanged behavior).
    assert "SECRET-BODY" in text_prompt


# --- 6: subagent closeout migrates to IR --------------------------------------


def test_subagent_closeout_appends_to_ir_history_when_native(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.round_subagent_output import (
        _record_subagent_result_ir_if_native,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    # simulate the write_file that wrote output.json already being in IR
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "write_file", "call_id": "toolu_w", "path": "output.json"},
        result=_ok("write_file", "WROTE"),
    )

    _record_subagent_result_ir_if_native(agent, params, "[SUBAGENT_RESULT]\n{...}\n[/SUBAGENT_RESULT]")

    # the closeout becomes the trailing assistant turn in IR history.
    assert isinstance(params.tool_ir_history[-1], AssistantTurn)
    assert params.tool_ir_history[-1].text.startswith("[SUBAGENT_RESULT]")
    # write_file call+result pair is still present and paired.
    assert any(isinstance(item, ToolResult) and item.tool_call_id == "toolu_w" for item in params.tool_ir_history)


def test_subagent_closeout_noop_for_text_protocol(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.round_subagent_output import (
        _record_subagent_result_ir_if_native,
    )

    agent = _native_agent(tmp_path, protocol="text")
    params = _params()
    _record_subagent_result_ir_if_native(agent, params, "[SUBAGENT_RESULT]\n{}")
    assert params.tool_ir_history == []


# --- compact prep: integer-pair drop interface (Step 3/4 contract) ------------


def test_drop_tool_call_pairs_removes_both_sides_no_orphans(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import drop_tool_call_pairs

    agent = _native_agent(tmp_path)
    params = _params()
    _record(
        agent, params, tool_rounds=1, idx=1,
        payload={"tool": "read_file", "call_id": "toolu_old", "path": "old"},
        result=_ok("read_file", "OLD"),
    )
    _record(
        agent, params, tool_rounds=2, idx=1,
        payload={"tool": "read_file", "call_id": "toolu_new", "path": "new"},
        result=_ok("read_file", "NEW"),
    )

    removed = drop_tool_call_pairs(params, {"toolu_old"})

    assert removed == 1
    messages = _native_provider_messages(agent, params)
    ids = [
        b.get("id") or b.get("tool_use_id")
        for m in messages
        for b in m["content"]
        if b["type"] in ("tool_use", "tool_result")
    ]
    # no dangling reference to the dropped pair; the newer pair survives intact.
    assert "toolu_old" not in ids
    assert ids.count("toolu_new") == 2  # tool_use + tool_result both present


def test_drop_tool_call_pair_discards_invalidated_thinking_signature(tmp_path):
    from agent_py_agent.agent.agent_core.tool_ir_history import (
        drop_tool_call_pairs,
        open_assistant_turn_ir,
    )

    agent = _native_agent(tmp_path)
    params = _params()
    open_assistant_turn_ir(
        params,
        tool_rounds=1,
        response_text="读取旧文件",
        response_content_blocks=[
            {"type": "thinking", "thinking": "旧推理", "signature": "signed-old"},
            {"type": "text", "text": "读取旧文件"},
            {
                "type": "tool_use",
                "id": "toolu_old",
                "name": "read_file",
                "input": {"path": "old"},
            },
        ],
    )
    _record(
        agent,
        params,
        tool_rounds=1,
        idx=1,
        payload={"tool": "read_file", "call_id": "toolu_old", "path": "old"},
        result=_ok("read_file", "OLD"),
    )

    drop_tool_call_pairs(params, {"toolu_old"})
    messages = _native_provider_messages(agent, params)

    assert messages == [
        {"role": "assistant", "content": [{"type": "text", "text": "读取旧文件"}]}
    ]
