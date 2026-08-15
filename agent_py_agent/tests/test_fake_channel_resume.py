from __future__ import annotations

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    ChannelMessageRuntime,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _EchoOnceBackend:
    name = "echo-once"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="我已恢复同一个会话上下文。", backend=self.name)


def test_fake_feishu_and_wechat_restore_same_thread_for_same_user(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _EchoOnceBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    messages = ChannelMessageRuntime(runtime=runtime, store=store)

    first = messages.receive({'channel': "feishu", 'channel_conversation_id': "feishu-chat", 'channel_user_id': "feishu-user", 'canonical_user_id': "same-person", 'content': "请记住这个长期任务：每小时看一次子代理状态。", 'now': 10.0, 'run_background': False})
    second = messages.receive({'channel': "wechat", 'channel_conversation_id': "wechat-chat", 'channel_user_id': "wechat-user", 'canonical_user_id': "same-person", 'content': "继续刚刚那个长期任务，看看现在怎么样。", 'now': 20.0, 'run_background': True})

    assert first.thread_id == second.thread_id
    assert "请记住这个长期任务" in backend.prompts[0]
    assert "继续刚刚那个长期任务" in backend.prompts[0]
    assert channels.adapter("wechat").sent_messages[0].target == "wechat-chat"
