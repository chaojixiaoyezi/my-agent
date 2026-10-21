# LLM: Broker 公开合同测试；临时 owner 夹具不触及真实用户数据，执行仅用显式注入回调。
# 模块用途: 验证插件隔离、脱敏、资产拓扑、遥测以及主动执行的接线。
import pytest

from agent_py_agent.agent.gateway_parts import plugin_broker_api as broker
from agent_py_agent.agent.plugin_host_broker_topics import query_broker_topics


# LLM: 为每例提供规范 owner 路径，不初始化真实宿主。
# 函数用途: 创建两个互不相同的测试用户家目录。
@pytest.fixture
def homes(tmp_path):
    first = tmp_path / 'owners/providers/test/users/alice'
    second = tmp_path / 'owners/providers/test/users/bob'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    return first, second


# LLM: 参数只代表测试已认证身份，请求体不能修改它。
# 函数用途: 以 Alice 的宿主身份调用公开 Broker。
def request(home, action, payload=None, **kwargs):
    return broker.dispatch_plugin_broker_request(
        home, owner_id='providers/test/users/alice', action=action,
        payload=payload, **kwargs,
    )


# LLM: 非法路由必须有结构化错误，不静默当空结果。
# 函数用途: 锁定未知动作拒绝行为。
def test_unknown_action(homes):
    result = request(homes[0], 'not.an.action')
    assert result['ok'] is False
    assert result['error_code'] == 'BROKER_INVALID_REQUEST'


# LLM: 身份攻击在加载任何依赖之前拒绝，admin 也不能跨 owner 写入。
# 函数用途: 覆盖请求体伪造身份与路径的多种形状。
@pytest.mark.parametrize('payload,admin', [
    ({'owner_id': 'providers/test/users/bob'}, False),
    ({'owner_home': '/tmp/another-owner'}, False),
    ({'owner_id': '../bob'}, True),
    ({'owner_id': 'providers/test/users/bob'}, True),
    ({'is_admin': True}, False),
])
def test_scope_spoofing_denied(homes, payload, admin):
    result = request(homes[0], 'assets.save', payload, is_admin=admin)
    assert result['ok'] is False
    assert result['error_code'] == 'BROKER_SCOPE_DENIED'
    assert list(homes[1].iterdir()) == []


# LLM: topics 只读映射不授予执行权限，完整校验应在读取前完成。
# 函数用途: 锁定主题类型和白名单校验。
@pytest.mark.parametrize('topics', [[], 'assets_summary', ['execute.tool'], [['topology']]])
def test_invalid_topics(homes, topics):
    with pytest.raises(ValueError):
        query_broker_topics(homes[0], 'providers/test/users/alice', {'topics': topics})


# LLM: invoker 缺失时无工具执行，不得另起子进程绕过授权。
# 函数用途: 四种主动执行都默认关闭。
@pytest.mark.parametrize('action,payload', [
    ('execute.tool', {'tool_name': 'example', 'arguments': {}}),
    ('execute.script', {'script_id': 's'}),
    ('execute.workflow', {'workflow_id': 'w'}),
    ('execute.workflow_resume', {'workflow_run_id': 'r'}),
])
def test_execution_requires_invoker(homes, action, payload):
    result = request(homes[0], action, payload)
    assert result['ok'] is False
    assert result['error_code'] == 'BROKER_EXECUTION_DENIED'


# LLM: 公共脱敏投影必须递归，且不能改动原对象或吞掉 Token 使用统计。
# 函数用途: 验证秘密键、文本赋值和数字明细同时被正确处理。
def test_public_projection():
    original = {'nested': [{'api_key': 'private-value', 'Cookie': 'session-private'}],
                'code': 'password = "private-value"', 'token_usage': {'total_tokens': 17},
                'credential_refs': [{'credential_ref': 'ref-1'}]}
    result = broker.public_payload(original)
    assert 'private-value' not in str(result)
    assert 'session-private' not in str(result)
    assert result['token_usage']['total_tokens'] == 17
    assert result['credential_refs'][0]['credential_ref'] == 'ref-1'
    assert original['nested'][0]['api_key'] == 'private-value'


# LLM: 请求与宿主传入值必须保持边界；非法请求不能回显秘密异常。
# 函数用途: 验证不合法请求体类型有稳定错误信封。
@pytest.mark.parametrize('payload', [[], 'password=private-value', 7])
def test_invalid_payload(homes, payload):
    result = request(homes[0], 'assets.list', payload)
    assert result['ok'] is False
    assert result['error_code'] == 'BROKER_INVALID_REQUEST'
    assert 'private-value' not in str(result)


# LLM: 公共接口创建真实资产，所有输入均在临时 owner 内。
# 函数用途: 保存用于后续拓扑测试的脚本和工作流。
def seed_assets(home):
    descriptor = {'id': 's', 'kind': 'script', 'name': '采集',
                  'data_sources': [{'source_id': 'logs', 'system': 'example',
                                    'query_scope': {'active_appnames': ['A']}}]}
    result = request(home, 'assets.save', {'kind': 'script', 'descriptor': descriptor,
                                         'code': 'print("{}")'})
    assert result['ok'], result
    result = request(home, 'assets.save', {'kind': 'workflow', 'descriptor': {
        'id': 'w', 'kind': 'workflow', 'name': '分析'}, 'steps': []})
    assert result['ok'], result


