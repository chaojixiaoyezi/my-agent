"""未知媒体不能拿引用长度证明容量；同模型推理与跨模型签名分别处理。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    candidate_input_modality_decision,
    compact_request_source_supported,
    filter_model_candidates_by_input_modality,
    text_request_capacity_known,
)
from agent_py_agent.agent.backends.request_content import (
    classify_nontext_content,
    compact_source_supported,
    text_messages_supported,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.conversation.input_media import import_input_media, input_media_root
from agent_py_agent.agent.settings.thread_model_selection import SUBAGENT_MODEL_ADVICE_KEY
from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import _params
from agent_py_agent.tests.test_subagent_first_request_selection import (
    _automatic_child,
    _install_automatic_provider,
)


@pytest.mark.parametrize("kind", ["thinking", "redacted_thinking"])
def test_same_model_reasoning_remains_text_estimable_but_not_cross_model_portable(kind):
    block = {"type": kind, "thinking" if kind == "thinking" else "data": "原签名推理文本", "signature": "signature"}
    prepared = ToolLoopRequestInput(tool_ir_history=(AssistantTurn(content_blocks=[block]),))
    assert text_request_capacity_known(prepared)
    assert not text_request_capacity_known(prepared, allow_reasoning=False)


# Responses 加密思考的 canonical 块（responses_wire 产出、message_adapter 回放的同一形态）。
RESPONSES_REASONING = {"type": "responses_reasoning", "model": "gpt-x", "item": {
    "type": "reasoning", "encrypted_content": "opaque-ciphertext", "summary": [{"type": "summary_text", "text": "要点"}]}}


def test_responses_reasoning_counts_as_same_model_reasoning_for_capacity_and_compaction():
    # 09-30 真实故障：GPT 会话带这种块时回合内从不自动压缩，被迫压缩又报 COMPACT_REQUEST_NON_TEXT。
    prepared = ToolLoopRequestInput(tool_ir_history=(
        AssistantTurn(content_blocks=[RESPONSES_REASONING, {"type": "text", "text": "先读文件"}]),))
    assert text_request_capacity_known(prepared)
    assert compact_request_source_supported(prepared, media_policy="off")
    assert not text_request_capacity_known(prepared, allow_reasoning=False)
    assert not compact_request_source_supported(prepared, media_policy="off", allow_reasoning=False)
    history = [{"role": "assistant", "content": [RESPONSES_REASONING, {"type": "text", "text": "好"}]}]
    assert text_messages_supported(history) and compact_source_supported(history, media_policy="off")
    assert classify_nontext_content(history).unknown == 0
    assert classify_nontext_content(history, allow_reasoning=False).unknown == 1


@pytest.mark.parametrize("broken", [
    {**RESPONSES_REASONING, "model": None},
    {**RESPONSES_REASONING, "item": {"type": "reasoning", "summary": []}},
    {**RESPONSES_REASONING, "item": {"type": "function_call", "encrypted_content": "x"}},
    {"type": "responses_reasoning", "model": "gpt-x"},
])
def test_malformed_responses_reasoning_stays_unknown(broken):
    history = [{"role": "assistant", "content": [broken]}]
    assert not text_messages_supported(history)
    assert not compact_source_supported(history, media_policy="archived_refs")
    assert classify_nontext_content(history).unknown == 1


@pytest.mark.parametrize("location", ["current", "prior", "assistant", "unknown"])
def test_media_or_unknown_content_is_not_text_capacity_proof(location):
    media = {"type": "image", "source": {"type": "local_file", "path": "not-read"}}
    prepared = ToolLoopRequestInput(
        tool_ir_history=(UserTurn("当前要求", media=({"path": "not-read"},)),) if location == "current" else
        (AssistantTurn(content_blocks=[media]),) if location == "assistant" else (),
        provider_history_messages=({"role": "user", "content": [media]},) if location == "prior" else
        ({"role": "user", "content": [{"type": "future_payload", "data": "unknown"}]},) if location == "unknown" else (),
    )
    assert not text_request_capacity_known(prepared)
    assert not text_request_capacity_known(prepared, allow_reasoning=False)
    assert not text_messages_supported([{"role": "user", "content": [{"type": "tool_result", "content": [media]}]}])


@pytest.mark.parametrize("kind,declared", [("image", ["text", "image"]), ("video", ["video"])])
def test_structured_media_requires_and_accepts_matching_declared_modality(kind, declared):
    ref = {"sha256": "a" * 64, "media_type": f"{kind}/test"}
    prepared = ToolLoopRequestInput(tool_ir_history=(UserTurn("检查附件", media=(ref,)),),
                                    provider_history_messages=())
    decision = candidate_input_modality_decision(prepared, declared)
    assert decision.status == "applicable"
    assert decision.required_modalities == (kind,)


def test_pure_text_needs_no_modality_declaration_but_missing_history_stays_unknown():
    text = ToolLoopRequestInput(tool_ir_history=(UserTurn("纯文字"),), provider_history_messages=())
    assert candidate_input_modality_decision(text, []).status == "applicable"
    unknown = candidate_input_modality_decision(ToolLoopRequestInput(), ["image", "video"])
    assert unknown.status == "unknown" and unknown.reason_code == "history_modality_unknown"


def test_all_candidates_without_required_modality_returns_structured_retain_hint():
    ref = {"sha256": "b" * 64, "media_type": "video/mp4"}
    prepared = ToolLoopRequestInput(tool_ir_history=(UserTurn("检查视频", media=(ref,)),),
                                    provider_history_messages=())
    result = filter_model_candidates_by_input_modality(prepared, {"plain": [], "image-only": ["image"]})
    assert result.applicable_profile_ids == ()
    assert result.reason_code == "no_candidate_supports_input_modalities"
    assert {row["reason_code"] for row in result.inapplicable.values()} == {
        "candidate_input_modalities_undeclared", "candidate_input_modalities_missing",
    }


@pytest.mark.parametrize("force", [False, True])
def test_native_compact_unknown_media_preserves_ir_before_summary_or_estimation(force):
    params = _params()
    params.tool_ir_history.append(UserTurn("完整图片要求", media=({"path": "never-read"},)))
    before = list(params.tool_ir_history)
    plan = _tool_loop_service._prepare_native_compact_plan(
        SimpleNamespace(), params, "prompt", estimator=lambda: pytest.fail("未知模态被拿去估计容量"), force=force,
    )
    assert plan is None and params.tool_ir_history == before


def _child_with_image(tmp_path, monkeypatch, *, input_modalities):
    values = {"input_modalities": input_modalities} if input_modalities is not None else None
    agent, task, key = _automatic_child(tmp_path, profile_values=values)
    source = tmp_path / "tiny.png"
    source.write_bytes(bytes.fromhex("89504e470d0a1a0a"))
    ref = import_input_media(source, input_media_root(agent))
    material = tmp_path / "material.txt"
    material.write_text("沿原模型完成工具读取。", encoding="utf-8")
    original = loop_support._tool_loop_execute_params

    def with_media(*args, **kwargs):
        params = original(*args, **kwargs)
        params.tool_ir_history[0] = replace(params.tool_ir_history[0], media=(ref,))
        return params

    monkeypatch.setattr(loop_support, "_tool_loop_execute_params", with_media)
    calls, probes = _install_automatic_provider(monkeypatch, agent, task, material)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    return agent, task, key, calls, probes, result


def test_child_image_adopts_candidate_that_declares_image(tmp_path, monkeypatch):
    agent, task, key, calls, probes, result = _child_with_image(
        tmp_path, monkeypatch, input_modalities=["image", "text"],
    )
    assert result.ok and len(calls) == 2
    assert {wire["model"] for wire, _ in calls} == {"MiniMax-M3"}
    assert {wire["model"] for wire in probes} == {"inherited", "MiniMax-M3"}
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == key
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "adopted"


def test_child_image_rejects_candidate_without_modality_declaration(tmp_path, monkeypatch):
    agent, task, _, calls, probes, result = _child_with_image(
        tmp_path, monkeypatch, input_modalities=None,
    )
    assert result.ok and len(calls) == 2
    assert {wire["model"] for wire, _ in calls} == {"inherited"}
    assert {wire["model"] for wire in probes} == {"inherited"}
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    advice = thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]
    assert thread.model_profile_id == "default"
    assert advice["status"] == "retained"
    assert advice["reason"] == "candidate_input_modalities_undeclared"
    assert advice["input_modality"] == {
        "status": "inapplicable", "reason_code": "candidate_input_modalities_undeclared",
        "required_modalities": ["image"], "declared_modalities": [], "missing_modalities": ["image"],
    }
