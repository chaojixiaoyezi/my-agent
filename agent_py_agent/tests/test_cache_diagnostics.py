import copy
import json

import pytest

from agent_py_agent.agent.backends.cache_diagnostics import (
    compare_request_surfaces,
    public_cache_diagnostic,
    request_surface,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)


def test_diagnostics_do_not_modify_payload_or_keep_plaintext():
    payload = {"model": "sample", "system": "private system", "messages": [{"role": "user", "content": "private user"}],
               "tools": [{"name": "tool_a"}], "secret_extension": "secret-value"}
    original = copy.deepcopy(payload)
    surface = request_surface(payload, "https://private.example/v1")
    assert payload == original
    assert "private" not in json.dumps(surface)
    assert "secret-value" not in json.dumps(surface)
    assert compare_request_surfaces({}, surface)["baseline_available"] is False


@pytest.mark.parametrize("count", [1, 32])
def test_append_and_compact_are_different_diagnostics(count):
    payload = {"model": "a", "messages": [{"role": "user", "content": f"m{i}"} for i in range(count)]}
    first = request_surface(payload, "endpoint")
    payload["messages"].append({"role": "assistant", "content": "two"})
    second = request_surface(payload, "endpoint")
    # 原 1→2 场景保留并严格核不可比；32→33 有共同末点，才证明纯追加。
    appended = compare_request_surfaces(first, second)
    assert appended["changes"] == (["history_appended"] if count == 32 else [])
    assert appended["changed_block_index"] is None
    assert appended["comparable"] is (count == 32)
    assert appended["partial"] is (count != 32)
    shortened = compare_request_surfaces(second, first)
    assert shortened["changes"] == (["history_shortened"] if count == 32 else [])
    assert shortened["comparable"] is (count == 32)
    assert shortened["partial"] is (count != 32)
    payload["messages"][0]["content"] = "new summary"
    assert "history_prefix_changed" in compare_request_surfaces(second, request_surface(payload, "endpoint"))["changes"]
    payload["tools"] = [{"name": "different"}]
    assert "tools_changed" in compare_request_surfaces(second, request_surface(payload, "endpoint"))["changes"]


def test_large_history_is_explicitly_partial():
    surface = request_surface({"messages": [{"content": str(i)} for i in range(600)]}, "endpoint")
    # 600 条 < 覆盖上限（128 块 × 32 条）时不再 partial，而且检查点足以覆盖整段历史。
    assert surface["schema"] == "request_surface.v2" and surface["partial"] is False
    assert [row["index"] for row in surface["chain_checkpoints"]][-1] == 600
    result = compare_request_surfaces(surface, surface)
    assert result["server_cache_state"] == "unknown"
    # 超过覆盖上限（4097 条）才 partial：前面被裁掉的窗口看不到。
    huge = request_surface({"messages": [{"content": str(i)} for i in range(4097)]}, "endpoint")
    assert huge["partial"] is True


def test_ledger_does_not_compare_different_threads():
    ledger = ModelCallLedger()
    surface = request_surface({"model": "a", "messages": []}, "endpoint")
    for call, thread in [("a", "thread-a"), ("b", "thread-b"), ("c", "thread-a")]:
        ledger.started(ModelCallStartedParams(call, "test", "a", 0, run_id=call, metadata={"thread_id": thread}))
        ledger.provider_attempt(ModelCallProviderAttemptParams(call, call, "started", request_surface=surface))
    rows = ledger.records()
    assert rows[1].metadata["cache_diagnostic"]["baseline_available"] is False
    assert rows[2].metadata["cache_diagnostic"]["changes"] == []
    assert rows[2].metadata["cache_diagnostic"]["server_cache_state"] == "unknown"