# LLM: 保存、读取、热更新、绑定、列表与拓扑必须共享真实权威存储。
# 函数用途: 覆盖四种资产标准 key、反向引用和根子图。
def test_assets_end_to_end(homes):
    home = homes[0]
    seed_assets(home)
    skill = home / 'skills/k/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: 分析技能\ndescription: 研判\n---\n技能正文\n')
    result = request(home, 'assets.bind', {'kind': 'workflow', 'id': 'w', 'steps': [
        {'step_id': 'collect', 'name': '采集', 'mode': 'deterministic_script', 'script_id': 's'},
        {'step_id': 'judge', 'name': '研判', 'mode': 'llm_skill_analysis', 'skill_ids': ['k']},
    ]})
    assert result['ok'], result
    result = request(home, 'assets.bind', {'kind': 'monitor', 'id': 'm',
                                         'name': '监控', 'workflow_ids': ['w']})
    assert result['ok'], result
    updated = request(home, 'assets.update_sources', {'kind': 'script', 'id': 's',
        'source_id': 'logs', 'query_scope': {'active_appnames': ['A', 'B']},
        'candidate': {'candidate_id': 'C', 'decision': 'ignored', 'evaluated_at': '2026-10-21T00:00:00Z',
                      'reason': '没有新数据'}})
    assert updated['ok'], updated
    detail = request(home, 'assets.get', {'kind': 'script', 'id': 's'})['data']
    assert detail['code'] == 'print("{}")'
    assert detail['used_by']['workflows'] == ['w']
    assert detail['data_sources'][0]['query_scope']['active_appnames'] == ['A', 'B']
    assert detail['data_sources'][0]['evaluated_candidates'][0]['decision'] == 'ignored'
    for key in ('access_targets', 'credential_refs', 'io_contract'):
        assert key in detail
    topology = request(home, 'assets.topology', {'root': 'monitor:m'})['data']
    assert {(edge['from'], edge['to']) for edge in topology['edges']} == {
        ('monitor:m', 'workflow:w'), ('workflow:w', 'script:s'), ('workflow:w', 'skill:k')}
    rows = request(home, 'assets.list', {'kind': 'all'})['data']
    assert {row['kind'] for row in rows} == {'monitor', 'workflow', 'script', 'skill'}
    assert request(home, 'assets.get', {'kind': 'skill', 'id': 'k'})['data']['body_markdown'].endswith('技能正文\n')
    assert len(request(home, 'assets.list', {'kind': 'workflow'})['data']) == 1
    assert request(home, 'assets.get', {'kind': 'monitor', 'id': 'm'})['data']['refs']['workflows'] == ['w']


# LLM: 先通过公开 Broker 调用真实 ledger，再从查询路由验证关联和耗时。
# 函数用途: 成功工具执行必须记账并在结果和账本双重脱敏。
def test_execute_tool_and_ledger(homes):
    observed = []

    # LLM: 代表宿主授权边界的测试回调，不启动外部程序。
    # 函数用途: 记录真实传入参数并返回带秘密的测试结果。
    def invoke(name, arguments):
        observed.append((name, arguments))
        return {'ok': True, 'answer': 42, 'authorization': 'private-output'}

    service = broker.PluginBrokerService(homes[0], owner_id='providers/test/users/alice',
        trusted_task_id='task-a', tool_invoker=invoke)
    result = service.dispatch('execute.tool', {'tool_name': 'calculator',
        'arguments': {'x': 1, 'password': 'private-input'}, 'task_id': 'task-a'})
    assert result['ok'], result
    data = result['data']
    assert data['result']['answer'] == 42
    assert observed == [('calculator', {'x': 1, 'password': 'private-input'})]
    assert 'private-output' not in str(result)
    assert data['ended_at'] >= data['started_at'] and data['duration_ms'] >= 0
    detail = request(homes[0], 'telemetry.tool_calls.get', {'call_id': data['call_id']})
    assert detail['ok'], detail
    assert detail['data']['task_id'] == 'task-a'
    assert detail['data']['status'] == 'ok'
    assert 'private-input' not in str(detail)
    listed = request(homes[0], 'telemetry.tool_calls.list', {'task_id': 'task-a',
        'tool_name': 'calculator', 'min_duration_ms': 0, 'limit': 1, 'cursor': 0})
    assert listed['ok'], listed
    assert data['call_id'] in str(listed['data'])
    stats = request(homes[0], 'telemetry.tool_stats')
    assert stats['ok'] and 'calculator' in str(stats['data'])


# LLM: 回调错误须记录实际调用状态，异常文本不得泄露给插件。
# 函数用途: 覆盖权限拒绝、超时和普通失败的账本状态。
@pytest.mark.parametrize('error,status', [(PermissionError, 'denied'), (TimeoutError, 'timeout'),
                                         (RuntimeError, 'error')])
def test_execution_error_ledger(homes, error, status):
    # LLM: 模拟授权或执行层抛错，测试错误归类而非外部工具可用性。
    # 函数用途: 产生带秘密文本的执行失败。
    def invoke(name, arguments):
        raise error('password=private-value')

    result = request(homes[0], 'execute.tool', {'tool_name': 'broken', 'arguments': {}}, tool_Invoker=invoke)
    assert result['ok'], result
    assert result['data']['status'] == status
    assert result['data']['result']['ok'] is False
    assert 'private-value' not in str(result)
    detail = request(homes[0], 'telemetry.tool_calls.get', {'call_id': result['data']['call_id']})
    assert detail['data']['status'] == status


