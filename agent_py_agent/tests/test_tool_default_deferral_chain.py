from __future__ import annotations

"""按工具声明默认收起的真实链路用例（只有供应商传输是脚本）。

真实 SimpleAgent + 真实 Gateway 前台 ask：脚本化模型先 tool_search，再调用被收起的工具，最后回复。
核对每次请求实际带了哪些原生工具：开关打开时首个请求不带收起的工具，tool_search 之后的那次请求带上并真正执行；
开关关闭时首个请求照旧直接带它。本机管理员和飞书普通用户两种 owner 都走一遍。
"""

import itertools
import json
import re
import time
from dataclasses import dataclass, field

import pytest

from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.tests.test_session_task_real_chain import (
    RealChain,
    _agent_config,
    _ready_gateway_paths,
    _text_of,
)

_PROMPT = "RC-DEFER 帮我看一下现在有哪些定时任务"


_SEARCH_THEN_CALL = (("tool_search", {"query": "schedule 定时任务", "limit": 3}), ("schedule", {"action": "list"}))
# 盲调：不先 tool_search、也不带必填参数直接调用收起的工具，宿主在参数校验阶段拒绝后应把完整定义带进下一次请求。
_BLIND_THEN_RETRY = (("schedule", {}), ("schedule", {"action": "list"}))


# 类用途: 进程内的供应商假线路：每个前台回合按调用次序出招（默认 tool_search → schedule → 回复），记下每次请求带的工具名。
class _DeferralWire:
    def __init__(self, script=_SEARCH_THEN_CALL) -> None:
        self._ids = itertools.count(1)
        self._script = script
        self.calls: list[tuple[str, ...]] = []
        self.tool_results: list[dict] = []

    # 函数用途: 替换 backends.http.post_json；探测请求照实回答，前台回合按次序出招。
    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        gateway_helpers._emit_provider_attempt({"attempt_id": f"td-{next(self._ids)}", "status": "started"})
        names = tuple(str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or [])
        if names == ("my_agent_capability_probe",):
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return self._reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}))
        messages = [message for message in payload.get("messages") or [] if isinstance(message, dict)]
        if not any("RC-DEFER" in str(message.get("content") or "") for message in messages):
            return self._reply(payload, "好的。", None)
        self.calls.append(names)
        self.tool_results = [message for message in messages if message.get("role") == "tool"]
        step = len(self.calls)
        if step <= len(self._script):
            return self._reply(payload, None, self._script[step - 1])
        return self._reply(payload, "目前没有定时任务。", None)

    # 函数用途: 把脚本动作包装成 OpenAI chat.completion 响应。
    def _reply(self, payload: dict, text: str | None, call: tuple[str, dict] | None) -> dict:
        message: dict = {"role": "assistant", "content": text}
        finish = "stop"
        if call is not None:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_td_{next(self._ids)}", "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)},
            }]}
            finish = "tool_calls"
        return {"id": f"chatcmpl-td-{next(self._ids)}", "object": "chat.completion", "created": int(time.time()),
                "model": str(payload.get("model") or ""),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}}


# 类用途: 一次链路用例的设置：开关、脚本化模型的出招序列、owner 身份（默认本机管理员）。
@dataclass(frozen=True)
class _Setup:
    deferral: bool
    script: tuple = _SEARCH_THEN_CALL
    owner: dict = field(default_factory=dict)


# 函数用途: 搭一个只有一个会话的真实前台环境。
def _chain(tmp_path, monkeypatch, setup: _Setup) -> tuple[RealChain, _DeferralWire]:
    config = _agent_config(tmp_path, **setup.owner)
    config.tool_default_deferral_enabled = setup.deferral
    agent = SimpleAgent(config, tmp_path / "root")
    wire = _DeferralWire(setup.script)
    monkeypatch.setattr(http, "post_json", wire)
    chain = RealChain(agent, _ready_gateway_paths(agent), wire, {}, scheduler=None)
    chain.open_session("A")
    return chain, wire


_OWNERS = {
    "local-main": {},
    "feishu-user": {"my_agent_owner_provider": "feishu", "my_agent_owner_kind": "user", "my_agent_owner_id": "td-feishu"},
}


@pytest.mark.parametrize("owner", sorted(_OWNERS))
def test_deferred_tool_is_found_loaded_and_executed_in_one_search(tmp_path, monkeypatch, owner):
    chain, wire = _chain(tmp_path, monkeypatch, _Setup(True, owner=_OWNERS[owner]))
    chain.ask("A", _PROMPT)

    assert len(wire.calls) == 3, wire.calls
    first, second, _final = wire.calls
    assert "schedule" not in first and "tool_search" in first, "收起后首个请求不带 schedule，但有 tool_search"
    assert "schedule" in second, "tool_search 之后的下一次请求带上 schedule 的完整定义"
    heads = [_text_of(message.get("content")).split("\n", 1)[0] for message in wire.tool_results]
    assert len(heads) == 2, heads
    assert "tool=tool_search; status=succeeded" in heads[0]
    assert "tool=schedule; status=succeeded; handler_executed=true" in heads[1], "schedule 真的执行并把结果交回模型"


def test_switch_off_keeps_the_tool_in_the_first_request(tmp_path, monkeypatch):
    chain, wire = _chain(tmp_path, monkeypatch, _Setup(False))
    chain.ask("A", _PROMPT)
    assert "schedule" in wire.calls[0]


def test_blind_call_of_a_deferred_tool_gets_its_schema_on_the_next_request(tmp_path, monkeypatch):
    chain, wire = _chain(tmp_path, monkeypatch, _Setup(True, _BLIND_THEN_RETRY))
    chain.ask("A", _PROMPT)

    assert len(wire.calls) == 3, wire.calls
    first, second, _final = wire.calls
    assert "schedule" not in first, "收起后首个请求不带 schedule"
    assert "schedule" in second, "盲调在参数校验阶段失败后，下一次请求带上 schedule 的完整定义"
    heads = [_text_of(message.get("content")) for message in wire.tool_results]
    assert len(heads) == 2, heads
    assert "tool=schedule; status=failed" in heads[0].split("\n", 1)[0]
    assert "[tool-schema-loaded] schedule " in heads[0]
    assert "tool=schedule; status=succeeded; handler_executed=true" in heads[1].split("\n", 1)[0]
