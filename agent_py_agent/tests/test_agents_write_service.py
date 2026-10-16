"""C真实临时来源测试；不是飞书真人、测试机或完整UI验收。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.agents_control_ledger import read_record, save_record
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.capability.agents_write_service import (
    AgentsControlRequest,
    AgentsWriteService,
    _direct_request,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy


# LLM: 仅临时home，无网络/密钥/真实owner；真实A写入口不替换。
# 函数用途: 建立来源与可调宿主时钟。
@pytest.fixture
def source(tmp_path):
    root = tmp_path.resolve() / 'data'
    home = root / 'owners/local/main'
    home.mkdir(parents=True)
    (home / 'AGENTS.md').write_text('原始用户约定\n')
    policy = PathAccessPolicy.from_values(mode='full_access', agent_home_root=root)
    return AgentsSourceRepository(owner='local/main', owner_home=home, path_policy=policy)


# LLM: 测试中模拟可信入口发行，不把native类型直接当作用户证据。
# 函数用途: 构造真实service要求的请求。
def request(raw, identity='request', **changes):
    facts = dict(owner='local/main', actor='local/main', thread_id='thread', request_id=identity, raw=raw)
    facts.update(changes)
    return _direct_request(**facts)


# LLM: 真实文本替换及配置变化应只发生在确认之后。
# 函数用途: 证明查看→预览→确认→真实文件→结构回执主链。
def test_view_preview_confirm_file_and_durable_original(source):
    service = AgentsWriteService(source, clock=lambda: 1000.0)
    assert service.execute(request('show'))['content'] == '原始用户约定\n'
    preview = service.execute(request('write "新的用户约定\\n"', 'preview'))
    assert preview['state'] == 'preview'
    assert source.default_path.read_text() == '原始用户约定\n'
    confirmed = service.execute(request('confirm ' + preview['nonce'], 'confirmation'))
    assert confirmed['ok'] and confirmed['state'] == 'completed'
    assert source.default_path.read_text() == '新的用户约定\n'
    record = read_record(source.owner_home, preview['nonce'])
    assert record['authorization']['request_id'] == 'confirmation'
    assert record['confirmation_request'] == 'confirm ' + preview['nonce']
    assert record['preview_request'] == 'write "新的用户约定\\n"'
    assert service.execute(request('receipt ' + preview['nonce']))['state'] == 'completed'


# LLM: 设置和恢复走同一授权与A的配置CAS，不扩大路径许可。
# 函数用途: 核对真实本地来源配置、内容写回及默认恢复。
def test_local_source_reset_and_refresh(source):
    service = AgentsWriteService(source)
    local = source.owner_home / 'rules.txt'
    local.write_text('本地约定')
    for index, raw in enumerate(('source local ' + str(local), 'reset')):
        preview = service.execute(request(raw, 'p' + str(index)))
        result = service.execute(request('confirm ' + preview['nonce'], 'c' + str(index)))
        assert result['ok'] and result['state'] == 'completed'
        assert source.configuration().revision == index + 1
    assert json.loads(source.config_path.read_text())['kind'] == 'owner_default'
    assert service.execute(request('refresh'))['content'] == '原始用户约定\n'


# LLM: JSON旗标/native无seal/学习投影都不能发行原授权。
# 函数用途: 验证非直接请求被拒且原文件不变。
@pytest.mark.parametrize('fake', [dict(role='user', confirmed=True, source_quote='请写'),
                                    AgentsControlRequest('local/main', 'local/main', 't', 'r', 'write "x"')])
def test_no_model_authority(source, fake):
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_NOT_USER_REQUEST'):
        AgentsWriteService(source).execute(fake)
    assert source.default_path.read_text() == '原始用户约定\n'


# LLM: owner/线程/动作/新值不能换绑，expiry不能由重送续期。
# 函数用途: 核对预览的绑定、过期和明确跨owner闸门。
def test_expiry_identity_action_and_value_binding(source):
    clock = [1000.0]
    service = AgentsWriteService(source, clock=lambda: clock[0])
    preview = service.execute(request('write "新正文"', 'p'))
    for raw in ('reset', 'write "换值"'):
        with pytest.raises(AgentsContractError, match='AGENTS_WRITE_BINDING_MISMATCH'):
            service.execute(request(raw, 'p'))
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_BINDING_MISMATCH'):
        service.execute(request('confirm ' + preview['nonce'], thread_id='another'))
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_CROSS_OWNER_FORBIDDEN'):
        service.execute(request('confirm ' + preview['nonce'], actor='providers/feishu/users/other'))
    clock[0] = preview['expires_at']
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_EXPIRED'):
        service.execute(request('confirm ' + preview['nonce']))
    assert source.default_path.read_text() == '原始用户约定\n'


# LLM: 已提交授权重放不得再次写，旧正文SHA不能覆盖外部新版本。
# 函数用途: 核对持久消费与真实文件前像保护。
def test_replay_and_stale_content(source):
    service = AgentsWriteService(source)
    preview = service.execute(request('write "第一次"', 'p1'))
    confirm = request('confirm ' + preview['nonce'], 'c1')
    assert service.execute(confirm)['ok']
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_REPLAYED'):
        AgentsWriteService(source).execute(confirm)
    old = service.execute(request('write "旧预览"', 'p2'))
    source.default_path.write_text('用户后来直接改的版本')
    result = service.execute(request('confirm ' + old['nonce'], 'c2'))
    assert not result['ok'] and result['error_code'] == 'AGENTS_SOURCE_CONTENT_MISMATCH'
    assert source.default_path.read_text() == '用户后来直接改的版本'


# LLM: 内容相同也不能用旧配置授权覆盖新配置代次。
# 函数用途: 验证A的完整CAS仍在C确认主链生效。
def test_stale_configuration_cannot_overwrite(source):
    service = AgentsWriteService(source)
    first, second = source.owner_home / 'one', source.owner_home / 'two'
    first.write_text('一样')
    second.write_text('一样')
    old = service.execute(request('source local ' + str(first), 'old'))
    new = service.execute(request('source local ' + str(second), 'new'))
    assert service.execute(request('confirm ' + new['nonce'], 'new-c'))['ok']
    before = source.config_path.read_bytes()
    result = service.execute(request('confirm ' + old['nonce'], 'old-c'))
    assert not result['ok'] and result['error_code'] == 'AGENTS_SOURCE_CONFIG_CONFLICT'
    assert source.config_path.read_bytes() == before


# LLM: 写成功但回执落盘失败保留未知及已消费，不能因重试重做副作用。
# 函数用途: 注入最终回执故障并验证未知状态、防重放和实际正文。
def test_receipt_failure_never_repeats_write(source, monkeypatch):
    import agent_py_agent.agent.capability.agents_write_service as module
    service = AgentsWriteService(source)
    preview = service.execute(request('write "已写入"', 'p'))
    original = module.save_record
    def fail_completed(home, record):
        if record['state'] == 'completed':
            raise OSError('receipt failure')
        original(home, record)
    monkeypatch.setattr(module, 'save_record', fail_completed)
    result = service.execute(request('confirm ' + preview['nonce'], 'c'))
    assert not result['ok'] and result['state'] == 'consumed_unknown'
    assert source.default_path.read_text() == '已写入'
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_REPLAYED'):
        service.execute(request('confirm ' + preview['nonce'], 'retry'))


# LLM: A还会从实际待提交新值重算intent，不相信上下文声明的digest。
# 函数用途: 检查独立原预览被换值时C在写前即拒绝。
def test_tampered_intent_refused(source):
    service = AgentsWriteService(source)
    preview = service.execute(request('write "合法正文"', 'p'))
    record = read_record(source.owner_home, preview['nonce'])
    record['value'] = '被替换的新值'
    save_record(source.owner_home, record)
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_BINDING_MISMATCH'):
        service.execute(request('confirm ' + preview['nonce'], 'c'))
    assert source.default_path.read_text() == '原始用户约定\n'


# LLM: 两个确认消费者必须共用耐久nonce，不能各自从pending写一次。
# 函数用途: 覆盖真实A提交和C并发消费边界。
def test_concurrent_confirmation_consumes_once(source):
    from concurrent.futures import ThreadPoolExecutor
    service = AgentsWriteService(source)
    preview = service.execute(request('write "只写一次"', 'p'))
    def submit(index):
        try:
            return service.execute(request('confirm ' + preview['nonce'], 'c' + str(index)))['state']
        except AgentsContractError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(submit, (1, 2)))
    assert sorted(states) == ['AGENTS_WRITE_REPLAYED', 'completed']
    assert source.default_path.read_text() == '只写一次'


# LLM: URL配置可更换但正文只读；传输内容使用夹具，不访问外网。
# 函数用途: 核对URL预览/确认真实配置及只读保护。
def test_url_configuration_readonly_body(source, monkeypatch):
    monkeypatch.setattr(source, '_network_enabled', lambda: True)
    monkeypatch.setattr(source, '_url_allowed', lambda url: True)
    monkeypatch.setattr(source, '_url_validator', lambda *a, **k: None)
    service = AgentsWriteService(source)
    preview = service.execute(request('source url https://example.com/AGENTS.md', 'url'))
    assert service.execute(request('confirm ' + preview['nonce'], 'url-c'))['ok']
    assert json.loads(source.config_path.read_text())['kind'] == 'url'
    monkeypatch.setattr(source, 'load', lambda: source._ready(source.configuration(), b'Remote rules'))
    with pytest.raises(AgentsContractError, match='AGENTS_WRITE_SOURCE_READ_ONLY'):
        service.execute(request('write "no remote write"', 'write-remote'))
