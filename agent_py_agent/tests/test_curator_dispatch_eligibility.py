"""待审核候选保留原资格；只有实际工作才能触发新策展运输和运行。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    curator_input_revision,
)
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
from agent_py_agent.agent.owner_wake_discovery import _has_pending_memory_curator_work
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)


# LLM: 原服务仅写本测试临时owner；先以原日终成功建立已消费状态，杜绝时钟/旧游标把候选only伪装成新输入。
# 函数用途: 创建不允许模型调用的临时策展器，并留下近期成功且当天已收尾的真实状态。
def _quiet_case(tmp_path):
    root = tmp_path / 'home'
    owner = OwnerIdentity.provider_user('local', 'synthetic-dispatch')
    home = _owner_home_dir(root, owner)
    store = ConversationStore(home / 'conversations')
    backend = _StaticStructuredBackend({})
    service = _service(home, backend, store, timezone_name='UTC')
    assert service.run(reason='daily_finalize').status == 'succeeded'
    return root, owner, home, service, backend


# LLM: 合成来源只用于原CandidateService资格/状态机，不构造Agent或绕过正式确认；approved仍不是自动发布授权。
# 函数用途: 准备真实仓库中的待确认候选，保留宿主计算的auto_eligible字段。
def _pending_candidate(service, status):
    candidate = service.candidate_service.observe(CandidateObservation(
        candidate_type='lesson', content='合成测试规则。', subject_key='synthetic.dispatch',
        scope={'scope_type': 'personal', 'scope_key': 'personal'}, origin='model_inferred',
        promotion_target='lesson', learning_source=user_dialogue_projection(),
        observation_id='synthetic-dispatch-observation'))
    if status == 'approved':
        candidate = service.candidate_service.transition(candidate.candidate_id, 'approved', reviewer='synthetic-review')
    assert candidate.promotion_mode == 'auto_eligible'
    return candidate


# LLM: 这里仅调用原只读来源枚举叶，不构造controller/Root或调用enqueue；正式P3/提交权限仍由既有背景回归覆盖。
# 函数用途: 查看实际owner是否提供新的策展运输候选，不执行或补造任何队列请求。
def _curator_sources(root, owner, home):
    discovery = SimpleNamespace(control=SimpleNamespace(home_root=root, config=SimpleNamespace(
        owner_maintenance_scan_interval_seconds=0, memory_curator_enabled=True, enable_self_learning=False)))
    return [ref for ref in CoordinatorBackgroundRequests._owner_sources(
        discovery, SimpleNamespace(identity=owner, home_dir=home)) if ref['kind'] == 'curator']


@pytest.mark.parametrize('status', ['pending_review', 'approved'])
@pytest.mark.parametrize('policy', ['conservative_v1', 'manual_only', 'no_callback'])
def test_candidate_only_does_not_publish_new_curator_source(tmp_path, status, policy):
    root, owner, home, service, backend = _quiet_case(tmp_path)
    _pending_candidate(service, status)
    if policy != 'no_callback':
        service.config = replace(service.config, auto_promotion_policy=policy)
        service.promotion_callback = lambda _candidate_id: pytest.fail('candidate-only must not attempt promotion')
    before = service.state_store.path.read_bytes(), service.candidate_service.path.read_bytes(), curator_input_revision(home)
    assert _has_pending_memory_curator_work(home) is False
    assert _curator_sources(root, owner, home) == []
    assert (service.state_store.path.read_bytes(), service.candidate_service.path.read_bytes(), curator_input_revision(home)) == before
    assert backend.calls == 0


@pytest.mark.parametrize('status', ['pending_review', 'approved'])
@pytest.mark.parametrize('policy', ['conservative_v1', 'manual_only', 'no_callback'])
def test_candidate_only_is_not_due_and_preserves_complete_state(tmp_path, status, policy):
    _root, _owner, home, service, backend = _quiet_case(tmp_path)
    candidate = _pending_candidate(service, status)
    if policy != 'no_callback':
        service.config = replace(service.config, auto_promotion_policy=policy)
        service.promotion_callback = lambda _candidate_id: pytest.fail('candidate-only must not attempt promotion')
    before = service.state_store.path.read_bytes(), service.candidate_service.path.read_bytes(), curator_input_revision(home)
    for _ in range(2):
        result = service.run_if_due()
        assert result.status == 'not_due' and result.run_id == ''
    assert (service.state_store.path.read_bytes(), service.candidate_service.path.read_bytes(), curator_input_revision(home)) == before
    assert service.candidate_service.get(candidate.candidate_id).promotion_mode == 'auto_eligible'
    assert backend.calls == 0


@pytest.mark.parametrize('reason', ['task_complete', 'pre_compact'])
def test_explicit_pending_reason_still_runs_original_curator(tmp_path, reason):
    root, owner, home, service, backend = _quiet_case(tmp_path)
    _pending_candidate(service, 'pending_review')
    service.request(reason)
    assert _has_pending_memory_curator_work(home) is True
    assert len(_curator_sources(root, owner, home)) == 1
    result = service.run_if_due()
    assert result.status == 'succeeded' and result.reason == reason
    assert service.state_store.load().pending_reasons == []
    assert backend.calls == 0


@pytest.mark.parametrize('expired', [False, True])
def test_original_lease_still_keeps_owner_discoverable(tmp_path, expired):
    root, owner, home, service, _backend = _quiet_case(tmp_path)
    _pending_candidate(service, 'pending_review')
    now = datetime.now(timezone.utc) - timedelta(hours=1 if expired else 0)
    acquired = service.state_store.acquire(reason='interval', config_revision=service.config.revision(), lease_seconds=60, now=now)
    assert acquired is not None
    assert _has_pending_memory_curator_work(home) is True
    assert len(_curator_sources(root, owner, home)) == 1
    assert service.state_store.load().active_lease == acquired[1]


def test_new_input_still_uses_original_collection_and_model(tmp_path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store, timezone_name='UTC')
    result = service.run_if_due()
    assert result.status == 'succeeded' and result.processed_messages == 1
    assert backend.calls == 1


def test_daily_finalize_still_runs_without_candidate_trigger(tmp_path):
    _root, _owner, _home, service, backend = _quiet_case(tmp_path)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    result = service.run_if_due(now=tomorrow.replace(hour=23))
    assert result.status == 'succeeded' and result.reason == 'daily_finalize'
    assert service.state_store.load().last_daily_finalize_date == tomorrow.date().isoformat()
    assert backend.calls == 0
