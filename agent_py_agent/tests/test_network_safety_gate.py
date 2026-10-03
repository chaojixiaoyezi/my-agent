from __future__ import annotations

from collections.abc import Iterable

import pytest

from agent_py_agent.agent.contracts.gates.network_safety import (
    NetworkSafetyFacts,
    _local_address_status,
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


def _facts(url: object, resolver: FakeResolver, **overrides) -> NetworkSafetyFacts:
    values = {
        "gateway_ports": (45123,),
        "gateway_port_state": "known",
        "gateway_port_source": "config",
        "local_address_probe": lambda _ip: False,
        **overrides,
    }
    return NetworkSafetyFacts(url, resolver, **values)


def test_public_dns_answer_is_allowed() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    decision = evaluate_network_safety_gate(_facts("https://example.com/data", resolver))

    assert decision.allowed is True
    assert decision.evidence["host"] == "example.com"
    assert decision.evidence["resolved_ips"] == ["93.184.216.34"]
    assert resolver.calls == ["example.com"]


def test_ipv4_shorthand_and_decimal_loopback_hosts_are_blocked_before_dns() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    shorthand = evaluate_network_safety_gate(_facts("http://127.1/admin", resolver))
    decimal = evaluate_network_safety_gate(_facts("http://2130706433/admin", resolver))

    assert shorthand.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert shorthand.findings[0].evidence["host"] == "127.1"
    assert shorthand.findings[0].evidence["ip"] == "127.0.0.1"
    assert decimal.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert decimal.findings[0].evidence["host"] == "2130706433"
    assert decimal.findings[0].evidence["ip"] == "127.0.0.1"
    assert resolver.calls == []


def test_post_request_dns_recheck_blocks_rebinding_to_private_ip() -> None:
    resolver = FakeResolver(("93.184.216.34",), ("127.0.0.1",))
    preflight = evaluate_network_safety_gate(_facts("https://files.example.test/blob", resolver))

    postflight = evaluate_network_safety_gate(
        _facts(
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
        _facts(
            "http://localhost:8000/health",
            resolver,
            allowed_private_hosts=("localhost",),
            gateway_ports=(8001,),
            gateway_port_source="config",
        )
    )

    assert decision.allowed is True
    assert decision.evidence["host"] == "localhost"
    assert decision.evidence["resolved_ips"] == ["127.0.0.1"]
    assert resolver.calls == ["localhost"]


def test_structured_private_resolution_opt_in_allows_proxy_dns() -> None:
    resolver = FakeResolver(("198.18.0.18",))

    decision = evaluate_network_safety_gate(
        _facts(
            "https://api.github.com/repos/example/project",
            resolver,
            allow_private_resolution=True,
        )
    )

    assert decision.allowed is True
    assert decision.evidence["allow_private_resolution"] is True
    assert decision.evidence["resolved_ips"] == ["198.18.0.18"]


def test_public_hostname_with_benchmark_proxy_dns_is_allowed_without_private_opt_in() -> None:
    resolver = FakeResolver(("198.18.0.18", "::ffff:0:c612:12"))

    decision = evaluate_network_safety_gate(
        _facts("https://api.example.test/search?q=project", resolver)
    )

    assert decision.allowed is True
    assert decision.evidence["resolved_ips"] == ["198.18.0.18", "::ffff:0:c612:12"]
    assert decision.evidence["allow_private_resolution"] is False
    assert decision.evidence["allow_benchmark_resolution"] is True
    assert decision.evidence["benchmark_resolution_allowed_ips"] == ["198.18.0.18", "::ffff:0:c612:12"]


def test_literal_benchmark_ip_is_still_blocked_without_private_opt_in() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    decision = evaluate_network_safety_gate(_facts("https://198.18.0.18/data", resolver))
    translated = evaluate_network_safety_gate(_facts("https://[::ffff:0:c612:12]/data", resolver))

    assert decision.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert decision.findings[0].evidence["ip"] == "198.18.0.18"
    assert translated.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert translated.findings[0].evidence["ip"] == "::ffff:0:c612:12"
    assert resolver.calls == []


def test_private_resolution_opt_in_still_blocks_metadata_targets() -> None:
    resolver = FakeResolver(("169.254.169.254",))

    hostname = evaluate_network_safety_gate(
        _facts(
            "http://metadata.google.internal/computeMetadata/v1",
            resolver,
            allow_private_resolution=True,
        )
    )
    address = evaluate_network_safety_gate(
        _facts(
            "http://example.test/redirected",
            resolver,
            allow_private_resolution=True,
        )
    )

    assert hostname.finding_codes == ("NETWORK_ALWAYS_BLOCKED_HOST",)
    assert address.finding_codes == ("NETWORK_ALWAYS_BLOCKED_IP",)


def test_private_resolution_opt_in_blocks_embedded_ipv4_metadata_targets() -> None:
    resolver = FakeResolver(("::ffff:0:a9fe:a9fe",))

    decision = evaluate_network_safety_gate(
        _facts(
            "https://public.example.test/data",
            resolver,
            allow_private_resolution=True,
        )
    )

    assert decision.finding_codes == ("NETWORK_ALWAYS_BLOCKED_IP",)
    assert decision.findings[0].evidence["ip"] == "::ffff:0:a9fe:a9fe"


def test_file_url_is_blocked_without_dns_resolution() -> None:
    resolver = FakeResolver(("93.184.216.34",))

    decision = evaluate_network_safety_gate(_facts("file:///etc/passwd", resolver))

    assert decision.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("url", "answers"),
    (
        ("http://127.0.0.1:{port}/", ("127.0.0.1",)),
        ("http://localhost:{port}/", ("127.0.0.1",)),
        ("http://[::1]:{port}/", ("::1",)),
        ("http://[::ffff:127.0.0.1]:{port}/", ("::ffff:127.0.0.1",)),
        ("http://0.0.0.0:{port}/", ("0.0.0.0",)),
        ("http://gateway-nic.test:{port}/", ("192.0.2.20",)),
    ),
)
def test_private_opt_in_cannot_reach_gateway_port_on_local_addresses(
    url: str, answers: tuple[str, ...]
) -> None:
    gateway_port = 45123
    resolver = FakeResolver(answers)

    decision = evaluate_network_safety_gate(
        _facts(
            url.format(port=gateway_port),
            resolver,
            allow_private_resolution=True,
            gateway_ports=(gateway_port,),
            gateway_port_state="known",
            gateway_port_source="config",
            local_address_probe=lambda ip: ip == "192.0.2.20",
        )
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("NETWORK_GATEWAY_LOCAL_PORT_BLOCKED",)
    assert decision.findings[0].evidence["gateway_ports"] == [gateway_port]


def test_private_opt_in_keeps_other_local_ports_and_other_private_hosts_allowed() -> None:
    local_port = 45124
    gateway_port = 45123
    local = evaluate_network_safety_gate(
        _facts(
            f"http://127.0.0.1:{local_port}/",
            FakeResolver(("127.0.0.1",)),
            allow_private_resolution=True,
            gateway_ports=(gateway_port,),
            gateway_port_source="config",
            local_address_probe=lambda _ip: True,
        )
    )
    remote_private = evaluate_network_safety_gate(
        _facts(
            f"http://other-private.test:{gateway_port}/",
            FakeResolver(("10.20.30.40",)),
            allow_private_resolution=True,
            gateway_ports=(gateway_port,),
            gateway_port_source="config",
            local_address_probe=lambda _ip: False,
        )
    )

    assert local.allowed is True
    assert remote_private.allowed is True


def test_private_opt_in_fails_closed_for_local_target_when_gateway_port_unknown() -> None:
    decision = evaluate_network_safety_gate(
        _facts(
            "http://127.0.0.1:45123/",
            FakeResolver(("127.0.0.1",)),
            allow_private_resolution=True,
            gateway_ports=(),
            gateway_port_state="unavailable",
            gateway_port_source="unavailable",
            local_address_probe=lambda _ip: True,
        )
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("NETWORK_GATEWAY_PORT_UNAVAILABLE",)


def test_unavailable_gateway_endpoint_does_not_block_public_target() -> None:
    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            "https://example.test/data",
            FakeResolver(("93.184.216.34",)),
            gateway_ports=(),
            gateway_port_state="unavailable",
            gateway_port_source="unavailable",
            local_address_probe=lambda _ip: False,
        )
    )

    assert decision.allowed is True


def test_private_access_disabled_keeps_existing_loopback_rejection() -> None:
    decision = evaluate_network_safety_gate(
        _facts(
            "http://127.0.0.1:45123/",
            FakeResolver(("127.0.0.1",)),
            gateway_ports=(45123,),
            gateway_port_source="config",
            local_address_probe=lambda _ip: True,
        )
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)


def test_missing_local_address_fact_only_blocks_gateway_port() -> None:
    matching_port = evaluate_network_safety_gate(
        _facts(
            "http://public.example.test:45123/",
            FakeResolver(("93.184.216.34",)),
            allow_private_resolution=True,
            local_address_probe=lambda _ip: None,
        )
    )
    other_port = evaluate_network_safety_gate(
        _facts(
            "http://public.example.test:45124/",
            FakeResolver(("93.184.216.34",)),
            allow_private_resolution=True,
            local_address_probe=lambda _ip: None,
        )
    )

    assert matching_port.finding_codes == ("NETWORK_GATEWAY_LOCAL_ADDRESS_UNAVAILABLE",)
    assert other_port.allowed is True


def test_ipv4_mapped_loopback_address_is_recognized_without_probe() -> None:
    assert _local_address_status("::ffff:127.0.0.1", None) is True
