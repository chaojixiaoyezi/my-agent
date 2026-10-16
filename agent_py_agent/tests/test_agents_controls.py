"""公共parser/direct/Gateway HTTP夹具入口主链；真实飞书真人未验证。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.models import Permission, Role
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import agents_control_service as controls
from agent_py_agent.agent.gateway_parts import control_service, http_handlers
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    _command_text,
    execute_chat_control,
)
from agent_py_agent.tests.test_agents_write_service import source


# LLM: 只替换宿主组合依赖；共享控制/授权服务/A/文件写回全部真实执行。
# 函数用途: 生成隔离owner的可信入口夹具。
@pytest.fixture
def host(source, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    root = source.path_policy.agent_home_root
    home = home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))
    from agent_py_agent.agent.conversation import ConversationStore
    agent = SimpleNamespace(home_paths=home, config=SimpleNamespace(access_mode='workspace-write'),
                            conversation_store=ConversationStore(home.owner_home_dir / "conversations"))
    def repository(selected, *, file_commit=None):
        source._file_commit = file_commit
        return source
    monkeypatch.setattr(controls, 'build_agents_source_repository', repository)
    monkeypatch.setattr(control_service, '_request_agent_for_scope', lambda base, scope: agent)
    return agent


# LLM: 公共parser与TUI传输要保留原JSON正文，不能切尾空白或改新值。
# 函数用途: 覆盖注册、解析及跨传输文本重建。
def test_public_parser_roundtrip():
    raw = '/agents write "原样\\n尾空白  "'
    command = parse_conversation_control(raw)
    assert command.kind == 'agents' and command.valid
    assert _command_text(command) == raw
    assert parse_conversation_control('/agents confirmed true').valid is False
    assert parse_conversation_control('请学习并修改 AGENTS') is None


# LLM: direct只从UI控制入口产生原请求，不借在跑模型的request_id。
# 函数用途: 驱动direct查看、预览、确认与真实文件/耐久结构回执。
def test_direct_entry_main_chain(host, source):  # noqa: F811
    execution = ChatControlExecution(host, False, ChatControlState(False, 0, '', 0, 'session'))
    show = execute_chat_control(execution, parse_conversation_control('/agents'), message_id='show')
    assert show.ok and show.details['content'] == '原始用户约定\n'
    preview = execute_chat_control(execution, parse_conversation_control('/agents write "直接修改"'), message_id='p')
    assert preview.ok and preview.control_state == 'preview'
    assert source.default_path.read_text() == '原始用户约定\n'
    confirmation = execute_chat_control(execution, parse_conversation_control(preview.details['confirm_command']), message_id='c')
    assert confirmation.ok and confirmation.to_dict()['details']['state'] == 'completed'
    assert source.default_path.read_text() == '直接修改'


# LLM: HTTP测试通过已认证handler夹具，不宣称认证协议、真人渠道或网络已验收。
# 函数用途: 覆盖Gateway/IM公共HTTP分派后真实预览确认路径。
@pytest.mark.parametrize('channel', ['chat', 'feishu'])
def test_http_entry_main_chain(host, source, monkeypatch, channel):  # noqa: F811
    sent = []
    handler = SimpleNamespace(_auth_middleware=object(), _send_json=lambda status, body: sent.append((status, body)))
    monkeypatch.setattr(http_handlers, '_request_identity', lambda handler: ('fixture-user', Permission(Role.ADMIN, 'fixture-user', [channel])))
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(source.owner_home / "gateway"))
    def send(text, message):
        scope = GatewayControlScope('fixture-user', channel, 'conversation', {'message_id': message})
        http_handlers._handle_persistent_control_operation(handler, server,
            command=parse_conversation_control(text), command_text=text, scope=scope)
        assert sent[-1][0] == 200
        assert sent[-1][1]['status'] == 'control'
        return sent[-1][1]
    assert send('/agents', 's')['ok']
    preview = send('/agents write "HTTP入口修改"', 'p')
    assert preview['control_state'] == 'completed'
    assert preview['details']['state'] == 'preview'
    result = send(preview['details']['confirm_command'], 'c')
    assert result['ok'] and result['details']['state'] == 'completed'
    assert source.default_path.read_text() == 'HTTP入口修改'
    replay = send(preview['details']['confirm_command'], 'retry')
    assert not replay['ok'] and replay['error_code'] == 'AGENTS_WRITE_REPLAYED'


# LLM: 普通Gateway scope和JSON自称user都没有直接入口证据。
# 函数用途: 拒绝旁路控制和未认证HTTP兼容入口。
def test_no_scope_metadata_authority(host, source, monkeypatch):  # noqa: F811
    command = parse_conversation_control('/agents write "不得写"')
    scope = GatewayControlScope('u', 'feishu', 't', {'role': 'user', 'confirmed': True, 'message_id': 'm'})
    result = control_service.execute_gateway_conversation_control(host, None, command, scope)
    assert not result.ok and result.error_code == 'AGENTS_WRITE_NOT_USER_REQUEST'
    sent = []
    handler = SimpleNamespace(_auth_middleware=None, _send_json=lambda status, body: sent.append(body))
    monkeypatch.setattr(http_handlers, '_request_identity', lambda handler: ('u', None))
    http_handlers._handle_persistent_control_operation(handler, SimpleNamespace(agent=host, paths=None),
        command=command, command_text="/agents " + command.value, scope=scope)
    assert not sent[-1]['ok']
    assert source.default_path.read_text() == '原始用户约定\n'


# LLM: target文本不是管理员身份，独立跨owner选择仍受宿主admin+FullAccess闸门。
# 函数用途: 检查管理员默认档和普通用户都不能仅靠命令目标越权。
def test_admin_target_requires_current_full_access(host, source):  # noqa: F811
    command = parse_conversation_control('/agents owner providers/feishu/users/other reset')
    result = controls.execute_agents_direct(host, command, 'session', 'message')
    assert not result.ok and result.error_code == 'AGENTS_WRITE_CROSS_OWNER_FORBIDDEN'
    assert not source.config_path.exists()


# LLM: 全权限管理员仍需独立显式target；模拟宿主owner解析，但真实目标A仓库提交不替换。
# 函数用途: 覆盖明确跨owner授权成功与管理员默认只操作自身。
def test_admin_full_access_explicit_target(host, source, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    root = source.path_policy.agent_home_root
    target_id = 'providers/feishu/users/other'
    home = root / 'owners' / target_id
    home.mkdir(parents=True)
    (home / 'AGENTS.md').write_text('目标用户原文')
    repo = AgentsSourceRepository(owner=target_id, owner_home=home,
        path_policy=PathAccessPolicy.from_values(mode='full_access', agent_home_root=root))
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    target = SimpleNamespace(home_paths=home_paths_with_owner(host.home_paths,
        resolve_owner_home(root, OwnerIdentity.provider_user('feishu', 'other'))), config=host.config)
    original_cold = controls.cold_owner_host
    monkeypatch.setattr(controls, 'cold_owner_host',
        lambda base, identity: target if identity != OwnerIdentity.local_main() else original_cold(base, identity))
    monkeypatch.setattr(controls, 'build_agents_source_repository', lambda selected, file_commit=None: repo if selected is target else source)
    host.config.access_mode = 'full-access'
    own = controls.execute_agents_direct(host, parse_conversation_control('/agents'), 'session', 'own')
    assert own.details['snapshot']['owner'] == 'local/main'
    preview = controls.execute_agents_direct(host,
        parse_conversation_control('/agents owner ' + target_id + ' write "明确目标修改"'), 'session', 'p')
    assert preview.ok and preview.details['old_cas'][0] == target_id
    confirmed = controls.execute_agents_direct(host,
        parse_conversation_control(preview.details['confirm_command']), 'session', 'c')
    assert confirmed.ok
    assert (home / 'AGENTS.md').read_text() == '明确目标修改'
    assert source.default_path.read_text() == '原始用户约定\n'


# LLM: IM的控制完成只表示消息投递，C副作用未知必须原样显示并指向receipt，不创建通用轮询或重放。
# 函数用途: 验证已有IM协调入口消费C的unknown回执，保留ok=false。
def test_im_unknown_receipt_is_delivered_without_generic_replay():
    from agent_py_agent.agent.adapter.manager import _ChannelIngressCoordinator
    replies = []
    manager = SimpleNamespace(_send_gateway_reply=lambda message, key, text: replies.append(text) or True)
    payload = dict(status='control', kind='agents', ok=False, request_id='confirmation',
                   message='结果未确认；请使用 /agents receipt abc',
                   control_state='completed', operation_id='control-fixture', receipt_id='control-fixture', delivery_status='accepted', details={'state': 'consumed_unknown'})
    record = SimpleNamespace(kind='message', state='prepared', ingress_id='fixture-ingress', submission=payload, content='/agents confirm abc', channel='feishu',
                             user_id='fixture-user', provider_message_id='message',
                             conversation_id='conversation', timestamp=1000, metadata={})
    _ChannelIngressCoordinator(manager).advance_submission(record)
    assert replies == [payload['message']]
    assert payload['ok'] is False and payload['delivery_status'] == 'accepted'
