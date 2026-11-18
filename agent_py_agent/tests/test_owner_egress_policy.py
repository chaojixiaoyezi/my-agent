from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from agent.user_space.owner_egress import (
    OwnerEgressPolicy,
    OwnerEgressPolicyError,
    OwnerEgressPolicyStore,
)
from agent.workflows.script_runner import ScriptRunner


def policy(mode, *endpoints):
    return OwnerEgressPolicy.from_dict({
        "mode": mode,
        "endpoints": [{"ip": ip, "port": port} for ip, port in endpoints],
    })


@pytest.mark.parametrize("ip,port", [
    ("example.org", 443), ("192.0.2.1/32", 443), ("fe80::1%eth0", 443), ("::1", True),
    ("::1", "443"), ("::1", 0), ("::1", 65536), (123, 443),
])
def test_literal_ip_and_integer_port_required(ip, port):
    with pytest.raises(OwnerEgressPolicyError) as caught:
        policy("allowlist", (ip, port))
    assert caught.value.code == "INVALID_EGRESS_POLICY"
    assert caught.value.side_effect == "not_started"


def test_normalization_and_effective_precedence():
    allow = policy("allowlist", ("2001:0db8::0001", 443), ("::ffff:192.0.2.1", 80), ("192.0.2.1", 80))
    assert allow.to_dict() == {"mode": "allowlist", "endpoints": [
        {"ip": "2001:db8::1", "port": 443}, {"ip": "192.0.2.1", "port": 80},
    ]}
    assert policy("inherit").effective(allow) == allow
    assert policy("unrestricted").effective(allow).mode == "unrestricted"
    assert policy("allowlist").effective(policy("unrestricted")).to_dict() == {"mode": "allowlist", "endpoints": []}
    assert allow.permits("::ffff:192.0.2.1", 80)
    assert allow.permits("2001:db8::1", 443)
    assert not allow.permits("2001:db8::1", 444)
    assert not allow.permits("2001:db8::2", 443)
    assert not policy("allowlist").permits("192.0.2.1", 80)


def test_rejects_unsafe_batch_and_default_without_publication(tmp_path):
    store = OwnerEgressPolicyStore(tmp_path)
    assert store.get_default().mode == "unrestricted"
    assert store.get("providers/feishu/users/user-a").mode == "inherit"
    assert store.get_effective("providers/feishu/users/user-a").mode == "unrestricted"
    assert not store.path.exists()
    for apply in [
        lambda: store.apply({"providers/feishu/users/user-a": "unrestricted", "providers/feishu/users/user-b": "allowlist"}),
        lambda: store.set_default("allowlist"),
    ]:
        with pytest.raises(OwnerEgressPolicyError) as caught:
            apply()
        assert caught.value.code == "EGRESS_ENFORCEMENT_UNAVAILABLE"
        assert caught.value.side_effect == "not_started"
        assert not store.path.exists()


def test_distinct_owner_namespaces_and_mode_only_patch(tmp_path):
    a, b = "local/same-id", "providers/feishu/users/same-id"
    store = OwnerEgressPolicyStore(tmp_path)
    given = policy("unrestricted", ("192.0.2.3", 443))
    store.apply({a: given, b: "inherit"})
    assert OwnerEgressPolicyStore(tmp_path).apply({a: "inherit"}) == {a: policy("inherit", ("192.0.2.3", 443))}
    assert store.get(b) == policy("inherit")


def test_mode_only_patch_loads_committed_endpoints_under_database_lock(tmp_path, monkeypatch):
    a = "providers/feishu/users/user-a"
    store = OwnerEgressPolicyStore(tmp_path)
    store.apply({a: policy("unrestricted", ("192.0.2.1", 80))})
    writer_locked = Event()
    patch_started = Event()
    release = Event()
    original = store._write

    def hold(conn, key, value):
        if value == policy("unrestricted", ("192.0.2.2", 443)):
            writer_locked.set()
            assert release.wait(1)
        original(conn, key, value)

    monkeypatch.setattr(store, "_write", hold)

    def patch():
        patch_started.set()
        return OwnerEgressPolicyStore(tmp_path).apply({a: "inherit"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(store.apply, {a: policy("unrestricted", ("192.0.2.2", 443))})
        assert writer_locked.wait(1)
        patch_future = pool.submit(patch)
        assert patch_started.wait(1)
        release.set()
        write_future.result(timeout=2)
        assert patch_future.result(timeout=2) == {a: policy("inherit", ("192.0.2.2", 443))}


def test_script_runner_rejects_policy_from_an_unmanaged_deployment_before_launch(tmp_path):
    store = OwnerEgressPolicyStore(tmp_path)
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "user-x"
    # 模拟读取到了已有策略。这里没有原部署的进程隔离。
    with store._database(write=True) as connection:
        store._write(connection, "providers/feishu/users/user-x", policy("allowlist"))
    result = ScriptRunner(owner_home).run_script("fixture", record_summary=False)
    assert result.error_code == "EGRESS_ENFORCEMENT_UNAVAILABLE"
    assert result.effect_outcome == "not_started"
    assert "隔离" in result.escalation_context["execution_error"]["next_action"]
