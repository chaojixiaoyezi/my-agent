# LLM: A2返工的真实认证磁盘和Host HTTP回归，只使用临时Owner及合成种子，不触碰生产认证资料。
# 模块用途: 守住TOTP密文、单步显式副作用许可与包摘要错误码。
from __future__ import annotations

import base64
import hashlib
import json
import shutil

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService, totp_code
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
from agent_py_agent.tests.test_plugin_package import _bundle
from agent_py_agent.tests.test_wave9_host_substrate_plugins_workflow_auth import (
    definition,
    local_user,
    script,
)
from agent_py_agent.tests.test_workflow_engine import assert_debug_contract, observe_script_runs

pytest_plugins = ('agent_py_agent.tests.test_plugin_host_gateway_composition',)


# LLM: 重建服务实例强制从磁盘读回；不使用私有解密器生成预期动态码。
# 函数用途: 验证待激活和已激活JSON均无明文且可跨重载认证。
def test_totp_ciphertext_pending_active_and_reloaded_confirmation(tmp_path):
    local_user(tmp_path)
    clock = [1700000010]
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock[0])
    setup = service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in json.loads(path.read_text())
    assert json.loads(path.read_text())['secret_ciphertext']
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock[0])
    assert service.confirm_totp('alice', totp_code(setup['secret'], clock[0] // 30))['enabled']
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in json.loads(path.read_text())
    clock[0] += 30
    assert TotpSSOAuthService(tmp_path, clock=lambda: clock[0]).verify_totp(
        'alice', totp_code(setup['secret'], clock[0] // 30))['enabled']
    keys = list((tmp_path / 'local_users_auth/.keys').glob('*.key'))
    assert len(keys) == 1 and len(keys[0].read_bytes()) == 32
    assert keys[0].stat().st_mode & 0o777 == 0o600
    assert keys[0].parent.stat().st_mode & 0o777 == 0o700


# LLM: 篡改真实密文/认证标签或移植不同UID，产品必须在任何计数消费前失败关闭。
# 函数用途: 防止未认证加密、跨Owner拷贝和丢失密钥后的静默重新生成。
@pytest.mark.parametrize('attack', ['ciphertext', 'tag', 'owner', 'missing_key', 'bad_key', 'linked_key'])
def test_totp_envelope_tampering_and_key_loss_fail_closed(tmp_path, attack):
    local_user(tmp_path)
    local_user(tmp_path, 'bob')
    clock = 1700000010
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock)
    setup = service.setup_totp('alice')
    service.setup_totp('bob')
    path = tmp_path / 'local_users_auth/alice.json'
    row = json.loads(path.read_text())
    assert 'secret_ciphertext' in row
    keys = tmp_path / 'local_users_auth/.keys'
    key = keys / 'alice.key'
    original_key = key.read_bytes()
    if attack in ('ciphertext', 'tag'):
        raw = bytearray(base64.b64decode(row['secret_ciphertext'], altchars=b'-_', validate=True))
        raw[20 if attack == 'ciphertext' else -1] ^= 1
        row['secret_ciphertext'] = base64.urlsafe_b64encode(raw).decode()
        path.write_text(json.dumps(row))
    elif attack == 'owner':
        shutil.copyfile(path, path.with_name('bob.json'))
        shutil.copyfile(key, keys / 'bob.key')
    elif attack == 'missing_key':
        key.unlink()
    elif attack == 'bad_key':
        key.write_bytes(b'bad')
    else:
        target = tmp_path / 'external.key'
        target.write_bytes(original_key)
        key.unlink()
        key.symlink_to(target)
    with pytest.raises(HostAPIError) as error:
        TotpSSOAuthService(tmp_path, clock=lambda: clock).verify_totp(
            'bob' if attack == 'owner' else 'alice', totp_code(setup['secret'], clock // 30))
    assert error.value.code == 'STORE_UNAVAILABLE'
    assert not key.exists() if attack == 'missing_key' else True


# LLM: 旧版本记录由合成数据构造；迁移不重置防重放或epoch，错误验证码也不能再次保存明文。
# 函数用途: 验证旧认证记录首次消费时原位密文化且保留原MFA身份。
def test_totp_legacy_plaintext_is_migrated_without_resetting_epoch(tmp_path):
    local_user(tmp_path)
    clock = 1700000010
    service = TotpSSOAuthService(tmp_path, clock=lambda: clock)
    setup = service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    row = json.loads(path.read_text())
    row.pop('secret_ciphertext', None)
    row.update(secret=setup['secret'], enabled=True, auth_epoch='original-epoch')
    path.write_text(json.dumps(row))
    with pytest.raises(HostAPIError):
        service.verify_totp('alice', 'not-a-code')
    current = json.loads(path.read_text())
    assert setup['secret'] not in path.read_text()
    assert 'secret' not in current and current['auth_epoch'] == 'original-epoch'
    assert service.verify_totp('alice', totp_code(setup['secret'], clock // 30))['enabled']


# LLM: 独立复核指出的setup旁路；已有pending密文不能因丢钥、换钥或损坏被新设置静默覆盖。
# 函数用途: 保存并对照原记录，要求重新setup在坏密钥/封套时无写入失败。
@pytest.mark.parametrize('attack', ['missing_key', 'changed_key', 'ciphertext'])
def test_totp_pending_setup_does_not_hide_key_loss_or_corruption(tmp_path, attack):
    local_user(tmp_path)
    service = TotpSSOAuthService(tmp_path)
    service.setup_totp('alice')
    path = tmp_path / 'local_users_auth/alice.json'
    key = tmp_path / 'local_users_auth/.keys/alice.key'
    if attack == 'missing_key':
        key.unlink()
    elif attack == 'changed_key':
        key.write_bytes(b'x' * 32)
    else:
        row = json.loads(path.read_text())
        row['secret_ciphertext'] = 'invalid-envelope'
        path.write_text(json.dumps(row))
    before = path.read_bytes()
    with pytest.raises(HostAPIError) as error:
        service.setup_totp('alice')
    assert error.value.code == 'STORE_UNAVAILABLE' and path.read_bytes() == before
    if attack == 'missing_key':
        assert not key.exists()


# LLM: 使用真实Gateway授权、闭合解码和回环HTTP；摘要失败必须早于安装副作用。
# 函数用途: 两种摘要字段的坏格式及错误摘要必须在领域层统一400，不能先被schema改成另一错误码。
@pytest.mark.parametrize('field', ['sha256', 'expected_sha256'])
@pytest.mark.parametrize('value', ['', 'bad', 'g' * 64, 'a' * 63, 'a' * 65, '0' * 64])
def test_host_package_sha256_mismatch_code(production_host, field, value):
    status, body = http_request(production_host[0], production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': 'plugins.packages.install',
        'idempotency_key': 'round3-sha256-mismatch',
        'payload': {'package_base64': base64.b64encode(_bundle()).decode(), field: value}})
    assert status == 400
    assert body['error']['code'] == 'PACKAGE_SHA256_MISMATCH'


# LLM: 真实HTTP透传同一沙箱执行器，诊断不写只读Owner；不mock执行结果或外层Seatbelt判断。
# 函数用途: 核验默认零真实调用，显式许可只调用一次且返回真实stderr/输出，不写正式run。
@pytest.mark.parametrize('mode', ['deterministic_script', 'script'])
def test_host_debug_default_preview_and_explicit_execution(production_host, monkeypatch, mode):
    home = production_host[2].home_dir
    calls = observe_script_runs(monkeypatch)
    script(home, 'marker', 'import sys; print("MARKER_EXECUTED", file=sys.stderr); print(\'{"value":3}\')')
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode, 'script_id': 'marker'}))
    data = {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 4}}
    preview = production_call(production_host, 'workflows.steps.debug', data)
    assert calls == [] and 'MARKER_EXECUTED' not in '\n'.join(preview['logs'])
    assert preview['dry_run'] and preview['side_effects_blocked']
    assert preview['rendered_input'] == {'n': 4} and preview['condition_branch'] is None
    actual = production_call(production_host, 'workflows.steps.debug', {**data, 'allow_side_effects': True})
    assert calls == ['marker'] and 'MARKER_EXECUTED' in '\n'.join(actual['logs'])
    assert actual['output_data'] == {'value': 3} and actual['status'] == 'succeeded'
    assert not actual['dry_run'] and not (home / 'data/workflow_runs').exists()


# LLM: 无live LLM时只返回可辨认的确定性mock预览，不能把echo说成模型已执行。
# 函数用途: 验证Host分析节点默认可调试、可mock，且不发外部请求。
@pytest.mark.parametrize('mode', ['llm_prompt', 'llm_skill_analysis'])
def test_host_debug_llm_fallback_is_safe_deterministic_and_mock_capable(production_host, mode):
    home = production_host[2].home_dir
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode,
        'config': {'mock_output': {'verdict': 'fixture'}}}))
    result = production_call(production_host, 'workflows.steps.debug',
        {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 2}})
    assert result['status'] == 'succeeded' and result['dry_run'] and result['side_effects_blocked']
    assert result['output_data'] == {'mock': True, 'llm_called': False, 'output': {'verdict': 'fixture'}}
    assert result['rendered_input'] == {'n': 2} and result['condition_branch'] is None
    assert not (home / 'data/workflow_runs').exists()


