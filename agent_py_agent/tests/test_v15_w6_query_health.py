"""W6纯值健康合同；失败不算零命中，报告不能注入宿主裁决。"""
from copy import deepcopy

import pytest

from agent_py_agent.agent.workflows.query_health import (
    build_query_health,
    normalize_source_report,
    retry_is_safe,
    transient_failure_code,
)


# LLM: 合成的结构化状态，不接外部查询。
# 函数用途: 构造单源观察。
def observation(source_id='events-a', status='ok', hits=1, *, http_status=None, error_code=''):
    return {'source_id': source_id, 'status': status, 'hit_count': hits,
            'http_status': http_status, 'error_code': error_code}


# LLM: probe集合与fetch观察分别显式给出。
# 函数用途: 构造两阶段报告。
def report(rows, *, discovered=None):
    return {'sources': rows, 'discovered_sources': discovered if discovered is not None else
            [row['source_id'] for row in rows], 'stage': 'probe+fetch'}


# LLM: 只传脱敏引用和core标志，不产生Store副作用。
# 函数用途: 构造清单快照。
def manifest(*, core=False):
    return {'schema_version': 'datasource_manifest.v1', 'sources': [
        {'source_id': 'events-a', 'core': core, 'credential_ref': 'cref-a', 'endpoint_ref': 'endpoint-a'}]}


# LLM: 四种分类使用同一个纯函数入口，不根据说明文本分支。
# 函数用途: 验证命中、真零、部分失败、全失败的互斥结果。
@pytest.mark.parametrize('rows,classification,blocked', [
    ([observation()], 'SUCCESS_WITH_HITS', False),
    ([observation(status='zero_hits', hits=0)], 'SUCCESS_ZERO_HITS', False),
    ([observation(), observation('events-b', 'transient_error', 0)], 'PARTIAL_SOURCE_FAILURE', False),
    ([observation(status='transient_error', hits=0)], 'TRANSIENT_FAILURE_EMPTY', True),
])
def test_four_health_classifications(rows, classification, blocked):
    value = build_query_health(report(rows), manifest())
    assert isinstance(value, dict)
    assert value['schema_version'] == 'query_health.v1' and value['coverage'] == 'declared'
    assert value['classification'] == classification and value['analysis_blocked'] is blocked
    assert value['block_reason'] == ('QUERY_FAILED_NO_DATA' if blocked else None)


# LLM: 缺失或错误类型报告不能被当成查到零条；坏形状不抛异常。
# 函数用途: 验证SOURCE_REPORT_MISSING及undeclared兼容边界。
@pytest.mark.parametrize('value', [None, [], 'bad', 3, {}, {'sources': 1},
    {'sources': [], 'discovered_sources': False, 'stage': 'probe+fetch'}])
def test_invalid_or_missing_report_blocks_only_declared_requirement(value):
    receipt = build_query_health(value, manifest())
    assert isinstance(receipt, dict)
    assert receipt['analysis_blocked'] is True and receipt['block_reason'] == 'SOURCE_REPORT_MISSING'
    undeclared = build_query_health(value)
    assert undeclared['coverage'] == 'undeclared' and undeclared['analysis_blocked'] is False


# LLM: 同时保留新源结果和退休源欠账，不修改调用者容器。
# 函数用途: 验证确定性差集、缺失降级及登记指引。
def test_drift_and_missing_sources_keep_new_source_hits():
    raw = report([observation('events-new', hits=7)], discovered=['events-new'])
    before = deepcopy(raw)
    value = build_query_health(raw, manifest(), declared_sources=['events-a', 'events-new'])
    assert isinstance(value, dict)
    assert value['classification'] == 'PARTIAL_SOURCE_FAILURE' and not value['analysis_blocked']
    assert value['unregistered_sources_detected'] == ['events-new']
    assert value['registered_sources_missing'] == ['events-a']
    assert value['degraded_sources'] == [{'source_id': 'events-a', 'error_code': 'SOURCE_MISSING'}]
    assert value['healthy_sources_queried'] == 1 and '登记' in value['guidance'] and '移除' in value['guidance']
    assert raw == before


# LLM: core失败优先于其他源的真实命中，失败行的hit_count不算有效数据。
# 函数用途: 验证核心源阻断与零命中不能掩盖失败。
def test_core_failure_blocks_even_with_other_source_hits():
    value = build_query_health(report([observation(status='offline', hits=99), observation('events-b')]),
                               manifest(core=True))
    assert isinstance(value, dict)
    assert value['healthy_sources_queried'] == 1
    assert value['classification'] == 'PARTIAL_SOURCE_FAILURE'
    assert value['analysis_blocked'] and value['block_reason'] == 'QUERY_FAILED_NO_DATA'


# LLM: HTTP状态不能被脚本自报ok或伪造credential_ref覆盖。
# 函数用途: 验证全部红灯入口及引用来自清单。
@pytest.mark.parametrize('status,http_status', [('auth_expired', None), ('endpoint_unreachable', None),
    ('not_found', None), ('ok', 401), ('ok', 403), ('ok', 404)])
