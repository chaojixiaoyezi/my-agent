import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.agent_tool_approval import (
    AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT,
)
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    create_input_dependency,
    input_dependency_path,
    resolve_input_dependency,
)
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    CredentialInputError,
    request_credential_card,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card

_FIXTURES = (background,)


# LLM: 使用真实TaskStore输入登记路径和fake owner执行上下文，容量只读取准确root refs，不依靠输出文案。
# 函数用途: 在隔离owner目录创建一条有限期合成必要输入。
def _create(ctx, input_id, *, scope=None):
    return create_input_dependency(ctx.agent,scope or tool_approval_scope(ctx.agent,ctx.task_id),
        input_id=input_id,provider='capacity-synthetic',expires_at=time.time()+120)


def test_exact_root_32_limit_duplicate_and_33_rejection_has_no_dependency(background):
    assert AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT == 32
    for number in range(32):
        _create(background,f'capacity-{number}')
    row = _create(background,'capacity-0')
    assert row['input_id']=='capacity-0'
    with pytest.raises(ValueError,match='TASK_INPUT_CAPACITY_EXCEEDED'):
        _create(background,'capacity-33')
    assert len(background.store.tasks.load(background.task_id).input_dependency_refs)==32
    assert not input_dependency_path(background.agent,'capacity-33').exists()


def test_33_concurrent_creates_share_original_task_lock_and_only_32_succeed(background):
    scope = tool_approval_scope(background.agent,background.task_id)
    barrier = Barrier(33)
    # LLM: 同步启动真实写入而非mock容量；失败入口仍通过原TaskStore/输入文件锁。
    # 函数用途: 模拟同根多个孩子并发申请必要输入，返回成功ID或原拒绝码。
    def attempt(number):
        input_id = f'concurrent-{number}'
        barrier.wait(timeout=10)
        try:
            _create(background,input_id,scope=scope)
            return input_id, ''
        except ValueError as error:
            return input_id, str(error)
    with ThreadPoolExecutor(max_workers=33) as pool:
        results = list(pool.map(attempt,range(33)))
    admitted = {input_id for input_id,error in results if not error}
    rejected = [(input_id,error) for input_id,error in results if error]
    assert len(admitted)==32 and len(rejected)==1
    assert rejected[0][1]=='TASK_INPUT_CAPACITY_EXCEEDED'
    assert set(background.store.tasks.load(background.task_id).input_dependency_refs)==admitted
    assert not input_dependency_path(background.agent,rejected[0][0]).exists()


def test_terminal_releases_slot_before_archive_and_replay_does_not_take_a_new_slot(background):
    for number in range(32):
        _create(background,f'capacity-{number}')
    resolve_input_dependency(background.agent,'capacity-0',status='cancelled')
    assert len(background.store.tasks.load(background.task_id).input_dependency_refs)==31
    assert background.store.wakes.pending()==[]
    _create(background,'new-slot')
    assert _create(background,'capacity-0')['status']=='cancelled'
    assert len(background.store.tasks.load(background.task_id).input_dependency_refs)==32
    arm_input_dependency(background.agent,'capacity-0')
    assert len(background.store.wakes.pending())==1
    assert _create(background,'capacity-0')['status']=='cancelled'
    assert len(background.store.tasks.load(background.task_id).input_dependency_refs)==32


def test_credential_capacity_failure_rolls_back_new_row_and_sends_no_card(background,monkeypatch):
    _ref,path,_body,sent = _card(background)
    for number in range(31):
        _create(background,f'capacity-{number}')
    before = set(path.parent.glob('*.json'))
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
        lambda _agent: SimpleNamespace(im_task_credentials_enabled=True))
    result = RequestCredentialsTool(background.agent).execute({'url':'https://example.com/api',
        'authentication':'bearer','__tool_call_id':'capacity-rejected-new-card'})
    assert not result.ok and result.error_code=='CREDENTIAL_INPUT_UNAVAILABLE'
    assert len(sent)==1 and set(path.parent.glob('*.json'))==before
    assert len(background.store.tasks.load(background.task_id).input_dependency_refs)==32


def test_credential_update_capacity_failure_restores_previous_encrypted_authorization(background):
    reference,path,body,sent = _card(background)
    arm_input_dependency(background.agent,read_json_object_report(path,context='test').payload['input_id'])
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    previous = path.read_text()
    for number in range(32):
        _create(background,f'capacity-{number}')
    with pytest.raises(CredentialInputError) as failure:
        request_credential_card(background.agent,url='https://example.com/api',authentication='basic',
            options=CredentialCardOptions(refresh_ref=reference))
    assert failure.value.code == 'CREDENTIAL_INPUT_UNAVAILABLE'
    assert failure.value.stage == 'input_registration'
    assert path.read_text()==previous and len(sent)==1
    assert not input_dependency_path(background.agent,reference+':2').exists()
