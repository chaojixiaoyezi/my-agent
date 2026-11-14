"""#73 固定脚本合同回归：原生资产、持久回执、真实 HTTP 与原审批门。"""
from __future__ import annotations

import ast
import base64
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.business_spaces import v13_w7_scripts as scripts
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.plugin_host.schemas import canonical_json
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import (
    v13_call,
    v13_seed,
    v13_space,
)

v13_host = harness.v13_host
_native_gateway = harness.v13_native


# LLM: 原生审批读口要求真实队列目录已初始化；只补测试服务器启动条件，不创建票据或替换 reader。
# 函数用途: 在原 HTTP 服务器上初始化待处理目录，让审批读取具备真实来源。
@pytest.fixture
def v13_native(_native_gateway):
    server, agent = _native_gateway
    server.paths.processing.mkdir(parents=True, exist_ok=True)
    return server, agent


# LLM: 原上下文由真实 binder 签发，不能以 SimpleNamespace 冒充权限。
# 函数用途: 创建隔离 owner 的原业务调用环境。
@pytest.fixture
def world(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    context = api.bind_business_context(v13_host, authorize=lambda action, facts: True, subject_id='script-tester')
    return SimpleNamespace(host=v13_host, context=context, services=api.BusinessServices(v13_host))


# LLM: 每次请求经过正式 dispatcher，不跳过 schema 或返回值验证。
# 函数用途: 发起一次进程内真实业务调用。
def invoke(world, action, params, *, key=None, revision=None):
    request = {'action': action, 'request_id': 'script-test-' + uuid4().hex, 'params': params}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, world.context, world.services)


# LLM: 合成代码只输出 JSON，不触碰外部服务或真实凭据。
# 函数用途: 通过公开创建入口保存可执行测试脚本。
def create(world, script_id='collect', *, patch=None, key=None):
    params = {'script_id': script_id, 'name': '固定采集', 'code': 'print(\'{"value": 7}\')'}
    params.update(patch or {})
    status, body = invoke(world, 'scripts.create', params, key=key or 'script-create-' + uuid4().hex)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return body['data']['script']


# LLM: 每个错误都核对真实运输状态、闭合失败信封和准确错误码。
# 函数用途: 断言正式业务失败响应。
def assert_error(result, status, code):
    actual, body = result
    assert actual == status, body
    assert body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: 参数筛选不能误删 any 脚本，列表不携带代码或宿主文件路径。
# 函数用途: 验证 #73-1 的上下文、标签、字段和分页合同。
def test_list_filters_context_tag_and_projects_contract_fields(world):
    create(world, 'alert-collect', patch={'tags': ['osquery'], 'contexts': ['alert']})
    create(world, 'case-collect', patch={'tags': ['osquery'], 'contexts': ['case']})
    create(world, 'any-collect', patch={'tags': ['osquery']})
    status, body = invoke(world, 'scripts.list', {'context': 'alert', 'tag': 'osquery'})
    assert status == 200, body
    rows = body['data']['items']
    assert [row['script_id'] for row in rows] == ['alert-collect', 'any-collect']
    assert set(rows[0]) == {'script_id', 'name', 'description', 'input_schema', 'risk_level',
        'requires_approval', 'enabled', 'tags', 'contexts', 'scope_layer', 'revision', 'last_run'}
    assert rows[0]['risk_level'] == 'low' and rows[0]['requires_approval'] is False
    assert body['data']['next_cursor'] is None


# LLM: 合成旧资产直接来自原生 Store，发现入口必须补齐输入而不改写旧定义。
# 函数用途: 验证 #73-2 详情、schema 回退、风险与脱敏。
def test_get_returns_input_schema_risk_and_approval_flag(world):
    home = world.host.home_paths.owner_home_dir
    store = ScriptStore(home)
    store.save_script(AssetDescriptor(id='legacy', kind='script', name='旧资产',
        io_contract=IOContract(inputs=('number',))), 'print("{}")')
    status, body = invoke(world, 'scripts.get', {'script_id': 'legacy'})
    assert status == 200, body
    row = body['data']['script']
    assert row['input_schema'] == {'type': 'object', 'properties': {'number': {}},
        'required': ['number'], 'additionalProperties': False}
    assert row['code'] == 'print("{}")' and row['entrypoint'] == 'main.py'
    assert row['risk_level'] == 'low' and row['requires_approval'] is False
    assert 'entrypoint_path' not in row
    created = create(world, 'guarded', patch={'risk_level': 'high', 'requires_approval': True})
    assert created['risk_level'] == 'high' and created['requires_approval'] is True
    assert_error(invoke(world, 'scripts.get', {'script_id': 'absent'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 同键同参跨服务实例应重放，新键重复 ID 必须冲突，不允许静默覆盖。
# 函数用途: 验证 #73-3 创建与持久幂等行为。
def test_create_then_duplicate_is_state_conflict(world):
    key = 'stable-create-key-0001'
    first = create(world, key=key)
    world.services = api.BusinessServices(world.host)
    replay = create(world, key=key)
    assert replay == first
    assert_error(invoke(world, 'scripts.create', {'script_id': 'collect', 'name': '同名', 'code': 'pass'},
        key='other-create-key-0001'), 409, 'STATE_CONFLICT')
    assert_error(invoke(world, 'scripts.create', {'script_id': 'other', 'name': '另一份', 'code': 'pass'},
        key=key), 409, 'IDEMPOTENCY_CONFLICT')
    assert_error(invoke(world, 'scripts.create', {'script_id': 'bad', 'name': '错误入口', 'code': '',
        'entrypoint': '../main.py'}, key='bad-entrypoint-key-0001'), 400, 'INVALID_SCHEMA')
    assert not (world.host.home_paths.owner_home_dir / 'scripts/bad').exists()


# LLM: 集合修订与脚本修订故意不同，防止错误使用集合 CAS；未改的原始代码必须保留。
# 函数用途: 验证 #73-4 记录 CAS、启停和安全更新。
def test_update_cas_and_enable_toggle(world):
    first = create(world, patch={'code': 'token = 1\nprint("{}")'})
    create(world, 'another')
    params = {'script_id': 'collect', 'description': '更新描述', 'enabled': False}
    status, body = invoke(world, 'scripts.update', params, key='update-script-key-0001', revision=first['revision'])
    assert status == 200, body
    updated = body['data']['script']
    assert updated['enabled'] is False and updated['description'] == '更新描述'
    assert updated['revision'] > first['revision']
    raw = ScriptStore(world.host.home_paths.owner_home_dir).get_script('collect')
    assert raw['code'] == 'token = 1\nprint("{}")'
    assert_error(invoke(world, 'scripts.update', {'script_id': 'collect', 'enabled': True},
        key='stale-update-key-0001', revision=first['revision']), 409, 'REVISION_CONFLICT')
    world.services = api.BusinessServices(world.host)
    replay = invoke(world, 'scripts.update', params, key='update-script-key-0001', revision=first['revision'])
    assert replay[0] == 200 and replay[1]['data']['script'] == updated
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect'}, key='disabled-run-key-0001'), 409, 'STATE_CONFLICT')


# LLM: 删除必须核对匹配记录版本，启用监控引用阻止删除而非留下悬空配置。
# 函数用途: 验证 #73-5 删除 CAS、引用冲突与删除幂等。
def test_delete_requires_matching_revision(world):
    row = create(world)
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='stale-delete-key-0001',
        revision=row['revision'] + 1), 409, 'REVISION_CONFLICT')
    home = world.host.home_paths.owner_home_dir
    v13_seed(home, 'monitor_rules', {'rule-one': {'rule_id': 'rule-one',
        'owner_id': world.context.owner_id, 'script_id': 'collect', 'enabled': True}})
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='used-delete-key-0001',
        revision=row['revision']), 409, 'STATE_CONFLICT')
    store = V13Store(home)
    store.transaction('monitor_rules', lambda records: records.clear() or {},
        idempotency_key='clear-rule-key-0001', params_digest='clear')
    result = invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='delete-script-key-0001', revision=row['revision'])
    assert result[0] == 200 and result[1]['data'] == {'script_id': 'collect', 'deleted': True}
    assert invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='delete-script-key-0001',
        revision=row['revision'])[1]['data'] == result[1]['data']
    assert_error(invoke(world, 'scripts.get', {'script_id': 'collect'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 修改 base64 里的偏移而不改变签名也必须失败，另一主体不能复用游标。
# 函数用途: 验证分页游标的篡改、筛选与版本绑定。
def test_cursor_binds_owner_subject_filter_and_snapshot(world):
    create(world, 'first')
    create(world, 'second')
    status, body = invoke(world, 'scripts.list', {'limit': 1})
    assert status == 200, body
    cursor = body['data']['next_cursor']
    assert cursor is not None
    token = json.loads(base64.urlsafe_b64decode(cursor))
    token['offset'] = 2
    changed = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    assert_error(invoke(world, 'scripts.list', {'limit': 1, 'cursor': changed}), 503, 'CURSOR_EXPIRED')
    assert_error(invoke(world, 'scripts.list', {'limit': 1, 'cursor': cursor, 'tag': 'other'}), 503, 'CURSOR_EXPIRED')
    status, next_body = invoke(world, 'scripts.list', {'limit': 1, 'cursor': cursor})
    assert status == 200 and next_body['data']['items'][0]['script_id'] == 'second'
    other = api.bind_business_context(world.host, authorize=lambda action, facts: True, subject_id='another-tester')
    assert_error(invoke(SimpleNamespace(context=other, services=world.services), 'scripts.list',
        {'cursor': cursor}), 503, 'CURSOR_EXPIRED')
    create(world, 'third')
    assert_error(invoke(world, 'scripts.list', {'cursor': cursor}), 503, 'CURSOR_EXPIRED')


# LLM: 真正调用原生 runner；持久 run 与返回数据必须一致，重放不会增加原生 run_count。
# 函数用途: 验证 #73-6 的同步执行、#73-7 结果读取和跨实例运行幂等。
def test_run_low_risk_native_runner_and_persisted_result(world):
    create(world)
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect'}, key='low-run-key-00000001')
    assert status == 200, body
    run = body['data']['run']
    assert run['state'] == 'completed' and run['ok'] is True, run
    assert run['output'] == {'value': 7} and run['task_id'] is None
    assert run['duration_ms'] >= 0 and run['started_at'] is not None and run['finished_at'] is not None
    home = world.host.home_paths.owner_home_dir
    stored = V13Store(home).get('script_runs', run['execution_id'])
    assert stored['state'] == 'completed' and stored['output'] == run['output']
    assert stored['inputs_sha256'] == scripts.digest({}) and 'inputs' not in stored
    assert ScriptStore(home).get_script('collect')['runtime_summary']['run_count'] == 1
    world.services = api.BusinessServices(world.host)
    replay = invoke(world, 'scripts.run', {'script_id': 'collect'}, key='low-run-key-00000001')
    assert replay[0] == 200 and replay[1]['data']['run'] == run
    assert ScriptStore(home).get_script('collect')['runtime_summary']['run_count'] == 1
    status, result = invoke(world, 'scripts.runs.get', {'execution_id': run['execution_id']})
    assert status == 200 and result['data']['run'] == run
    assert_error(invoke(world, 'scripts.runs.get', {'execution_id': 'absent'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 输入失败不写运行记录，输出失败不能变成 HTTP 层异常或假成功。
# 函数用途: 验证输入 schema、默认值、非零退出与结果脱敏边界。
def test_run_validates_inputs_redacts_and_bounds_output(world):
    schema = {'type': 'object', 'properties': {'number': {'type': 'integer', 'minimum': 1}},
        'required': ['number'], 'additionalProperties': False}
    create(world, patch={'input_schema': schema,
        'code': 'import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({"value":v["number"],"api_key":"synthetic-secret"}))'})
    home = world.host.home_paths.owner_home_dir
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'number': 'bad'}},
        key='invalid-input-key-0001'), 400, 'INVALID_SCHEMA')
    assert V13Store(home).list('script_runs') == []
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'number': 4}},
        key='valid-input-key-00001')
    assert status == 200 and body['data']['run']['output']['value'] == 4, body
    assert 'synthetic-secret' not in canonical_json(body)
    create(world, 'large', patch={'code': 'import json\nprint(json.dumps({"payload":"x"*70000,"api_key":"synthetic-secret"}))'})
    status, body = invoke(world, 'scripts.run', {'script_id': 'large'}, key='large-output-key-0001')
    assert status == 200 and body['data']['run']['ok'] is True, body
    output = body['data']['run']['output']
    assert output['truncated'] is True and len(canonical_json(output).encode()) <= 65536
    assert 'synthetic-secret' not in canonical_json(body)
    create(world, 'nonzero', patch={'code': 'import sys\nsys.exit(9)'})
    status, body = invoke(world, 'scripts.run', {'script_id': 'nonzero'}, key='nonzero-run-key-0001')
    assert status == 200, body
    failed = body['data']['run']
    assert failed['state'] == 'unknown' and failed['ok'] is None and failed['error'] == 'SCRIPT_EXIT_NONZERO'


