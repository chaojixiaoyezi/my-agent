# LLM: 真实临时owner目录、真实配额账本与回环HTTP；只在异常边界注入故障，不读真实home或秘密。
# 模块用途: 覆盖有限配额普通用户建空间(N03)与托管用户/业务/兼容入口/资产投递的错误透明度(R17/R12/R07/O03)。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.common.nofollow_fs import write_bytes_atomic_beneath
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.user_space.owner_quota import (
    LocalUserQuotaLimits,
    OwnerQuotaChange,
    OwnerQuotaUnavailable,
    owner_quota_enforcer_from_policy,
    reconcile_owner_quota_slice,
    seed_local_user_quota,
)
from agent_py_agent.tests.test_gap_w1_wire_roundtrip import (
    wire,  # noqa: F401  (OpenAI兼容回环fixture)
)

# 活跃普通用户目录的文件数：超过前台128项全新计数，准入只能沿逐目录基线核验（N03的实际触发面）。
_ACTIVE_USER_FILES = 160


# LLM: 只按原seed合同写quota.json，再铺普通工作区文件模拟活跃用户；不绕过配额账本。
# 函数用途: 创建一个有限配额的普通本地用户owner目录。
def _finite_user(tmp_path, owner_id='u1'):
    owner = tmp_path / 'owners' / 'local' / owner_id
    owner.mkdir(parents=True)
    seed_local_user_quota(tmp_path, owner, LocalUserQuotaLimits(max_disk_mb=1024))
    for index in range(_ACTIVE_USER_FILES):
        folder = owner / 'workspace' / f'notes-{index // 16:02d}'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'note-{index:03d}.md').write_text('draft\n')
    return owner


# 函数用途: 用原对账入口把owner配额账推进到一轮完整对账。
def _reconcile(owner):
    for _ in range(64):
        if reconcile_owner_quota_slice(owner, max_entries=4096, max_ms=5000)['complete']:
            return
    pytest.fail('owner quota reconciliation did not complete')


# 函数用途: 读取owner配额账本供断言。
def _ledger(owner):
    return json.loads((owner / '.owner-quota-ledger.json').read_text())


# LLM: 与调度器仓库相同，宿主写入先在owner配额准入内声明再原子替换。
# 函数用途: 在data/scheduler下做一次已声明的宿主写入。
def _declared_scheduler_write(owner):
    data = b'{"jobs": []}\n'
    with owner_quota_enforcer_from_policy(owner).admission() as admission:
        admission.check([OwnerQuotaChange(owner / 'data' / 'scheduler' / 'jobs.json', final_size=len(data))])
        write_bytes_atomic_beneath(owner, ('data', 'scheduler', 'jobs.json'), data)


# 函数用途: 绑定owner的业务空间服务与只放行本owner的调用主体。
def _service(owner, owner_id='u1'):
    service = BusinessSpaceService(owner, owner_id=owner_id, enabled=True)
    actor = BusinessActor(owner_id, owner_id + '-web', service,
                          lambda action, scope: scope['owner_id'] == owner_id)
    return service, actor


# 函数用途: 经领域统一入口执行一次spaces.create。
def _create(service, actor, name, key):
    return service.call('spaces.create', {'name': name}, actor=actor, idempotency_key=key)


# LLM: N03：首次建空间新建的data/business、日志复位都在同一准入声明内；两次创建都完成且账本保持可信。
# 函数用途: 验证有限配额普通用户对账完成并有宿主写入后连续两次建空间都成功。
def test_finite_quota_user_creates_space_twice_after_reconcile_and_scheduler_write(tmp_path):
    owner = _finite_user(tmp_path)
    _reconcile(owner)
    _declared_scheduler_write(owner)
    service, actor = _service(owner)

    first = _create(service, actor, '作品一', 'n03-create-one')
    second = _create(service, actor, '作品二', 'n03-create-two')

    assert first['state'] == second['state'] == 'active'
    assert first['space_id'] != second['space_id']
    listed = service.call('spaces.list', {}, actor=actor)['items']
    assert {row['space_id'] for row in listed} == {first['space_id'], second['space_id']}
    ledger = _ledger(owner)
    assert ledger['dirty_since'] is None and ledger['usage_authoritative'] is True
    assert ledger['reconcile_cursor'].get('metadata_invalidated') is not True