def test_public_diagnostic_never_persists_raw_values_or_server_guesses():
    from agent_py_agent.agent.conversation.model_metrics import public_model_metrics

    value = {"baseline_available": True, "changes": ["private prompt", "tools_changed", {}],
             "shared_message_prefix": 9999, "server_cache_state": "hit", "private": "secret"}
    result = public_model_metrics({"schema": "model_runtime_metrics.v1", "cache_diagnostic": value})
    assert result["cache_diagnostic"] == public_cache_diagnostic(value)
    assert result["cache_diagnostic"]["changes"] == ["tools_changed"]
    # 新实现不再把共享前缀截到 512：按检查点块给出真实条数，缺定位信息时给 None。
    assert result["cache_diagnostic"]["shared_message_prefix"] == 9999
    assert result["cache_diagnostic"]["changed_block_index"] is None
    assert result["cache_diagnostic"]["server_cache_state"] == "unknown"
    assert "private" not in json.dumps(result)
    assert "cache_diagnostic" not in public_model_metrics({"schema": "model_runtime_metrics.v1"})


# ---- 0.3：链式摘要去盲区、分区选项单报、累计计数 ----


# 函数用途: 造一个长度可控的合成消息列表（每条内容唯一，便于定位改写点）。
def _messages(count: int) -> dict:
    return {"model": "a", "messages": [{"role": "user", "content": f"m{i}"} for i in range(count)]}


def test_rewrite_far_past_the_old_512_limit_is_reported_with_its_block():
    # 旧实现只散列前 512 条，第 600 条被改写完全看不见；链式检查点必须能报出来并给出块号。
    before = request_surface(_messages(600), "endpoint")
    payload = _messages(600)
    payload["messages"][599]["content"] = "rewritten"
    after = request_surface(payload, "endpoint")
    result = compare_request_surfaces(before, after)
    assert result["changes"] == ["history_prefix_changed"]
    # 最后一条消息本身被改写：末链值不同，而最后一个检查点就是第 600 条，所以块号落在 600。
    assert result["changed_block_index"] == 600
    # 共享前缀只能报到上一个相同检查点（576）为止：检查点不同只能定位到块，
    # 不能证明块内更早的消息也相同（真实共享到 599，但只能证明 576）。
    assert result["shared_message_prefix"] == 576
    # 只改第 10 条，块号落在第一个不同的检查点 32。
    payload = _messages(600)
    payload["messages"][10]["content"] = "also-rewritten"
    assert compare_request_surfaces(before, request_surface(payload, "endpoint"))["changed_block_index"] == 32


def test_thinking_and_reasoning_effort_changes_get_their_own_codes():
    base = {"model": "a", "messages": [{"role": "user", "content": "x"}]}
    thinking = request_surface({**base, "thinking": {"type": "disabled"}}, "endpoint")
    enabled = request_surface({**base, "thinking": {"type": "enabled"}}, "endpoint")
    result = compare_request_surfaces(enabled, thinking)
    # 这两个选项各自切换服务端缓存分区（实测：换一个就整段不命中），所以单独报，不混进 options_changed。
    assert result["changes"] == ["thinking_changed"]
    low = request_surface({**base, "reasoning_effort": "low"}, "endpoint")
    high = request_surface({**base, "reasoning_effort": "high"}, "endpoint")
    assert compare_request_surfaces(high, low)["changes"] == ["reasoning_effort_changed"]
    assert compare_request_surfaces(request_surface(base, "endpoint"), low)["changes"] == ["reasoning_effort_changed"]
    # 其它选项仍只报 options_changed；分区选项没变时不重复报两条。
    warm = request_surface({**base, "temperature": 0.2}, "endpoint")
    hot = request_surface({**base, "temperature": 0.9}, "endpoint")
    assert compare_request_surfaces(warm, hot)["changes"] == ["options_changed"]
    both = request_surface({**base, "temperature": 0.9, "reasoning_effort": "low"}, "endpoint")
    assert compare_request_surfaces(warm, both)["changes"] == ["reasoning_effort_changed"]


