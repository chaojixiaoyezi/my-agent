"""W11路由使用真实线程存储；受理快照不得因后续开关变化而漂移。"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_command,
)
from agent_py_agent.agent.conversation.route_mode import (
    execute_route_control,
    parse_route_mode,
    route_setting,
    route_setting_for_paths,
    route_thread_for_paths,
    set_route_mode,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import (
    capture_im_dispatch,
    capture_im_dispatch_for_owner,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.scheduler.schedule import _canonical_zone_name
from agent_py_agent.agent.settings.im_task_preferences import preference_key, preference_path
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_business_v12_f3_agents import business_host as business_host
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


def _write_legacy_preference(agent, channel: str, actor_id: str) -> None:
    timezone = _canonical_zone_name(str(agent.config.timezone or ""))
    local = datetime.fromtimestamp(time.time(), ZoneInfo(timezone))
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(timezone))
    payload = {
        "schema_version": "im_user_preferences.v1",
        "owner_id": str(agent.home_paths.owner_id),
        "users": {
            preference_key(channel, actor_id): {
                "enabled": True,
                "expires_at": midnight.timestamp(),
                "business_day": local.date().isoformat(),
                "timezone": timezone,
                "revision": 1,
            }
        },
    }
    path = preference_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# LLM: pytest对封闭合法输入逐项调用纯解析函数，核对状态查询与布尔结果；无存储、线程或模型副作用。
# 函数用途: 检查route参数只解析为status、True或False。
@pytest.mark.parametrize(('value', 'expected'), [('', 'status'), ('status', 'status'), ('true', True), ('false', False)])
def test_typed_enum_parser(value, expected):
    assert parse_route_mode(value) == expected


# LLM: pytest仅向route解析器传入非法拼写、组合和类型，要求抛ValueError而非宽松转布尔；不执行控制或写配置。
# 函数用途: 检查不受支持的route参数会被明确拒绝。
@pytest.mark.parametrize('value', ['on', 'enable', 'TRUE', 'true false', '1', None, True])
def test_invalid_enum_is_usage_error(value):
    with pytest.raises(ValueError):
        parse_route_mode(value)


# LLM: pytest只检查公共命令解析边界，要求各合法route文本成为有效的结构化控制命令；不派发任务或调用模型，也不据此声称完整执行链已测。
# 函数用途: 确认route输入被识别为控制命令，而不是普通聊天内容。
@pytest.mark.parametrize('text', ['/route', '/route status', '/route true', '/route false'])
def test_public_command_never_enters_model(text):
    command = parse_conversation_command(text)
    assert isinstance(command, ConversationControlCommand)
    assert command.kind == 'route' and command.valid


# LLM: pytest在临时线程存储写入route并重建Store读取，验证默认关闭、按Actor/线程隔离和外Owner写入拒绝；不访问真实IM或运行模型。
# 函数用途: 检查路由开关可持久恢复，但不会泄漏给别的用户或会话。
def test_default_persistence_owner_and_actor_scope(tmp_path):
    agent, thread = fixture_agent(tmp_path)
    assert route_setting(agent, thread, actor_id='ou_a', channel='feishu') == {
        'enabled': False, 'source': 'default', 'revision': 0,
    }
    saved = set_route_mode(agent, thread, True, actor_id='ou_a', channel='feishu')
    assert saved == {'enabled': True, 'source': 'thread', 'revision': 1}
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    assert route_setting(agent, thread, actor_id='ou_a', channel='feishu') == saved
    assert route_setting(agent, thread, actor_id='ou_b', channel='feishu')['enabled'] is False
    other = agent.conversation_store.threads.create(owner_id=agent.home_paths.owner_id, canonical_user_id='ou_a')
    assert route_setting(agent, other, actor_id='ou_a', channel='feishu')['enabled'] is False
    foreign = replace(thread, owner_id='foreign')
    with pytest.raises(PermissionError):
        set_route_mode(agent, foreign, True, actor_id='ou_a', channel='feishu')


# LLM: pytest对临时会话提交显式非法控制并比对线程原字节，再检查无会话控制返回失败；不把用法错误转成配置写入或模型请求。
# 函数用途: 确认坏route命令和缺少会话的操作都被拒绝。
def test_invalid_command_and_threadless_control_never_write(tmp_path):
    agent, thread = fixture_agent(tmp_path)
    before = agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes()
    invalid = ConversationControlCommand('route', value='enable', valid=False, usage='用法：/route true|false|status')
    result = execute_route_control(agent, invalid, thread)
    assert result is not None and not result.ok and result.message == invalid.usage
    assert agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes() == before
    result = execute_route_control(agent, ConversationControlCommand('route', value='true'), None)
    assert result is not None and not result.ok and '会话' in result.message


# LLM: pytest包装临时线程的原子更新以注入一次修订碰撞；断言route只重试一次且保留并发写入的无关元数据，副作用限于测试Store。
# 函数用途: 检查路由保存遇到一次CAS冲突后能安全重试。
def test_compare_and_swap_retries_once_preserving_other_metadata(tmp_path, monkeypatch):
    agent, thread = fixture_agent(tmp_path)
    threads = agent.conversation_store.threads
    original = threads.update_atomic
    calls = []

    # LLM: 由本测试替换的update_atomic调用；仅第一次先用原更新器写无关字段并提高修订，再执行传入updater，以真实临时存储制造可恢复CAS冲突。
    # 函数用途: 模拟另一个写入者抢先修改一次线程。
    def collide(identifier, updater):
        calls.append(identifier)
        if len(calls) == 1:
            original(identifier, lambda latest: replace(latest, metadata={**latest.metadata, 'unrelated': 'preserved'}, revision=latest.revision + 1))
        return original(identifier, updater)

    monkeypatch.setattr(threads, 'update_atomic', collide)
    result = set_route_mode(agent, thread, True)
    assert result == {'enabled': True, 'source': 'thread', 'revision': 1}
    assert len(calls) == 2
    assert threads.require(thread.thread_id).metadata['unrelated'] == 'preserved'


# LLM: pytest在临时Store让每次原子更新都遭遇新修订，要求两次尝试后报告REVISION_CONFLICT且route仍关闭；不无限重试或吞掉冲突。
# 函数用途: 检查第二次CAS冲突会明确失败，不伪报路由已保存。
def test_second_compare_and_swap_conflict_is_reported(tmp_path, monkeypatch):
    agent, thread = fixture_agent(tmp_path)
    threads = agent.conversation_store.threads
    original = threads.update_atomic
    calls = []

    # LLM: 供本测试替换update_atomic；每次先用原更新器提高临时线程修订再交给updater，持续制造冲突并记录调用次数，不改route值。
    # 函数用途: 模拟每次保存前都有新的并发写入。
    def collide(identifier, updater):
        calls.append(identifier)
        original(identifier, lambda latest: replace(latest, revision=latest.revision + 1))
        return original(identifier, updater)

    monkeypatch.setattr(threads, 'update_atomic', collide)
    with pytest.raises(RuntimeError, match='REVISION_CONFLICT'):
        set_route_mode(agent, thread, True)
    assert len(calls) == 2
    assert route_setting(agent, thread)['enabled'] is False


# LLM: pytest以临时宿主和结构化Gateway scope采样路由快照，并切换持久开关；断言原快照不漂移、自执行仅认结构化覆盖且不改设置，不连接真实IM。
# 函数用途: 检查受理时冻结路由与来源，聊天文字不能偷偷改派发方式。
def test_im_capture_freezes_route_and_origin_and_only_structured_self_override(tmp_path, monkeypatch):
    agent, thread = fixture_agent(tmp_path)
    agent.config.enable_subagents = True
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope', lambda base, scope: agent)
    set_route_mode(agent, thread, True, actor_id='ou_a', channel='feishu')
    scope = GatewayControlScope('ou_a', 'feishu', 'oc_source', metadata={'message_id': 'message-original'})
    snapshot = capture_im_dispatch(agent, scope)
    assert snapshot['mode'] == 'delegated'
    assert snapshot['route_mode_frozen'] is True
    assert snapshot['route_origin'] == {'channel': 'feishu', 'chat_id': 'oc_source', 'thread_id': thread.thread_id,
                                        'message_id': 'message-original', 'owner_id': agent.home_paths.owner_id}
    set_route_mode(agent, thread, False, actor_id='ou_a', channel='feishu')
    assert capture_im_dispatch(agent, scope)['mode'] == 'original'
    assert snapshot['route_mode_frozen'] is True
    assert snapshot['route_origin']['thread_id'] == thread.thread_id
    set_route_mode(agent, thread, True, actor_id='ou_a', channel='feishu')
    override = capture_im_dispatch(agent, replace(scope, metadata={**scope.metadata, 'route_self_execution': True}))
    assert override['mode'] == 'original' and override['route_mode_frozen'] is True
    assert route_setting(agent, thread, actor_id='ou_a', channel='feishu')['enabled'] is True
    text = capture_im_dispatch(agent, replace(scope, metadata={**scope.metadata, 'text': '你自己做 不要委派'}))
    assert text['mode'] == 'delegated'


# LLM: pytest先在临时偏好存储启用旧delegate，再以替换的宿主解析入口采样IM派发；要求仍走original且route保持默认关闭，不自动迁移成持久路由。
# 函数用途: 确认旧委派开关不会自动开启新的route设置。
def test_legacy_enable_is_not_upgraded_to_persistent_route(tmp_path, monkeypatch):
    agent, thread = fixture_agent(tmp_path)
    agent.config.enable_subagents = True
    _write_legacy_preference(agent, 'feishu', 'ou_a')
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope', lambda base, scope: agent)
    snapshot = capture_im_dispatch(agent, GatewayControlScope('ou_a', 'feishu', 'oc_source'))
    assert snapshot['mode'] == 'original'
    assert route_setting(agent, thread, actor_id='ou_a', channel='feishu')['enabled'] is False


# LLM: 状态投影必须读route而非旧每日偏好；容量服务缺失不可伪造零。
# 函数用途: 验证route控制及IM状态均观察当前真实路由。
def test_route_status_uses_canonical_setting_and_unknown_capacity(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.im_task_status import im_task_status_facts

    agent, thread = fixture_agent(tmp_path)
    agent.config.max_subagents = 0
    agent.owner_policy = SimpleNamespace(max_subagents=0)
    set_route_mode(agent, thread, True, actor_id='ou_a')
    facts = im_task_status_facts(agent, thread, GatewayControlScope('ou_a', 'feishu', 'oc_source'))
    assert facts['route_mode'] is True
    assert facts['route_source'] == 'thread'
    agent.conversation_store.storage.thread_path('thread-unreadable').write_text('{bad', encoding='utf-8')
    result = execute_route_control(agent, ConversationControlCommand('route', value='status'), thread, actor_id='ou_a')
    assert result.ok and result.details['enabled'] is True
    assert '后台' in result.message and '未知' in result.message


# LLM: pytest在临时业务宿主创建身份后读取route状态，对有限和不限额配置核对同Owner真实配额及文案；不从名册页长补数，不启动模型执行。
# 函数用途: 检查route状态显示真实已用额度，并正确说明零配置代表不限。
@pytest.mark.parametrize('limit', [3, 0])
def test_route_status_reports_real_owner_quota_and_unlimited(business_host, limit):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService

    agent, actor = business_host
    agent.config.max_background_agents = limit
    BackgroundAgentService(agent).create({'space_id': 'space-A', 'name': 'worker'},
        actor=actor, idempotency_key='route-count')
    thread = agent.conversation_store.threads.require(actor.source_thread_id)
    result = execute_route_control(agent, ConversationControlCommand('route', value='status'), thread,
        actor_id=actor.actor_id, channel=actor.channel)
    assert result.ok
    facts = result.details['background_agents']
    assert facts['known'] is True
    assert (facts['used'], facts['limit'], facts['unassigned']) == (1, limit, 0)
    assert facts['available'] == (limit - 1 if limit else None)
    assert ('不限' in result.message) is (limit == 0)


# LLM: pytest仅为准备数据写入临时旧偏好，随后只读查询route状态并比对线程字节；要求提示迁移但不把旧值当路由事实或自动写新设置。
# 函数用途: 检查旧偏好只用于迁移提醒，不能改变当前路由状态。
def test_old_preference_is_a_readonly_migration_hint_not_route_state(tmp_path):
    agent, thread = fixture_agent(tmp_path)
    _write_legacy_preference(agent, 'feishu', 'ou_a')
    before = agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes()
    result = execute_route_control(agent, ConversationControlCommand('route', value='status'), thread,
        actor_id='ou_a', channel='feishu')
    assert result.ok and result.details['enabled'] is False
    assert '旧' in result.message and '/delegate' in result.message and '迁移' in result.message
    assert agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes() == before


def _empty_owner_route_case(tmp_path, owner_kind, *, enabled=False):
    identity = (OwnerIdentity.provider_user('feishu', 'ou_a') if owner_kind == 'user'
                else OwnerIdentity.provider_group('feishu', 'oc_source'))
    agent, thread = fixture_agent(tmp_path, identity=identity)
    agent.config.enable_subagents = True
    agent.config.conversation_workspace = str(agent.conversation_store.storage.root)
    scope = GatewayControlScope('ou_a', 'feishu', 'oc_source', resolved_owner=identity)
    if enabled:
        set_route_mode(agent, thread, True, actor_id=scope.user_id)
    thread = agent.conversation_store.threads.update_atomic(
        thread.thread_id, lambda current: replace(current, owner_id=''))
    return agent, thread, scope


def _route_capture(agent, scope, entry):
    if entry == 'agent':
        return capture_im_dispatch(agent, scope)
    return capture_im_dispatch_for_owner(
        home_paths=agent.home_paths, config=agent.config, scope=scope,
        capability_root=agent.root)


@pytest.mark.parametrize('owner_kind', ['user', 'group'])
def test_legacy_empty_owner_route_status_and_lookup_remain_readonly(tmp_path, owner_kind):
    from agent_py_agent.tests.test_im_dispatch_readonly import _tree

    agent, thread, scope = _empty_owner_route_case(tmp_path, owner_kind)
    before = _tree(tmp_path)
    status = execute_route_control(agent, ConversationControlCommand('route', value='status'),
        thread, actor_id=scope.user_id, channel=scope.channel)
    assert status.ok, status.message
    expected = {'enabled': False, 'source': 'default', 'revision': 0}
    assert route_setting(agent, thread, actor_id=scope.user_id) == expected
    assert route_setting_for_paths(agent.home_paths, thread, actor_id=scope.user_id) == expected
    resolved = route_thread_for_paths(agent.home_paths, agent.config, scope)
    assert resolved.thread_id == thread.thread_id and resolved.owner_id == ''
    assert _tree(tmp_path) == before
    saved = set_route_mode(agent, thread, True, actor_id=scope.user_id)
    assert saved == {'enabled': True, 'source': 'thread', 'revision': 1}
    assert route_setting(agent, thread, actor_id=scope.user_id) == saved
    assert route_setting(agent, thread, actor_id='ou_b')['enabled'] is False
    assert agent.conversation_store.threads.require(thread.thread_id).owner_id == ''


@pytest.mark.parametrize('owner_kind', ['user', 'group'])
@pytest.mark.parametrize('entry', ['agent', 'cold'])
@pytest.mark.parametrize('enabled', [False, True])
def test_legacy_empty_owner_im_capture_remains_readonly(tmp_path, monkeypatch, owner_kind, entry, enabled):
    from agent_py_agent.tests.test_im_dispatch_readonly import _tree

    agent, thread, scope = _empty_owner_route_case(tmp_path, owner_kind, enabled=enabled)
    monkeypatch.setattr(control_service, '_request_agent_for_scope', lambda base, scope: agent)
    before = _tree(tmp_path)
    captured = _route_capture(agent, scope, entry)
    assert captured['mode'] == ('delegated' if enabled else 'original')
    assert captured['route_mode_frozen'] is enabled
    assert captured['preference_revision'] == (1 if enabled else 0)
    assert captured['owner_id'] == agent.home_paths.owner_id
    assert captured['route_origin'] == {
        'channel': scope.channel, 'chat_id': scope.conversation_id, 'thread_id': thread.thread_id,
        'message_id': '', 'owner_id': agent.home_paths.owner_id,
    }
    assert _tree(tmp_path) == before


@pytest.mark.parametrize('entry', ['agent', 'cold'])
def test_workspace_control_stamps_owner_for_ordinary_capture(tmp_path, monkeypatch, entry):
    agent, _thread, scope = _empty_owner_route_case(tmp_path, 'user')
    scope = replace(scope, conversation_id='oc_first_dir')
    monkeypatch.setattr(control_service, '_request_agent_for_scope', lambda base, scope: agent)
    result = control_service._execute_gateway_workspace_control(
        agent, ConversationControlCommand('dir'), scope, is_admin=False)
    assert result.ok, result.message
    created = agent.conversation_store.threads.require(result.details['thread_id'])
    assert created.owner_id == agent.home_paths.owner_id
    assert created.owner_home == str(agent.home_paths.owner_home_dir)
    captured = _route_capture(agent, scope, entry)
    assert captured['mode'] == 'original' and captured['route_mode_frozen'] is False
    assert captured['route_origin']['thread_id'] == created.thread_id
    assert captured['owner_id'] == agent.home_paths.owner_id


@pytest.mark.parametrize('entry', ['lookup', 'paths', 'binding', 'agent', 'cold', 'write'])
def test_foreign_thread_owner_is_rejected_at_every_route_entry(tmp_path, monkeypatch, entry):
    from agent_py_agent.tests.test_im_dispatch_readonly import _tree

    agent, thread, scope = _empty_owner_route_case(tmp_path, 'user', enabled=True)
    thread = agent.conversation_store.threads.update_atomic(
        thread.thread_id, lambda current: replace(current, owner_id='foreign'))
    monkeypatch.setattr(control_service, '_request_agent_for_scope', lambda base, scope: agent)
    before = _tree(tmp_path)
    with pytest.raises(PermissionError, match='route owner scope mismatch'):
        if entry == 'lookup':
            route_setting(agent, thread, actor_id=scope.user_id)
        elif entry == 'paths':
            route_setting_for_paths(agent.home_paths, thread, actor_id=scope.user_id)
        elif entry == 'binding':
            route_thread_for_paths(agent.home_paths, agent.config, scope)
        elif entry == 'write':
            set_route_mode(agent, thread, False, actor_id=scope.user_id)
        else:
            _route_capture(agent, scope, entry)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize('entry', ['agent', 'cold'])
def test_legacy_empty_owner_does_not_accept_foreign_route_metadata(tmp_path, monkeypatch, entry):
    from agent_py_agent.tests.test_im_dispatch_readonly import _tree

    agent, thread, scope = _empty_owner_route_case(tmp_path, 'user', enabled=True)
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(
        current, metadata={**current.metadata, 'route_mode.v1': {
            **current.metadata['route_mode.v1'], 'owner_id': 'foreign'}}))
    monkeypatch.setattr(control_service, '_request_agent_for_scope', lambda base, scope: agent)
    before = _tree(tmp_path)
    with pytest.raises(DataCorruptionError, match='route setting is unreadable'):
        _route_capture(agent, scope, entry)
    assert _tree(tmp_path) == before
