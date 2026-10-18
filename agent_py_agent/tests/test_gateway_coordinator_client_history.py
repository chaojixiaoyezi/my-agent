"""真实冷C只读历史：原认证、owner Store和分页显示，不物化Agent或创建冷目录。"""
import copy
import hashlib
import http.client
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import client_service, request_worker
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent


# LLM: 此载体仅保存测试实际构造的原对象；不作宿主或权限替身，测试不得给它补Agent接口。
# 类用途: 在临时HTTP测试里传递真实C、原runtime和预先写好的canonical会话。
@dataclass
class HistoryCase:
    server: GatewayHTTPServer
    config: AgentConfig
    stores: dict
    threads: dict
    home: Path


# LLM: 快照只读测试根的名称、原字节和mtime，不读用户home；能发现读入口补目录、索引或重写账本。
# 函数用途: 为无副作用断言记录本次临时owner树。
def snapshot(root):
    return {str(p.relative_to(root)): (p.is_dir(), None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(),
                                     p.stat().st_mtime_ns)
            for p in root.rglob('*')}


# LLM: 会话由原Store写者在进入C之前准备，媒体只用canonical native IR，不读取图片或调用供应商。
# 函数用途: 为每个owner/actor生成可分页的真实用户、过程和final记录。
def seed(config, base_home, owner, actor, conversation):
    scoped = home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))
    selected = config if owner == OwnerIdentity.local_main() else replace(config, conversation_workspace='')
    root = resolve_runtime_paths_for_agent(selected, scoped.owner_home_dir, scoped).paths['conversation_workspace']
    store = ConversationStore(root)
    thread = store.threads.get_or_create({'canonical_user_id': actor, 'channel': 'local',
        'channel_conversation_id': conversation, 'channel_user_id': actor})
    for turn in range(8):
        request_id = f'{actor}-turn-{turn}'
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': f'{actor}问题{turn}',
                               'metadata': {'gateway_request_id': request_id}})
        store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '过程说明',
                               'metadata': {'gateway_request_id': request_id, 'assistant_part_id': 'commentary:1'}})
        native = canonical_native_messages_envelope([
            {'role': 'user', 'content': [{'type': 'text', 'text': f'{actor}问题{turn}'},
                {'type': 'image', 'source': {'type': 'local_file', 'path': str(base_home.root/'fixture.png'),
                  'name': 'fixture.png', 'mime_type': 'image/png', 'sha256': 'a'*64}}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': f'{actor}回答{turn}'}]},
        ])
        store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': f'{actor}回答{turn}',
            'metadata': {'gateway_request_id': request_id, 'assistant_part_id': 'final',
                         'canonical_native_messages': native}})
    return store, thread


# LLM: 本fixture只用真实C lease/controller/runtime及临时Store；禁止业务Agent或foreign pool冷构造，所有线程由原server退出。
# 函数用途: 启动一次显式隔离home的回环HTTP设施，不启动W、模型或日常服务。
@pytest.fixture
def case(tmp_path, monkeypatch):
    root = (tmp_path/'home').resolve()
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    assert root != (Path.home()/'.my-agent').resolve()
    config = AgentConfig(my_agent_home=str(root), gateway_per_user_owner_scoping=True,
                         conversation_workspace=str(tmp_path/'main-conversations'),
                         enable_plugins=False, enable_subagents=False, model_backend='echo')
    base = home_paths(root)
    stores, threads = {}, {}
    for actor in ('alice', 'bob'):
        stores[actor], threads[actor] = seed(config, base, OwnerIdentity.provider_user('local', actor), actor, 'shared-name')
        stores['group-'+actor], threads['group-'+actor] = seed(config, base, OwnerIdentity.provider_group('local', 'group-1'), actor, 'group-1')
    stores['main'], threads['main'] = seed(config, base, OwnerIdentity.local_main(), 'local-agent', 'main-session')
    def forbidden(*_args, **_kwargs):
        raise AssertionError('历史读取不能构造业务Agent或foreign pool')
    from agent_py_agent.agent.core import SimpleAgent
    monkeypatch.setattr(SimpleAgent, '__init__', forbidden)
    monkeypatch.setattr(request_worker, '_owner_pool', forbidden)
    monkeypatch.setattr(request_worker, '_record_active_owner', forbidden)
    paths = gateway_paths_from_root(tmp_path/'gateway')
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, 'history-C')
        control = GatewayControlPlane(config, root, paths, 2, controller.c_run_id, agent_root=tmp_path/'workspace')
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(control_plane=control, drain_runtime=runtime))
        try:
            server.start()
            yield HistoryCase(server, config, stores, threads, root)
        finally:
            server.stop()


