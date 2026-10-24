# LLM: 以真实协议投影和纯摘要回归缓存诊断，不访问供应商、不把客户端hash称为服务端命中。
# 模块用途: 区分请求面变化，验证严格结构LRU的等价性、命中和资源边界，保护公开诊断不泄漏正文。
import copy
import hashlib
import json
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

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


def test_surface_and_first_call_carry_a_tool_baseline():
    """工具名清单摘要/个数进请求面；run 第一次调用（无 previous）也带自己的工具面基线，供事后定位收窄。"""
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}],
               "tools": [{"name": "read_file"}, {"name": "write_file"}]}
    surface = request_surface(payload, "endpoint")
    assert surface["tool_count"] == 2
    assert len(surface["tool_names_digest"]) == 64
    first = compare_request_surfaces({}, surface)
    assert first["baseline_available"] is False
    assert first["current_tool_count"] == 2
    assert first["current_tool_names_digest"] == surface["tool_names_digest"]
    reordered = request_surface({**payload, "tools": [{"name": "write_file"}, {"name": "read_file"}]}, "endpoint")
    assert reordered["tool_names_digest"] != surface["tool_names_digest"], "顺序变化要反映在名字清单摘要里"
    assert reordered["tool_count"] == surface["tool_count"]
    assert "tools_changed" in compare_request_surfaces(surface, reordered)["changes"]


def test_public_diagnostic_keeps_only_safe_tool_baseline_fields():
    """公开投影只保留数字与 64 位十六进制摘要；坏形状清成默认值，不猜测、不回显原文。"""
    digest = "a" * 64
    kept = public_cache_diagnostic({"baseline_available": False, "changes": [],
                                    "current_tool_count": 3, "current_tool_names_digest": digest})
    assert kept["current_tool_count"] == 3
    assert kept["current_tool_names_digest"] == digest
    bad = public_cache_diagnostic({"baseline_available": True, "changes": [],
                                   "current_tool_count": "3", "current_tool_names_digest": "not-a-digest"})
    assert bad["current_tool_count"] == 0
    assert bad["current_tool_names_digest"] == ""


# LLM: 真实Chat/Responses/Anthropic/custom形状只提工具身份；同数量换名必须可见，不把正文写进遥测。
# 函数用途: 钉住不同工具协议的同数量替换诊断。
@pytest.mark.parametrize("shape", ["chat", "flat", "custom"])
def test_same_count_tool_replacement_changes_real_name_digest(shape):
    # LLM: 只生成协议wrapper，不排序工具序列或修改名字。
    # 函数用途: 在三种真实工具形状中包住相同合成身份。
    def tool(name):
        return {"type": "function", "function": {"name": name}} if shape == "chat" else (
            {"type": "custom", "custom": {"name": name}} if shape == "custom" else {"name": name})

    before = request_surface({"tools": [tool("read_file")]}, "endpoint")
    after = request_surface({"tools": [tool("write_file")]}, "endpoint")
    assert before["tool_count"] == after["tool_count"] == 1
    assert before["tool_names_digest"] != after["tool_names_digest"]
    compared = compare_request_surfaces(before, after)
    assert "tools_changed" in compared["changes"]
    assert "tools_names_changed" in compared["changes"]
    assert "tools_names_changed" in public_cache_diagnostic(compared)["changes"]


# LLM: 名字优先级独立于schema，非字典与未知内置类型不执行任何对象代码；测试中标识均为合成值。
# 函数用途: 核验工具名的层级优先级及内置kind兜底。
@pytest.mark.parametrize(("tool", "expected"), [
    ({"name": "top", "function": {"name": "nested"}}, "top"),
    ({"name": "", "function": {"name": "nested"}}, "nested"),
    ({"function": "bad", "custom": {"name": "custom-name"}}, "custom-name"),
    ({"type": "web_search_preview"}, "type:web_search_preview"),
    (None, ""), ([], ""), ({}, ""),
])
def test_tool_name_extraction_precedence_and_builtin_kind(tool, expected):
    from agent_py_agent.agent.backends import cache_diagnostics

    extract = getattr(cache_diagnostics, "_extract_tool_name", None)
    assert callable(extract), "缺少协议形状工具名提取器"
    assert extract(tool) == expected


