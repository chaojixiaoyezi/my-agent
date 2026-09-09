"""LLM: Verify the adapter durable ingress outbox and its single-thread recovery boundary.

测试说明: 覆盖 POST 前落盘、重复/冲突消息、响应丢失、进程崩溃恢复和瞬时 HTTP 错误。
"""

from __future__ import annotations

import stat
import threading
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressStore,
    build_gateway_ingress_record,
)
from agent_py_agent.agent.adapter.manager import (
    ChannelManager,
    GatewayAskSubmission,
    _gateway_ask_payload,
)
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.tests.test_adapter_manager import DummyAdapter


def _message(*, content: str = "开始长任务") -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        user_id="ou-ingress",
        content=content,
        message_id="om-ingress-1",
        conversation_id="oc-ingress",
        timestamp=123.0,
        metadata={"chat_type": "group", "chat_id": "oc-ingress"},
    )


def _register_dummy(manager: ChannelManager) -> DummyAdapter:
    adapter = DummyAdapter()
    adapter.adapter_name = "feishu"
    manager.register_adapter(adapter)
    return adapter


def _reset_ingress_backoff(manager: ChannelManager) -> None:
    record = manager._delivery_worker.store.pending()[0]
    assert manager._delivery_worker.store.compare_and_swap(
        record,
        replace(record, next_attempt_at=0.0),
    )


def test_route_persists_authenticated_ingress_before_media_or_gateway_io(
    tmp_path: Path,
) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    adapter = _register_dummy(manager)
    adapter.workspace_root = tmp_path / "workspace"
    adapter.fetch_media_to = MagicMock(return_value="image.png")
    message = _message(content="[图片]")
    message.metadata["media"] = {"image_key": "img-1"}

    with patch.object(
        manager,
        "_submit_gateway_payload",
        return_value=GatewayAskSubmission("req-ingress"),
    ) as submit:
        assert manager.route_message(message) is True
        submit.assert_not_called()
        adapter.fetch_media_to.assert_not_called()

        rows = manager._delivery_worker.store.records()
        assert len(rows) == 1
        row = rows[0]
        assert row.state == "prepared"
        assert (
            row.channel,
            row.user_id,
            row.conversation_id,
            row.provider_message_id,
        ) == ("feishu", "ou-ingress", "oc-ingress", "om-ingress-1")
        assert len(row.canonical_payload_digest) == 64
        record_file = next((tmp_path / "delivery" / "ingress" / "records").glob("*.json"))
        assert stat.S_IMODE(record_file.stat().st_mode) == 0o600

        assert manager._delivery_worker.run_once() == 1

    adapter.fetch_media_to.assert_called_once()
    submit.assert_called_once()
    submitted_payload, submitted_message = submit.call_args.args
    assert "已下载到" in submitted_message.content
    completed = manager._delivery_worker.store.records()[0]
    assert completed.state == "completed"
    assert completed.gateway_payload == submitted_payload
    assert submitted_payload == _gateway_ask_payload(submitted_message)


def test_transient_media_failure_stays_prepared_and_retries_before_post(
    tmp_path: Path,
) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    adapter = _register_dummy(manager)
    adapter.workspace_root = tmp_path / "workspace"
    adapter.fetch_media_to = MagicMock(
        side_effect=[OSError("temporary media outage"), "image.png"]
    )
    message = _message(content="[图片]")
    message.metadata["media"] = {"image_key": "img-1"}
    assert manager.route_message(message) is True

    with patch.object(
        manager,
        "_submit_gateway_payload",
        return_value=GatewayAskSubmission("req-media-retry"),
    ) as submit, patch.object(manager, "_poll_gateway_once", return_value=None):
        assert manager._delivery_worker.run_once() == 0
        submit.assert_not_called()
        failed = manager._delivery_worker.store.pending()[0]
        assert failed.state == "prepared"

        _reset_ingress_backoff(manager)
        assert manager._delivery_worker.run_once() == 1

    submit.assert_called_once()
    assert "已下载到" in str(submit.call_args.args[0]["prompt"])


