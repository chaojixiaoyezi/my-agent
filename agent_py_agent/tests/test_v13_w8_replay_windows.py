"""回放评测的固定样本、指标与隔离边界测试；数据全部为合成记录。"""
from __future__ import annotations

import csv
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w8_replay_windows as replay
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _error_code,
    bind_business_context,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec as registered_spec
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw, validate_object

pytest_plugins = ('agent_py_agent.tests.test_v13_w0_harness',)


# LLM: 仅为领域单测显式构造 F10 输入，不将此替身当真实 Gateway 验收。
# 类用途: 保存可替换的测试调用事实。
@dataclass(frozen=True)
class ReplayCall:
    action: str
    params: dict
    owner_id: str = 'local/main'
    subject_id: str = 'analyst'
    idempotency_key: str = ''
    expected_revision: int | None = None
    services: object = None
    context: object = None
    owner_home: object = None
    clock: object = None


# LLM: 该内存替身只验证领域编排，不替代 W0 持久化/授权/HTTP 测试。
# 类用途: 提供受控原生边界，记录候选输入和外部派工。
class ReplayBoundary:
    # LLM: 每个用例有独立状态和事务回执，避免测试共享数据。
    # 函数用途: 初始化合成记录与计量。
    def __init__(self, home):
        self.home = home
        self.observation = BusinessObservationService(home, owner_id='local/main', cursor_key=b'synthetic-cursor-key'.ljust(32, b'x'))
        self.tables = {'soc_alerts': {}, 'soc_feedback': {}, 'soc_replay_evaluations': {}, 'async_jobs': {}}
        self.receipts = {}
        self.agents = {}
        self.jobs = {}
        self.instructions = []
        self.allowed = True
        self.timings = []
        self.actual = {'coverage': 'unknown', 'currency': None, 'cost': None}
        self.created = []

    # LLM: 替身保留回执摘要冲突语义；生产实现仍使用 W0 store。
    # 函数用途: 执行领域事务回调并保存回执。
    def transaction(self, collection, mutate, *, idempotency_key, params_digest):
        key = (collection, idempotency_key)
        if key in self.receipts:
            digest, result = self.receipts[key]
            if digest != params_digest:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            return deepcopy(result)
        rows = deepcopy(self.tables[collection])
        result = mutate(rows)
        self.tables[collection] = rows
        self.receipts[key] = (params_digest, deepcopy(result))
        return deepcopy(result)

    # LLM: 精确查找返回独立记录，避免锁外对象原地改变测试存储。
    # 函数用途: 读取测试集合。
    def list(self, collection):
        return deepcopy(list(self.tables[collection].values()))

    # LLM: 不把缺失 ID 变成空成功。
    # 函数用途: 读取合成记录。
    def get(self, collection, record_id):
        return deepcopy(self.tables[collection].get(record_id))

    # LLM: 单测显式触发 owner 与实时拒绝，不从文案推断权限。
    # 函数用途: 模拟下游实时访问检查。
    def access(self, call, row):
        if not self.allowed:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if row.get('owner_id', call.owner_id) != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)

    # LLM: frozen 调用严格按动作分支，未声明动作不返回假成功。
    # 函数用途: 提供可记录的配置档、候选创建和计量读口。
    def frozen(self, call, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'model_profiles.list':
            return {'items': [], 'next_cursor': None}
        if action == 'background_agents.create':
            if idempotency_key not in self.agents:
                self.agents[idempotency_key] = {'agent_id': 'candidate-' + str(len(self.agents))}
                self.created.append(deepcopy(params))
            return {'agent': self.agents[idempotency_key]}
        if action == 'executions.list':
            return {'items': deepcopy(self.timings), 'next_cursor': None, 'coverage': 'complete'}
        if action == 'usage.get':
            return {'actual': deepcopy(self.actual)}
        raise AssertionError(action)

    # LLM: 模型边界记录输入，供标签泄露与候选归属断言核对。
    # 函数用途: 为合成候选创建一次可重放作业。
    def start_job(self, call, kind, instruction, *, space_id=None, agent_id=None):
        if call.idempotency_key not in self.jobs:
            self.instructions.append(instruction)
            self.jobs[call.idempotency_key] = {'job_id': 'job-' + str(len(self.jobs)),
                'task_id': 'task-' + str(len(self.jobs)), 'space_id': space_id,
                'agent_id': agent_id, 'kind': kind, 'state': 'accepted', 'result': None,
                'owner_id': call.owner_id, 'updated_at': '2026-11-02T12:00:00Z'}
        job = self.jobs[call.idempotency_key]
        self.tables['async_jobs'][job['job_id']] = job
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-' + job['job_id'],
            'job_id': job['job_id'], 'task_id': job['task_id']})

    # LLM: W0 作业是唯一状态源，切片不得直接修改其集合。
    # 函数用途: 提供 services.invoke 的合成作业读取。
    def invoke(self, action, params, context):
        assert action == 'async_jobs.get'
        return deepcopy(next(job for job in self.jobs.values() if job['job_id'] == params['job_id']))