# LLM: 脚本和工作流的执行始终穿过原授权 invoker，不另建执行旁路。
# 函数用途: 验证执行参数映射，保留一次性数据源覆盖和恢复断点。
@pytest.mark.parametrize('action,payload,name,arguments', [
    ('execute.script', {'script_id': 's', 'inputs': {'x': 1}, 'override_data_sources': []},
     'scripts', {'action': 'run', 'script_id': 's', 'inputs': {'x': 1}, 'override_data_sources': []}),
    ('execute.workflow', {'workflow_id': 'w', 'inputs': {}},
     'workflows', {'action': 'run', 'workflow_id': 'w', 'inputs': {}}),
    ('execute.workflow_resume', {'workflow_run_id': 'r', 'from_step_id': 'step'},
     'workflows', {'action': 'resume', 'workflow_run_id': 'r', 'from_step_id': 'step'}),
])
def test_execution_mapping(homes, action, payload, name, arguments):
    observed = []

    # LLM: 只验证 Broker 到受控调用方的协议，无外部执行副作用。
    # 函数用途: 接收映射后参数。
    def invoke(tool, args):
        observed.append((tool, args))
        return {'ok': True}

    result = request(homes[0], action, payload, tool_Invoker=invoke)
    assert result['ok'], result
    assert observed == [(name, arguments)]


# LLM: 普通读取固定当前 owner，管理员只有显式只读审计可切换。
# 函数用途: 验证真实两个用户的资产完全隔离。
def test_owner_admin_read_scope(homes):
    seed_assets(homes[0])
    result = broker.dispatch_plugin_broker_request(homes[1], owner_id='providers/test/users/bob',
                                                   action='assets.list')
    assert result['ok'] and result['data'] == []
    result = request(homes[0], 'assets.list', {'owner_id': 'providers/test/users/bob'}, is_admin=True)
    assert result['ok'] and result['data'] == []
    assert len(request(homes[0], 'assets.list')['data']) == 2


# LLM: 所有公开主题须确实接入对应服务，且没有将空结果误作成功的替身。
# 函数用途: 验证组合主题响应和身份注入拒绝。
def test_topics_real_services(homes):
    seed_assets(homes[0])
    topics = ['assets_summary', 'topology', 'recent_tasks', 'recent_tool_calls', 'tool_stats', 'workflow_runs']
    result = query_broker_topics(homes[0], 'providers/test/users/alice', {'topics': topics})
    assert set(result) == set(topics)
    assert all(row['ok'] for row in result.values()), result
    assert len(result['assets_summary']['data']) == 2
    with pytest.raises(ValueError):
        query_broker_topics(homes[0], 'providers/test/users/alice', {'topics': topics, 'owner_id': 'bob'})


# LLM: 宿主返回的业务 JSON 与传输成功不同；不能把暂停和失败记成成功。
# 函数用途: 验证真实 ToolHandlerOutcome 的业务结果解包。
def test_tool_handler_outcome_failure(homes):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    result = request(homes[0], 'execute.tool', {'tool_name': 'workflows', 'arguments': {}},
        tool_Invoker=lambda name, args: ToolHandlerOutcome(
            tool=name, ok=True, output='{"status":"paused_for_resume","password":"private-value"}'))
    assert result['ok'], result
    assert result['data']['status'] == 'error'
    assert result['data']['result']['status'] == 'paused_for_resume'
    assert 'private-value' not in str(result)


# LLM: 身份冲突按新合同裁决，但宿主路径与管理员权限仍必须拒绝，invoker承担其余审批。
# 函数用途: 保证替代执行入口不能覆盖宿主目录或管理员权限。
@pytest.mark.parametrize('field', ['owner_home', 'target_owner_home', 'is_admin', 'admin'])
def test_execution_rejects_identity_arguments(homes, field):
    calls = []
    result = request(homes[0], 'execute.tool', {'tool_name': 'scripts',
        'arguments': {field: 'forged', 'action': 'run'}},
        tool_Invoker=lambda name, args: calls.append(name))
    assert result['ok'] is False
    assert result['error_code'] == 'BROKER_SCOPE_DENIED'
    assert calls == []


# LLM: 授权撤销必须在请求时重新验证，不可用构造时的旧 grant 继续读取。
# 函数用途: 覆盖 grant 绑定身份与后续失效。
def test_revoked_grant(homes):
    from types import SimpleNamespace
    owner = SimpleNamespace(home_dir=homes[0], owner_id='providers/test/users/alice')
    reference = SimpleNamespace(require=lambda: None, owner=lambda: owner)
    service = broker.PluginBrokerService.from_grant(SimpleNamespace(activation_ref=reference))
    assert service.owner_id == owner.owner_id
    # LLM: 模拟激活 require 的实际失效异常，不修改宿主插件安装表。
    # 函数用途: 让旧 grant 立即失效。
    def revoked():
        raise ValueError('activation revoked')
    reference.require = revoked
    result = service.dispatch('assets.list')
    assert result == {'ok': False, 'error_code': 'BROKER_SCOPE_DENIED'}