# LLM: 两个服务实例并发使用同一回执；这是真实文件锁而非替换事务实现。
# 函数用途: 验证相同创建键只有一次原生写与记录 CAS 的单胜者。
def test_concurrent_idempotency_and_record_cas(world):
    contexts = [SimpleNamespace(host=world.host, context=world.context, services=api.BusinessServices(world.host))
                for index in range(2)]
    params = {'script_id': 'once', 'name': '只创建一次', 'code': 'print("{}")'}

    # LLM: 并行调用原 dispatcher，原生文件 revision 是副作用次数证据。
    # 函数用途: 对同一创建键发起并发请求。
    def concurrent_create(index):
        return invoke(contexts[index], 'scripts.create', params, key='concurrent-create-key-0001')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_create, range(2)))
    assert [status for status, body in results] == [200, 200], results
    assert results[0][1]['data'] == results[1][1]['data']
    revision = results[0][1]['data']['script']['revision']
    assert ScriptStore(world.host.home_paths.owner_home_dir).get_script('once')['revision'] == revision

    # LLM: 同一旧记录版本的不同请求，必须只有一个提交，另一个保留 REVISION_CONFLICT。
    # 函数用途: 发起并发 CAS 更新。
    def concurrent_update(index):
        return invoke(contexts[index], 'scripts.update', {'script_id': 'once', 'name': '修订' + str(index)},
            key='concurrent-update-key-' + str(index), revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_update, range(2)))
    assert sorted(status for status, body in results) == [200, 409], results
    assert next(body for status, body in results if status == 409)['error']['code'] == 'REVISION_CONFLICT'


# LLM: 中断后只读出 unknown，不通过重跑覆盖已发生但未确认的副作用。
# 函数用途: 验证持久 running 状态的恢复与幂等参数冲突。
def test_interrupted_run_is_unknown_and_never_reexecuted(world):
    create(world)
    key = 'interrupted-run-key-0001'
    execution_id = 'script-run-' + scripts.digest([world.context.owner_id, world.context.subject_id,
        'scripts.run', key])[:48]
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': execution_id, 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': False,
        'task_id': None, 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'running',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': now,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    store = V13Store(world.host.home_paths.owner_home_dir, owner_id=world.context.owner_id,
        subject_id=world.context.subject_id, action='scripts.run')

    # LLM: 模拟持久提交后的进程退出，只种入宿主原记录与回执，不伪造 runner 成功。
    # 函数用途: 写入一个真实中断点。
    def seed(records):
        records[execution_id] = row
        return {'execution_id': execution_id}

    store.transaction('script_runs', seed, idempotency_key=key,
        params_digest=scripts.digest(['scripts.run', {'script_id': 'collect'}, None]))
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect'}, key=key)
    assert status == 200 and body['data']['run']['state'] == 'unknown', body
    assert body['data']['run']['ok'] is None and body['data']['run']['coverage'] == 'unknown'
    assert ScriptStore(world.host.home_paths.owner_home_dir).get_script('collect')['runtime_summary']['run_count'] == 0
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'extra': True}}, key=key),
        409, 'IDEMPOTENCY_CONFLICT')