# LLM: 仅单测替换显式基础端口，保留真实模块的错误映射和冻结 schema；fixture 结束自动恢复。
# 函数用途: 建立完全隔离的领域测试边界。
@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    env = ReplayBoundary(tmp_path)
    from agent_py_agent.agent.business_spaces import v13_store
    from agent_py_agent.agent.gateway_parts import business_api_v13

    monkeypatch.setattr(business_api_v13, 'v13_require_access', env.access)
    monkeypatch.setattr(business_api_v13, 'v13_resolve_space',
                        lambda call: {'space_id': call.params.get('space_id', 'space-a')})
    monkeypatch.setattr(business_api_v13, 'v13_call_frozen', env.frozen)
    monkeypatch.setattr(business_api_v13, 'v13_start_model_job', env.start_job)
    monkeypatch.setattr(v13_store, 'v13_store_for', lambda call, scope: env)
    return env


# LLM: 时间、标签和原 verdict 均为显式合成值。
# 函数用途: 为单测装入三个已复核样本及窗口外记录。
def _seed(boundary):
    for index, label in enumerate(('true_positive', 'benign', 'true_positive')):
        alert_id = 'alert-' + str(index)
        boundary.tables['soc_alerts'][alert_id] = {'alert_id': alert_id, 'space_id': 'space-a',
            'owner_id': 'local/main', 'title': alert_id, 'description': '合成历史事实',
            'severity': 'medium', 'created_at': f'2026-11-0{index + 1}T10:00:00Z', 'verdict': label,
            'disposition': 'private_disposition', 'tags': ['private_tag'], 'evidence_refs': ['private-ref']}
        boundary.tables['soc_feedback'][alert_id] = {'override_id': alert_id, 'alert_id': alert_id,
            'space_id': 'space-a', 'owner_id': 'local/main', 'original_verdict': 'benign',
            'override_verdict': label, 'created_at': '2026-11-02T12:00:00Z', 'revision': 0}


# LLM: 所有测试调用使用显式测试身份和可信固定时钟。
# 函数用途: 生成 F10 领域调用。
def _call(boundary, action, params, key=''):
    return ReplayCall('evaluations.replay.' + action, params, idempotency_key=key,
        services=SimpleNamespace(invoke=boundary.invoke, observation=boundary.observation), owner_home=boundary.home,
        clock=lambda: 1793620800.0)


# LLM: 参数遵循精确闭合 schema，不塞派工私有字段。
# 函数用途: 返回合成回放启动参数。
def _params():
    return {'space_id': 'space-a', 'candidate': {'model_profile_id': 'candidate-profile'},
        'replay_window': {'since': '2026-11-01T00:00:00Z', 'until': '2026-11-04T00:00:00Z'}}


# LLM: 测试使用权威输入 schema 验证产品 DTO，不复制另一份 schema。
# 函数用途: 验证动作结果契约。
def _validate(action, data, accepted=False):
    spec = registered_spec('evaluations.replay.' + action)
    schema = spec.accepted_result_schema if accepted else spec.result_schema
    validate_object(thaw(data), schema)


# LLM: 能力发现统计可见标签，且结果满足公开闭合 schema。
# 函数用途: 验证最小示例能力发现。
def test_capabilities_example_empty_params_counts_labelled_alerts(boundary):
    _seed(boundary)
    result = replay.capabilities(_call(boundary, 'capabilities', {}))
    assert result['datasets'][0]['labelled_count'] == 3
    assert result['max_sample_size'] == 1000
    _validate('capabilities', result)


