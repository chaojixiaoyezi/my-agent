"""R9-R2 P1-R9-06：免密账号经可信SSO签发/验证会话；不创建密码、不开放免密密码登录。

真实 TotpSSOAuthService、state 存储、会话签发与验证链；只替换外部 IdP 协议验签器。
"""
import hashlib

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space import user_auth
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.totp_sso_auth import (
    TotpSSOAuthService,
    VerifiedSSOIdentity,
    authentication_stamp,
    totp_code,
)

BROWSER = 'browser-binding-r9-passwordless'


def _verifier(config, response, nonce):
    # 外部协议替身：只回显本次nonce，state/MAC/映射/会话仍走原产品链。
    if response != 'signed-provider-response':
        raise HostAPIError('SSO_ASSERTION_INVALID', 401)
    return VerifiedSSOIdentity(config['issuer_url'], 'subject-alice', nonce, ('employees',))


def _service(root):
    service = TotpSSOAuthService(root, assertion_verifier=_verifier)
    service.update_sso_config({'protocol': 'oidc', 'issuer_url': 'https://idp.example',
        'client_id': 'client', 'redirect_uri': 'https://portal.example/callback',
        'subject_mapping': {'subject-alice': 'alice'}, 'role_mapping': {'employees': 'USER'}, 'enabled': True})
    return service


def _complete(service, ticket, **extra):
    return service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'],
        response='signed-provider-response', browser_binding=BROWSER, **extra)


def _handshake(service, **extra):
    return _complete(service, service.initiate_sso_login(browser_binding=BROWSER), **extra)


@pytest.fixture
def passwordless(tmp_path):
    LocalUserLifecycle(LocalUserStore(tmp_path)).create('alice', idempotency_key='r9-passwordless-alice')
    assert LocalUserStore(tmp_path).read_user('alice').password_hash is None
    return tmp_path


def test_trusted_sso_issues_and_verifies_passwordless_session(passwordless):
    service = _service(passwordless)
    result = _handshake(service)
    assert result['principal_id'] == 'local/alice' and result['session_token']
    verified = user_auth.verify_user_session(passwordless, result['session_token'])
    assert verified.principal_id == 'local/alice' and verified.owner_id == result['owner_id']
    # 免密账号不因签发而获得密码。
    assert LocalUserStore(passwordless).read_user('alice').password_hash is None


def test_fresh_repeated_handshakes_issue_independent_valid_sessions(passwordless):
    service = _service(passwordless)
    tickets = [service.initiate_sso_login(browser_binding=BROWSER) for _ in range(3)]
    assert len({ticket['state'] for ticket in tickets}) == 3
    for ticket in tickets:
        result = _complete(service, ticket)
        assert user_auth.verify_user_session(passwordless, result['session_token']).principal_id == 'local/alice'
        # 每次握手state仍单次消费；成功签发不放宽重放。
        with pytest.raises(HostAPIError) as replayed:
            _complete(service, ticket)
        assert replayed.value.code == 'SSO_STATE_REPLAYED'


@pytest.mark.parametrize('password', ['', 'any-password', 'TestPassword123!'])
def test_password_login_stays_rejected_for_passwordless_account(passwordless, password):
    _service(passwordless)
    with pytest.raises(HostAPIError) as denied:
        user_auth.authenticate_user(passwordless, account_source='local', user_id='alice', password=password)
    assert denied.value.code == 'INVALID_CREDENTIALS'
    assert LocalUserStore(passwordless).read_user('alice').password_hash is None


def test_mfa_is_required_and_its_activation_revokes_older_passwordless_session(passwordless):
    service = _service(passwordless)
    before = _handshake(service)
    setup = service.setup_totp('alice')
    counter = int(__import__('time').time()) // 30
    service.verify_totp('alice', totp_code(setup['secret'], counter))
    with pytest.raises(HostAPIError) as denied:
        user_auth.verify_user_session(passwordless, before['session_token'])
    assert denied.value.code == 'SESSION_REVOKED'
    with pytest.raises(HostAPIError) as required:
        _handshake(service)
    assert required.value.code == 'TOTP_REQUIRED'
    after = _handshake(service, totp_code=setup['recovery_codes'][0])
    assert user_auth.verify_user_session(passwordless, after['session_token']).principal_id == 'local/alice'


def test_account_disable_and_authentication_stamp_change_revoke_passwordless_sessions(passwordless):
    service = _service(passwordless)
    stamped = _handshake(service)
    old_stamp = authentication_stamp(passwordless, 'alice')
    service.update_sso_config({'role_mapping': {'employees': 'USER', 'contractors': 'USER'}})
    assert authentication_stamp(passwordless, 'alice') != old_stamp
    with pytest.raises(HostAPIError) as revoked:
        user_auth.verify_user_session(passwordless, stamped['session_token'])
    assert revoked.value.code == 'SESSION_REVOKED'
    current = _handshake(service)
    LocalUserLifecycle(LocalUserStore(passwordless)).disable('alice', idempotency_key='r9-passwordless-disable')
    with pytest.raises(HostAPIError):
        user_auth.verify_user_session(passwordless, current['session_token'])
    with pytest.raises(HostAPIError):
        _handshake(service)


def test_existing_password_fingerprints_and_sessions_are_unchanged(tmp_path):
    LocalUserLifecycle(LocalUserStore(tmp_path)).create('bob', password='TestPassword123!',
        idempotency_key='r9-password-account-bob')
    session = user_auth.authenticate_user(tmp_path, account_source='local', user_id='bob', password='TestPassword123!')
    current = user_auth._current_credential(tmp_path, 'local', 'bob', '')
    stamp = authentication_stamp(tmp_path, 'bob')
    legacy = hashlib.sha256((current.password_hash + ('\n' + stamp if stamp else '')).encode()).hexdigest()
    assert current.password_hash and user_auth._credential_fingerprint(tmp_path, current) == legacy
    assert user_auth.verify_user_session(tmp_path, session.session_token).principal_id == 'local/bob'