# LLM: Gateway/owner/dispatcher/runner 全链路真实运行，不用手写 JSON 代替服务端响应。
# 函数用途: 验证 #73-1/6/7/10 的真实 HTTP 往返与跨 owner 隔离。
def test_http_crud_run_and_owner_isolation(v13_native):
    space_id = v13_space(v13_native)
    params = {'space_id': space_id, 'script_id': 'http-collect', 'name': 'HTTP采集', 'code': 'print(\'{"wire":true}\')'}
    status, body = v13_call(v13_native, 'scripts.create', params, user='business-alice',
        idempotency_key='http-create-script-key-0001')
    assert status == 200 and body['error'] is None, body
    status, body = v13_call(v13_native, 'scripts.list', {'space_id': space_id}, user='business-alice')
    assert status == 200 and body['data']['items'][0]['script_id'] == 'http-collect', body
    assert_error(v13_call(v13_native, 'scripts.get', {'script_id': 'http-collect'}, user='business-bob'),
        404, 'RESOURCE_NOT_FOUND')
    status, body = v13_call(v13_native, 'scripts.run', {'script_id': 'http-collect', 'space_id': space_id},
        user='business-alice', idempotency_key='http-run-script-key-0001')
    assert status == 200 and body['state'] == 'completed' and body['error'] is None, body
    run = body['data']['run']
    assert run['ok'] is True and run['output'] == {'wire': True}, run
    status, result = v13_call(v13_native, 'scripts.runs.get', {'execution_id': run['execution_id']}, user='business-alice')
    assert status == 200 and result['data']['run'] == run, result
    assert_error(v13_call(v13_native, 'scripts.runs.get', {'execution_id': run['execution_id']}, user='business-bob'),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 发现走正式扩展目录，冻结目录不被脚本动作污染，未知动作仍使用原错误码。
# 函数用途: 验证 #73-9 路由对应动作发现与 #73-10 信封。
def test_route_map_actions_are_discoverable_in_extensions(v13_native):
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200 and body['error'] is None, body
    expected = {'scripts.list', 'scripts.get', 'scripts.create', 'scripts.update',
        'scripts.delete', 'scripts.run', 'scripts.runs.get'}
    assert expected <= set(body['data']['supported_actions'])
    assert_error(v13_call(v13_native, 'scripts.unregistered', {}, user='business-alice'), 400, 'UNSUPPORTED_ACTION')
    assert_error(v13_call(v13_native, 'scripts.run', {'script_id': 'absent', 'query': 'free-form'},
        user='business-alice', idempotency_key='http-invalid-schema-key-01'), 400, 'INVALID_SCHEMA')


# LLM: 假模型只用现有 echo，不替换派工端口；受理信封绝不能代表脚本已经执行。
# 函数用途: 验证高风险真实 HTTP 派工、耐久任务关联与稳定重放。
@pytest.mark.parametrize('declared_approval', [True, False])
def test_high_risk_run_assigns_task_and_preserves_native_gate(v13_native, declared_approval, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    original = foundation.v13_call_frozen
    report = session_task_dispatch.runtime_error_report

    # LLM: 仅将当前合成测试的原始异常栈加入测试输出，不改变原生派工失败分类。
    # 函数用途: 保留派工器屏蔽公开异常前的诊断。
    def traced_report(error, *, context=''):
        import traceback
        traceback.print_exception(error)
        return report(error, context=context)

    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', traced_report)

    # LLM: 诊断包装不改变执行或结果，仅保留高风险派工失败的准确原生入口。
    # 函数用途: 对真实冻结端口打印失败栈，不吞掉异常。
    def traced(call, action, params, *, idempotency_key=None, expected_revision=None):
        import traceback
        try:
            return original(call, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)
        except Exception:
            print('scripts frozen failure:', action)
            traceback.print_exc()
            raise

    monkeypatch.setattr(foundation, 'v13_call_frozen', traced)
    space_id = v13_space(v13_native)
    params = {'space_id': space_id, 'script_id': 'approval-collect', 'name': '待确认采集',
        'code': 'print(\'{"must_not_run_directly":true}\')', 'risk_level': 'high',
        'requires_approval': declared_approval}
    status, body = v13_call(v13_native, 'scripts.create', params, user='business-alice',
        idempotency_key='high-create-script-key-0001')
    assert status == 200, body
    run_params = {'space_id': space_id, 'script_id': 'approval-collect', 'inputs': {}}
    if declared_approval is False:
        run_params.pop('space_id')
    status, body = v13_call(v13_native, 'scripts.run', run_params, user='business-alice',
        idempotency_key='high-run-script-key-0001')
    assert status == 202 and body['state'] == 'accepted' and body['error'] is None, body
    accepted = body['data']
    assert accepted['requires_approval'] is True and accepted['task_id'] and accepted['operation_id']
    status, replay = v13_call(v13_native, 'scripts.run', run_params, user='business-alice',
        idempotency_key='high-run-script-key-0001')
    assert status == 202 and replay['data'] == accepted and replay['error'] is None, replay
    status, observed = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']},
        user='business-alice')
    assert status == 200 and observed['data']['run']['task_id'] == accepted['task_id'], observed
    assert observed['data']['run']['ok'] is not True and observed['data']['run']['output'] is None
    assert_error(v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']},
        user='business-bob'), 404, 'RESOURCE_NOT_FOUND')
    status, approvals = v13_call(v13_native, 'approvals.list', {'filter': {'task_id': accepted['task_id']}},
        user='business-alice')
    assert status == 200 and 'items' in approvals['data'], approvals
    # 此处没有伪造 pending；原生后台默认非交互，echo 不产生工具调用。
    assert_error(v13_call(v13_native, 'approvals.decide', {'approval_id': 'not-a-ticket', 'decision': 'approved',
        'response_ticket': 'synthetic-invalid-ticket'},
        user='business-alice', idempotency_key='forbidden-approval-key-0001', expected_revision=0),
        403, 'CAPABILITY_DENIED')


# LLM: 合成无效 ticket 只是拒绝反例；跨 owner 读口为 partial 时不能把未知缺口判成不存在。
# 函数用途: 独立验证冻结审批读取与模型业务请求不能决定人工票据。
def test_http_approval_decision_is_human_only(v13_native):
    status, body = v13_call(v13_native, 'approvals.list', {}, user='business-alice')
    assert status == 200 and body['error'] is None and isinstance(body['data']['items'], list), body
    assert_error(v13_call(v13_native, 'approvals.get', {'approval_id': 'synthetic-absent-ticket'},
        user='business-alice'), 503, 'HOST_NOT_READY')
    assert_error(v13_call(v13_native, 'approvals.decide', {'approval_id': 'synthetic-absent-ticket',
        'decision': 'approved', 'response_ticket': 'synthetic-invalid-ticket'}, user='business-alice',
        idempotency_key='forbidden-approval-key-0002', expected_revision=0), 403, 'CAPABILITY_DENIED')


# LLM: 同一真实 Agent/Store 的审批读口完整，缺失详情才允许返回 404；不模拟 pending 或签票。
# 函数用途: 保留完整原生来源下的不存在断言，与跨 owner 未知分支分别验证。
def test_approval_detail_missing_requires_complete_native_source(v13_native):
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import native_approval_readers

    server, agent = v13_native
    context, services = api.local_business_context(agent)
    readers = native_approval_readers(agent, server.paths, agent.home_paths.owner_id,
        agent.conversation_store)
    page = readers['approvals']({})
    assert page.coverage == 'complete' and page.records == ()
    services.projection.readers.update(readers)
    request = {'action': 'approvals.get', 'request_id': 'complete-native-source-missing-approval',
        'params': {'approval_id': 'synthetic-absent-ticket'}}
    assert_error(api.execute_business(request, context, services), 404, 'OBSERVATION_NOT_FOUND')


# LLM: 真实 ScriptsTool 的 runtime_policy 交给原 ActionPolicy，不能以 metadata 绕过 ask 门。
# 函数用途: 验证高风险派工所依赖的原生工具审批策略不被 W7 改写。
def test_native_scripts_tool_requires_approval_in_ask_mode(tmp_path):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )
    native = ScriptsTool(tmp_path)
    snapshot = runtime_snapshot_for_tools({'scripts': native})
    call = canonical_test_call(snapshot, 'scripts', {'action': 'run', 'script_id': 'registered', 'inputs': {}})
    decision = ActionPolicy().decide(ActionPolicyRequest(call, snapshot, tmp_path, approval_mode='ask'))
    assert decision.status == 'ask'
    assert not list(tmp_path.glob('**/main.py'))


# LLM: 这些是显式合成的观察端口反例，不声称覆盖原生票据发布或人工批准链。
# 函数用途: 验证 get_run 不把回音任务或缺失观察转换为脚本成功。
@pytest.mark.parametrize('observation', ['pending', 'echo_done', 'failed', 'unavailable'])
def test_get_run_handles_pending_failure_and_unavailable_evidence(world, monkeypatch, observation):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': 'synthetic-observation', 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': True,
        'task_id': 'synthetic-task', 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'accepted',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': None,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    v13_seed(world.host.home_paths.owner_home_dir, 'script_runs', {'synthetic-observation': row})

    # LLM: 仅替换冻结观察响应，不替换写权限或脚本执行器；响应身份在测试名称中明确为合成。
    # 函数用途: 提供单一失败条件的结构化观察样本。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        assert params.get('task_id', params.get('filter', {}).get('task_id')) == 'synthetic-task'
        if observation == 'unavailable':
            raise HostAPIError('HOST_NOT_READY', 503)
        if action == 'tasks.get':
            return {'task': {'status': 'failed' if observation == 'failed' else 'done' if observation == 'echo_done' else 'running'}}
        if action == 'approvals.list':
            items = [{'approval_id': 'synthetic-pending', 'state': 'pending'}] if observation == 'pending' else []
            return {'items': items, 'next_cursor': None, 'coverage': 'complete'}
        assert action == 'tool_calls.list'
        return {'items': [], 'next_cursor': None, 'coverage': 'complete'}

    monkeypatch.setattr(foundation, 'v13_call_frozen', observe)
    status, body = invoke(world, 'scripts.runs.get', {'execution_id': 'synthetic-observation'})
    assert status == 200, body
    run = body['data']['run']
    if observation == 'pending':
        assert run['state'] == 'awaiting_approval' and run['approval_ids'] == ['synthetic-pending']
    elif observation == 'failed':
        assert run['state'] == 'failed' and run['ok'] is False
    else:
        assert run['state'] == 'unknown' and run['ok'] is None and run['coverage'] == 'unknown'


