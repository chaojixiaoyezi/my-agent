"""真实pending扫描、线程执行和canonical终态；仅provider边界是离线替身。"""
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_execution import current_worker_execution
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.support.multiprocess_dispatch_cases import (
    reservation_for,
    stamped_request,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # 轻量host没有经过SimpleAgent构造，必须显式走同一生产合同注册，不能依赖其它case先初始化进程全局端口。
    from agent_py_agent.agent.core import configure_canonical_background_contracts

    configure_canonical_background_contracts()
    request = stamped_request()
    launch = mode.WorkerLaunch(WorkerPartition(request['route']['worker'], 2, 'c-p5'), tmp_path / 'gateway', tmp_path / 'home', 101)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', launch.partition)
    paths = gateway_paths_from_root(launch.gateway_root)
    ensure_gateway_folders(paths)
    owner = OwnerIdentity(*request['route']['owner'])
    hp = home_paths_with_owner(home_paths(launch.owner_root), resolve_owner_home(launch.owner_root, owner))
    agent = SimpleNamespace(home_paths=hp, root=tmp_path, conversation_store=None,
                            config=AgentConfig(my_agent_home=str(launch.owner_root), my_agent_owner_provider=owner.provider,
                            my_agent_owner_kind=owner.owner_kind, my_agent_owner_id=owner.owner_id))
    owners = []
    pool = SimpleNamespace(get=lambda selected: (owners.append(selected), agent)[1], pin=lambda _: nullcontext())
    frozen = freeze_worker_bootstrap(launch)
    return SimpleNamespace(paths=paths, request=request, pool=pool, frozen=frozen, owners=owners, owner=owner)


def enqueue(case, request):
    path = case.paths.inbox / f"{request['id']}.json"
    write_json_file_atomic(path, request)
    write_json_file_atomic(reservation_file(case.paths, request['id']), reservation_for(request))
    return path


def test_poll_runs_claim_to_terminal_without_duplicate_execution(setup, monkeypatch):
    path = enqueue(setup, setup.request)
    seen = []
    def provider(context):
        execution = current_worker_execution()
        assert execution is not None
        assert execution.binding.claim.request_id == context.request_id
        seen.append(context.request_id)
        return {'id': context.request_id, 'ok': True, 'status': 'done', 'response': 'offline-provider'}
    monkeypatch.setattr(request_worker, '_process_claimed_gateway_request', provider)
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    try:
        assert pump.poll() == 1
    finally:
        pump.close()
    terminal = json.loads((setup.paths.terminal / path.name).read_text())
    assert terminal['status'] == 'done'
    assert terminal['route'] == setup.request['route']
    assert terminal['execution_attempt_id']
    assert terminal['lease_epoch'] == 1
    assert seen == [setup.request['id']]
    assert setup.owners == [setup.owner]
    assert not path.exists()
    assert not (setup.paths.processing / path.name).exists()
    assert pump.inflight == 0
    assert pump.poll() == 0


def test_capacity_keeps_other_request_pending_and_stop_claiming_does_not_drop_active(setup, monkeypatch):
    first = enqueue(setup, setup.request)
    second_request = dict(setup.request, id='z-p5-second')
    second = enqueue(setup, second_request)
    entered, release = threading.Event(), threading.Event()
    def provider(context):
        entered.set()
        assert release.wait(10)
        return {'id': context.request_id, 'ok': True, 'status': 'done'}
    monkeypatch.setattr(request_worker, '_process_claimed_gateway_request', provider)
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    try:
        assert pump.poll() == 1
        assert entered.wait(5)
        assert pump.inflight == 1
        assert pump.poll() == 0
        assert second.exists()
        pump.stop_claiming()
        assert pump.claiming_stopped is True
        assert pump.inflight == 1
    finally:
        release.set()
        pump.close()
    assert (setup.paths.terminal / first.name).exists()
    assert second.exists()
    assert pump.poll() == 0


def test_foreign_and_unadmitted_requests_never_construct_owner(setup):
    foreign = dict(setup.request, id='foreign', route={**setup.request['route'], 'worker': 1 - setup.frozen.launch.partition.index})
    foreign_path = enqueue(setup, foreign)
    unadmitted_path = setup.paths.inbox / 'unadmitted.json'
    write_json_file_atomic(unadmitted_path, dict(setup.request, id='unadmitted'))
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    try:
        assert pump.poll() == 0
        assert setup.owners == []
        assert foreign_path.exists() and unadmitted_path.exists()
    finally:
        pump.close()


def test_owner_initialization_failure_is_visible_and_processing_is_preserved(setup):
    path = enqueue(setup, setup.request)
    def broken_pool(owner):
        raise RuntimeError('owner-unavailable')
    setup.pool.get = broken_pool
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    try:
        assert pump.poll() == 1
    finally:
        with pytest.raises(RuntimeError, match='owner-unavailable'):
            pump.close()
    assert (setup.paths.processing / path.name).exists()
    assert not (setup.paths.terminal / path.name).exists()
    assert reservation_file(setup.paths, setup.request['id']).exists()
