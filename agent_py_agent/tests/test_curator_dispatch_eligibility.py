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
from agent_py_agent.agent.settings.config import AgentConfig
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


# LLM: 仅调用原只读来源枚举叶；实际owner配置沿原dataclass解析，timezone与本测试consumer同为UTC，不构造controller/Root或enqueue。
# 函数用途: 查看实际owner是否提供新的策展运输候选，不执行或补造任何队列请求。
def _curator_sources(root, owner, home):
    discovery = SimpleNamespace(control=SimpleNamespace(home_root=root, config=AgentConfig(
        owner_maintenance_scan_interval_seconds=0, memory_curator_enabled=True, enable_self_learning=False, timezone='UTC')))
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
    assert _has_pending_memory_curator_work(home,timezone_name='UTC') is False
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


# LLM: 冻结本测试进程clock；有tzset时设置并恢复真实本地TZ，无该API显式模拟fixture zone，不改系统时间/真实home/服务。
# 函数用途: 对照发现层与consumer在同一瞬间、同一服务器时区的日期判断。
def _curator_clock(monkeypatch, instant, local_zone):
    import time
    from contextlib import contextmanager
    from pathlib import Path

    from agent_py_agent.agent import owner_wake_discovery

    # LLM: 有tzset保真实宿主TZ并恢复；无该API只模拟fixture显式zone，不声称已改Windows系统时区。
    # 函数用途: 临时安装同源时间事实，保Mac/Linux实际本地转换与portable显式zone分支。
    @contextmanager
    def clock():
        has_tzset = callable(getattr(time, 'tzset', None))
        try:
            with monkeypatch.context() as patch:
                if has_tzset:
                    if not (Path('/usr/share/zoneinfo') / local_zone).is_file():
                        import tzdata
                        patch.setenv('TZDIR', str(Path(tzdata.__file__).resolve().parent / 'zoneinfo'))
                    patch.setenv('TZ',local_zone)
                    time.tzset()
                else:
                    from agent_py_agent.agent.memory_store import curator_due
                    localized = curator_due.localized_now
                    patch.setattr(curator_due, 'localized_now', lambda value, zone: localized(value, zone or local_zone))
                    boundary = curator_due.next_local_boundary
                    patch.setattr(curator_due, 'next_local_boundary', lambda current, zone, *args, **kwargs:
                                  boundary(current, zone or local_zone, *args, **kwargs))
                # LLM: 只替换发现层原datetime读点，aware instant不受进程时区转换损失。
                # 类用途: 返回固定时刻供旧、新发现入口共同读取。
                class FixedDateTime(datetime):
                    # LLM: 模拟标准库now可选时区，未指定时保持原服务器本地语义。
                    # 函数用途: 提供同一个合成时刻。
                    @classmethod
                    def now(cls,tz=None):
                        return instant.astimezone(tz)
                patch.setattr(owner_wake_discovery,'datetime',FixedDateTime)
                patch.setattr(owner_wake_discovery.time,'time',lambda:instant.timestamp())
                yield '' if has_tzset else local_zone
        finally:
            if has_tzset:
                time.tzset()
    return clock()


# LLM: 用规范state模型创建合成输入，不将payload缺失当空或略过consumer解析。
# 函数用途: 保存近期成功、无pending/lease的只读时间资格样本。
def _curator_due_state(tmp_path, instant):
    import json
    from dataclasses import asdict

    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState
    state = MemoryCuratorState(last_success_at=(instant-timedelta(seconds=60)).isoformat())
    path = tmp_path/'memory/curator/state.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(asdict(state)))
    return state


@pytest.mark.parametrize('zone,instant_text',[
    ('America/Los_Angeles','2026-10-19T23:20:00+00:00'),
    ('Asia/Shanghai','2026-10-19T15:00:00+00:00'),
])
def test_curator_due_time_discovery_matches_consumer_server_local_day(tmp_path,monkeypatch,zone,instant_text):
    from agent_py_agent.agent.memory_store.curator import _daily_finalize_due, _localized_now
    instant = datetime.fromisoformat(instant_text)
    state = _curator_due_state(tmp_path,instant)
    with _curator_clock(monkeypatch,instant,zone) as explicit_zone:
        expected = _daily_finalize_due(state,_localized_now(instant,explicit_zone),23)
        assert _has_pending_memory_curator_work(tmp_path) is expected


