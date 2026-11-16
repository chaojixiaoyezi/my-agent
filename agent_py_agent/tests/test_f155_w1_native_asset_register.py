"""资产解析只在精确缺失时转向原生登记，保留身份及故障。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_assets import BusinessAssetService


@pytest.mark.parametrize('code', ['HOST_NOT_READY', 'STORE_UNAVAILABLE', 'SPACE_SCOPE_MISMATCH'])
def test_unavailable_never_falls_back(code):
    calls = []
    def reference(kind, *args, **kwargs):
        calls.append(kind)
        raise BusinessError(code)
    service = BusinessAssetService(SimpleNamespace(reference=reference))
    with pytest.raises(BusinessError, match=code):
        service._artifact({'artifact_id': 'id', 'version_ref': 'v'}, actor=None, space_id='s', project_ref=None)
    assert calls == ['artifact_version']


def test_native_fallback_checks_exact_version():
    calls = []
    def reference(kind, ref, **kwargs):
        calls.append((kind, ref))
        if kind == 'artifact_version':
            raise BusinessError('REFERENCE_NOT_FOUND')
        return {'artifact_id': 'id', 'version_ref': 'v', 'state': 'available', 'content_sha256': 'sha', 'space_id': 's', 'project_ref': None}
    service = BusinessAssetService(SimpleNamespace(reference=reference))
    ref = {'artifact_id': 'id', 'version_ref': 'v', 'content_sha256': 'sha'}
    assert service._artifact(ref, actor=None, space_id='s', project_ref=None) == ref
    assert calls == [('artifact_version', 'v'), ('artifact', 'id')]
    with pytest.raises(BusinessError, match='REFERENCE_NOT_FOUND'):
        service._artifact({**ref, 'version_ref': 'wrong'}, actor=None, space_id='s', project_ref=None)


from agent_py_agent.tests.test_registered_run_artifact_routes import (  # noqa: E402
    background_http,
    client_for,
    isolated_home,
    publish_child,
    sampled_owner,
)

_FIXTURES = (background_http, isolated_home, sampled_owner)


def test_native_registration_real_client_http(background_http, sampled_owner, monkeypatch):
    services, task, runtime, parent = sampled_owner
    _, _, record = publish_child(services, task, runtime, parent)
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
    wire_business_host_ports(services.host)
    services.spaces.reference_resolver = services.host.business_space_reference_resolver
    from agent_py_agent.agent.conversation import registered_artifact_observation
    def forbidden_hash(*args, **kwargs):
        raise AssertionError('登记只核元数据，不能读取并hash正文')
    monkeypatch.setattr(registered_artifact_observation, 'hashlib', SimpleNamespace(sha256=forbidden_hash))
    client = client_for(background_http)
    result = client.call('assets.register', {'space_id': task.metadata['business_context']['space_id'],
        'name': '原生产物', 'kind': 'document', 'artifact_ref': {
            'artifact_id': record.artifact_id, 'version_ref': record.sha256,
            'content_sha256': record.sha256}})
    assert result['ok'], result
    assert result['effect_outcome'] == 'committed'


def test_workflow_registration_real_client_http(background_http):
    import hashlib

    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
    from agent_py_agent.tests.test_workflow_business_artifact_download import archived

    services, task, _, artifact_id, _, raw = archived(background_http)
    wire_business_host_ports(services.host)
    services.spaces.reference_resolver = services.host.business_space_reference_resolver
    result = client_for(background_http).call('assets.register', {
        'space_id': task.metadata['business_context']['space_id'], 'name': '工作流原生产物',
        'kind': 'document', 'artifact_ref': {'artifact_id': artifact_id,
            'version_ref': hashlib.sha256(raw).hexdigest()}})
    assert result['ok'], result
    assert result['effect_outcome'] == 'committed'
