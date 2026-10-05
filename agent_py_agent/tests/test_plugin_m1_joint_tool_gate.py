# LLM: 联合测试保留真实 B5 征询、唯一执行器、审批桥、归档、runtime_events 与 B6 info；只适配安装/启动和离线模型。
# 模块用途: 让真实 rm-guard 样例接到收紧执行路径；不安装启用插件、不证明 B7 沙箱或生产 Gateway。
"""真实17j必须具备B5执行器字段；缺接线直接失败，不再靠AST或skip掩盖回退。

jb6 在真实17j上修正测试激活碰撞；不能把缺 B5 时的 skip 当作联合链路通过。
队列消费仍由测试手动推进。handler 是零副作用计数探针，不执行 rm 或删除真实文件。
第 3/4 段接口变动需同步 guard_joint 的组合根接线、_run_case 的审批消费和本次 binding 断言。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import core, plugin_management
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.contracts.gates.command_policy import evaluate_command_policy
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)
from agent_py_agent.tests import test_plugin_event_e2e as _event_e2e
from agent_py_agent.tests.test_plugin_event_e2e import _Backend, _reply_with_user_denial
from agent_py_agent.tests.test_plugin_event_gateway import _Handler, gateway
from agent_py_agent.tests.test_plugin_m1_joint_e2e import _PUBLISH, _sample, watch

_FIXTURES = (gateway, watch)
_DELETE_PATCH = '*** Begin Patch\n*** Delete File: disposable.txt\n*** End Patch\n'
_EXPECTED_FIELDS = {
    'plugin_id', 'version', 'activation_id', 'gate_id', 'tool', 'call_id', 'operation_id',
    'args_hash', 'actor', 'outcome', 'verdict', 'reason_code', 'latency_ms', 'host_status', 'final_status',
}


# LLM: 用相同工具名/字符串 schema 进入真实执行器，保留宿主先验裁决；探针没有 Shell/删除副作用，避免反证真的删数据。
# 类用途: 只数 handler 进入次数，严格证明前置拒绝时是零执行，而不是执行后报错。
class _GuardCounter(BaseTool):
    # LLM: 只在隔离注册表装配计数工具，保留原宿主裁决，不获得真实 Shell 或文件删除能力。
    # 函数用途: 按原工具名构造没有业务副作用的 handler 探针。
    def __init__(self, name, field):
        self.executions = 0
        self.model_spec = ToolModelSpec(name, '隔离计数探针，不执行命令或删文件', {
            'type': 'object', 'properties': {field: {'type': 'string'}},
            'required': [field], 'additionalProperties': False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(default_effect='mutating'),
            approval_policy=ApprovalPolicy('never'), idempotency_policy=IdempotencyPolicy('operation'))

    # LLM: 进入次数只证明 handler 开始；返回值沿真实归档链记录，不执行参数里的命令或补丁。
    # 函数用途: 记录唯一执行器是否真的进入 handler。
    def execute(self, arguments):
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, 'counted')


# LLM: 联合结果固定成小对象，所有身份来自原执行路径；不手工编审批或写 runtime_events。
# 类用途: 把一次真实回合的执行回执、用户事件和实际征询帧带回断言。
@dataclass(frozen=True)
class _Case:
    execution: object
    permissions: list
    request: dict
    reply: dict


# LLM: 一次联合回合只在这四项上不同：跑哪个工具、什么参数、用户怎么答、客户端有没有声明可交互审批。
#   打包成小对象是为了不把参数堆到执行函数上，也避免每个出口各写一份几乎一样的驱动代码。
#   四项都是结构化事实（工具名/参数/决定词/能力布尔），不从文案或模型输出推断。
# 类用途: 描述一次真实工具回合要跑什么、用户如何作答。
@dataclass(frozen=True)
class _RunSpec:
    name: str
    arguments: dict
    decision: str = 'denied'
    interactive: bool = True


# LLM: 当前17j的B5字段是必备合同，缺失就失败；安装表/启动适配不代表产品启用，不给数据接口加生产旁路。
# 函数用途: 将原样 rm-guard 与观察样例接到同一个 B2 池，使用本 pytest 临时 owner。
@pytest.fixture
def guard_joint(tmp_path, gateway, watch, monkeypatch):
    assert 'plugin_gate_reviewer' in ToolExecutorRequest.__dataclass_fields__, '真实17j缺少B5执行器接线'
    from agent_py_agent.agent.plugin_events import tool_gate_review

    agent, _paths, server, _events = gateway
    project, row = _sample(tmp_path, 'rm-guard')
    watch.projects[row.manifest.plugin_id] = project
    monkeypatch.setattr(type(agent.config), 'plugin_events_enabled', True, raising=False)
    server.plugin_event_hub, server.plugin_channel_pool = watch.hub, watch.pool
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', _PUBLISH)
    monkeypatch.setattr(tool_gate_review, 'enabled_gate_installations', lambda _owner: (row,))
    agent.tools.plugin_gate_reviewer = core._build_plugin_gate_reviewer(agent, agent.config)
    assert callable(agent.tools.plugin_gate_reviewer)
    agent.tools.approval_mode_reader = lambda: 'auto'
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    yield SimpleNamespace(gateway=gateway, watch=watch, row=row, owner=owner, project=project)


# LLM: 探针原样转交生产归档函数；不能造 ToolExecution 或补写丢失的门决定来让联合测试变绿。
# 函数用途: 收集真实工具轮的 canonical 回执。
def _capture_executions(monkeypatch):
    recorded = []
    original = round_execution._record_execution
    # LLM: 原样转交归档参数和返回值，不手造成功回执或门决定。
    # 函数用途: 记录真实工具轮已产生的执行对象，随后继续原归档。
    def capture(*args):
        recorded.append(args[-1])
        return original(*args)
    monkeypatch.setattr(round_execution, '_record_execution', capture)
    return recorded


# LLM: 只换"用户选了哪个选项"这一处；发布、等待、binding 核验、执行器、归档一律走 e2e 那条真实路径，
#   所以批准、拒绝、不可用的差别只在宿主写进决策文件的那一个决定词，不会引入第二套审批通路。
# 函数用途: 按 spec.decision 让真实审批链收到批准、拒绝或不可用。
def _reply_with_user_decision(monkeypatch, client_events, decision):
    if decision == 'denied':
        _reply_with_user_denial(monkeypatch, client_events)
        return
    original = _event_e2e.write_gateway_permission_decision
    # 函数用途: 把 e2e 助手写下的拒绝换成 spec 指定的决定词，其余参数原样透传。
    def decide(chunk_path, request, _decision):
        return original(chunk_path, request, ToolApprovalDecision(request.permission_id, decision))
    monkeypatch.setattr(_event_e2e, 'write_gateway_permission_decision', decide)
    _reply_with_user_denial(monkeypatch, client_events)


# LLM: handler 与 execute_one/审批/归档不替换；只在客户端写决定文件，inbox 到 processing 明确手动推进。
#   批准时 handler 会真的进入（计数探针），拒绝与无法审批时必须是零执行——这两个期望值都来自 spec，不是放宽带过。
# 函数用途: 用离线模型提交一个真实工具回合，读回真实插件响应和 canonical 结果。
def _run_case(bundle, monkeypatch, spec):
    agent, paths, server, _events = bundle.gateway
    tool = _GuardCounter(spec.name, next(iter(spec.arguments)))
    # 仅在隔离注册表的装配期换成无副作用 handler，随后仍经原 register 校验与冻结。
    monkeypatch.delitem(agent.tools.tools, spec.name)
    agent.tools.register(tool)
    agent.backend = _Backend(spec.name, spec.arguments)
    recorded, permissions = _capture_executions(monkeypatch), []
    _reply_with_user_decision(monkeypatch, permissions, spec.decision)
    # 同一个 owner 里跑第二轮时客户端会累积帧；先记基线，只断言本轮新增的那一次征询。
    before_sent, before_replies = _review_frame_counts(bundle)
    # 不声明 tool_approval 就是宿主事实上的"这里没人能当场审批"（与 TUI/已绑定管理员 IM 之外一致）。
    capabilities = {'tool_approval': True} if spec.interactive else {}
    handler = _Handler({'prompt': 'joint-gate-private-marker', 'conversation_id': 'joint-gate-thread',
                        'client_capabilities': capabilities})
    http_handlers.handle_ask(handler, server, lambda: 'joint-gate-request')
    assert handler.replies[-1][0] == 202
    request_id = handler.replies[-1][1]['request_id']
    queued = json.loads((paths.inbox / f'{request_id}.json').read_text())
    queued.update(status='processing', turn_phase='open', execution_attempt_id='joint-gate-attempt')
    processing = paths.processing / f'{request_id}.json'
    processing.write_text(json.dumps(queued))
    response = _handle_gateway_request(agent, processing)
    assert response['ok'], response
    ran = spec.decision == 'approved' and spec.interactive
    assert tool.executions == int(ran) and agent.backend.calls == 2
    assert len(recorded) == 1
    execution = recorded[0]
    assert execution.call.arguments == spec.arguments
    assert execution.result.handler_executed is ran and execution.result.ok is ran
    bundle.watch.executor.run_all()
    clients = [client for client in bundle.watch.clients if client.installation.manifest.plugin_id == 'rm-guard']
    assert len(clients) == 1, {'error_code': execution.result.error_code,
                               'reason_codes': execution.decision.reason_codes}
    client = clients[0]
    sent, replies = _review_frames(client)
    sent, replies = sent[before_sent:], replies[before_replies:]
    assert len(sent) == len(replies) == 1
    return _Case(execution, permissions, sent[0], replies[0])


# LLM: 客户端跨轮累积帧，断言"这一轮恰好一次征询"必须减掉本轮之前的数量；身份只按协议方法名筛。
# 函数用途: 取出一个插件客户端收到的（征询请求, 征询应答）帧。
def _review_frames(client):
    sent = [value for method, value in client.calls if method == 'my-agent/tool-gate.review']
    replies = [value for method, value in client.responses if method == 'my-agent/tool-gate.review']
    return sent, replies


# 函数用途: 取当前 rm-guard 客户端已有的征询帧数，作为本轮增量断言的基线（没有客户端时是 0）。
def _review_frame_counts(bundle):
    client = next((item for item in bundle.watch.clients
                   if item.installation.manifest.plugin_id == 'rm-guard'), None)
    if client is None:
        return 0, 0
    sent, replies = _review_frames(client)
    return len(sent), len(replies)


# LLM: 只从event-watch目标客户端读取真实B3帧，不能用另一个插件的事件凑正向集合；未进入handler不能有工具事件。
# 函数用途: 核对本次隔离回合在目标插件留下三个生命周期事件，而没有工具开始或完成事件。
def _assert_no_tool_events(bundle):
    clients = [client for client in bundle.watch.clients
               if client.installation.manifest.plugin_id == bundle.watch.row.manifest.plugin_id]
    assert len(clients) == 1
    events = [event for method, params in clients[0].calls
              if method == 'my-agent/events.observe' for event in params['events']]
    assert len(events) == 3 and len({event['thread_ref'] for event in events}) == 1
    assert {event['type'] for event in events} == {'prompt_submitted', 'turn_started', 'turn_ended'}, (
        bundle.watch.hub.stats(str(bundle.owner.home_dir)))
    assert not any(event['type'] in {'tool_call_started', 'tool_call_finished'} for event in events)
    _assert_distinct_connections(bundle)


# LLM: B2的连接身份是owner/activation_id；两个插件必须各自握手并收到各自协议，不能用第二池避开碰撞。
# 函数用途: 从真实共用池和客户端帧证明观察与收紧连接分别归属两个激活。
def _assert_distinct_connections(bundle):
    rows = (bundle.row, bundle.watch.row)
    expected = {(str(bundle.owner.home_dir), row.activation.activation_id) for row in rows}
    assert len(expected) == 2
    assert set(bundle.watch.pool.connections) == expected
    assert {client.installation.manifest.plugin_id for client in bundle.watch.clients} == {
        row.manifest.plugin_id for row in rows}
    assert len(bundle.watch.clients) == 2
    assert all(item['failed'] == item['unavailable'] == 0 for types in
        bundle.watch.hub.stats(str(bundle.owner.home_dir)).values() for item in types.values())


# LLM: runtime.db 已由真实回合创建；只读查询不能创建表或编行，门决定必须由产品归档链写入。
# 函数用途: 读本临时 owner 的实际 plugin_gate.decided 事件。
def _gate_rows(bundle):
    path = plugin_management.runtime_db_path(bundle.owner.home_dir)
    assert path.is_file()
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM runtime_events WHERE event_type='plugin_gate.decided' ORDER BY seq")]


# LLM: 只替换本实例的安装 snapshot，B6 命令解析/版本检查/查询/渲染均保留；不调用安装启用或写真实安装表。
# 函数用途: 用实际 /plugins info 从同一临时 runtime.db 读最近决定。
def _plugins_info(bundle):
    context = plugin_management.PluginManagementContext(
        owner=bundle.owner, actor_id='local-agent', channel='chat', conversation_id='joint-gate-thread',
        threads=None, workspace=bundle.owner.home_dir,
        path_policy=plugin_management.PathAccessPolicy.from_values(mode='normal'),
        is_admin=False, enabled=True, event_hub=bundle.watch.hub)
    service = plugin_management.PluginManagement(context)
    row = SimpleNamespace(**vars(bundle.row), revision=1, installation_ref='a' * 64,
                          activation_id=bundle.row.activation.activation_id)
    service.installations = SimpleNamespace(snapshot=lambda: (row,))
    catalog = service.catalog()
    reply = service.command('/plugins info rm-guard', revision=catalog.revision, request_id='joint-info')
    assert reply['ok'], reply
    return reply['message']


# LLM: 用真实发出的结构化 call 字段判传输与截断，不替换 PluginToolGate 投影或接受完整参数被改写。
#   interactive 是宿主事实：客户端声明了 tool_approval 才是 True；不可交互时插件应当看到 False。
# 函数用途: 核对门命中、真实调用身份、可交互事实和四千字预算。
def _assert_query(case, gate_id, truncated=False, interactive=True):
    facts = case.request['call']
    assert case.request['gate_id'] == gate_id
    assert set(facts) == {'call_id', 'tool', 'effect', 'actor', 'interactive', 'args_hash',
                          'arguments', 'arguments_truncated'}
    assert facts['tool'] == case.execution.call.tool_name
    assert facts['call_id'] == case.execution.call.call_id
    assert facts['args_hash'] == case.execution.call.args_hash
    assert (facts['effect'], facts['actor'], facts['interactive']) == ('mutating', 'main', interactive)
    assert facts['arguments_truncated'] is truncated
    assert len(json.dumps(facts['arguments'], ensure_ascii=False, separators=(',', ':'))) <= 4000


# LLM: 审批必须实际发布/消费原 binding，不能因 auto 或旧授权省去插件要求的本次确认。
# 函数用途: 核用户拒绝与仅本次选项，确保 handler 前被停住。
def _assert_confirmation_denied(case):
    assert case.execution.result.error_code == 'APPROVAL_REJECTED'
    assert case.execution.decision.status == 'deny'
    assert [event['kind'] for event in case.permissions] == ['permission_requested', 'permission_resolved']
    permission, resolved = case.permissions
    assert resolved['decision'] == 'denied'
    assert resolved['permission_id'] == permission['permission']['permission_id']
    assert [item['decision'] for item in permission['permission']['options']] == ['approved', 'denied']
    assert permission['permission']['options'][0]['label'] == '仅本次'
    reference = json.loads(permission['permission']['binding']['plugin_gate_ref'])
    assert reference['call_id'] == case.execution.call.call_id
    assert reference['args_hash'] == case.execution.call.args_hash


# LLM: 先真实查询 SQL 和 /plugins info，再检查同一次征询唯一行；不补写缺失事件、不把复制 B6 投影当真实命令读取。
#   b5fix9b 之后 ask 后无论批准、拒绝还是无法审批都必须留下恰好一行（此前 ask 后重建 canonical 结果会丢门决定），
#   所以这里不再有"空账本"的豁免分支：读到 0 行就是真实失败。
# 函数用途: 对齐归档写账、最近决定、原因码及安全白名单。
def _assert_ledger_and_info(bundle, case):
    rows, text = _gate_rows(bundle), _plugins_info(bundle)
    assert len(rows) == 1, {'gate_reply': case.reply, 'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert set(payload) == _EXPECTED_FIELDS
    assert payload['plugin_id'] == bundle.row.manifest.plugin_id
    assert payload['version'] == bundle.row.manifest.version
    assert payload['activation_id'] == bundle.row.activation.activation_id
    assert payload['gate_id'] == case.request['gate_id']
    assert payload['call_id'] == case.execution.call.call_id
    assert payload['operation_id'] == case.execution.call.operation_id
    assert payload['args_hash'] == case.execution.call.args_hash
    assert (payload['tool'], payload['actor'], payload['outcome']) == (case.execution.call.tool_name, 'main', 'ok')
    assert (payload['verdict'], payload['reason_code'], payload['host_status'], payload['final_status']) == (
        case.reply['verdict'], case.reply['reason_code'], 'allow', case.reply['verdict'])
    assert rows[0]['attempt_id'] and rows[0]['agent_run_id']
    assert type(payload['latency_ms']) is int and payload['latency_ms'] >= 0
    assert 'private-marker' not in rows[0]['payload_json']
    # 文案只用于可见展示断言，不参与执行裁决。
    assert '最近收紧决定' in text and case.reply['reason_code'] in text
    assert f"结论 {case.reply['verdict']}" in text


def test_rm_rf_forces_confirmation_and_user_denial_never_enters_handler(guard_joint, monkeypatch):
    # 裸rm仍由宿主硬拒；只在无副作用计数handler上选现有规则的差集，不放宽宿主策略。
    assert evaluate_command_policy('rm -rf build', allow_shell_operators=True).finding_codes == (
        'COMMAND_DESTRUCTIVE_DELETE_BLOCKED',)
    command = 'sh -c "rm -rf build"'
    assert evaluate_command_policy(command, allow_shell_operators=True).allowed
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}))
    _assert_query(case, 'guard-rm')
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    _assert_confirmation_denied(case)
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_apply_patch_delete_is_denied_without_approval_handler_or_tool_events(guard_joint, monkeypatch):
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': _DELETE_PATCH}))
    _assert_query(case, 'guard-delete')
    assert case.reply['verdict'] == 'deny' and case.reply['reason_code'] == 'DELETE_FILE_BLOCKED'
    assert case.execution.result.error_code == 'PLUGIN_GATE_DENIED'
    assert case.execution.decision.status == 'deny' and case.permissions == []
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_truncated_patch_without_visible_delete_requires_confirmation(guard_joint, monkeypatch):
    patch = '*** Begin Patch\n*** Add File: padded.txt\n+' + 'x' * 5000 + '\n*** Delete File: disposable.txt\n*** End Patch\n'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': patch}))
    _assert_query(case, 'guard-delete', True)
    assert not any(line.startswith('*** Delete File: ') for line in case.request['call']['arguments']['patch'].splitlines())
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'ARGUMENTS_TRUNCATED'
    _assert_confirmation_denied(case)
    _assert_no_tool_events(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


def test_truncated_patch_with_visible_delete_remains_denied(guard_joint, monkeypatch):
    patch = '*** Begin Patch\n*** Delete File: disposable.txt\n*** Add File: padded.txt\n+' + 'x' * 5000 + '\n*** End Patch\n'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': patch}))
    _assert_query(case, 'guard-delete', True)
    assert any(line.startswith('*** Delete File: ') for line in case.request['call']['arguments']['patch'].splitlines())
    _assert_no_tool_events(guard_joint)
    assert (case.reply['verdict'], case.reply['reason_code']) == ('deny', 'DELETE_FILE_BLOCKED')
    assert case.execution.result.error_code == 'PLUGIN_GATE_DENIED' and case.permissions == []
    _assert_ledger_and_info(guard_joint, case)


# --- jb6b：ask 之后三个出口的真实账本（b5fix9b 已把门决定条目接到审批出口） ---


# LLM: 批准出口与拒绝出口的差别只在用户选了哪个选项：批准会真的重跑并进入 handler，但第一次真实征询的
#   条目必须仍然留在账本里（9b 实测原来批准路径也是空账本）。用零副作用计数探针证明"真跑了"。
# 函数用途: 断言批准后 handler 真执行，且账本与 /plugins info 仍读到那一次征询。
def test_rm_rf_confirmation_approved_runs_handler_and_keeps_ledger(guard_joint, monkeypatch):
    command = 'sh -c "rm -rf build"'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}, decision='approved'))
    _assert_query(case, 'guard-rm')
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.ok is True and case.execution.result.handler_executed is True
    assert [event['kind'] for event in case.permissions] == ['permission_requested', 'permission_resolved']
    assert case.permissions[1]['decision'] == 'approved'
    # 批准后 handler 真的跑了，所以观察插件这次应当同时看到工具开始与结束（与拒绝路径的零工具事件互为反证）。
    _assert_tool_events_visible(guard_joint)
    _assert_ledger_and_info(guard_joint, case)


# LLM: 与 _assert_no_tool_events 配对：handler 真的执行过时，观察插件必须看到工具开始与结束两类事件。
#   只读目标插件的真实 B3 帧与连接归属，不解析文案；缺任何一类都说明观察链断了。
#   注意（已核实，交 3a/9b 判定，不在本用例断言范围）：turn 事件的 thread_ref 来自
#   `conversation.channel_conversation_id`，tool 事件的 thread_ref 来自 task attribute
#   `conversation_thread_id`，两者在本回合不同；所以这里只断言工具事件内部一致，不断言跨族相等。
# 函数用途: 核对本次回合在目标插件留下五个生命周期事件（含工具开始/结束）。
def _assert_tool_events_visible(bundle):
    clients = [client for client in bundle.watch.clients
               if client.installation.manifest.plugin_id == bundle.watch.row.manifest.plugin_id]
    assert len(clients) == 1
    events = [event for method, params in clients[0].calls
              if method == 'my-agent/events.observe' for event in params['events']]
    assert {event['type'] for event in events} == {
        'prompt_submitted', 'turn_started', 'turn_ended', 'tool_call_started', 'tool_call_finished'}, (
        bundle.watch.hub.stats(str(bundle.owner.home_dir)))
    tool_events = [event for event in events if event['type'].startswith('tool_call_')]
    assert len(tool_events) == 2 and len({event['thread_ref'] for event in tool_events}) == 1
    assert all(event['thread_ref'] for event in events), '每个事件的会话引用都不许为空'
    _assert_distinct_connections(bundle)


# LLM: 无法审批是宿主结构化事实（这里没人能当场审批），不是模型猜的：客户端不声明 tool_approval 时，
#   插件 ask 必须变成明确拒绝，账本里那条征询的 final_status 要投影成 PLUGIN_GATE_APPROVAL_UNAVAILABLE，
#   否则 B6 的"无法审批"计数会漏掉所有审批阶段才判定的情况。handler 必须零执行。
# 函数用途: 断言非交互请求下 ask 收紧为不可审批、账本终态与计数都对。
def test_rm_rf_confirmation_unavailable_projects_unavailable_final_status(guard_joint, monkeypatch):
    command = 'sh -c "rm -rf build"'
    case = _run_case(guard_joint, monkeypatch, _RunSpec('run_command', {'command': command}, interactive=False))
    _assert_query(case, 'guard-rm', interactive=False)
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.error_code == 'PLUGIN_GATE_APPROVAL_UNAVAILABLE'
    assert case.execution.decision.status == 'deny' and case.permissions == []
    _assert_no_tool_events(guard_joint)

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 1, {'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert (payload['verdict'], payload['final_status']) == ('ask', 'PLUGIN_GATE_APPROVAL_UNAVAILABLE')
    assert payload['reason_code'] == 'RM_RF'
    assert '无法审批：1 次' in text


# LLM: 另一条"无法审批"入口：执行器当时看到可交互（客户端声明了 tool_approval，所以插件收到 ask），
#   但审批消费者实际答不了（返回 unavailable）。这时终态投影只能发生在审批阶段——执行器那次投影看不到，
#   所以这条专门钉 b5fix9b 的 unavailable 承接；漏了它 B6 的计数会漏掉整类"审批时才判定"的情况。
# 函数用途: 断言审批阶段判定不可用时，账本终态仍投影成不可审批。
def test_rm_rf_confirmation_consumer_unavailable_projects_unavailable_final_status(guard_joint, monkeypatch):
    command = 'sh -c "rm -rf build"'
    case = _run_case(guard_joint, monkeypatch,
                     _RunSpec('run_command', {'command': command}, decision='unavailable'))
    _assert_query(case, 'guard-rm')  # 客户端声明了 tool_approval，所以插件看到的是可交互
    assert case.reply['verdict'] == 'ask' and case.reply['reason_code'] == 'RM_RF'
    assert case.execution.result.error_code == 'PLUGIN_GATE_APPROVAL_UNAVAILABLE'
    assert case.execution.decision.status == 'deny'
    _assert_no_tool_events(guard_joint)

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 1, {'runtime_events': rows, 'plugins_info': text}
    payload = json.loads(rows[0]['payload_json'])
    assert (payload['verdict'], payload['final_status']) == ('ask', 'PLUGIN_GATE_APPROVAL_UNAVAILABLE')
    assert '无法审批：1 次' in text


# LLM: 同一 owner 多次征询后，"最近决定"必须新到旧。判据只用结构化事实：库里 seq 递增、B6 按 seq 倒序取，
#   所以最新那条的原因码在渲染文本里必须排在旧的那条之前；不靠时间文案（同秒会撞）。
# 函数用途: 断言同 owner 多行最近决定按新到旧渲染。
def test_recent_decisions_are_rendered_newest_first(guard_joint, monkeypatch):
    older = _run_case(guard_joint, monkeypatch, _RunSpec('apply_patch', {'patch': _DELETE_PATCH}))
    newer = _run_case(guard_joint, monkeypatch,
                      _RunSpec('run_command', {'command': 'sh -c "rm -rf build"'}))
    assert older.reply['reason_code'] == 'DELETE_FILE_BLOCKED'
    assert newer.reply['reason_code'] == 'RM_RF'

    rows, text = _gate_rows(guard_joint), _plugins_info(guard_joint)
    assert len(rows) == 2, {'runtime_events': rows, 'plugins_info': text}
    assert rows[0]['seq'] < rows[1]['seq'], '库里 seq 必须递增（写入顺序）'
    # 离线夹具两轮共用同一个 provider call_id（event-native-call），所以"两次是不同征询"要按门身份判：
    # 去重键含 gate_id，两次命中不同门就必须是两行，不能被合并成一条。
    gate_ids = [json.loads(row['payload_json'])['gate_id'] for row in rows]
    assert sorted(gate_ids) == ['guard-delete', 'guard-rm'], gate_ids

    older_at, newer_at = text.index('原因码 DELETE_FILE_BLOCKED'), text.index('原因码 RM_RF')
    assert newer_at < older_at, f'最近决定要从新到旧；实际文本：{text}'
    assert '最近收紧决定（最多 10 次，新到旧）' in text
