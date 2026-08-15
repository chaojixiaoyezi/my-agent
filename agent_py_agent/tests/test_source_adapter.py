from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import harvester as hv
from agent.ingestion import source_record_index as record_index
from agent.ingestion.puller import DrainBudget
from agent.ingestion.source_adapter import (
    AdapterPage,
    SourceAdapterError,
    accept_source_response,
    normalize_source_adapter,
    plan_source_request,
)
from agent.ingestion.source_binding import (
    audit_source_config_version,
    audit_source_runtime_projection,
    normalize_audit_source_bindings,
)
from agent.ingestion.source_http import SourceHttpRequest
from agent.ingestion.source_record_index import unseen_source_record_keys
from agent.ingestion.sources import drain_adapter_source
from agent.ingestion.watch_state import load_state, new_state, persist_state
from agent.ingestion.watch_tool import _action_parameter_error, _apply_audit_guarantee


def _digest() -> str:
    return "a" * 64


def test_source_adapter_reference_is_relative_and_hash_pinned() -> None:
    assert normalize_source_adapter(
        {"path": "work/sources/waf/adapter.py", "sha256": _digest()}
    ) == {"path": "work/sources/waf/adapter.py", "sha256": _digest()}
    with pytest.raises(SourceAdapterError, match="安全相对路径"):
        normalize_source_adapter({"path": "../escape.py", "sha256": _digest()})
    with pytest.raises(SourceAdapterError, match="sha256"):
        normalize_source_adapter({"path": "adapter.py", "sha256": "bad"})


def test_watch_open_action_accepts_the_declared_source_adapter_field() -> None:
    """Keep the runtime discriminator aligned with the public open schema."""

    adapter = {"path": "work/sources/dynamic/adapter.py", "sha256": _digest()}
    assert (
        _action_parameter_error(
            "open",
            {
                "action": "open",
                "url": "https://logs.example.test/search",
                "mode": "adapter",
                "source_adapter": adapter,
            },
        )
        == ""
    )
    assert "source_adapter" in _action_parameter_error(
        "pull",
        {
            "action": "pull",
            "watch_id": "ws-0123456789",
            "source_adapter": adapter,
        },
    )


def test_published_adapter_binding_preserves_one_source_specific_script() -> None:
    binding = dict(
        normalize_audit_source_bindings(
            [
                {
                    "source_id": "dynamic-api-1",
                    "source_profile_ref": "work/sources/dynamic-api-1/profile.md",
                    "url": "https://logs.example.test/search?tenant=alpha",
                    "mode": "adapter",
                    "http_request": {"method": "POST", "json_body": {}},
                    "source_adapter": {
                        "path": "work/sources/dynamic-api-1/adapter.py",
                        "sha256": _digest(),
                    },
                }
            ]
        )[0]
    )

    assert binding["mode"] == "adapter"
    assert binding["http_request"] == {"method": "POST", "json_body": {}}
    assert binding["source_adapter"]["path"].endswith("adapter.py")

    with pytest.raises(ValueError, match="只适用于 mode=adapter"):
        normalize_audit_source_bindings(
            [
                {
                    "source_id": "wrong-mode",
                    "source_profile_ref": "profile.md",
                    "url": "https://logs.example.test/feed?cursor=<next>",
                    "mode": "cursor",
                    "source_adapter": {"path": "adapter.py", "sha256": _digest()},
                }
            ]
        )


def test_adapter_runtime_projection_keeps_only_proved_transport_facts() -> None:
    binding = dict(
        normalize_audit_source_bindings(
            [
                {
                    "source_id": "dynamic-api-1",
                    "source_profile_ref": "work/sources/dynamic-api-1/profile.md",
                    "url": "https://logs.example.test/search",
                    "mode": "adapter",
                    "http_request": {"method": "POST", "json_body": {}},
                    "source_adapter": {
                        "path": "work/sources/dynamic-api-1/adapter.py",
                        "sha256": _digest(),
                    },
                }
            ]
        )[0]
    )
    runtime = audit_source_runtime_projection(binding)

    assert runtime["source_envelope"]["valid"] is True
    assert runtime["source_envelope"]["continuation_verified"] is True
    assert runtime["source_envelope"]["record_boundary"] == "adapter_records"
    transient_probe_envelope = {
        **runtime["source_envelope"],
        "observed_record_count": 17,
        "observed_at": 123.0,
    }
    assert audit_source_config_version(
        source_url=runtime["source_url"],
        source_mode=runtime["source_mode"],
        source_envelope=runtime["source_envelope"],
        poll_query_seconds=0,
    ) == audit_source_config_version(
        source_url=runtime["source_url"],
        source_mode=runtime["source_mode"],
        source_envelope=transient_probe_envelope,
        poll_query_seconds=0,
    )


