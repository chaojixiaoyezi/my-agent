"""W11路由使用真实线程存储；受理快照不得因后续开关变化而漂移。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_command,
)
from agent_py_agent.agent.conversation.route_mode import (
    execute_route_control,
    parse_route_mode,
    route_setting,
    set_route_mode,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import capture_im_dispatch
from agent_py_agent.agent.settings.im_task_preferences import set_delegation_preference
from agent_py_agent.tests.test_business_v12_f3_agents import business_host as business_host
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


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
    set_delegation_preference(agent, 'feishu', 'ou_a', True)
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
    set_delegation_preference(agent, 'feishu', 'ou_a', True)
    before = agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes()
    result = execute_route_control(agent, ConversationControlCommand('route', value='status'), thread,
        actor_id='ou_a', channel='feishu')
    assert result.ok and result.details['enabled'] is False
    assert '旧' in result.message and '/delegate' in result.message and '迁移' in result.message
    assert agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes() == before