# LLM: 明确的合成端口用于输入关联反例；不声称这是真实人工批准或真实原生操作落账。
# 函数用途: 验证只有同任务、同脚本、同输入的原生结果协议才能完成运行。
@pytest.mark.parametrize('case', ['list_only', 'wrong_script', 'completed', 'failed', 'missing_operation', 'unknown_operation'])
def test_get_run_requires_bound_script_run_result(world, monkeypatch, case):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': 'synthetic-bound-run', 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': True,
        'task_id': 'synthetic-task', 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'accepted',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': None,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    row['tool_args_hashes'] = [tool_arguments_hash({'action': 'run', 'script_id': 'collect', 'inputs': {}})]
    v13_seed(world.host.home_paths.owner_home_dir, 'script_runs', {'synthetic-bound-run': row})
    tool = {'operation_id': 'synthetic-operation', 'run_id': 'synthetic-runtime-run',
        'attempt_id': 'synthetic-attempt', 'task_id': 'synthetic-task', 'tool_name': 'scripts',
        'status': 'SUCCEEDED', 'executed': True, 'effect_outcome': 'committed', 'duration_ms': 5,
        'error': None, 'result_ref': 'synthetic-reference'}

    # LLM: 假端口严格断言查询身份，不按正文匹配或将 scripts.list 当成 run。
    # 函数用途: 提供结构化冻结观察，保持明确的 action 与参数反例。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'tasks.get':
            assert params == {'task_id': 'synthetic-task'}
            return {'task': {'status': 'done'}}
        if action == 'approvals.list':
            return {'items': [], 'next_cursor': None, 'coverage': 'complete'}
        if action == 'tool_calls.list':
            return {'items': [tool], 'next_cursor': None, 'coverage': 'complete'}
        assert action == 'tool_calls.get' and params == {'operation_id': 'synthetic-operation'}
        return {'tool_call': tool, 'redacted_input': {}}

    # LLM: 此假读口只模拟原协议的内容，参数断言证明实现请求准确 owner/run/attempt/tool。
    # 函数用途: 返回合成 ToolOperationRecord 的真实协议形状或缺失反例。
    def operation(self, *, owner_id, run_id, operation_id, task_id='', attempt_id='', tool_name='',
                  args_hash='', resource_scopes=None):
        assert owner_id == world.context.owner_id and run_id == 'synthetic-runtime-run'
        assert operation_id == 'synthetic-operation' and attempt_id == 'synthetic-attempt' and tool_name == 'scripts'
        if case == 'missing_operation':
            return None
        raw = {'script_id': 'collect', 'ok': case != 'failed', 'output_data': {'result': 8},
            'duration_ms': 5, 'started_at': 1, 'ended_at': 2, 'error_code': 'SCRIPT_EXIT_NONZERO' if case == 'failed' else '',
            'effect_outcome': 'failed' if case == 'failed' else 'confirmed'}
        status = 'unknown' if case == 'unknown_operation' else 'failed' if case == 'failed' else 'succeeded'
        arguments = {'action': 'list' if case == 'list_only' else 'run',
            'script_id': 'another' if case == 'wrong_script' else 'collect', 'inputs': {}}
        return SimpleNamespace(status=status, args_hash=tool_arguments_hash(arguments), result={'schema_version': 'tool_execution_result.v1',
            'handler_executed': True, 'output': canonical_json(raw)})

    monkeypatch.setattr(foundation, 'v13_call_frozen', observe)
    monkeypatch.setattr(ManagedOperationStore, 'get_tool_operation', operation)
    status, body = invoke(world, 'scripts.runs.get', {'execution_id': 'synthetic-bound-run'})
    assert status == 200, body
    run = body['data']['run']
    if case in ('completed', 'failed'):
        assert run['state'] == case and run['ok'] is (case == 'completed')
        assert run['output'] == {'result': 8} and run['duration_ms'] == 5
    else:
        assert run['state'] == 'unknown' and run['ok'] is None and run['coverage'] == 'unknown'


# LLM: 撤权只拒绝真实原记录事实，普通动作准入仍允许，防止用更宽准入检查掩盖回执泄露。
# 函数用途: 证明创建和更新回执重放都必须实时复核当前脚本记录。
@pytest.mark.parametrize('action', ['scripts.create', 'scripts.update'])
def test_p1_config_replay_rechecks_current_record_authorization(world, action):
    blocked = [False]

    # LLM: 授权回调由真实 binder 绑定，只在已撤销且传入记录版本时拒绝。
    # 函数用途: 区分动作事实和当前原生脚本记录授权。
    def authorize(requested, facts):
        return not (blocked[0] and 'revision' in facts)

    world.context = api.bind_business_context(world.host, authorize=authorize, subject_id='script-tester')
    params = {'script_id': 'p1-auth', 'name': '原记录授权', 'code': 'print("{}")'}
    revision = None
    if action == 'scripts.update':
        revision = create(world, 'p1-auth')['revision']
        params = {'script_id': 'p1-auth', 'description': '必须重新鉴权'}
    first = invoke(world, action, params, key='p1-authorization-key-0001', revision=revision)
    assert first[0] == 200, first
    blocked[0] = True
    world.services = api.BusinessServices(world.host)
    assert_error(invoke(world, action, params, key='p1-authorization-key-0001', revision=revision),
        403, 'CAPABILITY_DENIED')


# LLM: 原详情回执不能在脚本已删除后泄露旧正文，删除动作自身的无详情回执仍可合法重放。
# 函数用途: 证明已删除脚本的创建/更新成功回执不能绕过当前存在性。
@pytest.mark.parametrize('action', ['scripts.create', 'scripts.update'])
def test_p1_detail_receipt_does_not_leak_deleted_script(world, action):
    params = {'script_id': 'p1-removed', 'name': '删除前详情', 'code': 'print("{}")'}
    revision = None
    if action == 'scripts.update':
        revision = create(world, 'p1-removed')['revision']
        params = {'script_id': 'p1-removed', 'description': '删除后不得回显'}
    first = invoke(world, action, params, key='p1-deleted-detail-key-0001', revision=revision)
    assert first[0] == 200, first
    current = first[1]['data']['script']
    deleted = invoke(world, 'scripts.delete', {'script_id': 'p1-removed'},
        key='p1-delete-after-write-key', revision=current['revision'])
    assert deleted[0] == 200, deleted
    replay = invoke(world, 'scripts.delete', {'script_id': 'p1-removed'},
        key='p1-delete-after-write-key', revision=current['revision'])
    assert replay[0] == 200 and replay[1]['data'] == {'script_id': 'p1-removed', 'deleted': True}
    assert_error(invoke(world, action, params, key='p1-deleted-detail-key-0001', revision=revision),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 故障只发生在 V13 真实日志提交点之前，原生写入不回滚、不替换为假结果。
# 函数用途: 证明原生落盘后丢失最终回执的创建/更新/启停/删除均可按耐久身份恢复。
@pytest.mark.parametrize('operation', ['create', 'update', 'enable', 'mixed', 'delete'])
def test_p1_native_commit_before_receipt_is_recoverable(world, monkeypatch, operation):
    import hashlib

    from agent_py_agent.agent.business_spaces import v13_store
    home = world.host.home_paths.owner_home_dir
    action = 'scripts.create' if operation == 'create' else 'scripts.delete' if operation == 'delete' else 'scripts.update'
    params = {'script_id': 'p1-recover', 'name': '崩溃恢复', 'code': 'print("{}")'}
    revision = None
    if operation != 'create':
        initial = create(world, 'p1-recover')
        revision = initial['revision']
        params = {'script_id': 'p1-recover'}
        if operation in ('update', 'mixed'):
            params['description'] = '原生已提交的新描述'
        if operation in ('enable', 'mixed'):
            params['enabled'] = False
    key = 'p1-native-recovery-key-0001'
    receipt_key = hashlib.sha256((canonical_json([world.context.subject_id, action, key]) + '\n').encode()).hexdigest()
    original = v13_store.write_bytes_atomic_beneath
    fired = [False]

    # LLM: 仅拦截目标请求最终回执的首次 journal 发布，准备事务等其它真实写入保持原样。
    # 函数用途: 注入原生成功与 V13 最终提交之间的一次 I/O 失败。
    def fail_final_commit(root, parts, raw):
        if parts == ('data', 'business_v13', '.transaction.json') and not fired[0]:
            pending = json.loads(raw)
            if receipt_key in pending.get('states', {}).get('script_runs', {}).get('receipts', {}):
                fired[0] = True
                raise OSError('synthetic final receipt interruption')
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_final_commit)
    assert_error(invoke(world, action, params, key=key, revision=revision), 503, 'OUTCOME_UNKNOWN')
    assert fired[0]
    native_after = ScriptStore(home).get_script('p1-recover')
    if operation == 'delete':
        assert native_after is None
    else:
        assert native_after is not None
        if operation in ('enable', 'mixed'):
            assert native_after['enabled'] is False
    world.services = api.BusinessServices(world.host)
    status, replay = invoke(world, action, params, key=key, revision=revision)
    assert status == 200, replay
    if operation == 'delete':
        assert replay['data'] == {'script_id': 'p1-recover', 'deleted': True}
        assert ScriptStore(home).get_script('p1-recover') is None
    else:
        assert replay['data']['script']['revision'] == native_after['revision']
        assert ScriptStore(home).get_script('p1-recover')['revision'] == native_after['revision']