# LLM: 准入外直接写入owner根后旧基线不可信，增长仍关闭式拒绝；拒绝发生在提交日志之前，必须如实报not_started。
# 函数用途: 验证对账未完成的配额拒绝转为结构化HOST_NOT_READY，且对账后同键重试成功。
def test_reconciliation_pending_is_host_not_ready_not_started(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_api import _error_code, _error_details

    owner = _finite_user(tmp_path)
    _reconcile(owner)
    (owner / 'imported-outside-host.txt').write_text('copied in without admission\n')
    service, actor = _service(owner)

    with pytest.raises(BusinessError) as refused:
        _create(service, actor, '作品', 'n03-pending')

    error = refused.value
    assert error.code == 'HOST_NOT_READY' and error.effect_outcome == 'not_started'
    assert isinstance(error.__cause__, OwnerQuotaUnavailable)
    assert error.details['reason'].startswith('owner_quota_')
    assert error.details['reason'] == 'owner_quota_reconciliation_pending'
    assert error.details['side_effect_state'] == 'not_started'
    assert error.details['retry_same_arguments'] is True and error.details['next_action']
    business = owner / 'data' / 'business'
    assert not (business / 'catalog.json').exists() and not (business / 'receipts').exists()
    journal = business / '.catalog-transaction.json'
    assert not journal.exists() or journal.read_bytes() == b'{}\n'
    # HTTP信封：503且not_started，不再是“请勿重试写操作”的OUTCOME_UNKNOWN。
    assert _error_code(error) == ('HOST_NOT_READY', 503, 'not_started')
    assert {'key': 'reason', 'value': 'owner_quota_reconciliation_pending'} in _error_details(error)

    _reconcile(owner)
    created = _create(service, actor, '作品', 'n03-pending')
    assert created['state'] == 'active'


# LLM: 管理员式无限配额不建配额账本，准入不扫描；声明首次目录也不能改变这一点。
# 函数用途: 验证无限配额owner的建空间行为不变。
def test_unlimited_quota_owner_control_case_unchanged(tmp_path):
    owner = tmp_path / 'owners' / 'local' / 'main'
    owner.mkdir(parents=True)
    (owner / 'quota.json').write_text(json.dumps({'max_disk_mb': 0}))
    service, actor = _service(owner, 'main')

    first = _create(service, actor, '作品一', 'control-one')
    second = _create(service, actor, '作品二', 'control-two')

    assert first['state'] == second['state'] == 'active'
    assert _create(service, actor, '作品一', 'control-one') == first
    assert not (owner / '.owner-quota-ledger.json').exists()


# LLM: R17：真实回环HTTP；已登记普通用户在原池入口被宿主拒绝，必须原样返回HostAPIError，不能误报幂等冲突。
# 函数用途: 验证托管普通用户经非Web /ask 得到准确的HOST_NOT_READY而不是409。
def test_managed_local_user_ask_reports_host_error_not_idempotency_conflict(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.tests.test_gateway_c_http_wait import request
    from agent_py_agent.tests.test_local_ask_owner_transport import _http_case

    with _http_case(tmp_path, monkeypatch, 0) as (agent, _gate, server):
        LocalUserLifecycle(LocalUserStore(agent.home_paths.root)).create('alice', idempotency_key='r17-create-alice')
        # 运行中的Gateway已为其他owner建好原池；受理时被动查池即由原账号闸门拒绝该已登记用户。
        _owner_pool(agent)
        queued = sorted(path.name for path in server.paths.inbox.glob('*.json'))
        status, body = request(server, 'POST', '/ask', {
            'goal': '帮我整理今天的笔记', 'conversation_id': 'r17-tui',
            'metadata': {'message_id': 'r17-message-one'}}, headers={'X-User-Id': 'alice', 'X-Channel': 'local'})

    assert status == 503, body
    assert body['error_code'] == 'HOST_NOT_READY' != 'IDEMPOTENCY_CONFLICT'
    assert body['error']['code'] == 'HOST_NOT_READY' and body['error']['outcome'] == 'not_started'
    assert body['error']['retryable'] is True and body['request_id']
    assert '/client/business/ui' in body['error']['message']
    assert sorted(path.name for path in server.paths.inbox.glob('*.json')) == queued


# LLM: R12：未绑定会话的只读后台请求仍403且零副作用，但细节须说明原因、未开始和下一步。
# 函数用途: 验证ACTOR_REQUIRED带结构化details并保持不建线程。
def test_unbound_read_actor_required_explains_next_step(tmp_path):
    import http.client

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.cli.chat_client_context import (
        _gateway_request_identity,
        owner_identity_from_config,
    )

    host = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False, my_agent_home=str(tmp_path / 'home'),
        memory_path='memory.jsonl', business_spaces_enabled=True), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(host.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=host))
    server.start()
    identity = _gateway_request_identity(owner_identity_from_config(host.config))
    threads = host.conversation_store.storage.threads_dir
    before = sorted(path.name for path in threads.glob('*.json'))
    try:
        connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
        try:
            connection.request('POST', '/client/business', json.dumps({
                'action': 'background_agents.list', 'request_id': 'r12-unbound', 'params': {'limit': 10}}), {
                'X-Gateway-Token': server._local_client_credential, 'Content-Type': 'application/json',
                'X-User-Id': identity['user_id'], 'X-Channel': identity['channel'],
                'X-Conversation-Id': 'never-bound'})
            response = connection.getresponse()
            status, denied = response.status, json.loads(response.read())
        finally:
            connection.close()
    finally:
        server.stop()

    assert status == 403 and denied['error']['code'] == 'ACTOR_REQUIRED', denied
    assert denied['effect_outcome'] == 'not_started'
    details = {item['key']: item['value'] for item in denied['error']['details']}
    assert details['reason'] and details['side_effect_state'] == 'not_started'
    assert '/team list' in details['next_action']
    assert sorted(path.name for path in threads.glob('*.json')) == before


