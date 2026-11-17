"""A foreground IM turn whose first tool is send_message binds its conversation task first and delivers once."""
from __future__ import annotations

import json

from agent_py_agent.agent.backends import http
from agent_py_agent.tests.test_im_delegation_single_speaker import ask, ordinary_chain
from agent_py_agent.tests.test_session_task_real_chain import ScriptedWire


# 函数用途: 第一次采样直接调用 send_message，看到工具结果后给出最终回复；记录工具结果供断言。
def _first_tool_send_wire(results):
    def wire(request):
        payload = json.loads(json.dumps(request.payload))
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            return ScriptedWire()(request)
        tool_rows = [row for row in payload.get("messages") or [] if row.get("role") == "tool"]
        if not tool_rows:
            call = {"id": "call_first_send", "type": "function",
                    "function": {"name": "send_message", "arguments": json.dumps({"message": "outbound text check"})}}
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]},
                                 "finish_reason": "tool_calls"}]}
        results.append(str(tool_rows[-1].get("content") or ""))
        return {"choices": [{"message": {"role": "assistant", "content": "已发送。"}, "finish_reason": "stop"}]}

    return wire


# 函数用途: 本人私聊普通回合首个工具就是 send_message 时，消息应恰好发出一次，工具结果不能是结果未知。
def test_foreground_im_turn_sends_when_send_message_is_the_first_tool(tmp_path, monkeypatch):
    agent, paths, provider, calls, _blocked = ordinary_chain(tmp_path, monkeypatch)
    results = []
    monkeypatch.setattr(http, "post_json", _first_tool_send_wire(results))
    ask(agent, paths, "first-tool-send", "请直接给我发一条消息")
    assert calls and calls[-1]["actor"] is not None
    assert provider.messages == [("ou_a", "outbound text check")], results
    assert results and "TOOL_OPERATION_OUTCOME_UNKNOWN" not in results[-1]