def test_audit_guarantee_keeps_the_pinned_source_adapter() -> None:
    from agent.common.audit_activation import AUDIT_ATTR

    state = new_state(
        Path("/tmp/owner"),
        "https://logs.example.test/search",
        {},
    )
    state.source_envelope = {
        "mode": "adapter",
        "request": {"method": "POST", "url": "https://logs.example.test/search"},
        "adapter": {
            "path": "work/sources/dynamic-api-1/adapter.py",
            "sha256": _digest(),
        },
        "valid": True,
    }
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(task_attributes={AUDIT_ATTR: True})
    )

    _apply_audit_guarantee(SimpleNamespace(agent=agent), state, {})

    assert state.source_envelope["adapter"] == {
        "path": "work/sources/dynamic-api-1/adapter.py",
        "sha256": _digest(),
    }


def test_plan_can_vary_time_range_page_offset_and_token_without_changing_origin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checkpoint = {
        "start": "2026-08-04T00:00:00Z",
        "end": "2026-08-04T00:01:00Z",
        "page": 4,
        "offset": 300,
        "range": [301, 400],
        "token": "opaque-next",
    }
    seen = {}

    def fake_invoke(_owner, _audit, _adapter, payload):
        seen.update(payload)
        return {
            "request": {
                "url": (
                    "https://logs.example.test/search?"
                    "start=2026-08-04T00%3A00%3A00Z&end=2026-08-04T00%3A01%3A00Z"
                    "&page=4&offset=300&from=301&to=400&token=opaque-next"
                ),
                "headers": {"X-Tenant": "alpha"},
                "json_body": {"limit": 80},
            },
            "context": {"requested_range": [301, 400]},
        }

    monkeypatch.setattr("agent.ingestion.source_adapter._invoke", fake_invoke)
    plan = plan_source_request(
        owner_home=tmp_path,
        audit_id="audit-1",
        source_url="https://logs.example.test/search",
        request_facts={"method": "POST"},
        adapter={"path": "adapter.py", "sha256": _digest()},
        checkpoint=checkpoint,
        page_limit=80,
        now_unix=1_785_798_400.0,
    )

    assert seen["checkpoint"] == checkpoint
    assert seen["page_limit"] == 80
    assert plan.request.url.startswith("https://logs.example.test/search?")
    assert plan.request.method == "POST"
    assert plan.context == {"requested_range": [301, 400]}


def test_plan_cannot_move_request_or_secrets_to_another_origin(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent.ingestion.source_adapter._invoke",
        lambda *_args, **_kwargs: {
            "request": {"url": "https://evil.example/collect"},
            "context": {},
        },
    )
    with pytest.raises(SourceAdapterError, match="origin"):
        plan_source_request(
            owner_home=tmp_path,
            audit_id="audit-1",
            source_url="https://logs.example.test/search",
            request_facts={"method": "GET"},
            adapter={"path": "adapter.py", "sha256": _digest()},
            checkpoint={},
            page_limit=10,
            now_unix=1.0,
        )