# LLM: 执行后账本写失败不能抹掉业务结果，否则调用方重试会重复副作用。
# 函数用途: 测试记账失败显式回报与单次执行。
@pytest.mark.parametrize('failure', [OSError, RuntimeError])
def test_execution_preserves_result_on_ledger_failure(homes, monkeypatch, failure):
    from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
    calls = []
    # LLM: 模拟磁盘满，仅影响本测试账本落盘。
    # 函数用途: 触发真实执行之后的持久化失败分支。
    def broken(self, record):
        raise failure('disk full')
    monkeypatch.setattr(ToolTelemetryLedger, 'record_tool_call', broken)
    result = request(homes[0], 'execute.tool', {'tool_name': 'once'},
                     tool_Invoker=lambda name, args: calls.append(name) or {'ok': True, 'value': 7})
    assert calls == ['once']
    assert result['data']['result']['value'] == 7
    assert result['data']['telemetry_persisted'] is False


# LLM: 未认可的条件必须拒绝，不能悄悄丢掉后返回全量任务或工具。
# 函数用途: 检查查询拼错字段、分页上下界及 malformed 参数。
@pytest.mark.parametrize('action,payload', [
    ('telemetry.tool_calls.list', {'typo_task_id': 'a'}),
    ('telemetry.tasks.list', {'typo_status': 'failed'}),
    ('telemetry.tool_calls.list', {'limit': -1}),
    ('telemetry.tool_calls.list', {'limit': 1001}),
    ('telemetry.tool_calls.list', {'cursor': -1}),
    ('telemetry.tool_calls.list', {'limit': True}),
])
def test_invalid_telemetry_filters(homes, action, payload):
    result = request(homes[0], action, payload)
    assert result == {'ok': False, 'error_code': 'BROKER_INVALID_REQUEST'}


# LLM: 名称/ID允许来自元数据，正文位置必须沿相同发现顺序核验，不能把ID拼成目录。
# 函数用途: 验证嵌套目录和自定义ID的技能正文，避免读错同名技能。
def test_skill_metadata_id_and_nested_body(homes):
    path = homes[0] / 'skills/learned/different-folder/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: canonical-id\nname: test\n---\nexact-body\n')
    result = request(homes[0], 'assets.get', {'kind': 'skill', 'id': 'canonical-id'})
    assert result['ok'], result
    assert result['data']['body_markdown'].endswith('exact-body\n')


# LLM: 有效旧版 read grant 不是资产写授权，不得把可查看升级为可保存代码。
# 函数用途: 验证 grant 只读范围不被Broker扩张。
def test_read_grant_cannot_mutate(homes):
    from types import SimpleNamespace
    owner = SimpleNamespace(home_dir=homes[0], owner_id='providers/test/users/alice')
    reference = SimpleNamespace(require=lambda: None, owner=lambda: owner)
    service = broker.PluginBrokerService.from_grant(SimpleNamespace(activation_ref=reference))
    result = service.dispatch('assets.save', {'kind': 'script', 'descriptor': {
        'id': 'forbidden', 'kind': 'script', 'name': 'forbidden'}, 'code': 'print(1)'})
    assert result == {'ok': False, 'error_code': 'BROKER_SCOPE_DENIED'}
    assert not (homes[0] / 'scripts/forbidden').exists()


# LLM: 完整任务详情需要主子树、轮次、Token、资产和工具时间线同源。
# 函数用途: 创建真实任务记录，逐项核对列表分页和详情。
def test_task_telemetry_full_detail(homes):
    from agent_py_agent.agent.observability.task_call_inspector import TaskCallInspector
    inspector = TaskCallInspector(homes[0], owner_id='providers/test/users/alice')
    inspector.upsert_task_call({'task_id': 'task-a', 'trigger_kind': 'plugin_api', 'started_at': 1,
        'runs': [{'run_id': 'root', 'parent_run_id': ''}, {'run_id': 'child', 'parent_run_id': 'root'}],
        'mounted_assets': {'script_ids': ['s']}, 'artifacts': ['artifact-a']})
    inspector.append_round('task-a', {'run_id': 'root', 'duration_ms': 3, 'token_usage': {
        'prompt_tokens': 11, 'completion_tokens': 4, 'cached_tokens': 2}})
    inspector.complete_task_call('task-a', 'succeeded', ended_at=2)
    inspector.upsert_task_call({'task_id': 'task-b', 'started_at': 2})
    service = broker.PluginBrokerService(homes[0], owner_id='providers/test/users/alice',
        trusted_task_id='task-a', trusted_run_id='child', tool_invoker=lambda name, args: {'ok': True})
    executed = service.dispatch('execute.tool', {'tool_name': 'sample', 'task_id': 'task-a', 'run_id': 'child'})
    assert executed['ok'], executed
    page = request(homes[0], 'telemetry.tasks.list', {'limit': 1, 'cursor': 1})
    assert page['ok'], page
    assert page['data'][0]['task_id'] == 'task-b'
    detail = request(homes[0], 'telemetry.tasks.get', {'task_id': 'task-a'})['data']
    assert len(detail['runs']) == 2 and len(detail['rounds']) == 1
    assert detail['token_usage'] == {'prompt_tokens': 11, 'completion_tokens': 4, 'cached_tokens': 2, 'total_tokens': 15}
    assert detail['artifacts'] == ['artifact-a']
    assert detail['mounted_assets']['script_ids'] == ['s']
    assert detail['tool_calls'][0]['call_id'] == executed['data']['call_id']
    assert detail['duration_ms'] == 1000


