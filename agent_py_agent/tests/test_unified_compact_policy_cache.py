"""只缓存压缩只读解析；身份/文件替换及写前fresh读不能借缓存放权。"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_threshold_policy import (
    compact_policy_scope,
    resolve_thread_compact_policy,
    thread_with_compact_threshold,
)
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space import managed_user_governance as governance
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore


def _agent(root, uid="alice"):
    users = LocalUserStore(root)
    LocalUserLifecycle(users).create(uid, idempotency_key="compact_cache_user_" + uid)
    home = SimpleNamespace(root=root, owner_provider="local", owner_kind="user", owner_id="local/" + uid)
    return SimpleNamespace(home_paths=home, config=SimpleNamespace(memory_compact_trigger_percent=80))


def _reads(monkeypatch):
    calls, original = [], LocalUserStore.read_json

    def observe(self, parts):
        calls.append((str(self.root), tuple(parts)))
        return original(self, parts)

    monkeypatch.setattr(LocalUserStore, "read_json", observe)
    return calls


def test_unchanged_cross_turn_policy_does_not_reread_or_parse_authority(tmp_path, monkeypatch):
    agent = _agent(tmp_path / "host")
    calls = _reads(monkeypatch)
    first = resolve_thread_compact_policy(agent)
    count = len(calls)
    assert count > 0
    for _ in range(100):
        result = resolve_thread_compact_policy(agent)
        assert result == first and result is not first
    assert len(calls) == count
    resolve_thread_compact_policy(agent, use_frozen=False)
    assert len(calls) > count


def test_cache_is_only_governance_not_thread_or_config_or_mutable_results(tmp_path):
    agent = _agent(tmp_path / "host")
    first = resolve_thread_compact_policy(agent)
    first["effective_trigger_percent"] = 1
    agent.config.memory_compact_trigger_percent = 85
    thread = SimpleNamespace(thread_id="thread", metadata={"settings": {"compact_trigger_percent": 90}})
    assert resolve_thread_compact_policy(agent)["effective_trigger_percent"] == 85
    assert resolve_thread_compact_policy(agent, thread)["effective_trigger_percent"] == 90


def test_atomic_same_size_same_mtime_replacement_invalidates_without_writer_hook(tmp_path):
    agent = _agent(tmp_path / "host")
    service = governance.ManagedUserGovernanceService(agent.home_paths.root)
    service.set_user_compact_policy("alice", {"default_trigger_percent": 70})
    assert resolve_thread_compact_policy(agent)["effective_trigger_percent"] == 70
    path = governance._GovernanceControl(agent.home_paths.root, "alice").policy_path
    original = path.stat()
    raw = path.read_bytes()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(raw.replace(b'70', b'75'))
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert replacement.stat().st_size == original.st_size
    replacement.replace(path)
    assert resolve_thread_compact_policy(agent)["effective_trigger_percent"] == 75


@pytest.mark.parametrize("change", ["disabled", "corrupt_policy", "corrupt_identity", "replaced_home", "policy_symlink"])
def test_cached_policy_cannot_hide_identity_revocation_or_invalid_files(tmp_path, change):
    agent = _agent(tmp_path / "host")
    root = agent.home_paths.root
    governance.ManagedUserGovernanceService(root).set_user_compact_policy("alice", {"default_trigger_percent": 70})
    assert resolve_thread_compact_policy(agent)["effective_trigger_percent"] == 70
    users = LocalUserStore(root)
    policy_path = governance._GovernanceControl(root, "alice").policy_path
    if change == "disabled":
        record = users.read_record("alice")
        record["user"]["status"] = "disabled"
        record["user"]["revision"] += 1
        users.save("alice", record)
    elif change == "corrupt_policy":
        policy_path.write_text("{bad")
    elif change == "corrupt_identity":
        (root / "local_users/alice.json").write_text("{}")
    elif change == "replaced_home":
        home = users.owner_home("alice").home_dir
        home.rename(home.with_name("old-alice"))
        home.mkdir()
    else:
        saved = policy_path.with_suffix(".saved")
        policy_path.rename(saved)
        policy_path.symlink_to(saved)
    with pytest.raises(HostAPIError):
        resolve_thread_compact_policy(agent)


def test_turn_freeze_is_preserved_but_threshold_write_uses_current_admin_lock(tmp_path):
    agent = _agent(tmp_path / "host")
    thread = ConversationThread(thread_id="compact-cache", canonical_user_id="local/alice", title="fixture")
    service = governance.ManagedUserGovernanceService(agent.home_paths.root)
    with compact_policy_scope(agent, thread):
        assert resolve_thread_compact_policy(agent, thread)["force_locked"] is False
        service.set_user_compact_policy("alice", {"force_locked": True})
        assert resolve_thread_compact_policy(agent, thread)["force_locked"] is False
        assert resolve_thread_compact_policy(agent, thread, use_frozen=False)["force_locked"] is True
        with pytest.raises(HostAPIError) as caught:
            thread_with_compact_threshold(agent, thread, 90)
        assert caught.value.code == "COMPACT_POLICY_LOCKED_BY_ADMIN"
    assert resolve_thread_compact_policy(agent, thread)["force_locked"] is True


def test_hosts_with_same_uid_and_owners_in_same_host_are_isolated(tmp_path):
    alice = _agent(tmp_path / "host-a")
    bob = _agent(tmp_path / "host-a", "bob")
    other_alice = _agent(tmp_path / "host-b")
    for agent, percent in [(alice, 70), (bob, 75), (other_alice, 90)]:
        governance.ManagedUserGovernanceService(agent.home_paths.root).set_user_compact_policy(
            agent.home_paths.owner_id, {"default_trigger_percent": percent})
    for _ in range(2):
        assert [resolve_thread_compact_policy(agent)["effective_trigger_percent"]
                for agent in (alice, bob, other_alice)] == [70, 75, 90]


def test_default_governance_policy_reads_stay_fresh_for_other_callers(tmp_path):
    agent = _agent(tmp_path / "host")
    control = governance._GovernanceControl(agent.home_paths.root, "alice")
    assert control.policy()["revision"] == 0
    governance.ManagedUserGovernanceService(agent.home_paths.root).set_user_compact_policy("alice", {"force_locked": True})
    assert control.policy()["user_compact_policy"]["force_locked"] is True


def test_owner_status_revocation_is_not_hidden_by_warm_cache(tmp_path):
    agent = _agent(tmp_path / "host")
    resolve_thread_compact_policy(agent)
    users = LocalUserStore(agent.home_paths.root)
    users.write_json(("owners", "local", "alice", "owner_status.json"), {"status": "disabled"})
    with pytest.raises(HostAPIError) as caught:
        resolve_thread_compact_policy(agent)
    assert caught.value.code == "USER_DISABLED"


def test_host_root_identity_replacement_revalidates_even_when_leaf_inodes_survive(tmp_path, monkeypatch):
    root = tmp_path / "host"
    agent = _agent(root)
    calls = _reads(monkeypatch)
    resolve_thread_compact_policy(agent)
    before = len(calls)
    saved = tmp_path / "saved-host"
    root.rename(saved)
    root.mkdir()
    for child in saved.iterdir():
        child.rename(root / child.name)
    resolve_thread_compact_policy(agent)
    assert len(calls) > before


def test_global_lock_atomic_replacement_is_seen_without_writer_invalidation(tmp_path):
    import subprocess
    import sys

    agent = _agent(tmp_path / "host")
    root = agent.home_paths.root
    service = governance.ManagedUserGovernanceService(root)
    service.set_global_compact_force_policy(False, trigger_percent=75)
    assert resolve_thread_compact_policy(agent)["force_locked"] is False
    path = root.joinpath(*governance._GLOBAL_COMPACT_POLICY_PARTS)
    subprocess.run([sys.executable, "-c", "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                    "d=json.loads(p.read_text()); d['force_compact_trigger_percent']=True; d['revision']+=1; "
                    "q=p.with_suffix('.next'); q.write_text(json.dumps(d)); q.replace(p)", str(path)], check=True)
    result = resolve_thread_compact_policy(agent)
    assert result["force_locked"] is True and result["effective_trigger_percent"] == 75


def test_snapshot_cache_is_bounded_and_contains_only_frozen_values(tmp_path):
    from dataclasses import FrozenInstanceError

    for i in range(140):
        home = SimpleNamespace(root=tmp_path / str(i), owner_provider="feishu", owner_kind="group", owner_id="group")
        home.root.mkdir()
        snapshot = governance.read_compact_governance_snapshot(home)
        with pytest.raises(FrozenInstanceError):
            snapshot.user_locked = True
    assert len(governance._COMPACT_POLICY_CACHE) <= 128


@pytest.mark.parametrize("owners", [1, 2, 4, 8, 12])
@pytest.mark.parametrize("turns", [100, 1000, 10000])
def test_multi_owner_cross_turn_parse_operation_counts(tmp_path, monkeypatch, request, owners, turns):
    import json
    from pathlib import Path

    agents = [_agent(tmp_path / "host", f"user-{i}") for i in range(owners)]
    calls = _reads(monkeypatch)
    for agent in agents:
        resolve_thread_compact_policy(agent)
    cold = len(calls)
    for index in range(turns):
        resolve_thread_compact_policy(agents[index % owners])
    assert len(calls) == cold
    observation = {"owners": owners, "turns": turns, "cold_json_reads": cold, "warm_json_reads": len(calls) - cold}
    request.node.user_properties.append(("compact_cache_measurement", json.dumps(observation)))
    output = os.environ.get("W8_MEASUREMENT_OUTPUT")
    if output:
        root = Path(output)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"cache-{owners}-{turns}.json").write_text(json.dumps(observation, indent=2))
