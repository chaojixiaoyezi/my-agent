# LLM: 使用真实C生命周期锁和规范回执；只替换业务执行观察点，不替代转发判断。
# 模块用途: 钉住远端控制只能入队、重复和查询不能在C物化owner。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.mark.parametrize("text", ["/stop", "/btw 合成插话", "/approve synthetic-token", "/admin status"])
def test_remote_control_prepares_once_without_c_effect_or_owner_construction(tmp_path, monkeypatch, text):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="room",
                                metadata={"message_id": "control-a", "expected_turn_id": "task-a",
                                          "expected_execution": {"schema_version": "gateway_execution_target.v1",
                                                                 "thread_id": "thread-a", "kind": "task", "turn_id": "task-a",
                                                                 "claim_id": "claim-a", "execution_attempt_id": "",
                                                                 "runtime_authority": {}, "task_id": "task-a"}},
                                resolved_owner=OwnerIdentity.provider_user("feishu", "alice"))
    agent = SimpleNamespace()
    calls = []

    def effect(_agent, _paths, command, _scope):
        calls.append("effect")
        return ConversationControlResult(command.kind, True, "执行了")

    def materialize(*_args):
        calls.append("owner")
        return agent

    monkeypatch.setattr(ops, "execute_gateway_conversation_control", effect)
    monkeypatch.setattr(ops, "_control_operation_agent", materialize)
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "control-generation")
        command = parse_conversation_control(text, reject_unknown_slash=True)
        first = ops.execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
        assert calls == []
        assert first.state == "prepared"
        assert first.conversation_thread_id == ""
        assert first.to_dict()["dispatch"]["c_run_id"] == "control-generation"
        replay = ops.execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
        status = ops.reconcile_gateway_control_operation(agent, paths, first.operation_id)
        assert replay.to_dict() == first.to_dict() == status.to_dict()
        assert calls == []


# LLM: 本组只fake HTTP工作片/鉴权运输，不开socket/服务/模型；C来源、回执CAS、规范物化、W原pool及控制执行均用真实产品入口。
# 类用途: 提供合成普通IM成员的固定认证事实，不作为真实G1或租户验收证明。
class _OfflineControlAuth:
    # LLM: 合成认证叶只固定普通成员；没有外部凭据读取或真实G1认证。
    # 函数用途: 返回离线控制测试的通道与成员。
    def extract_identity(self, headers, peer_ip):
        return 'member', 'feishu'

    # LLM: 不授all-user权限，保持群普通成员输入边界。
    # 函数用途: 返回本测试的最小普通成员权限。
    def get_permission(self, headers, peer_ip):
        return SimpleNamespace(can_access_all_users=False)

    # LLM: 只比较合成值，供原source末门确认测试输入未变；不访问秘密。
    # 函数用途: 让纯测试可撤销同次合成凭据。
    def has_gateway_credential(self, headers):
        return headers.get('X-Gateway-Token') == 'synthetic-control-credential'


