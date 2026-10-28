# LLM: 原底座回归继续验证真实脚本/沙箱/超时，按新合同显式allow_side_effects=True；默认安全预览另有独立回归。
# 模块用途: 验证归档、显式许可的无账本调试及认证边界，不访问生产账号或IdP。
from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile

import pytest

from agent_py_agent.agent import plugin_package
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_package import _bundle

pytest_plugins = ('agent_py_agent.tests.test_plugin_host_gateway_composition',)


# LLM: 读取原凭据路径裁决，不通过测试专属保护列表掩盖遗漏。
# 函数用途: 锁定新认证目录不能由模型文件工具读取。
def test_new_auth_store_is_protected_from_model_file_tools(tmp_path):
    from agent_py_agent.agent.path_access_policy import host_credential_for_path
    assert host_credential_for_path(tmp_path / 'local_users_auth/alice.json', tmp_path) is not None


# LLM: 仅内存生成测试归档；可注入危险头，不在文件系统解包。
# 函数用途: 将既有合法ZIP或一个恶意成员转成tar.gz测试输入。
def tar_bundle(content=None, *, name=None, kind=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        if name is None:
            with zipfile.ZipFile(io.BytesIO(content or _bundle())) as source:
                for member in source.infolist():
                    raw = source.read(member)
                    info = tarfile.TarInfo(member.filename)
                    info.size = len(raw)
                    archive.addfile(info, io.BytesIO(raw))
        else:
            info = tarfile.TarInfo(name)
            info.type = kind or tarfile.REGTYPE
            info.linkname = 'plugin.json' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            archive.addfile(info, io.BytesIO())
    return data.getvalue()


# LLM: 始终调用真实产品入口，不用测试替身模拟归档接受或拒绝。
# 函数用途: 检查新入口存在并规范化测试包。
def normalize(raw):
    normalize_archive = getattr(plugin_package, 'normalize_plugin_archive_bytes', None)
    assert callable(normalize_archive), '缺少有界归档规范化入口'
    return normalize_archive(raw)


# LLM: 使用既有合法包，原ZIP字节不变，tar结果必须再次通过原校验器。
# 函数用途: 验证归档规范化确定性及manifest一致性。
def test_zip_preserved_and_tar_normalizes_to_verified_canonical_zip():
    raw = _bundle()
    assert normalize(raw) == raw
    first = normalize(tar_bundle(raw))
    second = normalize(tar_bundle(raw))
    assert first == second
    assert plugin_package.inspect_plugin_package(first).manifest == plugin_package.inspect_plugin_package(raw).manifest


# LLM: 每个输入都是独立危险成员，不以解包后路径清洗替代事前拒绝。
# 函数用途: 覆盖点段、绝对路径、链接和特殊tar成员。
@pytest.mark.parametrize('name,kind', [
    ('../plugin.json', None), ('/plugin.json', None), ('a/../plugin.json', None),
    ('a\\plugin.json', None), ('C:/plugin.json', None),
    ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE), ('dev', tarfile.CHRTYPE),
    ('fifo', tarfile.FIFOTYPE),
])
def test_tar_dangerous_members_rejected(name, kind):
    with pytest.raises(PluginPackageError):
        normalize(tar_bundle(name=name, kind=kind))


# LLM: 实际构造越过预算的归档，不能仅断言常量或mock头部结果。
# 函数用途: 验证成员数及解压总量硬上限。
def test_tar_member_and_expansion_bombs_rejected():
    for count, size in ((257, 0), (1, 10 * 1024 * 1024 + 1)):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for number in range(count):
                info = tarfile.TarInfo(f'f{number}')
                info.size = size
                archive.addfile(info, io.BytesIO(b'0' * size))
        with pytest.raises(PluginPackageError):
            normalize(buffer.getvalue())


# LLM: 拼接第二tar的危险头必须被检查，EOF零块不是信任边界。
# 函数用途: 防止归档尾部隐藏的成员绕过完整预检。
def test_tar_trailing_archive_members_cannot_hide_after_eof():
    import gzip
    combined = gzip.decompress(tar_bundle()) + gzip.decompress(tar_bundle(name='../hidden'))
    with pytest.raises(PluginPackageError):
        normalize(gzip.compress(combined))


