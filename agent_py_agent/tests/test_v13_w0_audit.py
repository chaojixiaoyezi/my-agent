"""真实请求文件、会话归属和 V13 写入的共享审计测试。"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.business_spaces import v13_audit as module
from agent_py_agent.agent.business_spaces.v13_audit import audit_records
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.owner_conversation_store import (
    owner_conversation_store_from_config,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import MODEL_OBSERVATION_KEY
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


# LLM: 仅在 tmp_path 建立真实会话、请求文件和事务审计；合成凭据不连接真实宿主。
# 函数用途: 构造只使用任务临时 home 的真实原生审计来源。
@pytest.fixture
def audit_home(tmp_path):
    paths_home = ensure_my_agent_home(tmp_path / 'home')
    base = home_paths_with_owner(paths_home, resolve_owner_home(paths_home.root))
    config = SimpleNamespace(gateway_workspace=str(tmp_path / 'queue'),
                             conversation_workspace=str(tmp_path / 'conversations'))
    conversations = owner_conversation_store_from_config(config, base, initialize=True)
    thread = conversations.threads.create(owner_id=base.owner_id)
    host = SimpleNamespace(home_paths=base, config=config, conversation_store=conversations)
    paths = gateway_paths_from_root(tmp_path / 'queue')
    paths.done.mkdir(parents=True)
    payload = {'id': 'request-one', 'conversation_claim': {'thread_id': thread.thread_id},
               'owner_id': base.owner_id, 'status': 'completed',
               MODEL_OBSERVATION_KEY: {'status': 'selected', 'reason': 'synthetic'},
               'prompt': 'private synthetic content', 'metadata': {'channel': 'chat'}}
    request = paths.done / 'request-one.json'
    request.write_text(json.dumps(payload))
    os.utime(request, (1700000020, 1700000020))
    # LLM: 写入指定临时 owner 的真实事务，以固定主体和时钟生成可追溯审计。
    # 函数用途: 使用正式事务写审计，而非手工拼造 v13 审计文件。
    def seed(home, subject, at, key):
        return V13Store(home, subject_id=subject, action='async.create', clock=lambda: at).transaction(
            'async_jobs', lambda rows: (rows.update({key: {'job_id': key}}) or {'job_id': key}),
            idempotency_key=key, params_digest=key)
    seed(base.owner_home_dir, 'main-actor', 1700000030, 'main-job')
    alice = home_paths_with_owner(base, resolve_owner_home(base.root, OwnerIdentity.local_user('alice')))
    alice.owner_home_dir.mkdir(parents=True)
    seed(alice.owner_home_dir, 'alice-actor', 1700000040, 'alice-job')
    foreign = paths.done / 'request-alice.json'
    foreign.write_text(json.dumps({'id': 'request-alice', 'owner_id': alice.owner_id, 'status': 'failed'}))
    os.utime(foreign, (1700000010, 1700000010))
    middleware = AuthMiddleware(AuthManager(), auth_token='synthetic-gateway-credential')
    handler = SimpleNamespace(_auth_middleware=middleware,
        headers={'X-Gateway-Token': 'synthetic-gateway-credential'}, client_address=('127.0.0.1', 1))
    return SimpleNamespace(base=base, alice=alice, host=host, handler=handler, paths=paths, request=request)


# LLM: 读取夹具中的真实审计源，跨 owner 记录和私有请求内容不得进入公开 DTO。
# 函数用途: 合并的三个来源按新到旧排序、owner 隔离、仅返回冻结 DTO。
def test_audit_merges_native_sources_and_v13_newest_first(audit_home):
    data = audit_home
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert page['coverage'] == 'complete', page
    assert page['unavailable'] == []
    assert [row['source'] for row in page['records']] == ['v13', 'request', 'decision']
    assert [row['at'] for row in page['records']] == sorted((row['at'] for row in page['records']), reverse=True)
    assert all(set(row) == {'record_id', 'at', 'actor', 'action', 'outcome', 'source'} for row in page['records'])
    assert 'alice-actor' not in json.dumps(page)
    assert 'private synthetic content' not in json.dumps(page)
    selected = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host,
                             actor='main-actor', since='2023-11-14T22:13:45Z', until='2023-11-14T22:14:00Z')
    assert len(selected['records']) == 1 and selected['records'][0]['source'] == 'v13'


# LLM: host 查询只能使用仍有效的原生管理员身份，改坏合成凭据后须立即拒绝跨 owner 读取。
# 函数用途: 管理员重验来自原生中间件，不接受 actor、对象属性或撤销后的旧身份。
def test_host_scope_requires_native_admin_and_spans_owners(audit_home):
    data = audit_home
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host', actor='admin', host=data.host)
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                      admin_context=SimpleNamespace(is_admin=True), host=data.host)
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                         admin_context=data.handler, host=data.host)
    assert page['coverage'] == 'complete', page
    assert page['records'][0]['actor'] == 'alice-actor'
    assert len(page['records']) == 5
    data.handler.headers = {'X-Gateway-Token': 'wrong'}
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                      admin_context=data.handler, host=data.host)


# LLM: 原游标仅对同一 owner、筛选和来源修订有效；临时源新增事务后必须拒绝旧游标。
# 函数用途: 同一页可由下一次函数调用延续，筛选和源修订变更或篡改都使游标过期。
def test_audit_cursor_hmac_binding_filters_owner_and_revision(audit_home):
    data = audit_home
    first = audit_records(data.base.owner_home_dir, data.base.owner_id, limit=1, host=data.host)
    assert first['coverage'] == 'partial' and first['next_cursor']
    second = audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=first['next_cursor'],
                           limit=1, host=data.host)
    assert second['records'][0]['source'] == 'request'
    third = audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=second['next_cursor'],
                          limit=1, host=data.host)
    assert third['records'][0]['source'] == 'decision'
    assert third['coverage'] == 'complete' and third['next_cursor'] is None
    for kwargs in ({'actor': 'main-actor'}, {'since': '2023-11-14T22:13:45Z'}, {'cursor': 'tampered'}):
        args = {'cursor': first['next_cursor'], 'host': data.host, **kwargs}
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            audit_records(data.base.owner_home_dir, data.base.owner_id, **args)
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        audit_records(data.alice.owner_home_dir, data.alice.owner_id, cursor=first['next_cursor'], host=data.host)
    V13Store(data.base.owner_home_dir).transaction('async_jobs', lambda rows: {},
        idempotency_key='new', params_digest='new')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=first['next_cursor'], host=data.host)


# LLM: 仅破坏夹具内请求文件并注入单源异常；其它来源仍应返回，缺口不能被隐藏。
# 函数用途: 真实坏请求文件与来源异常均必须披露 partial，而不是空 complete。
def test_failed_and_unreadable_sources_are_explicit(audit_home, monkeypatch):
    data = audit_home
    bad = data.paths.done / 'bad.json'
    bad.write_text('{bad')
    os.utime(bad, (1700000020, 1700000020))
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert page['coverage'] == 'partial'
    assert {item['source'] for item in page['unavailable']} == {'decision', 'request'}
    # LLM: 只向被替换的来源读口抛出合成异常，不改其它审计来源或持久数据。
    # 函数用途: 模拟一个原生来源无法打开。
    def failed(paths, query):
        raise OSError('synthetic source failure')
    monkeypatch.setattr(module, 'request_outcome_records', failed)
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert {'source': 'request', 'reason_code': 'HOST_NOT_READY'} in page['unavailable']
    assert any(row['source'] == 'v13' for row in page['records'])


# LLM: 对临时审计源提交无效筛选或页长，必须以结构化错误拒绝而不能退回默认全量。
# 函数用途: 查询形状错误不能被解释为宽查询。
@pytest.mark.parametrize('kwargs', [{'since': 'bad'}, {'until': '2024-01-01'}, {'actor': ''}, {'limit': True},
                                  {'limit': 101}, {'since': '2025-01-01T00:00:00Z', 'until': '2024-01-01T00:00:00Z'}])
def test_invalid_query_rejected(audit_home, kwargs):
    data = audit_home
    with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host, **kwargs)