# LLM: 原生两次写之间失败不是整体成功；恢复必须跳过已有身份标记的 save，仅执行尚未提交的启停。
# 函数用途: 证明元数据更新加启停能从准确中间阶段继续而不重复递增版本。
def test_p1_mixed_update_recovers_between_native_steps(world, monkeypatch):
    initial = create(world, 'p1-middle')
    home = world.host.home_paths.owner_home_dir
    original = ScriptStore.set_enabled
    fired = [False]

    # LLM: 只让首次启停在执行前抛错，前一原生 save 仍真实落盘。
    # 函数用途: 注入两次原生提交之间的真实中断。
    def interrupt_enable(self, script_id, enabled):
        if not fired[0]:
            fired[0] = True
            raise OSError('synthetic step boundary interruption')
        return original(self, script_id, enabled)

    monkeypatch.setattr(ScriptStore, 'set_enabled', interrupt_enable)
    params = {'script_id': 'p1-middle', 'description': '中间已保存', 'enabled': False}
    assert_error(invoke(world, 'scripts.update', params, key='p1-middle-native-key-0001',
        revision=initial['revision']), 503, 'OUTCOME_UNKNOWN')
    partial = ScriptStore(home).get_script('p1-middle')
    assert partial['revision'] == initial['revision'] + 1 and partial['enabled'] is True
    world.services = api.BusinessServices(world.host)
    status, body = invoke(world, 'scripts.update', params, key='p1-middle-native-key-0001', revision=initial['revision'])
    assert status == 200, body
    assert body['data']['script']['description'] == '中间已保存' and body['data']['script']['enabled'] is False
    assert body['data']['script']['revision'] == initial['revision'] + 2


# LLM: 准备回执不等于原生提交证明；另一操作者造成同内容/同缺失状态也不能被本请求认领。
# 函数用途: 核验缺少准确操作身份时拒绝把其它原生操作当作本请求成功。
@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
def test_p1_prepared_request_cannot_claim_another_native_operation(world, monkeypatch, operation):
    home = world.host.home_paths.owner_home_dir
    initial = None if operation == 'create' else create(world, 'p1-other')
    revision = None if initial is None else initial['revision']
    action = 'scripts.' + operation
    params = {'script_id': 'p1-other'}
    if operation == 'create':
        params.update(name='相同内容不是身份', code='print("{}")')
    elif operation == 'update':
        params['description'] = '相同内容不是身份'
    method_name = 'delete_script' if operation == 'delete' else 'save_script'
    original = getattr(ScriptStore, method_name)

    # LLM: 在原生方法执行前中断，准备已真实落盘但本请求未产生任何原生提交。
    # 函数用途: 注入原生开始边界故障，不能生成伪成功证明。
    def interrupt_save(self, descriptor, code, *, entrypoint='main.py', interpreter='python3',
                       timeout_seconds=120, target_scope='owner', expected_revision=None):
        raise OSError('synthetic interruption before native save')

    # LLM: 删除开始前中断，随后由其它原生操作删除，不属于本请求。
    # 函数用途: 注入删除开始边界故障。
    def interrupt_delete(self, script_id):
        raise OSError('synthetic interruption before native delete')

    monkeypatch.setattr(ScriptStore, method_name, interrupt_delete if operation == 'delete' else interrupt_save)
    assert_error(invoke(world, action, params, key='p1-other-native-key-0001', revision=revision),
        503, 'OUTCOME_UNKNOWN')
    monkeypatch.setattr(ScriptStore, method_name, original)
    native = ScriptStore(home)
    if operation == 'delete':
        assert native.delete_script('p1-other') is True
    elif operation == 'create':
        native.save_script(AssetDescriptor(id='p1-other', kind='script', name=params['name'], owner_id=world.context.owner_id),
            params['code'], expected_revision=0)
    else:
        old = native.get_script('p1-other')
        from dataclasses import fields, replace
        names = {field.name for field in fields(AssetDescriptor)}
        descriptor = AssetDescriptor.from_dict({name: value for name, value in old.items() if name in names})
        native.save_script(replace(descriptor, summary=params['description']), old['code'],
            expected_revision=revision)
    world.services = api.BusinessServices(world.host)
    expected = (404, 'RESOURCE_NOT_FOUND') if operation == 'delete' else (
        (409, 'STATE_CONFLICT') if operation == 'create' else (409, 'REVISION_CONFLICT'))
    assert_error(invoke(world, action, params, key='p1-other-native-key-0001', revision=revision), expected[0], expected[1])


# LLM: 旧删除操作的重试只能恢复原收据，不能误删同 ID 后来重建的新脚本。
# 函数用途: 核验原子移位证明将删除恢复绑定到原代资产而非当前同名资产。
def test_p1_delete_recovery_preserves_recreated_script(world, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    initial = create(world, 'p1-generation')
    home = world.host.home_paths.owner_home_dir
    original = v13_store.write_bytes_atomic_beneath
    failed = [False]

    # LLM: 仅最终删除收据被拦截，准备和原生移位仍真实提交。
    # 函数用途: 在删除原代资产后、最终回执提交前中断。
    def fail_delete_receipt(root, parts, raw):
        if parts == ('data', 'business_v13', '.transaction.json') and not failed[0]:
            pending = json.loads(raw)
            receipts = pending.get('states', {}).get('script_runs', {}).get('receipts', {})
            if any(row['result'] == {'script_id': 'p1-generation', 'deleted': True} for row in receipts.values()):
                failed[0] = True
                raise OSError('synthetic delete receipt interruption')
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_delete_receipt)
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'p1-generation'},
        key='p1-generation-delete-0001', revision=initial['revision']), 503, 'OUTCOME_UNKNOWN')
    native = ScriptStore(home)
    replacement = native.save_script(AssetDescriptor(id='p1-generation', kind='script', name='后来重建',
        owner_id=world.context.owner_id), 'print("new generation")', expected_revision=0)
    status, replay = invoke(world, 'scripts.delete', {'script_id': 'p1-generation'},
        key='p1-generation-delete-0001', revision=initial['revision'])
    assert status == 200 and replay['data']['deleted'] is True
    current = native.get_script('p1-generation')
    assert current['revision'] == replacement['revision'] and current['code'] == 'print("new generation")'


# LLM: 这是冻结 receipt 形状适配测试，不冒充真实任务执行或人工批准；真实 HTTP 测试独立保留。
# 函数用途: 校验同步 assignment_ref 映射、真实202操作ID以及不受理/额外伪字段的拒绝。
@pytest.mark.parametrize('kind', ['native', 'accepted', 'pending', 'extra_field'])
def test_assignment_receipt_uses_native_identity(kind):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import DomainResult
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    raw = {'assignment_ref': 'original-assignment', 'task_id': 'original-task', 'receipt_state': 'accepted'}
    if kind == 'native':
        assert scripts._assignment_receipt(raw) == {'task_id': 'original-task', 'operation_id': 'original-assignment'}
    elif kind == 'accepted':
        result = DomainResult(202, {'assignment_ref': 'original-assignment',
            'task_id': 'original-task', 'operation_id': 'original-operation'})
        assert scripts._assignment_receipt(result) == {'task_id': 'original-task', 'operation_id': 'original-operation'}
    else:
        if kind == 'pending':
            raw['receipt_state'] = 'pending'
        else:
            raw['operation_id'] = 'non-native-operation'
        with pytest.raises(HostAPIError) as error:
            scripts._assignment_receipt(raw)
        assert error.value.code == 'HOST_NOT_READY'