# LLM: 名称摘要保留工具顺序，工具描述/schema变化仍由全量tools摘要单独解释。
# 函数用途: 区分换名、换序与同名schema更新且不修改输入。
def test_tool_names_distinguish_schema_changes_and_order_without_mutation():
    payload = {"tools": [{"type": "function", "function": {"name": "a", "description": "private-marker"}},
                         {"type": "function", "function": {"name": "b"}}]}
    original = copy.deepcopy(payload)
    before = request_surface(payload, "endpoint")
    changed = copy.deepcopy(payload)
    changed["tools"][0]["function"]["description"] = "updated-private-marker"
    after = request_surface(changed, "endpoint")
    assert before["tool_names_digest"] == after["tool_names_digest"]
    assert compare_request_surfaces(before, after)["changes"] == ["tools_changed"]
    reordered = request_surface({"tools": list(reversed(payload["tools"]))}, "endpoint")
    assert before["tool_names_digest"] != reordered["tool_names_digest"]
    assert payload == original and "private-marker" not in json.dumps(before)


# LLM: 旧Chat名字摘要全为空，不能把算法升级伪报成工具替换；历史链v2合同保持不变。
# 函数用途: 让旧工具名摘要显式不可比较，而不丢全量tools变化。
def test_legacy_name_digest_is_not_misreported_as_tool_replacement():
    current = request_surface({"tools": [{"type": "function", "function": {"name": "a"}}]}, "endpoint")
    assert current.get("tool_names_schema") == "tool_names.v2"
    legacy = {**current, "tool_names_digest": "0" * 64}
    legacy.pop("tool_names_schema")
    compared = compare_request_surfaces(legacy, current)
    assert "tools_names_changed" not in compared["changes"]
    assert compared["tool_names_comparable"] is False


# LLM: 在既有HTTP后端上只用纯投影或假传输捕获，不改组包函数、不联网，synthetic不是用户凭据。
# 函数用途: 创建支持真实Chat组包的本地合成后端。
def _chat_backend():
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    return OpenAICompatibleBackend(BackendOptions(api_base="https://unrecognized.example.invalid/v1",
        api_key="synthetic", model_name="test-model", stream_enabled=False))


# LLM: 只扰动对象键序，数组及标量保持原值；不能在比较端排序来掩盖真实wire差异。
# 函数用途: 生成逻辑相同但对象插入顺序相反的输入。
def _reverse_object_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_object_keys(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_object_keys(item) for item in value]
    return value


# LLM: 使用真实project_generate_payload且按未排序JSON比较字节；send共用组包，未知端点不加扩展能力字段。
# 函数用途: 核验schema嵌套键稳定、输入不变和实际发送边界同源。
def test_chat_wire_schema_order_is_stable_and_shared_with_send(monkeypatch):
    backend = _chat_backend()
    tools = [{"name": "z_tool", "description": "原文\n\n顺序", "input_schema": {"type": "object",
        "properties": {"z": {"type": "string", "enum": ["second", "first"]}, "a": {"type": "integer"}},
        "required": ["z", "a"], "allOf": [{"minProperties": 1, "maxProperties": 5}]}},
        {"name": "a_tool", "input_schema": {"type": "object"}}]
    original = copy.deepcopy(tools)
    first = backend.project_generate_payload("canonical", messages=[], tools=tools)
    second = backend.project_generate_payload("canonical", messages=[], tools=_reverse_object_keys(tools))
    assert isinstance(first, dict) and isinstance(second, dict)
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert [row["function"]["name"] for row in first["tools"]] == ["z_tool", "a_tool"]
    assert first["tools"][0]["function"]["parameters"] == tools[0]["input_schema"]
    assert tools == original
    delta = compare_request_surfaces(request_surface(first, "endpoint"), request_surface(second, "endpoint"))
    assert "tools_changed" not in delta["changes"] and "system_changed" not in delta["changes"]
    captured = []

    # LLM: 只替换运输函数，真实generate与_request_payload照常执行，响应仅为本地合成数据。
    # 函数用途: 捕获真正发送边界的payload而不访问网络。
    def capture(_path, payload, _headers):
        captured.append(payload)
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(backend, "request_json", capture)
    backend.generate("canonical", messages=[], tools=tools)
    assert captured == [first]
    assert not ({"tool_updates", "configuration_update", "thinking"} & set(first))


