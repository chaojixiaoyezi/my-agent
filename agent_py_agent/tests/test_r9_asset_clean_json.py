"""资产净化保留完整安全合同，纯JSON路径不再遍历通用异常/Mapping适配器。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.assets import descriptor


def test_clean_json_exact_scalars_and_strings_keep_redaction_without_generic_dispatch(monkeypatch):
    payload = {'safe': [None, True, False, 42, -3, 0.5, 'ordinary'],
               'apiKey': 'must-not-survive', 'auth-token': 'must-not-survive',
               'notes': ['Bearer synthetic-credential', '--password=synthetic-credential'],
               'nested': {'tokens': 123, 'token_count': 456, 'credential_ref': 'vault/reference'}}
    expected = descriptor.clean_json(payload)
    calls = []
    from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
    original = redact_sensitive_text

    def text(value, **kwargs):
        calls.append(value)
        return original(value, **kwargs)

    def unexpected(*args, **kwargs):
        raise AssertionError('JSON scalar dispatched through generic object redactor')

    monkeypatch.setattr(descriptor, 'redact_sensitive_text', text, raising=False)
    monkeypatch.setattr(descriptor, 'redact_sensitive_value', unexpected)
    actual = descriptor.clean_json(payload)
    assert actual == expected
    assert actual['safe'] == payload['safe']
    assert 'apiKey' not in actual and 'auth-token' not in actual
    assert actual['nested']['tokens'] == 123 and actual['nested']['token_count'] == 456
    assert 'synthetic-credential' not in repr(actual)
    assert 'must-not-survive' not in repr(actual)
    assert len(calls) == 4  # 每个保留的字符串恰好经过原文本净化口。
    actual['safe'].append('changed')
    assert len(payload['safe']) == 7


@pytest.mark.parametrize('value', [float('nan'), float('inf'), b'not-json', {1: 'bad-key'}, object()])
def test_clean_json_still_rejects_non_json_values(value):
    with pytest.raises(ValueError):
        descriptor.clean_json({'nested': value})


def test_descriptor_serialization_rechecks_mutated_nested_secrets_across_threads():
    row = descriptor.AssetDescriptor('safe-asset', 'skill', 'Safe', metadata={'items': [{'note': 'first'}]})
    row.metadata['items'][0].update({'note': 'Bearer synthetic-live-secret', 'api_key': 'synthetic-live-secret'})

    def read(_):
        result = row.to_dict()
        assert 'synthetic-live-secret' not in repr(result)
        assert 'api_key' not in result['metadata']['items'][0]
        result['metadata']['items'][0]['note'] = 'caller-local'
        return result

    with ThreadPoolExecutor(max_workers=17) as pool:
        results = list(pool.map(read, range(51)))
    assert len(results) == 51
    assert row.metadata['items'][0]['note'] == 'Bearer synthetic-live-secret'
