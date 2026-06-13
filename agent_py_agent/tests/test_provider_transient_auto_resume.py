from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core import provider_transient_auto_resume
from agent_py_agent.agent.backends import ModelResponse, ProviderTransientError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _TransientThenOkBackend:
    name = "fake_provider_transient_then_ok"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderTransientError("HTTP 429: rate limited")
        return ModelResponse(text="已继续完成。", backend=self.name)


def test_provider_transient_model_turn_retries_with_configured_schedule(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    monkeypatch.setattr(provider_transient_auto_resume.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        provider_transient_auto_resume,
        "provider_transient_retry_delays",
        lambda _policy=None: (10.0, 25.0, 45.0, 100.0, 180.0),
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _TransientThenOkBackend(failures=2)

    chunks: list[str] = []
    result = agent.run("做一个长任务，中间如果模型限流就继续。", save=False, on_chunk=chunks.append)

    assert result.response == "已继续完成。"
    assert agent.backend.calls == 3
    assert len(sleeps) == 2, "重试次数仍由配置阶梯决定"
    for base, actual in zip((10.0, 25.0), sleeps):
        assert base <= actual <= base * 1.5, f"睡眠应为配置值+抖动: base={base} actual={actual}"
    joined = "".join(chunks)
    assert "attempt=1/5" in joined and "attempt=2/5" in joined
    assert "秒后自动重试当前模型回合" in joined, "通知文本展示实际等待秒数(含抖动)"


def test_provider_transient_model_turn_raises_after_schedule_exhausted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    monkeypatch.setattr(provider_transient_auto_resume.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        provider_transient_auto_resume,
        "provider_transient_retry_delays",
        lambda _policy=None: (10.0, 25.0),
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _TransientThenOkBackend(failures=3)

    try:
        agent.run("做一个长任务。", save=False)
    except ProviderTransientError as exc:
        assert "HTTP 429" in str(exc)
    else:
        raise AssertionError("provider transient errors must surface after retry schedule is exhausted")

    assert agent.backend.calls == 3
    assert len(sleeps) == 2, "重试次数仍由配置阶梯决定"
    for base, actual in zip((10.0, 25.0), sleeps):
        assert base <= actual <= base * 1.5, f"睡眠应为配置值+抖动: base={base} actual={actual}"


def test_provider_transient_empty_schedule_disables_auto_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    monkeypatch.setattr(provider_transient_auto_resume.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(provider_transient_auto_resume, "provider_transient_retry_delays", lambda _policy=None: ())

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _TransientThenOkBackend(failures=1)

    try:
        agent.run("做一个长任务。", save=False)
    except ProviderTransientError:
        pass
    else:
        raise AssertionError("empty provider transient schedule should disable auto-resume")

    assert agent.backend.calls == 1


def test_provider_transient_retry_delays_allow_empty_config(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_transient_auto_resume,
        "runtime_guard_data",
        lambda **_kwargs: {"provider_transient_auto_resume_delays_seconds": []},
    )

    assert provider_transient_auto_resume.provider_transient_retry_delays() == ()


def test_provider_transient_retry_delays_use_passed_policy() -> None:
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    policy = RuntimeGuardPolicy(
        values={"provider_transient_auto_resume_delays_seconds": [1, "2.5", "bad", 0, -1]},
        sources={"provider_transient_auto_resume_delays_seconds": "agent.runtime_guard_policy"},
    )

    assert provider_transient_auto_resume.provider_transient_retry_delays(policy) == (1.0, 2.5)
