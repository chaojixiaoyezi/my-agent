from __future__ import annotations

from collections.abc import Iterable

from agent_py_agent.agent.contracts.gates.network_safety import (
    NetworkSafetyFacts,
    evaluate_network_safety_gate,
)


class FakeResolver:
    def __init__(self, *answers: Iterable[str]) -> None:
        self._answers = [tuple(answer) for answer in answers]
        self.calls: list[str] = []

    def __call__(self, host: str) -> tuple[str, ...]:
        self.calls.append(host)
        if not self._answers:
            return ()
        if len(self._answers) == 1:
            return self._answers[0]
        return self._answers.pop(0)


def test_public_dns_answer_is_allowed() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    decision = evaluate_network_safety_gate(NetworkSafetyFacts("https://example.com/data", resolver))

    assert decision.allowed is True
    assert decision.evidence["host"] == "example.com"
    assert decision.evidence["resolved_ips"] == ["93.184.216.34"]
    assert resolver.calls == ["example.com"]


def test_ipv4_shorthand_and_decimal_loopback_hosts_are_blocked_before_dns() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    shorthand = evaluate_network_safety_gate(NetworkSafetyFacts("http://127.1/admin", resolver))
    decimal = evaluate_network_safety_gate(NetworkSafetyFacts("http://2130706433/admin", resolver))

    assert shorthand.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert shorthand.findings[0].evidence["host"] == "127.1"
    assert shorthand.findings[0].evidence["ip"] == "127.0.0.1"
    assert decimal.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert decimal.findings[0].evidence["host"] == "2130706433"
    assert decimal.findings[0].evidence["ip"] == "127.0.0.1"
    assert resolver.calls == []


def test_post_request_dns_recheck_blocks_rebinding_to_private_ip() -> None:
    resolver = FakeResolver(("93.184.216.34",), ("127.0.0.1",))
    preflight = evaluate_network_safety_gate(NetworkSafetyFacts("https://files.example.test/blob", resolver))

    postflight = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            "https://files.example.test/blob",
            resolver,
            previous_resolved_ips=preflight.evidence["resolved_ips"],
        )
    )

    assert preflight.allowed is True
    assert postflight.finding_codes == ("NETWORK_DNS_REBINDING_BLOCKED",)
    assert postflight.findings[0].evidence["previous_resolved_ips"] == ["93.184.216.34"]
    assert postflight.findings[0].evidence["resolved_ips"] == ["127.0.0.1"]
    assert resolver.calls == ["files.example.test", "files.example.test"]


def test_allowlisted_private_host_is_allowed() -> None:
    resolver = FakeResolver(("127.0.0.1",))

    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            "http://localhost:8000/health",
            resolver,
            allowed_private_hosts=("localhost",),
        )
    )

    assert decision.allowed is True
    assert decision.evidence["host"] == "localhost"
    assert decision.evidence["resolved_ips"] == ["127.0.0.1"]
    assert resolver.calls == ["localhost"]


def test_file_url_is_blocked_without_dns_resolution() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    decision = evaluate_network_safety_gate(NetworkSafetyFacts("file:///etc/passwd", resolver))

    assert decision.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert resolver.calls == []
