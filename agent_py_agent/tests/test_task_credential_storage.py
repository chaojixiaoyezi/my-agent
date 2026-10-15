"""三种 owner 的真实工具链存储故障；只替换故障点和卡片运输。"""
import errno
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.user_space import task_credentials as credentials
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.tests.test_im_credential_card_preflight import _invoke
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
@pytest.mark.parametrize('failure', ['key', 'corrupt_key', 'record', 'prepare', 'receipt'])
def test_storage_stage_tracks_actual_send(tmp_path, monkeypatch, kind, failure):
    agent = _agent(tmp_path, kind)
    _thread, actor, _claim = _active(agent)
    if kind == 'main':
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', actor.actor_id)
    sent = []
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_a, **_k:
        sent.append(True) or {'message_id': 'om_synthetic', 'chat_id': 'oc_synthetic'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    original = credentials._save_credential_row
    marker = 'SYNTHETIC_SECRET_EXCEPTION_TEXT'
    if failure == 'key':
        def broken_cipher(_home):
            raise PermissionError(errno.EACCES, marker)
        monkeypatch.setattr(credentials, '_cipher', broken_cipher)
    elif failure == 'corrupt_key':
        credentials._cipher(agent.home_paths.owner_home_dir)
        key = credentials.credential_path(agent.home_paths.owner_home_dir, 'cred_' + 'a'*32).parent / 'key.json'
        key.write_text('{"schema_version":"broken","key":"synthetic"}')
    else:
        def save(admission, path, row):
            target = (failure == 'record' and row['delivery_state'] == 'not_started' or
                failure == 'prepare' and row['delivery_state'] == 'started' or
                failure == 'receipt' and row['delivery_state'] == 'confirmed')
            if target:
                raise OSError(errno.ENOSPC, marker)
            return original(admission, path, row)
        monkeypatch.setattr(credentials, '_save_credential_row', save)
    result, _ = _invoke(agent, {'url':'https://example.com', 'authentication':'bearer'})
    assert not result.ok
    assert len(sent) == (1 if failure == 'receipt' else 0)
    assert result.effect_outcome == ('unknown' if failure == 'receipt' else 'not_started')
    assert result.reported_error_code == ('CREDENTIAL_KEY_INVALID' if failure == 'corrupt_key' else 'CREDENTIAL_STORAGE_UNAVAILABLE')
    assert marker not in str(result)
    link = agent.conversation_store.tasks.load(agent._current_run_params.task_id)
    assert bool(link.input_dependency_refs) == (failure == 'receipt')


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
@pytest.mark.parametrize('stage', ['started', 'confirmed'])
def test_same_request_recovers_before_send_but_never_repeats_after_send(tmp_path, monkeypatch, kind, stage):
    agent = _agent(tmp_path, kind)
    _thread, actor, _claim = _active(agent)
    if kind == 'main':
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', actor.actor_id)
    sent = []
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_a, **_k:
        sent.append(True) or {'message_id':'om_synthetic', 'chat_id':'oc_synthetic'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    original = credentials._save_credential_row
    def save(admission, path, row):
        if row['delivery_state'] == stage:
            raise OSError(errno.EROFS, 'SYNTHETIC_NEVER_EXPOSE')
        return original(admission, path, row)
    monkeypatch.setattr(credentials, '_save_credential_row', save)
    args = {'url':'https://example.com', 'authentication':'bearer', 'call_id':'same-operation'}
    with pytest.raises(credentials.CredentialInputError) as error:
        credentials.request_credential_card(agent, **args)
    assert error.value.stage == ('delivery_prepare' if stage == 'started' else 'receipt_commit')
    assert error.value.diagnostic['exception_type'] == 'OSError'
    assert error.value.diagnostic['errno'] == errno.EROFS
    monkeypatch.setattr(credentials, '_save_credential_row', original)
    if stage == 'started':
        credentials.request_credential_card(agent, **args)
        assert len(sent) == 1
    else:
        with pytest.raises(credentials.CredentialInputError) as replay:
            credentials.request_credential_card(agent, **args)
        assert replay.value.code == 'CREDENTIAL_DELIVERY_RECONCILIATION_REQUIRED'
        assert replay.value.effect_outcome == 'unknown'
        assert len(sent) == 1


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
def test_owner_quota_rejection_keeps_safe_type_and_no_input(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable
    agent = _agent(tmp_path, kind)
    _thread, actor, _claim = _active(agent)
    if kind == 'main':
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', actor.actor_id)
    def denied(*_a, **_k):
        raise OwnerQuotaUnavailable('SYNTHETIC_SECRET_QUOTA_TEXT')
    monkeypatch.setattr(agent.owner_quota, 'admission', denied)
    result, _ = _invoke(agent, {'url':'https://example.com', 'authentication':'bearer'})
    assert result.reported_error_code == 'CREDENTIAL_QUOTA_UNAVAILABLE'
    assert result.effect_outcome == 'not_started'
    assert 'SYNTHETIC_SECRET_QUOTA_TEXT' not in str(result)
    assert not agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs


def test_corrupt_record_is_not_missing_and_not_overwritten(tmp_path):
    from agent_py_agent.agent.user_space.task_credential_storage import read_credential_row
    path = tmp_path / 'record.json'
    path.write_text('{SYNTHETIC_SECRET_MALFORMED')
    with pytest.raises(credentials.CredentialInputError) as error:
        read_credential_row(path, stage='record_read')
    assert error.value.code == 'CREDENTIAL_RECORD_INVALID'
    assert error.value.diagnostic == {'exception_type':'JSONDecodeError'}
    assert path.read_text() == '{SYNTHETIC_SECRET_MALFORMED'


@pytest.mark.parametrize('mutation', ['ready', 'generation'])
def test_slow_transport_cannot_overwrite_concurrent_record(tmp_path, mutation):
    import json
    agent = _agent(tmp_path, 'group')
    _active(agent)
    home = agent.home_paths.owner_home_dir
    modified = []
    def deliver(*_a, **_k):
        path = next(credentials.credential_path(home, 'cred_' + 'a'*32).parent.glob('cred_*.json'))
        with credentials._credential_mutation(agent, path) as admission:
            row = credentials.read_credential_row(path, stage='test_concurrent_read')
            if mutation == 'ready':
                row.update(status='ready', ciphertext='synthetic-encrypted-placeholder')
            else:
                row.update(generation=2, input_id=row['credential_ref']+':2', reference='synthetic-new-signed-reference')
            credentials._save_credential_row(admission, path, row)
            modified.append(path)
        return {'message_id':'om_synthetic', 'chat_id':'oc_synthetic'}
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=deliver),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    result, _ = _invoke(agent, {'url':'https://example.com', 'authentication':'bearer'})
    row = json.loads(modified[0].read_text())
    if mutation == 'ready':
        assert result.ok
        assert row['status'] == 'ready'
        assert row['ciphertext'] == 'synthetic-encrypted-placeholder'
    else:
        assert not result.ok and result.reported_error_code == 'CREDENTIAL_RECEIPT_SUPERSEDED'
        assert row['generation'] == 2 and row['reference'] == 'synthetic-new-signed-reference'
        assert not row.get('message_id')


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_strict_record_reader_rejects_aliases(tmp_path, link_kind):
    from agent_py_agent.agent.user_space.task_credential_storage import read_credential_row
    original = tmp_path/'original.json'
    original.write_text('{}')
    alias = tmp_path/'alias.json'
    if link_kind == 'symlink':
        alias.symlink_to(original)
    else:
        alias.hardlink_to(original)
    with pytest.raises((credentials.CredentialInputError, ValueError, OSError)):
        read_credential_row(alias, stage='record_read')
