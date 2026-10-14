"""真实 HTTP/原入口回执测试；不调用模型或发送 IM。"""
from __future__ import annotations

import json
import urllib.request

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.client_credentials import gateway_client_credentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import IM_DISPATCH_KEY
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.im_task_preferences import set_delegation_preference
from agent_py_agent.tests.test_gateway_conversation_control import (
    _free_port,
    _hold_foreground_lane,
    _request,
)


# LLM: 原Gateway.start先生成宿主G1，本客户端从准确Agent根真实加载，连同普通IM身份头提交，不伪造派工字段或越过middleware。
# 函数用途: 提交一条带固定通道消息 ID 的测试消息，可验证重送与开关变化。
def submit(port, message_id, agent):
    body = json.dumps({"kind": "ask", "goal": "整理今天的资料", "conversation_id": "c-1",
                       "metadata": {"message_id": message_id}}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/ask", data=body,
                                    headers={"Content-Type": "application/json", "X-User-Id": "u-1", "X-Channel": "feishu",
                                             **gateway_client_credentials(agent).headers()})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_enabled_input_is_new_queued_work_and_replay_keeps_acceptance_snapshot(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    _hold_foreground_lane(agent, paths.processing / "req-1.json")
    set_delegation_preference(agent, "feishu", "u-1", True)
    port = _free_port()
    server = GatewayHTTPServer(port, paths, params=GatewayHTTPServerParams(
        agent=agent, auth_middleware=AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))))
    server.start()
    try:
        first = submit(port, "new-task", agent)
        assert first["status"] == "queued", first
        queued = json.loads(next(paths.inbox.glob("*.json")).read_text())
        assert queued[IM_DISPATCH_KEY]["mode"] == "delegated"
        revision = queued[IM_DISPATCH_KEY]["preference_revision"]
        set_delegation_preference(agent, "feishu", "u-1", False)
        replay = submit(port, "new-task", agent)
        assert replay["request_id"] == first["request_id"]
        assert json.loads(next(paths.inbox.glob("*.json")).read_text())[IM_DISPATCH_KEY]["preference_revision"] == revision
        new_message = submit(port, "normal-chat", agent)
        assert new_message["disposition"] == "active_turn_input"
        assert new_message["target_turn_id"] == "req-1"
    finally:
        server.stop()
