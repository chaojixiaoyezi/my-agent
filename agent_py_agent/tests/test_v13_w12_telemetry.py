"""遥测的真实临时账本、只读边界和原生 HTTP 信封回归，不替生产注册器补接线。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w12_telemetry as telemetry
from agent_py_agent.agent.common.json_io import append_private_jsonl_records
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call, v13_validate_result
from agent_py_agent.agent.gateway_parts.remote_push_service import persist_push_json
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_seed, v13_space

# 复用底座原夹具，不安装插件或构造替代 HTTP 入口。
v13_host = harness.v13_host
v13_native = harness.v13_native


# LLM: 允许的是测试宿主签发的上下文，不在请求 JSON 伪造 owner。
# 函数用途: 为直接领域调用提供实时授权函数。
def _allow(_action, _facts):
    return True


# LLM: 时钟和身份来自本次隔离宿主，仍使用真实 BusinessCallContext。
# 函数用途: 构造冻结 V13 调用。
def _call(host, *, action='telemetry.funnel.get', params=None, now=None):
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp() if now is None else now
    services = api.BusinessServices(host)
    context = api.bind_business_context(host, authorize=_allow, subject_id='telemetry-reader')
    return V13Call(action, {} if params is None else params, 'telemetry-test', host.home_paths.owner_id,
        context.subject_id, context, services, Path(host.home_paths.owner_home_dir), clock=lambda: now)


# LLM: 缺 owner 根会被原 no-follow 守卫拒绝，测试显式创建自己的临时根。
# 函数用途: 提供真实临时 owner 与冻结调用。
@pytest.fixture
def telemetry_call(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    v13_host.home_paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    return _call(v13_host)


# LLM: 对测试树比较正文、模式及修改时间，证明只读查询没有隐藏写入。
# 函数用途: 采集临时树的机械快照。
def _tree(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        for path in Path(root).rglob('*')}


# LLM: 通过原推送账本写叶保存合成回执，不发真实推送或网络请求。
# 函数用途: 初始化一份真实 receipts.json。
def _pushes(call, specs):
    root = call.context.host.home_paths.root / 'remote_push'
    root.mkdir(parents=True, exist_ok=True)
    entries = {str(index): {'digest': 'synthetic-digest', 'accepted': accepted,
        'receipt': {'push_id': identifier, 'target_owner': owner, 'queued_at': stamp}}
        for index, (identifier, owner, stamp, accepted) in enumerate(specs)}
    persist_push_json(root / 'receipts.json', {'entries': entries})


# LLM: 调度记录经真实创建事务获得原生 schema、owner、策略和版本。
# 函数用途: 为隔离 owner 创建原生 SchedulerRepository 和任务。
def _scheduler(call):
    paths = call.context.host.home_paths
    repository = SchedulerRepository(call.owner_home / 'data' / 'scheduler',
        owner_provider=paths.owner_provider, owner_kind=paths.owner_kind, owner_id=call.owner_id)
    job, _duplicate = repository.create_job(SchedulerJobCreateRequest(name='遥测测试', prompt='本地合成事件',
        thread_id='thread-telemetry', source_request_id='telemetry-' + uuid4().hex,
        schedule={'kind': 'every', 'every_seconds': 3600, 'anchor_at': call.clock() - 100000, 'timezone': 'UTC'},
        now=call.clock() - 100000))
    return repository, job['job_id']


# LLM: 终态通过原预约、领取、开始与完成链路生成，不手拼假 history 返回值。
# 函数用途: 完成一条真实调度运行并返回持久历史行。
def _run(repository, job_id, ended, *, status='done', wake='wake-one',
         error_code='', delivery_status='', delivery_reason=''):
    run = repository.reserve_manual_run(job_id, now=ended - 3)
    if wake:
        repository.attach_wake_signal(run['run_id'], wake)
    claimed = repository.claim_run(run['run_id'], lease_seconds=60, now=ended - 1)
    assert claimed is not None
    repository.mark_run_running(run['run_id'], claimed['claim_id'], now=ended - 1)
    repository.finish_run(run['run_id'], claimed['claim_id'], SchedulerRunFinish(status=status,
        error_code=error_code, delivery_status=delivery_status, delivery_reason=delivery_reason, now=ended))
    records, errors = repository.history(limit=200)
    assert not errors
    return next(row for row in records if row['run_id'] == run['run_id'])


# LLM: 记录采用 W8 冻结形状，时间和关联字段可独立调整。
# 函数用途: 生成可通过合同校验的临时告警。
def _alert(call, identifier, stamp, *, case_id=None, owner=None, space='soc-default'):
    at = telemetry._at(stamp)
    return dict(alert_id=identifier, space_id=space, title='合成告警', description='', severity='medium',
        status='new', verdict=None, ai_confidence=None, disposition=None, rule_id=None, source='monitor',
        external_ref=None, entities=[], techniques=[], assignee=None, case_id=case_id, tags=[], evidence_refs=[],
        acknowledged_at=None, resolved_at=None, revision=1, created_at=at, updated_at=at,
        owner_id=call.owner_id if owner is None else owner)


# LLM: 脚本文本错误不能冒充错误码，结构化 output.error_code 才参与分组。
# 函数用途: 生成 W7 冻结脚本运行记录。
def _script(call, identifier, stamp, *, code='SCRIPT_TEST_FAILED'):
    at = telemetry._at(stamp)
    return dict(execution_id=identifier, owner_id=call.owner_id, space_id='soc-default', script_id='script-test',
        subject_id='analyst-test', requires_approval=False, task_id=None, context=None, inputs_sha256='0' * 64,
        state='failed', ok=False, output={'error_code': code} if code else None, error='合成脚本错误正文',
        duration_ms=1, started_at=at, finished_at=at, revision=1, created_at=at, updated_at=at)


# LLM: 作业由 W0 集合原形状提供，失败原因来自 error.code。
# 函数用途: 生成临时异步失败作业。
def _job(call, identifier, stamp):
    at = telemetry._at(stamp)
    return dict(job_id=identifier, owner_id=call.owner_id, kind='rule_translation', action='tools.rule_translate',
        subject_id='analyst-test', space_id='soc-default', agent_id='agent-test', task_id=None, operation_id=None,
        state='failed', result=None, error={'code': 'JOB_RESULT_INVALID', 'message': '合成原因'},
        revision=1, created_at=at, updated_at=at)


# LLM: 直接断言真实账本统计、owner/时窗过滤、事件融合和查询零写入。
# 函数用途: 覆盖计划指定的漏斗与缺源语义。
def test_funnel_counts_from_real_ledgers_and_unknown_when_source_missing(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [('push-a', call.owner_id, now - 100, True), ('push-b', call.owner_id, now - 50, True),
        ('push-a', call.owner_id, now - 100, True), ('push-other', 'local/other', now - 20, True),
        ('push-old', call.owner_id, now - 86401, True), ('push-future', call.owner_id, now + 1, True)])
    repository, job_id = _scheduler(call)
    _run(repository, job_id, now - 300, wake='shared-event')
    _run(repository, job_id, now - 200, wake='shared-event')
    _run(repository, job_id, now - 100, wake='second-event')
    alerts = [_alert(call, 'alert-a', now - 80, case_id='case-test'), _alert(call, 'alert-b', now - 50),
        _alert(call, 'alert-old', now - 86401), _alert(call, 'alert-future', now + 1),
        _alert(call, 'alert-other', now - 10, owner='local/other')]
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.funnel(call)
    assert [row['count'] for row in result['stages']] == [2, 3, 2, 1, 2]
    assert result['reduction_ratio'] == 0
    assert result['unknown'] == [] and result['coverage'] == 'complete'
    assert all(row['source'] for row in result['stages'])
    assert _tree(call.context.host.home_paths.root) == before
    missing_host = SimpleNamespace(home_paths=replace(call.context.host.home_paths,
        root=call.context.host.home_paths.root / 'missing-host',
        owner_home_dir=call.owner_home / 'missing-owner'), config=call.context.host.config)
    missing_host.home_paths.owner_home_dir.mkdir()
    missing = telemetry.funnel(_call(missing_host))
    assert all(row['count'] is None for row in missing['stages'])
    assert missing['unknown'] == ['ingest', 'normalize', 'fuse', 'correlate', 'alert']
    assert missing['coverage'] == 'partial' and missing['reduction_ratio'] is None


# LLM: 空的已初始化来源可报零，未初始化来源必须未知。
# 函数用途: 覆盖空窗、零分母与原只读守卫。
def test_empty_initialized_sources_are_zero_not_missing(telemetry_call):
    call = telemetry_call
    _pushes(call, [])
    _scheduler(call)
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.funnel(call)
    assert [stage['count'] for stage in result['stages']] == [0, 0, 0, 0, 0]
    assert result['coverage'] == 'complete' and result['reduction_ratio'] is None


# LLM: 例示空间只是聚合过滤值，阶段来源仍为真实 owner 账本。
# 函数用途: 覆盖原文参数、五阶段吞吐和严格延迟阈值。
def test_pipeline_health_example_params_status_rules(telemetry_call):
    call = replace(telemetry_call, action='telemetry.pipeline.health',
                   params={'space_id': 'soc-default', 'window_hours': 24})
    now = call.clock()
    _pushes(call, [('push-pending', call.owner_id, now - 300, False)])
    repository, job_id = _scheduler(call)
    repository.reserve_manual_run(job_id, now=now - 300)
    v13_seed(call.owner_home, 'soc_alerts', {})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.pipeline_health(call)
    stages = {stage['id']: stage for stage in result['stages']}
    assert set(stages) == {'ingest', 'normalize', 'fuse', 'correlate', 'alert'}
    assert result['overall'] == 'healthy'
    assert stages['ingest']['lag_ms'] == stages['normalize']['lag_ms'] == 300000
    assert stages['ingest']['eps'] == pytest.approx(1 / 86400)
    assert stages['normalize']['failed'] == 0
    assert stages['ingest']['failed'] is None
    assert all(stages[name]['lag_ms'] is None for name in ('fuse', 'correlate', 'alert'))
    assert _tree(call.context.host.home_paths.root) == before
    later = telemetry.pipeline_health(replace(call, clock=lambda: now + 1))
    assert later['overall'] == 'degraded'
    assert all(row['status'] == 'degraded' for row in later['stages'][:2])


# LLM: 大于五个百分点才降级，测试原生历史投影而非替换聚合器输出。
# 函数用途: 验证失败率边界与 runtime_snapshot 的降级信号。
def test_pipeline_failure_ratio_and_runtime_degraded(telemetry_call):
    call = replace(telemetry_call, action='telemetry.pipeline.health')
    now = call.clock()
    _pushes(call, [])
    repository, job_id = _scheduler(call)
    original = _run(repository, job_id, now - 100)
    copies = [{**original, 'run_id': 'srun_' + uuid4().hex} for _index in range(18)]
    failure = {**original, 'run_id': 'srun_' + uuid4().hex, 'status': 'failed', 'error_code': 'RUN_TEST_FAILED'}
    append_private_jsonl_records(repository.history_path, [*copies, failure])
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.pipeline_health(call)
    normalize = result['stages'][1]
    assert normalize['processed'] == 19 and normalize['failed'] == 1
    assert normalize['status'] == 'healthy'
    append_private_jsonl_records(repository.history_path, [{**failure, 'run_id': 'srun_' + uuid4().hex}])
    assert telemetry.pipeline_health(call)['stages'][1]['status'] == 'degraded'
    # 保留健康 history，在原 store 注入一个坏 job，验证原 runtime_snapshot 的健康信号。
    state = json.loads(repository.store_path.read_text())
    state['jobs']['job-invalid'] = {'schema_version': 'invalid'}
    repository.store_path.write_text(json.dumps(state))
    result = telemetry.pipeline_health(call)
    assert result['stages'][1]['status'] == result['stages'][2]['status'] == 'degraded'
    assert result['stages'][1]['reason_code'] == 'SCHEDULER_JOB_INVALID'


# LLM: 四来源分别分组，旧记录和其它空间不计入，HTTP 边缘拒收始终未知。
# 函数用途: 覆盖计划指定的默认二十四小时死信统计。
def test_dead_letters_grouped_by_error_code_last_24h(telemetry_call):
    call = replace(telemetry_call, action='telemetry.dead_letters.list')
    now = call.clock()
    repository, job_id = _scheduler(call)
    _run(repository, job_id, now - 86401, status='failed', error_code='RUN_OLD')
    first = _run(repository, job_id, now - 90, status='failed', error_code='RUN_TEST_FAILED')
    second = _run(repository, job_id, now - 60, status='failed', error_code='RUN_TEST_FAILED',
                  delivery_status='failed', delivery_reason='DELIVERY_TEST_FAILED')
    scripts = [_script(call, 'exec-one', now - 40), _script(call, 'exec-two', now - 30),
               _script(call, 'exec-old', now - 86401)]
    v13_seed(call.owner_home, 'script_runs', {row['execution_id']: row for row in scripts})
    job = _job(call, 'job-model', now - 20)
    v13_seed(call.owner_home, 'async_jobs', {job['job_id']: job})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.dead_letters(call)
    groups = {(row['source'], row['error_code']): row for row in result['groups']}
    assert {key: row['count'] for key, row in groups.items()} == {
        ('scheduler_run', 'RUN_TEST_FAILED'): 2, ('alert_delivery', 'DELIVERY_TEST_FAILED'): 1,
        ('script_run', 'SCRIPT_TEST_FAILED'): 2, ('async_job', 'JOB_RESULT_INVALID'): 1}
    assert result['total'] == 6
    assert groups['scheduler_run', 'RUN_TEST_FAILED']['first_at'] == telemetry._at(now - 90)
    assert groups['scheduler_run', 'RUN_TEST_FAILED']['sample_refs'] == [second['run_id'], first['run_id']]
    assert result['groups'][0]['source'] == 'async_job'
    assert result['unknown'] == ['remote_push_rejections'] and result['coverage'] == 'partial'
    assert _tree(call.context.host.home_paths.root) == before
    filtered = telemetry.dead_letters(replace(call, params={'source': 'script_run'}))
    assert filtered['total'] == 2 and len(filtered['groups']) == 1
    assert filtered['unknown'] == ['remote_push_rejections']


# LLM: 未持久的错误码和缺来源不能通过通用失败标签补造。
# 函数用途: 验证文本错误、缺源及来源筛选的诚实覆盖。
def test_dead_letters_missing_sources_and_unstructured_error(telemetry_call):
    call = replace(telemetry_call, action='telemetry.dead_letters.list')
    row = _script(call, 'exec-no-code', call.clock() - 10, code=None)
    v13_seed(call.owner_home, 'script_runs', {row['execution_id']: row})
    result = telemetry.dead_letters(call)
    assert result['total'] == 0 and result['groups'] == []
    assert set(result['unknown']) == {'scheduler_run', 'alert_delivery', 'async_job',
                                      'remote_push_rejections', 'script_run_error_code'}
    filtered = telemetry.dead_letters(replace(call, params={'source': 'script_run'}))
    assert filtered['unknown'] == ['remote_push_rejections', 'script_run_error_code']


# LLM: 原生 history 有明确上限，不能将截断投影当全时窗计数。
# 函数用途: 验证二百条覆盖边界和缺融合身份。
def test_history_limit_and_missing_event_identity_are_unknown(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [])
    repository, job_id = _scheduler(call)
    row = _run(repository, job_id, now - 100, wake='')
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.funnel(call)
    assert result['normalized_count'] == 1 and result['fused_count'] is None
    assert result['unknown'] == ['fuse']
    append_private_jsonl_records(repository.history_path,
        [{**row, 'run_id': 'srun_' + uuid4().hex} for _index in range(199)])
    result = telemetry.funnel(call)
    assert result['normalized_count'] is None and result['fused_count'] is None
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_HISTORY_TRUNCATED'


# LLM: pending 不能被查询恢复，冷来源不应创建 data/scheduler 或锁。
# 函数用途: 验证原生只读守卫与明确错误码。
def test_reads_do_not_create_directories_or_recover_pending(telemetry_call):
    call = telemetry_call
    root = call.context.host.home_paths.root
    before = _tree(root)
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['overall'] == 'unavailable'
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_SOURCE_MISSING'
    assert _tree(root) == before
    pending = call.owner_home / '.asset-batch.json'
    pending.write_text('{"pending":"synthetic"}')
    before = _tree(root)
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_ASSET_RECOVERY_REQUIRED'
    assert pending.read_text() == '{"pending":"synthetic"}'
    assert _tree(root) == before


# LLM: 异常按结构化码而非消息文字分类，不将损坏账本转成健康零。
# 函数用途: 验证坏 JSON 与坏时间的来源不可用结果。
def test_corrupt_sources_preserve_structured_error_codes(telemetry_call):
    call = telemetry_call
    _pushes(call, [])
    receipts = call.context.host.home_paths.root / 'remote_push' / 'receipts.json'
    receipts.write_text('{broken')
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][0]['status'] == 'unavailable'
    assert health['stages'][0]['reason_code'] == 'REMOTE_PUSH_JSON_INVALID'
    row = _alert(call, 'alert-invalid', call.clock())
    row['created_at'] = 'not-a-time'
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row})
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][4]['processed'] is None
    assert health['stages'][4]['reason_code'] == 'TELEMETRY_TIMESTAMP_INVALID'


# LLM: 不同来源可能不是包含关系，不能为 schema 通过强行钳制缩减率。
# 函数用途: 验证独立流数量不一致的未知比例。
def test_alerts_exceed_ingest_keeps_ratio_unknown(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [('push-one', call.owner_id, now - 20, True)])
    _scheduler(call)
    alerts = [_alert(call, 'alert-one', now - 10), _alert(call, 'alert-two', now - 5)]
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    result = telemetry.funnel(call)
    assert result['reduction_ratio'] is None and result['unknown'] == ['reduction_ratio']
    assert result['coverage'] == 'partial'


# LLM: 实时权限失效不能被遥测的缺源容错吞掉。
# 函数用途: 验证三个动作均保持活跃身份门。
@pytest.mark.parametrize('handler', [telemetry.funnel, telemetry.pipeline_health, telemetry.dead_letters])
def test_permission_denial_is_not_hidden_as_unknown(telemetry_call, handler):
    call = telemetry_call
    context = api.bind_business_context(call.context.host, authorize=lambda _action, _facts: False,
                                       subject_id=call.subject_id)
    with pytest.raises(HostAPIError) as raised:
        handler(replace(call, context=context))
    assert raised.value.code == 'CAPABILITY_DENIED'


# LLM: 真正走 HTTP 鉴权、生产注册器和结果 schema，不在测试中手工注入动作。
# 函数用途: 覆盖计划指定的 completed 信封及真实账本 HTTP 往返。
def test_envelopes(v13_native):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    server, agent = v13_native
    space = v13_space(server)
    paths = home_paths_with_owner(agent.home_paths,
        resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user('feishu', 'business-alice')))
    host = SimpleNamespace(home_paths=paths, config=agent.config)
    call = _call(host, now=datetime.now(timezone.utc).timestamp())
    _pushes(call, [('push-http', call.owner_id, call.clock() - 30, True)])
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 20, wake='wake-http')
    alert = _alert(call, 'alert-http', call.clock() - 10, space=space, case_id='case-http')
    v13_seed(call.owner_home, 'soc_alerts', {alert['alert_id']: alert})
    v13_seed(call.owner_home, 'script_runs', {})
    v13_seed(call.owner_home, 'async_jobs', {})
    actions = [('telemetry.funnel.get', {'space_id': space}),
        ('telemetry.pipeline.health', {'space_id': 'soc-default', 'window_hours': 24}),
        ('telemetry.dead_letters.list', {})]
    replies = {}
    for action, params in actions:
        status, body = v13_call(server, action, params, user='business-alice')
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                             'allowed_actions', 'warnings', 'effect_outcome'}
        assert body['effect_outcome'] == 'none'
        v13_validate_result(action, body['data'])
        replies[action] = body['data']
    assert [stage['count'] for stage in replies['telemetry.funnel.get']['stages']] == [1, 1, 1, 1, 1]
    assert replies['telemetry.pipeline.health']['overall'] == 'healthy'
    assert replies['telemetry.dead_letters.list']['unknown'] == ['remote_push_rejections']


# LLM: 参数越界在领域调用前失败，显式空间不存在及其它 owner 均不返回空成功。
# 函数用途: 验证真实 HTTP 的闭合参数与精确 404 错误。
def test_http_schema_errors_and_funnel_space_not_found(v13_native):
    server, _agent = v13_native
    space = v13_space(server)
    for action in ('telemetry.funnel.get', 'telemetry.pipeline.health', 'telemetry.dead_letters.list'):
        for params in ({'window_hours': 0}, {'window_hours': 721}, {'window_hours': True}, {'unexpected': 1}):
            status, body = v13_call(server, action, params, user='business-alice')
            assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', body
            assert body['state'] == 'failed' and body['data'] == {}
    for owner, selected in (('business-alice', 'missing-space'), ('business-bob', space)):
        status, body = v13_call(server, 'telemetry.funnel.get', {'space_id': selected}, user=owner)
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body
        assert body['state'] == 'failed' and body['data'] == {}


# LLM: 底座停用必须保持原 503，不让容错聚合绕过 feature gate。
# 函数用途: 验证真实 HTTP 的未就绪错误信封。
def test_http_feature_disabled_is_host_not_ready(v13_native, monkeypatch):
    server, agent = v13_native
    monkeypatch.setattr(agent.config, 'business_spaces_enabled', False)
    for action in ('telemetry.funnel.get', 'telemetry.pipeline.health', 'telemetry.dead_letters.list'):
        status, body = v13_call(server, action, {}, user='business-alice')
        assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body
        assert body['state'] == 'failed' and body['data'] == {}


# LLM: 曾发现注册器导入环，静态门阻止把 handler 的注册器依赖移回模块顶层。
# 函数用途: 保留无顶层注册器循环、数字常量及宽泛参数的结构回归。
def test_product_structure_keeps_registry_import_lazy():
    import ast

    tree = ast.parse(Path(telemetry.__file__).read_text())
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or '').endswith('business_api_v13')
                   for node in tree.body)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assert not isinstance(node.value, ast.Constant) or type(node.value.value) not in (int, float)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert node.args.vararg is None and node.args.kwarg is None


# LLM: 字段缺失、重复冲突与极端时间均先单列为未知，不能污染数量统计。
# 函数用途: 验证聚合前的数据质量边界。
@pytest.mark.parametrize('defect', ['missing_case', 'extreme_time', 'conflicting_duplicate'])
def test_alert_data_quality_is_checked_before_aggregation(telemetry_call, defect):
    call = telemetry_call
    row = _alert(call, 'alert-quality', call.clock() - 10)
    if defect == 'missing_case':
        row.pop('case_id')
    elif defect == 'extreme_time':
        row['created_at'] = 10 ** 100
    records = {'first': row}
    if defect == 'conflicting_duplicate':
        records['second'] = {**row, 'case_id': 'case-conflict'}
    v13_seed(call.owner_home, 'soc_alerts', records)
    result = telemetry.funnel(call)
    assert result['alert_count'] is None and result['correlated_count'] is None
    assert result['coverage'] == 'partial'


# LLM: 原生任务记录损坏只降低相应阶段，不能引发属性异常或伪造空融合键。
# 函数用途: 验证 outbox 坏形状的结构化错误。
def test_invalid_delivery_outbox_is_unknown_not_exception(telemetry_call):
    call = telemetry_call
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 10)
    state = json.loads(repository.store_path.read_text())
    state['jobs'][job_id]['delivery_outbox'] = None
    repository.store_path.write_text(json.dumps(state))
    result = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert result['stages'][1]['processed'] == 1
    assert result['stages'][2]['processed'] is None
    assert result['stages'][2]['reason_code'] == 'SCHEDULER_EVENT_KEY_INVALID'


# LLM: 请求允许而单条空间记录被拒绝，不能只依赖工厂的请求级检查。
# 函数用途: 验证受限告警和失败作业不会被遥测聚合绕过。
@pytest.mark.parametrize('source', ['alert', 'script'])
def test_record_level_authorization_is_not_hidden(telemetry_call, source):
    call = telemetry_call
    if source == 'alert':
        row = _alert(call, 'restricted-alert', call.clock() - 10, space='private-space')
        collection, key, handler = 'soc_alerts', 'alert_id', telemetry.funnel
    else:
        row = {**_script(call, 'restricted-script', call.clock() - 10), 'space_id': 'private-space'}
        collection, key, handler = 'script_runs', 'execution_id', telemetry.dead_letters
        call = replace(call, action='telemetry.dead_letters.list', params={'source': 'script_run'})
    v13_seed(call.owner_home, collection, {row[key]: row})
    context = api.bind_business_context(call.context.host, subject_id=call.subject_id,
        authorize=lambda _action, facts: facts.get('space_id') != 'private-space')
    with pytest.raises(HostAPIError) as raised:
        handler(replace(call, context=context))
    assert raised.value.code == 'CAPABILITY_DENIED'


# LLM: 链接指向合法同身份的合成账本也必须拒绝，不能在读取后才校验身份。
# 函数用途: 验证历史来源使用 no-follow 打开而非继承的普通 Path 读取。
def test_history_symlink_is_unavailable(telemetry_call, tmp_path):
    call = telemetry_call
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 10)
    outside = tmp_path / 'synthetic-history.jsonl'
    repository.history_path.rename(outside)
    repository.history_path.symlink_to(outside)
    result = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert result['stages'][1]['processed'] is None
    assert result['stages'][1]['status'] == 'unavailable'
