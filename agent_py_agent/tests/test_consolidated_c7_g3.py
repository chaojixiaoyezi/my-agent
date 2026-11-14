"""Consolidated test shard module: test_consolidated_c7_g3.py.

Sources:
- agent_py_agent/tests/test_v15_w10_inbound_notes.py
- agent_py_agent/tests/test_v15_w10_progress_card_detail.py
- agent_py_agent/tests/test_v15_w10_topic_seed.py
- agent_py_agent/tests/test_v15_w11_delegate_retirement.py
- agent_py_agent/tests/test_v15_w11_route_command.py
- agent_py_agent/tests/test_v15_w1_revision_history.py
- agent_py_agent/tests/test_v15_w2_skill_bindings.py

[test_v15_w10_inbound_notes.py] 入站便签真实落盘、下载接线、并发与路径边界；仅使用临时 Owner。
[test_v15_w10_progress_card_detail.py] 同一进度卡展示真实步骤摘要、批次与已绑定的后台执行事件。
[test_v15_w10_topic_seed.py] 告警根与主群 M0 的首消息种子、重试恢复和准确线程隔离。
[test_v15_w11_delegate_retirement.py] delegate退役入口不再读写偏好或启动任何执行。
[test_v15_w11_route_command.py] W11路由使用真实线程存储；受理快照不得因后续开关变化而漂移。
[test_v15_w1_revision_history.py] 验证内容寻址历史、发布版本与兼容链，不执行历史脚本。
[test_v15_w2_skill_bindings.py] P1.1-b：目录与快照共享绑定并集，元数据消费不提前读取Skill正文。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent_py_agent.agent.adapter import inbound_notes, topic_seed
from agent_py_agent.agent.adapter.feishu_progress_card import render_progress_card
from agent_py_agent.agent.adapter.manager import _ChannelIngressCoordinator
from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.assets import skill_adapter as adapter
from agent_py_agent.agent.capability.skill_snapshot import (
    SkillSnapshot,
    SkillSnapshotEntry,
    SkillSnapshotError,
)
from agent_py_agent.agent.command_catalog import CommandAccessContext, render_command_help
from agent_py_agent.agent.common import nofollow_fs
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
from agent_py_agent.agent.delivery.execution_view import project_execution_view
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.delegation_control import execute_delegation_control
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
from agent_py_agent.tests.test_inbound_media_ownership import _manager, _message

# =============================================================================
# Source: agent_py_agent/tests/test_v15_w10_inbound_notes.py
# =============================================================================

# LLM: 样本仅包含合成身份与 Owner 相对路径，不触及真实配置。
# 函数用途: 按正式 API 保存一条可复现便签。
def _append(home, index=0):
    return inbound_notes.append_inbound_note(home, channel='feishu', chat_id='oc_fixture',
        message_id=f'om_{index}', media_kind='file', file_name='../../doc\n.txt',
        relative_path=f'media/input/{index}-doc.txt', size_bytes=3,
        sha256=hashlib.sha256(str(index).encode()).hexdigest(), text_excerpt='说明\n下一行',
        received_at=f'2026-11-04T00:00:{index:02d}+00:00')


# LLM: 直接核对合同字段、顺序和磁盘内容，不以写入回执代替持久化证据。
# 函数用途: 验证有界追加、默认读取及显示名清洗。
def test_append_caps_at_30_and_reads_newest_first(tmp_path):
    assert isinstance(_append(tmp_path), dict)
    for index in range(1, 34):
        _append(tmp_path, index)
    data = json.loads((tmp_path / 'media/recent_inbound_notes.json').read_text())
    assert data['schema_version'] == 'inbound_notes.v1'
    assert len(data['entries']) == 30
    assert data['entries'][0]['message_id'] == 'om_4'
    assert data['entries'][-1]['message_id'] == 'om_33'
    rows = inbound_notes.read_inbound_notes(tmp_path)
    assert [row['message_id'] for row in rows] == ['om_33', 'om_32', 'om_31', 'om_30', 'om_29']
    assert all('/' not in row['file_name'] and '\n' not in row['file_name'] for row in rows)
    assert all(len(row['text_excerpt']) <= 200 and '\n' not in row['text_excerpt'] for row in rows)
    assert inbound_notes.read_inbound_notes(tmp_path, limit=0) == []


# LLM: 路径必须为单一 Owner 内的规范相对引用，不能靠字符串前缀判断。
# 函数用途: 拒绝绝对路径、盘符、控制字符与点穿越。
@pytest.mark.parametrize('path', ['/tmp/file', '../file', 'media/../file', 'C:\\file', 'media\\file', 'media/\x00file'])
def test_relative_path_scope_validation(tmp_path, path):
    with pytest.raises(ValueError):
        inbound_notes.append_inbound_note(tmp_path, channel='feishu', chat_id='chat', message_id='m',
            media_kind='file', file_name='doc', relative_path=path, size_bytes=0, sha256='a' * 64)
    assert not (tmp_path / 'media/recent_inbound_notes.json').exists()


# LLM: 并发只使用临时账本，真实路径锁须保护整个读改写。
# 函数用途: 验证并发不丢记录及同一下载重放不重复记账。
def test_concurrent_append_and_retry_are_idempotent(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: _append(tmp_path, index), range(20)))
    assert len(inbound_notes.read_inbound_notes(tmp_path, limit=30)) == 20
    _append(tmp_path, 0)
    assert len(inbound_notes.read_inbound_notes(tmp_path, limit=30)) == 20


# LLM: 通过生产下载入口和真实身份解析验证便签，而非直接模拟成功。
# 函数用途: 下载成功后记录真实大小、哈希与当前 Owner 相对路径。
def test_download_records_actual_file_for_its_owner(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    msg = _message('ou_one')
    assert manager._maybe_download_media(msg, retry_on_error=True)
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    owner = resolve_owner_home(manager._inbound_media_agent.home_paths.root, OwnerIdentity.provider_user('feishu', 'ou_one'))
    rows = inbound_notes.read_inbound_notes(owner.home_dir)
    assert len(rows) == 1
    assert rows[0]['sha256'] == hashlib.sha256(b'own attachment').hexdigest()
    assert rows[0]['size_bytes'] == len(b'own attachment')
    assert rows[0]['relative_path'] == 'media/input/om_same-example.txt'
    assert rows[0]['text_excerpt'] == 'own attachment'


# LLM: 只对便签 writer 注入一次模拟 I/O 故障，下载及真实字节仍走生产入口。
# 函数用途: 验证可选便签失败不会使下载失败或发布残缺路径。
def test_note_write_failure_does_not_fail_download(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    def fail_note(owner_home, *, channel, chat_id, message_id, media_kind, file_name,
                  relative_path, size_bytes, sha256, text_excerpt='', received_at=None):
        raise OSError('simulated I/O error')
    monkeypatch.setattr(inbound_notes, 'append_inbound_note', fail_note)
    msg = _message('ou_one')
    assert manager._maybe_download_media(msg, retry_on_error=True)
    assert '[已下载到:' in msg.content


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w10_progress_card_detail.py
# =============================================================================

# LLM: 只使用宿主结构化展示字段；不让原始 arguments/output 进入卡片。
# 函数用途: 构造一条带批次和有界结果的工具进度。
def _event():
    return {'kind': 'tool_progress', 'request_id': 'request', 'progress': {
        'tool': 'scripts', 'call_id': 'call', 'phase': 'finished', 'ok': True,
        'handler_executed': True, 'duration_ms': 42,
        'display': {'connector_id': 'fixture_connector', 'endpoint_path': '/health', 'subcommand': 'status'},
        'result_excerpt': '结果摘要' * 40, 'batch_index': 2, 'batch_total': 8,
        'arguments': {'private': 'not-for-display'}, 'output': 'raw-body-not-for-display'}}


# LLM: 先核执行投影再核真实 Card 2.0，原始输出禁止通过结果摘要旁路出现。
# 函数用途: 验证名称、状态、耗时、结果节选与批次同一行展示。
def test_step_detail_result_excerpt_and_batch_survive_projection():
    view = project_execution_view([_event()], request_id='request')
    row = view['steps'][0]
    assert row['label'] == 'fixture_connector /health status'
    assert row['result_excerpt'] == ('结果摘要' * 40)[:60]
    assert row['batch_index'] == 2 and row['batch_total'] == 8
    card = render_progress_card(view)
    panel = next(el for el in card['body']['elements'] if el['tag'] == 'collapsible_panel')
    assert len(panel['elements']) == 1
    step = panel['elements'][0]
    assert step['tag'] == 'collapsible_panel' and step['expanded'] is False
    text = step['header']['title']['content'] + step['elements'][0]['text']['content']
    assert all(value in text for value in ['执行脚本', '已完成', '42ms', '/health', '批次：2/8', row['result_excerpt']])
    assert 'raw-body-not-for-display' not in json.dumps(card)
    assert 'not-for-display' not in json.dumps(card)


# LLM: 只允许显式绑定当前父请求的后台事件合流，不以名字相似合并跨请求状态。
# 函数用途: 验证后台同一身份更新只占一行且外来事件不进入卡片。
def test_background_progress_merges_into_the_same_card_and_keeps_scope():
    events = [_event(),
        {'kind': 'subagent_progress', 'parent_request_id': 'request', 'run_id': 'child', 'phase': 'started', 'label': '处理子任务'},
        {'kind': 'subagent_progress', 'parent_request_id': 'request', 'run_id': 'child', 'phase': 'completed',
         'duration_ms': 12, 'result_excerpt': '已核对'},
        {'kind': 'session_task_progress', 'parent_request_id': 'another', 'task_id': 'foreign', 'phase': 'started'}]
    view = project_execution_view(events, request_id='request')
    assert len(view['steps']) == 2
    assert view['steps'][1]['phase'] == 'completed'
    panel = next(el for el in render_progress_card(view)['body']['elements'] if el['tag'] == 'collapsible_panel')
    assert len(panel['elements']) == 2
    assert 'foreign' not in json.dumps(panel)


# LLM: 真正的 manager 轮询必须使用同一执行投影，否则模块单测通过也不能证明卡片接线。
# 函数用途: 验证嵌套工具进度与后台事件经过轮询后仍进入一个步骤批次。
def test_manager_poll_uses_enriched_view(monkeypatch):
    from agent_py_agent.agent.adapter import manager
    from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
    body = {'events': [_event(), {'kind': 'subagent_progress', 'parent_request_id': 'request',
        'run_id': 'child', 'phase': 'started'}], 'next': 2}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_value, traceback):
            return False
        def read(self):
            return json.dumps(body).encode()
    monkeypatch.setattr(manager, 'open_authenticated_request', lambda request, timeout: Response())
    client = manager._GatewayReplyPollClient(12345)
    client.credentials = SimpleNamespace(headers=lambda: {})
    batch = client.poll_progress(PendingGatewayReply('request', 'feishu', 'actor', 'message'))
    assert len(batch.steps) == 2
    assert batch.steps[0]['batch_total'] == 8
    assert '/health' in batch.steps[0]['label']


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w10_topic_seed.py
# =============================================================================

# LLM: 输入只含临时 Owner 的合成告警和附件引用。
# 函数用途: 保存一个有上下文、来源和 run 引用的话题根。
def _save(home, *, text='原始告警正文'):
    return topic_seed.remember_topic_seed(home, chat_id='oc_fixture', root_message_id='om_root',
        title='示例告警', source='fixture-source', event_id='evt-1', original_text=text,
        run_refs=('run-one',), attachments=({'relative_path': 'media/input/a.txt', 'file_name': 'a.txt'},))


# LLM: 只把种子交给第一个精确消息；同一消息重放必须返回相同资料，不能丢掉已消费上下文。
# 函数用途: 验证一次注入、首消息恢复及不同 root 不串读。
def test_seed_is_once_per_thread_but_first_message_retry_is_lossless(tmp_path):
    assert isinstance(_save(tmp_path), dict)
    args = {'chat_id': 'oc_fixture', 'root_message_id': 'om_root', 'thread_id': 'omt_fixture', 'message_id': 'om_first'}
    seed = topic_seed.take_topic_seed(tmp_path, **args)
    assert isinstance(seed, dict)
    assert seed['title'] == '示例告警' and seed['source'] == 'fixture-source' and seed['event_id'] == 'evt-1'
    assert seed['original_text'] == '原始告警正文' and seed['run_refs'] == ['run-one']
    assert seed['attachments'][0]['relative_path'] == 'media/input/a.txt'
    assert topic_seed.take_topic_seed(tmp_path, **args) == seed
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'message_id': 'om_second'}) is None
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'chat_id': 'oc_other'}) is None
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'thread_id': 'omt_other'}) is None


# LLM: UTF-8 字节而非字符上限防止中文原文突破 4KiB，保存不暴露凭据赋值。
# 函数用途: 验证根正文有界并保留规范相对附件定位。
def test_seed_root_text_has_a_4kib_byte_limit(tmp_path):
    result = _save(tmp_path, text='中文说明' * 2000)
    assert isinstance(result, dict)
    assert 0 < len(result['original_text'].encode()) <= 4096


# LLM: 两条消息确定竞争同一临时种子时，必须仅有一条获得首消息身份。
# 函数用途: 验证并发首消息只注入一次。
def test_two_first_messages_do_not_both_claim_the_seed(tmp_path):
    assert isinstance(_save(tmp_path), dict)
    def take(message_id):
        return topic_seed.take_topic_seed(tmp_path, chat_id='oc_fixture', root_message_id='om_root',
            thread_id='omt_fixture', message_id=message_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, ('first-a', 'first-b')))
    assert sum(row is not None for row in results) == 1


# LLM: 使用生产 ingress 还原所需的准确字段，不改变身份、请求或媒体下载器。
# 函数用途: 将合成 IncomingMessage 作为可重放的持久入站资料。
def _record(msg):
    return SimpleNamespace(kind='message', content=msg.content, channel=msg.channel, user_id=msg.user_id,
        provider_message_id=msg.message_id, conversation_id=msg.conversation_id,
        timestamp=msg.timestamp, metadata=dict(msg.metadata))


# LLM: 真正 prepare_payload 在完整下载后写入 M0；第一次 prepare 后故意不提交 payload，再重放同条持久行。
# 函数用途: 验证主群正文、下载附件跨新话题且准备重试不丢种子。
def test_ingress_prepare_bridges_m0_and_attachment_and_retries_losslessly(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    coordinator = _ChannelIngressCoordinator(manager)
    root = _message('ou_one', group='oc_fixture', message_id='om_root')
    root.content = '原始主群说明'
    coordinator.prepare_payload(_record(root))
    child = _message('ou_one', group='oc_fixture', message_id='om_first')
    child.content = '接着处理'
    child.conversation_id = 'oc_fixture:thread:omt_fixture'
    child.metadata.pop('media')
    child.metadata.update(feishu_thread_id='omt_fixture', feishu_root_id='om_root')
    record = _record(child)
    first = coordinator.prepare_payload(record)
    assert '原始主群说明' in first['prompt']
    assert 'om_root-example.txt' in first['prompt']
    assert first == coordinator.prepare_payload(record)
    child.message_id = 'om_later'
    later = coordinator.prepare_payload(_record(child))
    assert later['prompt'] == '接着处理'
    assert first['metadata']['channel_conversation_id'] == child.conversation_id


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w11_delegate_retirement.py
# =============================================================================

# LLM: pytest为各旧delegate写法构造临时会话并调用退役控制入口；核对仅返回route迁移提示、线程字节不变且不创建旧偏好文件，不以迁移回执当执行成功。
# 函数用途: 检查所有旧委派命令都只提示迁移，不再保存开关。
@pytest.mark.parametrize('suffix', ['', 'enable', 'disable', 'status', 'true', 'enable other', 'arbitrary'])
def test_every_delegate_form_is_migration_only(tmp_path, suffix):
    agent, thread = fixture_agent(tmp_path)
    before = agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes()
    command = parse_conversation_command('/delegate' + (' ' + suffix if suffix else ''))
    assert isinstance(command, ConversationControlCommand) and command.kind == 'delegate'
    result = execute_delegation_control(agent, command, GatewayControlScope('ou_a', 'feishu', 'oc_source'))
    assert '/route' in result.message and not result.ok
    assert agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes() == before
    assert not (agent.home_paths.owner_home_dir / 'config' / 'im_preferences.json').exists()


# LLM: pytest只渲染普通IM私聊身份的静态帮助文本，检查唯一公开路由开关为route；不读写偏好、创建任务或连接IM。
# 函数用途: 确认帮助不再宣传delegate或另一个router命令。
def test_help_has_one_route_switch_and_no_delegate_advertisement():
    help_text = render_command_help(CommandAccessContext(surface='im', is_admin=False, private_chat=True))
    assert '/route' in help_text
    assert '/delegate' not in help_text
    assert '/router' not in help_text


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w11_route_command.py
# =============================================================================

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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
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
    monkeypatch.setattr(control_service, 'resolve_loaded_gateway_scope_agent', lambda base, scope: agent)
    before = _tree(tmp_path)
    with pytest.raises(DataCorruptionError, match='route setting is unreadable'):
        _route_capture(agent, scope, entry)
    assert _tree(tmp_path) == before


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w1_revision_history.py
# =============================================================================

# LLM: 所有读写均在tmp_path，路径约定来自跨分片合同。
# 函数用途: 返回测试历史根。
def _root(home):
    return home / 'data' / 'asset_evolution'


# LLM: 描述符只提供执行所需定义，不读取真实用户资产。
# 函数用途: 为测试构造独立脚本定义。
def _payload():
    return {'id': 'demo', 'kind': 'script', 'entrypoint': 'main.py',
            'metadata': {'source_system': 'local'}, 'io_contract': {'outputs': ['result']}}


# LLM: 字节身份与原store读出的UTF-8代码及现有pin计算式相同。
# 函数用途: 验证原始blob身份、幂等和二进制无损。
def test_blob_raw_identity_and_store_pin(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    code = 'print("result")\r\n'
    row = ScriptStore(tmp_path).save_script(AssetDescriptor('demo', 'script', 'Demo'), code)
    expected = hashlib.sha256(row['code'].encode('utf-8')).hexdigest()
    actual = history.blob_put(tmp_path, code.encode('utf-8'))
    assert actual == expected
    assert history.blob_put(tmp_path, code.encode('utf-8'), max_total_bytes=0) == actual
    assert history.blob_get(tmp_path, actual) == code.encode('utf-8')
    assert history.blob_exists(tmp_path, actual) is True
    assert history.blob_get(tmp_path, '0' * 64) is None
    assert history.blob_exists(tmp_path, '0' * 64) is False
    raw = b'\x00\xff\x80\r\n'
    sha = history.blob_put(tmp_path, raw)
    assert sha == hashlib.sha256(raw).hexdigest()
    assert (_root(tmp_path) / 'blobs' / sha[:2] / sha).read_bytes() == raw


# LLM: 指纹忽略行尾和末尾空行，不忽略缩进或内部空行。
# 函数用途: 验证精确代码规范化合同。
def test_code_fingerprint_normalization():
    assert history.normalize_code_text('if True:\r\n  x = 1 \t\r\n\r\n') == 'if True:\n  x = 1'
    first = history.code_fingerprint(b'if True:\n  x = 1\n')
    assert first is not None
    assert first == history.code_fingerprint('if True:\r\n  x = 1 \t\r\n\r\n')
    assert first != history.code_fingerprint('if True:\n    x = 1\n')
    assert first != history.code_fingerprint('if True:\n\n  x = 1\n')


# LLM: 执行外统计和宿主metadata不参与指纹，来源系统和用户参数参与。
# 函数用途: 验证载荷的规范JSON、易变字段剥离和不可变输入。
def test_payload_and_asset_fingerprint():
    payload = _payload()
    before = json.dumps(payload, sort_keys=True)
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
    fingerprint = history.payload_fingerprint(payload)
    assert fingerprint == 'sha256:' + expected
    assert history.payload_fingerprint(payload | {'revision': 9, 'scope_layer': 'workspace',
        'source_scope': 'workspace', 'workspace_root': '/not-used', 'runtime_summary': {'run_count': 5},
        'created_at': 'later', 'updated_at': 'later', 'last_run_at': 'later', 'run_count': 5}) == fingerprint
    other = payload | {'metadata': payload['metadata'] | {'used_by': ['ignored'], 'version': 4}}
    assert history.payload_fingerprint(other) == fingerprint
    assert history.payload_fingerprint(payload | {'metadata': {'source_system': 'changed'}}) != fingerprint
    code_fp = history.code_fingerprint(b'print(1)\n')
    side_fp = history.code_fingerprint('note\n')
    raw = f'code={code_fp}\npayload={fingerprint}\nside=README.md:{side_fp}'
    expected_asset = 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()
    assert history.asset_fingerprint(payload=payload, code=b'print(1)\n', side_files={'README.md': 'note\n'}) == expected_asset
    assert history.asset_fingerprint(payload=payload, side_files={}) == history.asset_fingerprint(payload=payload, side_files={'removed.md': None})
    assert json.dumps(payload, sort_keys=True) == before


# LLM: 只规范化去重，真实缩进与内容更改必须升版；旧记录字节不变。
# 函数用途: 验证两个计数分离和不可变追加。
def test_versions_deduplicate_and_append(tmp_path):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'print(1)\n', storage_revision=7)
    assert isinstance(first, dict)
    assert first['created'] is True and first['version'] == 1
    first_path = _root(tmp_path) / 'versions' / 'script' / 'demo' / 'v1.json'
    frozen = first_path.read_bytes()
    unchanged = history.record_version(tmp_path, 'script', 'demo', payload=_payload() | {'revision': 99}, code=b'print(1) \t\r\n\r\n')
    assert unchanged == {'created': False, 'version': 1, 'record': first['record']}
    second = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'print(2)\n', expected_latest=1)
    third = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'  print(2)\n')
    assert second['version'] == 2 and third['version'] == 3
    assert first_path.read_bytes() == frozen
    assert history.latest_version(tmp_path, 'script', 'demo') == third['record']
    assert history.get_version(tmp_path, 'script', 'demo', 1) == first['record']
    assert history.get_version(tmp_path, 'script', 'demo', 99) is None
    assert history.latest_version(tmp_path, 'workflow', 'missing') is None


# LLM: 元信息按原样冻结；多条同sha时返回最新匹配，多个过滤条件同时成立才匹配。
# 函数用途: 验证记录字段、pins和历史查询顺序。
def test_record_fields_and_find_newest(tmp_path):
    pins = {'scripts': {'child': {'version': 1, 'code_sha256': 'a' * 64, 'execution_definition_ref': None}},
            'skills': {'builtin:method': {'content_sha256': 'b' * 64}}, 'workflows': {}}
    first = history.record_version(tmp_path, 'workflow', 'demo', payload={'steps': []}, code=b'pass\n',
        side_files={'WORKFLOW.md': 'text\n', 'removed.md': None}, pins=pins, legacy=True,
        restored_from=2, storage_revision=8, execution_definition_ref='definition',
        published_at='2026-11-01T00:00:00+00:00', summary_line='one\nline\x00' + 'x' * 150)
    assert isinstance(first, dict)
    record = first['record']
    assert record['schema_version'] == 'asset_version.v1'
    assert record['legacy'] is True and record['restored_from'] == 2
    assert record['storage_revision'] == 8 and record['pins'] == pins
    assert record['code_sha256'] == hashlib.sha256(b'pass\n').hexdigest()
    assert record['published_at'] == '2026-11-01T00:00:00+00:00'
    assert len(record['summary_line']) <= 120 and not any(c in record['summary_line'] for c in '\n\x00')
    assert set(record['side_blobs']) == {'WORKFLOW.md'}
    second = history.record_version(tmp_path, 'workflow', 'demo', payload={'steps': [1]}, code=b'pass\n', execution_definition_ref='definition')
    assert history.find_version(tmp_path, 'workflow', 'demo', code_sha256=record['code_sha256']) == second['record']
    assert history.find_version(tmp_path, 'workflow', 'demo', execution_definition_ref='definition') == second['record']
    assert history.find_version(tmp_path, 'workflow', 'demo', fingerprint=record['fingerprint']) == record
    assert history.find_version(tmp_path, 'workflow', 'demo', code_sha256='0' * 64, execution_definition_ref='definition') is None
    rows = history.list_versions(tmp_path, 'workflow', 'demo', limit=1)
    assert [row['version'] for row in rows] == [2]
    assert 'payload_blob' not in rows[0]
    assert history.list_versions(tmp_path, 'workflow', 'demo', limit=0) == []
    index = json.loads((_root(tmp_path) / 'versions' / 'workflow' / 'demo.json').read_text())
    assert [row['version'] for row in index['versions']] == [1, 2]


# LLM: 版本CAS须在同一资产锁内核验；不把bool当整数版本。
# 函数用途: 验证冲突错误和并发写者不丢行。
def test_version_cas_and_concurrent_writers(tmp_path):
    history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    with pytest.raises(history.AssetHistoryError) as error:
        history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'next', expected_latest=0)
    assert error.value.code == 'ASSET_VERSION_CONFLICT'
    barrier = Barrier(2)

    # LLM: 同时进入真实存储调用，不用睡眠猜测线程顺序。
    # 函数用途: 竞争发布不同代码。
    def publish(code):
        barrier.wait(timeout=10)
        return history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (b'two', b'three')))
    assert sorted(result['version'] for result in results) == [2, 3]
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]


# LLM: 历史代码按raw字节落盘；任何所需blob缺失都不能回退到新版本。
# 函数用途: 验证staging布局、侧文件及结构化缺失错误。
@pytest.mark.parametrize('missing', ['code_sha256', 'payload_blob', 'side'])
def test_materialize_and_missing_blob(tmp_path, missing):
    result = history.record_version(tmp_path, 'script', 'demo', payload=_payload(),
        code=b'print(1)\r\n', side_files={'README.md': 'note\n'})
    assert isinstance(result, dict)
    record = result['record']
    staging = tmp_path / 'stage'
    actual = history.materialize_script(tmp_path, record, staging)
    assert actual == staging / 'demo' / 'v1'
    assert (actual / 'main.py').read_bytes() == b'print(1)\r\n'
    assert (actual / 'README.md').read_text() == 'note\n'
    sha = record['side_blobs']['README.md'] if missing == 'side' else record[missing]
    (_root(tmp_path) / 'blobs' / sha[:2] / sha).unlink()
    with pytest.raises(history.AssetHistoryError) as error:
        history.materialize_script(tmp_path, record, tmp_path / 'second-stage')
    assert error.value.code == 'ASSET_HISTORY_BLOB_MISSING'
    assert not (tmp_path / 'second-stage' / 'demo' / 'v1' / 'main.py').exists()


# LLM: 索引可重建，发布记录和软删除标记不是可清理版本。
# 函数用途: 验证损坏索引修复和删除后历史保留。
@pytest.mark.parametrize('broken', ['not json', '{}', '{"latest":999}', '[]'])
def test_corrupt_index_rebuild_and_deleted_history(tmp_path, broken):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    assert isinstance(first, dict)
    history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'second')
    index_path = _root(tmp_path) / 'versions' / 'script' / 'demo.json'
    index_path.write_text(broken)
    assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 2
    assert json.loads(index_path.read_text())['latest'] == 2
    history.mark_deleted(tmp_path, 'script', 'demo', deleted_at='2026-11-02T00:00:00Z')
    assert json.loads(index_path.read_text())['deleted_at'] is not None
    assert history.get_version(tmp_path, 'script', 'demo', 1) == first['record']


# LLM: 写索引失败后旧索引原样可读，已落盘版本不可在重试时被覆盖。
# 函数用途: 注入一次I/O故障并检查追加恢复不变量。
def test_index_write_error_preserves_previous_index(tmp_path, monkeypatch):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    assert isinstance(first, dict)
    index_path = _root(tmp_path) / 'versions' / 'script' / 'demo.json'
    previous = index_path.read_bytes()
    real_write = history.write_text_atomic_beneath
    raised = []

    # LLM: 只在指定索引写入前失败一次，其他真实原子写照常执行。
    # 函数用途: 模拟索引替换入口I/O错误。
    def simulated_write(root, parts, text):
        if parts[-1] == 'demo.json' and 'versions' in parts and not raised:
            raised.append(True)
            raise OSError('simulated I/O error')
        return real_write(root, parts, text)

    monkeypatch.setattr(history, 'write_text_atomic_beneath', simulated_write)
    with pytest.raises(OSError, match='simulated I/O error'):
        history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'second')
    assert index_path.read_bytes() == previous
    assert json.loads(previous)['latest'] == 1
    second_path = _root(tmp_path) / 'versions' / 'script' / 'demo' / 'v2.json'
    immutable = second_path.read_bytes()
    third = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'third')
    assert third['version'] == 3
    assert second_path.read_bytes() == immutable
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]


# LLM: 草稿不占发布版本号。
# 函数用途: 验证草稿CRUD。
def test_drafts_prune_and_published_retention(tmp_path):
    result = history.save_draft(tmp_path, 'script', 'demo', payload=_payload(), code=b'draft',
        side_files={'README.md': 'draft'}, base_version=2)
    assert isinstance(result, dict)
    assert 'version' not in result and result['base_version'] == 2
    assert history.get_draft(tmp_path, 'script', 'demo') == result
    assert history.latest_version(tmp_path, 'script', 'demo') is None
    assert history.delete_draft(tmp_path, 'script', 'demo') is True
    assert history.delete_draft(tmp_path, 'script', 'demo') is False


# LLM: 回放判定只认枚举；后写入的相同边结论覆盖旧观察，非连续边不跳级。
# 函数用途: 验证兼容链在首个未通过边停止。
def test_compatibility_chain(tmp_path):
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) is None
    first = history.record_compat_edge(tmp_path, 'script', 'demo', from_version=1, to_version=2,
        verdict='replay_passed', replayed=10, evidence_ref='ref-one')
    assert isinstance(first, dict)
    assert first['replayed'] == 10 and first['evidence_ref'] == 'ref-one'
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=3, verdict='replay_passed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 3
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=3, verdict='replay_failed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 2
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=4, verdict='replay_passed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 2
    assert history.read_compat_edges(tmp_path, 'script', 'demo')[-1]['to_version'] == 4
    with pytest.raises(ValueError):
        history.record_compat_edge(tmp_path, 'script', 'demo', from_version=1, to_version=2, verdict='unknown')


# LLM: 配额按Owner原始字节计算；重复blob不多计，独立写者不能共用最后空间。
# 函数用途: 验证配额边界、并发准入和隔离。
def test_blob_quota_concurrent_admission(tmp_path):
    assert history.blob_put(tmp_path, b'abc', max_total_bytes=3) == hashlib.sha256(b'abc').hexdigest()
    with pytest.raises(history.AssetHistoryError) as error:
        history.blob_put(tmp_path, b'd', max_total_bytes=3)
    assert error.value.code == 'ASSET_HISTORY_QUOTA_EXCEEDED'
    other = tmp_path / 'other'
    barrier = Barrier(2)

    # LLM: 两个资产共用Owner blob配额，返回结构化准入结果。
    # 函数用途: 同时竞争三字节配额。
    def put(raw):
        barrier.wait(timeout=10)
        try:
            return history.blob_put(other, raw, max_total_bytes=3)
        except history.AssetHistoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(put, (b'one', b'two')))
    assert results.count('ASSET_HISTORY_QUOTA_EXCEEDED') == 1


# LLM: 用户段必须是安全单段，非法输入在发生任何状态写入前拒绝。
# 函数用途: 验证kind、ID、digest及侧文件路径边界。
@pytest.mark.parametrize('kind,asset_id', [('unknown', 'demo'), ('script', '../demo'), ('script', '/absolute'), ('script', 'x/y')])
def test_invalid_asset_identity(tmp_path, kind, asset_id):
    with pytest.raises(ValueError):
        history.record_version(tmp_path, kind, asset_id, payload={})
    assert not _root(tmp_path).exists()


# LLM: 标识与文件名不能穿越；所有历史读写入口共用相同校验。
# 函数用途: 验证错误版本、摘要hash和侧文件均被拒绝。
def test_invalid_blob_and_side_names(tmp_path):
    with pytest.raises(ValueError):
        history.blob_get(tmp_path, '../bad')
    with pytest.raises(ValueError):
        history.record_version(tmp_path, 'script', 'demo', payload={}, side_files={'../bad': 'text'})
    with pytest.raises(ValueError):
        history.get_version(tmp_path, 'script', 'demo', True)


# LLM: Windows不支持dir_fd时走既有portable原子写，不引入fcntl。
# 函数用途: 验证替代文件系统入口上的真实往返。
def test_portable_atomic_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    result = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'pass\n')
    assert isinstance(result, dict)
    assert history.get_version(tmp_path, 'script', 'demo', 1) == result['record']
    actual = history.materialize_script(tmp_path, result['record'], tmp_path / 'stage')
    assert (actual / 'main.py').read_bytes() == b'pass\n'


# LLM: 合同公开参数按名字、位置/关键字、默认值核对，不依赖测试作者手抄。
# 函数用途: 验证跨分片公共API形状。
def test_history_public_signatures():
    expected = {
        'blob_put': 'owner_home, data: bytes, *, max_total_bytes: int = 268435456',
        'blob_get': 'owner_home, sha256: str', 'blob_exists': 'owner_home, sha256: str',
        'record_version': "owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None, side_files: Mapping[str, str | None] | None = None, execution_definition_ref: str | None = None, storage_revision: int | None = None, pins: Mapping | None = None, summary_line: str = '', published_at: str | None = None, legacy: bool = False, restored_from: int | None = None, expected_latest: int | None = None",
        'latest_version': 'owner_home, kind, asset_id', 'get_version': 'owner_home, kind, asset_id, version: int',
        'find_version': 'owner_home, kind, asset_id, *, code_sha256: str | None = None, execution_definition_ref: str | None = None, fingerprint: str | None = None',
        'list_versions': 'owner_home, kind, asset_id, *, limit: int = 20',
        'mark_deleted': 'owner_home, kind, asset_id, *, deleted_at: str',
        'materialize_script': 'owner_home, record: Mapping, staging_root',
        'save_draft': 'owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None, side_files: Mapping[str, str | None] | None = None, base_version: int | None = None',
        'get_draft': 'owner_home, kind, asset_id', 'delete_draft': 'owner_home, kind, asset_id',
        'normalize_code_text': 'text: str', 'code_fingerprint': 'code: bytes | str',
        'payload_fingerprint': 'payload: Mapping',
        'asset_fingerprint': '*, payload: Mapping, code: bytes | str | None = None, side_files: Mapping[str, str | None] | None = None',
        'record_compat_edge': "owner_home, kind, asset_id, *, from_version: int, to_version: int, verdict: str, replayed: int = 0, evidence_ref: str = '', recorded_at: str | None = None",
        'read_compat_edges': 'owner_home, kind, asset_id',
        'compatible_successor': 'owner_home, kind, asset_id, from_version: int',
    }
    for name, arguments in expected.items():
        tree = ast.parse(f'def f({arguments}): pass')
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), tree.body[0]], type_ignores=[])), '<signature>', 'exec'), namespace)
        actual = inspect.signature(getattr(history, name))
        reference = inspect.signature(namespace['f'])
        assert [(p.name, p.kind, p.default) for p in actual.parameters.values()] == [(p.name, p.kind, p.default) for p in reference.parameters.values()], name


# LLM: 原资产锁默认阻塞语义保持，维护调用blocking=False须在真实竞争时立即拒绝，锁文件不删除。
# 函数用途: 覆盖W9新增的资产锁非阻塞参数。
def test_w9_asset_lock_nonblocking_contention(tmp_path):
    import threading

    ready, release = threading.Event(), threading.Event()
    # LLM: 用原资产领域锁持有临界区，不猴补锁返回值。
    # 函数用途: 构造跨线程的真实资产锁竞争。
    def worker():
        with history._asset_lock(tmp_path, 'script', 'busy'):
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(5)
    try:
        with pytest.raises(BlockingIOError):
            with history._asset_lock(tmp_path, 'script', 'busy', blocking=False):
                pytest.fail('busy asset lock unexpectedly acquired')
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
    with history._asset_lock(tmp_path, 'script', 'busy', blocking=False):
        assert (tmp_path / 'data' / 'asset_evolution' / 'versions' / 'script' / 'busy.json.lock').exists()


# =============================================================================
# Source: agent_py_agent/tests/test_v15_w2_skill_bindings.py
# =============================================================================

# LLM: 所有样本仅写入pytest隔离目录，不使用真实Owner目录。
# 函数用途: 创建含结构化头部和可选侧文件的技能样本。
def _skill(root: Path, name: str, front: dict, side: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    fields = {"name": name, "description": "绑定测试", **front}
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    path = directory / "SKILL.md"
    path.write_text(f"---\n{header}\n---\n# 不应提前输出的正文\n", encoding="utf-8")
    if side is not None:
        (directory / "skill_meta.json").write_text(json.dumps(side), encoding="utf-8")
    return path


# LLM: 四类引用沿AssetRefs既有字典形状，不发明新的资产种类。
# 函数用途: 给断言补齐所有空引用列表。
def _refs(values: dict) -> dict:
    return {"workflows": [], "scripts": [], "skills": [], "monitors": [], **values}


# LLM: 禁止路径一旦调用就断言失败，不以空返回掩盖提前读取。
# 函数用途: 阻止metadata路径预读正文或重新发现候选。
def _forbidden_read(first, second=None, third=None, *, max_chars=0):
    pytest.fail("元数据路径不得提前读取Skill正文或重新发现候选")


# LLM: frontmatter与侧文件分别归一后求并集，不能让侧文件覆盖整组引用。
# 函数用途: 覆盖单来源、重叠及重复，核对catalog/list/冻结entry相等。
@pytest.mark.parametrize(
    ("front", "side", "expected"),
    [
        ({"scripts": ["b", "a"]}, None, {"scripts": ["a", "b"]}),
        ({}, {"workflows": ["w-b", "w-a"]}, {"workflows": ["w-a", "w-b"]}),
        ({"scripts": ["b", "a"], "workflows": ["w-a"]},
         {"scripts": ["c", "b"], "workflows": ["w-b"]},
         {"scripts": ["a", "b", "c"], "workflows": ["w-a", "w-b"]}),
        ({"scripts": ["b", "b", "a", "a"]}, {"scripts": ["b", "c", "c"]},
         {"scripts": ["a", "b", "c"]}),
    ],
    ids=["frontmatter-only", "sidecar-only", "overlap", "dedup"],
)
def test_refs_union_catalog_list_snapshot(tmp_path, skill_catalog_factory, front, side, expected):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {"refs": front}, None if side is None else {"refs": side})
    catalog = skill_catalog_factory(home)
    wanted = _refs(expected)
    assert adapter.extract_skill_descriptor(path)["refs"] == wanted
    assert adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir)[0]["refs"] == wanted
    rows = adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)
    assert rows[0]["refs"] == wanted
    assert getattr(catalog.snapshot.resolve("owner:bound"), "refs", {}) == wanted


# LLM: 限额作用于排序去重后的总并集，不先截断任意一个来源。
# 函数用途: 验证各类最多64且稳定排序。
def test_refs_union_has_per_kind_sorted_cap(tmp_path, skill_catalog_factory):
    kinds = ("workflows", "scripts", "skills", "monitors")
    front = {kind: [f"z-{index:03d}" for index in reversed(range(80))] for kind in kinds}
    side = {kind: [f"a-{index:03d}" for index in reversed(range(20))] * 2 for kind in kinds}
    expected = {kind: [f"a-{index:03d}" for index in range(20)] + [f"z-{index:03d}" for index in range(44)]
                for kind in kinds}
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "capped", {"refs": front}, {"refs": side})
    catalog = skill_catalog_factory(home)
    assert adapter.extract_skill_descriptor(path)["refs"] == expected
    assert catalog.snapshot.resolve("capped").refs == expected
    assert adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)[0]["refs"] == expected


# LLM: 新字段不能改变既有位置参数，缺省容器不得跨实例共享。
# 函数用途: 固定keyword-only默认值和公开序列化的身份边界。
def test_snapshot_entry_fields_are_defaulted_keyword_only():
    signature = inspect.signature(SkillSnapshotEntry)
    assert "refs" in signature.parameters
    assert signature.parameters["refs"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["asset_metadata"].kind is inspect.Parameter.KEYWORD_ONLY
    first = SkillSnapshotEntry("owner:x", "x", "x", "", "owner", "general", "hash")
    second = SkillSnapshotEntry("owner:y", "y", "y", "", "owner", "general", "hash")
    assert first.refs == second.refs == {}
    assert first.refs is not second.refs
    assert "path" not in first.to_dict()
    assert first.to_ref() == {"stable_id": "owner:x", "name": "x", "source": "owner", "content_sha256": "hash"}


# LLM: 发现阶段结束后只消费冻结entry，不能为了目录渲染调用read_body或重新发现。
# 函数用途: 验证不读正文的元数据入口和冻结可见性元数据。
def test_metadata_projection_uses_frozen_refs_without_body_read(tmp_path, skill_catalog_factory, monkeypatch):
    home = tmp_path / "catalog"
    _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["s"]}},
           {"refs": {"workflows": ["w"]}, "metadata": {"hidden": True, "member_of": ["workflow:parent"]}})
    catalog = skill_catalog_factory(home)
    monkeypatch.setattr(SkillSnapshot, "read_body", _forbidden_read)
    monkeypatch.setattr(adapter, "_catalog_paths", _forbidden_read)
    monkeypatch.setattr(adapter, "_text", _forbidden_read)
    entry = catalog.snapshot.resolve("bound")
    rows = adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)
    detail = adapter.get_owner_skill_descriptor(catalog.home.owner_home_dir, "owner:bound", skill_snapshot=catalog.snapshot)
    assert rows[0]["refs"] == detail["refs"] == _refs({"scripts": ["s"], "workflows": ["w"]})
    assert entry.asset_metadata["hidden"] is True
    public = entry.to_dict()
    assert public["refs"] == detail["refs"]
    public["refs"]["scripts"].append("not-frozen")
    assert entry.refs["scripts"] == ["s"]
    assert detail["metadata"]["member_of"] == ["workflow:parent"]
    assert "body_markdown" not in detail and "path" not in detail
    assert "不应提前输出" not in json.dumps(detail, ensure_ascii=False)
    narrowed = catalog.snapshot.restricted(("owner:bound",))
    assert narrowed.resolve("bound").refs == entry.refs


# LLM: 旧快照不重新合入磁盘新绑定；显式正文读取仍核原hash。
# 函数用途: 验证侧文件事件重建、冻结引用与正文陈旧错误。
def test_snapshot_refs_stay_frozen_and_body_hash_still_enforced(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {}, {"refs": {"scripts": ["before"]}})
    catalog = skill_catalog_factory(home)
    restricted = catalog.snapshot.restricted(("owner:bound",))
    path.with_name("skill_meta.json").write_text(json.dumps({"refs": {"scripts": ["after"]}}), encoding="utf-8")
    assert restricted.resolve("bound").refs == _refs({"scripts": ["before"]})
    refreshed = catalog.service.snapshot_for(force_reload=True)
    assert refreshed.resolve("bound").refs == _refs({"scripts": ["after"]})
    detail = adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=restricted, include_body=True)
    assert detail["refs"] == _refs({"scripts": ["before"]})
    assert detail["body_markdown"].endswith("# 不应提前输出的正文\n")
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(SkillSnapshotError) as caught:
        adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=restricted, include_body=True)
    assert caught.value.error_code == "SKILL_SNAPSHOT_STALE"


# LLM: 当前canonical侧开关停用不能被旧授权或冻结refs复活。
# 函数用途: 验证禁用条目及空/受限快照不扩大候选集合。
def test_disabled_and_restricted_snapshots_do_not_discover_extra_refs(tmp_path, skill_catalog_factory, monkeypatch):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "allowed", {}, {"refs": {"scripts": ["s"]}})
    _skill(home / "owner/skills", "disabled", {}, {"enabled": False, "refs": {"scripts": ["private"]}})
    catalog = skill_catalog_factory(home)
    restricted = catalog.snapshot.restricted(("owner:allowed",))
    monkeypatch.setattr(adapter, "_catalog_paths", _forbidden_read)
    assert [row["name"] for row in adapter.list_owner_skill_descriptors(home / "owner", skill_snapshot=restricted)] == ["allowed"]
    assert adapter.list_owner_skill_descriptors(home / "owner", skill_snapshot=replace(restricted, entries=())) == []
    path.with_name("skill_meta.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
    with pytest.raises(SkillSnapshotError) as caught:
        adapter.get_owner_skill_descriptor(home / "owner", "allowed", skill_snapshot=restricted)
    assert caught.value.error_code == "ASSET_DISABLED_BY_OWNER"


# LLM: 所有新增绑定读取都必须拒绝链接，不能等正文get才拒绝。
# 函数用途: 验证nofollow侧文件及hash不符的entry不会发表绑定。
def test_snapshot_ref_capture_rejects_sidecar_links_and_stale_body(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["s"]}})
    catalog = skill_catalog_factory(home)
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"refs": {"scripts": ["not-authorized"]}}), encoding="utf-8")
    path.with_name("skill_meta.json").symlink_to(external)
    fresh_entry = replace(catalog.snapshot.resolve("bound"), refs={}, asset_metadata={})
    linked = replace(catalog.snapshot, entries=(fresh_entry,))
    assert not linked.enabled_entries()
    assert linked.errors[-1].code == "SKILL_READ_FAILED"
    path.with_name("skill_meta.json").unlink()
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    stale = replace(catalog.snapshot, entries=(fresh_entry,))
    assert not stale.enabled_entries()
    assert stale.errors[-1].code == "SKILL_SNAPSHOT_STALE"


# LLM: 历史正文hash换绑必须连同frontmatter绑定换版，不能沿用当前正文的refs。
# 函数用途: 验证原历史授权解析口产生的entry冻结正确历史头部与当前合法侧绑定。
def test_historical_snapshot_refs_follow_the_pinned_body(tmp_path, skill_catalog_factory, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_versions as versions
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    home = tmp_path / "catalog"
    _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["current"]}},
           {"refs": {"workflows": ["side"]}})
    old_path = _skill(tmp_path / "history", "bound", {"refs": {"scripts": ["historical"]}})
    old_text = old_path.read_text(encoding="utf-8")
    old_hash = skill_content_sha256(old_text)
    catalog = skill_catalog_factory(home)

    # LLM: 只替代已有历史授权口的返回值，不扩大snapshot的候选集合。
    # 函数用途: 给同一身份提供准确旧hash条目。
    def resolve(entry, expected):
        assert expected == old_hash
        return replace(entry, content_sha256=old_hash, frozen_path=str(old_path))

    # LLM: 原历史读取仍须接收准确固定hash，测试正文来自隔离版本文件。
    # 函数用途: 提供已授权历史正文，不访问真实学习目录。
    def read_historical(entry):
        assert entry.content_sha256 == old_hash
        return old_text

    monkeypatch.setattr(versions, "resolve_learned_revision", resolve)
    monkeypatch.setattr(versions, "read_frozen_learned_body", read_historical)
    historical = catalog.snapshot.restricted(("owner:bound",), expected_sha256={"owner:bound": old_hash})
    assert historical.resolve("bound").refs == _refs({"scripts": ["historical"], "workflows": ["side"]})


# LLM: 去掉正文预读不能丢失原先从头部提取的IO/访问声明和版本字段。
# 函数用途: 验证冻结元数据路径保留既有提取键，不为refs修复制造字段回退。
def test_snapshot_projection_preserves_extraction_fields(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {
        "version": "3.0.0",
        "refs": {"scripts": ["s"]},
        "access_targets": [{"target_id": "fixture", "type": "file", "endpoint": "fixture-input"}],
        "data_sources": [{"source_id": "fixture", "system": "fixture", "query_scope": {"limit": 1}}],
        "credential_refs": [{"credential_ref": "credential-fixture"}],
        "io_contract": {"inputs": ["indicator"], "outputs": ["report"]},
    })
    catalog = skill_catalog_factory(home)
    normal = adapter.extract_skill_descriptor(path)
    frozen = adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=catalog.snapshot)
    for key in ("version", "access_targets", "data_sources", "credential_refs", "io_contract"):
        assert frozen[key] == normal[key], key
