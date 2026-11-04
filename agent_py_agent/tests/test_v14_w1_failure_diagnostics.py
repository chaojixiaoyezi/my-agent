# LLM: 公共诊断只补充无 user_error 的最终通用提示，不改变错误映射和执行语义。
# 模块用途: 覆盖 W1 通用失败诊断、输入展示边界及真实 TUI 终态发表。
from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome


# LLM: 使用真实 runtime 接收 summary，不访问网络或模型，也不补造终态。
# 函数用途: 构造最小调用上下文，取得失败 summary 与真实 runtime。
def _outcome(fields):
    runtime = TuiRuntime("failure-diagnostics")
    ctx = SimpleNamespace(
        job=SimpleNamespace(show_prompt=False),
        cfg=SimpleNamespace(
            tui_runtime=runtime, state_lock=threading.Lock(), last_token_estimate_ref=[0],
        ),
    )
    text, recorded, summary = _gateway_outcome(ctx, {"ok": False, **fields})
    assert text == ""
    assert recorded is False
    assert summary.ok is False
    return summary, runtime


def test_generic_failure_shows_only_public_fields_in_compact_order():
    summary, _ = _outcome({
        "error_code": "UNCLASSIFIED_FAILURE", "request_id": "gwreq-public",
        "duration_seconds": 85.24, "attempts": 2,
        "error": "private provider detail", "diagnostic_ref": "private reference",
        "id": "not-the-public-request-id", "metadata": {"attempts": 9},
    })
    assert summary.error == (
        gateway_client_error_message(None)
        + "（错误码 UNCLASSIFIED_FAILURE · 请求 gwreq-public · 用时 85.2s · 尝试 2 次）"
    )


@pytest.mark.parametrize("fields,suffix", [
    ({"error_code": "UNCLASSIFIED_FAILURE"}, "错误码 UNCLASSIFIED_FAILURE"),
    ({"request_id": "gwreq-only"}, "请求 gwreq-only"),
    ({"request_id": "请求甲"}, "请求 请求甲"),
    ({"duration_seconds": 0}, "用时 0.0s"),
    ({"duration_seconds": 85}, "用时 85.0s"),
    ({"duration_seconds": 85.24}, "用时 85.2s"),
    ({"attempts": 0}, "尝试 0 次"),
    ({"attempts": 2}, "尝试 2 次"),
])
def test_individual_valid_diagnostics(fields, suffix):
    summary, _ = _outcome(fields)
    assert summary.error == gateway_client_error_message(None) + f"（{suffix}）"


@pytest.mark.parametrize("field", ["error_code", "request_id"])
@pytest.mark.parametrize("value", [
    None, "", "   ", False, 42, [], {},
    "line\nbreak", "tab\there", "return\rhere", "nul\x00here", "esc\x1b[31m",
    "delete\x7f", "next\x85line", "direction\u202ereverse",
])
def test_invalid_identifiers_are_not_displayed(field, value):
    summary, _ = _outcome({field: value})
    assert summary.error == gateway_client_error_message(None)


@pytest.mark.parametrize("field", ["error_code", "request_id"])
def test_long_identifiers_do_not_expand_the_diagnostic_without_bound(field):
    summary, _ = _outcome({field: "X" * 1000})
    assert summary.error.startswith(gateway_client_error_message(None))
    assert summary.error.isprintable()
    # 长字段可以截断或丢弃；不把某个具体截断阈值冻结为运行协议。
    assert len(summary.error) < 300


@pytest.mark.parametrize("value", [
    None, "85.2", "bad", "", True, False, [], {}, -1, float("nan"),
    float("inf"), float("-inf"), pytest.param(10**5000, id="oversized-int"),
])
def test_invalid_durations_do_not_break_the_error_path(value):
    summary, _ = _outcome({"duration_seconds": value})
    assert summary.error == gateway_client_error_message(None)


@pytest.mark.parametrize("value", [
    None, "2", "bad", "", True, False, [], {}, -1, 2.0, 2.5,
    float("nan"), float("inf"), pytest.param(10**5000, id="oversized-int"),
])
def test_invalid_attempt_counts_do_not_break_the_error_path(value):
    summary, _ = _outcome({"attempts": value})
    assert summary.error == gateway_client_error_message(None)


def test_large_finite_duration_still_has_bounded_display():
    summary, _ = _outcome({"duration_seconds": 1e300})
    assert summary.error.startswith(gateway_client_error_message(None))
    assert summary.error.isprintable()
    assert len(summary.error) < 300


def test_invalid_fields_do_not_hide_an_independent_valid_diagnostic():
    summary, _ = _outcome({
        "error_code": "bad\ncode", "request_id": "gwreq-valid",
        "duration_seconds": float("nan"), "attempts": True, "error": "private detail",
    })
    assert summary.error == gateway_client_error_message(None) + "（请求 gwreq-valid）"


@pytest.mark.parametrize("user_error", [
    "模型服务拒绝了当前配置。", gateway_client_error_message(None), " 原文保持空格。 ",
])
def test_user_error_is_byte_identical_even_when_it_equals_the_generic_text(user_error):
    summary, _ = _outcome({
        "user_error": user_error, "error_code": "UNCLASSIFIED_FAILURE",
        "request_id": "gwreq-public", "duration_seconds": 85.2, "attempts": 2,
    })
    assert summary.error.encode("utf-8") == user_error.encode("utf-8")


@pytest.mark.parametrize("code", [
    "PROVIDER_REQUEST_REJECTED", "PROVIDERTIMEOUTERROR", "COMPACT_UNKNOWN",
    "MODEL_UNKNOWN", "MODEL_NOT_CONFIGURED",
])
def test_specific_code_mapping_is_byte_identical(code):
    summary, _ = _outcome({
        "error_code": code, "request_id": "gwreq-public", "duration_seconds": 85.2,
        "attempts": 2, "error": "private detail",
    })
    assert summary.error.encode("utf-8") == gateway_client_error_message(code).encode("utf-8")


def test_no_public_diagnostics_keeps_exact_generic_text_and_never_uses_raw_error():
    summary, _ = _outcome({
        "error": "private detail", "id": "private-id", "diagnostic_ref": "private-ref",
        "metadata": {"error_code": "nested-code"},
    })
    assert summary.error == gateway_client_error_message(None)


def test_diagnostic_summary_reaches_the_real_runtime_failure_block():
    summary, runtime = _outcome({"request_id": "gwreq-visible", "error": "private detail"})
    runtime.begin_turn("failure-turn").finalize(summary)
    snapshot = runtime.store.snapshot()
    assert not snapshot.has_active_work
    assert any("请求 gwreq-visible" in block.text for block in snapshot.stable_blocks)
    assert not any("private detail" in block.text for block in snapshot.stable_blocks)


def test_gateway_outcome_signature_is_preserved():
    signature = inspect.signature(_gateway_outcome)
    assert str(signature) == "(ctx: 'Any', response: 'dict[str, Any]') -> 'tuple[str, bool, TuiTurnSummary]'"