@pytest.mark.parametrize('interval,hour,expected',[(10800,17,True),(30,23,True),(10800,23,False)])
def test_curator_due_time_uses_explicit_configuration(tmp_path,monkeypatch,interval,hour,expected):
    import os

    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    instant = datetime.fromisoformat('2026-10-19T18:00:00+00:00')
    _curator_due_state(tmp_path,instant)
    audit = tmp_path/'audit/new.jsonl'
    audit.parent.mkdir()
    audit.write_text('{}\n')
    os.utime(audit,(instant.timestamp(),instant.timestamp()))
    with _curator_clock(monkeypatch,instant,'UTC'):
        assert _has_pending_memory_curator_work(tmp_path,curator_config=MemoryCuratorConfig(
            interval_seconds=interval,daily_finalize_hour=hour),timezone_name='UTC') is expected


@pytest.mark.parametrize('zone',['','America/Los_Angeles'])
def test_curator_due_time_future_local_deadline_keeps_dst_rules(tmp_path,monkeypatch,zone):
    from zoneinfo import ZoneInfo

    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    instant = datetime.fromisoformat('2026-11-01T08:30:00+00:00')
    _curator_due_state(tmp_path,instant)
    deadlines = []
    with _curator_clock(monkeypatch,instant,'America/Los_Angeles'):
        assert not _has_pending_memory_curator_work(tmp_path,curator_config=MemoryCuratorConfig(),
            timezone_name=zone,deadline_out=deadlines)
    target = datetime(2026,11,1,23,tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()
    assert target in deadlines
    assert datetime.fromtimestamp(target,timezone.utc).hour==7


@pytest.mark.parametrize('code,interval,elapsed,expected',[
    ('SYNTHETIC_FAILURE',30,31,True),('SYNTHETIC_FAILURE',30,29,False),
    ('CURATOR_MODEL_NOT_CONFIGURED',30,31,False),('CURATOR_MODEL_NOT_CONFIGURED',30,3600,True),
])
def test_curator_due_time_failure_backoff_matches_consumer(tmp_path,monkeypatch,code,interval,elapsed,expected):
    import json
    from dataclasses import asdict

    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    instant = datetime.fromisoformat('2026-10-19T18:00:00+00:00')
    state = _curator_due_state(tmp_path,instant)
    state.pending_reasons=['admin']
    state.last_failure_code=code
    state.last_failure_at=(instant-timedelta(seconds=elapsed)).isoformat()
    (tmp_path/'memory/curator/state.json').write_text(json.dumps(asdict(state)))
    with _curator_clock(monkeypatch,instant,'UTC'):
        assert _has_pending_memory_curator_work(tmp_path,curator_config=MemoryCuratorConfig(interval_seconds=interval),
            timezone_name='UTC') is expected


@pytest.mark.parametrize('change',['time','config','source','unknown'])
def test_curator_due_time_final_commit_rechecks_inside_original_c_gate(tmp_path,monkeypatch,change):
    import json
    from dataclasses import asdict

    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
        BackgroundSourceChanged,
        freeze_source,
    )
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
    from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context

    with curator_case(tmp_path,monkeypatch) as (agent,service,backend,gate,_claim,_close):
        context = cold_context(agent,gate)
        context.control_plane.config.timezone='UTC'
        context.control_plane.config.memory_curator_daily_finalize_hour=23
        producer = CoordinatorBackgroundRequests(context.control_plane,drain_runtime=context.drain_runtime)
        instant = datetime.fromisoformat('2026-10-19T23:59:59+00:00')
        state = service.state_store.load()
        state.pending_reasons=[]
        state.active_lease={}
        state.last_success_at=instant.isoformat()
        state.last_daily_finalize_date=''
        service.state_store.path.write_text(json.dumps(asdict(state)))
        original_commit = producer.commit
        entered=[]
        # LLM: 仍进入真正self.commit持有原C/controller/lease/runtime门；时间交错用内层context先恢复，不能由外层fixture把冻结时钟泄漏给后续测试。
        # 函数用途: 在最后实际写动作前移动资格或来源，并在原写动作返回后立即恢复时间替身。
        def commit(owner,operation):
            def late():
                entered.append(True)
                if change=='time':
                    from agent_py_agent.agent import owner_wake_discovery
                    with monkeypatch.context() as shift:
                        shift.setattr(owner_wake_discovery.time,'time',lambda:(instant+timedelta(seconds=2)).timestamp())
                        return operation()
                elif change=='config':
                    context.control_plane.config.memory_curator_interval_seconds += 1
                elif change=='source':
                    updated={**asdict(state),'last_daily_finalize_date':'2026-10-19'}
                    service.state_store.path.write_text(json.dumps(updated))
                else:
                    service.state_store.path.write_text('{"schema_version":"unknown"}')
                return operation()
            return original_commit(owner,late)
        monkeypatch.setattr(producer,'commit',commit)
        with _curator_clock(monkeypatch,instant,'UTC'):
            config = MemoryCuratorConfig.from_agent_config(context.control_plane.config)
            ref = freeze_source(agent.home_paths.root,OwnerIdentity.local_main(),service.state_store.path,'curator',
                curator_config=config,timezone_name='UTC')
            inbox_before={p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}
            if change=='time':
                assert producer._submit(ref) is False
            else:
                with pytest.raises(BackgroundSourceChanged):
                    producer._submit(ref)
        assert entered==[True]
        assert {p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}==inbox_before
        assert backend.calls==0