# LLM: 检查模型真实收到的结构化输入，不只检查调用存在。
# 函数用途: 验证窗口采样、最新标签、候选模型与标签隔离。
def test_start_samples_labelled_alerts_in_window_and_withholds_labels(boundary):
    _seed(boundary)
    boundary.tables['soc_feedback']['latest'] = {**boundary.tables['soc_feedback']['alert-2'],
        'override_id': 'latest', 'override_verdict': 'benign', 'created_at': '2026-11-03T12:00:00Z'}
    call = _call(boundary, 'start', {**_params(), 'sample_size': 2}, 'start-synthetic-key')
    result = replay.start(call)
    _validate('start', result.data, accepted=True)
    row = boundary.tables['soc_replay_evaluations'][result.data['evaluation_id']]
    assert row['sample_alert_ids'] == ['alert-2', 'alert-1']
    assert row['decisions'][0]['expected_verdict'] == 'benign'
    facts = json.loads(boundary.instructions[0].split('\n', 1)[1])
    assert [alert['alert_id'] for alert in facts['alerts']] == row['sample_alert_ids']
    assert all('verdict' not in alert and 'disposition' not in alert and 'tags' not in alert
               and 'evidence_refs' not in alert for alert in facts['alerts'])
    assert boundary.created == [{'space_id': 'space-a', 'name': row['evaluation_id'],
        'purpose': '独立离线评测候选', 'after_task': 'retire', 'model_profile_id': 'candidate-profile'}]
    assert row['agent_id'] == next(iter(boundary.jobs.values()))['agent_id']
    assert replay.start(call).data == result.data
    assert len(boundary.jobs) == len(boundary.agents) == len(boundary.instructions) == 1
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        replay.start(replace(call, params={**call.params, 'sample_size': 1}))


