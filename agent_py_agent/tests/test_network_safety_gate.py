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


# LLM: 测实际安全门，域名授权与网段授权不能把混合 DNS 回答中的未授权地址放过。
# 函数用途: 覆盖内网白名单正例及严格的子域名和网段边界。
@pytest.mark.parametrize(("host", "entries", "answers", "allowed"), [
    ("git.corp.local", [" GIT.CORP.LOCAL. "], ["10.1.2.3"], True),
    ("api.corp.local", ["*.corp.local"], ["10.1.2.3"], True),
    ("a.b.corp.local", ["*.corp.local"], ["10.1.2.3"], True),
    ("corp.local", ["*.corp.local"], ["10.1.2.3"], False),
    ("evilcorp.local", ["*.corp.local"], ["10.1.2.3"], False),
    ("corp.local.evil", ["*.corp.local"], ["10.1.2.3"], False),
    ("api.local", ["*.local"], ["10.1.2.3"], False),
    ("192.168.0.50", ["192.168.0.0/24"], [], True),
    ("10.2.3.4", ["10.0.0.0/8"], [], True),
    ("[fd12::1]", ["fd00::/8"], [], True),
    ("[::ffff:192.168.0.50]", ["::ffff:192.168.0.0/120"], [], True),
    ("localhost", ["127.0.0.0/8"], ["127.0.0.1"], True),
    ("localhost", ["127.0.0.1"], ["127.0.0.1"], True),
    ("localhost", ["::1"], ["::1"], True),
    ("internal.test", ["10.0.0.0/8"], ["10.2.3.4", "93.184.216.34"], True),
    ("internal.test", ["10.0.0.0/8"], ["10.2.3.4", "192.168.1.5"], False),
    ("localhost", ["127.0.0.1"], ["127.0.0.1", "::1"], False),
    ("10.2.3.4", ["0.0.0.0/0", "::/0", "*", "*foo", "foo*"], [], False),
    ("localhost", "localhost", ["127.0.0.1"], False),
    ("localhost", {"localhost": True}, ["127.0.0.1"], False),
    ("localhost", [None, 1, b"localhost"], ["127.0.0.1"], False),
])
def test_private_allowlist_matching(host, entries, answers, allowed):
    decision = evaluate_network_safety_gate(_facts(
        f"http://{host}:8000/", FakeResolver(answers), allowed_private_hosts=entries,
    ))
    assert decision.allowed is allowed


# LLM: 总开关和网段授权不能把非显式域名的公网到私网翻转解释成新的授权。
# 函数用途: 测重绑定保护，显式域名授权才允许该变化。
@pytest.mark.parametrize("entries", [[], ["10.0.0.0/8"], ["internal.test"], ["*.internal.test"]])
def test_public_to_private_rebinding_requires_explicit_hostname(entries):
    decision = evaluate_network_safety_gate(_facts(
        "http://api.internal.test:8000/", FakeResolver(["10.1.2.3"]),
        previous_resolved_ips=("93.184.216.34",), allow_private_resolution=True,
        allowed_private_hosts=entries,
    ))
    assert decision.allowed is (entries == ["*.internal.test"])
    if not decision.allowed:
        assert decision.finding_codes == ("NETWORK_DNS_REBINDING_BLOCKED",)


# LLM: 默认端口在无运行端口事实时也不可绕过；未知端口仍优先 fail closed。
# 函数用途: 测默认 Gateway 端口和私网白名单互相独立。
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]", "[::ffff:127.0.0.1]", "0.0.0.0", "nic.test"])
@pytest.mark.parametrize("ports", [(), (9000,), (8420,)])
def test_default_gateway_port_is_always_protected(host, ports):
    decision = evaluate_network_safety_gate(_facts(
        f"http://{host}:8420/", FakeResolver(["192.168.1.50"]),
        allowed_private_hosts=("localhost", "127.0.0.0/8", "0.0.0.0/0"),
        allow_private_resolution=True, gateway_ports=ports,
        gateway_port_state="known" if ports else "not_applicable",
        local_address_probe=lambda _ip: True,
    ))
    assert decision.finding_codes == ("NETWORK_GATEWAY_LOCAL_PORT_BLOCKED",)
    assert decision.findings[0].evidence["gateway_ports"] == list(ports or (8420,))