def test_change_counts_accumulate_across_calls_and_never_decrease():
    from agent_py_agent.agent.conversation.model_metrics import (
        _add_cache_change_counts,
        public_model_metrics,
    )

    counts = _add_cache_change_counts(None, {"changes": ["history_prefix_changed"]})
    assert counts == {"history_prefix_changed": 1}
    counts = _add_cache_change_counts(counts, {"changes": ["history_prefix_changed", "tools_changed"]})
    assert counts == {"history_prefix_changed": 2, "tools_changed": 1}
    # 未知原因码不进计数；没有变化时不增加。
    assert _add_cache_change_counts(counts, {"changes": ["not_a_code"]}) == counts
    assert _add_cache_change_counts(counts, {"changes": []}) == counts
    snapshot = public_model_metrics({"schema": "model_runtime_metrics.v1", "cache_change_counts": counts})
    assert snapshot["cache_change_counts"] == counts


def test_legacy_records_without_new_fields_still_read():
    from agent_py_agent.agent.conversation.model_metrics import public_model_metrics

    # 旧格式：没有 cache_change_counts，cache_diagnostic 也没有 changed_block_index。
    legacy = {"schema": "model_runtime_metrics.v1", "input_tokens": 12,
              "cache_diagnostic": {"baseline_available": True, "changes": ["tools_changed"],
                                   "shared_message_prefix": 512, "partial": True}}
    result = public_model_metrics(legacy)
    assert result["input_tokens"] == 12
    assert result["cache_diagnostic"]["changes"] == ["tools_changed"]
    assert result["cache_diagnostic"]["changed_block_index"] is None
    assert "cache_change_counts" not in result


# ---- cachediag2：共享前缀证明边界、v1 基线、窗口滚动、别名去重 ----


# 函数用途: 共享前缀只能报到上一个相同检查点，块内更早的消息不能声称相同（不虚报）。
def test_shared_prefix_stops_at_the_last_proven_checkpoint():
    before = request_surface(_messages(64), "endpoint")
    payload = _messages(64)
    payload["messages"][0]["content"] = "rewritten-first"
    result = compare_request_surfaces(before, request_surface(payload, "endpoint"))
    # 第 1 条就改了：第一个不同检查点是 32，它之前没有相同检查点，共享前缀只能是 0（不能报 31）。
    assert result["changes"] == ["history_prefix_changed"]
    assert result["changed_block_index"] == 32
    assert result["shared_message_prefix"] == 0
    # 改第 33 条：32 号检查点仍相同，共享前缀报到 32 为止（33 之前的消息在块内，不能声称相同）。
    payload = _messages(64)
    payload["messages"][32]["content"] = "rewritten-33"
    result = compare_request_surfaces(before, request_surface(payload, "endpoint"))
    assert result["changed_block_index"] == 64
    assert result["shared_message_prefix"] == 32


# 函数用途: 旧 v1 快照作为基线时历史不可比，不报 history_*，用 comparable=False 结构化说明。
def test_v1_baseline_reports_incomparable_history_instead_of_a_fake_prefix_change():
    current = request_surface(_messages(10), "endpoint")
    v1 = {key: value for key, value in current.items() if key not in ("chain_checkpoints", "partition_options")}
    v1["schema"] = "request_surface.v1"
    result = compare_request_surfaces(v1, current)
    assert result["comparable"] is False
    assert "history_prefix_changed" not in result["changes"] and "history_appended" not in result["changes"]
    assert result["changed_block_index"] is None
    # 投影层保留这个结构化标志；旧记录没有该键时按可比较读，不让旧格式读失败。
    assert public_cache_diagnostic(result)["comparable"] is False
    assert public_cache_diagnostic({"baseline_available": True, "changes": []})["comparable"] is True


# 函数用途: reasoning 与 reasoning_effort 指同一档位时只报一次，不重复计数。
def test_reasoning_alias_switch_reports_a_single_change():
    base = {"model": "a", "messages": [{"role": "user", "content": "x"}]}
    alias = request_surface({**base, "reasoning": "high"}, "endpoint")
    canonical = request_surface({**base, "reasoning_effort": "high"}, "endpoint")
    assert compare_request_surfaces(alias, canonical)["changes"] == ["reasoning_effort_changed"]
    # 真换档位（high → low）也只报一次，不因为两个键名而报两条。
    low = request_surface({**base, "reasoning_effort": "low"}, "endpoint")
    assert compare_request_surfaces(canonical, low)["changes"] == ["reasoning_effort_changed"]
    assert compare_request_surfaces(alias, low)["changes"] == ["reasoning_effort_changed"]