# LLM: 原生目录操作使用 POSIX 描述符；枚举结束后再 fstat，排除采样器自身的临时目录 FD。
# 函数用途: 取得当前进程真实打开的描述符身份，不引入额外测试依赖。
def _script_fd_snapshot():
    directory = next(path for path in (Path('/proc/self/fd'), Path('/dev/fd')) if path.is_dir())
    with os.scandir(directory) as entries:
        numbers = [int(entry.name) for entry in entries if entry.name.isdecimal()]
    result = {}
    for number in numbers:
        try:
            status = os.fstat(number)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            result[number] = (status.st_dev, status.st_ino, status.st_mode)
    return result


def _script_fd_is_open(number, identity):
    try:
        status = os.fstat(number)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return False
    return (status.st_dev, status.st_ino, status.st_mode) == identity


# LLM: 只观察原生获取/删除，不替换存储、服务或信封；单次错误只发生在目的目录获取处。
# 函数用途: 记录删除调用内的真实 FD 差额，失败断言后也收回测试观测到的残留资源。
@pytest.fixture
def delete_resources(monkeypatch):
    observed = SimpleNamespace(handles=[], measurements=[], failure=None, errors=0, home=None)
    original_open = scripts.open_directory_beneath
    original_delete = scripts._NativeCASStore._delete

    def open_directory(root, parts, **kwargs):
        if parts == ('data', 'business_v13', 'script_delete_proofs') and observed.failure is not None:
            error, observed.failure = observed.failure, None
            observed.errors += 1
            raise error
        number = original_open(root, parts, **kwargs)
        status = os.fstat(number)
        observed.home = Path(root)
        observed.handles.append((number, (status.st_dev, status.st_ino, status.st_mode), parts))
        return number

    def delete(store, script_id):
        before = _script_fd_snapshot()
        try:
            return original_delete(store, script_id)
        finally:
            after = _script_fd_snapshot()
            observed.measurements.append(len(after) - len(before))
            if len(after) != len(before):
                import fcntl
                added = {}
                for number, identity in after.items():
                    if before.get(number) != identity:
                        try:
                            path = fcntl.fcntl(number, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0].decode()
                        except (AttributeError, OSError):
                            path = 'descriptor identity: ' + repr(identity)
                        added[number] = path
                print('Delete FD observation:', {'delta': observed.measurements[-1], 'added': added})

    monkeypatch.setattr(scripts, 'open_directory_beneath', open_directory)
    monkeypatch.setattr(scripts._NativeCASStore, '_delete', delete)
    try:
        yield observed
    finally:
        for number, identity, _parts in observed.handles:
            if _script_fd_is_open(number, identity):
                os.close(number)


def _script_delete_proof(directory, script_id):
    matches = [json.loads(path.read_text()) for path in directory.glob('*.json')]
    matches = [row for row in matches if row.get('schema_version') == 'script-config-proof.v1'
               and row.get('action') == 'scripts.delete' and row.get('script_id') == script_id]
    assert len(matches) == 1, matches
    return matches[0]


# LLM: 真实 HTTP 创建/删除及同键重试均经过原 dispatcher、CAS、原生资产与耐久回执，不手写响应。
# 函数用途: 验证成功和单次目的目录 EIO 都关闭 FD，失败保留准确证明，重试不删除后建同名资产。
@pytest.mark.parametrize('destination_error', [False, True])
def test_script_delete_cleanup_http(v13_native, delete_resources, destination_error):
    script_id, key = 'r4-http-cleanup', 'r4-http-delete-key-0001'
    status, created = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '删除资源检查', 'code': 'print("original")'},
        user='business-alice', idempotency_key='r4-http-create-key-0001')
    assert status == 200 and created['state'] == 'completed' and created['error'] is None, created
    revision = created['data']['script']['revision']
    delete_resources.failure = OSError(errno.EIO, 'single destination acquisition error') if destination_error else None
    result = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
        user='business-alice', idempotency_key=key, expected_revision=revision)
    if destination_error:
        assert_error(result, 503, 'OUTCOME_UNKNOWN')
    print('HTTP delete fd_delta:', delete_resources.measurements)
    source_handles = [(fd, identity) for fd, identity, parts in delete_resources.handles if parts == ('scripts',)]
    assert source_handles
    assert not any(_script_fd_is_open(fd, identity) for fd, identity in source_handles), 'source descriptor retained'
    assert delete_resources.measurements == [0]
    home = delete_resources.home
    previous_proof = None
    if destination_error:
        assert delete_resources.errors == 1
        assert ScriptStore(home).get_script(script_id)['code'] == 'print("original")'
        previous_proof = _script_delete_proof(home / 'scripts' / script_id, script_id)
        assert previous_proof['phase'] == 'deleted'
        result = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
            user='business-alice', idempotency_key=key, expected_revision=revision)
        assert delete_resources.measurements == [0, 0]
    assert result[0] == 200 and result[1]['state'] == 'completed' and result[1]['error'] is None, result
    assert result[1]['data'] == {'script_id': script_id, 'deleted': True}
    assert ScriptStore(home).get_script(script_id) is None
    proof_root = home / 'data' / 'business_v13' / 'script_delete_proofs'
    directories = list(proof_root.iterdir())
    assert len(directories) == 1
    proof = _script_delete_proof(directories[0], script_id)
    assert proof['operation_id'] == directories[0].name and proof['phase'] == 'deleted'
    assert proof['fingerprint'] == scripts.digest(['scripts.delete', {'script_id': script_id}, revision])
    assert {path.name for path in directories[0].iterdir()} == {proof['operation_id'] + '.json'}
    if previous_proof is not None:
        assert proof == previous_proof
    status, replacement = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '后建资产', 'code': 'print("replacement")'},
        user='business-alice', idempotency_key='r4-http-recreate-key-0001')
    assert status == 200 and replacement['error'] is None, replacement
    replay = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
        user='business-alice', idempotency_key=key, expected_revision=revision)
    assert replay[0] == 200 and replay[1]['data'] == result[1]['data'] and replay[1]['error'] is None, replay
    assert ScriptStore(home).get_script(script_id)['code'] == 'print("replacement")'
    assert _script_delete_proof(directories[0], script_id) == proof
    assert all(delta == 0 for delta in delete_resources.measurements)
    assert not any(_script_fd_is_open(fd, identity) for fd, identity, _parts in delete_resources.handles)


# LLM: 遍历整个模块的目录/原生 FD 获取；禁止第二次获取先于第一次清理登记，保留单 FD try/finally。
# 函数用途: 返回没有立即登记关闭或紧邻 finally 保护的获取点，防止同模式在其它函数重现。
def _script_fd_cleanup_gaps(source):
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    gaps = []
    for parent in ast.walk(tree):
        for _field, block in ast.iter_fields(parent):
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block):
                if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
                    continue
                if ast.unparse(statement.value.func) not in ('open_directory_beneath', 'os.open', 'os.dup'):
                    continue
                name = ast.unparse(statement.targets[0])
                following = block[index + 1] if index + 1 < len(block) else None
                registered = False
                if isinstance(following, ast.Expr) and isinstance(following.value, ast.Call):
                    call = following.value
                    if (isinstance(call.func, ast.Attribute) and call.func.attr == 'callback'
                            and len(call.args) == 2 and ast.unparse(call.args[0]) == 'os.close'
                            and ast.unparse(call.args[1]) == name):
                        scope = parents.get(statement)
                        while scope is not None:
                            if isinstance(scope, ast.With):
                                registered = any(isinstance(item.context_expr, ast.Call)
                                    and ast.unparse(item.context_expr.func) == 'ExitStack'
                                    and item.optional_vars is not None
                                    and ast.unparse(item.optional_vars) == ast.unparse(call.func.value)
                                    for item in scope.items)
                                if registered:
                                    break
                            scope = parents.get(scope)
                if isinstance(following, ast.Try):
                    registered = any(isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.close'
                        and len(node.args) == 1 and ast.unparse(node.args[0]) == name
                        for final in following.finalbody for node in ast.walk(final))
                if not registered:
                    gaps.append((statement.lineno, name))
    return gaps


