"""Gateway 出站治理从可信启动绑定到管理发布。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import owner_egress_gateway, owner_egress_linux
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_egress import (
    OwnerEgressEndpoint,
    OwnerEgressPolicy,
    OwnerEgressPolicyError,
    OwnerEgressPolicyStore,
    bound_owner_egress_policy,
    require_owner_process_egress,
)
from agent_py_agent.agent.user_space.owner_egress_admin import manage_owner_egress
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)


class _Lease:
    def __init__(self):
        self.current = OwnerEgressPolicy("unrestricted")
        self.history = []
        self.fail_policy = None

    def apply(self, policy):
        if self.fail_policy == policy:
            raise OwnerEgressPolicyError(
                "EGRESS_KERNEL_UPDATE_FAILED", "fixture rejected the kernel update",
                status_code=503, side_effect="unconfirmed", next_action="inspect fixture",
            )
        self.current = policy
        self.history.append(("apply", policy))

    def verify(self, policy):
        if self.current != policy:
            raise OwnerEgressPolicyError(
                "EGRESS_KERNEL_READBACK_FAILED", "fixture has a different effective rule",
                status_code=503, side_effect="unconfirmed", next_action="inspect fixture",
            )
        self.history.append(("verify", policy))


def _write_manifest(root, names):
    path = root / "config" / "owner_egress_namespaces.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "owner_egress_namespaces.v1", "enabled": True,
        "owners": [{"owner_id": name, "holder_pid": 500 + index, "holder_start_ticks": 70 + index}
                   for index, name in enumerate(names)],
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def _gateway(tmp_path, monkeypatch, names):
    authority = tmp_path / "authority"
    gateway = tmp_path / "gateway"
    gateway.mkdir()
    monkeypatch.setattr(owner_egress_gateway, "sys", SimpleNamespace(platform="linux"))
    _write_manifest(authority, names)
    monkeypatch.setattr(owner_egress_gateway, "_holder_namespace_fds", lambda stack, pid, ticks: (pid, ticks))
    monkeypatch.setattr(owner_egress_gateway, "_binding_file_identity", lambda fd: [10, fd])
    monkeypatch.setattr(owner_egress_gateway, "_boot_identity", lambda: "30b0b898-478a-48a2-a9d9-22eb64df76a8")
    leases = {}

    @contextmanager
    def bind(name, network_fd, user_fd, *, authority_root):
        assert authority_root == authority and network_fd >= 500 and user_fd >= 70
        lease = leases[name] = _Lease()
        yield lease

    monkeypatch.setattr(owner_egress_linux, "bind_linux_owner_namespace", bind)
    lifecycle = SimpleNamespace(require_held=lambda: None)
    return authority, gateway, lifecycle, leases


def test_startup_binds_before_account_creation_and_rejects_old_generation(tmp_path, monkeypatch):
    authority, gateway, lifecycle, leases = _gateway(tmp_path, monkeypatch, ("local/alice",))
    store = OwnerEgressPolicyStore(authority)
    assert store.get_effective("local/alice").mode == "unrestricted"
    with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
        runtime = owner_egress_gateway.current_gateway_owner_egress(authority)
        assert runtime.leases == leases
        mike = ensure_owner_home(authority, OwnerIdentity.local_user("mike"))
        with pytest.raises(OwnerEgressPolicyError) as blocked:
            require_owner_process_egress(mike.home_dir, authority)
        assert blocked.value.code == "EGRESS_OWNER_RUNTIME_UNMANAGED"
        with pytest.raises(OwnerEgressPolicyError) as wrong_home:
            require_owner_process_egress(authority / "unrecognized", authority)
        assert wrong_home.value.code == "EGRESS_OWNER_IDENTITY_UNPROVEN"
        with pytest.raises(OwnerEgressPolicyError) as blocked:
            bound_owner_egress_policy(authority, owner_id=mike.owner_id, owner_kind="user").http_scope()
        assert blocked.value.code == "EGRESS_OWNER_RUNTIME_UNMANAGED"
    assert owner_egress_gateway.current_gateway_owner_egress(authority) is None
    from agent_py_agent.agent.gateway_parts import daemon_metadata

    (gateway / "gateway_state.json").write_text('{"status":"stopped","process_identity":{"fixture":true}}')
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _: True)
    with pytest.raises(OwnerEgressPolicyError) as live:
        with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
            pytest.fail("old host process can still have active HTTP")
    assert live.value.code == "EGRESS_PRIOR_GATEWAY_EXIT_UNCONFIRMED"
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _: False)
    with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
        assert owner_egress_gateway.current_gateway_owner_egress(authority) is not None
    monkeypatch.setattr(owner_egress_gateway, "_binding_file_identity", lambda fd: [11, fd])
    with pytest.raises(OwnerEgressPolicyError) as old:
        with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
            pytest.fail("old unverified generation started")
    assert old.value.code == "EGRESS_PREDECESSOR_UNPROVEN"


def test_admin_default_and_override_publish_exact_kernel_readbacks(tmp_path, monkeypatch):
    authority, gateway, lifecycle, leases = _gateway(tmp_path, monkeypatch, ("local/alice", "local/bob", "local/carol"))
    with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
        for identifier in ("alice", "bob", "carol"):
            ensure_owner_home(authority, OwnerIdentity.local_user(identifier))
        admin = home_paths_with_owner(home_paths(authority), ensure_owner_home(authority, OwnerIdentity.local_main()))
        policy = OwnerEgressPolicy("allowlist", (OwnerEgressEndpoint("192.0.2.1", 443),))
        manage_owner_egress(admin, {"action": "egress_set_users", "updates": [
            {"owner_id": "local/alice", "mode": "unrestricted"},
            {"owner_id": "local/carol", **policy.to_dict()},
        ]})
        report = manage_owner_egress(admin, {"action": "egress_set_default", **policy.to_dict()})
        assert report["default"] == policy.to_dict()
        assert leases["local/alice"].current.mode == "unrestricted"
        assert leases["local/bob"].current == leases["local/carol"].current == policy
        assert leases["local/bob"].history[-1] == ("verify", policy)

        proposed = OwnerEgressPolicy("allowlist", (OwnerEgressEndpoint("192.0.2.2", 443),))
        leases["local/bob"].fail_policy = proposed
        with pytest.raises(OwnerEgressPolicyError) as hot_error:
            manage_owner_egress(admin, {"action": "egress_set_default", **proposed.to_dict()})
        assert hot_error.value.code == "EGRESS_DEFAULT_KERNEL_UNCONFIRMED"
        assert hot_error.value.details["policy_committed"] is True
        assert leases["local/bob"].current == OwnerEgressPolicy("allowlist")
        assert OwnerEgressPolicyStore(authority).get_default() == proposed
        assert leases["local/alice"].current.mode == "unrestricted"
        assert leases["local/carol"].current == policy

        leases["local/bob"].fail_policy = None
        manage_owner_egress(admin, {"action": "egress_set_default", **proposed.to_dict()})
        assert leases["local/bob"].current == proposed
        default = manage_owner_egress(admin, {"action": "egress_set_default", "mode": "unrestricted"})
        assert default["default"]["mode"] == "unrestricted"
        assert leases["local/bob"].current.mode == "unrestricted"
        assert leases["local/carol"].current == policy


def test_user_batch_reports_verified_and_post_commit_failures_separately(tmp_path, monkeypatch):
    authority, gateway, lifecycle, leases = _gateway(tmp_path, monkeypatch, ("local/alice", "local/bob"))
    with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
        for identifier in ("alice", "bob", "carol"):
            ensure_owner_home(authority, OwnerIdentity.local_user(identifier))
        admin = home_paths_with_owner(home_paths(authority), ensure_owner_home(authority, OwnerIdentity.local_main()))
        strict = OwnerEgressPolicy("allowlist", (OwnerEgressEndpoint("192.0.2.4", 80),))
        manage_owner_egress(admin, {"action": "egress_set_users", "updates": [{"owner_id": "local/bob", **strict.to_dict()}]})
        leases["local/bob"].fail_policy = OwnerEgressPolicy("unrestricted", strict.endpoints)
        with pytest.raises(OwnerEgressPolicyError) as batch:
            manage_owner_egress(admin, {"action": "egress_set_users", "updates": [
                {"owner_id": "local/alice", **strict.to_dict()},
                {"owner_id": "local/bob", "mode": "unrestricted"},
                {"owner_id": "local/carol", **strict.to_dict()},
            ]})
        assert batch.value.code == "EGRESS_BATCH_PARTIAL_COMMIT"
        rows = batch.value.details["users"]
        assert [row["status"] for row in rows] == ["applied", "unconfirmed", "not_started"]
        assert rows[1]["policy_committed"] is True
        assert leases["local/bob"].current == strict
        assert OwnerEgressPolicyStore(authority).get("local/bob").mode == "unrestricted"
        observed = manage_owner_egress(admin, {"action": "egress_get", "owner_id": "local/bob"})
        assert observed["kernel_readback"]["status"] == "unconfirmed"
        leases["local/bob"].fail_policy = None
        repaired = manage_owner_egress(admin, {"action": "egress_set_users", "updates": [
            {"owner_id": "local/bob", **observed["policy"]},
        ]})
        assert repaired["users"][0]["status"] == "applied"
        assert manage_owner_egress(admin, {"action": "egress_get", "owner_id": "local/bob"})["kernel_readback"] == {
            "status": "verified",
        }




def test_existing_user_takeover_requires_actual_changed_boot_and_preserves_owner_data(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import daemon_metadata

    authority, gateway, lifecycle, _ = _gateway(tmp_path, monkeypatch, ("local/alice",))
    owner = ensure_owner_home(authority, OwnerIdentity.local_user("alice"))
    previous_history = owner.home_dir / "prior-history.txt"
    previous_history.write_text("保留原历史", encoding="utf-8")
    state = gateway / "gateway_state.json"
    state.write_text(json.dumps({"status": "stopped", "process_identity": {"fixture": True}}), encoding="utf-8")
    with pytest.raises(OwnerEgressPolicyError) as previous:
        with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
            pytest.fail("unverified installed Gateway started")
    assert previous.value.code == "EGRESS_PREDECESSOR_UNPROVEN"
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _: False)
    prepared = owner_egress_gateway.prepare_owner_egress_cold_takeover(authority, gateway, lifecycle)
    assert prepared == {"status": "prepared_requires_cold_boot", "owners": ["local/alice"]}
    with pytest.raises(OwnerEgressPolicyError) as same_boot:
        with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
            pytest.fail("same boot started despite possible unconfined executors")
    assert same_boot.value.code == "EGRESS_COLD_TAKEOVER_UNPROVEN"

    monkeypatch.setattr(owner_egress_gateway, "_boot_identity", lambda: "52cd4347-a59a-44f1-a78b-97d62491d167")
    with owner_egress_gateway.bind_gateway_owner_egress(authority, gateway, lifecycle):
        assert owner_egress_gateway.current_gateway_owner_egress(authority) is not None
        assert previous_history.read_text(encoding="utf-8") == "保留原历史"
    store = OwnerEgressPolicyStore(authority)
    assert store._takeover() is None
    assert store._coverage()["boot_id"] == "52cd4347-a59a-44f1-a78b-97d62491d167"



def test_policy_database_rejects_symlink_without_touching_destination(tmp_path):
    store = OwnerEgressPolicyStore(tmp_path)
    target = tmp_path / "unrelated"
    target.write_bytes(b"unchanged")
    store.path.parent.mkdir()
    try:
        store.path.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create symlinks")
    for action in (store.get_default, lambda: store.set_default("unrestricted")):
        with pytest.raises(OwnerEgressPolicyError) as unsafe:
            action()
        assert unsafe.value.code == "EGRESS_POLICY_STORAGE_UNSAFE"
    assert target.read_bytes() == b"unchanged"
