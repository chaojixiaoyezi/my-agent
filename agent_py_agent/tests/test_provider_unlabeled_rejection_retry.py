"""LLM: 供应商"无定位信息 400"的有界重试合同。

真机证据(2026-09-11 四路复刻验收): 上游在持续并发下返回 body 形如
{"object":"error","model":"..."} 的空洞 400，同一 payload 稍后重放即成功；
此前它会直接终结子代理（41 个子代理里 9 个因此死亡）。
规则：只对这一种客观形状做一次有界重试；其它 400 与未知错误保持"不重试"。
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
from agent_py_agent.agent.backends.gateway_helpers import (
    _is_unlabeled_provider_rejection,
    _retry_unlabeled_rejection,
)


def _http_error(status: int, body: object) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError("https://example.test/v1/chat/completions", status, "err", {}, BytesIO(raw))


def test_unlabeled_400_is_recognized():
    assert _is_unlabeled_provider_rejection(_http_error(400, {"object": "error", "model": "m"})) is True


@pytest.mark.parametrize("body", [
    {"error": {"message": "reasoning_content must be passed back", "type": "invalid_request_error"}},
    {"error": {"type": "invalid_request_error", "code": "invalid_request_error"}},
    {"error": {"message": "bad"}},
])
def test_labeled_400_is_not_retried(body):
    assert _is_unlabeled_provider_rejection(_http_error(400, body)) is False


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_other_status_codes_are_never_treated_as_transient(status):
    assert _is_unlabeled_provider_rejection(_http_error(status, {"object": "error"})) is False


def test_unlabeled_400_retries_once_then_succeeds():
    calls = {"n": 0}

    def operation():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(400, {"object": "error", "model": "m"})
        return b'{"ok": true}'

    assert _retry_unlabeled_rejection(operation) == b'{"ok": true}'
    assert calls["n"] == 2


def test_unlabeled_400_is_bounded_and_finally_typed():
    calls = {"n": 0}

    def operation():
        calls["n"] += 1
        raise _http_error(400, {"object": "error", "model": "m"})

    with pytest.raises(ProviderRequestRejectedError):
        _retry_unlabeled_rejection(operation)
    assert calls["n"] == 2, "必须有界：连续空洞 400 只重试一次，随后抛出 typed 错误"


def test_labeled_400_raises_immediately_without_retry():
    calls = {"n": 0}

    def operation():
        calls["n"] += 1
        raise _http_error(400, {"error": {"message": "永久拒绝"}})

    with pytest.raises(ProviderRequestRejectedError):
        _retry_unlabeled_rejection(operation)
    assert calls["n"] == 1, "有定位信息的 400 保持不重试"