# LLM: 使用原server刚准备的临时G1凭据，保真实重复HTTP头；不mock middleware或正文中的身份字段。
# 函数用途: 发送一条原HTTP请求，返回完整安全JSON，额外头可用于歧义负例。
def post(case, body=None, *, actor='alice', path='/client/history', credential=True, headers=()):
    connection = http.client.HTTPConnection('127.0.0.1', case.server.server.server_address[1], timeout=3)
    raw = json.dumps(body if body is not None else {'conversation_id': 'shared-name', 'limit': 2}).encode()
    try:
        connection.putrequest('POST', path)
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', str(len(raw)))
        if credential:
            connection.putheader('X-Gateway-Token', case.server._local_client_credential)
        connection.putheader('X-User-Id', actor)
        connection.putheader('X-Channel', 'local')
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(raw)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_cold_c_history_reads_original_store_without_agent_or_any_owner_write(case):
    before = snapshot(case.home)
    status, result = post(case)
    assert status == 200 and result['ok'] is True, result
    assert result['thread_id'] == case.threads['alice'].thread_id
    assert [turn['assistant_message'] for turn in result['turns']] == ['alice回答6', 'alice回答7']
    assert all('bob' not in str(event) for event in result['display_events'])
    assert any('fixture.png' in str(event) for event in result['display_events'])
    assert snapshot(case.home) == before
    assert case.server.agent is None


@pytest.mark.parametrize('actor', ['alice', 'bob', 'local-agent'])
def test_original_store_leaf_and_count0_wrapper_keep_identical_history(case, monkeypatch, actor):
    from types import SimpleNamespace
    key, conversation = ('main', 'main-session') if actor == 'local-agent' else (actor, 'shared-name')
    store = case.stores[key]
    # Count0 wrapper使用已存在owner Agent的Store；此测试载体不传给C或作为scope权威。
    monkeypatch.setattr(client_service, 'resolve_gateway_scope_agent', lambda *_args: SimpleNamespace(conversation_store=store))
    expected = client_service.read_gateway_client_history(object(), scope=GatewayControlScope(actor, 'local', conversation), max_turns=2).to_dict()
    status, result = post(case, {'conversation_id': conversation, 'limit': 2}, actor=actor)
    assert status == 200 and result == json.loads(json.dumps(expected))


def test_group_history_keeps_exact_authenticated_actor_and_original_media(case):
    for actor in ('alice', 'bob'):
        body = {'conversation_id': 'group-1', 'user_id': 'other', 'channel': 'chat',
                'metadata': {'user_id': 'other', 'channel': 'chat', 'channel_chat_type': 'group', 'channel_chat_id': 'group-1'}}
        status, result = post(case, body, actor=actor)
        assert status == 200 and result['ok']
        assert result['thread_id'] == case.threads['group-'+actor].thread_id
        assert all(turn['user_message'].startswith(actor) for turn in result['turns'])


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'cursor_bool', 'cursor_negative', 'cursor_string'])
def test_original_credential_identity_and_cursor_rejections_precede_store(case, monkeypatch, fault):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('拒绝的请求不得打开历史Store')
    monkeypatch.setattr(ConversationStore, '__init__', forbidden)
    body = {'conversation_id': 'shared-name'}
    if fault.startswith('cursor_'):
        body['before_message_cursor'] = {'cursor_bool': True, 'cursor_negative': -1, 'cursor_string': '1'}[fault]
    status, result = post(case, body, credential=fault != 'missing',
                          headers=(('X-User-Id', 'bob'),) if fault == 'duplicate' else ())
    assert status == (403 if fault in {'missing', 'duplicate'} else 400), result


