from __future__ import annotations

"""Step 3/4 测试：native 下 compact 对 IR「整对」增删 + 出站孤儿净化。

Step 3（compact 在 native 下整对操作 IR）：
- window（字符预算）回收最旧工具往返 → IR 整对摘除，出站 messages 无孤儿；
- PTL（provider 实报上下文超限）回收最旧一批 → IR 整对摘除，返回对数；
- 「文本条目 → tool_use id」映射（按 [tool-record round=N index=M] 标记）正确，
  据此整对摘除后无孤儿。

Step 4（出站孤儿净化 sweep，最后防线）：
- 孤儿 tool_use（有调用无结果）→ 补 stub tool_result，不删 assistant 文本；
- 孤儿 tool_result（指向不存在的 tool_use）→ 剔除，空 user 消息整条删；
- 已配对的 messages → 原样不动；
- 真实 to_provider_messages 出口默认带 sweep。

全程不依赖真实模型/网络。text 协议路径在既有套件中验证不变，这里只测 native 红线。
"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _ptl_reclaim_oldest,
    _record_tool_call,
    _window_native_ir_to_budget,
)
from agent_py_agent.agent.agent_core.tool_ir_compact import (
    compact_native_ir_to_char_budget,
    reclaim_oldest_native_ir_pairs,
    tool_use_ids_for_tool_records,
)
from agent_py_agent.agent.agent_core.tool_ir_history import drop_tool_call_pairs
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.message_adapter import (
    AnthropicMessageAdapter,
    strip_orphaned_tool_blocks,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall, ToolResult, UserTurn
from agent_py_agent.agent.tooling import ToolExecutionResult

# --- shared native fixtures (no archive/network side effects) -----------------


def _native_agent(root: Path, *, protocol: str = "native", backend: str = "anthropic_compatible"):
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,
            tool_output_externalize_min_chars=10_000_000,  # keep results inline
            tool_output_preview_chars=160,
        ),
        root=root,
        tools=SimpleNamespace(),
    )


def _params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="x",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="r",
        run_id="run",
        task_id="t",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        save=False,
        delivery_contract={},
    )


def _rec(agent, params, *, rnd, idx, cid, body):
    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params,
            tool_rounds=rnd,
            idx=idx,
            payload={"tool": "read_file", "call_id": cid, "path": f"{cid}.md"},
            result=ToolExecutionResult("read_file", True, body),
        ),
    )


def _message_block_ids(messages):
    """(tool_use ids, tool_result ids) actually present in provider messages."""
    tool_use = {b["id"] for m in messages for b in m["content"] if b["type"] == "tool_use"}
    tool_result = {b["tool_use_id"] for m in messages for b in m["content"] if b["type"] == "tool_result"}
    return tool_use, tool_result


def _assert_no_orphans(messages):
    tool_use, tool_result = _message_block_ids(messages)
    assert tool_use == tool_result, f"orphan! tool_use={tool_use} tool_result={tool_result}"


# === Step 3: window reclaim → IR integer-pair drop, no orphans ================


def test_window_reclaim_drops_oldest_ir_pairs_without_orphans(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    for i in range(1, 6):
        _rec(agent, params, rnd=i, idx=1, cid=f"toolu_{i}", body="X" * 2000)

    _assert_no_orphans(_native_provider_messages(agent, params))  # healthy before

    dropped = compact_native_ir_to_char_budget(params, max_chars=4500)

    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)  # the core red line: still paired after compact
    assert dropped >= 1
    tool_use, _ = _message_block_ids(messages)
    assert "toolu_1" not in tool_use  # oldest reclaimed
    assert "toolu_5" in tool_use  # newest preserved


def test_window_under_budget_is_noop(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_a", body="tiny")
    assert compact_native_ir_to_char_budget(params, max_chars=10_000_000) == 0
    _assert_no_orphans(_native_provider_messages(agent, params))


def test_window_keeps_at_least_newest_pair(tmp_path):
    # Even with an absurdly small budget, never drop the last remaining pair to zero.
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_only", body="X" * 5000)
    compact_native_ir_to_char_budget(params, max_chars=1)
    messages = _native_provider_messages(agent, params)
    tool_use, _ = _message_block_ids(messages)
    assert tool_use == {"toolu_only"}
    _assert_no_orphans(messages)


def test_tool_window_never_discards_current_turn_user_input(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_old", body="X" * 3000)
    params.tool_ir_history.append(UserTurn("用户刚补充的当前任务要求"))
    _rec(agent, params, rnd=2, idx=1, cid="toolu_new", body="Y" * 3000)

    compact_native_ir_to_char_budget(params, max_chars=100)

    assert any(
        isinstance(item, UserTurn) and item.text == "用户刚补充的当前任务要求"
        for item in params.tool_ir_history
    )
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    assert sum("用户刚补充" in str(message) for message in messages) == 1
    _assert_no_orphans(messages)


def test_window_via_loop_helper_is_gated_native_only(tmp_path):
    # text protocol: the native IR window helper must be a no-op even with stray IR.
    text_agent = _native_agent(tmp_path, protocol="text")
    text_params = _params()
    # seed stray IR by recording under a native agent, then feed the same list (mutated
    # in place; params is frozen so we extend rather than reassign).
    seed_agent = _native_agent(tmp_path)
    seed_params = _params()
    for i in range(1, 5):
        _rec(seed_agent, seed_params, rnd=i, idx=1, cid=f"toolu_{i}", body="X" * 30_000)
    text_params.tool_ir_history.extend(seed_params.tool_ir_history)
    before = len(text_params.tool_ir_history)
    _window_native_ir_to_budget(text_agent, text_params)
    assert len(text_params.tool_ir_history) == before  # text protocol: IR untouched


# === Step 3: PTL reclaim → IR integer-pair drop ==============================


def test_ptl_reclaim_drops_oldest_ir_pairs(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    for i in range(1, 6):
        _rec(agent, params, rnd=i, idx=1, cid=f"tu_{i}", body="BODY")

    dropped = reclaim_oldest_native_ir_pairs(params, fraction=0.2)

    assert dropped == 1  # max(1, 5*0.2)
    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert "tu_1" not in tool_use and "tu_5" in tool_use


def test_ptl_reclaim_empty_ir_returns_zero(tmp_path):
    # No tool round in IR -> 0 so the caller stops PTL retry and falls back to compact.
    params = _params()
    assert reclaim_oldest_native_ir_pairs(params, fraction=0.2) == 0


def test_ptl_loop_helper_native_drops_ir_text_drops_text(tmp_path):
    # native: _ptl_reclaim_oldest shrinks the IR (what the provider actually sees).
    agent = _native_agent(tmp_path)
    params = _params()
    for i in range(1, 4):
        _rec(agent, params, rnd=i, idx=1, cid=f"n_{i}", body="X" * 400)
    ir_pairs_before = sum(1 for it in params.tool_ir_history if isinstance(it, ToolResult))
    assert _ptl_reclaim_oldest(agent, params) is True
    ir_pairs_after = sum(1 for it in params.tool_ir_history if isinstance(it, ToolResult))
    assert ir_pairs_after < ir_pairs_before
    _assert_no_orphans(_native_provider_messages(agent, params))

    # text protocol: IR stays empty, text track is what gets reclaimed.
    text_agent = _native_agent(tmp_path, protocol="text")
    text_params = _params()
    for i in range(1, 4):
        _rec(text_agent, text_params, rnd=i, idx=1, cid=f"t_{i}", body="X" * 400)
    assert text_params.tool_ir_history == []  # text never builds IR
    # the text reclaim path returns True while there is reclaimable text body.
    assert _ptl_reclaim_oldest(text_agent, text_params) is True


# === Step 3: text-entry → tool_use id mapping (round/index join) ==============


def test_tool_use_ids_for_reclaimed_text_records(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="cid_A", body="A")
    _rec(agent, params, rnd=1, idx=2, cid="cid_B", body="B")  # same round, second call
    _rec(agent, params, rnd=2, idx=1, cid="cid_C", body="C")

    reclaimed = [e for e in params.tool_context if "round=1 index=2" in e or "round=2 index=1" in e]
    mapped = tool_use_ids_for_tool_records(params.tool_ir_history, reclaimed)
    assert mapped == {"cid_B", "cid_C"}

    # dropping by that mapping leaves the survivor paired, no orphan.
    drop_tool_call_pairs(params, mapped)
    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert tool_use == {"cid_A"}


def test_tool_use_id_mapping_ignores_unparseable_entries(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="cid_X", body="X")
    # entries with no [tool-record round= index=] marker map to nothing.
    assert tool_use_ids_for_tool_records(params.tool_ir_history, ["garbage", "[tool-system]\nfoo"]) == set()
    # out-of-range index is skipped, not mis-mapped.
    assert tool_use_ids_for_tool_records(params.tool_ir_history, ["[tool-record round=1 index=9]"]) == set()


# === Step 4: orphan sweep — stub for orphan tool_use =========================


def test_sweep_stubs_orphan_tool_use_without_dropping_assistant_text():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "推理过程"},
                {"type": "tool_use", "id": "tu_orphan", "name": "read_file", "input": {"path": "a"}},
            ],
        }
    ]
    out = strip_orphaned_tool_blocks(messages)

    # assistant text + tool_use preserved (we never delete assistant content).
    assert out[0]["role"] == "assistant"
    assert any(b["type"] == "text" and b["text"] == "推理过程" for b in out[0]["content"])
    assert any(b["type"] == "tool_use" and b["id"] == "tu_orphan" for b in out[0]["content"])
    # a stub tool_result was synthesized to pair the orphan tool_use.
    stub = out[1]
    assert stub["role"] == "user"
    assert stub["content"][0]["tool_use_id"] == "tu_orphan"
    assert stub["content"][0]["is_error"] is True
    _assert_no_orphans(out)


def test_sweep_stubs_only_missing_of_multiple_tool_use():
    # one tool_use has a real result, the sibling doesn't -> only the sibling gets a stub.
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "has_result", "name": "r", "input": {}},
                {"type": "tool_use", "id": "no_result", "name": "r", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "has_result", "content": "ok"}]},
    ]
    out = strip_orphaned_tool_blocks(messages)
    _assert_no_orphans(out)
    result_ids = [b["tool_use_id"] for m in out for b in m["content"] if b["type"] == "tool_result"]
    assert sorted(result_ids) == ["has_result", "no_result"]
    # exactly one synthesized stub (for no_result), the real one kept its content.
    real = [b for m in out for b in m["content"] if b.get("tool_use_id") == "has_result"][0]
    assert real["content"] == "ok"


# === Step 4: orphan sweep — strip orphan tool_result =========================


def test_sweep_strips_orphan_tool_result_and_removes_empty_user():
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]}]
    assert strip_orphaned_tool_blocks(messages) == []


def test_sweep_strips_only_orphan_result_keeps_sibling():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "real", "name": "r", "input": {}}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "real", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "ghost", "content": "drop me"},
            ],
        },
    ]
    out = strip_orphaned_tool_blocks(messages)
    _assert_no_orphans(out)
    result_ids = [b["tool_use_id"] for m in out for b in m["content"] if b["type"] == "tool_result"]
    assert result_ids == ["real"]  # ghost removed, real kept


def test_sweep_leaves_well_paired_messages_unchanged():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "r", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
    ]
    assert strip_orphaned_tool_blocks(messages) == messages


def test_sweep_handles_empty_and_blockless_messages():
    assert strip_orphaned_tool_blocks([]) == []
    plain = [{"role": "user", "content": "plain string prompt"}]
    assert strip_orphaned_tool_blocks(plain) == plain


# === Step 4: the sweep runs at the real out-bound boundary ====================
# (translation stays pure in to_provider_messages; the sweep is applied by
#  _native_provider_messages right before backend.generate.)


def test_outbound_boundary_stubs_orphan_tool_use(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    # seed a record then forge an orphan tool_use into the IR (e.g. a result lost to a
    # mid-round truncation that Step3's pair-drop didn't cover).
    _rec(agent, params, rnd=1, idx=1, cid="kept", body="ok")
    params.tool_ir_history.append(
        AssistantTurn(text="", tool_calls=[ToolCall(id="lonely", name="read_file", input={"path": "x"})])
    )

    messages = _native_provider_messages(agent, params)

    _assert_no_orphans(messages)  # boundary sweep paired the orphan
    assert any(
        b["type"] == "tool_result" and b["tool_use_id"] == "lonely"
        for m in messages
        for b in m["content"]
    )


def test_outbound_boundary_strips_orphan_tool_result(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="kept", body="ok")
    # forge a ToolResult that references no ToolCall (e.g. a stale resume fragment).
    params.tool_ir_history.append(ToolResult(tool_call_id="ghost", content="dangling", is_error=False))

    messages = _native_provider_messages(agent, params)

    _assert_no_orphans(messages)
    result_ids = {b["tool_use_id"] for m in messages for b in m["content"] if b["type"] == "tool_result"}
    assert "ghost" not in result_ids and "kept" in result_ids


def test_to_provider_messages_stays_pure_translation():
    # the translator itself must NOT sweep — isolated fragments map 1:1 (unit contract).
    history = [
        AssistantTurn(text="", tool_calls=[ToolCall(id="solo", name="read_file", input={"path": "x"})]),
    ]
    messages = AnthropicMessageAdapter().to_provider_messages(history)
    # no synthesized stub here: pure translation leaves the lone tool_use as-is.
    assert messages == [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "solo", "name": "read_file", "input": {"path": "x"}}]}
    ]
