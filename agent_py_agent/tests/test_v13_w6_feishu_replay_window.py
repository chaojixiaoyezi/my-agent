"""只使用合成数据的飞书时间窗与防重放测试。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.adapter import feishu_crypto as crypto
from agent_py_agent.agent.adapter.feishu_replay_guard import (
    FeishuReplayGuard,
)


@pytest.fixture
def now():
    return 1_800_000_000.0


def _body(*, token=None):
    header = {"event_id": "SYNTHETIC_ONLY_EVENT_ID"}
    if token is not None:
        header["token"] = token

    return json.dumps(
        {
            "header": header,
            "event": {
                "message": "SYNTHETIC_ONLY_MESSAGE",
                "url": "https://example.invalid/SYNTHETIC_ONLY_EVENT",
            },
        },
        sort_keys=True,
    ).encode("utf-8")


def _request(
    now,
    *,
    timestamp=None,
    nonce="SYNTHETIC_ONLY_NONCE",
    guard=None,
    tolerance=300,
):
    if timestamp is None:
        timestamp = str(int(now))
    if guard is None:
        guard = FeishuReplayGuard()

    raw_body = _body()
    encrypt_key = "SYNTHETIC_ONLY_ENCRYPT_KEY"
    signature = crypto.compute_signature(
        crypto.FeishuSignParts(
            timestamp,
            nonce,
            encrypt_key,
            raw_body,
        )
    )
    return crypto.FeishuWebhookDecodeRequest(
        raw_body=raw_body,
        encrypt_key=encrypt_key,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        timestamp_tolerance_seconds=tolerance,
        now=now,
        replay_guard=guard,
    )


def _codes(caplog):
    return [
        getattr(record, "diagnostic_code", None)
        for record in caplog.records
    ]


def test_request_appends_defaults_without_breaking_old_positionals():
    request = crypto.FeishuWebhookDecodeRequest(
        b"{}",
        "",
        "",
        "",
        "",
        "",
    )

    assert request.timestamp_tolerance_seconds == 300
    assert request.now is None
    assert request.replay_guard is None


def test_verify_signature_still_accepts_nonnumeric_ts():
    # "ts" 是需求指定的非数字时间戳回归样例；
    # 纯签名函数不承担 webhook 时间窗校验。
    parts = crypto.FeishuSignParts(
        "ts",
        "SYNTHETIC_ONLY_NONCE",
        "SYNTHETIC_ONLY_ENCRYPT_KEY",
        _body(),
    )
    signature = crypto.compute_signature(parts)

    assert crypto.verify_signature(parts, signature)
    assert not crypto.verify_signature(
        parts,
        "SYNTHETIC_ONLY_INVALID_SIGNATURE",
    )


@pytest.mark.parametrize("offset", [-300, -299, 0, 300])
def test_signed_timestamp_closed_window_accepts(now, offset):
    request = _request(
        now,
        timestamp=str(int(now) + offset),
    )

    assert crypto.verify_and_decode_webhook(request) == json.loads(
        request.raw_body
    )


@pytest.mark.parametrize("offset", [-301, 301])
def test_signed_timestamp_outside_window_rejects(
    now,
    offset,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp=str(int(now) + offset),
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)
    assert len(guard) == 0


def test_expired_1609459200_rejects(now, caplog):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp="1609459200",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)
    assert len(guard) == 0


def test_abc_timestamp_rejects_with_structured_diagnostic(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp="abc",
        guard=guard,
    )

    # 签名本身正确；拒绝原因必须来自时间戳解析。
    parts = crypto.FeishuSignParts(
        request.timestamp,
        request.nonce,
        request.encrypt_key,
        request.raw_body,
    )
    assert crypto.verify_signature(parts, request.signature)

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_INVALID" in _codes(caplog)
    assert len(guard) == 0


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf")])
def test_nonfinite_now_fails_closed(now, invalid_now, caplog):
    guard = FeishuReplayGuard()
    request = replace(
        _request(now, guard=guard),
        now=invalid_now,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_INVALID" in _codes(caplog)
    assert len(guard) == 0


def test_same_request_second_time_rejects_without_secret_logs(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(now, guard=guard)

    assert crypto.verify_and_decode_webhook(request) is not None
    assert crypto.verify_and_decode_webhook(request) is None

    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)
    assert len(guard) == 1

    for forbidden in (
        request.signature,
        request.encrypt_key,
        request.nonce,
        request.raw_body.decode("utf-8"),
    ):
        assert forbidden not in caplog.text


def test_different_nonce_is_not_same_replay_key(now):
    guard = FeishuReplayGuard()
    first = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    second = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(first) is not None
    assert crypto.verify_and_decode_webhook(second) is not None
    assert len(guard) == 2


def test_invalid_signature_does_not_consume_slot(now):
    guard = FeishuReplayGuard(max_entries=1)
    valid = _request(now, guard=guard)
    invalid = replace(
        valid,
        signature="SYNTHETIC_ONLY_INVALID_SIGNATURE",
    )

    assert crypto.verify_and_decode_webhook(invalid) is None
    assert len(guard) == 0

    assert crypto.verify_and_decode_webhook(valid) is not None
    assert len(guard) == 1


def test_none_guard_preserves_no_deduplication(now):
    request = replace(_request(now), replay_guard=None)
    assert crypto.verify_and_decode_webhook(request) is not None
    assert crypto.verify_and_decode_webhook(request) is not None


def test_future_timestamp_does_not_expire_at_first_seen_ttl(
    now,
    caplog,
):
    guard = FeishuReplayGuard(window_seconds=300)
    request = _request(
        now,
        timestamp=str(int(now) + 300),
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is not None

    # 已超过首次接收时间 + 300，但 timestamp 仍在容差内。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 301)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    caplog.clear()

    # timestamp + tolerance 边界仍必须拒绝重放。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 600)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    caplog.clear()

    # 再往后由时间窗直接拒绝，而非重新准入。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 601)
    ) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)


def test_guard_expiry_is_inclusive_and_duplicate_does_not_extend_it():
    guard = FeishuReplayGuard(window_seconds=2)

    def claim(observed_now):
        return guard.check_and_record(
            "100",
            "SYNTHETIC_ONLY_NONCE",
            "SYNTHETIC_ONLY_SIGNATURE",
            now=observed_now,
            valid_until=observed_now,
        )

    assert claim(100.0)
    assert not claim(102.0)
    assert claim(102.001)
    assert len(guard) == 1


def test_capacity_is_bounded_and_eviction_is_lru(now):
    guard = FeishuReplayGuard(max_entries=2)

    first = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    second = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    third = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_C",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(first) is not None
    assert crypto.verify_and_decode_webhook(second) is not None

    # 重复命中刷新 A 的 LRU 顺序，但仍拒绝该次调用。
    assert crypto.verify_and_decode_webhook(first) is None
    assert crypto.verify_and_decode_webhook(third) is not None
    assert len(guard) == 2

    # 此时 A 尚在，B 已被容量淘汰。
    assert crypto.verify_and_decode_webhook(first) is None
    assert crypto.verify_and_decode_webhook(second) is not None
    assert len(guard) == 2


def test_concurrent_same_request_has_exactly_one_success(now):
    guard = FeishuReplayGuard()
    request = _request(now, guard=guard)
    barrier = threading.Barrier(16)

    def invoke(_):
        barrier.wait(timeout=10)
        return crypto.verify_and_decode_webhook(request)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(invoke, range(16)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 15
    assert len(guard) == 1


def test_clock_rollback_keeps_live_replay_record(now, caplog):
    guard = FeishuReplayGuard()
    original = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(original) is not None

    # 现存记录不因回拨被清理。
    assert crypto.verify_and_decode_webhook(
        replace(original, now=now - 1)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    # 尚未遗忘到期记录时，不把小幅回拨等同于所有请求重放。
    distinct = _request(
        now - 1,
        timestamp=original.timestamp,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(distinct) is not None


def test_clock_rollback_cannot_resurrect_expired_record(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    original = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(original) is not None

    # 推进时间并触发原记录的到期清理。
    advanced = _request(
        now + 301,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(advanced) is not None

    caplog.clear()

    # 原记录已被清理，但回拨使其重新落入时间容差。
    # 必须 fail closed，不能因缓存中找不到它就重新接受。
    assert crypto.verify_and_decode_webhook(
        replace(original, now=now + 300)
    ) is None
    assert "FEISHU_CLOCK_ROLLBACK" in _codes(caplog)

    # 超过已清理截止线后，可继续接受新的有效请求。
    recovered = _request(
        now + 302,
        nonce="SYNTHETIC_ONLY_NONCE_C",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(recovered) is not None


def test_token_only_does_not_use_signature_window_or_guard():
    token = "SYNTHETIC_ONLY_VERIFICATION_TOKEN"
    raw_body = _body(token=token)
    request = crypto.FeishuWebhookDecodeRequest(
        raw_body=raw_body,
        verification_token=token,
        timestamp="abc",
        timestamp_tolerance_seconds=-1,
        now=float("nan"),
        replay_guard=object(),
    )
    expected = json.loads(raw_body)

    # 此层不新增 token-only 去重，保留上层已有 event_id 准入。
    assert crypto.verify_and_decode_webhook(request) == expected
    assert crypto.verify_and_decode_webhook(request) == expected

    assert crypto.verify_and_decode_webhook(
        replace(
            request,
            verification_token="SYNTHETIC_ONLY_WRONG_TOKEN",
        )
    ) is None


def test_unsigned_request_is_still_rejected(now):
    request = replace(
        _request(now),
        signature="",
    )

    assert crypto.verify_and_decode_webhook(request) is None