@pytest.mark.parametrize('fault', ['missing_owner', 'missing_thread', 'corrupt_binding', 'corrupt_thread', 'corrupt_messages', 'bad_offset'])
def test_missing_is_empty_and_unreadable_history_remains_explicit_error(case, fault):
    actor, body = 'alice', {'conversation_id': 'shared-name'}
    store, thread = case.stores['alice'], case.threads['alice']
    if fault == 'missing_owner':
        actor = 'never-seen'
    elif fault == 'missing_thread':
        body['conversation_id'] = 'never-seen'
    elif fault == 'corrupt_binding':
        store.storage.bindings_path.write_text('broken')
    elif fault == 'corrupt_thread':
        store.storage.thread_path(thread.thread_id).write_text('broken')
    elif fault == 'corrupt_messages':
        with store.storage.message_path(thread.thread_id).open('ab') as handle:
            handle.write(b'broken\n')
    else:
        body['before_message_cursor'] = 1
    before = snapshot(case.home)
    status, result = post(case, body, actor=actor)
    assert status == 200
    if fault in {'missing_owner', 'missing_thread'}:
        assert result['ok'] and not result['thread_id'] and not result['load_errors']
    else:
        assert result['ok'] is False and len(result['load_errors']) == 1
        assert not result['turns'] and not result['display_events']
    assert snapshot(case.home) == before


@pytest.mark.parametrize('fault', ['copy_control', 'copy_runtime', 'wrong_paths', 'closed'])
def test_cold_history_rejects_changed_original_runtime_before_owner_read(case, monkeypatch, fault):
    server = case.server
    if fault == 'copy_control':
        server.control_plane = copy.copy(server.control_plane)
    elif fault == 'copy_runtime':
        server.drain_runtime = copy.copy(server.drain_runtime)
    elif fault == 'wrong_paths':
        server.paths = copy.copy(server.paths)
    else:
        server.drain_runtime._closing = True
    def forbidden(*_args, **_kwargs):
        raise AssertionError('旧或复制C来源不得解析owner')
    monkeypatch.setattr(request_worker, '_resolve_request_owner_from_config', forbidden)
    if fault == 'closed':
        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _read_coordinator_client_history,
        )

        # 原pool出生门先于handler拒绝关闭后的新连接；这里直接复验已经进入handler的同一只读末门。
        result = _read_coordinator_client_history(server, GatewayControlScope('alice', 'local', 'shared-name'),
                                                  max_turns=2, before=None).to_dict()
        status = 200
    else:
        status, result = post(case)
    assert status == 200 and result['ok'] is False
    assert result['load_errors'] == [{'error_code': 'CLIENT_HISTORY_LOAD_FAILED', 'error_type': 'ValueError'}]


# LLM: 原未实现断言已由111实际200红证实过时；这里验证启用的真实C搜索、认证owner、scope退出和closing，外部模型仍关闭。
# 函数用途: 在同原HTTP服务搜索两份隔离正式记忆，证明不会借历史会话或客户端字段读取另一owner。
def test_implemented_client_memory_search_keeps_current_owner_and_original_source(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )
    from agent_py_agent.agent.memory_store.jsonl import MemoryRecord

    paths = {}
    roots = {}
    for actor in ("alice", "bob"):
        scoped = home_paths_with_owner(home_paths(case.home), resolve_owner_home(
            case.home, OwnerIdentity.provider_user("local", actor)
        ))
        roots[actor] = scoped.owner_home_dir
        paths[actor] = resolve_runtime_paths_for_agent(
            replace(case.config, conversation_workspace=""), scoped.owner_home_dir, scoped
        ).paths["memory_path"]
        paths[actor].parent.mkdir(parents=True, exist_ok=True)
        paths[actor].write_text(MemoryRecord(
            "user", f"sharedfact owner {actor}", entry_id=f"{actor}-only"
        ).to_json() + "\n", encoding="utf-8")
    sources = []
    perform = _LocalControlWriteSource.perform

    def observe(source, operation):
        if not any(source is old for old in sources):
            sources.append(source)
        return perform(source, operation)

    monkeypatch.setattr(_LocalControlWriteSource, "perform", observe)
    foreign_before = snapshot(roots["bob"])
    for actor in ("alice", "bob"):
        status, result = post(case, {"conversation_id": "shared-name", "operation": "search",
            "query": "sharedfact", "user_id": "forged-foreign", "limit": 5},
            actor=actor, path="/client/memory")
        assert status == 200 and result["ok"] is True and result["error_code"] == "", result
        assert [record["entry_id"] for record in result["records"]] == [f"{actor}-only"]
        assert [record["content"] for record in result["records"]] == [f"sharedfact owner {actor}"]
        assert sources[-1].http_origin.owner == OwnerIdentity.provider_user("local", actor)
        assert sources[-1].lifetime.active is False and not sources[-1].lease._commit_pins
        if actor == "alice":
            assert snapshot(roots["bob"]) == foreign_before
    assert case.server.agent is None
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    search = JsonlMemory.search
    at_close = []

    def close_after_search(memory, *args, **options):
        found = search(memory, *args, **options)
        at_close.append(snapshot(case.home))
        case.server.drain_runtime._closing = True
        return found

    monkeypatch.setattr(JsonlMemory, "search", close_after_search)
    status, result = post(case, {"conversation_id": "shared-name", "operation": "search",
        "query": "sharedfact"}, path="/client/memory")
    assert status == 200 and result["ok"] is False and result["records"] == []
    assert result["error_code"] == "LocalControlCoordinatorUnavailable"
    assert len(at_close) == 1 and snapshot(case.home) == at_close[0]