# LLM: 只组装原闭合WorkflowStore协议，不注入测试专属执行分支。
# 函数用途: 为单节点与暂停恢复回归构造最小真实工作流定义。
def definition(*steps):
    return {'descriptor': {'name': 'debug-flow', 'id': 'debug-flow', 'kind': 'workflow',
                           'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': [{'name': step['step_id'], **step} for step in steps]}


# LLM: 只写测试临时Owner，使用原ScriptStore保存合法代码与IO合同。
# 函数用途: 准备输出value字段的真实Python脚本资产。
def script(home, script_id, code):
    return ScriptStore(home).save_script(AssetDescriptor.from_dict({'name': script_id, 'id': script_id,
        'kind': 'script', 'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {'outputs': ['value']}}), code, interpreter='python3', entrypoint='main.py')


# LLM: 旧真实执行用例显式请求副作用，不删除输出/超时/文件断言；安全默认由新测试直调产品验证。
# 函数用途: 调用真实单步执行并保留全部原断言，适配新的默认只预览合同。
def debug(engine, *args, **kwargs):
    call = getattr(engine, 'debug_step', None)
    assert callable(call), '缺少不写账的单节点调试入口'
    kwargs.setdefault('allow_side_effects', True)
    return call(*args, **kwargs)


# LLM: 真脚本执行配合原写端陷阱及前后文件对照，不用mock执行伪装零写入。
# 函数用途: 验证mock输入、只跑选定节点以及零run/摘要变更。
def test_debug_only_selected_step_mock_inputs_and_no_ledger_or_summary(tmp_path, monkeypatch):
    script(tmp_path, 'double', 'import json,sys; print(json.dumps({"value":json.load(sys.stdin)["number"]*2}))')
    WorkflowStore(tmp_path).save_workflow(definition(
        {'step_id': 'upstream', 'mode': 'deterministic_script', 'script_id': 'double'},
        {'step_id': 'selected', 'mode': 'deterministic_script', 'script_id': 'double',
         'inputs_map': {'number': {'step_id': 'upstream', 'key': 'value'}}}))
    engine = WorkflowEngine(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')}
    # LLM: 任何被调用的持久写端立即令测试失败，不能静默吞掉写账。
    # 函数用途: 捕获调试意外调用正式运行或资产摘要保存入口。
    def forbidden(*args, **kwargs):
        pytest.fail('debug不能写运行账或推进脚本/工作流摘要')
    for method in ('create_run', 'save_run', 'save_step_result'):
        monkeypatch.setattr(engine.store, method, forbidden)
    monkeypatch.setattr(ScriptStore, 'record_run_summary', forbidden)
    monkeypatch.setattr(WorkflowStore, 'record_run_summary', forbidden)
    result = debug(engine, 'debug-flow', 'selected', mock_inputs={'number': 7})
    assert result['ok'] is True and result['status'] == 'succeeded'
    assert result['output_data'] == {'value': 14}
    assert result['workflow_id'] == 'debug-flow' and result['step_id'] == 'selected'
    assert result['duration_ms'] >= 0 and result['error_detail'] is None
    assert isinstance(result['logs'], list)
    assert not (tmp_path / 'data/workflow_runs').exists()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')} == before


# LLM: 先真实暂停再调试并恢复同一run，不能新开运行代替恢复验证。
# 函数用途: 锁定调试不会引入脚本修订漂移。
def test_debug_does_not_drift_a_paused_real_run(tmp_path):
    script(tmp_path, 'pause', 'print(\'{"value":1,"_pause_for_resume":true}\')')
    script(tmp_path, 'tail', 'print(\'{"value":2}\')')
    WorkflowStore(tmp_path).save_workflow(definition(
        {'step_id': 'pause', 'mode': 'deterministic_script', 'script_id': 'pause'},
        {'step_id': 'tail', 'mode': 'deterministic_script', 'script_id': 'tail'}))
    engine = WorkflowEngine(tmp_path)
    paused = engine.run_workflow('debug-flow')
    assert paused.status == 'paused_for_resume'
    before = ScriptStore(tmp_path).get_script('tail')
    result = debug(engine, 'debug-flow', 'tail')
    assert result['status'] == 'succeeded'
    after = ScriptStore(tmp_path).get_script('tail')
    # source_check.checked_at是每次真实检查的内存投影；pin及持久摘要必须精确不变。
    assert {k: v for k, v in after.items() if k != 'source_check'} == {k: v for k, v in before.items() if k != 'source_check'}
    resumed = engine.resume_workflow_run(paused.workflow_run_id,
        patched_step_output={'value': 1}, resolution_origin='user')
    assert resumed.status == 'succeeded'
    assert all(s.error_code != 'SCRIPT_REVISION_DRIFT' for s in resumed.steps)


# LLM: 剩余映射读取注入上游对象，失败和非法预算必须保留原语义。
# 函数用途: 验证无需上游执行的上下文映射及调试错误结构。
def test_debug_upstream_context_and_failure_and_timeout(tmp_path):
    script(tmp_path, 'number', 'import json,sys; print(json.dumps({"value":json.load(sys.stdin)["n"]}))')
    WorkflowStore(tmp_path).save_workflow(definition(
        {'step_id': 'before', 'mode': 'deterministic_script', 'script_id': 'number'},
        {'step_id': 'pick', 'mode': 'deterministic_script', 'script_id': 'number',
         'inputs_map': {'n': {'step_id': 'before', 'key': 'value'}}}))
    engine = WorkflowEngine(tmp_path)
    assert debug(engine, 'debug-flow', 'pick', context_overrides={'before': {'value': 5}})['output_data'] == {'value': 5}
    failed = debug(engine, 'debug-flow', 'pick')
    assert failed['ok'] is True and failed['status'] == 'failed' and failed['error_detail']
    for timeout in (0, -1, float('nan'), True):
        with pytest.raises(ValueError):
            debug(engine, 'debug-flow', 'pick', timeout_seconds=timeout)


# LLM: 实际子进程睡眠超过预算，不以假runner返回timeout代替超时证明。
# 函数用途: 验证脚本超时有界且资产文件不变。
def test_debug_real_script_timeout_is_bounded_and_pin_neutral(tmp_path):
    script(tmp_path, 'slow', 'import time; time.sleep(8); print(\'{"value":1}\')')
    WorkflowStore(tmp_path).save_workflow(definition(
        {'step_id': 'slow', 'mode': 'deterministic_script', 'script_id': 'slow'}))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')}
    result = debug(WorkflowEngine(tmp_path), 'debug-flow', 'slow', timeout_seconds=0.1)
    assert result['status'] == 'failed' and result['error_detail']['code'] == 'SCRIPT_TIMEOUT'
    assert result['duration_ms'] < 7000
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')} == before


# LLM: 真实失败脚本需保留脱敏诊断；不能只验证logs字段存在而允许失败分支丢失内容。
# 函数用途: 锁定单步调试失败后的stderr预览，且不触发修复回调或正式账本。
def test_debug_failed_script_retains_diagnostic_logs(tmp_path):
    script(tmp_path, 'fail', 'import sys; print("node-diagnostic",file=sys.stderr); sys.exit(2)')
    WorkflowStore(tmp_path).save_workflow(definition(
        {'step_id': 'fail', 'mode': 'deterministic_script', 'script_id': 'fail'}))
    result = debug(WorkflowEngine(tmp_path), 'debug-flow', 'fail')
    assert result['status'] == 'failed'
    assert any('node-diagnostic' in line for line in result['logs'])


# LLM: 导入真实标准库认证模块；缺模块不能由测试fake补齐。
# 函数用途: 为标准向量和持久化认证回归取得产品服务。
def auth_module():
    name = 'agent_py_agent.agent.user_space.totp_sso_auth'
    assert importlib.util.find_spec(name) is not None, '缺少通用TOTP/SSO认证底座'
    return __import__(name, fromlist=['TotpSSOAuthService'])


# LLM: 通过原用户生命周期创建临时普通账号，不写生产密码或另造身份库。
# 函数用途: 准备TOTP与SSO需要的已存在、已启用测试用户。
def local_user(root, uid='alice'):
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    LocalUserLifecycle(LocalUserStore(root)).create(uid, password='TestPassword123!', idempotency_key='create-local-user-' + uid)


# LLM: 固定RFC6238 SHA1向量取六位后缀，不用产品函数生成预期值。
# 函数用途: 核验标准算法、长计数及六位补零。
@pytest.mark.parametrize('stamp,expected', [(59, '287082'), (1111111109, '081804'),
    (1111111111, '050471'), (1234567890, '005924'), (2000000000, '279037'), (20000000000, '353130')])
def test_rfc6238_sha1_six_digit_vectors(stamp, expected):
    import base64
    secret = base64.b32encode(b'12345678901234567890').decode()
    assert auth_module().totp_code(secret, stamp // 30) == expected


# LLM: 真实私有存储跨调用读取，恢复码不应以明文落盘。
# 函数用途: 验证TOTP激活、同计数拒绝、单次恢复码和停用。
def test_totp_setup_replay_recovery_disable_and_private_storage(tmp_path):
    local_user(tmp_path)
    clock = [1700000010]
    module = auth_module()
    service = module.TotpSSOAuthService(tmp_path, clock=lambda: clock[0])
    setup = service.setup_totp('alice')
    import base64
    assert len(base64.b32decode(setup['secret'])) == 20
    assert setup['otpauth_uri'].startswith('otpauth://totp/')
    assert len(set(setup['recovery_codes'])) == 8
    path = tmp_path / 'local_users_auth/alice.json'
    raw = path.read_text()
    assert all(code not in raw for code in setup['recovery_codes'])
    assert path.stat().st_mode & 0o777 == 0o600
    code = module.totp_code(setup['secret'], clock[0] // 30)
    assert service.verify_totp('alice', code)['enabled'] is True
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    with pytest.raises(HostAPIError) as error:
        service.verify_totp('alice', code)
    assert error.value.code == 'TOTP_CODE_REPLAYED'
    with pytest.raises(HostAPIError):
        service.setup_totp('alice')
    recovery = setup['recovery_codes'][0]
    assert service.verify_totp('alice', recovery)['ok'] is True
    with pytest.raises(HostAPIError):
        service.verify_totp('alice', recovery)
    clock[0] += 30
    assert service.disable_totp('alice', module.totp_code(setup['secret'], clock[0] // 30))['enabled'] is False


# LLM: 从原密码签发与session验证入口观察，不把独立verify成功当作MFA接通。
# 函数用途: 证明启用MFA会阻止旧密码旁路并撤销旧session。
def test_mfa_cannot_be_bypassed_by_old_password_login_and_revokes_old_session(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.user_auth import authenticate_user, verify_user_session
    local_user(tmp_path)
    old = authenticate_user(tmp_path, account_source='local', user_id='alice', password='TestPassword123!')
    module = auth_module()
    service = module.TotpSSOAuthService(tmp_path)
    setup = service.setup_totp('alice')
    counter = int(module.time.time()) // 30
    service.verify_totp('alice', module.totp_code(setup['secret'], counter))
    with pytest.raises(HostAPIError) as error:
        authenticate_user(tmp_path, account_source='local', user_id='alice', password='TestPassword123!')
    assert error.value.code == 'TOTP_REQUIRED'
    with pytest.raises(HostAPIError):
        verify_user_session(tmp_path, old.session_token)
    session = authenticate_user(tmp_path, account_source='local', user_id='alice', password='TestPassword123!',
        totp_code=setup['recovery_codes'][0])
    assert verify_user_session(tmp_path, session.session_token).owner_id == session.owner_id


# LLM: 受控IdP替身仅隔离外部协议，state/MAC/映射/session仍执行真实产品链。
# 函数用途: 验证SSO状态绑定、过期、配置修订和单次回调。
def test_sso_state_expiry_nonce_config_revision_replay_and_verified_mapping(tmp_path):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    local_user(tmp_path)
    module = auth_module()
    clock = [1700000010]
    # LLM: 这是测试协议边界而非真实IdP验签，不接受本用例之外的断言。
    # 函数用途: 提供受控的可信身份并回显本次预期nonce。
    def verifier(config, response, nonce):
        if response != 'signed-provider-response':
            raise HostAPIError('SSO_ASSERTION_INVALID', 401)
        return module.VerifiedSSOIdentity(config['issuer_url'], 'subject-1', nonce, ('employees',))
    service = module.TotpSSOAuthService(tmp_path, clock=lambda: clock[0], assertion_verifier=verifier)
    public = service.update_sso_config({'protocol': 'oidc', 'issuer_url': 'https://idp.example',
        'client_id': 'client', 'client_secret': 'synthetic-test-secret',
        'redirect_uri': 'https://portal.example/callback', 'role_mapping': {'employees': 'USER'},
        'subject_mapping': {'subject-1': 'alice'}, 'enabled': True})
    assert public['client_secret'] != 'synthetic-test-secret'
    assert 'synthetic-test-secret' not in json.dumps(service.get_sso_config())
    ticket = service.initiate_sso_login(browser_binding='browser-binding-one-long')
    assert ticket['expires_at'] - clock[0] == 300
    for change in ({'nonce': 'wrong'}, {'browser_binding': 'browser-two'}, {'response': 'unsigned'}):
        args = {'state': ticket['state'], 'nonce': ticket['nonce'], 'response': 'signed-provider-response',
                'browser_binding': 'browser-binding-one-long', **change}
        with pytest.raises(HostAPIError):
            service.complete_sso_callback(**args)
    result = service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'],
        response='signed-provider-response', browser_binding='browser-binding-one-long')
    assert result['principal_id'] == 'local/alice' and result['session_token']
    with pytest.raises(HostAPIError) as error:
        service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'],
            response='signed-provider-response', browser_binding='browser-binding-one-long')
    assert error.value.code == 'SSO_STATE_REPLAYED'
    expired = service.initiate_sso_login(browser_binding='browser-binding-one-long')
    clock[0] += 301
    with pytest.raises(HostAPIError):
        service.complete_sso_callback(state=expired['state'], nonce=expired['nonce'],
            response='signed-provider-response', browser_binding='browser-binding-one-long')
    stale = service.initiate_sso_login(browser_binding='browser-binding-one-long')
    service.update_sso_config({'enabled': False})
    with pytest.raises(HostAPIError):
        service.complete_sso_callback(state=stale['state'], nonce=stale['nonce'],
            response='signed-provider-response', browser_binding='browser-binding-one-long')


# LLM: 缺真实协议端口不能信任客户端同形claims，角色映射不能升级main。
# 函数用途: 验证未接IdP与非法管理员映射失败关闭。
def test_sso_unverified_claims_missing_adapter_and_admin_mapping_fail_closed(tmp_path):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    module = auth_module()
    service = module.TotpSSOAuthService(tmp_path)
    for protocol in ('oidc', 'saml'):
        service.update_sso_config({'protocol': protocol, 'issuer_url': 'https://idp.example',
            'client_id': 'client', 'redirect_uri': 'https://portal.example/callback',
            'subject_mapping': {}, 'role_mapping': {}, 'enabled': True})
        ticket = service.initiate_sso_login(browser_binding='browser-binding-example')
        with pytest.raises(HostAPIError) as error:
            service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'],
                response={'sub': 'alice', 'verified': True}, browser_binding='browser-binding-example')
        assert error.value.code == 'HOST_NOT_READY'
    with pytest.raises(HostAPIError):
        service.update_sso_config({'subject_mapping': {'external': 'main'}})


# LLM: verifier是明确测试替身，不是已验证IdP；配置走原服务的校验和私有写入口。
# 函数用途: 为握手反例建立最小通用SSO配置与受控身份映射。
def configured_sso(root, verifier):
    module = auth_module()
    service = module.TotpSSOAuthService(root, assertion_verifier=verifier)
    service.update_sso_config({'protocol': 'oidc', 'issuer_url': 'https://idp.example',
        'client_id': 'client', 'redirect_uri': 'https://portal.example/callback',
        'subject_mapping': {'subject-1': 'alice'}, 'role_mapping': {'employees': 'USER'}, 'enabled': True})
    return service


# LLM: 不伪造Verified结果或跳过state，始终执行产品callback全部检查。
# 函数用途: 将同一测试浏览器的完整握手材料提交给认证服务。
def complete_sso(service, ticket, **extra):
    return service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'], response='signed',
        browser_binding='browser-binding-example', **extra)


# LLM: 验签器输入的嵌套修改不能反向影响宿主授权快照。
# 函数用途: 复现并防止可信适配器规范化时误改身份映射。
def test_sso_verifier_cannot_mutate_authorization_snapshot(tmp_path):
    local_user(tmp_path)
    local_user(tmp_path, 'bob')
    module = auth_module()
    # LLM: 故意原地改嵌套映射，以检测浅拷贝共享对象。
    # 函数用途: 提供会修改输入的协议替身，不写持久配置。
    def verifier(config, response, nonce):
        config['subject_mapping']['subject-1'] = 'bob'
        config['role_mapping']['employees'] = 'ADMIN'
        return module.VerifiedSSOIdentity('https://idp.example', 'subject-1', nonce, ('employees',))
    service = configured_sso(tmp_path, verifier)
    ticket = service.initiate_sso_login(browser_binding='browser-binding-example')
    result = complete_sso(service, ticket)
    assert result['principal_id'] == 'local/alice'
    assert service.get_sso_config()['subject_mapping'] == {'subject-1': 'alice'}


# LLM: 使用真实签发token与原入站解析器，不伪造OwnerIdentity认证结果。
# 函数用途: 验证SSO返回DTO能够进入既有Host身份主链。
def test_sso_session_works_in_original_host_ingress(tmp_path):
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    local_user(tmp_path)
    module = auth_module()
    service = configured_sso(tmp_path, lambda config, response, nonce:
        module.VerifiedSSOIdentity(config['issuer_url'], 'subject-1', nonce, ('employees',)))
    result = complete_sso(service, service.initiate_sso_login(browser_binding='browser-binding-example'))
    assert IngressIssuer()._session_owner(tmp_path, result['session_token']) == OwnerIdentity.local_user('alice')


# LLM: 屏障位于state提交和签发之间；即使回调报错也检查期间已签token是否仍有效。
# 函数用途: 防止旧身份判断在新SSO配置下获得有效session。
def test_sso_config_race_cannot_mint_current_valid_token(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space import user_auth
    local_user(tmp_path)
    module = auth_module()
    service = configured_sso(tmp_path, lambda config, response, nonce:
        module.VerifiedSSOIdentity(config['issuer_url'], 'subject-1', nonce, ('employees',)))
    ticket = service.initiate_sso_login(browser_binding='browser-binding-example')
    original, minted = user_auth._issue_session, []
    # LLM: 只在真实签发前改变配置，再调用原函数，捕获结果用于独立原验签入口检查。
    # 函数用途: 确定性制造配置竞态，不使用假的session验证器。
    def change_then_issue(*args, **kwargs):
        service.update_sso_config({'client_id': 'changed-after-state-commit'})
        result = original(*args, **kwargs)
        minted.append(result.session_token)
        return result
    monkeypatch.setattr(user_auth, '_issue_session', change_then_issue)
    with pytest.raises(HostAPIError):
        complete_sso(service, ticket)
    for token in minted:
        with pytest.raises(HostAPIError):
            user_auth.verify_user_session(tmp_path, token)


# LLM: 明确本版one-shot策略，MFA缺码不签session且旧IdP state不可恢复。
# 函数用途: 验证重新握手后才可用未消费第二因素完成登录。
def test_sso_mfa_failure_requires_new_handshake_and_never_bypasses_factor(tmp_path):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    local_user(tmp_path)
    module = auth_module()
    service = configured_sso(tmp_path, lambda config, response, nonce:
        module.VerifiedSSOIdentity(config['issuer_url'], 'subject-1', nonce, ('employees',)))
    setup = service.setup_totp('alice')
    service.verify_totp('alice', module.totp_code(setup['secret'], int(module.time.time()) // 30))
    ticket = service.initiate_sso_login(browser_binding='browser-binding-example')
    with pytest.raises(HostAPIError) as error:
        complete_sso(service, ticket)
    assert error.value.code == 'TOTP_REQUIRED'
    with pytest.raises(HostAPIError) as error:
        complete_sso(service, ticket, totp_code=setup['recovery_codes'][0])
    assert error.value.code == 'SSO_STATE_REPLAYED'
    fresh = service.initiate_sso_login(browser_binding='browser-binding-example')
    assert complete_sso(service, fresh, totp_code=setup['recovery_codes'][0])['principal_id'] == 'local/alice'


# LLM: 合法UID不能撞配置/握手权威文件，不通过禁用合法账号名规避冲突。
# 函数用途: 验证MFA用户记录与SSO状态命名空间分离。
@pytest.mark.parametrize('uid', ['sso-config', 'sso-states'])
def test_valid_user_ids_cannot_collide_with_sso_authority_files(tmp_path, uid):
    local_user(tmp_path)
    local_user(tmp_path, uid)
    module = auth_module()
    service = configured_sso(tmp_path, lambda config, response, nonce:
        module.VerifiedSSOIdentity(config['issuer_url'], 'subject-1', nonce, ('employees',)))
    ticket = service.initiate_sso_login(browser_binding='browser-binding-example')
    service.setup_totp(uid)
    assert service.get_sso_config()['enabled'] is True
    assert complete_sso(service, ticket)['principal_id'] == 'local/alice'


# LLM: 两个独立解释器竞争同一真实磁盘账，不能用单进程锁模拟跨进程防重放。
# 函数用途: 验证动态码、恢复码和SSO state最多各成功一次。
@pytest.mark.parametrize('operation', ['totp', 'recovery', 'sso'])
def test_auth_single_use_across_independent_processes(tmp_path, operation):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    local_user(tmp_path)
    module = auth_module()
    service = configured_sso(tmp_path, None)
    setup = service.setup_totp('alice')
    service.verify_totp('alice', module.totp_code(setup['secret'], int(module.time.time()) // 30))
    request = {'root': str(tmp_path), 'operation': operation, 'code': setup['recovery_codes'][0]}
    if operation == 'totp':
        request['clock'] = int(module.time.time()) + 60
        request['code'] = module.totp_code(setup['secret'], request['clock'] // 30)
    if operation == 'sso':
        # 并发回调不与MFA消费竞争；各进程各用一个有效恢复码，仍只准一个state提交成功。
        request['ticket'] = service.initiate_sso_login(browser_binding='browser-binding-example')
        request['codes'] = setup['recovery_codes'][:2]
    program = '''import json,sys
from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService,VerifiedSSOIdentity
from agent_py_agent.agent.plugin_host.errors import HostAPIError
p=json.load(sys.stdin)
service=TotpSSOAuthService(p['root'],clock=(lambda:p['clock']) if 'clock' in p else None,
 assertion_verifier=lambda config,response,nonce:VerifiedSSOIdentity(config['issuer_url'],'subject-1',nonce,('employees',)))
try:
 if p['operation']=='sso':
  ticket=p['ticket']
  service.complete_sso_callback(state=ticket['state'],nonce=ticket['nonce'],response='signed',
   browser_binding='browser-binding-example',totp_code=p['code'])
 else: service.verify_totp('alice',p['code'])
 print('ok')
except HostAPIError as error: print(error.code)
'''
    # LLM: 子进程cwd继承当前测试树，stdout仅给结果标签，不回传种子、恢复码或session。
    # 函数用途: 启动一个有界真实消费进程并严格检查退出码。
    def invoke(index):
        data = {**request, 'code': request.get('codes', [request['code']] * 2)[index]}
        result = subprocess.run([sys.executable, '-c', program], input=json.dumps(data),
            text=True, capture_output=True, timeout=20, check=True)
        return result.stdout.strip()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert results.count('ok') == 1, results
    assert any(value in ('TOTP_CODE_REPLAYED', 'TOTP_INVALID', 'SSO_STATE_REPLAYED') for value in results), results


# LLM: 旧受限execute不能隐式升级成管理员空间代码执行。
# 函数用途: 锁定调试动作的独立批准能力。
def test_debug_requires_new_explicit_permission_not_legacy_execute():
    from agent_py_agent.agent.plugin_host.action_specs import required_capabilities
    assert 'workflows.debug' in required_capabilities('workflows.steps.debug', {'workflow_id': 'flow', 'step_id': 'step'})


HOST_CASES = (
    ('plugins.packages.install', 'write', {'package_base64': 'AA=='}),
    ('plugins.packages.lifecycle', 'execute', {'plugin_id': 'sample-peek', 'action': 'status'}),
    ('workflows.steps.debug', 'execute', {'workflow_id': 'debug-flow', 'step_id': 'one'}),
    ('users.auth.totp.setup', 'execute', {'user_id': 'alice'}),
    ('users.auth.totp.verify', 'execute', {'user_id': 'alice', 'code': '123456'}),
    ('users.auth.totp.disable', 'execute', {'user_id': 'alice', 'code': '123456'}),
    ('users.auth.sso.config.get', 'read', {}),
    ('users.auth.sso.config.update', 'execute', {'config': {}}),
    ('users.auth.sso.initiate', 'execute', {'browser_binding': 'browser-binding-example'}),
    ('users.auth.sso.callback', 'execute', {'state': 's', 'nonce': 'n', 'response': {}, 'browser_binding': 'browser-binding-example'}),
)


# LLM: 十项独立需求清单与唯一目录核对，额外admin字段必须拒绝。
# 函数用途: 验证所有底座动作已登记且参数闭合。
@pytest.mark.parametrize('action,mode,payload', HOST_CASES)
def test_substrate_host_closed_action_contracts(action, mode, payload):
    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    assert action in action_specs(), 'Host未登记任务要求的通用动作'
    body = {'schema': 'plugin_host_call.v2', 'action': action, 'payload': payload}
    if mode == 'write':
        body['idempotency_key'] = 'wave9-package-install-1'
    assert decode_call(json.dumps(body).encode()).action == action
    with pytest.raises(HostAPIError):
        decode_call(json.dumps({**body, 'payload': {**payload, 'admin': True}}).encode())


# LLM: 原令牌/授权/回环HTTP/安装表均执行；暂存tar不经64KiB内联旁路。
# 函数用途: 核验base64、暂存tar、状态及真实卸载链路。
def test_host_real_http_package_install_staged_tar_and_lifecycle(production_host):
    import base64

    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    assert 'plugins.packages.install' in action_specs()
    raw = _bundle()
    result = production_call(production_host, 'plugins.packages.install',
        {'package_base64': base64.b64encode(raw).decode()}, key='wave9-install-package-1')
    assert result['plugin_id'] == 'sample-peek' and result['status'] == 'disabled'
    assert len(result['sha256']) == 64 and result['manifest']['plugin_id'] == 'sample-peek'
    current = production_call(production_host, 'plugins.packages.lifecycle', {'plugin_id': 'sample-peek', 'action': 'status'})
    assert current['sha256'] == result['sha256']
    uploads = production_host[2].home_dir / 'uploads'
    uploads.mkdir()
    staged = uploads / 'next.tgz'
    staged.write_bytes(tar_bundle(_bundle(change=lambda row: row.update(plugin_id='second-plugin'))))
    staged_result = production_call(production_host, 'plugins.packages.install', {'package_path': str(staged)},
        key='wave9-install-package-2')
    assert staged_result['plugin_id'] == 'second-plugin'
    removed = production_call(production_host, 'plugins.packages.lifecycle', {'plugin_id': 'sample-peek', 'action': 'uninstall'},
        key='wave9-remove-package-1')
    assert removed['status'] == 'uninstalled'


# LLM: 同键重放来源已删仍不重写；新更新须原安装CAS，旧修订不能成功。
# 函数用途: 验证安装重放和已有包更新的真实HTTP行为。
def test_host_package_update_cas_and_idempotent_staging_retry(production_host):
    import base64

    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    uploads = production_host[2].home_dir / 'uploads'
    uploads.mkdir()
    staged = uploads / 'first.zip'
    staged.write_bytes(_bundle())
    payload = {'package_path': str(staged)}
    first = production_call(production_host, 'plugins.packages.install', payload, key='wave9-stable-install-1')
    staged.unlink()
    assert production_call(production_host, 'plugins.packages.install', payload, key='wave9-stable-install-1') == first
    updated = {'package_base64': base64.b64encode(_bundle(change=lambda row: row.update(version='0.2.0'))).decode(),
        'expected_revision': first['revision']}
    second = production_call(production_host, 'plugins.packages.install', updated, key='wave9-update-install-2')
    assert second['version'] == '0.2.0' and second['revision'] > first['revision']
    status, body = http_request(production_host[0], production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'plugins.packages.install', 'payload': updated, 'idempotency_key': 'wave9-update-stale-3'})
    assert status == 409 and body['error']['code'] == 'PLUGIN_MANAGEMENT_FAILED'


# LLM: 只替换管理命令副作用，明确本例不证明插件进程已真实启停。
# 函数用途: 核验三种生命周期动作确实沿原管理命令分派。
@pytest.mark.parametrize('action,commands', [('enable', ['/plugins enable sample-peek']), ('disable', ['/plugins disable sample-peek']),
    ('reload', ['/plugins disable sample-peek', '/plugins enable sample-peek'])])
def test_host_lifecycle_dispatches_original_management(production_host, monkeypatch, action, commands):
    import base64

    from agent_py_agent.agent.plugin_management import PluginManagement
    production_call(production_host, 'plugins.packages.install', {'package_base64': base64.b64encode(_bundle()).decode()},
        key='wave9-lifecycle-install-1')
    seen = []
    # LLM: 记录原命令协议与冻结请求标识，不启动插件或自动批准权限。
    # 函数用途: 为生命周期分派提供明确成功的受控领域边界。
    def command(self, arguments, *, revision, request_id):
        seen.append(arguments)
        assert revision and request_id.startswith('host-')
        return {'state': 'succeeded'}
    monkeypatch.setattr(PluginManagement, 'command', command)
    production_call(production_host, 'plugins.packages.lifecycle', {'plugin_id': 'sample-peek', 'action': action},
        key='wave9-lifecycle-action-2')
    assert seen == commands


# LLM: 原HTTP真实执行用例显式许可副作用，不绕过授权或弱化输出断言，默认预览另有标记文件回归。
# 函数用途: 核验MFA和显式脚本调试接入正确Owner及无账本执行器。
def test_host_real_http_totp_and_debug_use_original_owner(production_host):
    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    assert 'users.auth.totp.setup' in action_specs()
    production_call(production_host, 'users.local.create', {'user_id': 'alice', 'password': 'TestPassword123!'},
        key='wave9-create-alice-1', status=201)
    setup = production_call(production_host, 'users.auth.totp.setup', {'user_id': 'alice'})
    code = auth_module().totp_code(setup['secret'], int(auth_module().time.time()) // 30)
    assert production_call(production_host, 'users.auth.totp.verify', {'user_id': 'alice', 'code': code})['enabled']
    home = production_host[2].home_dir
    script(home, 'double', 'print(\'{"value":8}\')')
    WorkflowStore(home).save_workflow(definition({'step_id': 'one', 'mode': 'deterministic_script', 'script_id': 'double'}))
    result = production_call(production_host, 'workflows.steps.debug',
        {'workflow_id': 'debug-flow', 'step_id': 'one', 'allow_side_effects': True})
    assert result['status'] == 'succeeded' and result['output_data'] == {'value': 8}
    assert not (home / 'data/workflow_runs').exists()


# LLM: 原task登记和artifact注册是真实前置资料，安装仍经过原HTTP权限/哈希/FD读口。
# 函数用途: 验证超过JSON容量的包能通过授权opaque制品引用安装。
@pytest.mark.parametrize('production_host', [{'task_ids': ['artifact-task']}], indirect=True)
def test_host_registered_artifact_larger_than_json_cap_installs(production_host):
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call, resolve_read_context
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    thread = production_call(production_host, 'conversations.threads.create', {'title': 'package staging'},
        key='wave9-artifact-thread-1', status=201)
    envelope = {'schema': 'plugin_host_call.v2', 'action': 'plugins.packages.install',
        'payload': {'artifact_ref': 'plugin-archive'}, 'idempotency_key': 'wave9-artifact-install-1'}
    composition = production_host[0].plugin_host.composition
    authorized = authorize_call(composition.issuer.ingress(production_host[1]), decode_call(json.dumps(envelope).encode()))
    context = resolve_read_context(authorized)
    store = composition.ports.conversation.store_resolver(context)
    root = context.owner_home.tasks_dir / '2026-10-28/package-fixture'
    root.mkdir(parents=True)
    store.tasks.bind({'task_id': 'artifact-task', 'thread_id': thread['thread_id'], 'task_path': str(root), 'status': 'done'})
    buffer = io.BytesIO(_bundle())
    with zipfile.ZipFile(buffer, 'a') as archive:
        archive.comment = b'x' * 60000
    raw = buffer.getvalue()
    assert len(__import__('base64').b64encode(raw)) > 65536
    path = root / 'plugin.zip'
    path.write_bytes(raw)
    register_artifact(ArtifactRegistration(root, path, artifact_id='plugin-archive', task_id='artifact-task', status='ready'))
    status, result = http_request(production_host[0], production_host[1], envelope)
    assert status == 200, result
    assert result['data']['plugin_id'] == 'sample-peek'
    assert result['data']['sha256'] == __import__('hashlib').sha256(raw).hexdigest()


# LLM: 暂存读入必须在安装副作用前拒绝逃逸与软链接，不依赖路径resolve清洗。
# 函数用途: 验证Host来源路径安全边界。
@pytest.mark.parametrize('source', ['../secret.zip', '/etc/passwd', 'uploads/../secret.zip', 'uploads/link.zip'])
def test_host_package_path_escape_and_link_rejected(production_host, source):
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    uploads = production_host[2].home_dir / 'uploads'
    uploads.mkdir()
    (uploads / 'actual.zip').write_bytes(_bundle())
    (uploads / 'link.zip').symlink_to(uploads / 'actual.zip')
    status, body = http_request(production_host[0], production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'plugins.packages.install', 'payload': {'package_path': source}, 'idempotency_key': 'wave9-bad-package-1'})
    assert status == 403 and body['error']['code'] == 'PACKAGE_SOURCE_DENIED'
