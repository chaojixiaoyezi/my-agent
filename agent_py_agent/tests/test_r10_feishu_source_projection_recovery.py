"""索引丢失保未知；复用真实owner分页发现、原C门和同一规范解析器恢复。"""
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.scheduler.due_index import SchedulerDueIndex
from agent_py_agent.tests.test_r10_feishu_personal_event_sources import case, pending

_FIXTURES = (case,)


def lose_projection(case):
    path = case.base.scheduler_repository.due_index.path
    for suffix in ('', '-wal', '-shm'):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    return SchedulerDueIndex(path)


def test_count_zero_real_discovery_restores_two_personal_owners_before_negative_proof(case, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery
    from agent_py_agent.agent.gateway_parts.channel_observations import accept_channel_observation
    from agent_py_agent.cli.gateway_loops import _OwnerWakeHints

    index = lose_projection(case)
    assert accept_channel_observation(case.base, case.event)['reason_code'] == 'EVENT_SOURCE_INDEX_RECOVERING'
    assert case.leases == []
    monkeypatch.setattr(discovery, '_owner_fact_kind', lambda _home: 'none')
    hints = _OwnerWakeHints(SimpleNamespace(agent=case.base))
    cursor, pages, positive_seen = None, 0, False
    try:
        for _ in range(16):
            page = hints.discovery.page(case.base.home_paths.owners_dir, limit=1, after_cursor=cursor)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break
            assert not index.source_discovery_ready()
            if index.source_owner_page(case.event) == ('feishu', 'user', 'ou_a'):
                result = accept_channel_observation(case.base, case.event)
                assert result['status'] == 'retryable' and 'source_owner_cursor' not in result
                assert len(pending(case, 'ou_a')) == 1
                positive_seen = True
        assert cursor is None and pages >= 2 and positive_seen and index.source_discovery_ready()
        first = accept_channel_observation(case.base, case.event)
        assert first['status'] == 'partial'
        assert accept_channel_observation(case.base, case.event, source_owner_cursor=first['source_owner_cursor'])['status'] == 'accepted'
        assert len(pending(case, 'ou_a')) == len(pending(case, 'ou_b')) == 1
    finally:
        hints.close()


def test_discovery_error_on_earlier_page_is_not_forgotten_at_full_round_end(case, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery
    from agent_py_agent.cli.gateway_loops import _OwnerWakeHints

    index = lose_projection(case)
    monkeypatch.setattr(discovery, '_owner_fact_kind', lambda _home: 'none')
    hints = _OwnerWakeHints(SimpleNamespace(agent=case.base))
    original = hints.discovery.note_owner
    failed = []
    def note(owner, home):
        if not failed:
            failed.append(owner)
            raise PermissionError('synthetic')
        return original(owner, home)
    hints.discovery.note_owner = note
    cursor = None
    try:
        for _ in range(16):
            page = hints.discovery.page(case.base.home_paths.owners_dir, limit=1, after_cursor=cursor)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert failed and cursor is None and not index.source_discovery_ready()
    finally:
        hints.close()


def test_count_positive_uses_original_root_commit_and_same_paged_source_repair(case, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_discovery as discovery
    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
    from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context

    lose_projection(case)
    with curator_case(tmp_path, monkeypatch) as (agent, _service, _backend, gate, _claim, _close):
        context = cold_context(agent, gate)
        producer = CoordinatorBackgroundRequests(context.control_plane, drain_runtime=context.drain_runtime)
        monkeypatch.setattr(producer, '_owner_sources', lambda _target: ())
        original = discovery.discover_owner_home_page
        monkeypatch.setattr(discovery, 'discover_owner_home_page', lambda root, **kwargs:
            original(root, limit=1, after_cursor=kwargs['after_cursor']))
        clock = [100.0]
        monkeypatch.setattr(discovery, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        index = agent.scheduler_repository.due_index
        for _ in range(16):
            clock[0] += 2
            producer.poll()
            if index.source_discovery_ready():
                break
        assert index.source_discovery_ready()
        assert index.source_owner_page(case.event) == ('feishu', 'user', 'ou_a')
        assert index.source_owner_page(case.event, after=('feishu', 'user', 'ou_a')) == ('feishu', 'user', 'ou_b')


def test_pending_receipt_and_unrelated_run_never_call_full_source_rows(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts.channel_observations import accept_channel_observation
    from agent_py_agent.agent.scheduler import source_owner_index

    monkeypatch.setattr(source_owner_index, 'source_owner_rows', lambda *_a, **_kw:
        (_ for _ in ()).throw(AssertionError('not a recovery or definition write')))
    assert accept_channel_observation(case.base, case.event)['status'] == 'partial'


def test_mid_sweep_projection_replaced_and_preinitialized_does_not_sign_incomplete_round(case, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery
    from agent_py_agent.cli.gateway_loops import _OwnerWakeHints

    lose_projection(case)
    monkeypatch.setattr(discovery, '_owner_fact_kind', lambda _home: 'none')
    hints = _OwnerWakeHints(SimpleNamespace(agent=case.base))
    try:
        first = hints.discovery.page(case.base.home_paths.owners_dir, limit=1, after_cursor=None)
        assert first.next_cursor is not None
        old_epoch = hints.due_index.source_projection_epoch()
        replacement = lose_projection(case)  # 别处先ensure，后半轮只能读到False，不会抛缺表异常。
        assert replacement.source_projection_epoch() != old_epoch
        cursor = first.next_cursor
        for _ in range(16):
            page = hints.discovery.page(case.base.home_paths.owners_dir, limit=1, after_cursor=cursor)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None and not replacement.source_discovery_ready()
        hints.discovery.next_sweep_at = 0
        for _ in range(16):
            page = hints.discovery.page(case.base.home_paths.owners_dir, limit=1, after_cursor=cursor)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert replacement.source_discovery_ready()
        assert replacement.source_owner_page(case.event) == ('feishu', 'user', 'ou_a')
    finally:
        hints.close()