# LLM: 显式零、非数字或越界端口不得变成默认 HTTP 端口；不调用解析器。
# 函数用途: 测安全门与实际连接端口不会分歧。
@pytest.mark.parametrize("port", ["0", "-1", "65536", "bad"])
def test_invalid_url_ports_fail_closed(port):
    resolver = FakeResolver(["127.0.0.1"])
    decision = evaluate_network_safety_gate(_facts(f"http://127.0.0.1:{port}/", resolver, allow_private_resolution=True))
    assert decision.finding_codes == ("NETWORK_PORT_INVALID",)
    assert resolver.calls == []


# LLM: 元数据保留地址先于任何域名、网段或总开关，覆盖直接和 DNS 入口。
# 函数用途: 测新增云厂商与映射地址的永久拒绝。
@pytest.mark.parametrize("host", ["169.254.169.254", "100.100.100.200", "[fd00:ec2::254]", "[::ffff:169.254.169.254]", "metadata.google.internal", "metadata.goog"])
@pytest.mark.parametrize("via_dns", [False, True])
def test_metadata_never_allowlisted(host, via_dns):
    ip = host.strip("[]") if not host.startswith("metadata") else "169.254.169.254"
    decision = evaluate_network_safety_gate(_facts(
        f"http://{'allowed.corp.local' if via_dns else host}/", FakeResolver([ip]),
        allowed_private_hosts=(host, "*.corp.local", "169.254.0.0/16", "100.64.0.0/10", "fd00::/8"),
        allow_private_resolution=True,
    ))
    assert not decision.allowed
    assert decision.finding_codes[0].startswith("NETWORK_ALWAYS_BLOCKED_")


# LLM: 独立复核发现 URI 分隔符被当主机名；校验只需忽略这些条目，不能生成无效授权。
# 函数用途: 覆盖白名单解析器对 URI 分隔符和不合法通配符的拒绝。
@pytest.mark.parametrize("entry", ["user@example.com", "*.bad?.example", "foo#bar.test", "foo\\bar.test", "*", "*.com", "*.corp..local", "http://corp.local", "::ffff:0.0.0.0/96"])
def test_allowlist_rejects_uri_fragments_and_blanket_entries(entry):
    from agent_py_agent.agent.contracts.gates.network_safety import _parse_private_allowlist

    assert _parse_private_allowlist([entry]) == ()


# LLM: 整组地址中已确认本机应优先于未知探针，不因 DNS 返回顺序改变端口不可用诊断。
# 函数用途: 复现并固定不可用 Gateway 端口错误的顺序独立性。
@pytest.mark.parametrize("answers", [("8.8.8.8", "127.0.0.1"), ("127.0.0.1", "8.8.8.8")])
@pytest.mark.parametrize("port", [8420, 8000])
def test_unavailable_gateway_local_priority_is_order_independent(answers, port):
    decision = evaluate_network_safety_gate(_facts(
        f"http://mixed.test:{port}/", FakeResolver(answers),
        allow_private_resolution=True, gateway_ports=(), gateway_port_state="unavailable",
        local_address_probe=lambda _ip: None,
    ))
    assert decision.finding_codes == ("NETWORK_GATEWAY_PORT_UNAVAILABLE",)


# LLM: 仅识别标准IPv6嵌入IPv4前缀（::ffff:0:0/96、64:ff9b::/96、64:ff9b:1::/48、2002::/16），普通前缀不得因低32位误判回环。
# 函数用途: 覆盖IPv6转换前缀的私网/元数据拦截与非转换前缀的回环误判防护。
def test_ipv6_transition_prefixes_and_non_transition_low32_boundaries():
    from agent_py_agent.agent.contracts.gates.network_safety import (
        _ipv4_projection_candidates,
        _is_loopback_or_unspecified_ip,
    )

    assert _is_loopback_or_unspecified_ip("2001:db8::7f00:1") is False
    assert _ipv4_projection_candidates((0, 0, 0, 0, 0, 0, 0, 0))[-1][0] is False
    assert _ipv4_projection_candidates((0, 0, 0, 0, 0, 0, 0, 1))[-1][0] is False
    for addr in ("64:ff9b::a00:1", "64:ff9b:1::c0a8:101", "2002:a00:1::"):
        blocked = evaluate_network_safety_gate(_facts(f"http://[{addr}]:8080/", FakeResolver([addr])))
        assert not blocked.allowed
        assert blocked.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
        allowed = evaluate_network_safety_gate(_facts(
            f"http://[{addr}]:8080/", FakeResolver([addr]),
            allowed_private_hosts=("64:ff9b::/96", "64:ff9b:1::/48", "2002:a00::/24"),
        ))
        assert allowed.allowed is True
