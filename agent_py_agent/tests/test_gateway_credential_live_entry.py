"""首次普通 Gateway 请求的持久输入合同；只用临时 owner、假模型及假卡片运输。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.agent.user_space.task_credentials import credential_path
from agent_py_agent.tests.test_background_main_agent_runtime import _native_probe


# LLM: 只替换模型响应，队列认领、owner 路由、任务晋升和工具权限均使用原生产入口。
# 类用途: 在首次采样确认尚无任务，再申请一次指定工具，不接触真实网络。
class _InputBackend:
    name = 'gateway-input-native-test'
    probe_tool_capability = _native_probe

    # LLM: agent 是正式 owner 实例；参数仅为合成工具请求，不允许 fixture 预建 TaskLink 掩盖首次入口。
    # 函数用途: 记住要申请的输入和采样次数。
    def __init__(self, agent, request_id, tool='request_credentials', arguments=None):
        self.agent, self.tool = agent, tool
        self.request_id = request_id
        self.arguments = arguments or {}
        self.calls = 0
        self.observed = ''
        self.tool_counts = []

    # LLM: 模型按需先发现工具，再发一次申请或查询；后续仅返回合成正文，不补产物或替宿主登记输入。
    # 函数用途: 驱动真实工具循环，并证明第一次模型调用前没有活动任务。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        self.tool_counts.append(len(kwargs.get('tools') or []))
        if self.calls == 1:
            assert self.agent.conversation_store.tasks.load(self.request_id) is None
        if self.tool == 'feishu_auth' and self.calls == 1:
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                'id': 'discover-auth', 'name': 'tool_search', 'input': {'query': 'feishu_auth'},
            }])
        if self.calls == (2 if self.tool == 'feishu_auth' else 1):
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                'id': 'synthetic-input-call', 'name': self.tool, 'input': self.arguments,
            }])
        self.observed = json.dumps(kwargs.get('messages') or [], ensure_ascii=False, default=str)
        return ModelResponse(text='已处理合成输入请求。', backend=self.name)


# LLM: 基础 Gateway 与 owner 目录故意不同，工具必须使用原 writer 的队列路径；仅真实运输由记录器替换。
# 函数用途: 把一个普通飞书请求入队，建立其真实 scoped Agent，返回可执行的隔离环境。
def _queued_input(tmp_path, kind, *, tool='request_credentials', arguments=None):
    base = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        prompt_files=[], enable_plugins=False), tmp_path / 'gateway-host')
    paths = gateway_paths(base)
    request_id, request_path, response_path = submit_gateway_ask(paths, params=GatewayAskParams(
        prompt='请发一张信息卡，我填写后继续。', save=False, chat_session_id='oc_synthetic',
        channel='feishu', channel_user_id='ou_synthetic', canonical_user_id='ou_synthetic',
        channel_chat_type='group' if kind == 'group' else 'p2p', channel_chat_id='oc_synthetic'))
    owner = _resolve_request_agent(base, json.loads(request_path.read_text()))
    backend = _InputBackend(owner, request_id, tool, arguments)
    owner.backend = backend
    sent = []

    # LLM: 只记录投递目标和卡片，真实签名、密钥、输入账与消息回执仍由产品代码处理。
    # 函数用途: 模拟一张卡成功送达，不访问平台或目标链接。
    def send(target, card, **kwargs):
        sent.append((target, card))
        return {'message_id': 'om_synthetic', 'chat_id': 'oc_synthetic'}

    owner.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=send),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    assert owner is not base and gateway_paths(owner).root != paths.root
    return SimpleNamespace(base=base, agent=owner, paths=paths, request_id=request_id,
        response_path=response_path, backend=backend, sent=sent)


@pytest.mark.parametrize('kind', ['group', 'user'])
def test_first_gateway_credential_request_materializes_original_task_and_waits(tmp_path, kind):
    world = _queued_input(tmp_path, kind)
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['ok'], (response.get('error_code'), response.get('error'))
    assert response['runtime_status'] == 'needs_user_input'
    assert response['runtime_reason'] == 'TASK_INPUT_WAIT' and response['turn_end_reason'] == 'blocked'
    assert world.backend.calls == 2 and world.backend.tool_counts[-1] == 0
    assert '[natural-user-reply]' in world.backend.observed
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'created'
    attempt = world.agent.subagents.runtime_db.current_attempt(run['agent_run_id'])
    assert attempt['status'] == 'done' and attempt['ended_at'] > 0
    assert world.backend.calls, json.dumps(response, ensure_ascii=False)
    assert world.sent, json.dumps(response, ensure_ascii=False)
    assert len(world.sent) == 1
    assert world.sent[0][0] == ('oc_synthetic' if kind == 'group' else 'ou_synthetic')
    link = world.agent.conversation_store.tasks.load(world.request_id)
    assert link is not None and link.status == 'active' and len(link.input_dependency_refs) == 1
    dependency = world.agent.conversation_store.session_tasks.storage.session_tasks_dir / 'inputs'
    row = next(json.loads(path.read_text()) for path in dependency.glob('*.json'))
    assert row['status'] == 'pending' and row['armed']
    assert row['scope']['root_task_id'] == link.task_id and row['scope']['thread_id'] == link.thread_id
    credential = json.loads(credential_path(world.agent.home_paths.owner_home_dir, row['reference']).read_text())
    assert credential['delivery_state'] == 'confirmed'
    assert credential['actor_id'] == 'ou_synthetic'
    assert world.agent.conversation_store.wakes.pending() == []


@pytest.mark.parametrize('kind', ['group', 'user'])
@pytest.mark.parametrize('decision', ['submit', 'cancel'])
def test_original_first_request_callback_only_resumes_exact_input_once(tmp_path, kind, decision):
    world = _queued_input(tmp_path, kind)
    world.base.config.feishu_session_lock_enabled = False
    assert _process_gateway_requests(world.base, world.paths) == 1
    assert json.loads(world.response_path.read_text())['ok']
    assert len(world.sent) == 1
    store = world.agent.conversation_store
    link = store.tasks.load(world.request_id)
    path = next((store.session_tasks.storage.session_tasks_dir / 'inputs').glob('*.json'))
    dependency = json.loads(path.read_text())
    credential = json.loads(credential_path(world.agent.home_paths.owner_home_dir, dependency['reference']).read_text())
    body = {'reference': credential['reference'], 'decision': decision,
        'message_id': credential['message_id'], 'chat_id': credential['chat_id']}
    if decision == 'submit':
        body['fields'] = {'target_url': 'https://synthetic.example.invalid', 'username': 'Synthetic-Account',
            'secret': 'Synthetic-Password-91e2'}
    if kind == 'group':
        body['submitter'] = {'actor_id': 'ou_synthetic', 'chat_id': 'oc_synthetic', 'current_group_member': True}
    first = resolve_private_interaction(world.base, body, channel='feishu', actor_id='ou_synthetic')
    assert first['ok']
    assert resolve_private_interaction(world.base, body, channel='feishu', actor_id='ou_synthetic')['ok']
    assert not store.tasks.load(world.request_id).input_dependency_refs
    saved = json.loads(path.read_text())
    assert saved['status'] == ('ready' if decision == 'submit' else 'cancelled')
    wakes = store.wakes.pending()
    assert len(wakes) == (1 if decision == 'submit' else 0)
    if wakes:
        assert wakes[0].root_task_id == link.task_id and wakes[0].thread_id == link.thread_id
    assert 'Synthetic-Password-91e2' not in json.dumps(saved)
    assert 'Synthetic-Password-91e2' not in json.dumps(wakes, default=str)


@pytest.mark.parametrize('expression', ['tool_then_text', 'empty', 'model_error'])
def test_wait_reply_retries_without_replaying_card_or_failing_original_run(tmp_path, expression):
    world = _queued_input(tmp_path, 'group')
    generate = world.backend.generate

    # LLM: 表达模型故意违约；宿主必须拒绝它的工具调用，不能把无工具轮变成第二次发卡。
    # 函数用途: 注入一次原生越权调用或两次空正文，检查原输入保持可恢复且没有额外副作用。
    def reply(prompt, on_chunk=None, **kwargs):
        response = generate(prompt, on_chunk, **kwargs)
        if world.backend.calls > 1:
            assert not kwargs.get('tools')
            if expression in {'empty', 'model_error'}:
                return ModelResponse(text='', backend=world.backend.name,
                    runtime_status='failed' if expression == 'model_error' else 'ok')
            if world.backend.calls == 2:
                return ModelResponse(text='', backend=world.backend.name, tool_use_blocks=[{
                    'id': 'forbidden-expression-call', 'name': 'request_credentials', 'input': {},
                }])
        return response

    world.backend.generate = reply
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['ok'] is (expression == 'tool_then_text')
    if expression != 'tool_then_text':
        assert response['error_code'] == 'USER_REPLY_UNAVAILABLE'
        assert not response.get('response')
    else:
        assert response['runtime_status'] == 'needs_user_input'
        assert 'TOOL_CHOICE_VIOLATION' in world.backend.observed
    assert world.backend.calls == 3 and world.backend.tool_counts[-2:] == [0, 0]
    assert len(world.sent) == 1
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'created'
    assert world.agent.subagents.runtime_db.current_attempt(run['agent_run_id'])['status'] == 'done'
    link = world.agent.conversation_store.tasks.load(world.request_id)
    assert link.status == 'active' and len(link.input_dependency_refs) == 1
    assert world.agent.conversation_store.wakes.pending() == []


def test_stop_during_wait_expression_keeps_original_control_closeout(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as loop

    world = _queued_input(tmp_path, 'user')
    generate, stopped = world.backend.generate, []

    # LLM: 只注入已有中断信号；不把正文当停止，也不替产品取消输入或创建Wake。
    # 函数用途: 在无工具回执生成期间停止，证明模型回复不能覆盖真实控制结果。
    def reply(prompt, on_chunk=None, **kwargs):
        response = generate(prompt, on_chunk, **kwargs)
        if world.backend.calls == 2:
            assert not kwargs.get('tools')
            stopped.append(True)
        return response

    world.backend.generate = reply
    monkeypatch.setattr(loop, 'is_interrupted', lambda: bool(stopped))
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['runtime_status'] == 'cancelled' and response['runtime_reason'] == 'user_stop'
    assert not response.get('response')
    assert world.backend.calls == 2 and len(world.sent) == 1
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'cancelled'
    assert world.agent.subagents.runtime_db.current_attempt(run['agent_run_id'])['status'] != 'running'
    assert world.agent.conversation_store.wakes.pending() == []


@pytest.mark.parametrize('decision', ['cancel', 'submit'])
@pytest.mark.parametrize('other_input', [False, True])
def test_card_resolved_during_expression_uses_remaining_input_or_original_wake(tmp_path, decision, other_input):
    world = _queued_input(tmp_path, 'group')
    world.base.config.feishu_session_lock_enabled = False
    generate = world.backend.generate

    # LLM: 取消使用原卡回调及同群成员事实；表达只返回旧草稿，不替宿主改输入行或清理任务。
    # 函数用途: 稳定复现表达在途取消，防止无输入无Wake时仍留下等待运行。
    def reply(prompt, on_chunk=None, **kwargs):
        response = generate(prompt, on_chunk, **kwargs)
        if world.backend.calls == 2:
            path = next((world.agent.conversation_store.session_tasks.storage.session_tasks_dir / 'inputs').glob('*.json'))
            dependency = json.loads(path.read_text())
            card = json.loads(credential_path(world.agent.home_paths.owner_home_dir, dependency['reference']).read_text())
            if other_input:
                from agent_py_agent.agent.conversation.input_dependencies import (
                    arm_input_dependency,
                    create_input_dependency,
                )
                from agent_py_agent.agent.conversation.tool_approval_scope import ToolApprovalScope
                create_input_dependency(world.agent, ToolApprovalScope(**dependency['scope']),
                    input_id='other-pending-input', provider='synthetic-extra', expires_at=dependency['expires_at'])
                arm_input_dependency(world.agent, 'other-pending-input')
            body = {'reference': card['reference'], 'decision': decision,
                'message_id': card['message_id'], 'chat_id': card['chat_id'],
                'submitter': {'actor_id': 'ou_synthetic', 'chat_id': 'oc_synthetic', 'current_group_member': True}}
            if decision == 'submit':
                body['fields'] = {'target_url': 'https://synthetic.example.invalid',
                    'username': 'Synthetic-Account', 'secret': 'Synthetic-Password'}
            assert resolve_private_interaction(world.base, body, channel='feishu', actor_id='ou_synthetic')['ok']
        return response

    world.backend.generate = reply
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    cancelled = decision == 'cancel' and not other_input
    assert response['runtime_status'] == ('cancelled' if cancelled else 'needs_user_input' if other_input else 'unfinished')
    if cancelled:
        assert response['runtime_reason'] == 'TASK_INPUT_CANCELLED' and not response.get('response')
    elif not other_input:
        assert response['runtime_reason'] == 'TASK_INPUT_RESUME_PENDING' and response['ok']
    assert len(world.sent) == 1 and world.backend.calls == (2 if cancelled else 3)
    link = world.agent.conversation_store.tasks.load(world.request_id)
    assert link.status == 'active' and len(link.input_dependency_refs) == int(other_input)
    assert len(world.agent.conversation_store.wakes.pending()) == (1 if decision == 'submit' else 0)
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == ('cancelled' if cancelled else 'created')


@pytest.mark.parametrize('failure', ['response', 'configuration'])
def test_typed_provider_expression_exception_reports_reply_failure_but_preserves_input(tmp_path, failure):
    from agent_py_agent.agent.backends.errors import (
        ProviderConfigurationError,
        ProviderResponseError,
    )

    world = _queued_input(tmp_path, 'user')
    generate = world.backend.generate

    # LLM: 供应商错误走真实模型调用异常边界；不伪造来源或吞宿主/预算/代码异常。
    # 函数用途: 无工具表达未成功时保留原输入，公开出口明确失败且卡片不重复发送。
    def reply(prompt, on_chunk=None, **kwargs):
        response = generate(prompt, on_chunk, **kwargs)
        if world.backend.calls == 2:
            error_type = ProviderResponseError if failure == 'response' else ProviderConfigurationError
            raise error_type('synthetic expression failure')
        return response

    world.backend.generate = reply
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert not response['ok'] and response['error_code'] == 'USER_REPLY_UNAVAILABLE'
    assert world.backend.calls == 2 and len(world.sent) == 1
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'created'
    assert world.agent.subagents.runtime_db.current_attempt(run['agent_run_id'])['status'] == 'done'
    assert len(world.agent.conversation_store.tasks.load(world.request_id).input_dependency_refs) == 1


@pytest.mark.parametrize('reason,source,silent', [
    ('TASK_INPUT_CANCELLED', 'input_dependency', True),
    ('TASK_INPUT_SUPERSEDED', 'input_dependency', True),
    ('TASK_INPUT_CANCELLED', 'model_provider', False),
    ('TASK_INPUT_WAIT', 'input_dependency', False),
])
def test_silent_input_control_requires_exact_source_and_reason(reason, source, silent):
    from agent_py_agent.agent.gateway_parts.response_renderer import is_silent_user_stop

    assert is_silent_user_stop(SimpleNamespace(runtime_status='cancelled',
        runtime_reason=reason, runtime_source=source)) is silent


@pytest.mark.parametrize('failure', ['cancelled_response', 'budget', 'code_error'])
def test_input_expression_does_not_reinterpret_control_budget_or_code_failure(tmp_path, failure):
    from agent_py_agent.agent.contracts.model_call_budget import ModelCallBudgetError

    world = _queued_input(tmp_path, 'user')
    generate = world.backend.generate

    # LLM: 只有供应商typed故障可保留表达等待；预算拒绝、代码异常和取消保持原控制/失败合同。
    # 函数用途: 从整轮Gateway验证表达层没有吞硬门，也不会重试取消或再次发送卡片。
    def reply(prompt, on_chunk=None, **kwargs):
        response = generate(prompt, on_chunk, **kwargs)
        if world.backend.calls == 2:
            if failure == 'budget':
                raise ModelCallBudgetError('synthetic_budget_rejected')
            if failure == 'code_error':
                raise RuntimeError('synthetic code failure')
            return ModelResponse(text='', backend=world.backend.name, runtime_status='cancelled',
                runtime_reason='user_stop', runtime_source='conversation_control')
        return response

    world.backend.generate = reply
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == ('cancelled' if failure == 'cancelled_response' else 'failed')
    if failure == 'cancelled_response':
        assert response['runtime_status'] == 'cancelled' and response['runtime_reason'] == 'user_stop'
    else:
        assert not response['ok']
    assert world.backend.calls == 2 and len(world.sent) == 1
    assert len(world.agent.conversation_store.tasks.load(world.request_id).input_dependency_refs) == 1


# LLM: 只注入合成部署配置，不操作真实应用；OAuth 请求/回调保存和原 Wake 全部保留。
# 函数用途: 让隔离用户能发现并调用本人授权工具，不在 fixture 里预建任务。
def _oauth_world(tmp_path, monkeypatch, action):
    monkeypatch.setattr(oauth, 'deployment_capability_config', lambda _home: CapabilityConfig(
        feishu_user_oauth_enabled=True, feishu_oauth_redirect_uri='https://callback.example.invalid/feishu/oauth/callback'))
    world = _queued_input(tmp_path, 'user', tool='feishu_auth', arguments={'action': action})
    world.agent.config.feishu_app_id = 'synthetic-app'
    world.agent.config.feishu_app_secret = 'Synthetic-App-Secret'
    return world


def test_first_gateway_authorize_materializes_task_then_callback_uses_original_wake(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.feishu_oauth_service import oauth_callback_payload

    world = _oauth_world(tmp_path, monkeypatch, 'authorize')
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['ok'], (response.get('error_code'), response.get('error'))
    assert response['runtime_status'] == 'needs_user_input' and response['turn_end_reason'] == 'blocked'
    assert world.backend.calls == 3 and world.backend.tool_counts[-1] == 0
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'created'
    assert len(world.sent) == 1, json.loads(world.response_path.read_text())
    link = world.agent.conversation_store.tasks.load(world.request_id)
    assert link and len(link.input_dependency_refs) == 1
    state = parse_qs(urlsplit(world.sent[0][1]['elements'][1]['actions'][0]['url']).query)['state'][0]
    payload = oauth_callback_payload(world.base, state)
    calls = []

    # LLM: 仅替换远端 token 与本人证明响应；准确 state、owner、输入代次及幂等仍走原函数。
    # 函数用途: 为本次合成授权提供成功结果，不发真实请求。
    def api(method, path, body=None, token=''):
        calls.append(path)
        if path.endswith('/user_info'):
            return {'code': 0, 'data': {'open_id': 'ou_synthetic'}}
        return {'code': 0, 'access_token': 'Synthetic-Access-Token', 'expires_in': 3600,
            'refresh_token': 'Synthetic-Refresh-Token', 'refresh_token_expires_in': 86400, 'scope': 'offline_access'}

    monkeypatch.setattr(oauth, '_oauth_api', api)
    assert oauth.complete_oauth(world.agent, payload, state, 'Synthetic-Code')['ok']
    assert oauth.complete_oauth(world.agent, payload, state, 'Synthetic-Code')['ok']
    assert len(calls) == 2
    wakes = world.agent.conversation_store.wakes.pending()
    assert len(wakes) == 1 and wakes[0].root_task_id == world.request_id
    assert not world.agent.conversation_store.tasks.load(world.request_id).input_dependency_refs


def test_gateway_oauth_status_does_not_materialize_task_or_send_card(tmp_path, monkeypatch):
    world = _oauth_world(tmp_path, monkeypatch, 'status')
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['ok'] and world.backend.calls == 3
    assert 'not_authorized' in world.backend.observed and 'FEISHU_USER_AUTH_REQUIRED' not in world.backend.observed
    assert not world.sent and world.agent.conversation_store.tasks.load(world.request_id) is None