def test_same_provider_message_same_body_is_idempotent_and_changed_body_is_quarantined(
    tmp_path: Path,
) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    _register_dummy(manager)

    assert manager.route_message(_message()) is True
    replay = _message()
    replay.timestamp = 999.0  # 接收时钟不属于 provider 正文，不应破坏幂等。
    assert manager.route_message(replay) is True
    assert len(manager._delivery_worker.store.records()) == 1

    assert manager.route_message(_message(content="被篡改的正文")) is False
    assert len(manager._delivery_worker.store.records()) == 1
    quarantine = manager._delivery_worker.store.quarantine_records()
    assert len(quarantine) == 1
    assert quarantine[0]["reason"] == "same_provider_message_id_different_body"
    assert quarantine[0]["current_payload_digest"] != quarantine[0]["attempted_payload_digest"]


def test_gateway_response_loss_retries_the_exact_persisted_payload_after_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "delivery"
    first = ChannelManager(delivery_state_dir=state_dir)
    _register_dummy(first)
    assert first.route_message(_message()) is True
    submitted_bodies: list[dict[str, object]] = []

    def lose_response(
        payload: dict[str, object],
        _message: IncomingMessage,
    ) -> GatewayAskSubmission:
        submitted_bodies.append(dict(payload))
        raise ConnectionResetError("Gateway accepted POST but response was lost")

    with patch.object(first, "_submit_gateway_payload", side_effect=lose_response):
        assert first._delivery_worker.run_once() == 0

    failed = first._delivery_worker.store.pending()[0]
    assert failed.state == "payload_ready"
    assert failed.gateway_payload == submitted_bodies[0]
    assert first._reply_delivery.store.pending() == []

    second = ChannelManager(delivery_state_dir=state_dir)
    _register_dummy(second)
    _reset_ingress_backoff(second)

    def accept_replay(
        payload: dict[str, object],
        _message: IncomingMessage,
    ) -> GatewayAskSubmission:
        submitted_bodies.append(dict(payload))
        return GatewayAskSubmission("stable-input-1")

    with patch.object(
        second,
        "_submit_gateway_payload",
        side_effect=accept_replay,
    ), patch.object(second, "_poll_gateway_once", return_value=None), patch.object(
        second._reply_delivery, "_poll_progress", return_value=([], 0),
    ):
        assert second._delivery_worker.run_once() == 1

    assert submitted_bodies[0] == submitted_bodies[1]
    assert second._delivery_worker.store.records()[0].state == "completed"
    assert [row.request_id for row in second._reply_delivery.store.pending()] == [
        "stable-input-1"
    ]