@pytest.mark.parametrize('field', ['sha256', 'expected_sha256', 'both'])
def test_host_package_digest_aliases_accept_valid_hex(production_host, field):
    raw = _bundle()
    digest = hashlib.sha256(raw).hexdigest()
    fields = {'sha256': digest, 'expected_sha256': digest.upper()} if field == 'both' else {field: digest.upper()}
    result = production_call(production_host, 'plugins.packages.install',
        {'package_base64': base64.b64encode(raw).decode(), **fields}, key='round3-valid-digest')
    assert result['sha256'] == digest


@pytest.mark.parametrize('bad', ['0' * 64, 'bad'])
@pytest.mark.parametrize('bad_field', ['sha256', 'expected_sha256'])
def test_host_package_digest_aliases_cannot_override_each_other(production_host, bad_field, bad):
    raw = _bundle()
    fields = dict.fromkeys(('sha256', 'expected_sha256'), hashlib.sha256(raw).hexdigest())
    fields[bad_field] = bad
    status, body = http_request(production_host[0], production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': 'plugins.packages.install', 'idempotency_key': 'round3-digest-conflict',
        'payload': {'package_base64': base64.b64encode(raw).decode(), **fields}})
    assert status == 400 and body['error']['code'] == 'PACKAGE_SHA256_MISMATCH'


@pytest.mark.parametrize('mode', ['condition_branch', 'approval_wait', 'notification'])
def test_host_debug_contract_aliases_and_new_control_modes(production_host, mode):
    home = production_host[2].home_dir
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': mode,
        'config': {'expression': "inputs['n'] > 3", 'title': 'fixture', 'message': 'notice'}}))
    data = {'workflow_id': 'debug-flow', 'step_id': 'one', 'mock_inputs': {'n': 4}}
    preview = production_call(production_host, 'workflows.steps.debug', data)
    assert_debug_contract(preview, mode)
    assert preview['rendered_input'] == {'n': 4}
    if mode == 'condition_branch':
        assert preview['condition_branch'] is True and preview['output'] == {'matched': True}
        assert not preview['side_effects_suppressed'] and not preview['dry_run']
    else:
        assert preview['side_effects_suppressed'] and preview['dry_run'] and preview['output'] == {}
        result = production_call(production_host, 'workflows.steps.debug', {**data, 'allow_side_effects': True})
        assert_debug_contract(result, mode)
        assert result['status'] == 'succeeded' and not result['side_effects_suppressed']
    assert not (home / 'data/workflow_runs').exists()