def test_script_descriptor_acquisitions_register_cleanup_immediately():
    assert _script_fd_cleanup_gaps(Path(scripts.__file__).read_text()) == []
    paired = '''
def paired():
    source = open_directory_beneath(root, source_parts)
    destination = open_directory_beneath(root, destination_parts)
    try:
        move(source, destination)
    finally:
        os.close(destination)
        os.close(source)
'''
    assert _script_fd_cleanup_gaps(paired) == [(3, 'source')]
    unentered = '''
def unentered():
    cleanup = ExitStack()
    source = open_directory_beneath(root, source_parts)
    cleanup.callback(os.close, source)
'''
    assert _script_fd_cleanup_gaps(unentered) == [(4, 'source')]


# LLM: CancelledError 只模拟同步删除栈的取消展开，不声称覆盖会话 stop；操作准备和证明仍由原 dispatcher 写入。
# 函数用途: 验证 BaseException 展开也关闭源 FD，保留证明并可用原键恢复。
def test_script_delete_cleanup_on_cancellation(world, delete_resources):
    from asyncio import CancelledError

    script_id, key = 'r4-cancel-cleanup', 'r4-cancel-delete-key-0001'
    initial = create(world, script_id)
    home = world.host.home_paths.owner_home_dir
    delete_resources.failure = CancelledError()
    with pytest.raises(CancelledError):
        invoke(world, 'scripts.delete', {'script_id': script_id}, key=key, revision=initial['revision'])
    print('Cancelled delete fd_delta:', delete_resources.measurements)
    assert delete_resources.errors == 1 and delete_resources.measurements == [0]
    assert not any(_script_fd_is_open(fd, identity) for fd, identity, _parts in delete_resources.handles)
    proof = _script_delete_proof(home / 'scripts' / script_id, script_id)
    assert ScriptStore(home).get_script(script_id) is not None
    status, body = invoke(world, 'scripts.delete', {'script_id': script_id}, key=key, revision=initial['revision'])
    assert status == 200 and body['state'] == 'completed' and body['error'] is None, body
    private = home / 'data' / 'business_v13' / 'script_delete_proofs' / proof['operation_id']
    assert _script_delete_proof(private, script_id) == proof
    assert delete_resources.measurements == [0, 0]
    assert ScriptStore(home).get_script(script_id) is None


# LLM: Event 等到第二请求竞争首请求实持的原生锁才释放，不以 HTTP 到达代替锁竞争，不替换真实锁行为。
# 函数用途: 证明同键请求只删除一次、活动删除数不超过一，后到重放不擦除准确删除证明。
def test_script_delete_same_key_interleaving_retains_proof(v13_native, delete_resources, monkeypatch):
    from agent_py_agent.agent.common import json_io

    entered, contending, release = Event(), Event(), Event()
    guard = Lock()
    active = {'current': 0, 'maximum': 0, 'calls': 0}
    held = {'thread': None, 'mutexes': (), 'contended_path': None}
    original_path_lock = json_io.path_thread_lock
    original_delete = scripts._NativeCASStore._delete
    key, script_id = 'r4-interleaved-delete-key-0001', 'r4-interleaved'
    status, created = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '并发删除', 'code': 'print("{}")'},
        user='business-alice', idempotency_key='r4-interleaved-create-key-0001')
    assert status == 200, created
    revision = created['data']['script']['revision']

    def observe_path_lock(path):
        handle = original_path_lock(path)
        with guard:
            waiting = not release.is_set() and not contending.is_set()
            other_thread = held['thread'] is not None and get_ident() != held['thread']
            same_mutex = any(handle is mutex for mutex in held['mutexes'])
            if waiting and other_thread and same_mutex:
                # 先用同一实际锁证明竞争失败，再走原获取器；不靠调度快慢推测第二请求是否已竞争。
                if handle.lock.acquire(blocking=False):
                    handle.lock.release()
                    raise AssertionError('首请求仍在等待时原生锁已提前释放')
                held['contended_path'] = str(path)
                contending.set()
        return handle

    def delete(store, item):
        with json_io._JSON_FILE_LOCKS_GUARD:
            observed_mutexes = tuple(
                mutex for name, mutex in json_io._JSON_FILE_LOCKS.items()
                if name.startswith(str(store.owner_home) + os.sep) and mutex.lock.locked()
            )
        assert observed_mutexes, '原生删除没有实持的 owner 锁'
        with guard:
            active['current'] += 1
            active['calls'] += 1
            active['maximum'] = max(active['maximum'], active['current'])
            held['thread'] = get_ident()
            held['mutexes'] = observed_mutexes
        entered.set()
        try:
            assert release.wait(10), '未收到第二个请求的交错释放'
            with guard:
                held['mutexes'] = ()
            return original_delete(store, item)
        finally:
            with guard:
                held['mutexes'] = ()
                active['current'] -= 1

    monkeypatch.setattr(json_io, 'path_thread_lock', observe_path_lock)
    monkeypatch.setattr(scripts._NativeCASStore, '_delete', delete)

    def request():
        return v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
            user='business-alice', idempotency_key=key, expected_revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        try:
            assert entered.wait(10), '首个删除未进入原生边界'
            second = pool.submit(request)
            assert contending.wait(10), '第二个 HTTP 请求未竞争首请求实持的原生锁'
            assert active == {'current': 1, 'maximum': 1, 'calls': 1}
        finally:
            release.set()
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert active == {'current': 0, 'maximum': 1, 'calls': 1}
    assert held['contended_path'] is not None
    assert [status for status, _body in results] == [200, 200], results
    assert all(body['state'] == 'completed' and body['error'] is None for _status, body in results)
    assert results[0][1]['data'] == results[1][1]['data'] == {'script_id': script_id, 'deleted': True}
    proof_root = delete_resources.home / 'data' / 'business_v13' / 'script_delete_proofs'
    directories = list(proof_root.iterdir())
    assert len(directories) == 1
    proof = _script_delete_proof(directories[0], script_id)
    assert proof['operation_id'] == directories[0].name
    assert delete_resources.measurements == [0]
    print('Interleaved delete maximum_active:', active['maximum'], 'fd_delta:', delete_resources.measurements,
          'contended_lock:', Path(held['contended_path']).name)


# LLM: 经真实 HTTP 准入高风险运行，任务和 operation 仍由原 writer 产生；不签审批票或模拟展示输入。
# 函数用途: 保存批准路径的关联并通过规范 managed 写口发布真实 runner 输出。
def _150_approved_roundtrip(native, *, inputs=None, arguments=None, ok=True, effect_path=None):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    from agent_py_agent.tests.test_v13_w0_observation_readers import managed_roundtrip_operation

    space = v13_space(native)
    schema = {'type': 'object', 'properties': {'number': {'type': 'integer'}}, 'additionalProperties': False}
    status, created = v13_call(native, 'scripts.create', {'script_id': 'roundtrip-script',
        'space_id': space, 'name': '原生结算往返', 'code': (
            f"import os\nwith open({str(effect_path)!r}, 'a') as f:\n f.write('issued\\n');f.flush();os.fsync(f.fileno())\nraise SystemExit(9)"
            if effect_path else 'print(\'{"value": 7}\')' if ok else 'raise SystemExit(9)'),
        'risk_level': 'high', 'requires_approval': True, 'input_schema': schema},
        user='business-alice', idempotency_key='150-create-roundtrip')
    assert status == 200, created
    params = {'script_id': 'roundtrip-script', 'space_id': space}
    if inputs is not None:
        params['inputs'] = inputs
    key = '150-admit-roundtrip'
    status, accepted = v13_call(native, 'scripts.run', params,
        user='business-alice', idempotency_key=key)
    assert status == 202, accepted
    [services] = [service for service in native[0]._business_services.values()
        if service.owner_id.endswith('/business-alice')]
    store = services.host.conversation_store
    task = store.session_tasks.require(accepted['data']['task_id'])
    if task.status == 'queued':
        task = store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    if task.status == 'accepted':
        task = store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done'))
    assert task.status == 'done'
    actual_inputs = inputs or {}
    # 这里只运行隔离测试资产，验证结果运输与恢复；不声称覆盖真实人工批准链。
    result = ScriptRunner(services.owner_home, owner_scope_root=services.owner_home,
        agent_home_root=services.host.home_paths.root,
        extra_write_roots=(effect_path.parent,) if effect_path else ()).run_script('roundtrip-script', actual_inputs)
    assert result.ok is ok
    raw = {**vars(result), 'script_id': 'roundtrip-script'}
    args = arguments if arguments is not None else {
        'action': 'run', 'script_id': 'roundtrip-script', 'inputs': actual_inputs}
    runtime, run, operation_id, payload = managed_roundtrip_operation(services, task,
        arguments=args, raw=raw, ok=ok)
    return services, task, runtime, run, operation_id, params, key, accepted['data']