def test_accept_requires_progress_only_for_immediate_continuation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    outputs = iter(
        [
            {"records": [], "checkpoint": {"page": 2}, "has_more": True},
            {
                "records": [{"id": 2}],
                "checkpoint": {"page": 2},
                "has_more": False,
                "record_keys": ["position-2"],
            },
            {
                "records": [{"id": 2}],
                "checkpoint": {"page": 2},
                "has_more": True,
                "record_keys": ["position-2"],
            },
        ]
    )
    monkeypatch.setattr(
        "agent.ingestion.source_adapter._invoke",
        lambda *_args, **_kwargs: next(outputs),
    )
    page = accept_source_response(
        owner_home=tmp_path,
        audit_id="audit-1",
        adapter={"path": "adapter.py", "sha256": _digest()},
        checkpoint={"page": 1},
        context={},
        response={"rows": []},
        max_records=10,
    )
    assert page.records == []
    assert page.checkpoint == {"page": 2}
    assert page.has_more is True

    terminal_overlap = accept_source_response(
        owner_home=tmp_path,
        audit_id="audit-1",
        adapter={"path": "adapter.py", "sha256": _digest()},
        checkpoint={"page": 2},
        context={},
        response={"rows": [{"id": 2}]},
        max_records=10,
    )
    assert terminal_overlap.records == [{"id": 2}]
    assert terminal_overlap.checkpoint == {"page": 2}
    assert terminal_overlap.has_more is False
    assert terminal_overlap.record_keys == ("position-2",)

    with pytest.raises(SourceAdapterError, match="推进 checkpoint"):
        accept_source_response(
            owner_home=tmp_path,
            audit_id="audit-1",
            adapter={"path": "adapter.py", "sha256": _digest()},
            checkpoint={"page": 2},
            context={},
            response={"rows": [{"id": 2}]},
            max_records=10,
        )


def test_accept_rejects_more_complete_records_than_host_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agent.ingestion.source_adapter._invoke",
        lambda *_args, **_kwargs: {
            "records": [{"id": 1}, {"id": 2}],
            "checkpoint": {"page": 2},
            "has_more": True,
        },
    )

    with pytest.raises(SourceAdapterError) as error:
        accept_source_response(
            owner_home=tmp_path,
            audit_id="audit-1",
            adapter={"path": "adapter.py", "sha256": _digest()},
            checkpoint={"page": 1},
            context={},
            response={"rows": [{"id": 1}, {"id": 2}]},
            max_records=1,
        )

    assert error.value.code == "SOURCE_PAGE_TOO_LARGE"


def test_accept_requires_source_position_key_for_every_nonempty_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agent.ingestion.source_adapter._invoke",
        lambda *_args, **_kwargs: {
            "records": [{"same": "content"}],
            "checkpoint": {"page": 2},
            "has_more": False,
        },
    )

    with pytest.raises(SourceAdapterError) as error:
        accept_source_response(
            owner_home=tmp_path,
            audit_id="audit-a",
            adapter={"path": "adapter.py", "sha256": _digest()},
            checkpoint={"page": 1},
            context={},
            response={"rows": []},
            max_records=10,
        )

    assert error.value.code == "SOURCE_RECORD_KEYS_REQUIRED"


def test_adapter_drain_keeps_local_sequence_separate_from_opaque_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pages = iter(
        [
            AdapterPage([], {"window": 2, "token": "b"}, True),
            AdapterPage(
                [{"event": "x"}, {"event": "y"}],
                {"window": 3},
                False,
                ("window-3-row-1", "window-3-row-2"),
            ),
        ]
    )
    monkeypatch.setattr(
        "agent.ingestion.source_adapter.plan_source_request",
        lambda **_kwargs: SimpleNamespace(
            request=SourceHttpRequest("https://logs.example.test/feed", "GET", {}, None),
            context={},
        ),
    )
    monkeypatch.setattr(
        "agent.ingestion.source_adapter.accept_source_response",
        lambda **_kwargs: next(pages),
    )
    state = SimpleNamespace(
        cursor=40,
        source_checkpoint={"window": 1, "token": "a"},
        source_envelope={
            "request": {"method": "GET"},
            "adapter": {"path": "adapter.py", "sha256": _digest()},
        },
        source_url="https://logs.example.test/feed",
        owner_home=tmp_path,
        audit_root_task_id="audit-1",
        prepare_root_task_id="",
    )

    result = drain_adapter_source(
        state,
        lambda _request: (True, {"rows": []}, ""),
        DrainBudget(max_events=10, page_limit=5, deadline=10**20),
    )

    assert result.events == [(40, {"event": "x"}), (41, {"event": "y"})]
    assert result.cursor == 42
    assert result.source_checkpoint == {"window": 3}
    assert sorted(result.source_record_keys) == [40, 41]
    assert list(result.source_record_keys.values()) == [
        "source-position:window-3-row-1",
        "source-position:window-3-row-2",
    ]
    assert result.pages == 2
    assert result.reached_end is True