# LLM: 数组顺序是已发送表面的一部分；规范对象键不能把工具换序、required/enum换序或描述改写藏掉。
# 函数用途: 验证真实工具/数组变化继续产生tools_changed。
@pytest.mark.parametrize("change", ["tools", "required", "enum", "description"])
def test_chat_wire_canonicalization_does_not_hide_real_changes(change):
    backend = _chat_backend()
    tools = [{"name": "z", "description": "before", "input_schema": {"required": ["z", "a"],
        "properties": {"z": {"enum": ["second", "first"]}, "a": {"type": "string"}}}}, {"name": "a"}]
    changed = copy.deepcopy(tools)
    if change == "tools":
        changed.reverse()
    elif change == "description":
        changed[0]["description"] = "after"
    elif change == "required":
        changed[0]["input_schema"]["required"].reverse()
    else:
        changed[0]["input_schema"]["properties"]["z"]["enum"].reverse()
    before = backend.project_generate_payload("canonical", messages=[], tools=tools)
    after = backend.project_generate_payload("canonical", messages=[], tools=changed)
    assert "tools_changed" in compare_request_surfaces(request_surface(before, "endpoint"), request_surface(after, "endpoint"))["changes"]


# LLM: 普通与typed布局均保留系统/developer正文和段落次序；只扰动对象键，不按推荐或工具名重排段落。
# 函数用途: 锁住system稳定、真实system变更可见与volatile只追加尾部的合同。
def test_chat_system_developer_and_typed_sections_are_deterministic():
    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    backend = _chat_backend()
    options = ProviderRequestOptions(system_instruction="系统乙\n\n系统甲")
    history = [{"role": "developer", "content": "开发乙\n\n开发甲"}, {"role": "user", "content": "history"}]
    first = backend.project_generate_payload("canonical", messages=history, request_options=options)
    second = backend.project_generate_payload("canonical", messages=_reverse_object_keys(history), request_options=options)
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert first["messages"][:2] == [{"role": "system", "content": options.system_instruction}, {"role": "user", "content": "canonical"}]
    assert first["messages"][2:] == history
    changed = backend.project_generate_payload("canonical", messages=history,
        request_options=ProviderRequestOptions(system_instruction="系统甲\n\n系统乙"))
    assert "system_changed" in compare_request_surfaces(request_surface(first, "endpoint"), request_surface(changed, "endpoint"))["changes"]
    typed = backend.project_generate_payload(CacheStructuredPrompt("稳定乙\n\n稳定甲", "volatile-a", stable_user_prefix="canonical"),
        messages=[], request_options=options)
    volatile = backend.project_generate_payload(CacheStructuredPrompt("稳定乙\n\n稳定甲", "volatile-b", stable_user_prefix="canonical"),
        messages=[], request_options=options)
    assert typed["messages"][0]["content"] == "系统乙\n\n系统甲\n\n稳定乙\n\n稳定甲"
    assert "system_changed" not in compare_request_surfaces(request_surface(typed, "endpoint"), request_surface(volatile, "endpoint"))["changes"]


# LLM: 每例使用独立内存LRU，不改生产配置或持久化正文；退出后还原原缓存及字节账。
# 函数用途: 隔离摘要缓存测试，避免顺序影响命中次数。
@pytest.fixture
def digest_cache(monkeypatch):
    from agent_py_agent.agent.backends import cache_diagnostics as diagnostic

    monkeypatch.setattr(diagnostic, "_ROW_DIGEST_CACHE", OrderedDict(), raising=False)
    monkeypatch.setattr(diagnostic, "_CHAIN_STEP_CACHE", OrderedDict(), raising=False)
    monkeypatch.setattr(diagnostic, "_ROW_CACHE_BYTES", 0, raising=False)
    return diagnostic


