"""前台诚实失败后，后台收尾的回复仍送达 TUI（2026-10-01，G01 待定项核对）。

真机（ae 的 G01，gpt-6-luna）：前台请求在子代理还在跑时两次只有思考、没有正文，宿主有界补问后以
USER_REPLY_UNAVAILABLE 诚实失败；之后子代理结束，父级后台续跑写完报告、TaskRun 以 done 关闭。当时没记录这条后台回复
是否送到用户。隔离网关加脚本化假模型复现确认已送达；这里用真实 echo Gateway、真实工具循环、真实后台调度器和
agent 自己的 delivery_service 钉住：
1. 前台只有思考没有正文 → 请求以 USER_REPLY_UNAVAILABLE 失败，线程里没有前台的助手回复；
2. 子代理结束的 wake 由后台调度器跑出父级最终回复：投递原因 root_subagents_terminal、落到 canonical 会话记录，
   并出现在 TUI 轮询的后台消息页（read_background_response_page，Gateway 的后台通知接口读的就是它）。
"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.runtime import COMPLETION_COALESCE_WINDOW_SECONDS
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.settings.config import AgentConfig

_SESSION = "fg-fail-bg-reply"
_REPORT = "后台汇总：子任务已经完成，结果已写好。"


def _thinking_only(self, prompt, *args, **kwargs):
    # 只有思考、没有可见正文：工具循环有界补问后以 USER_REPLY_UNAVAILABLE 诚实失败（不是供应商级空响应）。
    return ModelResponse(text="", backend="echo",
                         assistant_content_blocks=[{"type": "thinking", "thinking": "还在等子代理，先不回复。"}])


def _report(self, prompt, *args, **kwargs):
    return ModelResponse(text=_REPORT, backend="echo")


@pytest.fixture
def gateway(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False, orphan_supervision_interval_seconds=0),
                        tmp_path / "ws")
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return agent, paths


def _ask(agent, paths, prompt: str) -> tuple[str, dict]:
    request_id, _request_path, response_path = submit_gateway_ask(
        paths, params=GatewayAskParams(prompt=prompt, save=False, chat_session_id=_SESSION, agent=agent))
    _process_gateway_requests(agent, paths)
    return request_id, json.loads(response_path.read_text(encoding="utf-8"))


def test_background_completion_after_failed_foreground_reaches_the_tui(gateway, monkeypatch):
    agent, paths = gateway
    store = agent.conversation_store
    with monkeypatch.context() as patch:
        patch.setattr(EchoBackend, "generate", _thinking_only)
        request_id, response = _ask(agent, paths, "派一个子代理去做这件事，做完告诉我。")
    assert response["ok"] is False and response["error_code"] == "USER_REPLY_UNAVAILABLE"
    thread_id = store.threads.resolve(channel="chat", channel_conversation_id=_SESSION,
                                      channel_user_id="local-agent").thread_id
    rows, cursor, _errors = store.messages.page_after_offset_report(thread_id, after=0)
    assert not [row for row in rows if row.role == "assistant" and (row.metadata or {}).get("assistant_part_id") == "final"]

    # 真机里前台派了子代理，请求因此升格成会话任务（任务关联存在、状态 active）；子代理在前台失败后才结束。
    # 这里按同一结构登记：没调工具的失败请求本身不建关联，所以显式登记 active 关联，再让子代理 DONE、发出结束 wake。
    assert store.tasks.load(request_id) is None
    store.tasks.bind({"thread_id": thread_id, "task_id": request_id, "goal": "派子代理做事", "status": "active"})
    child = agent.subagents.create_run(goal="完成子任务", thought="", plan=["执行"], parent_id=request_id, root_id=request_id)
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    wake = store.wakes.raise_signal({
        "thread_id": thread_id, "root_task_id": request_id, "reason": "subagent_runner_finished",
        "source_agent_id": child.id, "summary": "子代理结束", "dedupe_key": f"subagent-finished:{child.id}",
        "metadata": {"task_id": child.id, "status": "DONE"},
    })
    scheduler = BackgroundMainAgentScheduler({
        "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=agent.delivery_service),
        "store": store,
    })
    monkeypatch.setattr(EchoBackend, "generate", _report)

    # 成功完成的 wake 先短暂合批（COMPLETION_COALESCE_WINDOW_SECONDS），到期后由父级后台片收尾。
    reports = scheduler.tick(now=wake.created_at + COMPLETION_COALESCE_WINDOW_SECONDS + 1.0)

    assert len(reports) == 1 and reports[0].delivery_reason == "root_subagents_terminal"
    later, _cursor, _errors = store.messages.page_after_offset_report(thread_id, after=cursor)
    finals = [row for row in later if row.role == "assistant" and (row.metadata or {}).get("assistant_part_id") == "final"]
    assert [(row.content, row.metadata.get("background_delivery_reason")) for row in finals] == [
        (_REPORT, "root_subagents_terminal")]
    notices, _next, ok = read_background_response_page(store, thread_id, after=cursor)
    assert ok is True
    assert [(item["display_kind"], item["content"]) for item in notices] == [("assistant_response", _REPORT)]
    assert store.wakes.pending() == []