# LLM: 只初始化宿主共享资源，不创建目标owner；实际HTTP工作片是明确fake边界，原source其余生命周期/正文/回执/owner/Root门保持。
# 函数用途: 创建无监听器、无实际进程或模型调用的首次远端控制场景。
@pytest.fixture
def first_owner(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from copy import deepcopy

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import coordinator_control_transport as transport
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPHandler,
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    # LLM: 每场独立临时home，只由真实宿主初始化共享资源；不会预建目标owner或启动监听器。
    # 函数用途: 在原C lease内装配可清理的离线控制场景。
    @contextmanager
    def build(*, ready=False, group=True, command='/delegate enable'):
        home, root = (tmp_path/'home').resolve(), (tmp_path/'repo').resolve()
        monkeypatch.setenv('MY_AGENT_HOME', str(home))
        config = AgentConfig(my_agent_home=str(home), model_backend='echo', enable_plugins=False,
                             gateway_per_user_owner_scoping=True, enable_subagents=True)
        agent = SimpleAgent(deepcopy(config), root)
        owner = (OwnerIdentity.provider_group('feishu', 'new-room') if group
                 else OwnerIdentity.provider_user('feishu', 'member'))
        target = resolve_owner_home(home, owner).home_dir
        assert not target.exists()
        paths = gateway_paths_from_root(tmp_path/'gateway')
        with coordinator_guard(paths.root, 2):
            controller = prepare_coordinator_takeover(paths, 2, 'first-control-C')
            control = GatewayControlPlane(config, home, paths, 2, controller.c_run_id,
                                          agent_root=root, workspace_roots=(root,))
            agent._gateway_control_plane = control
            runtime = GatewayDrainRuntime(control)
            server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(
                agent=agent if ready else None, control_plane=control, drain_runtime=runtime))
            handler = object.__new__(GatewayHTTPHandler)
            handler.headers = {'X-Gateway-Token': 'synthetic-control-credential'}
            handler.client_address = ('127.0.0.1', 1)
            handler._auth_middleware = _OfflineControlAuth()
            body = {'command': command, 'conversation_id': 'new-room' if group else 'member',
                    'metadata': {'message_id': 'first-control', 'channel_chat_type': 'group' if group else 'p2p',
                                 'channel_chat_id': 'new-room' if group else 'member'}}
            work, active, responses = object(), [True], []

            # LLM: 唯一运输fake只确认当前fixture对象和原Root未closing；不替换source、resolver、receipt、controller或物化叶。
            # 函数用途: 让纯测试不用启动HTTP监听器，同时可使工作片失效验证零首写。
            def require_work(actual, actual_handler, actual_server):
                if (actual is not work or actual_handler is not handler or actual_server is not server
                        or not active[0] or runtime._closing):
                    raise ValueError('offline HTTP work unavailable')
                runtime.require_original()
                return actual

            monkeypatch.setattr(transport, 'capture_http_handler_work', lambda h, s: require_work(work, h, s))
            monkeypatch.setattr(transport, 'require_http_handler_work', require_work)
            handler._read_json = lambda: deepcopy(body)
            handler._send_json = lambda code, payload: responses.append((code, payload))
            case = SimpleNamespace(agent=agent, config=config, home=home, root=root, owner=owner,
                target=target, paths=paths, controller=controller, control=control, runtime=runtime,
                server=server, handler=handler, body=body, active=active, responses=responses)
            yield case
    return build


# LLM: 原W冻结/registry/P0及pool均为真实本进程组件，只有产品Worker线程会执行控制；不预建owner，不替业务服务写偏好。
# 函数用途: 消费C已发表的唯一回执并返回原终态，结束自有控制池线程。
def _consume_first_owner(case, monkeypatch, operation_id):
    import os
    import time

    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
        registered_worker_heartbeat,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_control_worker import WorkerControlPump
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool

    launch = mode.WorkerLaunch(WorkerPartition(owner_worker(case.owner, 2), 2, case.controller.c_run_id),
        case.paths.root, case.home, os.getppid(), 'offline-first-control', workspace_roots=(case.root,))
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', launch.partition)
        frozen = freeze_worker_bootstrap(launch)
        heartbeat = registered_worker_heartbeat(launch, launch.instance_id,
            {'ready': True, 'gateway_ports': [1]}, time.time())
        registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': launch.partition.c_run_id,
            'worker_count': 2, 'process_identity': heartbeat['process_identity'], 'http_port': 1,
            'workers': [{'worker': launch.partition.index, 'instance_id': launch.instance_id,
                'process_identity': heartbeat['process_identity'],
                'executor_ledger_ref': f'workers/executors/w{launch.partition.index}'}]}
        write_json_file_atomic(case.paths.root/'workers/registry.json', registry)
        write_json_file_atomic(case.paths.root/'workers'/f'w{launch.partition.index}.json', heartbeat)
        pool = create_worker_pool(frozen, case.config, case.root)
        pump = WorkerControlPump(frozen, pool)
        try:
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                pump.poll()
                receipt = ops.read_gateway_control_operation(case.paths, operation_id, read_only=True)
                if receipt.state in {'completed', 'terminal_unknown'}:
                    return receipt, pool.peek(case.owner)
                time.sleep(.01)
            pytest.fail('offline control did not settle')
        finally:
            pump.close()


