"""F4适配主线原控制身份后的独立边界回归，全部使用临时owner及认证夹具。"""
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.auth.models import Permission, Role
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import agents_control_service as controls
from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.tests.test_agents_controls import host as host
from agent_py_agent.tests.test_agents_write_service import source as source


def test_ingress_seal_expires_with_authenticated_stack(host, source):
    command = parse_conversation_control('/agents write "不得晚到写入"')
    scope = GatewayControlScope('user-a', 'feishu', 'conversation', {'message_id': 'sealed'})
    permission = Permission(Role.ADMIN, 'user-a', ['feishu'])
    with controls.agents_http_ingress(scope, command, permission) as authenticated:
        bound = controls.bind_agents_effect_scope(authenticated, scope, command)
        assert bound.agents_ingress is not None
    result = controls.execute_agents_control(host, command, bound)
    assert not result.ok and result.error_code == 'AGENTS_WRITE_NOT_USER_REQUEST'
    assert source.default_path.read_text() == '原始用户约定\n'


def test_ingress_seal_cannot_bind_different_actor_or_command(host, source):
    command = parse_conversation_control('/agents write "原精确意图"')
    scope = GatewayControlScope('user-a', 'feishu', 'conversation', {'message_id': 'sealed'})
    permission = Permission(Role.ADMIN, 'user-a', ['feishu'])
    with controls.agents_http_ingress(scope, command, permission) as authenticated:
        for changed, selected in [
            (replace(scope, user_id='user-b'), command),
            (scope, parse_conversation_control('/agents write "不同意图"')),
        ]:
            bound = controls.bind_agents_effect_scope(authenticated, changed, selected)
            result = controls.execute_agents_control(host, selected, bound)
            assert not result.ok and result.error_code == 'AGENTS_WRITE_NOT_USER_REQUEST'
    assert source.default_path.read_text() == '原始用户约定\n'


def test_two_authenticated_actors_sharing_owner_cannot_confirm_each_other(host, source, monkeypatch):
    sent = []
    caller = {'user': 'user-a'}
    handler = SimpleNamespace(_auth_middleware=object(), _send_json=lambda status, body: sent.append((status, body)))
    monkeypatch.setattr(http_handlers, '_request_identity', lambda _: (caller['user'], Permission(Role.ADMIN, caller['user'], ['feishu'])))
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(source.owner_home / 'gateway'))

    # LLM: 这里只共享隔离owner路由，不替换身份绑定、预览、nonce或写入；不宣称真实群租约验收。
    # 函数用途: 在原HTTP入口模拟不同已认证成员操作同一owner。
    def send(text, message_id):
        scope = GatewayControlScope(caller['user'], 'feishu', 'conversation', {'message_id': message_id})
        http_handlers._handle_persistent_control_operation(handler, server,
            command=parse_conversation_control(text), command_text=text, scope=scope)
        assert sent[-1][0] == 200
        return sent[-1][1]

    preview = send('/agents write "只由原用户确认"', 'preview-a')
    assert preview['ok'] and preview['details']['state'] == 'preview'
    caller['user'] = 'user-b'
    denied = send(preview['details']['confirm_command'], 'confirm-b')
    assert not denied['ok'] and denied['error_code'] == 'AGENTS_WRITE_BINDING_MISMATCH'
    assert source.default_path.read_text() == '原始用户约定\n'
    caller['user'] = 'user-a'
    accepted = send(preview['details']['confirm_command'], 'confirm-a')
    assert accepted['ok'] and accepted['details']['state'] == 'completed'
    assert source.default_path.read_text() == '只由原用户确认'


# LLM: 普通provider用户的AGENTS仍是本人owner控制；HTTP认证/owner解析/路径许可/nonce和写入都用真实实现，不借全机admin设置门。
# 函数用途: 验证普通用户可在自己的来源上预览确认切换及修改，且不改默认原件或其它owner。
def test_regular_owner_can_confirm_own_agents_source_and_content(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    root = tmp_path / 'isolated-home'
    base = SimpleAgent(AgentConfig(my_agent_home=str(root), prompt_files=[]), tmp_path / 'workspace')
    owner = SimpleAgent(AgentConfig(my_agent_home=str(root), prompt_files=[],
        my_agent_owner_provider='feishu', my_agent_owner_kind='user', my_agent_owner_id='alice'), tmp_path / 'alice-workspace')
    source = owner.agents_source_repository
    source.default_path.write_text('本人默认规则\n', encoding='utf-8')
    selected = source.owner_home / 'explicit-rules.txt'
    selected.write_text('本人选择的规则\n', encoding='utf-8')
    sent = []
    handler = SimpleNamespace(_auth_middleware=object(), _send_json=lambda status, body: sent.append((status, body)))
    monkeypatch.setattr(http_handlers, '_request_identity', lambda _: ('alice', Permission(Role.USER, 'alice', ['feishu'])))
    server = SimpleNamespace(agent=base, paths=gateway_paths_from_root(tmp_path / 'gateway'))

    # LLM: resolved_owner模拟HTTP前段已解析的规范本人身份，不来自模型metadata或命令目标字符串。
    # 函数用途: 通过原持久HTTP控制提交普通用户自己的直接命令。
    def send(text, message_id):
        scope = GatewayControlScope('alice', 'feishu', 'alice-private', {'message_id': message_id},
            resolved_owner=OwnerIdentity.provider_user('feishu', 'alice'))
        http_handlers._handle_persistent_control_operation(handler, server,
            command=parse_conversation_control(text), command_text=text, scope=scope)
        code, result = sent[-1]
        assert code == 200 and result['ok'], result
        return result['details']

    preview = send('/agents source local ' + str(selected), 'source-preview')
    assert preview['state'] == 'preview' and preview['old_cas'][0] == source.owner
    switched = send(preview['confirm_command'], 'source-confirm')
    assert switched['state'] == 'completed'
    preview = send('/agents write "本人确认后的新规则"', 'write-preview')
    assert selected.read_text() == '本人选择的规则\n'
    assert send(preview['confirm_command'], 'write-confirm')['state'] == 'completed'
    assert selected.read_text() == '本人确认后的新规则'
    assert source.default_path.read_text() == '本人默认规则\n'
