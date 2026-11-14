"""空间同名拒绝沿真实客户端/HTTP保留具体原因，内部空白不隐式改名。"""
from concurrent.futures import ThreadPoolExecutor

from agent_py_agent.tests.test_business_v12_background_scheduling import (
    call,
    ok,
)
from agent_py_agent.tests.test_business_v12_background_scheduling import (
    native as native,
)


def test_native_normalized_name_conflict_explains_nonretryable_no_effect(native):
    original = ok(native, 'spaces.create', {'name': 'Ａｃｃｅｓｓ  Space'},
                  idempotency_key='name-contract-create-001')
    status, conflict = call(native, 'spaces.create', {'name': ' access  SPACE '},
                            idempotency_key='name-contract-conflict-001')
    assert (status, conflict['error']['code'], conflict['effect_outcome']) == (
        409, 'SPACE_NAME_CONFLICT', 'not_started')
    assert conflict['error']['retryable'] is False
    details = {row['key']: row['value'] for row in conflict['error']['details']}
    assert details['field_path'] == '$.params.name'
    assert details['normalization'] == 'NFKC+strip+casefold'
    assert details['retry_same_request'] is False
    assert 'spaces.list' in details['next_action']
    assert '未创建或覆盖' in details['reason']
    assert ok(native, 'spaces.get', {'space_id': original['space_id']})['space']['revision'] == 0
    # 内部空白不是当前名称协议的等价规则；不能重命名或合并旧对象。
    other = ok(native, 'spaces.create', {'name': 'Access Space'},
               idempotency_key='name-contract-distinct-001')
    assert other['space_id'] != original['space_id']
    assert original['name'] == 'Access  Space'
    assert len(ok(native, 'spaces.list')['items']) == 2


def test_concurrent_native_same_names_commit_one_object(native):
    def create(index):
        return call(native, 'spaces.create', {'name': 'Concurrent Name'},
                    idempotency_key=f'concurrent-name-key-{index:04d}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, range(2)))
    assert sorted(status for status, _ in outcomes) == [200, 409]
    [refusal] = [body for status, body in outcomes if status == 409]
    assert refusal['effect_outcome'] == 'not_started'
    assert refusal['error']['retryable'] is False
    assert len(ok(native, 'spaces.list')['items']) == 1
