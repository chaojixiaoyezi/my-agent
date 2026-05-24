from __future__ import annotations


# LLM: Runtime rate limiting is keyed by tool plus args_hash, not by tool alone.
# 函数用途: 验证同 tool/args_hash 在窗口内超过预算时返回 retry_after_seconds。
def test_same_tool_args_rate_limit_blocks_after_budget() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(policy=ToolRateLimitPolicy(max_calls=2, window_seconds=10))
    ledger.record_attempt(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=100.0))
    ledger.record_attempt(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=101.0))

    decision = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=102.0))

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_RATE_LIMIT_EXCEEDED",)
    assert decision.findings[0].evidence["retry_after_seconds"] == 8.0


# LLM: zero max_calls disables the rate cap instead of creating a one-call fuse.
# 函数用途: 验证 max_calls=0 表示不限制同一工具身份在窗口内的调用次数。
def test_zero_max_calls_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(policy=ToolRateLimitPolicy(max_calls=0, window_seconds=60))
    for offset in range(8):
        facts = ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=100.0 + offset)
        assert ledger.check(facts).allowed is True
        ledger.record_attempt(facts)


# LLM: Tool rate limits must isolate different argument identities.
# 函数用途: 验证同一工具换 args_hash 时不会被另一个参数桶的速率预算阻断。
def test_rate_limit_uses_args_hash_as_part_of_key() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(policy=ToolRateLimitPolicy(max_calls=1, window_seconds=30))
    ledger.record_attempt(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=10.0))

    decision = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:b", now=11.0))

    assert decision.allowed is True
    assert decision.evidence["tool_name"] == "fetch_url"
    assert decision.evidence["args_hash"] == "sha256:b"


# LLM: Consecutive failures should fail closed by opening a per-key circuit.
# 函数用途: 验证同 tool/args_hash 连续失败达到阈值后，重试窗口内会被 circuit breaker 阻断。
def test_consecutive_failures_open_circuit_for_same_key() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(
        policy=ToolRateLimitPolicy(failure_threshold=2, backoff_schedule_seconds=(2, 5, 10)),
    )
    ledger.record_failure(ToolRateLimitFacts(tool_name="query_logs", args_hash="sha256:a", now=200.0))
    ledger.record_failure(ToolRateLimitFacts(tool_name="query_logs", args_hash="sha256:a", now=201.0))

    decision = ledger.check(ToolRateLimitFacts(tool_name="query_logs", args_hash="sha256:a", now=201.5))

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_CIRCUIT_OPEN",)
    assert decision.findings[0].evidence["retry_after_seconds"] == 1.5
    assert decision.findings[0].evidence["circuit_state"] == "open"


# LLM: zero failure_threshold disables the failure circuit rather than opening it immediately.
# 函数用途: 验证 failure_threshold=0 表示不限制连续失败次数，不产生 circuit open 阻断。
def test_zero_failure_threshold_disables_circuit() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(
        policy=ToolRateLimitPolicy(failure_threshold=0, backoff_schedule_seconds=(1, 2, 4)),
    )
    for offset in range(5):
        ledger.record_failure(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=10.0 + offset))

    decision = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=20.0))

    assert decision.allowed is True
    assert decision.evidence["consecutive_failures"] == 5


# LLM: Backoff should increase by structured failure count, not by parsing error text.
# 函数用途: 验证半开探测再次失败时，retry_after_seconds 按 schedule 递增。
def test_circuit_backoff_increases_after_half_open_failure() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(
        policy=ToolRateLimitPolicy(failure_threshold=1, backoff_schedule_seconds=(1, 2, 4)),
    )
    ledger.record_failure(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=10.0))
    first = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=10.25))
    assert first.findings[0].evidence["retry_after_seconds"] == 0.75

    probe = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=11.0))
    assert probe.allowed is True
    assert probe.evidence["circuit_state"] == "half_open"

    ledger.record_failure(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=11.0))
    second = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=11.25))

    assert second.allowed is False
    assert second.findings[0].evidence["retry_after_seconds"] == 1.75


# LLM: A successful half-open probe should close the circuit and reset failure history.
# 函数用途: 验证成功结果会清零连续失败、关闭 circuit，并让下一次失败从首个 backoff 重新开始。
def test_success_closes_circuit_and_resets_backoff() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
        ToolRateLimitPolicy,
    )

    ledger = ToolRateLimitLedger(
        policy=ToolRateLimitPolicy(failure_threshold=1, backoff_schedule_seconds=(1, 2, 4)),
    )
    ledger.record_failure(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=50.0))
    ledger.record_success(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=51.0))

    decision = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=51.1))
    assert decision.allowed is True
    assert decision.evidence["circuit_state"] == "closed"

    ledger.record_failure(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=52.0))
    blocked = ledger.check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="sha256:a", now=52.5))

    assert blocked.findings[0].evidence["retry_after_seconds"] == 0.5


# LLM: Missing runtime identities should fail closed instead of sharing one global bucket.
# 函数用途: 验证 tool 或 args_hash 缺失时返回机器码，避免自然语言推断 identity。
def test_missing_rate_limit_identity_denies_closed() -> None:
    from agent_py_agent.agent.contracts.gates.tool_rate_limit import (
        ToolRateLimitFacts,
        ToolRateLimitLedger,
    )

    decision = ToolRateLimitLedger().check(ToolRateLimitFacts(tool_name="fetch_url", args_hash="", now=1.0))

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_RATE_LIMIT_IDENTITY_MISSING",)