# LLM: 仅此测试受控允许临时owner的两种工具，使用真实runner/engine，不代表外部HTTP或宿主审批联调。
# 函数用途: 真实脚本、工作流失败断点和续跑通过Broker完整走通。
def test_real_script_workflow_and_resume(homes):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    script_tool, workflow_tool = ScriptsTool(homes[0]), WorkflowsTool(homes[0])
    # LLM: 默认工具已提供分析摘要；显式注入失败确保此测试仍覆盖真实失败和恢复，而非依赖缺省实现。
    # 函数用途: 在分析步制造确定的失败，保留前序真实脚本执行快照。
    def failing_analysis(step, outputs, skills):
        raise RuntimeError('intentional analysis failure')
    workflow_tool._engine().analysis_handler = failing_analysis
    # LLM: 该测试授权只覆盖两个绑定临时owner的工具，不支持任意命令或跨用户。
    # 函数用途: 作为真实工具与Broker之间的受控调用器。
    def invoke(name, arguments):
        return {'scripts': script_tool, 'workflows': workflow_tool}[name].execute(arguments)
    code = 'import json, os\nprint(json.dumps({"sources": json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])}))'
    saved = request(homes[0], 'assets.save', {'kind': 'script', 'descriptor': {
        'id': 's', 'kind': 'script', 'name': '真实脚本'}, 'code': code})
    assert saved['ok'], saved
    result = request(homes[0], 'execute.script', {'script_id': 's', 'override_data_sources': {'active': ['B']}},
                     tool_Invoker=invoke)
    assert result['ok'] and result['data']['status'] == 'ok', result
    assert result['data']['result']['output_data']['sources'] == {'active': ['B']}
    assert request(homes[0], 'assets.get', {'kind': 'script', 'id': 's'})['data']['data_sources'] == []
    saved = request(homes[0], 'assets.save', {'kind': 'workflow', 'descriptor': {
        'id': 'w', 'kind': 'workflow', 'name': '真实工作流'}, 'steps': [
        {'step_id': 'collect', 'name': '采集', 'mode': 'deterministic_script', 'script_id': 's'},
        {'step_id': 'review', 'name': '研判', 'mode': 'llm_skill_analysis'}]})
    assert saved['ok'], saved
    failed = request(homes[0], 'execute.workflow', {'workflow_id': 'w'}, tool_Invoker=invoke)
    assert failed['ok'] and failed['data']['status'] == 'error', failed
    run_id = failed['data']['result']['workflow_run_id']
    calls = request(homes[0], 'telemetry.tool_calls.list', {'workflow_run_id': run_id})
    assert calls['ok'] and calls['data'][0]['call_id'] == failed['data']['call_id'], calls
    first = request(homes[0], 'telemetry.workflows.runs.get', {'workflow_run_id': run_id})['data']
    assert first['steps'][0]['status'] == 'succeeded'
    assert first['failed_step_id'] == 'review'
    workflow_tool._engine().analysis_handler = lambda step, outputs, skills: {'reviewed': True}
    resumed = request(homes[0], 'execute.workflow_resume', {'workflow_run_id': run_id}, tool_Invoker=invoke)
    assert resumed['data']['status'] == 'ok', resumed
    final = request(homes[0], 'telemetry.workflows.runs.get', {'workflow_run_id': run_id})['data']
    assert final['status'] == 'succeeded' and final['steps'][1]['llm_involved'] is True
    assert final['steps'][0] == first['steps'][0]
    listed = request(homes[0], 'telemetry.workflows.runs.list', {'workflow_id': 'w', 'status': 'succeeded'})
    assert len(listed['data']) == 1 and listed['data'][0]['workflow_run_id'] == run_id


# LLM: 缺失引用可展示在拓扑，但不能冒充已存在资产；get与list应语义一致。
# 函数用途: 验证悬空依赖保留在图而不污染可用资产列表。
def test_dangling_assets(homes):
    result = request(homes[0], 'assets.save', {'kind': 'workflow', 'descriptor': {
        'id': 'w', 'kind': 'workflow', 'name': 'w'}, 'steps': [
        {'step_id': 'a', 'name': 'a', 'mode': 'deterministic_script', 'script_id': 'absent'}]})
    assert result['ok'], result
    graph = request(homes[0], 'assets.topology')['data']
    assert any(row.get('missing') for row in graph['nodes'])
    assert request(homes[0], 'assets.list', {'kind': 'script'}) == {'ok': True, 'data': []}
    assert request(homes[0], 'assets.get', {'kind': 'script', 'id': 'absent'}) == {'ok': True, 'data': None}


# LLM: 动态绑定只修改显式字段，不能丢弃既有调度、名称和其他类型引用。
# 函数用途: 验证已有监控再次绑定工作流不清空其脚本和调度设置。
def test_monitor_bind_preserves_unmentioned_fields(homes):
    first = request(homes[0], 'assets.save', {'kind': 'monitor', 'id': 'm', 'name': 'original',
        'script_ids': ['s'], 'schedule_spec': {'kind': 'every', 'expression': '60', 'enabled': True}})
    assert first['ok'], first
    bound = request(homes[0], 'assets.bind', {'kind': 'monitor', 'id': 'm', 'workflow_ids': ['w']})
    assert bound['ok'], bound
    result = request(homes[0], 'assets.get', {'kind': 'monitor', 'id': 'm'})['data']
    assert result['name'] == 'original' and result['refs']['scripts'] == ['s']
    assert result['refs']['workflows'] == ['w'] and result['schedule_spec']['enabled'] is True


