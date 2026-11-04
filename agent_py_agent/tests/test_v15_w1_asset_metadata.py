"""验证资产元数据的确定性归一化，不读取用户状态。"""
from copy import deepcopy

import pytest

from agent_py_agent.agent.assets import asset_metadata as metadata


# LLM: 输入包括无效成员；输出不得改变输入或依赖原始顺序。
# 函数用途: 验证归属去重、排序、安全标识和容量。
def test_member_of_mixed_sorted_capped():
    value = ['workflow:z', {'kind': 'skill', 'id': 'alpha'}, 'skill:alpha',
             {'kind': 'script', 'id': 'ignored'}, {'kind': [], 'id': 'bad'},
             'skill:../outside', 9, None, 'workflow:a:b']
    original = deepcopy(value)
    assert metadata.normalize_member_of(value) == [
        {'kind': 'skill', 'id': 'alpha'}, {'kind': 'workflow', 'id': 'z'}]
    assert value == original
    large = [f'workflow:w{i:02d}' for i in reversed(range(24))]
    assert metadata.normalize_member_of(large) == [
        {'kind': 'workflow', 'id': f'w{i:02d}'} for i in range(16)]


# LLM: 错误类型不是一条可用归属，不抛错或从字符串逐字推断。
# 函数用途: 验证归属错误类型容忍。
@pytest.mark.parametrize('value', [None, {}, 'skill:one', 4, True, ([],), [[], {}, '', 'skill:']])
def test_member_of_bad_input(value):
    assert metadata.normalize_member_of(value) == []


# LLM: 枚举与跨分片合同逐项一致，字符串顺序也是接口的一部分。
# 函数用途: 防止触发类型枚举漂移。
def test_indicator_enum_contract():
    assert metadata.INDICATOR_TYPES == (
        'ipv4', 'ipv6', 'domain', 'url', 'email', 'md5', 'sha1', 'sha256',
        'cve_id', 'hostname', 'port', 'process_name', 'file_path', 'alert_id',
        'monitor_alias', 'image_attachment')


# LLM: 四个字段各守类型和上限，不把提示字段解释为权限。
# 函数用途: 验证匹配器去重、清洗、保序和幂等。
def test_trigger_matchers_normalize():
    value = {'indicator_types': ['ipv4', 'unknown', 'ipv6', 'ipv4', {}],
             'monitor_ids': ['job48', '../bad', 'job48', 'job49', None],
             'intent_keywords': ['  ALPHA\nBeta  ', 'alpha beta', 'x' * 50, '\x00'],
             'alert_sources': ['source\nname', 'source name', 'x' * 90, 1],
             'ignored': ['never']}
    original = deepcopy(value)
    result = metadata.normalize_trigger_matchers(value)
    assert result == {'indicator_types': ['ipv4', 'ipv6'], 'monitor_ids': ['job48', 'job49'],
                      'intent_keywords': ['alpha beta', 'x' * 32],
                      'alert_sources': ['source name', 'x' * 64]}
    assert metadata.normalize_trigger_matchers(result) == result
    assert value == original


# LLM: 大输入不得突破各字段独立容量，也不能因字段类型错误报错。
# 函数用途: 验证匹配器容量及错误类型。
def test_trigger_matchers_limits_and_types():
    result = metadata.normalize_trigger_matchers({
        'indicator_types': list(metadata.INDICATOR_TYPES) * 3,
        'monitor_ids': [f'job{i}' for i in range(24)],
        'intent_keywords': [f'KEY{i}' for i in range(24)],
        'alert_sources': [f'source{i}' for i in range(24)]})
    assert result is not None
    assert [len(result[key]) for key in ('indicator_types', 'monitor_ids', 'intent_keywords', 'alert_sources')] == [16, 16, 16, 8]
    empty = {'indicator_types': [], 'monitor_ids': [], 'intent_keywords': [], 'alert_sources': []}
    for value in (None, 1, [], 'ipv4', {'indicator_types': 'ipv4', 'monitor_ids': {}, 'intent_keywords': 9}):
        assert metadata.normalize_trigger_matchers(value) == empty


# LLM: source_system 是用户定义字段，宿主管理键只从副本顶层剥离。
# 函数用途: 验证精确宿主键集与隔离拷贝。
def test_strip_host_metadata_contract_and_copy():
    expected = ('used_by', 'known_pitfalls', 'version', 'versions', 'version_info',
                'draft', 'evolution', 'hidden', 'composition', 'query_health', 'runtime_summary')
    assert expected == metadata.HOST_MANAGED_METADATA_KEYS
    value = dict.fromkeys(expected, True) | {'source_system': 'local', 'custom': {'version': 2}}
    original = deepcopy(value)
    result = metadata.strip_host_managed_metadata(value)
    assert result is not None
    clean, stripped = result
    assert clean == {'source_system': 'local', 'custom': {'version': 2}}
    assert stripped == sorted(expected)
    clean['custom']['version'] = 3
    assert value == original
    assert metadata.strip_host_managed_metadata(None) == ({}, [])
