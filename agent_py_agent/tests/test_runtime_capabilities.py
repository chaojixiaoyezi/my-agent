from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.log_analysis.capabilities import SECURITY_TOOL_NAMES
from agent_py_agent.agent.log_analysis.storage import LocalLogStore


class StaticBackend(BaseBackend):
    name = "static"

    def __init__(self, text: str = "ok"):
        self.text = text
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text=self.text, backend=self.name)


class SecurityQueryCallingBackend(BaseBackend):
    name = "security_query_calling"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "security_query [log_analysis]" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    "{"
                    '"tool":"security_query",'
                    '"attacker_ip":"198.51.100.10",'
                    '"start_time":"2026-04-30T09:00:00Z",'
                    '"end_time":"2026-04-30T11:00:00Z",'
                    '"limit":10'
                    "}\n"
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "[tool=security_query; status=ok]" in prompt
        assert '"row_count": 1' in prompt
        return ModelResponse(text="security query completed", backend=self.name)


def make_agent(workspace: Path, backend: BaseBackend) -> SimpleAgent:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            memory_rule_routing_enabled=False,
        ),
        workspace,
    )
    agent.backend = backend
    return agent


def assert_security_tools_hidden(prompt: str) -> None:
    for tool_name in SECURITY_TOOL_NAMES:
        assert f"{tool_name} [log_analysis]" not in prompt
        assert f"## {tool_name}" not in prompt


def assert_security_tools_visible(prompt: str) -> None:
    for tool_name in SECURITY_TOOL_NAMES:
        assert f"{tool_name} [log_analysis]" in prompt


def test_runtime_hides_security_tools_for_ordinary_task(tmp_path: Path):
    agent = make_agent(tmp_path, StaticBackend())

    result = agent.run("Summarize the README and list next steps.", save=False)
    security_advice = agent.run("Review password security for a login form.", save=False)

    assert_security_tools_hidden(result.prompt)
    assert_security_tools_hidden(security_advice.prompt)


def test_runtime_exposes_security_tools_for_explicit_capability_grant(tmp_path: Path):
    agent = make_agent(tmp_path, StaticBackend())

    result = agent.run(
        "Review the incident summary.",
        save=False,
        granted_capabilities=["logs/security"],
    )

    assert_security_tools_visible(result.prompt)


def test_runtime_auto_grant_authorizes_obvious_security_log_task(tmp_path: Path):
    store = LocalLogStore(tmp_path)
    store.upsert_event(
        {
            "event_id": "evt-1",
            "event_time": "2026-04-30T10:00:00Z",
            "source_id": "waf-prod",
            "alert_type": "web_attack",
            "attacker_ip": "198.51.100.10",
            "payload": "blocked request",
        }
    )
    agent = make_agent(tmp_path, SecurityQueryCallingBackend())

    result = agent.run(
        "Investigate security logs for attacker IP 198.51.100.10 between "
        "2026-04-30T09:00:00Z and 2026-04-30T11:00:00Z.",
        save=False,
    )

    assert result.tool_rounds == 1
    assert result.executed_tools == ["security_query"]
    assert_security_tools_visible(result.prompt)


def test_runtime_auto_grant_authorizes_chinese_security_log_task(tmp_path: Path):
    agent = make_agent(tmp_path, StaticBackend())
    prompt = (
        "\u8bf7\u5206\u6790\u5b89\u5168\u65e5\u5fd7\u91cc\u7684"
        "\u653b\u51fb IP \u548c\u53ef\u7591\u5165\u4fb5\u3002"
    )

    result = agent.run(prompt, save=False)

    assert_security_tools_visible(result.prompt)