@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('group', [True, False], ids=['group', 'user'])
def test_first_remote_control_materializes_owner_in_c_before_readonly_worker(first_owner, monkeypatch, ready, group):
    from agent_py_agent.agent.gateway_parts.http_handlers import handle_control
    from agent_py_agent.agent.settings.im_task_preferences import delegation_preference

    with first_owner(ready=ready, group=group) as case:
        # 只替换最外层HTTP鉴权返回；原source每个末门仍重核合成middleware凭据。
        monkeypatch.setattr('agent_py_agent.agent.gateway_parts.http_handlers.require_trusted_source', lambda h: False)
        handle_control(case.handler, case.server)
        status, result = case.responses[-1]
        assert status == 202 and result['control_state'] == 'prepared', case.responses
        receipt, actual = _consume_first_owner(case, monkeypatch, result['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        assert case.target.is_dir(), 'C must materialize the canonical cold owner before publishing dispatch'
        assert actual.home_paths.owner_home_dir == case.target
        assert delegation_preference(actual, 'feishu', 'member')['enabled']
        assert not delegation_preference(actual, 'feishu', 'other-member')['enabled']
        assert case.server.agent is (case.agent if ready else None)
        before = {p.relative_to(case.target): p.read_bytes() for p in case.target.rglob('*') if p.is_file()}
        handle_control(case.handler, case.server)
        assert case.responses[-1][0] == 200
        assert {p.relative_to(case.target): p.read_bytes() for p in case.target.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('failure', ['work', 'credential', 'body', 'agent', 'root_closing', 'quarantine', 'link'])
def test_first_control_rejects_changed_authority_or_unsafe_owner_before_materialization(first_owner, monkeypatch, failure):
    from agent_py_agent.agent.gateway_parts.coordinator_control_transport import (
        _CoordinatorControlTransport,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
        WorkerSharedResourceError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest

    with first_owner(ready=True) as case:
        source = _CoordinatorControlTransport(case.handler, case.server, case.body)
        receipt = ops._new_control_operation_receipt(source.scope, source.command.kind, source.command_text,
                                                    source.operation_id, source.input_digest)
        try:
            if failure == 'work':
                case.active[0] = False
            elif failure == 'credential':
                case.handler.headers.clear()
            elif failure == 'body':
                case.body['command'] = '/delegate disable'
            elif failure == 'agent':
                case.server.agent = None
            elif failure == 'root_closing':
                case.runtime._closing = True
            elif failure == 'quarantine':
                case.controller._unproven_owners.add(owner_digest(case.owner))
            else:
                outside = case.home.parent/'outside'
                outside.mkdir()
                case.target.parent.mkdir(parents=True)
                case.target.symlink_to(outside, target_is_directory=True)
            with pytest.raises((GlobalWriteError, WorkerSharedResourceError)):
                source.publish(receipt, None)
            assert not ops.gateway_control_operation_path(case.paths, receipt.operation_id).exists()
            if failure == 'link':
                assert not list(outside.iterdir())
            else:
                assert not case.target.exists()
        finally:
            source.close()


@pytest.mark.parametrize('ready,change', [(False, 'ready'), (True, 'replacement'), (True, 'home'), (True, 'control')])
def test_control_source_keeps_exact_cold_or_ready_agent_binding(first_owner, ready, change):
    from agent_py_agent.agent.gateway_parts.coordinator_control_transport import (
        _CoordinatorControlTransport,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError

    with first_owner(ready=ready) as case:
        source = _CoordinatorControlTransport(case.handler, case.server, case.body)
        try:
            if change == 'ready':
                case.server.agent = case.agent
            elif change == 'replacement':
                case.server.agent = object()
            elif change == 'home':
                case.agent.home_paths = SimpleNamespace(root=case.home/'foreign')
            else:
                case.agent._gateway_control_plane = object()
            with pytest.raises(GlobalWriteError):
                source.require_current()
            assert not case.target.exists()
        finally:
            source.close()


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('state', ['prepared', 'claimed', 'executing', 'completed', 'terminal_unknown'])
def test_existing_receipt_and_status_never_materialize_missing_owner(first_owner, ready, state):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts.coordinator_control_transport import (
        _CoordinatorControlTransport,
    )
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import (
        read_remote_control_status,
    )

    with first_owner(ready=ready) as case:
        source = _CoordinatorControlTransport(case.handler, case.server, case.body)
        original = ops._new_control_operation_receipt(source.scope, source.command.kind, source.command_text,
                                                     source.operation_id, source.input_digest)
        import os

        from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id
        from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import (
            _control_dispatch_for_receipt,
        )
        from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
        route = _control_dispatch_for_receipt(original, source.lease, source.lease.c_run_id, None)
        if state in {'claimed', 'executing', 'completed'}:
            route.update(executor={'schema_version': 'multiprocess_process_identity.v1',
                'host_id': process_host_id(), 'pid': os.getpid(), 'start_time': process_birth(os.getpid())},
                instance_id='synthetic-original-executor')
        stored = replace(original, state=state, dispatch=route,
            result=ConversationControlResult(original.command_kind, True, '合成原结果').to_dict() if state == 'completed' else {},
            error={'error_code': 'ORIGINAL_UNKNOWN'} if state == 'terminal_unknown' else {})
        assert ops.GatewayControlOperationReceipt.from_dict(stored.to_dict()) == stored
        path = ops.gateway_control_operation_path(case.paths, stored.operation_id)
        write_json_file_atomic(path, stored.to_dict())
        before = path.read_bytes()
        try:
            assert source.publish(original, None) == stored
            assert read_remote_control_status(case.paths, stored, agent=case.agent) == stored
            assert path.read_bytes() == before and not case.target.exists()
        finally:
            source.close()


@pytest.mark.parametrize('ready', [False, True])
def test_refused_target_control_does_not_materialize_owner(first_owner, ready):
    from agent_py_agent.agent.gateway_parts.coordinator_control_transport import (
        _CoordinatorControlTransport,
    )

    with first_owner(ready=ready, command='/stop') as case:
        source = _CoordinatorControlTransport(case.handler, case.server, case.body)
        receipt = ops._new_control_operation_receipt(source.scope, source.command.kind, source.command_text,
                                                    source.operation_id, source.input_digest)
        try:
            result = source.publish(receipt, None)
            assert result.dispatch is None and result.state == 'completed' and not result.result['ok']
            assert not case.target.exists()
        finally:
            source.close()


def test_missing_first_owner_is_exact_resource_missing_and_worker_never_seeds(first_owner, monkeypatch):
    import os

    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
        WorkerSharedResourceError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool

    with first_owner() as case:
        launch = mode.WorkerLaunch(WorkerPartition(owner_worker(case.owner, 2), 2, case.controller.c_run_id),
            case.paths.root, case.home, os.getppid(), 'offline-missing-owner', workspace_roots=(case.root,))
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', launch.partition)
            pool = create_worker_pool(freeze_worker_bootstrap(launch), case.config, case.root)
            with pytest.raises(WorkerSharedResourceError) as failure:
                pool.get(case.owner)
            assert (failure.value.reason, failure.value.field) == ('resource_missing', 'owner_directory_0')
            assert not case.target.exists()


@pytest.mark.parametrize('ready', [False, True])
def test_local_main_ready_falls_through_and_cold_stays_waiting(first_owner, monkeypatch, ready):
    from agent_py_agent.agent.gateway_parts import http_handlers

    with first_owner(ready=ready) as case:
        case.handler._auth_middleware.extract_identity = lambda headers, peer: ('local-agent', 'local')
        calls = []
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        monkeypatch.setattr(http_handlers, '_handle_persistent_control_operation', lambda *a, **k: calls.append(True))
        http_handlers.handle_control(case.handler, case.server)
        assert calls == ([True] if ready else [])
        if not ready:
            assert case.responses[-1][0] == 503
            assert case.responses[-1][1]['error_code'] == 'COORDINATOR_BUSINESS_NOT_READY'
        assert not case.target.exists()


@pytest.mark.parametrize('actor', ['worker', 'executor'])
def test_extracted_c_owner_materializer_rejects_restricted_actors_without_writes(first_owner, monkeypatch, actor):
    from agent_py_agent.agent.common import global_write_guard as guard
    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.multiprocess_native_materialize import (
        materialize_coordinator_owner_home,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition

    with first_owner() as case:
        monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
        monkeypatch.setattr(guard, '_restricted_executor', False)
        if actor == 'worker':
            monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'synthetic-restricted-W'))
            error_type = guard.GlobalWriteInWorkerError
        else:
            guard.restrict_executor_global_writes()
            error_type = guard.GlobalWriteInExecutorError
        before = {p.relative_to(case.home): p.read_bytes() for p in case.home.rglob('*') if p.is_file()}
        with pytest.raises(error_type) as failure:
            materialize_coordinator_owner_home(case.home, case.owner)
        assert failure.value.resource == 'owner_materialization'
        assert not case.target.exists()
        assert {p.relative_to(case.home): p.read_bytes() for p in case.home.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('text', ['/delegate enable', '/delegate status', '/status', '/schedule'])
@pytest.mark.parametrize('field', ['prompt', 'goal'])
@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('group', [True, False], ids=['group', 'user'])
def test_first_ask_slash_materializes_same_owner_before_worker(first_owner, monkeypatch, text, field, ready, group):
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.settings.im_task_preferences import delegation_preference

    with first_owner(ready=ready, group=group, command=text) as case:
        case.body.pop('command')
        case.body[field] = text
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        assert not case.target.exists(), 'fixture must not initialize the incoming owner'
        http_handlers.handle_ask(case.handler, case.server, lambda: 'ordinary-request-must-not-be-created')
        status, result = case.responses[-1]
        assert status == 202 and result['control_state'] == 'prepared', case.responses
        receipt, actual = _consume_first_owner(case, monkeypatch, result['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        assert receipt.owner_provider == case.owner.provider
        assert receipt.owner_kind == case.owner.owner_kind
        assert receipt.owner_id == case.owner.owner_id
        assert actual.home_paths.owner_home_dir == case.target
        assert case.target.is_dir()
        assert delegation_preference(actual, 'feishu', 'member')['enabled'] is (text == '/delegate enable')
        assert not delegation_preference(actual, 'feishu', 'other-member')['enabled']
        original = ops.gateway_control_operation_path(case.paths, receipt.operation_id).read_bytes()
        before = {p.relative_to(case.target): p.read_bytes() for p in case.target.rglob('*') if p.is_file()}
        http_handlers.handle_ask(case.handler, case.server, lambda: 'ordinary-request-must-not-be-created')
        assert case.responses[-1][0] == 200
        assert ops.gateway_control_operation_path(case.paths, receipt.operation_id).read_bytes() == original
        assert {p.relative_to(case.target): p.read_bytes() for p in case.target.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('failure', ['work', 'credential', 'root_closing', 'quarantine', 'home', 'link'])
@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
def test_ask_slash_rejects_lost_authority_before_owner_or_dispatch(first_owner, monkeypatch, failure, ready):
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
        WorkerSharedResourceError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest

    with first_owner(ready=ready) as case:
        case.body['prompt'] = case.body.pop('command')
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        if failure == 'work':
            case.active[0] = False
        elif failure == 'credential':
            case.handler.headers.clear()
        elif failure == 'root_closing':
            case.runtime._closing = True
        elif failure == 'quarantine':
            case.controller._unproven_owners.add(owner_digest(case.owner))
        elif failure == 'home':
            if ready:
                case.agent.home_paths = SimpleNamespace(root=case.home/'foreign')
            else:
                case.server.agent = object()
        else:
            outside = case.home.parent/'outside'
            outside.mkdir()
            case.target.parent.mkdir(parents=True)
            case.target.symlink_to(outside, target_is_directory=True)
        if failure == 'link':
            with pytest.raises(WorkerSharedResourceError):
                http_handlers.handle_ask(case.handler, case.server, lambda: 'unused')
            assert not list(outside.iterdir())
        else:
            http_handlers.handle_ask(case.handler, case.server, lambda: 'unused')
            assert case.responses[-1][0] == 503
            assert not case.target.exists()
        assert not list((case.paths.root/'control_operations').glob('*.json'))


@pytest.mark.parametrize('field', ['prompt', 'goal'])
def test_ask_slash_preserves_goal_prompt_authority_over_body_command(first_owner, monkeypatch, field):
    from agent_py_agent.agent.gateway_parts import http_handlers

    with first_owner(ready=True) as case:
        case.body['command'] = '/model'
        case.body['prompt'] = '/delegate enable' if field == 'prompt' else '/delegate disable'
        if field == 'goal':
            case.body['goal'] = '/delegate enable'
        before = dict(case.body)
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        http_handlers.handle_ask(case.handler, case.server, lambda: 'unused')
        assert case.responses[-1][0] == 202
        receipt = ops.read_gateway_control_operation(case.paths, case.responses[-1][1]['operation_id'], read_only=True)
        assert receipt.command_text == '/delegate enable' and receipt.command_kind == 'delegate'
        assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == ('feishu', 'group', 'new-room')
        assert case.body == before, 'transport adaptation must not rewrite the incoming body'


@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('text', ['/model', '/agents', '/memory candidates', '/delegate invalid'])
def test_ask_special_or_invalid_slash_keeps_original_boundary(tmp_path, monkeypatch, ready, text):
    # 588明确迁移585新增的special冷503预期；真实领域/回执重放替代selector mock，不改变非法语法的旧边界。
    from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
    from agent_py_agent.tests.test_coordinator_model_menu import post_model
    from agent_py_agent.tests.test_gateway_ask_control_parity import control_files, parity_request

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=ready, text=text)
        before = control_files(server)
        status, result = post_model(server, body, route='/ask', actor=row['actor_id'])
        if text == '/delegate invalid':
            assert status == (200 if ready else 503) and not result.get('ok')
            assert control_files(server) == before and not delivered
        else:
            assert status == 200 and result['ok'] and result['control_state'] == 'completed', result
            receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
            assert receipt.state == 'completed' and receipt.dispatch is None and receipt.command_text == text
            assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == ('feishu', 'user', row['actor_id'])
            after = control_files(server)
            assert post_model(server, body, route='/ask', actor=row['actor_id']) == (status, result)
            assert control_files(server) == after
            assert len(delivered) == (1 if text == '/model' else 0)
        assert server.agent is (agent if ready else None)


@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
def test_ask_main_owner_never_materializes_remote_owner(first_owner, monkeypatch, ready):
    from agent_py_agent.agent.gateway_parts import http_handlers

    with first_owner(ready=ready) as case:
        case.body['prompt'] = case.body.pop('command')
        case.handler._auth_middleware.extract_identity = lambda headers, peer: ('local-agent', 'local')
        calls = []
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        monkeypatch.setattr(http_handlers, '_handle_persistent_control_operation', lambda *a, **k: calls.append(True))
        http_handlers.handle_ask(case.handler, case.server, lambda: 'unused')
        assert calls == ([True] if ready else [])
        if not ready:
            assert case.responses[-1][0] == 503
        assert not case.target.exists()


@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
def test_ask_target_refusal_never_materializes_owner(first_owner, monkeypatch, ready):
    from agent_py_agent.agent.gateway_parts import http_handlers

    with first_owner(ready=ready, command='/stop') as case:
        case.body['prompt'] = case.body.pop('command')
        monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
        http_handlers.handle_ask(case.handler, case.server, lambda: 'unused')
        assert case.responses[-1][0] == 200
        receipt = ops.read_gateway_control_operation(case.paths, case.responses[-1][1]['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.dispatch is None and not receipt.result['ok']
        assert not case.target.exists()


@pytest.mark.parametrize('text', ['/delegate enable', '/delegate status', '/status', '/schedule'])
def test_single_process_first_ask_slash_keeps_original_execution(tmp_path, monkeypatch, text):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPHandler,
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    home, root = (tmp_path/'home').resolve(), (tmp_path/'repo').resolve()
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    config = AgentConfig(my_agent_home=str(home), model_backend='echo', enable_plugins=False,
                         gateway_per_user_owner_scoping=True, enable_subagents=True)
    agent = SimpleAgent(config, root)
    paths = gateway_paths_from_root(tmp_path/'gateway')
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    handler = object.__new__(GatewayHTTPHandler)
    handler.headers = {'X-Gateway-Token': 'synthetic-control-credential'}
    handler.client_address = ('127.0.0.1', 1)
    handler._auth_middleware = _OfflineControlAuth()
    body = {'prompt': text, 'conversation_id': 'new-room', 'metadata': {'message_id': 'single-control',
            'channel_chat_type': 'group', 'channel_chat_id': 'new-room'}}
    responses = []
    handler._read_json = lambda: dict(body)
    handler._send_json = lambda status, payload: responses.append((status, payload))
    monkeypatch.setattr(http_handlers, 'require_trusted_source', lambda h: False)
    monkeypatch.setattr(http_handlers, '_handle_coordinator_control_operation',
                        lambda *args, **kwargs: pytest.fail('Count0 entered the C transport'))
    target = resolve_owner_home(home, OwnerIdentity.provider_group('feishu', 'new-room')).home_dir
    assert not target.exists()
    http_handlers.handle_ask(handler, server, lambda: 'unused')
    assert responses[-1][0] == 200
    receipt = ops.read_gateway_control_operation(paths, responses[-1][1]['operation_id'], read_only=True)
    assert receipt.state == 'completed' and receipt.result['ok'] and receipt.dispatch is None
    assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == ('feishu', 'group', 'new-room')
    assert target.is_dir()
