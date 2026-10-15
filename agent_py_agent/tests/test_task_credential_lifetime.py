from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.user_space import task_credentials
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_headers,
    manage_credential,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card

_FIXTURES = (background,)


@pytest.mark.parametrize('options', [None, CredentialCardOptions(ttl_seconds=0)])
def test_saved_credential_reusable_without_automatic_expiry(background, monkeypatch, options):
    ref, path, body, _ = _card(background, options=options)
    pending = read_json_object_report(path, context='test').payload
    assert pending['expires_at'] > 0
    assert pending['expires_at'] - __import__('time').time() <= 1800
    assert resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    saved = read_json_object_report(path, context='test').payload
    assert saved['ttl_seconds'] == 0 and saved['ready_expires_at'] == 0
    monkeypatch.setattr(task_credentials, 'time', SimpleNamespace(time=lambda: saved['submitted_at'] + 63072000))
    for _ in range(2):
        assert credential_headers(background.agent, ref, 'https://example.com/api')[0]['Authorization']
    assert manage_credential(background.agent, ref, operation='status')['expires_at'] == 0
    with pytest.raises(ValueError, match='不属于'):
        credential_headers(background.agent, ref, 'https://other.example/api')
    manage_credential(background.agent, ref, operation='revoke')
    with pytest.raises(ValueError, match='已撤销'):
        credential_headers(background.agent, ref, 'https://example.com/api')


def test_explicit_finite_credential_keeps_absolute_deadline(background, monkeypatch):
    ref, path, body, _ = _card(background, options=CredentialCardOptions(ttl_seconds=60))
    assert resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    row = read_json_object_report(path, context='test').payload
    monkeypatch.setattr(task_credentials, 'time', SimpleNamespace(time=lambda: row['ready_expires_at'] + 1))
    with pytest.raises(ValueError, match='已过期'):
        credential_headers(background.agent, ref, 'https://example.com/api')
    assert manage_credential(background.agent, ref, operation='status')['status'] == 'expired'
    assert read_json_object_report(path, context='test').payload == row


def test_capability_default_is_used_and_explicit_request_wins(background):
    background.agent._capability_config_runtime_snapshot = SimpleNamespace(
        config=CapabilityConfig(im_task_credentials_ttl_seconds=120))
    _, path, _, _ = _card(background)
    assert read_json_object_report(path, context='test').payload['ttl_seconds'] == 120
    _, explicit_path, _, _ = _card(background, options=CredentialCardOptions(ttl_seconds=0))
    assert read_json_object_report(explicit_path, context='test').payload['ttl_seconds'] == 0


def test_rejection_notice_requires_original_current_scope_and_generation(background):
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.agent.user_space.task_credential_verification import (
        record_credential_verification,
    )
    from agent_py_agent.agent.user_space.task_credentials import credential_rejection_current
    ref, path, body, _ = _card(background)
    assert resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    row = read_json_object_report(path, context='test').payload
    assert not credential_rejection_current(background.agent, ref, row['generation'])
    record_credential_verification(background.agent, path, row, RawResponseParts(401, {}, b'', 'https://example.com/api'))
    assert credential_rejection_current(background.agent, ref, row['generation'])
    assert not credential_rejection_current(background.agent, ref, row['generation'] + 1)
    assert not credential_rejection_current(background.agent, ref, True)
    manage_credential(background.agent, ref, operation='revoke')
    assert not credential_rejection_current(background.agent, ref, row['generation'])


@pytest.mark.parametrize('deadline,ttl,expired', [(0, 0, False), (0, 86400, True), (None, 0, True),
                                               (False, 0, True), (float('nan'), 0, True),
                                               (float('inf'), 0, True), (10**400, 0, True), (0, False, True),
                                               (-1, 0, True), (100, 0, True), (101, 0, False)])
def test_ready_deadline_is_explicit_and_malformed_fails_closed(deadline, ttl, expired):
    from agent_py_agent.agent.user_space.task_credential_schema import credential_ready_expired
    row = {'ttl_seconds': ttl}
    if deadline is not None:
        row['ready_expires_at'] = deadline
    assert credential_ready_expired(row, now=100) is expired


@pytest.mark.parametrize('value', [0, 60, 31536000])
def test_default_retention_config_accepts_unlimited_or_finite(tmp_path, value):
    path = tmp_path / 'capability.yaml'
    path.write_text(f'im_task_credentials_ttl_seconds: {value}\n')
    assert load_capability_config(path).im_task_credentials_ttl_seconds == value
    assert CapabilityConfig().im_task_credentials_ttl_seconds == 0


@pytest.mark.parametrize('value', ['true', '-1', '1', '31536001', '"0"'])
def test_default_retention_config_rejects_invalid_values(tmp_path, value):
    path = tmp_path / 'capability.yaml'
    path.write_text(f'im_task_credentials_ttl_seconds: {value}\n')
    with pytest.raises(ValueError):
        load_capability_config(path)


@pytest.mark.parametrize('value,valid', [(0, True), (60, True), (31536000, True), (59, False),
                                      (-1, False), (31536001, False), (False, False), (0.0, False)])
def test_tool_schema_matches_supported_retention_values(value, valid):
    from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
    from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
    schema = RequestCredentialsTool(SimpleNamespace()).model_spec.input_schema
    result = validate_tool_input({'operation': 'request', 'ttl_seconds': value}, schema)
    assert result.ok is valid