# LLM: 工具失败的真实错误码、拒绝/超时分类必须保留在账本，不能一律归为未知error。
# 函数用途: 核对结构化业务失败和工具传输失败的账本状态。
@pytest.mark.parametrize('status', ['denied', 'timeout'])
def test_structured_execution_failure_status(homes, status):
    result = request(homes[0], 'execute.tool', {'tool_name': 'sample'}, tool_Invoker=lambda name, args: {
        'ok': False, 'status': status, 'error_code': 'SPECIFIC_' + status.upper()})
    assert result['data']['status'] == status
    call = request(homes[0], 'telemetry.tool_calls.get', {'call_id': result['data']['call_id']})['data']
    assert call['error_code'] == 'SPECIFIC_' + status.upper()


# LLM: script工具调用总计不能代替每个固化脚本统计；聚合复用底层同一百分位算法。
# 函数用途: 验证按脚本ID展示调用次数、成功率和耗时。
def test_per_script_statistics(homes):
    from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
    ledger = ToolTelemetryLedger(homes[0], owner_id='providers/test/users/alice')
    for index, (script, duration, status) in enumerate([('s', 10, 'ok'), ('s', 30, 'error'), ('other', 90, 'ok')]):
        ledger.record_tool_call({'call_id': str(index), 'tool_name': 'scripts', 'started_at': 1,
            'ended_at': 2, 'duration_ms': duration, 'status': status, 'args_summary': {'action': 'run', 'script_id': script}})
    stats = request(homes[0], 'telemetry.tool_stats')['data']
    assert stats['tools']['scripts']['calls'] == 3
    assert stats['scripts']['s']['calls'] == 2 and stats['scripts']['s']['success_rate'] == .5
    assert stats['scripts']['s']['avg_duration_ms'] == 20 and stats['scripts']['s']['p95_duration_ms'] == 30


# LLM: 两种绑定形式必须明确互斥，不允许返回成功却只做其中一部分。
# 函数用途: 检查混合steps与skill_id请求在任何保存前拒绝。
def test_mixed_binding_rejected(homes):
    seed_assets(homes[0])
    assert request(homes[0], 'assets.bind', {'kind': 'workflow', 'id': 'w', 'steps': [
        {'step_id': 'a', 'name': 'a', 'mode': 'hybrid', 'script_id': 's'}]})['ok']
    result = request(homes[0], 'assets.bind', {'kind': 'workflow', 'id': 'w',
        'step_id': 'a', 'skill_id': 'k', 'steps': []})
    assert result == {'ok': False, 'error_code': 'BROKER_INVALID_REQUEST'}
    workflow = request(homes[0], 'assets.get', {'kind': 'workflow', 'id': 'w'})
    assert workflow['data']['steps'][0]['skill_ids'] == []


# LLM: 同一进程处理两个Owner仍只绑定各自账本，禁止普通身份借正文选择目标。
# 函数用途: 核对遥测记录实际写入和读出都不串Owner。
def test_telemetry_owner_isolation(homes):
    first = request(homes[0], 'execute.tool', {'tool_name': 'sample'}, tool_Invoker=lambda name, args: {'ok': True})
    assert first['data']['telemetry_persisted'] is True
    own = broker.dispatch_plugin_broker_request(homes[1], owner_id='providers/test/users/bob',
        action='telemetry.tool_calls.list')
    assert own == {'ok': True, 'data': []}
    foreign = broker.dispatch_plugin_broker_request(homes[1], owner_id='providers/test/users/bob',
        action='telemetry.tool_calls.get', payload={'call_id': first['data']['call_id']})
    assert foreign == {'ok': True, 'data': None}


# LLM: Store路径规则与Broker一起实测；链接拒绝而不是跟随到另一Owner目录。
# 函数用途: 验证资产非法ID与遥测目录链接不会越域读写。
def test_path_and_symlink_boundary(homes):
    result = request(homes[0], 'assets.save', {'kind': 'script', 'code': 'print(1)', 'descriptor': {
        'id': '../bob', 'kind': 'script', 'name': 'bad'}})
    assert result == {'ok': False, 'error_code': 'BROKER_INVALID_REQUEST'}
    (homes[0] / 'data').symlink_to(homes[1], target_is_directory=True)
    assert request(homes[0], 'telemetry.tool_calls.list')['ok'] is False
    executed = request(homes[0], 'execute.tool', {'tool_name': 'sample'},
                       tool_Invoker=lambda name, args: {'ok': True})
    assert executed['data']['telemetry_persisted'] is False
    assert list(homes[1].iterdir()) == []


# LLM: 临时授权只用于测试，权限和可信身份均由宿主夹具构造，不来自请求正文。
# 函数用途: 生成具有可复验引用的外部插件授权。
def source_grant(home, permissions=('read',), **context):
    from types import SimpleNamespace
    owner = SimpleNamespace(home_dir=home, owner_id='providers/test/users/alice')
    reference = SimpleNamespace(require=lambda: None, owner=lambda: owner)
    return SimpleNamespace(activation_ref=reference, activation={'host_api': permissions, **context})


# LLM: 公开详情、列表与拓扑不得携带真实宿主路径；权威存储仍应保留自己的路径。
# 函数用途: 用真实脚本与Skill验证路径投影没有修改源数据。
def test_asset_public_paths(homes):
    from agent_py_agent.agent.assets.script_store import ScriptStore
    seed_assets(homes[0])
    skill = homes[0] / 'skills/private/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: private\n---\nprivate body\n')
    for action, payload in [('assets.get', {'kind': 'script', 'id': 's'}),
                            ('assets.get', {'kind': 'skill', 'id': 'private'}),
                            ('assets.list', {}), ('assets.topology', {})]:
        result = request(homes[0], action, payload)
        assert result['ok'], result
        assert 'entrypoint_path' not in str(result)
        assert str(homes[0]) not in str(result)
    assert ScriptStore(homes[0]).get_script('s')['entrypoint_path'].startswith(str(homes[0]))