def test_red_light_status_and_manifest_credential(status, http_status):
    row = {**observation(status=status, http_status=http_status), 'credential_ref': 'untrusted-ref'}
    value = build_query_health(report([row]), manifest())
    assert isinstance(value, dict)
    assert value['auth_alerts'] == [{'source_id': 'events-a', 'credential_ref': 'cref-a',
        'source_health_status': 'AUTH_OR_ENDPOINT_EXPIRED'}]
    assert value['healthy_sources_queried'] == 0 and value['analysis_blocked']


# LLM: 输入上限在解析循环前施加，未知状态统一失败，坏值不得获得健康身份。
# 函数用途: 验证有界列表和不可信类型的保守归一。
def test_report_caps_and_unknown_values_are_conservative():
    raw = report([observation(f'events-{n}') for n in range(80)],
                 discovered=[f'events-{n}' for n in range(300)])
    value = normalize_source_report(raw)
    assert isinstance(value, dict)
    assert len(value['sources']) == 64 and len(value['discovered_sources']) == 256
    malformed = report([observation(status=['ok']), observation('events-b', hits=True),
                        {'source_id': [], 'status': 'ok'}, False], discovered=[])
    value = build_query_health(malformed, {'sources': []})
    assert value['classification'] == 'TRANSIENT_FAILURE_EMPTY' and value['healthy_sources_queried'] == 0


# LLM: 只认可机器码，不从stdout或其他自然语言字段推断重试。
# 函数用途: 验证瞬态分类入口及非瞬态拒绝。
@pytest.mark.parametrize('row,code,expected', [(None, 'ECONNRESET', 'ECONNRESET'),
    (None, 'ETIMEDOUT', 'ETIMEDOUT'), (None, 'SCRIPT_TIMEOUT', 'SCRIPT_TIMEOUT'),
    (observation(http_status=502), '', '502'), (observation(http_status=503), '', '503'),
    (observation(http_status=504), '', '504'),
    (observation(status='transient_error'), '', 'transient_error'),
    (observation(), 'SCRIPT_EXIT_NONZERO', ''), (observation(status='auth_expired'), '', '')])
def test_transient_errors_use_only_structured_fields(row, code, expected):
    assert transient_failure_code(report([row]) if row else None, code) == expected


# LLM: 写目标与未知目标保守拒绝；显式幂等必须是真布尔值。
# 函数用途: 验证自动重试的权限安全前置条件。
@pytest.mark.parametrize('target,metadata,expected', [
    ({'type': 'http', 'endpoint': 'https://example.invalid', 'method': 'GET'}, {}, True),
    ({'type': 'http', 'endpoint': 'https://example.invalid', 'method': 'POST'}, {}, False),
    ({'type': 'file', 'endpoint': 'events.json', 'extra': {'read_only': True}}, {}, True),
    ({'type': 'file', 'endpoint': 'events.json'}, {}, False),
    ({'type': 'http', 'method': 'POST'}, {'retry_policy': {'idempotent': True}}, True),
    ({'type': 'http', 'method': 'POST'}, {'retry_policy': {'idempotent': 'true'}}, False),
])
def test_retry_requires_readonly_targets_or_explicit_idempotence(target, metadata, expected):
    assert retry_is_safe({'access_targets': [target], 'metadata': metadata}) is expected


# LLM: 重试账是宿主观察，不可由source_report同名字段注入。
# 函数用途: 验证最终成功保留重试证据且不继承旧失败。
def test_retry_receipt_is_a_pure_copy_of_host_attempts():
    retries = [{'attempt': 1, 'error_code': 'ECONNRESET', 'delay_ms': 100}]
    value = build_query_health({**report([observation()]), 'retries': ['injected']}, manifest(), retries)
    assert isinstance(value, dict)
    assert value['retries'] == retries and not value['analysis_blocked']
    value['retries'][0]['delay_ms'] = 200
    assert retries[0]['delay_ms'] == 100


# LLM: 超过上限不能静默丢弃后半段再宣称真零命中。
# 函数用途: 验证有界归一仍保留覆盖不完整这一事实。
def test_capped_report_cannot_produce_clean_zero_hits():
    rows = [observation(f'events-{n}', hits=0) for n in range(65)]
    rows[-1] = observation('events-64', hits=7)
    value = build_query_health(report(rows), {'sources': []})
    assert value['classification'] != 'SUCCESS_ZERO_HITS'
    assert value['analysis_blocked'] is True


# LLM: 原SDK把None合同视作未指定并回退data_sources；坏范围及双层冲突不能获得安全重试。
# 函数用途: 验证重试权限容错与原SDK默认语义一致。
@pytest.mark.parametrize('contract,sources,expected', [
    (None, [{'source_id': 'events-a', 'query_scope': {'access': 'read'}}], True),
    (None, [{'source_id': 'events-a', 'query_scope': {'access': 'read_write'}}], False),
    ({'data_sources': [{'id': 'events-a', 'query_scope': None}]}, [], False),
    ({'data_sources': [{'id': 'events-a', 'access': 'read', 'query_scope': {'access': 'read_write'}}]}, [], False),
    ('invalid', [], False),
])
def test_retry_source_contract_defaults_and_bad_types(contract, sources, expected):
    record = {'access_targets': [{'type': 'http', 'method': 'GET'}],
              'access_contract': contract, 'data_sources': sources}
    assert retry_is_safe(record) is expected
