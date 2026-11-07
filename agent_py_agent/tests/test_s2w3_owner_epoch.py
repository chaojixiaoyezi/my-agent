"""使用真实 SQLite 事务和真实文件叶验证 owner 栅栏。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space import owner_epoch as epoch


@pytest.fixture
def repo(tmp_path):
    epoch.reset_process_activations_for_tests()
    return RuntimeRepository(tmp_path / "runtime.db")


def test_concurrent_activators_commit_distinct_consecutive_epochs(repo):
    assert epoch.read_owner_epoch(repo) == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = sorted(pool.map(lambda _: epoch.activate_owner_epoch(repo), range(2)))
    assert values == [1, 2]
    assert epoch.read_owner_epoch(repo) == 2


def test_process_activation_is_once_even_with_repo_cache_reload(repo):
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: epoch.ensure_process_owner_epoch_activated(repo), range(2))) == [1, 1]
    rebuilt = RuntimeRepository(repo.db_path)
    assert epoch.ensure_process_owner_epoch_activated(rebuilt) == 1
    assert epoch.read_owner_epoch(repo) == 1


def test_simulated_restart_stamps_new_epoch_and_rejects_old(repo):
    old = epoch.ensure_process_owner_epoch_activated(repo)
    epoch.reset_process_activations_for_tests()
    new = epoch.ensure_process_owner_epoch_activated(RuntimeRepository(repo.db_path))
    assert new == old + 1
    with pytest.raises(epoch.StaleOwnerEpoch) as error:
        epoch.require_current(repo, old)
    assert error.value.code == "STALE_OWNER_EPOCH"
    epoch.require_current(repo, new)


def test_legitimate_concurrent_sessions_same_epoch(repo):
    current = epoch.ensure_process_owner_epoch_activated(repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: epoch.require_current(repo, current), range(2))) == [None, None]


@pytest.fixture
def gateway_claim(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    rid, pending, _ = submit_gateway_ask(paths, params=GatewayAskParams("work", chat_session_id="conversation"))
    claim = claim_request(paths, pending)
    assert claim is not None
    return paths, rid, claim


def test_old_epoch_gateway_finish_keeps_original_responsibility(repo, gateway_claim):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import request_worker as worker
    paths, rid, claim = gateway_claim
    old = epoch.activate_owner_epoch(repo)
    epoch.activate_owner_epoch(repo)
    before = claim.path.read_bytes()
    config = SimpleNamespace(owner_epoch_fence_enabled=True)
    with epoch.request_epoch_scope(repo, old, config):
        worker._finish_claimed_gateway_request(paths, claim.path, rid, {"id": rid, "status": "done"},
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    assert claim.path.exists(), "stale attempt must keep processing responsibility"
    assert claim.path.read_bytes() == before
    assert not (paths.terminal / f"{rid}.json").exists()
    assert not (paths.responses / f"{rid}.json").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_gateway_finish_observes_live_switch_at_each_use(repo, gateway_claim, enabled):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import request_worker as worker
    paths, rid, claim = gateway_claim
    old = epoch.activate_owner_epoch(repo)
    epoch.activate_owner_epoch(repo)
    config = SimpleNamespace(owner_epoch_fence_enabled=not enabled)
    with epoch.request_epoch_scope(repo, old, config):
        config.owner_epoch_fence_enabled = enabled
        worker._finish_claimed_gateway_request(paths, claim.path, rid, {"id": rid, "status": "done"},
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    assert (paths.terminal / f"{rid}.json").exists() is (not enabled)


def test_recovered_attempt_new_epoch_finishes_normally(repo, gateway_claim):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import request_worker as worker
    paths, rid, claim = gateway_claim
    epoch.ensure_process_owner_epoch_activated(repo)
    epoch.reset_process_activations_for_tests()
    current = epoch.ensure_process_owner_epoch_activated(repo)
    with epoch.request_epoch_scope(repo, current, SimpleNamespace(owner_epoch_fence_enabled=True)):
        worker._finish_claimed_gateway_request(paths, claim.path, rid, {"id": rid, "status": "done"},
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    assert (paths.terminal / f"{rid}.json").is_file()


@pytest.mark.parametrize("initial", [False, True])
def test_yaml_refresh_between_claim_and_finish(repo, gateway_claim, tmp_path, monkeypatch, initial):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.settings import live_switches as live
    from agent_py_agent.agent.settings.config import load_simple_yaml

    # 本片不占用S2-INT的配置写权；在测试中只补它约定的字段/白名单接缝，刷新实现仍是真实入口。
    key = "owner_epoch_fence_enabled"
    real_load = live.load_config

    def seam_load(path):
        result = real_load(path)
        setattr(result, key, load_simple_yaml(path)[key])
        result.config_sources.applied = (*result.config_sources.applied, (key, getattr(result, key)))
        return result

    monkeypatch.setattr(live, "load_config", seam_load)
    monkeypatch.setattr(live, "LIVE_SWITCH_KEYS", live.LIVE_SWITCH_KEYS | {key})
    path = tmp_path / "live.yaml"
    path.write_text(f"{key}: {str(initial).lower()}\n")
    config = seam_load(path)
    live.register_live_config(config)
    old = epoch.activate_owner_epoch(repo)
    epoch.activate_owner_epoch(repo)
    paths, rid, claim = gateway_claim
    with epoch.request_epoch_scope(repo, old, config):
        path.write_text(f"{key}: {str(not initial).lower()}\n")
        changed = live.refresh_live_switches(config, force=True)
        assert key in changed
        assert getattr(config, key) is (not initial)
        worker._finish_claimed_gateway_request(paths, claim.path, rid, {"id": rid, "status": "done"},
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
    assert (paths.terminal / f"{rid}.json").exists() is initial


def test_claim_path_activates_before_stamping_and_reuses_on_cache_reload(repo, gateway_claim, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import request_worker as worker

    paths, rid, claim = gateway_claim
    config = SimpleNamespace(owner_epoch_fence_enabled=True, conversation_actor_workers=0)
    agent = SimpleNamespace(config=config, subagents=SimpleNamespace(runtime_db=repo),
                            conversation_store=ConversationStore(tmp_path / "conversations"))
    captured = []
    monkeypatch.setattr(worker, "_reject_worker_route", lambda *_: False)
    monkeypatch.setattr(worker, "_resolve_request_agent", lambda *_: agent)
    monkeypatch.setattr(worker, "_execute_claimed_gateway_request",
                        lambda context, *_: captured.append(context.owner_epoch) or True)
    assert worker._process_claimed_gateway_request_path(agent, paths, claim.path, "worker", claim=claim)
    agent.subagents.runtime_db = RuntimeRepository(repo.db_path)
    assert worker._process_claimed_gateway_request_path(agent, paths, claim.path, "worker", claim=claim)
    assert captured == [1, 1]
    assert epoch.read_owner_epoch(repo) == 1
