"""已安装内容包沿正常业务HTTP目录、预览和挂载共用Owner事实；不手工装配全套宿主端口。"""
# ruff: noqa: F811

import pytest

from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_business_v12_background_scheduling import native  # noqa: F401
from agent_py_agent.tests.test_session_space_selection_wire import client_for


def write_call(client, action, params, **kwargs):
    return client.send(client.prepare(action, params, **kwargs))


def install_content(agent, name='mount-proof', *, enabled=True):
    package = inspect_plugin_package(build_capability_pack({'plugin_id': name, 'version': '1.0.0',
        'summary': '合成内容包', 'capability': {'description': '挂载验证', 'keywords': ['mount'],
            'entry_document': 'README.md'}, 'files': [{'path': 'README.md'}], 'settings_schema': {}},
        {'README.md': b'# isolated mount proof\n'}).payload)
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    entry = store.install(PluginInstallRequest(package, 'install-' + name, 0)).installation
    if enabled:
        activation = PluginContentActivation('activate-' + name, name, entry.package_sha256,
            entry.revision, entry.settings_revision)
        entry = store.change_activation(PluginActivationRequest(activation.operation_id,
            entry.revision, activation)).installation
    return entry


def test_normal_http_installed_catalog_preview_and_apply_share_original_package(native):  # noqa: F811
    from agent_py_agent.agent.gateway_parts.business_host_ports import (
        is_business_host_ports_enabled,
    )

    entry = install_content(native[1])
    assert not is_business_host_ports_enabled(native[1])
    installation = native[1].home_paths.owner_home_dir / 'data/plugins/installations.json'
    before = installation.read_bytes()
    client = client_for(native, 'mount-proof')
    space = client.call('spaces.create', {'name': '装配验证空间'})
    assert space['ok'], space
    sid = space['data']['space_id']
    catalog = client.call('spaces.packages.catalog', {})
    assert catalog['ok'], catalog
    [card] = catalog['data']['items']
    assert card['availability']['state'] == 'available'
    ref = card['package_ref']
    assert card['version_ref'] == entry.package_sha256
    preview = client.call('spaces.packages.preview', {'space_id': sid, 'mounts': [{'package_ref': ref}]})
    assert preview['ok'] and preview['data']['unavailable'] == [], preview
    attached = write_call(client, 'spaces.packages.attach', {'space_id': sid, 'package_refs': [ref]}, expected_revision=0)
    assert attached['ok'], attached
    [mount] = attached['data']['mounts']
    assert mount['package_ref'] == ref and mount['version_ref'] == entry.package_sha256
    assert mount['availability']['state'] == 'available'
    assert installation.read_bytes() == before
    # 内容挂载不得顺便启用其它宿主对象端口。
    capabilities = client.call('system.capabilities', {})
    assert capabilities['ok'] and 'interfaces.list' not in capabilities['data']['supported_actions']


def test_installed_package_mount_rejects_mixed_invalid_batch_without_writes(native):
    install_content(native[1])
    client = client_for(native, 'atomic-packages')
    sid = client.call('spaces.create', {'name': '原子整批'})['data']['space_id']
    ref = client.call('spaces.packages.catalog', {})['data']['items'][0]['package_ref']
    attached = write_call(client, 'spaces.packages.attach', {'space_id': sid, 'package_refs': [ref]}, expected_revision=0)
    assert attached['ok'], attached
    before = client.call('spaces.get', {'space_id': sid})['data']
    failure = write_call(client, 'spaces.packages.set', {'space_id': sid, 'mounts': [
        {'package_ref': ref, 'enabled': False}, {'package_ref': 'capability:absent'}]}, expected_revision=1)
    assert not failure['ok'] and failure['error']['code'] == 'REFERENCE_NOT_FOUND', failure
    assert client.call('spaces.get', {'space_id': sid})['data'] == before
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        write_call(client, 'spaces.packages.set', {'space_id': sid,
            'mounts': [{'package_ref': ref}, {'package_ref': ref}]}, expected_revision=1)
    assert client.call('spaces.get', {'space_id': sid})['data'] == before


def test_two_spaces_share_installation_and_keep_independent_preferences(native):
    install_content(native[1], 'first-pack')
    install_content(native[1], 'second-pack', enabled=False)
    client = client_for(native, 'preferences')
    refs = [row['package_ref'] for row in client.call('spaces.packages.catalog', {})['data']['items']]
    installation = native[1].home_paths.owner_home_dir / 'data/plugins/installations.json'
    before = installation.read_bytes()
    spaces = [client.call('spaces.create', {'name': name})['data']['space_id'] for name in ('空间甲', '空间乙')]
    for sid, order in zip(spaces, (refs, refs[::-1])):
        response = write_call(client, 'spaces.packages.set', {'space_id': sid, 'mounts': [
            {'package_ref': ref, 'enabled': idx == 0} for idx, ref in enumerate(order)]}, expected_revision=0)
        assert response['ok'], response
        assert [m['package_ref'] for m in response['data']['mounts']] == order
    assert installation.read_bytes() == before
    states = {m['availability']['state'] for m in client.call('spaces.get', {'space_id': spaces[0]})['data']['mounts']}
    assert states == {'available', 'disabled'}


def test_package_owner_source_cannot_be_substituted(native, tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    from agent_py_agent.agent.gateway_parts.business_host_ports import (
        build_default_business_package_reference_resolver,
    )

    install_content(native[1])
    agent = native[1]
    resolver = build_default_business_package_reference_resolver(agent)
    other = SimpleNamespace(owner_id=agent.home_paths.owner_id, owner_root=tmp_path / 'foreign-owner')
    actor = BusinessActor(other.owner_id, 'foreign', other, lambda *_: True)
    with pytest.raises(BusinessError, match='SPACE_SCOPE_MISMATCH'):
        resolver('package', 'capability:mount-proof', actor)
    mismatched_host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local:other',
        owner_home_dir=agent.home_paths.owner_home_dir))
    with pytest.raises(BusinessError, match='SPACE_SCOPE_MISMATCH'):
        build_default_business_package_reference_resolver(mismatched_host)('package', 'capability:mount-proof', None)


def test_missing_package_wiring_mutation_reproduces_original_disagreement(native, monkeypatch):
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService

    install_content(native[1])
    client = client_for(native, 'unwired-negative-control')
    sid = client.call('spaces.create', {'name': '负对照'})['data']['space_id']
    card = client.call('spaces.packages.catalog', {})['data']['items'][0]
    assert card['availability']['state'] == 'available'
    original = BusinessSpaceService.reference

    def unwired(service, kind, ref, **kwargs):
        resolver = service.package_reference_resolver
        service.package_reference_resolver = None
        try:
            return original(service, kind, ref, **kwargs)
        finally:
            service.package_reference_resolver = resolver

    monkeypatch.setattr(BusinessSpaceService, 'reference', unwired)
    unavailable = client.call('spaces.packages.preview', {'space_id': sid,
        'mounts': [{'package_ref': card['package_ref']}]})['data']['unavailable']
    assert unavailable[0]['reason_code'] == 'HOST_NOT_READY'
    monkeypatch.undo()
    assert client.call('spaces.packages.preview', {'space_id': sid,
        'mounts': [{'package_ref': card['package_ref']}]})['data']['unavailable'] == []