# LLM: grant只读时隐藏源码，但拓扑和标准描述符保留；显式源码权限与内部入口可读脱敏正文。
# 函数用途: 覆盖脚本、技能、工作流的权限矩阵及参数提权拒绝。
@pytest.mark.parametrize('permissions,allowed', [(('read',), False), ((), False),
    (('read', 'assets.source'), True), (('write',), True), (('execute',), True)])
def test_grant_asset_source_permissions(homes, permissions, allowed):
    seed_assets(homes[0])
    skill = homes[0] / 'skills/k/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: k\n---\nsource body\n')
    service = broker.PluginBrokerService.from_grant(source_grant(homes[0], permissions))
    for kind, identifier, field in [('script', 's', 'code'), ('skill', 'k', 'body_markdown'),
                                    ('workflow', 'w', 'body_markdown')]:
        detail = service.dispatch('assets.get', {'kind': kind, 'id': identifier,
            'include_asset_source': True, 'host_api': ['assets.source']})['data']
        assert detail['id'] == identifier and detail['kind'] == kind
        assert all(key in detail for key in ('refs', 'data_sources', 'access_targets', 'io_contract'))
        if kind == 'workflow':
            assert detail['descriptor']['id'] == 'w' and detail['steps'] == []
        if allowed:
            assert not detail.get('source_redacted', False)
            assert detail[field] == request(homes[0], 'assets.get', {'kind': kind, 'id': identifier})['data'][field]
        else:
            assert detail[field] == '' and detail['source_redacted'] is True


# LLM: 直接主题读取也必须执行同一源码权限，不能绕开Service投影。
# 函数用途: 锁定query_assets的授权入参与相对路径结果。
def test_query_assets_grant_projection(homes):
    from agent_py_agent.agent.plugin_host_broker_topics import query_assets
    seed_assets(homes[0])
    grant = source_grant(homes[0])
    result = query_assets(homes[0], 'providers/test/users/alice', 'assets.get',
                          {'kind': 'script', 'id': 's'}, grant=grant)
    assert result['code'] == '' and result['source_redacted'] is True
    assert 'entrypoint_path' not in result


# LLM: 空可信上下文也不能从请求补身份；全部execute动作使用宿主绑定值并记入真实账本。
# 函数用途: 验证冲突裁决、返回告警、输入不变与实际调用不串身份。
@pytest.mark.parametrize('action,params', [('execute.tool', {'tool_name': 'sample', 'arguments': {}}),
    ('execute.script', {'script_id': 's'}), ('execute.workflow', {'workflow_id': 'w'}),
    ('execute.workflow_resume', {'workflow_run_id': 'wr'})])
@pytest.mark.parametrize('context', [{}, {'run_id': 'trusted-run', 'task_id': 'trusted-task'}])
def test_execution_trusted_scope(homes, action, params, context):
    from copy import deepcopy
    calls = []
    grant = source_grant(homes[0], ('execute',), **context)
    service = broker.PluginBrokerService.from_grant(grant, tool_invoker=lambda name, args: calls.append((name, args)))
    payload = {**params, 'owner_id': 'other', 'run_id': 'other-run', 'task_id': 'other-task'}
    original = deepcopy(payload)
    result = service.dispatch(action, payload)
    assert result['ok'], result
    assert len(calls) == 1 and payload == original
    expected = {'owner_id': service.owner_id, 'run_id': context.get('run_id', ''), 'task_id': context.get('task_id', '')}
    assert result['data']['scope_resolution'] == expected
    assert len(result['data']['scope_warnings']) == 3
    detail = service.dispatch('telemetry.tool_calls.get', {'call_id': result['data']['call_id']})['data']
    assert {key: detail[key] for key in expected} == expected


# LLM: 嵌套参数同样不能伪造归属；宿主构造值胜过外部声明且不修改原请求。
# 函数用途: 验证直接构造的可信上下文和arguments冲突裁决。
def test_execution_nested_trusted_scope(homes):
    calls = []
    service = broker.PluginBrokerService(homes[0], owner_id='alice', trusted_run_id='run-a',
        trusted_task_id='task-a', tool_invoker=lambda name, args: calls.append(args))
    arguments = {'owner_id': 'bob', 'run_id': 'run-b', 'task_id': 'task-b', 'x': 1}
    result = service.dispatch('execute.tool', {'tool_name': 'sample', 'arguments': arguments})
    assert result['ok'], result
    assert calls == [{'owner_id': 'alice', 'run_id': 'run-a', 'task_id': 'task-a', 'x': 1}]
    assert result['data']['scope_resolution'] == {key: calls[0][key] for key in ('owner_id', 'run_id', 'task_id')}
    assert len(result['data']['scope_warnings']) == 3
    assert arguments['owner_id'] == 'bob'


