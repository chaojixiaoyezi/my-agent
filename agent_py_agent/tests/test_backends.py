"""后端适配器请求载荷约定检查。"""

from agent_py_agent.agent.backend import AnthropicCompatibleBackend, OpenAICompatibleBackend


class FakeOpenAI(OpenAICompatibleBackend):
    def __init__(self):
        super().__init__(api_base="http://fake/v1", api_key="k", model_name="m")
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"choices": [{"message": {"content": "openai ok"}}]}


class FakeAnthropic(AnthropicCompatibleBackend):
    def __init__(self):
        super().__init__(api_base="http://fake/anthropic", api_key="k", model_name="m")
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"content": [{"type": "text", "text": "anthropic ok"}]}


def test_openai_payload():
    backend = FakeOpenAI()
    response = backend.generate("hello")
    assert response.text == "openai ok"
    path, payload, headers = backend.seen
    assert path == "/chat/completions"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"


def test_anthropic_payload():
    backend = FakeAnthropic()
    response = backend.generate("hello")
    assert response.text == "anthropic ok"
    path, payload, headers = backend.seen
    assert path == "/v1/messages"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"
    assert "anthropic-version" in headers