def test_curator_commit_clock_fixture_restores_global_time_after_teardown(tmp_path,monkeypatch):
    import time

    original_clock = time.time
    # 注册等值原函数，保证本断言失败时也由外层fixture恢复整个进程的时钟。
    monkeypatch.setattr(time,'time',original_clock)
    with monkeypatch.context() as isolated:
        test_curator_due_time_final_commit_rechecks_inside_original_c_gate(tmp_path,isolated,'time')
    assert time.time is original_clock


@pytest.mark.parametrize('enabled',[True,False])
def test_curator_due_time_original_commit_creates_once_or_disabled_no_effect(tmp_path,monkeypatch,enabled):
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import freeze_source
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
    from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context

    with curator_case(tmp_path,monkeypatch) as (agent,service,backend,gate,_claim,_close):
        context=cold_context(agent,gate)
        context.control_plane.config.timezone='UTC'
        producer=CoordinatorBackgroundRequests(context.control_plane,drain_runtime=context.drain_runtime)
        import json
        from dataclasses import asdict
        state=service.state_store.load()
        state.pending_reasons.append('task_complete')
        service.state_store.path.write_text(json.dumps(asdict(state)))
        ref=freeze_source(agent.home_paths.root,OwnerIdentity.local_main(),service.state_store.path,'curator',
            curator_config=MemoryCuratorConfig.from_agent_config(context.control_plane.config),timezone_name='UTC')
        context.control_plane.config.memory_curator_enabled=enabled
        before={p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}
        if enabled:
            assert producer._submit(ref) is True
            assert producer._submit(ref) is False
        else:
            assert producer._submit(ref) is False
        after={p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}
        if enabled:
            assert len(after)==len(before)+1
            assert {key:after[key] for key in before}==before
        else:
            assert after==before
        assert backend.calls==0


def test_curator_due_time_does_not_catch_ordinary_value_error(tmp_path,monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_discovery as discovery
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import freeze_source
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
    from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context

    with curator_case(tmp_path,monkeypatch) as (agent,service,backend,gate,_claim,_close):
        context=cold_context(agent,gate)
        producer=CoordinatorBackgroundRequests(context.control_plane,drain_runtime=context.drain_runtime)
        ref=freeze_source(agent.home_paths.root,OwnerIdentity.local_main(),service.state_store.path,'curator')
        before={p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}
        # LLM: 仅注入普通配置读取故障，故障文字故意与私有异常相同，不能被正文相等降成not-due。
        # 函数用途: 验证_submit只捕获准确私有类型。
        def rejected(*args,**kwargs):
            raise ValueError('')
        monkeypatch.setattr(discovery,'_curator_submission_config',rejected)
        with pytest.raises(ValueError):
            producer._submit(ref)
        assert {p.name:p.read_bytes() for p in gate.paths.inbox.glob('*.json')}==before
        assert backend.calls==0