def test_adapter_drain_accepts_idle_then_bursty_source_with_arbitrary_progression(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checkpoints_seen: list[dict[str, object]] = []
    pages = iter(
        [
            AdapterPage([], {"time": [0, 60], "range": [1, 100], "token": "a"}, False),
            AdapterPage(
                [{"id": "burst-1"}, {"id": "burst-2"}],
                {"time": [60, 75], "range": [101, 200], "token": "b"},
                True,
                ("range-101", "range-102"),
            ),
            AdapterPage(
                [{"id": "burst-3"}],
                {"time": [75, 180], "range": [201, 300], "token": "c"},
                False,
                ("range-201",),
            ),
            AdapterPage([], {"time": [75, 180], "range": [201, 300], "token": "c"}, False),
        ]
    )

    def fake_plan(**kwargs):
        checkpoints_seen.append(dict(kwargs["checkpoint"]))
        return SimpleNamespace(
            request=SourceHttpRequest("https://logs.example.test/feed", "GET", {}, None),
            context={"planned_at": kwargs["now_unix"]},
        )

    monkeypatch.setattr("agent.ingestion.source_adapter.plan_source_request", fake_plan)
    monkeypatch.setattr(
        "agent.ingestion.source_adapter.accept_source_response",
        lambda **_kwargs: next(pages),
    )
    state = SimpleNamespace(
        cursor=0,
        source_checkpoint={"time": [0, 60], "range": [1, 100], "token": "a"},
        source_envelope={
            "request": {"method": "GET"},
            "adapter": {"path": "adapter.py", "sha256": _digest()},
        },
        source_url="https://logs.example.test/feed",
        owner_home=tmp_path,
        audit_root_task_id="audit-open-world",
        prepare_root_task_id="",
    )
    budget = DrainBudget(max_events=10, page_limit=5, deadline=10**20)

    idle = drain_adapter_source(state, lambda _request: (True, {}, ""), budget)
    assert idle.events == []
    assert idle.cursor == 0
    assert idle.source_checkpoint == state.source_checkpoint

    state.cursor = idle.cursor
    state.source_checkpoint = idle.source_checkpoint
    burst = drain_adapter_source(state, lambda _request: (True, {}, ""), budget)
    assert [event for _offset, event in burst.events] == [
        {"id": "burst-1"},
        {"id": "burst-2"},
        {"id": "burst-3"},
    ]
    assert burst.cursor == 3
    assert burst.source_checkpoint == {
        "time": [75, 180],
        "range": [201, 300],
        "token": "c",
    }

    state.cursor = burst.cursor
    state.source_checkpoint = burst.source_checkpoint
    idle_again = drain_adapter_source(state, lambda _request: (True, {}, ""), budget)
    assert idle_again.events == []
    assert idle_again.cursor == 3
    assert idle_again.source_checkpoint == burst.source_checkpoint
    assert checkpoints_seen == [
        {"time": [0, 60], "range": [1, 100], "token": "a"},
        {"time": [0, 60], "range": [1, 100], "token": "a"},
        {"time": [60, 75], "range": [101, 200], "token": "b"},
        {"time": [75, 180], "range": [201, 300], "token": "c"},
    ]


def test_opaque_checkpoint_shares_the_harvest_commit_and_rollback_boundary(tmp_path: Path) -> None:
    state = new_state(tmp_path, "https://logs.example.test/feed", {})
    state.source_checkpoint = {"token": "committed-a", "range": [1, 100]}
    persist_state(state)

    transaction = hv._begin_harvest_transaction(state)
    state.source_checkpoint = {"token": "uncommitted-b", "range": [101, 200]}
    state.cursor = 10
    assert hv._recover_pending_harvest(
        state,
        expected_transaction_id=str(transaction["transaction_id"]),
    )
    assert state.source_checkpoint == {"token": "committed-a", "range": [1, 100]}
    assert state.cursor == 0

    transaction = hv._begin_harvest_transaction(state)
    state.source_checkpoint = {"token": "committed-b", "range": [101, 200]}
    state.cursor = 10
    hv._commit_harvest_transaction(state, transaction)
    restored = load_state(tmp_path, state.watch_id)
    assert restored is not None
    assert restored.source_checkpoint == {"token": "committed-b", "range": [101, 200]}
    assert restored.cursor == 10


def test_adapter_record_keys_deduplicate_overlap_across_cycles_and_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pages = iter(
        [
            AdapterPage(
                [{"payload": "identical"}],
                {"overlap_start": 100},
                False,
                ("one",),
            ),
            AdapterPage(
                [{"payload": "identical"}, {"payload": "identical"}],
                {"overlap_start": 100},
                False,
                ("one", "two"),
            ),
            AdapterPage(
                [
                    {"payload": "identical"},
                    {"payload": "identical"},
                    {"payload": "identical"},
                ],
                {"overlap_start": 100},
                False,
                ("one", "two", "three"),
            ),
        ]
    )
    monkeypatch.setattr(
        "agent.ingestion.source_adapter.plan_source_request",
        lambda **_kwargs: SimpleNamespace(
            request=SourceHttpRequest("https://logs.example.test/feed", "GET", {}, None),
            context={},
        ),
    )
    monkeypatch.setattr(
        "agent.ingestion.source_adapter.accept_source_response",
        lambda **_kwargs: next(pages),
    )
    state = new_state(tmp_path, "https://logs.example.test/feed", {})
    state.source_mode = "adapter"
    state.source_envelope = {
        "mode": "adapter",
        "record_boundary": "adapter_records",
        "request": {"method": "GET"},
        "adapter": {"path": "adapter.py", "sha256": _digest()},
        "valid": True,
        "continuation_verified": True,
    }
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-durable-dedupe"
    persist_state(state)

    assert hv._harvest_cycle(state, lambda _request: (True, {}, "")) is True
    assert hv._harvest_cycle(state, lambda _request: (True, {}, "")) is True
    assert state.cursor == 2
    assert state.spool_seq == 2
    assert state.totals["source_duplicates"] == 1
    assert state.source_checkpoint == {"overlap_start": 100}
    assert unseen_source_record_keys(
        state,
        ["source-position:one", "source-position:two", "source-position:three"],
    ) == [False, False, True]

    restored = load_state(tmp_path, state.watch_id)
    assert restored is not None
    assert hv._harvest_cycle(restored, lambda _request: (True, {}, "")) is True
    assert restored.cursor == 3
    assert restored.spool_seq == 3
    assert restored.totals["source_duplicates"] == 3
    assert restored.source_checkpoint == {"overlap_start": 100}
    rows = [
        json.loads(line)
        for line in hv.spool_path(restored).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["candidates"][0]["event"] for row in rows] == [
        {"payload": "identical"},
        {"payload": "identical"},
        {"payload": "identical"},
    ]
    assert [row["candidates"][0]["source_record_key"] for row in rows] == [
        "source-position:one",
        "source-position:two",
        "source-position:three",
    ]


def test_committed_harvest_recovers_record_key_index_before_next_pull(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = new_state(tmp_path, "https://logs.example.test/feed", {})
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-record-key-recovery"
    persist_state(state)
    transaction = hv._begin_harvest_transaction(state)
    transaction_id = str(transaction["transaction_id"])
    hv._stage_harvest_source_record_keys(
        state,
        transaction,
        ["source-position:one"],
    )
    hv.spool_path(state).write_text('{"spool_seq":1}\n', encoding="utf-8")
    state.cursor = 1
    state.spool_seq = 1

    def _index_unavailable(*_args, **_kwargs):
        raise record_index.SourceRecordIndexError("index unavailable")

    original_commit = record_index.commit_source_record_keys
    monkeypatch.setattr(record_index, "commit_source_record_keys", _index_unavailable)
    with pytest.raises(record_index.SourceRecordIndexError, match="index unavailable"):
        hv._commit_harvest_transaction(state, transaction)

    assert state.harvest_commit_id == transaction_id
    assert hv._harvest_transaction_path(state).exists()
    assert hv.spool_path(state).read_text(encoding="utf-8") == '{"spool_seq":1}\n'

    monkeypatch.setattr(record_index, "commit_source_record_keys", original_commit)
    assert hv._recover_pending_harvest(
        state,
        expected_transaction_id=transaction_id,
    )
    assert not hv._harvest_transaction_path(state).exists()
    assert unseen_source_record_keys(state, ["source-position:one"]) == [False]
    assert hv.spool_path(state).read_text(encoding="utf-8") == '{"spool_seq":1}\n'