# LLM: R07：OpenAI兼容入口的未预期异常仍不回显正文，但响应带异常类型、宿主stderr留结构化诊断。
# 函数用途: 验证兼容入口兜底500带details.exception_type并记录gateway-loop-error。
def test_openai_compat_unexpected_error_reports_exception_type(wire, monkeypatch, capsys):  # noqa: F811
    from agent_py_agent.agent.business_spaces import gap158_w1_openai_compat
    from agent_py_agent.tests.test_gap_w1_openai_compat import body
    from agent_py_agent.tests.test_gap_w1_wire_roundtrip import request

    def broken(*_args, **_kwargs):
        raise KeyError('profile-index')

    monkeypatch.setattr(gap158_w1_openai_compat, 'execute', broken)
    capsys.readouterr()
    status, _headers, raw = request(wire, '/v1/chat/completions', body('private-alice'))

    payload = json.loads(raw)
    assert status == 500 and payload['error']['code'] == 'HOST_NOT_READY', payload
    assert payload['error']['details'] == {'exception_type': 'KeyError'}
    assert 'profile-index' not in raw.decode()
    logged = [line for line in capsys.readouterr().err.splitlines() if line.startswith('[gateway-loop-error]')]
    assert len(logged) == 1 and json.loads(logged[0].split(' ', 1)[1])['error_type'] == 'KeyError'


# LLM: O03：资产投递通用兜底保留原异常的结构化reason_code，模型才能区分配额对账未完成等暂态原因。
# 函数用途: 验证asset_delivery异常的error_details含reason_code。
def test_asset_delivery_generic_failure_keeps_reason_code(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import asset_delivery_tool

    def pending(_service, _owner_id, _params):
        raise OwnerQuotaUnavailable('owner usage reconciliation pending; growth refused',
                                    reason_code='reconciliation_pending')

    monkeypatch.setattr(asset_delivery_tool, 'dispatch_delivery_action', pending)
    tool = asset_delivery_tool.AssetDeliveryTool(tmp_path / 'home', 'local/caller')
    result = tool.execute({'action': 'send', 'recipient_owner_id': 'local/target',
                           'asset_kind': 'script', 'asset_id': 'demo'})

    assert not result.ok and result.error_code == 'ASSET_DELIVERY_FAILED'
    details = result.result_envelope['error_details']
    assert details['reason_code'] == 'reconciliation_pending'
    assert details['exception_type'] == 'OwnerQuotaUnavailable' and details['action'] == 'send'
