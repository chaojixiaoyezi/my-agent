"""使用真实 dispatcher、Store、合成 provider 及原生 HTTP 验证资产发现。"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w12_asset_discovery as module
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests import test_v13_w0_harness as harness

v13_native = harness.v13_native
v13_call = harness.v13_call


# LLM: 夹具只创建任务临时目录，不触碰真实 home。
# 函数用途: 创建可控制时钟与原生权限的真实宿主。
@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'host'
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    clock = [1793664000.0]
    return SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root)),
        config=AgentConfig(business_spaces_enabled=True),
        business_v13_clock=lambda: clock[0], clock=clock, business_v13_providers={}, business_native_admin=lambda: True)


# LLM: provider 是端口假体，不伪造 gateway 或 Store 响应。
# 类用途: 保存真实传入的目标、端口与续扫引用。
class DiscoveryProvider:
    # LLM: 合成 scope 仅使用保留测试域和网段。
    # 函数用途: 初始化提供者响应与调用账。
    def __init__(self):
        self.scope = {'domains': ['example.test'], 'cidrs': ['192.0.2.0/24', '2001:db8::/48']}
        self.assets = []
        self.calls = []
        self.complete = True
        self.failure = None

    # LLM: 权限查询不视作扫描，不产生副作用。
    # 函数用途: 返回绑定的资产权限范围。
    def allowed_scope(self):
        return deepcopy(self.scope)

    # LLM: 一次调用返回一批，不在假体内循环或访问网络。
    # 函数用途: 记录请求并返回可控资产结果。
    def scan(self, *, targets, ports, resume_ref):
        self.calls.append({'targets': targets, 'ports': ports, 'resume_ref': resume_ref})
        if self.failure is not None:
            raise self.failure
        return {'assets': deepcopy(self.assets), 'scan_ref': 'scan-synthetic', 'complete': self.complete}


# LLM: 绑定只在本测试宿主生效，不安装生产扫描器。
# 函数用途: 提供测试扫描端口。
@pytest.fixture
def provider(host):
    bound = DiscoveryProvider()
    host.business_v13_providers['asset_discovery'] = bound
    return bound


# LLM: 幂等键全部为合成值，满足真实运输校验。
# 函数用途: 生成测试写入键。
def idem():
    return 'w12-assets-' + uuid4().hex


# LLM: 身份来自宿主 bind_context，不能从请求 JSON 注入 owner。
# 函数用途: 通过真实统一 dispatcher 执行业务动作。
def invoke(host, action, params=None, *, key=None, revision=None, subject='alice', authorize=None):
    context = api.bind_business_context(host, subject_id=subject,
        authorize=authorize if authorize is not None else lambda action, facts: True)
    request = {'action': action, 'request_id': uuid4().hex, 'params': {} if params is None else params}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, context, api.BusinessServices(host))


# LLM: 成功断言校验实际信封，不手写预期响应代替产品调用。
# 函数用途: 取得 200 completed 数据。
def ok(host, action, params=None, *, key=None, revision=None, subject='alice'):
    status, body = invoke(host, action, params, key=key, revision=revision, subject=subject)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None, body
    return body['data']


# LLM: 失败必须保留精确状态码与失败信封。
# 函数用途: 核对真实错误输出。
def failed(result, status, code):
    actual, body = result
    assert actual == status, body
    assert body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: 提供者记录使用精确声明字段，不塞 owner 或未知元数据。
# 函数用途: 构造合成的真实格式暴露资产。
def asset(value='example.test', *, kind='domain', risk='medium', seen='2026-11-02T12:00:00+00:00'):
    return {'kind': kind, 'value': value, 'ports': [443], 'services': ['https'], 'certificate': None,
        'risk': risk, 'findings': [], 'last_seen_at': seen, 'scan_ref': 'scan-synthetic'}


# LLM: 不能用伪资产或额外集合表达零资产扫描。
# 函数用途: 验证初次 unknown 与成功空扫描后的 complete。
def test_assets_list_example_empty_params_unknown_until_first_scan(host, provider):
    assert ok(host, 'easm.assets.list') == {'items': [], 'next_cursor': None, 'coverage': 'unknown'}
    result = ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    assert result['assets_upserted'] == 0 and result['complete'] is True
    assert ok(host, 'easm.assets.list') == {'items': [], 'next_cursor': None, 'coverage': 'complete'}
    state = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)._read('asset_discovery_assets')
    assert state['records'] == {} and state['receipts'] == {}
    assert state['metadata'] == {'scan_observed': True, 'pending_scan_refs': []}
    receipt_dir = host.home_paths.owner_home_dir / 'data/business_v13/history/asset_discovery_assets/receipts'
    assert len(list(receipt_dir.glob('*.json'))) == 1
    assert {p.name for p in (host.home_paths.owner_home_dir / 'data/business_v13').glob('*.json')} == {
        'asset_discovery_assets.json', 'audit_log.json', '.transaction.json'}
    assert len(provider.calls) == 1


# LLM: 远端仍在继续的来源不能仅因当前页读尽就被误报为完整。
# 函数用途: 验证空批次、续批次及完成回执对列表覆盖的影响。
def test_assets_list_remains_partial_until_scan_complete(host, provider):
    provider.complete = False
    first = ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    page = ok(host, 'easm.assets.list')
    assert page == {'items': [], 'next_cursor': None, 'coverage': 'partial'}
    provider.assets = [asset()]
    ok(host, 'easm.scan.run', {'targets': ['example.test'], 'resume_ref': first['scan_ref']}, key=idem())
    page = ok(host, 'easm.assets.list')
    assert len(page['items']) == 1 and page['next_cursor'] is None and page['coverage'] == 'partial'
    provider.complete = True
    ok(host, 'easm.scan.run', {'targets': ['example.test'], 'resume_ref': first['scan_ref']}, key=idem())
    assert ok(host, 'easm.assets.list')['coverage'] == 'complete'


# 空批次也必须在原集合、原journal内保留覆盖事实，不能重扫所有永久回执。
def test_scan_coverage_tracks_independent_incomplete_scans(host, provider, monkeypatch):
    scan = provider.scan
    current = ['scan-a']

    def batch(**kwargs):
        result = scan(**kwargs)
        return {**result, 'scan_ref': current[0]}

    monkeypatch.setattr(provider, 'scan', batch)
    provider.complete = False
    ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    current[0], provider.complete = 'scan-b', True
    ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    assert ok(host, 'easm.assets.list')['coverage'] == 'partial'
    current[0] = 'scan-a'
    ok(host, 'easm.scan.run', {'targets': ['example.test'], 'resume_ref': 'scan-a'}, key=idem())
    assert ok(host, 'easm.assets.list')['coverage'] == 'complete'
    current[0], provider.complete = 'scan-b', False
    ok(host, 'easm.scan.run', {'targets': ['example.test'], 'resume_ref': 'scan-b'}, key=idem())
    assert ok(host, 'easm.assets.list')['coverage'] == 'complete'
    state = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)._read('asset_discovery_assets')
    assert state['metadata'] == {'scan_observed': True, 'pending_scan_refs': []}
    assert state['records'] == state['receipts'] == {}
    assert len(provider.calls) == 4


@pytest.mark.parametrize('complete', [False, True])
def test_scan_coverage_survives_committed_journal_recovery(host, provider, monkeypatch, complete):
    from agent_py_agent.agent.business_spaces import v13_store

    provider.complete = complete
    context = api.bind_business_context(host, subject_id='alice', authorize=lambda action, facts: True)
    call = V13Call('easm.scan.run', {'targets': ['example.test']}, 'scan-recovery',
        host.home_paths.owner_id, 'alice', context, api.BusinessServices(host), host.home_paths.owner_home_dir,
        idem(), None, host.business_v13_clock)
    original, interrupted = v13_store.write_bytes_atomic_beneath, []

    def cut(root, parts, raw):
        if parts[-1] == 'asset_discovery_assets.json' and not interrupted:
            interrupted.append(True)
            raise OSError('scan metadata materialization interrupted')
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', cut)
    with pytest.raises(OSError):
        module.scan_run(call)
    store = V13Store(call.owner_home, owner_id=call.owner_id)
    expected = {'scan_observed': True, 'pending_scan_refs': [] if complete else ['scan-synthetic']}
    assert store._read('asset_discovery_assets').get('metadata') == expected
    coverage = 'complete' if complete else 'partial'
    assert ok(host, 'easm.assets.list')['coverage'] == coverage
    assert module.scan_run(call)['complete'] is complete
    assert len(provider.calls) == 1 and store._pending() == {}
    assert store._read('asset_discovery_assets')['metadata'] == expected
    assert ok(host, 'easm.assets.list')['coverage'] == coverage
    paths = list((call.owner_home / 'data/business_v13/history/asset_discovery_assets/metadata').glob('*.json'))
    assert len(paths) == int(complete)
    if complete:
        rows = store._rows('asset_discovery_assets')
        fact = rows.decode(paths[0].read_bytes(), 'metadata')
        assert rows.parts('metadata', fact['key'])[-1] == paths[0].name
        assert rows.get('metadata', fact['key']) == {'scan_ref': 'scan-synthetic', 'complete': True}


# LLM: 同笔回放不可重扫，后续批次更新须保留首次发现时间与 ID。
# 函数用途: 验证范围检查、规范化、有界扫描及 upsert。
def test_scan_scope_check_rejects_out_of_scope_and_upserts_provider_assets(host, provider):
    failed(invoke(host, 'easm.scan.run', {'targets': ['notexample.test']}, key=idem()),
        403, 'BUSINESS_ACCESS_DENIED')
    assert provider.calls == []
    provider.assets = [asset('APP.EXAMPLE.TEST.', kind='subdomain')]
    provider.complete = False
    key = idem()
    params = {'targets': ['EXAMPLE.TEST.', 'example.test'], 'ports': [443]}
    first = ok(host, 'easm.scan.run', params, key=key)
    assert first == {'scan_ref': 'scan-synthetic', 'complete': False, 'assets_upserted': 1,
        'scanned_targets': ['example.test']}
    assert provider.calls == [{'targets': ['example.test'], 'ports': [443], 'resume_ref': None}]
    assert ok(host, 'easm.scan.run', params, key=key) == first
    before = ok(host, 'easm.assets.list')['items'][0]
    host.clock[0] += 60
    provider.assets[0]['risk'] = 'critical'
    provider.complete = True
    ok(host, 'easm.scan.run', {'targets': ['example.test'], 'resume_ref': first['scan_ref']}, key=idem())
    after = ok(host, 'easm.assets.list')['items'][0]
    assert after['value'] == 'app.example.test' and after['risk'] == 'critical'
    assert after['asset_id'] == before['asset_id'] and after['first_seen_at'] == before['first_seen_at']
    assert len(provider.calls) == 2 and provider.calls[-1]['resume_ref'] == 'scan-synthetic'
    failed(invoke(host, 'easm.scan.run', {'targets': ['192.0.2.1']}, key=key), 409, 'IDEMPOTENCY_CONFLICT')
    assert len(provider.calls) == 2


# LLM: 同族子网必须整个包含，前缀欺骗、URL、跨协议族均不能触发扫描。
# 函数用途: 为范围安全边界保留参数化反例。
@pytest.mark.parametrize('target', ['notexample.test', 'example.test.other', '192.0.0.0/16',
    '192.0.3.1', '2001:db9::/48', 'https://example.test/path', '*.example.test'])
def test_scan_rejects_scope_boundary_before_scan(host, provider, target):
    failed(invoke(host, 'easm.scan.run', {'targets': [target]}, key=idem()), 403, 'BUSINESS_ACCESS_DENIED')
    assert provider.calls == []


# LLM: 允许域名与网段的正例都要实际到达同一个 provider 端口。
# 函数用途: 验证 IPv4、IPv6、CIDR 与子域授权。
@pytest.mark.parametrize('target', ['app.example.test', '192.0.2.7', '192.0.2.128/25', '2001:db8:0:1::/64'])
def test_scan_accepts_contained_ip_cidr_and_domain(host, provider, target):
    result = ok(host, 'easm.scan.run', {'targets': [target]}, key=idem())
    assert result['scanned_targets'] == [target] and len(provider.calls) == 1


# LLM: 输出端与输入端遵守同一范围，坏输出不能部分污染集合。
# 函数用途: 验证越界资产、缺字段和过大批次的原子拒绝。
@pytest.mark.parametrize('variant,code,status', [('outside', 'BUSINESS_ACCESS_DENIED', 403),
    ('missing', 'HOST_NOT_READY', 503), ('large', 'LIMIT_EXCEEDED', 400)])
def test_scan_invalid_provider_output_never_pollutes_store(host, provider, variant, code, status):
    provider.assets = [asset()]
    if variant == 'outside':
        provider.assets.append(asset('external.test'))
    elif variant == 'missing':
        provider.assets[0].pop('risk')
    else:
        provider.assets = [asset(f'app{i}.example.test') for i in range(101)]
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), status, code)
    assert len(provider.calls) == 1
    assert ok(host, 'easm.assets.list') == {'items': [], 'next_cursor': None, 'coverage': 'unknown'}


# LLM: 自定义排序仍须使用原 Store HMAC，不可用无签名偏移量。
# 函数用途: 验证风险/真实时间排序与游标筛选、主体、修订绑定。
def test_assets_sort_filter_and_cursor_negative_cases(host, provider):
    provider.assets = [asset('low.example.test', risk='low'),
        asset('earlier.example.test', risk='high', seen='2026-11-02T10:00:00+00:00'),
        asset('later.example.test', risk='high', seen='2026-11-02T08:00:00-03:00'),
        asset('critical.example.test', risk='critical')]
    ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    all_rows = ok(host, 'easm.assets.list')['items']
    assert [row['value'] for row in all_rows] == ['critical.example.test', 'later.example.test',
        'earlier.example.test', 'low.example.test']
    assert [row['value'] for row in ok(host, 'easm.assets.list', {'query': 'LATER', 'kind': 'domain'})['items']] == [
        'later.example.test']
    assert ok(host, 'easm.assets.list', {'query': '中文条件'})['items'] == []
    first = ok(host, 'easm.assets.list', {'risk': 'high', 'limit': 1})
    cursor = first['next_cursor']
    assert cursor and first['coverage'] == 'partial'
    second = ok(host, 'easm.assets.list', {'risk': 'high', 'limit': 1, 'cursor': cursor})
    assert second['items'][0]['value'] == 'earlier.example.test' and second['next_cursor'] is None
    failed(invoke(host, 'easm.assets.list', {'risk': 'low', 'limit': 1, 'cursor': cursor}), 410, 'CURSOR_EXPIRED')
    failed(invoke(host, 'easm.assets.list', {'risk': 'high', 'limit': 1, 'cursor': cursor}, subject='bob'),
        410, 'CURSOR_EXPIRED')
    failed(invoke(host, 'easm.assets.list', {'risk': 'high', 'limit': 1, 'cursor': cursor + 'x'}),
        410, 'CURSOR_EXPIRED')
    ok(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem())
    failed(invoke(host, 'easm.assets.list', {'risk': 'high', 'limit': 1, 'cursor': cursor}), 410, 'CURSOR_EXPIRED')


# LLM: ready 的 context 可为空，只读宿主绑定，权限在 invoke 中实时执行。
# 函数用途: 验证缺 provider、空范围、非管理员和撤权全部不扫描。
def test_readiness_admin_live_access_and_unissued_context(host, provider):
    probe = SimpleNamespace(context=None, services=SimpleNamespace(host=host))
    assert module.asset_discovery_ready(probe) is True
    assert provider.calls == []
    provider.scope = {'domains': [], 'cidrs': []}
    assert module.asset_discovery_ready(probe) is False
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), 503, 'HOST_NOT_READY')
    provider.scope = {'domains': ['example.test'], 'cidrs': []}
    host.business_native_admin = lambda: False
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), 403, 'BUSINESS_ACCESS_DENIED')
    host.business_native_admin = lambda: True
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem(),
        authorize=lambda action, facts: False), 403, 'CAPABILITY_DENIED')
    status, body = api.execute_business({'action': 'easm.assets.list', 'request_id': 'unissued', 'params': {}},
        None, api.BusinessServices(host))
    failed((status, body), 403, 'CAPABILITY_DENIED')
    host.business_v13_providers.clear()
    assert module.asset_discovery_ready(probe) is False
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), 503, 'HOST_NOT_READY')
    assert provider.calls == []


# LLM: 闭合请求先拒绝；提供者抛错后的未知结果不能冒充没有开始。
# 函数用途: 验证 INVALID_SCHEMA、OUTCOME_UNKNOWN 和记录 owner 隔离。
def test_schema_unknown_outcome_and_other_owner_record(host, provider):
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}), 400, 'INVALID_SCHEMA')
    failed(invoke(host, 'easm.assets.list', {'unexpected': True}), 400, 'INVALID_SCHEMA')
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test'], 'ports': [0]}, key=idem()),
        400, 'INVALID_SCHEMA')
    assert provider.calls == []
    provider.failure = OSError('synthetic provider failure')
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), 503, 'OUTCOME_UNKNOWN')
    assert ok(host, 'easm.assets.list')['coverage'] == 'unknown'
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)

    # LLM: 仅测试种入错误 owner，产品查询必须拒绝该行而不是把它展示出来。
    # 函数用途: 在真实 Store 中构造被隔离的记录。
    def seed(records):
        records['foreign'] = {'asset_id': 'foreign', 'owner_id': 'local/other'}
        return {'seeded': True}

    store.transaction('asset_discovery_assets', seed, idempotency_key=idem(), params_digest='foreign-owner')
    failed(invoke(host, 'easm.assets.list'), 404, 'RESOURCE_NOT_FOUND')


# LLM: 对扫描前范围检查保留结构化回归，防止以后先调用 scan 再过滤结果。
# 函数用途: 直接验证错误携带 out_of_scope 且 scan 未调用。
def test_scope_error_details_and_scan_order(host, provider):
    context = api.bind_business_context(host, subject_id='alice', authorize=lambda action, facts: True)
    call = V13Call('easm.scan.run', {'targets': ['example.test.other']}, 'scope-check',
        host.home_paths.owner_id, 'alice', context, api.BusinessServices(host), host.home_paths.owner_home_dir,
        idem(), None, host.business_v13_clock)
    with pytest.raises(module.BusinessError) as error:
        module.scan_run(call)
    assert error.value.details == {'out_of_scope': ['example.test.other']}
    assert provider.calls == []


# LLM: service/certificate 也必须证明主机归属，不能只保护普通域名资产。
# 函数用途: 验证复合值越界与错误 scope 类型拒绝。
@pytest.mark.parametrize('kind,value', [('service', 'https://external.test:443'),
    ('certificate', 'external.test'), ('service', 'https://example.test@external.test')])
def test_complex_provider_asset_scope_is_enforced(host, provider, kind, value):
    provider.assets = [asset(value, kind=kind)]
    failed(invoke(host, 'easm.scan.run', {'targets': ['example.test']}, key=idem()), 403, 'BUSINESS_ACCESS_DENIED')
    assert ok(host, 'easm.assets.list')['coverage'] == 'unknown'
    provider.scope = {'domains': ['example.test'], 'cidrs': [1]}
    assert module.asset_discovery_ready(SimpleNamespace(services=SimpleNamespace(host=host))) is False


# LLM: 原生 HTTP 往返验证不能由 in-process 响应外推。
# 函数用途: 经 W0 真实临时 Gateway 验证示例空参数与闭合信封。
def test_envelopes(v13_native):
    status, body = v13_call(v13_native, 'easm.assets.list', {}, user='business-alice')
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    assert body['data'] == {'items': [], 'next_cursor': None, 'coverage': 'unknown'}
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
