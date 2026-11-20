"""M64 的本地日期领取与已发布压缩检查点恢复回归。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.compact_checkpoint import (
    load_latest_committed_compact_checkpoint,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    CURATOR_REPLAY_BREAKER_OPEN,
)
from agent_py_agent.tests.test_clfix_curator_daily_finalize_progress import _daily_case
from agent_py_agent.tests.test_compact_scoped_checkpoint import (  # noqa: F401
    _commit_transcript,
    compact_case,
)
from agent_py_agent.tests.test_curator_observation_identity import _service
from agent_py_agent.tests.test_d2_curator_breaker_shrink_quarantine import _ShapeBackend, _thread


def _append_input(store, thread, when, number):
    return store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "channel": "internal",
        "content": "我平时喝绿茶。", "now": when.timestamp(),
        "metadata": {"session_id": "session-test", "request_id": f"request-{number}"},
    })


def test_missed_finalize_is_claimed_across_midnight(tmp_path):
    service, _, _, _, _ = _daily_case(tmp_path, 1)
    service.config = replace(service.config, daily_finalize_hour=23)
    previous = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
    assert service.run_if_due(now=previous).reason == "daily_finalize"

    after_midnight = datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)
    outcome = service.run_if_due(now=after_midnight)

    assert (outcome.status, outcome.reason) == ("succeeded", "daily_finalize")
    assert service.state_store.load().last_daily_finalize_date == "2026-11-19"


def test_catchup_leaves_next_day_input_at_its_cursor(tmp_path):
    store, thread, _ = _thread(tmp_path, 0)
    backend = _ShapeBackend(thread.thread_id, lambda _ids: False)
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, daily_finalize_hour=23)
    service.timezone_name = "UTC"
    previous = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
    assert service.run_if_due(now=previous).status == "succeeded"
    yesterday = _append_input(store, thread, datetime(2026, 11, 19, 22, tzinfo=timezone.utc), 1)
    _append_input(store, thread, datetime(2026, 11, 20, 0, 1, tzinfo=timezone.utc), 2)

    result = service.run_if_due(now=datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc))

    assert (result.status, result.processed_messages) == ("succeeded", 1)
    assert backend.seen == [[yesterday.message_id]]
    state = service.state_store.load()
    assert state.last_daily_finalize_date == "2026-11-19"
    assert state.per_thread_cursors == {thread.thread_id: yesterday.message_id}


def test_first_enable_uses_the_existing_interval_as_a_frozen_window(tmp_path):
    store, thread, _ = _thread(tmp_path, 0)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    _append_input(store, thread, now - timedelta(hours=4), 1)
    recent = _append_input(store, thread, now - timedelta(hours=2), 2)
    backend = _ShapeBackend(thread.thread_id, lambda _ids: False)
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, interval_seconds=10_800, daily_finalize_hour=23)
    service.timezone_name = "UTC"

    result = service.run_if_due(now=now)

    assert (result.status, result.processed_messages) == ("succeeded", 1)
    assert backend.seen == [[recent.message_id]]
    state = service.state_store.load()
    assert state.first_enable_after == (now - timedelta(hours=3)).timestamp()
    assert state.per_thread_cursors == {thread.thread_id: recent.message_id}


def test_breaker_detaches_only_its_date_then_replays_without_rewinding(tmp_path):
    store, thread, _ = _thread(tmp_path, 0)
    yesterday = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    today = yesterday + timedelta(days=1)
    bad = _append_input(store, thread, yesterday - timedelta(minutes=5), 1)
    backend = _ShapeBackend(thread.thread_id, lambda ids: bad.message_id in ids)
    service = _service(tmp_path, backend, store)
    service.timezone_name = "UTC"
    service.config = replace(service.config, daily_finalize_hour=23, interval_seconds=3600)
    for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT):
        result = service.run(reason="interval", now=yesterday, scheduled=True)
        assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    broken = service.state_store.load()
    assert broken.last_failure_code == CURATOR_REPLAY_BREAKER_OPEN
    deferred, = broken.pending_local_dates
    assert deferred["date"] == "2026-11-19"
    assert deferred["source_range"]["messages"] == {thread.thread_id: bad.message_id}
    assert deferred["cursor"]["messages"] == {thread.thread_id: ""}
    assert deferred["failure_type"] == CURATOR_REPLAY_BREAKER_OPEN
    retry = datetime.fromisoformat(deferred["retry_at"])

    good = _append_input(store, thread, today, 2)
    service.request("task_complete")
    recovered = service.run_if_due(now=retry - timedelta(seconds=1))
    assert (recovered.status, recovered.processed_messages) == ("succeeded", 1)
    assert backend.seen[-1] == [good.message_id]

    backend.fails = lambda _ids: False
    restarted = _service(tmp_path, backend, ConversationStore(store.storage.root))
    restarted.timezone_name = "UTC"
    result = restarted.run_if_due(now=retry + timedelta(seconds=1))

    assert (result.status, result.processed_messages) == ("succeeded", 1)
    assert backend.seen[-1] == [bad.message_id]
    settled = restarted.state_store.load()
    assert not settled.pending_local_dates
    assert settled.per_thread_cursors == {thread.thread_id: good.message_id}


def test_restart_after_checkpoint_commit_retains_compact_consumption(compact_case):  # noqa: F811
    case = compact_case
    now = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    case.clock[0] = now.timestamp()
    _, source, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="检查点", content="待提炼来源", publish=True,
    )
    # 原线程 CAS 已完成，后置唤醒尚未执行。
    reopened = ConversationStore(case.store.storage.root, compact_root=case.root / "compact")
    thread = reopened.threads.require(case.thread_id)
    row = load_latest_committed_compact_checkpoint(case.agent, thread)
    consumption_id, = row["pending_consumption_ids"]
    backend = _ShapeBackend(thread.thread_id, lambda _ids: False)
    service = _service(case.root, backend, reopened)
    service.config = replace(service.config, daily_finalize_hour=23)
    service.timezone_name = "UTC"

    first = service.run_if_due(now=now)

    assert (first.status, first.reason, first.processed_messages) == ("succeeded", "pre_compact", 1)
    state = service.state_store.load()
    assert state.compact_consumption_cursors[thread.thread_id] == {"through": 1, "last_id": consumption_id}
    assert state.per_thread_cursors == {thread.thread_id: source.message_id}
    reopened_again = ConversationStore(case.store.storage.root, compact_root=case.root / "compact")
    again = _service(case.root, backend, reopened_again)
    again.config = service.config
    again.timezone_name = "UTC"
    assert again.run_if_due(now=now + timedelta(seconds=1)).status == "not_due"
    assert backend.seen == [[source.message_id]]
    assert again.state_store.load().pending_reason_generations == state.pending_reason_generations


def test_compact_claims_cover_a_bounded_chain_and_a_stale_reader(compact_case):  # noqa: F811
    case = compact_case
    thread, _, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="检查点一", content="来源一", publish=True,
    )
    case.clock[0] += 1
    thread, _, _ = _commit_transcript(
        case, scope=CompactScope(kind="task", task_id="work", created_at=1),
        summary="检查点二", content="来源二", publish=False,
    )
    store = ConversationStore(case.store.storage.root, compact_root=case.root / "compact")
    service = _service(case.root, _ShapeBackend(thread.thread_id, lambda _ids: False), store)
    service.config = replace(service.config, batch_message_limit=1)
    stale = service.state_store.load()

    page = service.claim_compact_checkpoint(thread, state=stale)
    assert page.compact_consumption_cursors[thread.thread_id]["next_generation"] == 1
    assert service.claim_compact_checkpoint(thread, state=stale) == page
    settled = service.claim_compact_checkpoint(thread, state=page)

    assert settled.pending_reasons == ["pre_compact"]
    assert settled.pending_reason_generations == {"pre_compact": 1}
    assert settled.compact_consumption_cursors[thread.thread_id]["through"] == 2
    assert service.claim_compact_checkpoint(thread) == settled
