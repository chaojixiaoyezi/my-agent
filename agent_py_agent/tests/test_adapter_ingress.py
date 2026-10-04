"""LLM: Verify the adapter durable ingress outbox and its single-thread recovery boundary.

测试说明: 覆盖 POST 前落盘、重复/冲突消息、响应丢失、进程崩溃恢复和瞬时 HTTP 错误。
"""

from __future__ import annotations

import stat
import threading
import urllib.error
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("gateway_client_credential")

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
from agent_py_agent.agent.gateway_parts.local_client_token import LocalClientCredentialError
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


# LLM: G2b 拒绝路径：/ask 收到 401/403 是确定性鉴权拒绝，重试不会变好。按 G3 凭据拒绝同一口径收口成
#   credential_error（由 ingress 交给用户一句可见原因），不再落进无上限退避重试。判据只看状态码与结构化 error_code。
# 函数用途: 断言 /ask 提交收到 403 时转成 credential_error 并带结构化原因码。
def test_ask_submission_auth_denial_becomes_credential_error(tmp_path: Path) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    body = b'{"error": "forbidden", "error_code": "LOCAL_CREDENTIAL_REQUIRED"}'
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8420/ask", 403, "forbidden", hdrs=None, fp=BytesIO(body)
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LocalClientCredentialError) as caught:
            manager._submit_gateway_payload({"goal": "x"}, _message())
    assert caught.value.reason_code == "LOCAL_CREDENTIAL_REQUIRED"
    # 必须是带码的专用文案，不能是通用鉴权文案（通用版也含“本机凭据”“重启”，旧断言抓不住读两次响应体的回归）。
    assert "本机凭据无效或缺失" in str(caught.value) and "重启" in str(caught.value)


# LLM: 只有 auth 类拒绝才收口；5xx/429 仍然是暂时性错误，必须继续上抛交给原退避重试，不能被吞成终态。
# 函数用途: 断言 /ask 提交收到 503 时原样上抛 HTTPError。
def test_ask_submission_transient_error_still_raises(tmp_path: Path) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8420/ask", 503, "unavailable", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(urllib.error.HTTPError):
            manager._submit_gateway_payload({"goal": "x"}, _message())


# LLM: 401 同样属于 auth 类；没有 error_code 时退回通用鉴权码，但仍然是终态而不是重试。
# 函数用途: 断言 /ask 提交收到无 error_code 的 401 时也收口成 credential_error。
def test_ask_submission_401_without_code_uses_generic_denial_code(tmp_path: Path) -> None:
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8420/ask", 401, "unauthorized", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LocalClientCredentialError) as caught:
            manager._submit_gateway_payload({"goal": "x"}, _message())
    assert caught.value.reason_code == "GATEWAY_AUTH_DENIED"
    assert "鉴权失败" in str(caught.value) and "本机凭据无效或缺失" not in str(caught.value)


# LLM: g2bfix2b 起 _deliver_available_progress 经 _quarantine_aware 包裹：进度已发出后游标落盘（commit_claim）
#   失败一次，只记 warning 并释放认领，不能让异常逃出 run_once（底版会逃出，生产投递线程直接退出、最终回复不送达）。
#   代价是同一条进度最多重发一次。由 9b 终审探针转正；去掉该捕获面时本用例必须红。
# 函数用途: 断言进度游标落盘失败一次时，最终回复照常送达一次、run_once 不抛、进度最多重复一次。
def test_progress_cursor_commit_failure_once_still_delivers_final(tmp_path: Path) -> None:
    store = GatewayReplyDeliveryStore(tmp_path / "delivery")
    progress_sent: list[str] = []
    final_sent: list[str] = []

    def poll_progress(record):
        return (["进度A"], 1) if record.progress_cursor < 1 else ([], record.progress_cursor)

    worker = GatewayReplyDeliveryWorker(
        store,
        poll_response=lambda _record: "最终回复",
        deliver_response=lambda record, text: final_sent.append(text) is None,
        poll_progress=poll_progress,
        deliver_progress=lambda record, text: progress_sent.append(text) is None,
    )
    original_commit = store.commit_claim
    state = {"failed": False}

    def flaky_commit(expected, replacement, **claim):
        if not claim.get("release", True) and replacement.progress_cursor == 1 and not state["failed"]:
            state["failed"] = True
            raise OSError("cursor persist failed once")
        return original_commit(expected, replacement, **claim)

    store.commit_claim = flaky_commit
    worker.enqueue(PendingGatewayReply(
        request_id="req-commit-once", channel="feishu", user_id="ou-1",
        message_id="om-commit-once", conversation_id="oc-1", progress_handle="typing-1",
    ))
    for _ in range(3):
        worker.run_once()

    assert state["failed"] is True
    assert final_sent == ["最终回复"]
    assert 1 <= len(progress_sent) <= 2 and set(progress_sent) == {"进度A"}
    assert store.pending() == []