# LLM: 独立重建优化前的未排序JSON和完整链，不复用被测摘要或缓存。
# 函数用途: 为边界值及中间改写提供逐字节等价基准。
def _uncached_checkpoints(rows):
    # LLM: 基准算法始终未排序序列化，不调用被测_digest或其缓存。
    # 函数用途: 独立计算优化前的片段摘要。
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    chain, checkpoints = digest("request_surface.v2.seed"), []
    for index, row in enumerate(rows, 1):
        chain = hashlib.sha256(f"{chain}\0{digest(row)}".encode()).hexdigest()
        if index % 32 == 0 or index == len(rows):
            checkpoints.append({"index": index, "chain": chain})
    return checkpoints[-128:]


# LLM: 首尾和长度不变时仍须检测中间原位改写，比较未排序wire基准而非Python宽松相等。
# 函数用途: 验证数值类型、零符号和dict键序改变不会复用旧链。
@pytest.mark.parametrize(("before", "after"), [
    (1, True), (-0.0, 0.0), ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
])
def test_w21_middle_in_place_mutation_preserves_wire_exact_digest(digest_cache, before, after):
    rows = [{"role": "user", "content": index} for index in range(96)]
    rows[40]["content"] = before
    original = digest_cache._chain_checkpoints(rows)
    assert original == _uncached_checkpoints(rows)
    rows[40]["content"] = after
    changed = digest_cache._chain_checkpoints(rows)
    assert changed == _uncached_checkpoints(rows)
    assert original[0] == changed[0] and original[1:] != changed[1:]


# LLM: 记录真实_digest调用并保留独立链等价检查，不能仅靠假摘要证明命中。
# 函数用途: 验证追加轮不会重复散列旧消息。
def test_w21_repeated_append_only_hashes_new_rows(digest_cache, monkeypatch):
    original, calls = digest_cache._digest, []

    # LLM: 包裹原函数而不改变摘要结果。
    # 函数用途: 记录每次真正进行JSON摘要的输入。
    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(digest_cache, "_digest", counted)
    rows = [{"content": str(index)} for index in range(80)]
    assert digest_cache._chain_checkpoints(rows) == _uncached_checkpoints(rows)
    assert len(calls) == 81
    for index in range(4):
        calls.clear()
        rows.append({"content": f"append-{index}"})
        assert digest_cache._chain_checkpoints(rows) == _uncached_checkpoints(rows)
        assert len(calls) <= 2, "旧消息不能在追加轮再次执行JSON摘要"


# LLM: 记录真实SHA并改写返回检查点，分别守住链步复用及缓存输出隔离。
# 函数用途: 验证重复扫描不重算链步、调用方不能污染下次检查点。
def test_w21_repeated_scan_reuses_chain_steps_and_returns_fresh_checkpoints(digest_cache, monkeypatch):
    original, calls = hashlib.sha256, []

    # LLM: 仍调用真实SHA，计数只观测而不伪造摘要。
    # 函数用途: 统计重复扫描的实际散列次数。
    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(digest_cache.hashlib, "sha256", counted)
    rows = [{"content": str(index)} for index in range(80)]
    first = digest_cache._chain_checkpoints(rows)
    calls.clear()
    second = digest_cache._chain_checkpoints(copy.deepcopy(rows))
    assert first == second
    assert len(calls) <= 1, "相同行及链步都应命中，不能重复SHA256"
    second[-1]["chain"] = "caller mutation"
    assert digest_cache._chain_checkpoints(rows) == first


