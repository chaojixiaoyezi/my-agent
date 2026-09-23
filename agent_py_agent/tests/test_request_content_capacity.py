"""未知媒体不能拿引用长度证明容量；同模型推理与跨模型签名分别处理。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    text_request_capacity_known,
)
from agent_py_agent.agent.backends.request_content import text_messages_supported
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


@pytest.mark.parametrize("force", [False, True])
def test_native_compact_unknown_media_preserves_ir_before_summary_or_estimation(force):
    params = _params()
    params.tool_ir_history.append(UserTurn("完整图片要求", media=({"path": "never-read"},)))
    before = list(params.tool_ir_history)
    plan = _tool_loop_service._prepare_native_compact_plan(
        SimpleNamespace(), params, "prompt", estimator=lambda: pytest.fail("未知模态被拿去估计容量"), force=force,
    )
    assert plan is None and params.tool_ir_history == before


def test_child_media_keeps_inherited_model_without_extra_candidate_probe(tmp_path, monkeypatch):
    agent, task, _ = _automatic_child(tmp_path)
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
    calls, _ = _install_automatic_provider(
        monkeypatch, agent, task, material,
        before_candidate_probe=lambda: pytest.fail("媒体未知时不应探测候选模型"),
    )
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok and len(calls) == 2
    assert {wire["model"] for wire, _ in calls} == {"inherited"}
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == "default"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "history_modality_unknown"