# LLM: 使用真实严格Schema和真实引擎，不能用任意字典回调假定身份裁决后仍可执行。
# 函数用途: 验证直接工具执行的身份控制字段不破坏脚本或工作流业务参数。
@pytest.mark.parametrize('tool_name', ['scripts', 'workflows'])
@pytest.mark.parametrize('conflict', [True, False])
def test_real_tool_nested_identity_fields(homes, tool_name, conflict):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    seed_assets(homes[0])
    tools = {'scripts': ScriptsTool(homes[0]), 'workflows': WorkflowsTool(homes[0])}
    service = broker.PluginBrokerService(homes[0], owner_id='providers/test/users/alice',
        trusted_run_id='r', trusted_task_id='t', tool_invoker=lambda name, args: tools[name].execute(args))
    expected = {'owner_id': service.owner_id, 'run_id': 'r', 'task_id': 't'}
    arguments = {'action': 'run', **({'script_id': 's'} if tool_name == 'scripts' else {'workflow_id': 'w'}),
                 **{key: 'foreign' if conflict else value for key, value in expected.items()}}
    result = service.dispatch('execute.tool', {'tool_name': tool_name, 'arguments': arguments})
    assert result['ok'] and result['data']['status'] == 'ok', result
    assert result['data']['scope_resolution'] == expected
    assert len(result['data']['scope_warnings']) == (3 if conflict else 0)
    call = service.dispatch('telemetry.tool_calls.get', {'call_id': result['data']['call_id']})['data']
    assert {key: call[key] for key in expected} == expected
    assert not set(expected).intersection(call['args_summary'])


# LLM: 引用允许嵌套结构，域外或含..的绝对路径不得假装位于Owner内，也不修改原记录。
# 函数用途: 核对路径相对化、外部遮蔽和嵌套源码投影的精确值。
def test_asset_projection_nested_paths(homes):
    from agent_py_agent.agent.plugin_host_broker_topics import project_asset
    original = {'entrypoint_path': str(homes[0] / 'main.py'), 'refs': {
        'skill_path': str(homes[0] / 'skills/k/SKILL.md'),
        'manifest_path': str(homes[0] / '../outside/manifest.json'),
        'nested': [str(homes[0] / 'scripts/s/script.json'), 'script:s']},
        'definition': {'body_markdown': 'private body', 'steps': [{'step_id': 'a'}]}}
    result = project_asset(original, homes[0], False)
    assert 'entrypoint_path' not in result
    assert result['refs'] == {'skill_path': 'skills/k/SKILL.md', 'manifest_path': '[external-path]',
                              'nested': ['scripts/s/script.json', 'script:s']}
    assert result['definition'] == {'body_markdown': '', 'source_redacted': True, 'steps': [{'step_id': 'a'}]}
    assert original['definition']['body_markdown'] == 'private body'


# LLM: 源码权限必须是显式集合且每次查询仍受Grant当前声明约束。
# 函数用途: 验证畸形权限不提权，原高权限Grant降级立即隐藏代码。
@pytest.mark.parametrize('permissions', ['assets.source', None, {'assets.source': True}])
def test_source_permission_fail_closed(homes, permissions):
    seed_assets(homes[0])
    grant = source_grant(homes[0], ('read', 'assets.source'))
    service = broker.PluginBrokerService.from_grant(grant)
    assert service.dispatch('assets.get', {'kind': 'script', 'id': 's'})['data']['code']
    grant.activation['host_api'] = permissions
    result = service.dispatch('assets.get', {'kind': 'script', 'id': 's'})['data']
    assert result['code'] == '' and result['source_redacted'] is True


# LLM: Broker只调用明确授权的本地工具；实际脚本失败与补丁恢复贯穿同一运行账本。
# 函数用途: 验证新恢复参数经Broker透传后完成真实工作流修复。
def test_broker_patched_workflow_resume(homes):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    scripts, workflows = ScriptsTool(homes[0]), WorkflowsTool(homes[0])
    # LLM: 测试invoker仅映射两种固定工具，不授予任意工具或跨Owner权限。
    # 函数用途: 让Broker进入真实工作流工具执行入口。
    def invoke(name, arguments):
        return {'scripts': scripts, 'workflows': workflows}[name].execute(arguments)
    service = broker.PluginBrokerService(homes[0], owner_id='providers/test/users/alice', tool_invoker=invoke)
    assert service.dispatch('assets.save', {'kind': 'script', 'descriptor': {'id': 'bad', 'kind': 'script',
        'name': '失败脚本', 'io_contract': {'outputs': ['value']}}, 'code': 'raise SystemExit(4)'})['ok']
    assert service.dispatch('assets.save', {'kind': 'workflow', 'descriptor': {'id': 'repair', 'kind': 'workflow', 'name': '修复'},
        'steps': [{'step_id': 'bad', 'name': '失败', 'mode': 'deterministic_script', 'script_id': 'bad'}]})['ok']
    paused = service.dispatch('execute.workflow', {'workflow_id': 'repair'})['data']['result']
    assert paused['status'] == 'paused_for_resume'
    request = {'workflow_run_id': paused['workflow_run_id'], 'patched_step_output': {'value': 14}, 'updated_data_sources': []}
    final = service.dispatch('execute.workflow_resume', request)
    assert final['ok'] and final['data']['status'] == 'ok'
    assert final['data']['result']['final_output'] == {'value': 14}
    assert final['data']['result']['steps'][0]['status'] == 'escalated_recovered'
    from agent_py_agent.agent.workflows import WorkflowRunStore
    assert WorkflowRunStore(homes[0]).load_workflow_snapshot(paused['workflow_run_id'])['descriptor']['data_sources'] == []