# LLM: NaN、无穷、负零、未知类型及有界循环均按明确结构规则断言。
# 函数用途: 验证严格键不会混淆wire差异或无限递归。
def test_w21_strict_keys_distinguish_types_order_and_signed_zero(digest_cache):
    key = getattr(digest_cache, "_strict_json_cache_key", None)
    assert callable(key), "缺少严格结构键"
    assert key(1) != key(True) and key(-0.0) != key(0.0)
    assert key({"a": 1, "b": 2}) != key({"b": 2, "a": 1})
    assert key([None, True, "中文"]) == key((None, True, "中文"))
    assert key(float("nan")) == key(float("nan"))
    assert key(float("inf")) != key(float("-inf"))
    assert key(object()) is None and key([], _depth=65) is None
    cyclic = []
    cyclic.append(cyclic)
    assert key(cyclic) is None


# LLM: 缩小测试内限额检验真实淘汰及字节账，不放宽生产限额。
# 函数用途: 同时验证LRU条数、内存估算和最旧项回收。
def test_w21_lru_enforces_row_count_bytes_and_chain_count(digest_cache, monkeypatch):
    monkeypatch.setattr(digest_cache, "_MAX_ROW_CACHE_ENTRIES", 2, raising=False)
    monkeypatch.setattr(digest_cache, "_MAX_ROW_CACHE_BYTES", 4096, raising=False)
    monkeypatch.setattr(digest_cache, "_MAX_CHAIN_CACHE_ENTRIES", 3, raising=False)
    rows = [{"content": str(index)} for index in range(6)]
    digest_cache._chain_checkpoints(rows)
    assert len(digest_cache._ROW_DIGEST_CACHE) == 2
    assert len(digest_cache._CHAIN_STEP_CACHE) == 3
    assert sum(size for _, size in digest_cache._ROW_DIGEST_CACHE.values()) == digest_cache._ROW_CACHE_BYTES
    assert 0 < digest_cache._ROW_CACHE_BYTES <= 4096
    assert digest_cache._strict_json_cache_key(rows[-1]) in digest_cache._ROW_DIGEST_CACHE
    assert digest_cache._strict_json_cache_key(rows[0]) not in digest_cache._ROW_DIGEST_CACHE
    monkeypatch.setattr(digest_cache, "_MAX_ROW_CACHE_BYTES", 1)
    digest_cache._chain_checkpoints([{"content": "new"}])
    assert not digest_cache._ROW_DIGEST_CACHE
    assert digest_cache._ROW_CACHE_BYTES == 0


# LLM: 超大行仍走原摘要，未知对象和循环保持JSON原错误而不缓存伪成功。
# 函数用途: 验证旁路缓存不改变错误或链值。
def test_w21_large_or_unsupported_rows_bypass_cache_without_changing_json_errors(digest_cache):
    large = {"content": "x" * (256 * 1024 + 1)}
    assert digest_cache._chain_checkpoints([large]) == _uncached_checkpoints([large])
    assert not digest_cache._ROW_DIGEST_CACHE
    with pytest.raises(TypeError):
        digest_cache._chain_checkpoints([{"content": object()}])
    with pytest.raises(ValueError, match="Circular reference"):
        cyclic = []
        cyclic.append(cyclic)
        digest_cache._chain_checkpoints([cyclic])


# LLM: 多线程只共享LRU，散列前实取同一锁验证非锁内执行，结果对照独立原算法。
# 函数用途: 验证并发扫描的锁边界和完整输出。
def test_w21_digest_work_is_outside_cache_lock_and_concurrent_results_match(digest_cache, monkeypatch):
    original = digest_cache._digest

    # LLM: 非阻塞尝试真实缓存锁后释放，再执行原摘要，不篡改锁或输出。
    # 函数用途: 捕捉在LRU锁内进行JSON/SHA的错误接线。
    def checked(value):
        lock = getattr(digest_cache, "_CACHE_LOCK", None)
        assert lock is not None, "缺少LRU锁"
        assert lock.acquire(blocking=False), "不能在LRU锁内做JSON/SHA256"
        lock.release()
        return original(value)

    monkeypatch.setattr(digest_cache, "_digest", checked)
    cases = [[{"content": str(index % 5)} for index in range(size)] for size in range(20, 40)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(digest_cache._chain_checkpoints, cases))
    assert actual == [_uncached_checkpoints(rows) for rows in cases]
