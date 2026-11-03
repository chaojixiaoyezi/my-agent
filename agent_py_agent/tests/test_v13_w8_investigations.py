"""调查领域定向测试；原媒体入库和下载为真实实现，缺 W0 时仅使用显式源替身。"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w8_alerts as shared
from agent_py_agent.agent.business_spaces import v13_w8_investigations as investigation
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_observability import (
    BusinessObservationService,
    ObservationPage,
    ObservationRecord,
)
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_artifacts_http as media
from agent_py_agent.agent.gateway_parts.business_artifacts_http import artifact_content
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.tests import test_v13_w0_harness
from agent_py_agent.tests.test_mega_business_api_actions import native_request

native_http = test_v13_w0_harness.v13_native


# LLM: 测试 DTO 只描述输入，不冒充发行的业务授权上下文。
# 类用途: 在 W0 尚未同步时验证领域边界。
@dataclass(frozen=True)
class InvestigationCall:
    action: str
    params: dict
    context: object
    services: object
    owner_home: object
    owner_id: str
    subject_id: str
    request_id: str = 'investigation-request'
    idempotency_key: str = 'investigation-key-001'
    expected_revision: int | None = None
    clock: object = None


# LLM: 全部测试资料是合成数据，HOME 不指向真实个人资料。
# 函数用途: 构建真实 W1/媒体服务和可控制的调查事实源。
@pytest.fixture
def investigation_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path / 'alice',
        owner_id='local/user/alice', config_dir=tmp_path / 'alice' / 'config'),
        config=SimpleNamespace(business_spaces_enabled=True))
    services = api.BusinessServices(host)
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    # LLM: 真实观察服务负责 HMAC 游标，媒体读口从真实 registry 读回而非复制上传返回值。
    # 函数用途: 为有限测试宿主提供显式 artifacts.get 来源。
    def artifact_reader(params):
        records, size = media._registry(services)
        record = records.get(params['artifact_id'])
        if record is None or params.get('version_ref', record.metadata['version_ref']) != record.metadata['version_ref']:
            return ObservationPage()
        artifact = media._view(record)
        value = {'artifact': artifact, 'available': artifact['available'],
                 'version_ref': artifact['version_ref'], 'read_ref': artifact['artifact_id']}
        return ObservationPage(records=(ObservationRecord(value, context.owner_id,
            space_id=artifact['space_id'], task_id=artifact['task_id']),))
    services.projection = BusinessObservationService(host.home_paths.owner_home_dir,
        owner_id=context.owner_id, readers={'artifacts.get': artifact_reader})
    status, result = api.execute_business({'action': 'spaces.create', 'request_id': 'space-create',
        'params': {'name': '合成案件空间'}, 'idempotency_key': 'space-key-for-investigation'}, context, services)
    assert status == 200, result
    sid = result['data']['space_id']
    now = [datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()]
    call = InvestigationCall('soc.cases.investigate', {'space_id': sid, 'case_id': 'CASE-2026-001',
        'mode': 'autonomous_agent'}, context, services, host.home_paths.owner_home_dir,
        context.owner_id, context.subject_id, clock=lambda: now[0])
    stamp = shared.w8_now(call)
    case = {'case_id': 'CASE-2026-001', 'owner_id': call.owner_id, 'space_id': sid,
        'title': '<b>合成案件</b>', 'description': 'password=synthetic-report-secret',
        'status': 'open', 'severity': 'high', 'assignee': None, 'alert_ids': ['alert-one'],
        'merged_into': None, 'tags': [], 'custom_values': {}, 'closed_at': None,
        'revision': 0, 'created_at': stamp, 'updated_at': stamp}
    alert = {'alert_id': 'alert-one', 'owner_id': call.owner_id, 'space_id': sid,
        'title': '合成告警', 'description': 'password=synthetic-alert-secret', 'severity': 'high',
        'status': 'new', 'entities': [{'type': 'host', 'value': 'synthetic-host'}],
        'techniques': [], 'evidence_refs': []}
    tables = {'soc_cases': {case['case_id']: case}, 'soc_alerts': {alert['alert_id']: alert},
        'soc_investigations': {}, 'soc_investigation_shares': {}, 'async_jobs': {}}
    receipts, calls, sources = {}, [], {}
    job = {'job_id': 'job-one', 'task_id': 'task-one', 'agent_id': 'agent-one',
        'state': 'accepted', 'result': None, 'operation_id': 'operation-one', 'updated_at': stamp}
    task = {'task_id': 'task-one', 'thread_id': 'agent-one', 'status': 'queued',
        'result_refs': [], 'owner_id': call.owner_id, 'space_id': sid}
    host.business_reference_reader = lambda kind, rid, actor: task if rid == task['task_id'] else None

    # LLM: 仅模拟 W0 未提供的存储端口，领域函数仍使用真正公共 helper 的分页。
    # 函数用途: 在测试中执行明确的 owner/space 可见性检查。
    def access(current, row):
        if row.get('owner_id') != current.owner_id or (
                current.params.get('space_id') is not None and row.get('space_id') not in (
                    None, current.params['space_id'])):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return row

    # LLM: 返回独立记录，防止测试无锁共享引用掩盖覆盖问题。
    # 函数用途: 读取合成集合的一条可见记录。
    def get(current, col, rid):
        row = tables[col].get(rid)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return deepcopy(access(current, row))

    # LLM: 测试单独列出可见记录，不让其它 owner 混入聚合。
    # 函数用途: 返回完整合成列表。
    def rows(current, col):
        return [deepcopy(row) for row in tables[col].values()
                if row.get('owner_id') == current.owner_id and (
                    'space_id' not in current.params or row.get('space_id') == current.params['space_id'])]

    # LLM: 回执替身明确绑定参数，不能靠忽略幂等冲突让测试变绿。
    # 函数用途: 原子提交合成记录和回执。
    def transaction(current, collections, mutate):
        key = (current.owner_id, current.subject_id, current.action, current.idempotency_key)
        digest = json.dumps([current.params, current.expected_revision], sort_keys=True)
        if key in receipts:
            old_digest, data = receipts[key]
            if old_digest != digest:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            return deepcopy(data)
        local = {col: deepcopy(tables[col]) for col in collections}
        data = mutate(local)
        tables.update(local)
        receipts[key] = digest, deepcopy(data)
        return data

    # LLM: 冻结源替身必须首先通过真实闭合参数 schema。
    # 函数用途: 提供分页事实并把媒体读口交回真实 services。
    def frozen(current, action, params):
        calls.append((action, deepcopy(params)))
        validate_object(params, business_action_spec(action).params_schema)
        if action == 'tasks.get':
            if task.get('unavailable'):
                raise HostAPIError('HOST_NOT_READY', 503)
            return {'task': deepcopy(task)}
        if action == 'artifacts.get':
            return services.invoke(action, params, context)
        items = sources.get(action, [])
        start = int(params.get('cursor', '0'))
        end = start + params.get('limit', 50)
        cursor = str(end) if end < len(items) else None
        return {'events' if action == 'events.list' else 'items': deepcopy(items[start:end]),
                'next_cursor': cursor, 'coverage': 'partial' if cursor else 'complete'}

    # LLM: 记录模型提交次数和 context，不能把它当实际模型执行证明。
    # 函数用途: 提供 W0 accepted 回执形状。
    def start_job(current, kind, instruction, *, space_id=None):
        calls.append(('start_model_job', {'kind': kind, 'instruction': instruction, 'space_id': space_id}))
        tables['async_jobs'][job['job_id']] = dict(job, owner_id=current.owner_id, space_id=sid)
        return DomainResult(202, {'state': 'accepted', 'operation_id': job['operation_id'],
                                 'job_id': job['job_id'], 'task_id': job['task_id']})

    providers = {}
    monkeypatch.setattr(shared, 'w8_access', access)
    monkeypatch.setattr(shared, 'w8_get', get)
    monkeypatch.setattr(shared, 'w8_rows', rows)
    monkeypatch.setattr(shared, 'w8_transaction', transaction)
    monkeypatch.setattr(investigation, '_foundation', lambda: SimpleNamespace(
        v13_call_frozen=frozen, v13_start_model_job=start_job,
        v13_provider=lambda current, name: providers.get(name)))
    monkeypatch.setattr(investigation, '_job', lambda current, run: deepcopy(job))
    return SimpleNamespace(call=call, tables=tables, calls=calls, sources=sources, job=job,
        task=task, now=now, providers=providers, sid=sid, frozen=frozen)


# LLM: 原例参的空间标识由实际 W1 创建，不硬编码不存在的空间。
# 函数用途: 验证 accepted、任务关联、上下文及重复提交。
def test_investigate_example_params_returns_202_with_run_and_task(investigation_env):
    env = investigation_env
    first = investigation.investigate_case(env.call)
    assert first.http_status == 202
    assert first.data['state'] == 'accepted'
    assert first.data['task_id'] == 'task-one'
    row = env.tables['soc_investigations'][first.data['run_id']]
    assert row['job_id'] == 'job-one' and row['task_id'] == first.data['task_id']
    assert env.tables['soc_cases']['CASE-2026-001']['status'] == 'investigating'
    assert investigation.investigate_case(env.call).data == first.data
    submissions = [data for action, data in env.calls if action == 'start_model_job']
    assert len(submissions) == 1 and submissions[0]['kind'] == 'case_investigation'
    assert 'synthetic-host' in submissions[0]['instruction']
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        investigation.investigate_case(replace(env.call, params=dict(env.call.params, instruction='different')))


# LLM: 单测覆盖任务读口刷新、job 校验终态和不可读状态。
# 函数用途: 验证状态筛选不使用旧缓存。
def test_runs_list_refreshes_state_from_tasks(investigation_env):
    env = investigation_env
    investigation.investigate_case(env.call)
    env.task['status'], env.job['state'] = 'running', 'running'
    read = replace(env.call, action='investigations.runs.list', params={'state': 'running'})
    assert investigation.list_runs(read)['items'][0]['state'] == 'running'
    env.task['status'], env.job['state'] = 'completed', 'completed'
    env.job['result'] = _conclusion()
    read = replace(read, params={'state': 'completed'})
    result = investigation.list_runs(read)['items'][0]
    assert result['state'] == 'completed' and result['verdict'] == 'benign'
    env.task['unavailable'], env.job['state'], env.job['result'] = True, 'unknown', None
    assert investigation.list_runs(replace(read, params={}))['items'][0]['state'] == 'unknown'


# LLM: 结论是合成结构，不来自真实模型。
# 函数用途: 创建符合 case_investigation schema 的结果。
def _conclusion():
    return {'verdict': 'benign', 'summary': '合成结论 password=synthetic-conclusion-secret',
        'confidence': None, 'recommended_actions': [], 'entities': [], 'techniques': []}


# LLM: 运行 ID 可读性检查必须发生在任何源调用之前。
# 函数用途: 验证其它 owner 无法访问运行。
def test_run_get_404_for_other_owner(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    other = replace(env.call, action='investigations.runs.get', owner_id='local/user/bob', params={'run_id': rid})
    with pytest.raises(HostAPIError) as error:
        investigation.get_run(other)
    assert error.value.status == 404


# LLM: 源本身跨页且混有同 agent 的其它 task，必须完整读取后再精确过滤。
# 函数用途: 验证步骤分页、完整性及游标绑定。
def test_run_events_pages_observation_steps(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.sources['messages.list'] = [{'message_id': 'm-' + str(index), 'task_id': 'task-one',
        'role': 'assistant', 'text': '合成假设 ' + str(index), 'occurred_at': '2026-11-02T00:00:00Z'}
        for index in range(241)] + [{'message_id': 'other-task', 'task_id': 'task-other',
                                    'role': 'assistant', 'text': '不可混入', 'occurred_at': None}]
    read = replace(env.call, action='investigations.runs.events', params={'run_id': rid, 'limit': 100})
    items, cursor = [], None
    while True:
        params = dict(read.params)
        if cursor:
            params['cursor'] = cursor
        page = investigation.run_events(replace(read, params=params))
        items.extend(page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(items) == 241 and len({row['ref'] for row in items}) == 241
    assert page['coverage'] == 'complete'
    first = investigation.run_events(read)
    env.tables['soc_investigations']['run-other'] = dict(
        env.tables['soc_investigations'][rid], run_id='run-other')
    with pytest.raises(BusinessError) as error:
        investigation.run_events(replace(read, params={'run_id': 'run-other', 'cursor': first['next_cursor']}))
    assert error.value.code == 'CURSOR_EXPIRED'


# LLM: explain 必须是记录投影，禁止额外模型提交。
# 函数用途: 验证回放包含假设、工具、证据及结论。
def test_replay_example_run_id_full_timeline_and_explain_without_model(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.tables['soc_investigations']['run-sample-01'] = dict(
        env.tables['soc_investigations'][rid], run_id='run-sample-01')
    env.job.update(state='completed', result=_conclusion())
    env.task['status'] = 'completed'
    env.sources['messages.list'] = [{'message_id': 'hypothesis-one', 'task_id': 'task-one',
        'role': 'assistant', 'text': '已记录假设', 'occurred_at': '2026-11-02T00:00:00Z'}]
    env.sources['tool_calls.list'] = [{'operation_id': 'tool-one', 'task_id': 'task-one',
                                      'tool_name': 'synthetic_read', 'status': 'completed'}]
    env.sources['artifacts.list'] = [{'artifact_id': 'evidence-one', 'task_id': 'task-one',
        'name': '合成证据', 'space_id': env.sid, 'version_ref': 'version-one', 'mime_type': 'text/plain',
        'size_bytes': 1, 'content_sha256': 'a' * 64, 'available': True}]
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': 'run-sample-01'})
    data = investigation.replay(read)
    assert {row['kind'] for row in data['steps']} == {'hypothesis', 'tool_call', 'evidence', 'conclusion'}
    assert data['coverage'] == 'complete' and data['conclusion']['verdict'] == 'benign'
    explanation = investigation.explain(replace(read, action='investigations.runs.explain'))
    assert explanation['decision'] == 'benign'
    assert explanation['evidence_refs'] == ['evidence-one'] and explanation['tool_call_ids'] == ['tool-one']
    assert 'synthetic-conclusion-secret' not in explanation['rationale']
    assert len([row for row in env.calls if row[0] == 'start_model_job']) == 1


# LLM: 快照冻结、幂等和有效期均按注入时钟验证。
# 函数用途: 验证分享不泄露合成秘密、不跨 owner、不会被后续记录修改。
def test_share_snapshot_is_redacted_and_expires(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.job.update(state='completed', result=_conclusion())
    create = replace(env.call, action='investigations.share.create', params={'run_id': rid, 'expires_in_hours': 1})
    first = investigation.create_share(create)
    assert first == investigation.create_share(create)
    read = replace(create, action='investigations.share.get', params={'slug': first['slug']})
    snapshot = investigation.get_share(read)
    assert snapshot['redacted'] and 'synthetic-conclusion-secret' not in json.dumps(snapshot)
    env.job['result']['summary'] = '后续修改'
    assert investigation.get_share(read) == snapshot
    with pytest.raises(HostAPIError) as other:
        investigation.get_share(replace(read, owner_id='local/user/bob'))
    assert other.value.status == 404
    env.now[0] += 3600
    with pytest.raises(HostAPIError) as expired:
        investigation.get_share(read)
    assert expired.value.status == 404


# LLM: 文本报告使用真实媒体配额、registry、冻结读取及内容校验，不替换上传函数。
# 函数用途: 验证脱敏报告入库、幂等和原 content 下载。
@pytest.mark.parametrize('fmt', ['md', 'html'])
def test_report_md_html_persisted_in_artifact_store(investigation_env, fmt):
    env = investigation_env
    export = replace(env.call, action='soc.reports.export', params={'case_id': 'CASE-2026-001', 'format': fmt})
    first = investigation.export_report(export)
    assert first == investigation.export_report(export)
    report = first['report']
    assert report['mime_type'] == 'text/plain' and report['format'] == fmt
    read = env.frozen(export, 'artifacts.get', {'artifact_id': report['artifact_id'], 'version_ref': report['version_ref']})
    assert read['available'] and read['artifact']['content_sha256'] == report['content_sha256']
    status, headers, content = artifact_content(report['artifact_id'], report['version_ref'], export.context, export.services)
    assert status == 200 and len(content) == report['size_bytes']
    assert hashlib.sha256(content).hexdigest() == report['content_sha256']
    assert b'synthetic-report-secret' not in content and b'synthetic-alert-secret' not in content
    assert '合成告警'.encode() in content
    if fmt == 'html':
        assert b'&lt;b&gt;' in content and b'<b>' not in content


# LLM: 未绑定 renderer 不假装导出，绑定后仍必须有真实 bytes 入库。
# 函数用途: 验证 PDF/DOCX 的 provider 边界和真实登记。
@pytest.mark.parametrize('fmt', ['pdf', 'docx'])
def test_report_renderer_required_and_bytes_persisted(investigation_env, fmt):
    env = investigation_env
    call = replace(env.call, action='soc.reports.export', params={'case_id': 'CASE-2026-001', 'format': fmt})
    with pytest.raises(HostAPIError) as unbound:
        investigation.export_report(call)
    assert unbound.value.code == 'HOST_NOT_READY'
    seen = []

    # LLM: renderer 是明确替身，只验证转发及持久字节，不声称文档格式已渲染。
    # 函数用途: 返回可辨认合成字节。
    def render(markdown, *, format, title):
        seen.append((markdown, format, title))
        return b'%PDF-synthetic-renderer' if format == 'pdf' else b'PK\x03\x04synthetic-renderer'

    env.providers['document_renderer'] = SimpleNamespace(render=render)
    report = investigation.export_report(call)['report']
    assert seen[0][1] == fmt and 'synthetic-report-secret' not in seen[0][0]
    status, headers, content = artifact_content(report['artifact_id'], report['version_ref'], call.context, call.services)
    assert status == 200 and content.endswith(b'synthetic-renderer')


# LLM: JSON parser 不修复模型输出，也不接受字段覆盖或非有限数。
# 函数用途: 验证严格结构化 parse。
@pytest.mark.parametrize('text', ['[]', 'null', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '```json\n{}\n```'])
def test_finalize_rejects_invalid_structured_result(text):
    with pytest.raises(ValueError):
        investigation.finalize_investigation(text)


# LLM: 合法 JSON 保留原字段，最终字段约束由 registry schema 守住。
# 函数用途: 验证结构化结论解析无额外推理。
def test_finalize_preserves_structured_result():
    result = _conclusion()
    assert investigation.finalize_investigation(json.dumps(result)) == result


# LLM: 原生读口的 BusinessError 不能使只读账本误报成功，也不能吞掉撤权。
# 函数用途: 验证来源缺口与安全拒绝分开处理。
def test_source_unavailable_is_partial_but_revocation_propagates(investigation_env, monkeypatch):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': rid})

    # LLM: 明确模拟不可用的原生源。
    # 函数用途: 产生结构化来源读取失败。
    def missing_source(call, action, params):
        raise BusinessError('HOST_NOT_READY')

    monkeypatch.setattr(investigation, '_frozen', missing_source)
    assert investigation.replay(read)['coverage'] == 'partial'

    # LLM: 安全拒绝不得被转换成 unknown 后披露其它记录。
    # 函数用途: 模拟调用过程中实时撤权。
    def revoked_source(call, action, params):
        raise HostAPIError('CAPABILITY_DENIED', 403)

    monkeypatch.setattr(investigation, '_frozen', revoked_source)
    with pytest.raises(HostAPIError) as revoked:
        investigation.replay(read)
    assert revoked.value.status == 403


# LLM: 回放 schema 有上限，但 explain 不应把后续已有步骤当作不存在。
# 函数用途: 验证超长时间轴 partial 以及指定后续步骤解释。
def test_replay_bound_and_late_step_explain(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.sources['messages.list'] = [{'message_id': 'long-' + str(index), 'task_id': 'task-one',
        'role': 'assistant', 'text': '步骤' + str(index), 'occurred_at': '2026-11-02T00:00:00Z'}
        for index in range(1002)]
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': rid})
    data = investigation.replay(read)
    assert len(data['steps']) == 1000 and data['coverage'] == 'partial'
    explanation = investigation.explain(replace(read, action='investigations.runs.explain',
                                               params={'run_id': rid, 'step_seq': 1001}))
    assert explanation['step_seq'] == 1001 and explanation['decision'].startswith('步骤')


# LLM: list 的分页范围和状态过滤必须共同绑定游标。
# 函数用途: 验证完整运行列表及过滤变更拒绝旧游标。
def test_runs_page_all_records_and_cursor_scope(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    row = env.tables['soc_investigations'].pop(rid)
    for index in range(137):
        record = dict(row, run_id='run-index-' + str(index))
        env.tables['soc_investigations'][record['run_id']] = record
    read = replace(env.call, action='investigations.runs.list', params={'limit': 50})
    first = investigation.list_runs(read)
    second = investigation.list_runs(replace(read, params={'limit': 50, 'cursor': first['next_cursor']}))
    third = investigation.list_runs(replace(read, params={'limit': 50, 'cursor': second['next_cursor']}))
    combined = first['items'] + second['items'] + third['items']
    assert len(combined) == 137 and len({row['run_id'] for row in combined}) == 137
    assert third['next_cursor'] is None
    with pytest.raises(BusinessError) as stale:
        investigation.list_runs(replace(read, params={'state': 'accepted', 'cursor': first['next_cursor']}))
    assert stale.value.code == 'CURSOR_EXPIRED'


# LLM: 此测试必须在 W0 和 W8 registry 同步后运行，不用源替身替代 HTTP 主链路。
# 函数用途: 验证真实 native 受理、任务、账本、报告入库与鉴权下载往返。
def test_native_investigation_report_and_share_roundtrip(native_http):
    import time

    from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile

    echo_profile(native_http)
    sid = v13_space(native_http)
    status, created = v13_call(native_http, 'soc.cases.create', {'space_id': sid,
        'case_id': 'CASE-2026-001', 'title': '合成调查', 'description': 'password=synthetic-native-secret',
        'severity': 'high'}, user='business-alice', idempotency_key='native-investigation-case-create')
    assert status == 200 and created['error'] is None, created
    params = {'space_id': sid, 'case_id': 'CASE-2026-001', 'mode': 'autonomous_agent'}
    status, accepted = v13_call(native_http, 'soc.cases.investigate', params, user='business-alice',
                               idempotency_key='native-investigation-start-one')
    assert status == 202 and accepted['error'] is None, accepted
    run = accepted['data']
    status, task = v13_call(native_http, 'tasks.get', {'task_id': run['task_id']}, user='business-alice')
    assert status == 200 and task['data']['task']['task_id'] == run['task_id'], task
    status, replayed = v13_call(native_http, 'soc.cases.investigate', params, user='business-alice',
                              idempotency_key='native-investigation-start-one')
    assert status == 202 and replayed['data'] == accepted['data'], replayed
    drive_job(native_http, run['job_id'])
    for attempt in range(100):
        status, job = v13_call(native_http, 'async_jobs.get', {'job_id': run['job_id']}, user='business-alice')
        assert status == 200, job
        if job['data']['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert job['data']['state'] == 'failed' and job['data']['error']['code'] == 'JOB_RESULT_INVALID', job
    # 原 echo 正文不符合 job schema；失败必须如实投影，不伪造完成结论。
    status, ledger = v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                             user='business-alice')
    assert status == 200 and ledger['data']['run']['state'] == job['data']['state'], ledger
    status, owner_b = v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                              user='business-bob')
    assert status == 404 and owner_b['error']['code'] == 'RESOURCE_NOT_FOUND', owner_b
    report_params = {'case_id': 'CASE-2026-001', 'run_id': run['run_id'], 'format': 'md'}
    status, exported = v13_call(native_http, 'soc.reports.export', report_params, user='business-alice',
                               idempotency_key='native-investigation-report-one')
    assert status == 200 and exported['error'] is None, exported
    report = exported['data']['report']
    status, fetched = v13_call(native_http, 'artifacts.get', {'artifact_id': report['artifact_id'],
        'version_ref': report['version_ref']}, user='business-alice')
    assert status == 200 and fetched['data']['available'], fetched
    status, headers, content = native_request(native_http, 'GET', '/client/business/artifacts/'
        + report['artifact_id'] + '/content?version_ref=' + report['version_ref'], user='business-alice')
    assert status == 200 and hashlib.sha256(content).hexdigest() == report['content_sha256']
    assert b'synthetic-native-secret' not in content
    status, share = v13_call(native_http, 'investigations.share.create', {'run_id': run['run_id']},
        user='business-alice', idempotency_key='native-investigation-share-one')
    assert status == 200 and share['error'] is None, share
    status, snapshot = v13_call(native_http, 'investigations.share.get', {'slug': share['data']['slug']},
                               user='business-alice')
    assert status == 200 and snapshot['data']['redacted'], snapshot


# LLM: 模型替身只产生结构化结论；真实任务、终态校验、账本和分享不使用替身。
# 函数用途: 验证调查成功终态和已记录结论回放的原生链路。
def test_native_structured_investigation_finishes_and_replays(native_http, monkeypatch):
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.tests.test_v13_w0_harness import v13_call
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile
    from agent_py_agent.tests.test_v13_w8_cases import completed, create, ingest, rejected

    echo_profile(native_http)
    case = create(native_http, ingest(native_http))
    seen = []

    # LLM: 没有真实模型/外网参与，确定性结论只服务于底座与切片接线验证。
    # 函数用途: 为实际后台任务返回可被唯一 job schema 验证的输出。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        seen.append(prompt)
        return ModelResponse(text=json.dumps(_conclusion()), backend='echo')

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    status, accepted = v13_call(native_http, 'soc.cases.investigate', {'case_id': case['case_id']},
        user='business-alice', idempotency_key='structured-native-investigation')
    assert status == 202, accepted
    run = accepted['data']
    drive_job(native_http, run['job_id'])
    assert seen, '真实任务没有到达合成模型入口'
    for attempt in range(200):
        job = completed(v13_call(native_http, 'async_jobs.get', {'job_id': run['job_id']}, user='business-alice'))
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert seen and job['state'] == 'completed', job
    current = completed(v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                                user='business-alice'))['run']
    assert current['state'] == 'completed' and current['verdict'] == 'benign' and current['finished_at']
    replayed = completed(v13_call(native_http, 'investigations.runs.replay', {'run_id': run['run_id']},
                                 user='business-alice'))
    assert replayed['conclusion']['verdict'] == 'benign'
    assert any(row['kind'] == 'conclusion' for row in replayed['steps'])
    explanation = completed(v13_call(native_http, 'investigations.runs.explain', {'run_id': run['run_id']},
                                    user='business-alice'))
    assert explanation['decision'] == 'benign' and 'synthetic-conclusion-secret' not in explanation['rationale']
    before = len(seen)
    completed(v13_call(native_http, 'investigations.runs.explain', {'run_id': run['run_id']}, user='business-alice'))
    assert len(seen) == before
    rejected(v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']}, user='business-bob'),
             404, 'RESOURCE_NOT_FOUND')