def test_missing_conversation_keeps_original_default_empty_page(case):
    before = snapshot(case.home)
    status, result = post(case, {})
    assert status == 200 and result['ok'] and not result['thread_id']
    assert snapshot(case.home) == before


def test_c_lifecycle_closing_during_read_discards_page_without_owner_writes(case, monkeypatch):
    from agent_py_agent.agent.conversation.store_messages import MessageStore

    original = MessageStore.history_page_report
    def close_after_read(*args, **kwargs):
        page = original(*args, **kwargs)
        case.server.drain_runtime._closing = True
        return page
    monkeypatch.setattr(MessageStore, 'history_page_report', close_after_read)
    before = snapshot(case.home)
    status, result = post(case)
    assert status == 200 and not result['ok']
    assert not result['turns'] and not result['display_events']
    assert result['load_errors'] == [{'error_code': 'CLIENT_HISTORY_LOAD_FAILED', 'error_type': 'ValueError'}]
    assert snapshot(case.home) == before


def test_c_history_keeps_read_oserror_explicit_and_does_not_echo_path(case, monkeypatch):
    from agent_py_agent.agent.conversation.store_threads import ThreadStore

    def unreadable(*_args, **_kwargs):
        raise PermissionError('/synthetic/private/unreadable')
    monkeypatch.setattr(ThreadStore, 'resolve_report', unreadable)
    before = snapshot(case.home)
    status, result = post(case)
    assert status == 200 and result['ok'] is False
    assert result['load_errors'] == [{'error_code': 'CLIENT_HISTORY_LOAD_FAILED', 'error_type': 'PermissionError'}]
    assert 'synthetic' not in json.dumps(result) and snapshot(case.home) == before


def test_c_backward_pages_keep_exact_original_cursors_and_all_display_groups(case):
    before, found = None, []
    baseline = snapshot(case.home)
    while True:
        body = {'conversation_id': 'shared-name', 'limit': 2}
        if before is not None:
            body['before_message_cursor'] = before
        status, result = post(case, body)
        assert status == 200 and result['ok']
        assert result['message_cursor'] == (before if before is not None else case.stores['alice'].storage.message_path(case.threads['alice'].thread_id).stat().st_size)
        found[:0] = [event['payload']['text'] for event in result['display_events'] if event['kind'] == 'user_message']
        if not result['has_older']:
            break
        assert result['before_message_cursor'] > 0
        assert before is None or result['before_message_cursor'] < before
        before = result['before_message_cursor']
    assert len(found) == len(set(found)) == 8
    assert all(f'alice问题{turn}' in found[turn] for turn in range(8))
    status, result = post(case, {'conversation_id': 'shared-name', 'before_message_cursor': 0})
    assert status == 200 and result['ok'] and result['message_cursor'] == result['before_message_cursor'] == 0
    assert not result['display_events'] and snapshot(case.home) == baseline


def test_original_owner_wrapper_reuses_loaded_store_foreign_path_and_model_default(case):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.owner_conversation_store import owner_conversation_store

    base_home = home_paths(case.home)
    base = SimpleNamespace(config=case.config, home_paths=base_home, conversation_store=case.stores['main'],
        runtime_path_resolution=resolve_runtime_paths_for_agent(case.config, base_home.owner_home_dir, base_home))
    assert owner_conversation_store(base, base_home, initialize=False) is case.stores['main']
    foreign = home_paths_with_owner(base_home, resolve_owner_home(case.home, OwnerIdentity.provider_user('local', 'alice')))
    before = snapshot(case.home)
    selected = owner_conversation_store(base, foreign, initialize=False)
    assert selected.storage.root == case.stores['alice'].storage.root
    assert callable(selected.threads.model_default)
    assert selected.threads.model_default() == 'default'
    assert snapshot(case.home) == before
