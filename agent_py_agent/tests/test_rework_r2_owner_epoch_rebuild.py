"""R2：真实 SQLite 自愈、queue/claim 与终态栅栏；仅业务执行使用替身。"""
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.io.sqlite_scope import _file_identity
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space import owner_epoch as epoch


@pytest.fixture(autouse=True)
def isolated_activations():
    epoch.reset_process_activations_for_tests()
    yield
    epoch.reset_process_activations_for_tests()


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "owner" / "runtime" / "runtime.db")


@pytest.fixture
def gateway(repo, tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    agent = SimpleNamespace(
        config=SimpleNamespace(owner_epoch_fence_enabled=True, conversation_actor_workers=0),
        subagents=SimpleNamespace(runtime_db=repo),
        conversation_store=ConversationStore(tmp_path / "owner" / "conversations"),
    )
    captured = []
    monkeypatch.setattr(worker, "_reject_worker_route", lambda *_: False)
    monkeypatch.setattr(worker, "_resolve_request_agent", lambda *_: agent)

    def execute(context, paths, _timing, expected):
        captured.append(context.owner_epoch)
        worker._finish_claimed_gateway_request(
            paths, context.processing_path, context.request_id,
            {"id": context.request_id, "status": "done"},
            expected_execution_attempt_id=expected[0], expected_lease_epoch=expected[1],
        )
        return True

    monkeypatch.setattr(worker, "_execute_claimed_gateway_request", execute)
    return SimpleNamespace(agent=agent, paths=paths, captured=captured)


def _submit_claim(gateway):
    request_id, pending, _ = submit_gateway_ask(
        gateway.paths, params=GatewayAskParams("work", chat_session_id="conversation"),
    )
    claim = claim_request(gateway.paths, pending)
    assert claim is not None
    assert claim.request_id == request_id
    return claim


def _process_and_assert_terminal(gateway):
    claim = _submit_claim(gateway)
    assert worker._process_claimed_gateway_request_path(
        gateway.agent, gateway.paths, claim.path, "worker", claim=claim,
    )
    terminal = json.loads((gateway.paths.terminal / f"{claim.request_id}.json").read_text())
    response = {"id": claim.request_id, "status": "done"}
    assert terminal["schema_version"] == "gateway_terminal_request.v1"
    assert terminal["terminal_response"] == response
    assert json.loads((gateway.paths.responses / f"{claim.request_id}.json").read_text()) == response
    assert not claim.path.exists()
    return gateway.captured[-1]


def _delete_owner_and_rematerialize_parent(repo, tmp_path):
    # 临时移开旧库保留 inode，保证普通分支确实经历物理替换；随后删除整个 owner。
    repo.db_path.replace(tmp_path / "retired-runtime.db")
    shutil.rmtree(repo.db_path.parent.parent)
    # 对应正常 owner 物化；不重建 Repository、不建表，也不清 activation 缓存。
    repo.db_path.parent.mkdir(parents=True)


@pytest.mark.parametrize("reuse_identity", [False, True], ids=["changed-inode", "reused-inode"])
def test_real_queue_claim_finishes_with_epochs_one_two_two(gateway, repo, tmp_path, monkeypatch, reuse_identity):
    identity = _file_identity(repo.db_path)
    if reuse_identity:
        # 确定性模拟文件系统复用 inode；DB、schema 自愈、epoch 与 queue/claim 均是真实的。
        monkeypatch.setattr(epoch, "_file_identity", lambda _path: identity, raising=False)
    assert _process_and_assert_terminal(gateway) == 1
    original_repository = gateway.agent.subagents.runtime_db
    _delete_owner_and_rematerialize_parent(repo, tmp_path)
    assert epoch.read_owner_epoch(repo) == 0
    assert _file_identity(repo.db_path) != identity
    assert _process_and_assert_terminal(gateway) == 2
    assert _process_and_assert_terminal(gateway) == 2
    assert gateway.captured == [1, 2, 2]
    assert gateway.agent.subagents.runtime_db is original_repository
    with pytest.raises(epoch.StaleOwnerEpoch) as error:
        epoch.require_current(repo, 1)
    assert (error.value.expected, error.value.current) == (1, 2)
    epoch.require_current(repo, 2)


def test_old_captured_claim_cannot_finish_after_database_rebuild(gateway, repo, tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(worker, "_execute_claimed_gateway_request",
                        lambda context, *_: captured.append(context.owner_epoch) or True)
    claim = _submit_claim(gateway)
    assert worker._process_claimed_gateway_request_path(
        gateway.agent, gateway.paths, claim.path, "worker", claim=claim,
    )
    assert captured == [1]
    before = claim.path.read_bytes()
    _delete_owner_and_rematerialize_parent(repo, tmp_path)
    assert epoch.ensure_process_owner_epoch_activated(repo) == 2
    worker._finish_claimed_gateway_request(
        gateway.paths, claim.path, claim.request_id, {"id": claim.request_id, "status": "done"},
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    assert claim.path.read_bytes() == before
    assert not (gateway.paths.terminal / f"{claim.request_id}.json").exists()
    assert not (gateway.paths.responses / f"{claim.request_id}.json").exists()


def test_repository_object_reload_does_not_advance_epoch(repo):
    assert epoch.ensure_process_owner_epoch_activated(repo) == 1
    rebuilt = RuntimeRepository(repo.db_path)
    assert epoch.ensure_process_owner_epoch_activated(rebuilt) == 1
    assert epoch.read_owner_epoch(repo) == 1


def test_other_activation_does_not_make_old_domain_follow_new_epoch(gateway, repo):
    assert _process_and_assert_terminal(gateway) == 1
    assert epoch.activate_owner_epoch(RuntimeRepository(repo.db_path)) == 2
    assert epoch.ensure_process_owner_epoch_activated(repo) == 1
    claim = _submit_claim(gateway)
    before = claim.path.read_bytes()
    with pytest.raises(epoch.StaleOwnerEpoch) as error:
        worker._process_claimed_gateway_request_path(
            gateway.agent, gateway.paths, claim.path, "worker", claim=claim,
        )
    assert (error.value.expected, error.value.current) == (1, 2)
    assert gateway.captured == [1]
    # 调度遥测可更新，但旧域不得清理 processing 责任或提交终态。
    after = json.loads(claim.path.read_text())
    original = json.loads(before)
    for key in ("id", "execution_attempt_id", "lease_epoch", "lease_owner", "status"):
        assert after[key] == original[key]
    assert not (gateway.paths.terminal / f"{claim.request_id}.json").exists()
    assert not (gateway.paths.responses / f"{claim.request_id}.json").exists()


def test_physical_replacement_with_nonzero_epoch_reactivates(repo, tmp_path):
    assert epoch.ensure_process_owner_epoch_activated(repo) == 1
    identity = _file_identity(repo.db_path)
    _delete_owner_and_rematerialize_parent(repo, tmp_path)
    assert epoch.activate_owner_epoch(repo) == 1
    assert _file_identity(repo.db_path) != identity
    assert epoch.ensure_process_owner_epoch_activated(repo) == 2
    epoch.require_current(repo, 2)


@pytest.mark.parametrize("current,minimum,expected", [(0, 0, 1), (0, 7, 8), (4, 2, 5), (4, 7, 8), (4, 4, 5)])
def test_activation_allocates_above_persisted_epoch_and_minimum(repo, current, minimum, expected):
    if current:
        with repo.transaction() as conn:
            conn.execute("INSERT INTO metadata(key, value) VALUES('owner_epoch', ?)", (str(current),))
    assert epoch.activate_owner_epoch(repo, minimum=minimum) == expected
    assert epoch.read_owner_epoch(repo) == expected


def test_concurrent_minimum_allocations_are_serialized(repo):
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = sorted(pool.map(lambda _: epoch.activate_owner_epoch(repo, minimum=7), range(2)))
    assert values == [8, 9]
    assert epoch.read_owner_epoch(repo) == 9