def test_adapter_crash_after_gateway_response_resumes_without_reposting(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "delivery"
    first = ChannelManager(delivery_state_dir=state_dir)
    first_adapter = _register_dummy(first)
    assert first.route_message(_message()) is True

    with patch.object(
        first,
        "_submit_gateway_payload",
        return_value=GatewayAskSubmission("req-after-response"),
    ), patch.object(
        first_adapter,
        "send_progress_placeholder",
        side_effect=OSError("adapter process crashed before placeholder receipt"),
    ):
        assert first._delivery_worker.run_once() == 0

    crashed = first._delivery_worker.store.pending()[0]
    assert crashed.state == "submitted"
    assert crashed.submission is not None

    second = ChannelManager(delivery_state_dir=state_dir)
    second_adapter = _register_dummy(second)
    _reset_ingress_backoff(second)
    with patch.object(second, "_submit_gateway_payload") as repost, patch.object(
        second_adapter,
        "send_progress_placeholder",
        return_value="typing-recovered",
    ), patch.object(second, "_poll_gateway_once", return_value=None), patch.object(
        second._reply_delivery, "_poll_progress", return_value=([], 0),
    ):
        assert second._delivery_worker.run_once() == 1

    repost.assert_not_called()
    pending = second._reply_delivery.store.pending()
    assert len(pending) == 1
    assert pending[0].request_id == "req-after-response"
    assert pending[0].progress_handle == "typing-recovered"


def test_ingress_io_callbacks_run_without_holding_the_store_lock() -> None:
    store = GatewayIngressStore(None)
    reply_worker = GatewayReplyDeliveryWorker(
        GatewayReplyDeliveryStore(None),
        poll_response=lambda _pending: None,
        deliver_response=lambda _pending, _text: True,
    )
    callback_states: list[str] = []

    def assert_store_is_unlocked(state: str) -> None:
        observed = threading.Event()

        def read_from_another_thread() -> None:
            store.records()
            observed.set()

        thread = threading.Thread(target=read_from_another_thread)
        thread.start()
        assert observed.wait(0.5), f"store lock leaked into {state} callback"
        thread.join(timeout=0.5)
        callback_states.append(state)

    def prepare(_record):
        assert_store_is_unlocked("media")
        return {"kind": "ask", "prompt": "hello", "metadata": {}}

    def submit(_record):
        assert_store_is_unlocked("post")
        return {"request_id": "req-1", "status": "queued"}

    def advance(_record):
        assert_store_is_unlocked("placeholder")
        return GatewayIngressAdvance("placeholder_ready", "typing")

    def handoff(_record):
        assert_store_is_unlocked("handoff")

    worker = GatewayAdapterDeliveryWorker(
        store,
        reply_worker=reply_worker,
        prepare_payload=prepare,
        submit_payload=submit,
        advance_submission=advance,
        handoff_reply=handoff,
    )
    worker.enqueue(
        build_gateway_ingress_record(
            channel="feishu",
            user_id="ou-1",
            conversation_id="oc-1",
            provider_message_id="om-1",
            content="hello",
            metadata={},
            timestamp=1.0,
        )
    )

    assert worker.run_once() == 1
    assert callback_states == ["media", "post", "placeholder", "handoff"]


def test_reply_poll_and_provider_callbacks_run_without_holding_store_lock() -> None:
    store = GatewayReplyDeliveryStore(None)
    callback_states: list[str] = []

    def assert_store_is_unlocked(state: str) -> None:
        observed = threading.Event()

        def read_from_another_thread() -> None:
            store.pending()
            observed.set()

        thread = threading.Thread(target=read_from_another_thread)
        thread.start()
        assert observed.wait(0.5), f"reply store lock leaked into {state} callback"
        thread.join(timeout=0.5)
        callback_states.append(state)

    def poll(_record: PendingGatewayReply) -> str:
        assert_store_is_unlocked("poll")
        return "done"

    def deliver(_record: PendingGatewayReply, _text: str) -> bool:
        assert_store_is_unlocked("provider")
        return True

    worker = GatewayReplyDeliveryWorker(
        store,
        poll_response=poll,
        deliver_response=deliver,
    )
    worker.enqueue(
        PendingGatewayReply(
            request_id="req-lock",
            channel="feishu",
            user_id="ou-1",
            message_id="om-1",
        )
    )

    assert worker.run_once() == 1
    assert callback_states == ["poll", "provider"]


def test_ingress_claim_takeover_fences_stale_epoch_across_store_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "delivery"
    first_store = GatewayIngressStore(root)
    second_store = GatewayIngressStore(root)
    record = build_gateway_ingress_record(
        channel="feishu",
        user_id="ou-1",
        conversation_id="oc-1",
        provider_message_id="om-claim",
        content="hello",
        metadata={},
        timestamp=1.0,
        now=1.0,
    )
    assert first_store.put_if_absent(record) == (record, True, False)

    first_claim = first_store.claim(record, owner="process-a", now=10.0, ttl=5.0)
    assert first_claim is not None
    assert first_claim.claim_epoch == 1
    observed_by_second = second_store.pending()[0]
    assert second_store.claim(
        observed_by_second,
        owner="process-b",
        now=11.0,
        ttl=5.0,
    ) is None

    second_claim = second_store.claim(
        observed_by_second,
        owner="process-b",
        now=16.0,
        ttl=5.0,
    )
    assert second_claim is not None
    assert second_claim.claim_epoch == 2
    assert first_store.commit_claim(
        first_claim,
        replace(first_claim, state="payload_ready", gateway_payload={"prompt": "stale"}),
        owner="process-a",
        epoch=first_claim.claim_epoch,
    ) is None

    committed = second_store.commit_claim(
        second_claim,
        replace(second_claim, state="payload_ready", gateway_payload={"prompt": "fresh"}),
        owner="process-b",
        epoch=second_claim.claim_epoch,
    )
    assert committed is not None
    assert committed.claim_owner == ""
    assert committed.claim_epoch == 2
    assert committed.gateway_payload == {"prompt": "fresh"}


def test_reply_claim_takeover_fences_stale_epoch_across_store_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "delivery"
    first_store = GatewayReplyDeliveryStore(root)
    second_store = GatewayReplyDeliveryStore(root)
    record = PendingGatewayReply(
        request_id="req-claim",
        channel="feishu",
        user_id="ou-1",
        message_id="om-1",
        conversation_id="oc-1",
        watch_kind="input_receipt",
    )
    assert first_store.put_if_absent(record) == (record, True)

    first_claim = first_store.claim(record, owner="process-a", now=10.0, ttl=5.0)
    assert first_claim is not None
    observed_by_second = second_store.pending()[0]
    assert second_store.claim(
        observed_by_second,
        owner="process-b",
        now=11.0,
        ttl=5.0,
    ) is None

    second_claim = second_store.claim(
        observed_by_second,
        owner="process-b",
        now=16.0,
        ttl=5.0,
    )
    assert second_claim is not None
    assert second_claim.claim_epoch == 2
    assert first_store.commit_claim(
        first_claim,
        replace(first_claim, watch_kind="request_result"),
        owner="process-a",
        epoch=first_claim.claim_epoch,
    ) is None

    committed = second_store.commit_claim(
        second_claim,
        replace(second_claim, watch_kind="request_result"),
        owner="process-b",
        epoch=second_claim.claim_epoch,
    )
    assert committed is not None
    assert committed.claim_owner == ""
    assert committed.claim_epoch == 2
    assert committed.watch_kind == "request_result"


def test_progress_handle_is_mutable_and_not_an_immutable_route_conflict() -> None:
    store = GatewayReplyDeliveryStore(None)
    first = PendingGatewayReply(
        request_id="req-1",
        channel="feishu",
        user_id="ou-1",
        message_id="om-1",
        conversation_id="oc-1",
        progress_handle="typing-old",
    )
    assert store.put_if_absent(first) == (first, True)

    replay, created = store.put_if_absent(
        replace(first, progress_handle="typing-recovered")
    )
    assert (replay, created) == (first, False)
    updated = replace(first, progress_handle="typing-recovered")
    assert store.compare_and_swap(first, updated) is True
    assert store.pending() == [updated]


@pytest.mark.parametrize("status_code", [429, 503])
def test_transient_result_http_error_remains_wait_and_is_not_user_visible(
    status_code: int,
) -> None:
    manager = ChannelManager(gateway_port=8420)
    pending = PendingGatewayReply(
        request_id="req-5xx",
        channel="feishu",
        user_id="ou-1",
        message_id="om-1",
    )
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8420/result/req-5xx",
        status_code,
        "unavailable",
        hdrs=None,
        fp=None,
    )

    with patch("urllib.request.urlopen", side_effect=error):
        assert manager._poll_gateway_once(pending, interval=0.0) is None