# LLM: 参数 hash 在准入时固定，空对象只允许省略和显式两种原生形状；服务重建后仍可恢复。
# 函数用途: 真实 HTTP 核对准入摘要、成功/失败恢复、重放终态和来源失效后的单调性。
@pytest.mark.parametrize('inputs,omit_tool_inputs,ok', [
    (None, True, True), ({}, False, True), ({'number': 4}, False, True), ({}, True, False),
])
def test_150_approved_managed_http_terminal(v13_native, inputs, omit_tool_inputs, ok):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash

    args = {'action': 'run', 'script_id': 'roundtrip-script'}
    if not omit_tool_inputs:
        args['inputs'] = inputs or {}
    services, task, runtime, run, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, inputs=inputs, arguments=args, ok=ok)
    stored = V13Store(services.owner_home).get('script_runs', accepted['execution_id'])
    expected = [tool_arguments_hash({'action': 'run', 'script_id': 'roundtrip-script', 'inputs': inputs or {}})]
    if not inputs:
        expected.append(tool_arguments_hash({'action': 'run', 'script_id': 'roundtrip-script'}))
    assert stored['tool_args_hashes'] == expected
    status, body = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200, body
    terminal = body['data']['run']
    assert terminal['state'] == ('completed' if ok else 'unknown') and terminal['ok'] is (True if ok else None), terminal
    assert terminal['output'] == ({'value': 7} if ok else {})
    assert terminal['task_id'] == task.task_id
    assert terminal['coverage'] == (services.observation.readers['approvals']({}).coverage if ok else 'unknown')
    status, replay = v13_call(v13_native, 'scripts.run', params, user='business-alice', idempotency_key=key)
    assert status == 200 and replay['data']['run'] == terminal, replay
    # 精确来源此时损坏；已保存终态不依赖再次观察才能继续返回。
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
        conn.commit()
    assert runtime.db_path == runtime_db_path(services.owner_home)
    services.observation.readers.clear()
    status, again = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200 and again['data']['run'] == terminal, again
    status, again = v13_call(v13_native, 'scripts.run', params, user='business-alice', idempotency_key=key)
    assert status == 200 and again['data']['run'] == terminal, again


# LLM: 各反例都通过真实 claim/settle 写入，公开观察不会提供虚构 redacted_input。
# 函数用途: 验证摘要包含 action、script、完整输入和额外参数，坏 task provenance 不恢复结果。
@pytest.mark.parametrize('case', ['action', 'script', 'inputs', 'extra', 'task_provenance', 'owner', 'attempt'])
def test_150_native_binding_rejects_mismatches(v13_native, case):
    args = {'action': 'run', 'script_id': 'roundtrip-script', 'inputs': {}}
    if case == 'action':
        args['action'] = 'list'
    elif case == 'script':
        args['script_id'] = 'different-script'
    elif case == 'inputs':
        args['inputs'] = {'number': 99}
    elif case == 'extra':
        args['other'] = True
    services, task, runtime, run, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, arguments=args)
    with runtime._runtime_connection() as conn:
        if case == 'task_provenance':
            metadata = json.loads(conn.execute('SELECT metadata_json FROM task_runs WHERE task_run_id=?',
                (run['task_run_id'],)).fetchone()[0])
            metadata['session_task_ref'] = 'different-task'
            conn.execute('UPDATE task_runs SET metadata_json=? WHERE task_run_id=?', (json.dumps(metadata), run['task_run_id']))
        elif case == 'owner':
            conn.execute('UPDATE tasks SET owner_id=? WHERE task_id=?', ('local/other', run['task_id']))
        elif case == 'attempt':
            other = runtime.record_run_creation(owner_id=services.owner_id, run_id='different-run')
            conn.execute('UPDATE tool_operations SET attempt_id=? WHERE operation_id=?', (other['attempt_id'], operation_id))
        conn.commit()
    status, body = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200, body
    assert body['data']['run']['state'] == 'unknown' and body['data']['run']['ok'] is None, body


# LLM: 此组只验证已准入记录的消费，不代替上面的真实派工测试；规范任务创建不发布 wake。
# 函数用途: 在受限环境也能覆盖真实 managed 结算、观察与脚本终态恢复。
from agent_py_agent.tests import test_v13_w0_observation_readers as observation_roundtrip

_observation_gateway = observation_roundtrip.background_http


@pytest.fixture
def background_http(_observation_gateway):
    _observation_gateway[0].paths.processing.mkdir(parents=True, exist_ok=True)
    return _observation_gateway


@pytest.mark.parametrize('case', ['explicit_empty', 'omitted_empty', 'populated', 'wrong_input', 'failed'])
def test_150_native_consumer_without_dispatch(background_http, case):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash

    services, task = observation_roundtrip.observed_task(background_http)
    inputs = {'number': 4} if case == 'populated' else {}
    status, created = observation_roundtrip.background_call(background_http, 'scripts.create', {
        'script_id': 'consumer-script', 'name': '消费者往返',
        'code': 'raise SystemExit(9)' if case == 'failed' else 'print(\'{"value": 7}\')',
        'input_schema': {'type': 'object', 'properties': {'number': {'type': 'integer'}}, 'additionalProperties': False}},
        idempotency_key='150-consumer-create')
    assert status == 200, created
    status, executed = observation_roundtrip.background_call(background_http, 'scripts.run',
        {'script_id': 'consumer-script', 'inputs': inputs}, idempotency_key='150-consumer-native-run')
    assert status == 200, executed
    actual = executed['data']['run']
    assert actual['state'] == ('unknown' if case == 'failed' else 'completed'), actual
    admitted = V13Store(services.owner_home).get('script_runs', actual['execution_id'])
    args = {'action': 'run', 'script_id': 'consumer-script', 'inputs': inputs}
    hashes = [tool_arguments_hash(args)]
    if not inputs:
        hashes.append(tool_arguments_hash({'action': 'run', 'script_id': 'consumer-script'}))
    raw = {'script_id': 'consumer-script', 'ok': actual['ok'], 'output_data': actual['output'],
        'duration_ms': actual['duration_ms'], 'started_at': 1, 'ended_at': 2,
        'error_code': actual['error'], 'effect_outcome': 'confirmed' if actual['ok'] else 'unknown'}
    if case == 'omitted_empty':
        args.pop('inputs')
    elif case == 'wrong_input':
        args['inputs'] = {'number': 99}
    runtime, run, operation_id, payload = observation_roundtrip.managed_roundtrip_operation(
        services, task, arguments=args, raw=raw, ok=actual['ok'])
    task = services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done'))
    # 这是消费端的明确初态夹具，由真实 V13Store 保存，不注入观察 reader 或结果字典。
    bound = {**admitted, 'execution_id': 'consumer-approved', 'task_id': task.task_id,
        'requires_approval': True, 'state': 'accepted', 'ok': None, 'output': None, 'error': None,
        'started_at': None, 'finished_at': None, 'tool_args_hashes': hashes}
    v13_seed(services.owner_home, 'script_runs', {'consumer-approved': bound})
    status, body = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
    assert status == 200, body
    result = body['data']['run']
    expected = 'unknown' if case in ('wrong_input', 'failed') else 'completed'
    assert result['state'] == expected, result
    if expected != 'unknown':
        assert result['output'] == actual['output'] and result['ok'] is actual['ok']
        status, again = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
        assert status == 200 and again['data']['run'] == result, again
        with runtime._runtime_connection() as conn:
            conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
            conn.commit()
        status, again = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
        assert status == 200 and again['data']['run'] == result, again


def test_approved_unknown_script_same_key_never_dispatches_or_repeats_effect(v13_native, tmp_path):
    marker = tmp_path / 'business-issued.log'
    services, task, runtime, _, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, ok=False, effect_path=marker)
    assert marker.read_text().splitlines() == ['issued']
    original = None
    for _ in range(3):
        status, body = v13_call(v13_native, 'scripts.runs.get', {
            'execution_id': accepted['execution_id']}, user='business-alice')
        assert status == 200 and body['data']['run']['state'] == 'unknown'
        observed = body['data']['run']
        assert observed['task_id'] == task.task_id and observed['error'] == 'SCRIPT_EXIT_NONZERO'
        original = original or observed
        assert observed == original
        status, replay = v13_call(v13_native, 'scripts.run', params,
            user='business-alice', idempotency_key=key)
        assert status == 200 and replay['data']['run'] == original
        assert marker.read_text().splitlines() == ['issued']
    saved = V13Store(services.owner_home).get('script_runs', accepted['execution_id'])
    assert saved['execution_result_ref']['operation_id'] == operation_id
    # 原证明已冻结，观察源损坏不能抹去原错误或制造新受理责任。
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
        conn.commit()
    status, replay = v13_call(v13_native, 'scripts.run', params,
        user='business-alice', idempotency_key=key)
    assert status == 200 and replay['data']['run'] == original
    assert marker.read_text().splitlines() == ['issued']