# LLM: 对空数据和逆序窗口要明确拒绝，不能制造空成功作业。
# 函数用途: 验证无标签和窗口错误。
def test_no_labels_and_invalid_window(boundary):
    with pytest.raises(HostAPIError) as error:
        replay.start(_call(boundary, 'start', _params(), 'empty-synthetic-key'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')
    assert not boundary.jobs and not boundary.agents
    params = _params()
    params['replay_window'] = {'since': '2026-11-04T00:00:00Z', 'until': '2026-11-01T00:00:00Z'}
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        replay.start(_call(boundary, 'start', params, 'inverse-window-key'))


# LLM: 完成结果与真实执行观测显式装入替身，不用模型正文推导状态。
# 函数用途: 完成一个合成评测作业。
def _complete(boundary, verdicts=('true_positive', 'true_positive', 'benign')):
    _seed(boundary)
    accepted = replay.start(_call(boundary, 'start', _params(), 'metrics-synthetic-key'))
    row = boundary.tables['soc_replay_evaluations'][accepted.data['evaluation_id']]
    job = next(iter(boundary.jobs.values()))
    job['state'] = 'completed'
    job['result'] = {'decisions': [{'alert_id': alert_id, 'verdict': verdict, 'rationale': '合成依据'}
        for alert_id, verdict in zip(row['sample_alert_ids'], verdicts)]}
    return row['evaluation_id'], job


# LLM: 基线与候选用相同样本；时延只能来自实际时序而非模型自报。
# 函数用途: 验证准确率、精确率、召回率、延迟与真实用量成本。
def test_get_finalizes_metrics_from_job_result(boundary):
    evaluation_id, job = _complete(boundary)
    boundary.timings = [{'task_id': job['task_id'], 'started_at': '2026-11-02T12:00:00Z',
                         'ended_at': '2026-11-02T12:00:02Z'}]
    boundary.actual = {'coverage': 'complete', 'currency': 'USD', 'cost': 0.125}
    result = replay.get(_call(boundary, 'get', {'evaluation_id': evaluation_id}))
    assert result['evaluation']['metrics'] == {'accuracy': 1 / 3, 'precision': 1 / 2,
        'recall': 1 / 2, 'baseline_accuracy': 1 / 3,
        'latency_ms_p50': 2000, 'latency_ms_p95': 2000, 'cost_usd': 0.125}
    assert result['evaluation']['state'] == 'completed'
    assert not {'owner_id', 'sample_alert_ids', 'decisions'} & result['evaluation'].keys()
    _validate('get', result)
    stored = deepcopy(boundary.tables['soc_replay_evaluations'][evaluation_id])
    job['state'] = 'running'
    assert replay.get(_call(boundary, 'get', {'evaluation_id': evaluation_id})) == result
    assert boundary.tables['soc_replay_evaluations'][evaluation_id] == stored


# LLM: 不完整来源、估算成本和缺失时间不能变成零值或可比较指标。
# 函数用途: 验证未知用量和时延保持 null。
def test_unknown_metrics_remain_null(boundary):
    evaluation_id, job = _complete(boundary)
    boundary.actual = {'coverage': 'partial', 'currency': 'USD', 'cost': 9.0}
    boundary.timings = [{'task_id': job['task_id'], 'started_at': None, 'ended_at': None}]
    metrics = replay.get(_call(boundary, 'get', {'evaluation_id': evaluation_id}))['evaluation']['metrics']
    assert metrics['latency_ms_p50'] is None
    assert metrics['latency_ms_p95'] is None
    assert metrics['cost_usd'] is None
    assert all(item['latency_ms'] is None for item in boundary.tables['soc_replay_evaluations'][evaluation_id]['decisions'])


# LLM: 少报、多报、重复或换 ID 均不能作为同一样本评测完成。
# 函数用途: 验证候选 ID 集合与结构完整性。
@pytest.mark.parametrize('change', ['missing', 'extra', 'duplicate', 'foreign', 'extra_field'])
def test_invalid_sample_result_fails_without_metrics(boundary, change):
    evaluation_id, job = _complete(boundary)
    rows = job['result']['decisions']
    if change == 'missing':
        rows.pop()
    elif change == 'extra':
        rows.append({'alert_id': 'unknown-id', 'verdict': 'benign', 'rationale': ''})
    elif change == 'duplicate':
        rows[1] = deepcopy(rows[0])
    elif change == 'foreign':
        rows[1]['alert_id'] = 'unknown-id'
    else:
        rows[1]['latency_ms'] = 1
    result = replay.get(_call(boundary, 'get', {'evaluation_id': evaluation_id}))
    assert result['evaluation']['state'] == 'failed'
    assert result['evaluation']['metrics']['accuracy'] is None
    with pytest.raises(HostAPIError) as error:
        replay.export(_call(boundary, 'export', {'evaluation_id': evaluation_id, 'format': 'json'}, 'export-invalid-key'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')


# LLM: 使用原 HMAC cursor 服务验证作用域，而不是自制总返回成功的游标。
# 函数用途: 验证分页、mismatch、跨过滤/主体及篡改游标拒绝。
def test_decisions_page_and_mismatch_filter(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call(boundary, 'decisions', {'evaluation_id': evaluation_id, 'limit': 1})
    first = replay.decisions(call)
    _validate('decisions', first)
    assert first['items'][0]['alert_id'] == 'alert-2'
    assert first['next_cursor'] is not None
    second = replay.decisions(replace(call, params={**call.params, 'cursor': first['next_cursor']}))
    assert second['items'][0]['alert_id'] == 'alert-1'
    mismatch = replay.decisions(replace(call, params={'evaluation_id': evaluation_id, 'mismatches_only': True}))
    assert [item['alert_id'] for item in mismatch['items']] == ['alert-1', 'alert-0']
    for params, subject in (({**call.params, 'cursor': first['next_cursor'], 'mismatches_only': True}, 'analyst'),
                            ({**call.params, 'cursor': first['next_cursor']}, 'other-subject'),
                            ({**call.params, 'cursor': 'invalid-cursor'}, 'analyst')):
        with pytest.raises(BusinessError) as error:
            replay.decisions(replace(call, params=params, subject_id=subject))
        assert _error_code(error.value)[:2] == ('CURSOR_EXPIRED', 410)


# LLM: 单项不存在、其它 owner 与实时撤权须分别保留其边界错误。
# 函数用途: 验证读取和导出的 scope 检查先于披露。
def test_owner_and_live_scope_errors(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call(boundary, 'get', {'evaluation_id': evaluation_id})
    with pytest.raises(HostAPIError) as error:
        replay.get(replace(call, owner_id='local/other'))
    assert (error.value.status, error.value.code) == (404, 'RESOURCE_NOT_FOUND')
    with pytest.raises(HostAPIError) as error:
        replay.get(replace(call, params={'evaluation_id': 'missing-id'}))
    assert error.value.code == 'RESOURCE_NOT_FOUND'
    boundary.allowed = False
    with pytest.raises(HostAPIError) as error:
        replay.get(call)
    assert (error.value.status, error.value.code) == (403, 'CAPABILITY_DENIED')


# LLM: JSON 之外的正文、错误类型和未知字段不能进入结构化结果。
# 函数用途: 验证纯 JSON job finalizer 的拒绝行为。
@pytest.mark.parametrize('text', ['not-json', '[]', '{}', '{"decisions":null}',
    '{"decisions":[],"decisions":[]}',
    '{"decisions":[{"alert_id":"a","verdict":"benign","rationale":"","extra":1}]}'])
def test_finalize_replay_rejects_invalid_json(text):
    with pytest.raises(ValueError):
        replay.finalize_replay(text)


# LLM: 仅任务引用读口为合成依赖；空间、活跃上下文、配额、登记和 blob 读写均用生产实现。
# 函数用途: 验证 JSON/CSV 评测报告进入真实 artifact store 并能完整读回。
def test_export_json_csv_artifacts(boundary):
    from agent_py_agent.agent.gateway_parts.business_artifacts_http import artifact_content

    evaluation_id, job = _complete(boundary)
    replay.get(_call(boundary, 'get', {'evaluation_id': evaluation_id}))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/main', owner_home_dir=boundary.home),
                           config=SimpleNamespace(business_spaces_enabled=True))
    services = BusinessServices(host)
    context = bind_business_context(host, subject_id='analyst', authorize=lambda action, facts: boundary.allowed)
    space = services.spaces.create({'name': '回放产物测试'}, actor=services.actor(context),
                                   idempotency_key='artifact-space-synthetic')
    boundary.tables['soc_replay_evaluations'][evaluation_id]['space_id'] = space['space_id']
    host.business_reference_reader = lambda kind, ref, actor: ({'owner_id': 'local/main', 'space_id': space['space_id']}
        if kind == 'task' and ref == job['task_id'] else None)
    exports = []
    for format_name in ('json', 'csv'):
        call = replace(_call(boundary, 'export', {'evaluation_id': evaluation_id, 'format': format_name},
                             'artifact-export-' + format_name), services=services, context=context)
        result = replay.export(call)
        _validate('export', result)
        artifact = result['export']
        exports.append(artifact)
        status, headers, content = artifact_content(artifact['artifact_id'], artifact['version_ref'], context, services)
        assert status == 200
        assert len(content) == artifact['size_bytes']
        assert hashlib.sha256(content).hexdigest() == artifact['content_sha256']
        assert replay.export(call) == result
        if format_name == 'json':
            report = json.loads(content)
            assert report['evaluation']['evaluation_id'] == evaluation_id
            assert len(report['decisions']) == 3
            assert report['metrics']['accuracy'] == 1 / 3
            assert report['metrics']['cost_usd'] is None
            assert 'owner_id' not in report['evaluation']
        else:
            rows = list(csv.DictReader(content.decode().splitlines()))
            assert len(rows) == 3
            assert {row['alert_id'] for row in rows} == {'alert-0', 'alert-1', 'alert-2'}
            assert {row['evaluation_id'] for row in rows} == {evaluation_id}
            assert all(float(row['metric_accuracy']) == 1 / 3 for row in rows)
        with pytest.raises(HostAPIError) as error:
            replay.export(replace(call, params={**call.params, 'format': 'csv' if format_name == 'json' else 'json'}))
        assert (error.value.status, error.value.code) == (409, 'IDEMPOTENCY_CONFLICT')
    assert exports[0]['artifact_id'] != exports[1]['artifact_id']
    boundary.allowed = False
    with pytest.raises(HostAPIError) as error:
        artifact_content(exports[0]['artifact_id'], exports[0]['version_ref'], context, services)
    assert error.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 未完成任务不能导出，不能把空结果包装为完成报告。
# 函数用途: 验证异步未完成导出的准确冲突错误。
def test_export_before_completion_is_409(boundary):
    _seed(boundary)
    accepted = replay.start(_call(boundary, 'start', _params(), 'unfinished-synthetic'))
    with pytest.raises(HostAPIError) as error:
        replay.export(_call(boundary, 'export', {'evaluation_id': accepted.data['evaluation_id'], 'format': 'json'},
                            'unfinished-export'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')


# LLM: 列表和详情共用同一规范视图，列表游标绑定过滤和结果版本。
# 函数用途: 验证评测列表、详情、state 过滤和分页冲突。
def test_list_and_get(boundary):
    _seed(boundary)
    first = replay.start(_call(boundary, 'start', _params(), 'first-evaluation-key')).data['evaluation_id']
    second = replay.start(_call(boundary, 'start', _params(), 'second-evaluation-key')).data['evaluation_id']
    call = _call(boundary, 'list', {'space_id': 'space-a', 'limit': 1})
    page = replay.list_evaluations(call)
    _validate('list', page)
    assert len(page['items']) == 1
    assert page['next_cursor'] is not None
    more = replay.list_evaluations(replace(call, params={**call.params, 'cursor': page['next_cursor']}))
    assert {row['evaluation_id'] for row in page['items'] + more['items']} == {first, second}
    details = replay.get(_call(boundary, 'get', {'evaluation_id': first}))
    assert details['evaluation'] in page['items'] + more['items']
    assert replay.list_evaluations(replace(call, params={'state': 'completed'}))['items'] == []
    with pytest.raises(BusinessError) as error:
        replay.list_evaluations(replace(call, params={**call.params, 'state': 'running', 'cursor': page['next_cursor']}))
    assert _error_code(error.value)[:2] == ('CURSOR_EXPIRED', 410)


# LLM: 初次回执固定标签；后续反馈或样本删除不能改变同键派工输入。
# 函数用途: 验证幂等重放不重新采样或重新派工。
def test_start_receipt_keeps_sample_after_source_changes(boundary):
    _seed(boundary)
    call = _call(boundary, 'start', _params(), 'stable-snapshot-key')
    accepted = replay.start(call)
    boundary.tables['soc_alerts'].clear()
    boundary.tables['soc_feedback'].clear()
    assert replay.start(call).data == accepted.data
    assert len(boundary.instructions) == 1
    assert len(boundary.jobs) == 1
    assert len(boundary.agents) == 1


# LLM: UTC 与不同偏移表达同一时刻，闭区间两端均应包含。
# 函数用途: 验证时间窗按实际时刻筛选且未知日期不参与采样。
def test_time_window_uses_offsets_and_closed_bounds(boundary):
    _seed(boundary)
    boundary.tables['soc_alerts']['alert-1']['created_at'] = '2026-11-02T18:00:00+08:00'
    boundary.tables['soc_alerts']['alert-2']['created_at'] = None
    params = {**_params(), 'replay_window': {'since': '2026-11-02T10:00:00Z', 'until': '2026-11-02T10:00:00Z'}}
    accepted = replay.start(_call(boundary, 'start', params, 'offset-window-key'))
    row = boundary.tables['soc_replay_evaluations'][accepted.data['evaluation_id']]
    assert row['sample_alert_ids'] == ['alert-1']
    assert row['sample_size'] == 1


# LLM: 只有真实人工反馈可作真值，模型自己判定的告警仍属于未标注集合。
# 函数用途: 回归评测不会用 AI verdict 自我打分。
def test_unreviewed_model_verdict_is_not_ground_truth(boundary):
    _seed(boundary)
    boundary.tables['soc_feedback'].clear()
    for row in boundary.tables['soc_alerts'].values():
        row['ai_confidence'] = 0.95
    result = replay.capabilities(_call(boundary, 'capabilities', {}))
    assert result['datasets'][0]['record_count'] == 3
    assert result['datasets'][0]['labelled_count'] == 0
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        replay.start(_call(boundary, 'start', _params(), 'unreviewed-synthetic'))


# LLM: 校验钩子读取真实固定样本关系，不改原 job 状态；W0 负责记录 JOB_RESULT_INVALID。
# 函数用途: 验证同样本结果发布前钩子的拒绝边界。
def test_result_hook_checks_sample_ids_before_job_publication(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call(boundary, 'get', {'evaluation_id': evaluation_id})
    assert replay.validate_replay_result(call, job, job['result']) is True
    wrong = deepcopy(job['result'])
    wrong['decisions'][0]['alert_id'] = 'different-sample'
    with pytest.raises(ValueError):
        replay.validate_replay_result(call, job, wrong)
    assert job['state'] == 'completed'


# LLM: 仅真实 HTTP 返回才算原生边界证据；统一使用 harness 的准确用户和信封。
# 函数用途: 发起一次回放相关原生请求。
def _wire(server, action, params, key=None, revision=None, user='business-alice'):
    from agent_py_agent.tests.test_v13_w0_harness import v13_call

    return v13_call(server, action, params, user=user, idempotency_key=key, expected_revision=revision)


# LLM: 不把 HTTP 成功但业务失败的信封当完成。
# 函数用途: 断言标准完成信封并取出投影。
def _completed(response):
    status, body = response
    assert status == 200, body
    assert body['state'] == 'completed', body
    assert body['error'] is None, body
    return body['data']


# LLM: 失败必须保留准确状态和结构化错误码，不能变成空成功。
# 函数用途: 断言原生错误信封。
def _rejected(response, status, code):
    actual, body = response
    assert actual == status, body
    assert body['state'] == 'failed', body
    assert body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: W0 fixture 使用真正 Gateway 与隔离 HOME；空间只创建一次。
# 函数用途: 验证能力发现最小示例、无标签与参数闭合错误的 HTTP 往返。
def test_http_capabilities_empty_and_no_label_conflict(v13_native):
    from agent_py_agent.tests.test_v13_w0_harness import v13_space

    server, agent = v13_native
    space_id = v13_space(server)
    data = _completed(_wire(server, 'evaluations.replay.capabilities', {}))
    _validate('capabilities', data)
    assert data['datasets'][0]['labelled_count'] == 0
    params = {**_params(), 'space_id': space_id}
    _rejected(_wire(server, 'evaluations.replay.start', params, uuid.uuid4().hex), 409, 'STATE_CONFLICT')
    _rejected(_wire(server, 'evaluations.replay.capabilities', {'unknown': True}), 400, 'INVALID_SCHEMA')
    _rejected(_wire(server, 'evaluations.replay.get', {'evaluation_id': 'unknown-evaluation'}),
              404, 'RESOURCE_NOT_FOUND')


# LLM: 模型可使用合成 echo，但候选创建、派工、存储、权限、游标和信封均经过真实 HTTP。
# 函数用途: 验证启动到详情/决策/导出状态冲突的原生关键链路。
def test_http_start_list_get_decisions_owner_idempotency(v13_native):
    from agent_py_agent.tests.test_v13_w0_harness import v13_space
    from agent_py_agent.tests.test_v13_w8_alerts import echo_profile

    server, agent = v13_native
    space_id = v13_space(server)
    configured = echo_profile(v13_native)
    profiles = _completed(_wire(server, 'model_profiles.list', {'limit': 100}))['items']
    profile = next(row['profile_id'] for row in profiles if row['enabled'] and row['capability'] == 'agentic')
    assert profile == configured
    ingest = _completed(_wire(server, 'soc.alerts.ingest', {'space_id': space_id, 'alerts': [
        {'external_ref': 'replay-http-synthetic', 'title': '合成回放事件', 'severity': 'medium',
         'occurred_at': '2026-11-02T10:00:00Z'}]}, uuid.uuid4().hex))
    alert_id = ingest['results'][0]['alert_id']
    alert = _completed(_wire(server, 'soc.alerts.get', {'alert_id': alert_id}))['alert']
    _completed(_wire(server, 'soc.alerts.update', {'alert_id': alert_id, 'verdict': 'true_positive'},
                     uuid.uuid4().hex, alert['revision']))
    params = {**_params(), 'space_id': space_id, 'candidate': {'model_profile_id': profile}}
    key = uuid.uuid4().hex
    status, accepted = _wire(server, 'evaluations.replay.start', params, key)
    assert status == 202, accepted
    assert accepted['state'] == 'accepted' and accepted['error'] is None, accepted
    _validate('start', accepted['data'], accepted=True)
    evaluation_id = accepted['data']['evaluation_id']
    replay_status, replay_body = _wire(server, 'evaluations.replay.start', params, key)
    assert replay_status == 202 and replay_body['data'] == accepted['data'], replay_body
    _rejected(_wire(server, 'evaluations.replay.start', {**params, 'sample_size': 1}, key),
              409, 'IDEMPOTENCY_CONFLICT')
    details = _completed(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}))
    _validate('get', details)
    assert details['evaluation']['sample_size'] == 1
    assert details['evaluation']['agent_id'] is not None
    candidate = _completed(_wire(server, 'background_agents.get',
                                 {'agent_id': details['evaluation']['agent_id']}))['agent']
    assert candidate['model_profile_id'] == profile
    _validate('list', _completed(_wire(server, 'evaluations.replay.list', {'space_id': space_id})))
    _validate('decisions', _completed(_wire(server, 'evaluations.replay.decisions', {'evaluation_id': evaluation_id})))
    _rejected(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}, user='business-bob'),
              404, 'RESOURCE_NOT_FOUND')
    if details['evaluation']['state'] != 'completed':
        _rejected(_wire(server, 'evaluations.replay.export', {'evaluation_id': evaluation_id, 'format': 'json'},
                        uuid.uuid4().hex), 409, 'STATE_CONFLICT')


# LLM: 唯一替身是本地模型输出；原任务执行、job 校验、指标、上传与HTTP下载必须全部真实往返。
# 函数用途: 验证同样本成功完成后 JSON/CSV 导出的完整原生链路。
@pytest.mark.parametrize('wrong_ids', [False, True])
def test_http_structured_candidate_to_metrics_and_download(v13_native, monkeypatch, wrong_ids):
    import http.client
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile, invoke, seed_alert
    from agent_py_agent.tests.test_v13_w8_cases import completed, space

    server, agent = v13_native
    sid = space(v13_native)
    configured = echo_profile(v13_native)
    profiles = completed(_wire(server, 'model_profiles.list', {'limit': 100}))['items']
    profile = next(row['profile_id'] for row in profiles if row['enabled'] and row['capability'] == 'agentic')
    assert profile == configured
    row = seed_alert(v13_native, 'native-label-one', verdict='true_positive', confidence=0.9)
    completed(invoke(v13_native, 'feedback.submit_override', {'alert_id': row['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工固定标签'}))
    captured = []

    # LLM: 合成模型不访问网络，不代替调度、存储、身份校验和HTTP入口。
    # 函数用途: 返回指定的规范决策或错误样本标识供完整链路复核。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        captured.append(prompt)
        return ModelResponse(backend='echo', text=json.dumps({'decisions': [{
            'alert_id': 'different-native-sample' if wrong_ids else row['alert_id'],
            'verdict': 'benign', 'rationale': '合成独立判断'}]}))

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    params = {'space_id': sid, 'candidate': {'model_profile_id': profile},
              'replay_window': {'since': None, 'until': None}}
    status, accepted = _wire(server, 'evaluations.replay.start', params, uuid.uuid4().hex)
    assert status == 202, accepted
    evaluation_id = accepted['data']['evaluation_id']
    drive_job(v13_native, accepted['data']['job_id'])
    assert captured, '真实任务没有到达合成模型入口'
    for attempt in range(200):
        job = completed(_wire(server, 'async_jobs.get', {'job_id': accepted['data']['job_id']}))
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert captured, '真实任务没有到达合成模型入口'
    assert all('expected_verdict' not in prompt and 'ai_confidence' not in prompt for prompt in captured)
    evaluation = completed(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}))['evaluation']
    if wrong_ids:
        assert job['state'] == 'failed' and job['error']['code'] == 'JOB_RESULT_INVALID', job
        assert evaluation['state'] == 'failed' and evaluation['metrics']['accuracy'] is None
        return
    assert job['state'] == evaluation['state'] == 'completed', job
    assert evaluation['metrics']['accuracy'] == 1 and evaluation['metrics']['baseline_accuracy'] == 0
    decisions = completed(_wire(server, 'evaluations.replay.decisions', {'evaluation_id': evaluation_id}))['items']
    assert len(decisions) == 1 and decisions[0]['match'] is True
    for fmt in ('json', 'csv'):
        result = completed(_wire(server, 'evaluations.replay.export', {'evaluation_id': evaluation_id,
            'format': fmt}, uuid.uuid4().hex))['export']
        metadata = completed(_wire(server, 'artifacts.get', {'artifact_id': result['artifact_id'],
            'version_ref': result['version_ref']}))
        assert metadata['available'] and metadata['artifact']['content_sha256'] == result['content_sha256']
        connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
        try:
            connection.request('GET', '/client/business/artifacts/' + result['artifact_id']
                + '/content?version_ref=' + result['version_ref'], headers={
                    'X-Gateway-Token': server._local_client_credential, 'X-User-Id': 'business-alice',
                    'X-Channel': 'feishu'})
            response = connection.getresponse()
            content = response.read()
            assert response.status == 200 and len(content) == result['size_bytes']
        finally:
            connection.close()
        assert hashlib.sha256(content).hexdigest() == result['content_sha256']
        if fmt == 'json':
            assert json.loads(content)['decisions'][0]['alert_id'] == row['alert_id']
        else:
            assert list(csv.DictReader(content.decode().splitlines()))[0]['match'] == 'True'
