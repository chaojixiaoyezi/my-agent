from __future__ import annotations

"""Step 5: native 下「文本工具调用 → 结构化 IR」严格门控提升 + 灰度回退。

覆盖修复网的关键不变量（不依赖真实模型/网络）：
1. native 下本轮无 tool_use block 但正文含合法 [TOOL_CALL] 且工具名命中注册 →
   提升为 run_tools、执行后进 IR、出站 messages 无孤儿（合成 id 配对）。
2. native 下正文是散文 / 工具名不命中 → **不**误提升（落回无工具路径）。
3. text 协议：完全不进这道闸（行为与 Step 5 之前一字不变）—— 灰度回退安全网。
4. guard 短路（执行前被拦）的文本兜底调用 call_id 为空 → IR 回填合成 id，无空 id 孤儿。
5. 观测埋点：提升/拒绝各自计数（长期助手 风格泄漏检测，不引入重试）。
"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling import ToolExecutionResult

# --- fakes that drive native_tool_use_active() deterministically -------------


class _FakeRegistry:
    """Minimal registry: parse_tool_calls echoes a scripted result; .tools is the
    registered-name source the strict gate consults."""

    def __init__(self, parsed, registered_names):
        self._parsed = parsed
        # native_tool_protocol / promotion gate read registry.tools (dict of names).
        self.tools = {name: object() for name in registered_names}
        self.parsed_texts: list[str] = []

    def parse_tool_calls(self, text: str):
        self.parsed_texts.append(text)
        return list(self._parsed) if "[TOOL_CALL]" in text else []


def _agent(root: Path, *, parsed, registered, protocol="native", backend="anthropic_compatible"):
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,
            tool_output_externalize_min_chars=10_000_000,
            tool_output_preview_chars=160,
        ),
        root=root,
        tools=_FakeRegistry(parsed, registered),
    )


def _params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="t",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
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


def _decide(agent, response: ModelResponse):
    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=_params(),
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )


_TEXT_CALL = '[TOOL_CALL]\n{"tool":"read_file","path":"a.md"}\n[/TOOL_CALL]'


# --- 1: registered tool name promotes ----------------------------------------


def test_native_promotes_text_call_when_tool_registered(tmp_path: Path):
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "read_file", "path": "a.md"}],
        registered={"read_file", "write_file"},
    )
    response = ModelResponse(text=_TEXT_CALL, backend="anthropic_compatible")

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "read_file", "path": "a.md"}]
    # observability: a promotion was recorded, no rejection.
    assert getattr(response, "native_text_tool_call_promotions", 0) == 1
    assert getattr(response, "native_text_tool_call_rejections", 0) == 0


# --- 2a: prose / parse-error block does NOT promote --------------------------


def test_native_does_not_promote_parse_error_sentinel(tmp_path: Path):
    # parse_tool_calls returns the __parse_error__ sentinel for a malformed block.
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "__parse_error__", "raw": "..."}],
        registered={"read_file"},
    )
    response = ModelResponse(text=_TEXT_CALL, backend="anthropic_compatible")

    decision = _decide(agent, response)

    # not promoted -> no tool calls -> plain break on this prose-only turn.
    assert decision.action == "break"
    assert decision.calls == []
    assert getattr(response, "native_text_tool_call_rejections", 0) == 1
    assert getattr(response, "native_text_tool_call_promotions", 0) == 0


# --- 2b: unregistered tool name does NOT promote -----------------------------


def test_native_does_not_promote_unregistered_tool_name(tmp_path: Path):
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "definitely_not_a_tool", "x": 1}],
        registered={"read_file", "write_file"},
    )
    response = ModelResponse(text=_TEXT_CALL, backend="anthropic_compatible")

    decision = _decide(agent, response)

    assert decision.action == "break"
    assert decision.calls == []
    assert getattr(response, "native_text_tool_call_rejections", 0) == 1


def test_native_rejects_batch_if_any_call_unregistered(tmp_path: Path):
    # all-or-nothing: one bad name in the batch blocks the whole promotion.
    agent = _agent(
        tmp_path,
        parsed=[
            {"tool": "read_file", "path": "a.md"},
            {"tool": "made_up_tool", "y": 2},
        ],
        registered={"read_file"},
    )
    response = ModelResponse(text=_TEXT_CALL, backend="anthropic_compatible")

    decision = _decide(agent, response)

    assert decision.action == "break"
    assert decision.calls == []


# --- 3: text protocol is NOT gated (gray rollback safety net) -----------------


def test_text_protocol_does_not_apply_promotion_gate(tmp_path: Path):
    # Even an unregistered/parse-error name passes through untouched on text protocol:
    # the gate is native-only; text relies on the existing __parse_error__ repair loop.
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "__parse_error__", "raw": "x"}],
        registered={"read_file"},
        protocol="text",
    )
    response = ModelResponse(text=_TEXT_CALL, backend="anthropic_compatible")

    decision = _decide(agent, response)

    # text path forwards parsed calls verbatim (run_tools), no gate, no counters.
    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "__parse_error__", "raw": "x"}]
    assert not hasattr(response, "native_text_tool_call_rejections")


def test_openai_native_backend_applies_promotion_gate(tmp_path: Path):
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "__parse_error__", "raw": "x"}],
        registered={"read_file"},
        backend="openai_compatible",
    )
    response = ModelResponse(text=_TEXT_CALL, backend="openai_compatible")

    decision = _decide(agent, response)

    assert decision.action == "break"
    assert decision.calls == []


def test_non_native_backend_does_not_apply_promotion_gate(tmp_path: Path):
    agent = _agent(
        tmp_path,
        parsed=[{"tool": "__parse_error__", "raw": "x"}],
        registered={"read_file"},
        backend="echo",
    )
    response = ModelResponse(text=_TEXT_CALL, backend="echo")

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "__parse_error__", "raw": "x"}]


def test_structured_tool_use_blocks_skip_promotion_gate(tmp_path: Path):
    # When the model DID emit native tool_use, text parsing/gate must not run at all.
    agent = _agent(tmp_path, parsed=[{"tool": "read_file", "path": "x"}], registered={"read_file"})
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}}],
    )

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert decision.calls[0]["call_id"] == "toolu_1"
    # text protocol parse was never consulted.
    assert agent.tools.parsed_texts == []
    assert not hasattr(response, "native_text_tool_call_promotions")


# --- 1 (end-to-end): promoted text call enters IR paired, no orphans ----------


def _record(agent, params, *, tool_rounds, idx, payload, result):
    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params, tool_rounds=tool_rounds, idx=idx, payload=payload, result=result
        ),
    )


def test_promoted_text_call_enters_ir_with_synthetic_id_no_orphan(tmp_path: Path):
    agent = _agent(tmp_path, parsed=[{"tool": "read_file", "path": "a.md"}], registered={"read_file"})
    params = _params()

    # text-fallback payload has NO call_id; normal execution stamps result.call_id.
    result = ToolExecutionResult("read_file", True, "BODY-A", call_id="round-1-tool-1")
    _record(agent, params, tool_rounds=1, idx=1, payload={"tool": "read_file", "path": "a.md"}, result=result)

    messages = _native_provider_messages(agent, params)
    use_ids = [b["id"] for m in messages for b in m["content"] if b.get("type") == "tool_use"]
    res_ids = [b["tool_use_id"] for m in messages for b in m["content"] if b.get("type") == "tool_result"]
    assert use_ids == ["round-1-tool-1"]
    assert res_ids == ["round-1-tool-1"]  # paired, no orphan


# --- 4: guard short-circuit (empty call_id) gets a synthetic id, no orphan -----


def test_guard_short_circuited_text_call_gets_synthetic_id_no_empty_orphan(tmp_path: Path):
    """A text-fallback call blocked by a guard BEFORE execution yields a result with
    empty call_id (no envelope attached). It must still enter IR with a NON-empty,
    paired id so no empty-id orphan reaches the adapter (which would strip the result
    and dangle the tool_use -> Anthropic 400).

    Production path: ``archive_tool_call_record`` runs before IR recording and stamps
    ``result.call_id`` with the canonical archive synthetic ``{round}-{idx}`` when no
    real provider id exists; IR picks that up. The ``round-N-tool-idx`` back-fill in
    ``_record_tool_call_ir_if_native`` is a defense-in-depth net for the case archive
    leaves it empty.  Either way: no empty id ever pairs into the messages."""
    agent = _agent(tmp_path, parsed=[{"tool": "read_file", "path": "a.md"}], registered={"read_file"})
    params = _params()

    # guard-produced result: ok=False, call_id="" (no envelope was attached).
    blocked = ToolExecutionResult("read_file", False, "BLOCKED_BY_BUDGET")
    assert blocked.call_id == ""
    _record(agent, params, tool_rounds=2, idx=3, payload={"tool": "read_file", "path": "a.md"}, result=blocked)

    messages = _native_provider_messages(agent, params)
    use_ids = [b["id"] for m in messages for b in m["content"] if b.get("type") == "tool_use"]
    res_ids = [b["tool_use_id"] for m in messages for b in m["content"] if b.get("type") == "tool_result"]
    # archive synthetic {round}-{idx} stamped onto the result -> paired in messages.
    assert use_ids == ["2-3"]
    assert res_ids == ["2-3"]
    # crucially: no empty-id block anywhere (the orphan failure mode is impossible).
    assert "" not in use_ids
    assert "" not in res_ids


def test_empty_id_back_fill_when_archive_does_not_stamp(tmp_path: Path):
    """Direct unit on the defense-in-depth back-fill: call the IR helper in isolation
    with a result whose call_id is empty and a payload without call_id. The IR pair
    must carry the round-N-tool-idx synthetic id rather than an empty id."""
    from agent_py_agent.agent.agent_core._tool_loop_service import _record_tool_call_ir_if_native

    agent = _agent(tmp_path, parsed=[], registered={"read_file"})
    params = _params()
    result = ToolExecutionResult("read_file", True, "BODY")  # call_id stays ""
    _record_tool_call_ir_if_native(
        agent,
        ToolCallRecordParams(params=params, tool_rounds=5, idx=2, payload={"tool": "read_file"}, result=result),
        {"tool": "read_file"},
        "rendered result",
    )

    messages = _native_provider_messages(agent, params)
    use_ids = [b["id"] for m in messages for b in m["content"] if b.get("type") == "tool_use"]
    res_ids = [b["tool_use_id"] for m in messages for b in m["content"] if b.get("type") == "tool_result"]
    assert use_ids == ["round-5-tool-2"]
    assert res_ids == ["round-5-tool-2"]
