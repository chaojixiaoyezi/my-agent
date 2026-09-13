"""审计 #19(成本部分,medium/稳定)真测:分模型 USD 计价。

只有 token 数无法换算成钱、无法跨模型对齐租户成本。真按单价表算 USD,断言:已知模型正确计价、
带日期后缀 ID 最长前缀匹配、未知模型保守默认不低估、set_pricing/env 可覆盖、负 token 按 0、
response_cost_usd 从响应 usage 算成本。学 长期助手 持久化 cost、Anthropic SDK total_cost_usd。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.llm_scale import model_pricing
from agent_py_agent.agent.llm_scale.model_pricing import (
    ModelPrice,
    _apply_env_entry,
    cost_usd,
    resolve_price,
    set_pricing,
)


def test_known_model_cost() -> None:
    # opus: 15/Mtok in, 75/Mtok out → 1000*15e-6 + 500*75e-6 = 0.015 + 0.0375
    assert cost_usd("claude-opus-4-8", 1000, 500) == round(0.015 + 0.0375, 6)
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 0) == 3.0  # sonnet 3/Mtok in
    assert cost_usd("claude-haiku-4-5", 0, 1_000_000) == 5.0  # haiku 5/Mtok out


def test_dated_model_id_prefix_match() -> None:
    # 完整 ID 带日期后缀 → 最长前缀匹配到 haiku 档(不掉进默认)
    assert resolve_price("claude-haiku-4-5-20251001") == resolve_price("claude-haiku-4-5")
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.0


def test_unknown_model_uses_conservative_default(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.llm_scale.model_pricing"):
        price = resolve_price("some-unknown-model-x")
    assert price == model_pricing._DEFAULT_PRICE  # 保守默认(最贵档),不低估成本
    assert price.input_per_mtok_usd >= 15.0
    assert any("未知模型" in r.getMessage() for r in caplog.records)  # 不静默,告警


def test_unknown_model_warns_once_per_process(caplog) -> None:
    import logging

    model = "unknown-warning-dedup-model"
    model_pricing._WARNED_UNKNOWN_MODELS.discard(model)
    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.llm_scale.model_pricing"):
        resolve_price(model)
        resolve_price(model)
        resolve_price(model)

    warnings = [record for record in caplog.records if model in record.getMessage()]
    assert len(warnings) == 1


def test_negative_tokens_floored_to_zero() -> None:
    assert cost_usd("claude-opus-4-8", -100, -50) == 0.0


def test_set_pricing_override() -> None:
    try:
        set_pricing("my-private-model", ModelPrice(2.0, 8.0))
        assert cost_usd("my-private-model", 1_000_000, 1_000_000) == 10.0  # 2 + 8
    finally:
        model_pricing._OVERRIDES.pop("my-private-model", None)


def test_env_entry_override_and_bad_entry(caplog) -> None:
    import logging

    try:
        _apply_env_entry("custom-model:4/20")
        assert cost_usd("custom-model", 1_000_000, 1_000_000) == 24.0  # 4 + 20
        with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.llm_scale.model_pricing"):
            _apply_env_entry("garbage-no-numbers:abc/def")  # 非法:忽略并告警,不崩
        assert any("非法" in r.getMessage() for r in caplog.records)
    finally:
        model_pricing._OVERRIDES.pop("custom-model", None)


def test_response_cost_usd_from_usage() -> None:
    from agent_py_agent.agent.agent_core.model.usage import response_cost_usd

    response = SimpleNamespace(usage={"input_tokens": 1000, "output_tokens": 500})
    assert response_cost_usd("claude-opus-4-8", response) == round(0.015 + 0.0375, 6)
    # usage 缺失 → 成本 0(不崩)
    assert response_cost_usd("claude-opus-4-8", SimpleNamespace()) == 0.0


# LLM: 计费档位互斥：普通输入 = 总输入 − 缓存命中 − 缓存写入；缓存档缺失必须回落普通输入价并
# 标 partial，既不假装精确也不把缓存当免费。这是 TOK-03 的回归锁。
# 函数用途: 验证分档计价、缺档回落标记、以及 env 声明四段价生效。
def test_cost_breakdown_prices_cache_tiers_and_marks_unknown_tiers() -> None:
    set_pricing("tiered-model", ModelPrice(0.30, 1.20, cache_read_per_mtok_usd=0.03, cache_write_per_mtok_usd=0.375))
    cost, partial = model_pricing.cost_usd_breakdown(
        "tiered-model",
        input_tokens=1000,
        cached_input_tokens=700,
        cache_creation_input_tokens=200,
        output_tokens=50,
    )
    expect = 100 / 1e6 * 0.30 + 700 / 1e6 * 0.03 + 200 / 1e6 * 0.375 + 50 / 1e6 * 1.20
    assert partial is False
    assert abs(cost - expect) < 1e-9

    set_pricing("no-tier-model", ModelPrice(1.0, 2.0))
    cost, partial = model_pricing.cost_usd_breakdown(
        "no-tier-model",
        input_tokens=1000,
        cached_input_tokens=700,
        cache_creation_input_tokens=200,
        output_tokens=50,
    )
    assert partial is True
    assert abs(cost - (1000 / 1e6 * 1.0 + 50 / 1e6 * 2.0)) < 1e-9  # 全部回落普通输入价

    _apply_env_entry("env-tiered:1.0/2.0/0.1/0.5")
    price = resolve_price("env-tiered")
    assert price.cache_read_per_mtok_usd == 0.1 and price.cache_write_per_mtok_usd == 0.5
