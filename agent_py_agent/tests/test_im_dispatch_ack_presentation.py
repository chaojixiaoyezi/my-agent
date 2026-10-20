# LLM: 沿原派工回执 producer、表达参数和 provider serializer 验证既有 presentation_only 事实；HTTP 叶只返回合成文本，不执行工具或业务，也不推断真实模型遵循。
# 模块用途: 保证接单表达轮说明自身零工具的原因，并保留真实任务状态、原优先级及无回执时的零副作用。
import copy
import json

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    natural_user_reply_model_params,
    pending_natural_user_reply,
    queue_natural_user_reply,
)
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.conversation import im_delegation
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

_TASK = "synthetic-dispatch-task-617"
_BUSINESS = "原主轮业务由已派工任务执行"


# LLM: 只读取本测试的公开合成 provider payload，不扫描环境或日志，也不作为产品路由依据。
# 函数用途: 从两种 provider 原消息形状取出文本，以检查实际 serializer 的出站事实。
def _message_texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _message_texts(item)]
    if isinstance(value, dict):
        return [text for key, item in value.items() if key in {"content", "text"}
                for text in _message_texts(item)]
    return []


# LLM: 解码仅限本测试已知 natural-user-reply 合同的 JSON 行，不能把回复正文变为机器权威。
# 函数用途: 定位实际 wire 中唯一结构化表达请求，避免只测内部 prompt 诊断副本。
def _wire_fact_packets(payload):
    marker = "[natural-user-reply]\n"
    return [json.loads(text.split(marker, 1)[1].split("\n", 1)[0])
            for text in _message_texts(payload["messages"]) if marker in text]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("status", ["queued", "accepted", "done", "failed", "cancelled"])
def test_dispatch_presentation_fact_reaches_native_wire_without_business_execution(
    tmp_path, monkeypatch, provider, status,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", prompt_files=[], enable_plugins=False,
                    subagent_workspace="subs", my_agent_home=str(tmp_path / "home")),
        tmp_path / "workspace",
    )
    options = BackendOptions(
        api_base="https://synthetic.invalid/v1", api_key="synthetic-only",
        model_name="synthetic-model", max_tokens=128, stream_enabled=False,
    )
    backend = (AnthropicCompatibleBackend(options) if provider == "anthropic"
               else OpenAICompatibleBackend(options))
    captured = []

    def request_json(path, payload, headers):
        del path, headers
        captured.append(copy.deepcopy(payload))
        if provider == "anthropic":
            return {"content": [{"type": "text", "text": "收到。"}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
        return {"choices": [{"message": {"content": "收到。"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(backend, "request_json", request_json)
    agent.backend = backend
    business_calls = []

    def reject_business(*args, **kwargs):
        business_calls.append((args, kwargs))
        raise AssertionError("派工表达轮不得执行工具或再次准备业务")

    monkeypatch.setattr(agent.tools, "execute_tool", reject_business)
    monkeypatch.setattr(im_delegation, "prepare_delegated_execution", reject_business)
    attrs = {"host_im_task_ack": {"task_id": _TASK, "status": status}}
    original_ir = [UserTurn("# User Task\n" + _BUSINESS)]
    params = _tool_loop_params(
        user_prompt=_BUSINESS, root_user_prompt=_BUSINESS,
        allowed_tools=["tool_search"], tool_ir_history=list(original_ir),
        task_attributes=copy.deepcopy(attrs), live_archive_state={},
        tool_runtime_snapshot=agent.tools.runtime_snapshot(
            run_id="main-run-1", allowed_tools=["tool_search"],
        ),
    )
    assert params.tool_runtime_snapshot.runtimes
    im_delegation.prepare_dispatch_ack(params)
    phase = pending_natural_user_reply(params)
    expected = {
        **attrs["host_im_task_ack"], "current_user_request": _BUSINESS,
        "reply_is_interim": True,
        "task_continues_without_more_user_input": status not in {"done", "failed", "cancelled"},
        "current_turn_role": "dispatch_acknowledgement",
        "worker_execution_outcome": "not_observed_in_this_turn",
        "coordinator_role": "本片仅确认宿主派工，不评估孩子的业务能力；无工具表达轮不能证明孩子无法执行。",
        "turn_execution": {"presentation_only": True},
    }
    projected = natural_user_reply_model_params(params)
    assert projected is not params and projected.allowed_tools == []
    assert len(projected.tool_ir_history) == 1
    assert params.tool_ir_history == original_ir and params.task_attributes == attrs
    # 不预置 Compact/已提交候选；正常首轮会再次观察同一 pending 并走原表达请求入口。
    outcome = service.next_tool_loop_model_response(agent, params, 0)
    assert len(captured) == 1 and business_calls == []
    assert not captured[0].get("tools")
    packets = _wire_fact_packets(captured[0])
    assert len(packets) == 1
    wire_text = "\n".join(_message_texts(captured[0]["messages"]))
    assert "这是已由宿主耐久派工后的接单回执" in wire_text
    assert "不要让用户等待本回执下一轮获得工具" in wire_text
    assert "不能据此声称后台已经启动、正在执行或任务已经成功" in wire_text
    assert packets[0]["reply_kind"] == "background_dispatch"
    assert packets[0]["facts"] == expected
    assert phase["facts"] == expected
    assert pending_natural_user_reply(params) is phase
    assert outcome.response.text == "收到。" and not outcome.response.tool_use_blocks
    assert params.allowed_tools == ["tool_search"]
    assert params.executed_tools == [] and params.archive_tool_calls == []
    assert params.tool_ir_history == original_ir
    assert params.task_attributes == {
        **attrs, "runtime_tool_output_archive_root": str(runtime_owner_root(agent).resolve()),
    }


@pytest.mark.parametrize("attributes", [{}, {"host_im_task_ack": None}, {"host_im_task_ack": {}}])
def test_missing_dispatch_ack_has_no_presentation_side_effects(attributes):
    state = {"existing_fact": {"value": True}}
    params = _tool_loop_params(task_attributes=copy.deepcopy(attributes), live_archive_state=state)
    before_state = copy.deepcopy(state)
    before_attrs = copy.deepcopy(params.task_attributes)
    im_delegation.prepare_dispatch_ack(params)
    assert state == before_state and params.task_attributes == before_attrs
    assert pending_natural_user_reply(params) is None
    assert natural_user_reply_model_params(params) is params
    assert params.executed_tools == [] and params.archive_tool_calls == []


def test_existing_pending_reply_keeps_its_original_facts_and_priority():
    params = _tool_loop_params(
        task_attributes={"host_im_task_ack": {"task_id": _TASK, "status": "queued"}},
        live_archive_state={},
    )
    queue_natural_user_reply(params, kind="input_wait", facts={"request_id": "input-617"})
    phase = pending_natural_user_reply(params)
    before = copy.deepcopy(phase)
    im_delegation.prepare_dispatch_ack(params)
    assert pending_natural_user_reply(params) is phase and phase == before
    assert phase["facts"] == {"request_id": "input-617"}
    assert params.executed_tools == [] and params.archive_tool_calls == []