# 函数用途: 4096 条窗口滚动后只追加不再误报成块 32 的改写；截短报 history_shortened。
def test_window_rollover_keeps_append_and_shorten_distinct_from_rewrite():
    full = request_surface(_messages(4096), "endpoint")
    appended = compare_request_surfaces(full, request_surface(_messages(4097), "endpoint"))
    assert appended["changes"] == ["history_appended"]
    assert appended["changed_block_index"] is None
    assert appended["shared_message_prefix"] == 4096
    shortened = compare_request_surfaces(full, request_surface(_messages(4095), "endpoint"))
    assert shortened["changes"] == []
    assert shortened["changed_block_index"] is None
    assert shortened["comparable"] is False and shortened["partial"] is True
    assert shortened["shared_message_prefix"] == 4064


# LLM: 全历史扫描的宽松性能守卫：实测 2000/4000/10000 条约 4.7/9.5/23 ms（<50 ms 阈值，未触发增量化）。
#   阈值取 2 秒只抓数量级退化（例如 O(n²)），不给慢 CI 机器制造假红。
# 函数用途: 10000 条消息的全历史扫描不得出现数量级退化。
def test_full_history_scan_has_a_loose_performance_guard():
    import time

    payload = _messages(10000)
    started = time.perf_counter()
    request_surface(payload, "endpoint")
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"10000 条消息的全历史扫描耗时 {elapsed:.3f}s，疑似数量级退化"


@pytest.mark.parametrize("count", [31, 4097])
def test_rewritten_unaligned_tail_then_append_is_explicitly_incomparable(count):
    before = request_surface(_messages(count), "endpoint")
    payload = _messages(count + 1)
    payload["messages"][count - 1]["content"] = "rewritten old tail"
    result = compare_request_surfaces(before, request_surface(payload, "endpoint"))
    assert result["changes"] == [], "未比较到旧末块，不能声称只是追加"
    assert result["comparable"] is False
    assert result["partial"] is True
    assert result["shared_message_prefix"] == count - count % 32
    assert result["changed_block_index"] is None
    assert public_cache_diagnostic(result)["comparable"] is False
    assert public_cache_diagnostic(result)["partial"] is True


@pytest.mark.parametrize("count", [31, 4097])
def test_pure_unaligned_append_is_not_claimed_as_proven(count):
    before = request_surface(_messages(count), "endpoint")
    result = compare_request_surfaces(before, request_surface(_messages(count + 1), "endpoint"))
    assert result["changes"] == []
    assert result["comparable"] is False
    assert result["partial"] is True


def test_missing_shortened_tail_is_incomparable_but_aligned_shortening_is_proven():
    full = request_surface(_messages(4096), "endpoint")
    missing = compare_request_surfaces(full, request_surface(_messages(4095), "endpoint"))
    assert missing["changes"] == []
    assert missing["comparable"] is False and missing["partial"] is True
    assert missing["shared_message_prefix"] == 4064
    proven = compare_request_surfaces(full, request_surface(_messages(4064), "endpoint"))
    assert proven["changes"] == ["history_shortened"] and proven["comparable"] is True


def test_unaligned_tail_does_not_hide_a_proven_earlier_rewrite_or_option_change():
    before = request_surface(_messages(4097), "endpoint")
    payload = _messages(4098)
    payload["messages"][0]["content"] = "rewritten first"
    result = compare_request_surfaces(before, request_surface(payload, "endpoint"))
    assert result["changes"] == ["history_prefix_changed"]
    assert result["changed_block_index"] == 64 and result["shared_message_prefix"] == 0
    options = compare_request_surfaces(before, request_surface({**_messages(4098), "thinking": "off"}, "endpoint"))
    assert options["changes"] == ["thinking_changed"]
    assert options["comparable"] is False and options["partial"] is True
