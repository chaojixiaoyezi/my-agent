"""没有可用模型的 owner 不再按维护周期反复失败（真机 2026-09-26：飞书 owner 与一个 TUI 测试 owner 未选模型，
每约 7 分钟重建一次 owner 实例、整批收集后以 ModelNotConfiguredError 失败并原地重试一次，各 28 次/3 小时）。
本测试锁定：永久配置错误不原地重试；失败码为 CURATOR_MODEL_NOT_CONFIGURED；发现层与待处理原因路径用同一个一小时退避，
其它失败码仍是原来的 5 分钟。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderRequestRejectedError,
)
from agent_py_agent.agent.memory_store.curator_backend import (
    extract_with_retries,
    last_model_attempts,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_NOT_CONFIGURED_RETRY_SECONDS,
    MemoryCuratorConfig,
    curator_failure_retry_seconds,
)
from agent_py_agent.agent.owner_wake_discovery import discover_wake_pending_owners
from agent_py_agent.tests.test_curator_input_budget import _batch, _conversation, _service
from agent_py_agent.tests.test_owner_wake_discovery import _owner_home, _write_curator_state


# 函数用途: 假后端，记录被调用次数，每次都抛同一种宿主的永久配置错误（默认是未配置模型）。
class _UnconfiguredBackend:
    name = "unconfigured"

    def __init__(self, error_factory=ModelNotConfiguredError) -> None:
        self.calls = 0
        self.error_factory = error_factory

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        self.calls += 1
        raise self.error_factory()


@pytest.mark.parametrize("error_factory", [ModelNotConfiguredError, lambda: ProviderRequestRejectedError("rejected")])
def test_permanent_configuration_error_is_not_retried_in_place(error_factory) -> None:
    backend = _UnconfiguredBackend(error_factory)
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=1, max_retries=2)

    # 永久配置错误（未配模型、4xx 拒绝）按基类合同不得重试：同输入只调用一次就按原异常失败。
    with pytest.raises(type(error_factory())):
        extract_with_retries(backend, config, _batch(messages=2, audits=0))

    assert backend.calls == 1
    assert len(last_model_attempts()) == 1


def test_run_records_a_distinct_code_and_waits_an_hour_before_retrying(tmp_path: Path) -> None:
    store, thread, _messages = _conversation(tmp_path, 3)
    backend = _UnconfiguredBackend()
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=1, max_retries=1)
    service = _service(tmp_path, backend, store, config)
    service.request("session_close")

    failed = service.run_if_due()
    state = service.state_store.load()
    assert failed.failure_code == "CURATOR_MODEL_NOT_CONFIGURED"
    assert state.last_failure_code == "CURATOR_MODEL_NOT_CONFIGURED" and state.pending_reasons
    assert state.per_thread_cursors == {}

    failed_at = datetime.fromisoformat(state.last_failure_at)
    # 原 5 分钟退避已过，但没配模型要等满一小时，期间不再调用。
    assert service.run_if_due(now=failed_at + timedelta(minutes=10)).status == "not_due"
    assert backend.calls == 1
    service.run_if_due(now=failed_at + timedelta(seconds=CURATOR_NOT_CONFIGURED_RETRY_SECONDS + 5))
    assert backend.calls == 2
    assert thread.thread_id not in service.state_store.load().per_thread_cursors


def test_discovery_uses_the_same_long_backoff_only_for_the_not_configured_code(tmp_path: Path) -> None:
    owners = tmp_path / "owners"
    ten_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-no-model"), pending_reasons=["session_close"],
                         last_failure_at=ten_minutes_ago, last_failure_code="CURATOR_MODEL_NOT_CONFIGURED")
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-timeout"), pending_reasons=["session_close"],
                         last_failure_at=ten_minutes_ago, last_failure_code="CURATOR_MODEL_TIMEOUT")

    # 同样 10 分钟前失败：普通失败已过 5 分钟退避会被唤醒，没配模型的仍在一小时退避里。
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u-timeout"]
    assert curator_failure_retry_seconds("CURATOR_MODEL_TIMEOUT", 300) == 300
    assert curator_failure_retry_seconds("CURATOR_MODEL_NOT_CONFIGURED", 300) == CURATOR_NOT_CONFIGURED_RETRY_SECONDS
