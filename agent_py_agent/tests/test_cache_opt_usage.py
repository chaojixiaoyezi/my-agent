"""归一缺口只修计量；合成计数不代表供应商缓存增长。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.usage import (
    cache_creation_input_token_usage,
    cached_input_token_usage,
    goal_token_usage,
    input_token_usage,
    normalize_usage,
    provider_usage_fields,
    provider_visible_input_token_usage,
    reported_cache_read_token_usage,
)
from agent_py_agent.agent.conversation.model_metrics import _last_response_metrics


@pytest.mark.parametrize("details", ["input_tokens_details", "prompt_tokens_details"])
@pytest.mark.parametrize("alias", ["cache_creation_tokens", "cache_write_tokens"])
@pytest.mark.parametrize("written", [0, 1000])
def test_nested_write_only_is_inclusive_responses(details, alias, written):
    response = SimpleNamespace(usage={"input_tokens": 10000, "output_tokens": 100,
                                     details: {alias: written}})
    normalized = normalize_usage(response)
    assert normalized.protocol == "openai_responses"
    assert normalized.input_tokens == provider_visible_input_token_usage(response) == 10000
    assert normalized.cache_creation_input_tokens == written
    assert cache_creation_input_token_usage(response) == written
    assert "cache_write_input_tokens" in provider_usage_fields(response)
    assert "cache_read_input_tokens" not in provider_usage_fields(response)
    assert goal_token_usage(response) == 10100


def test_responses_nested_read_write_reaches_same_normalized_total():
    response = SimpleNamespace(usage={"input_tokens": 10000, "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 9000, "cache_write_tokens": 1000}})
    normalized = normalize_usage(response)
    assert (normalized.input_tokens, normalized.cached_input_tokens,
            normalized.cache_creation_input_tokens) == (10000, 9000, 1000)
    assert input_token_usage(response) == provider_visible_input_token_usage(response) == 10000
    assert goal_token_usage(response) == 1100
    assert cached_input_token_usage(response) == 9000


@pytest.mark.parametrize("usage,read", [
    ({"prompt_cache_hit_tokens": 9000}, 9000),
    ({"prompt_cache_hit_tokens": 0}, 0),
    ({"prompt_cache_hit_tokens": 9000, "cache_read_input_tokens": 0}, 0),
    ({"prompt_cache_hit_tokens": 9000, "cache_read_input_tokens": 123}, 123),
    ({"prompt_cache_hit_tokens": 9000, "prompt_tokens_details": {"cached_tokens": 0}}, 0),
    ({"prompt_cache_hit_tokens": 9000, "input_tokens_details": {"cache_read_tokens": 321}}, 321),
])
def test_deepseek_fallback_requires_valid_total_and_preserves_canonical_zero(usage, read):
    response = SimpleNamespace(usage={"prompt_tokens": 10000, "completion_tokens": 100, **usage})
    assert normalize_usage(response).cached_input_tokens == read
    assert reported_cache_read_token_usage(response) == read
    assert goal_token_usage(response) == 10100 - read
    assert "cache_read_input_tokens" in provider_usage_fields(response)
    metrics = _last_response_metrics(response, SimpleNamespace(records=lambda: ()), "")
    assert metrics["cache_percent"] == read / 100


@pytest.mark.parametrize("total", [None, -1, "bad"])
def test_hit_without_valid_prompt_total_is_not_reported(total):
    response = SimpleNamespace(usage={"prompt_tokens": total, "input_tokens": 10000,
                                     "prompt_cache_hit_tokens": 9000, "output_tokens": 100})
    assert reported_cache_read_token_usage(response) == 0
    assert "cache_read_input_tokens" not in provider_usage_fields(response)
    assert _last_response_metrics(response, SimpleNamespace(records=lambda: ()), "")["cache_percent"] is None


def test_misses_are_not_cache_writes_and_unknown_read_is_not_zero_percent():
    response = SimpleNamespace(usage={"prompt_tokens": 10000, "completion_tokens": 100,
                                     "prompt_cache_miss_tokens": 10000})
    assert normalize_usage(response).cache_creation_input_tokens == 0
    assert "cache_write_input_tokens" not in provider_usage_fields(response)
    assert "cache_read_input_tokens" not in provider_usage_fields(response)
    assert _last_response_metrics(response, SimpleNamespace(records=lambda: ()), "")["cache_percent"] is None


def test_anthropic_disjoint_counters_and_legacy_inclusive_helper_are_unchanged():
    response = SimpleNamespace(usage={"input_tokens": 100, "cache_read_input_tokens": 700,
                                     "cache_creation_input_tokens": 200, "output_tokens": 50})
    normalized = normalize_usage(response)
    assert normalized.protocol == "anthropic_compatible"
    assert normalized.input_tokens == provider_visible_input_token_usage(response) == 1000
    assert (normalized.cached_input_tokens, normalized.cache_creation_input_tokens) == (700, 200)
    assert goal_token_usage(response) == 350
    assert cached_input_token_usage(response) == 0


def test_nested_aliases_ignore_invalid_values_without_losing_known_zero():
    response = SimpleNamespace(usage={"prompt_tokens": "100", "completion_tokens": "1",
        "input_tokens_details": {"cached_tokens": -1, "cache_read_tokens": 0,
                                 "cache_creation_tokens": "bad", "cache_write_tokens": 7},
        "prompt_cache_hit_tokens": 80})
    assert normalize_usage(response).cached_input_tokens == 0
    assert normalize_usage(response).cache_creation_input_tokens == 7
    assert reported_cache_read_token_usage(response) == 0


def test_missing_usage_is_partial_and_has_no_reported_fields():
    response = SimpleNamespace(usage={})
    assert normalize_usage(response).partial
    assert input_token_usage(response) is None
    assert provider_usage_fields(response) == ()


def test_existing_nested_read_with_top_write_keeps_inclusive_total():
    response = SimpleNamespace(usage={"input_tokens": 10000, "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 9000}, "cache_creation_input_tokens": 1000})
    assert normalize_usage(response).protocol == "openai_responses"
    assert normalize_usage(response).input_tokens == 10000
    assert provider_visible_input_token_usage(response) == 10000
    assert goal_token_usage(response) == 1100


def test_nested_counters_without_total_remain_reported_but_input_unknown():
    response = SimpleNamespace(usage={"output_tokens": 1,
        "input_tokens_details": {"cached_tokens": 3, "cache_creation_tokens": 4}})
    normalized = normalize_usage(response)
    assert normalized.protocol == "unknown" and normalized.partial
    assert input_token_usage(response) is None
    assert (normalized.cached_input_tokens, normalized.cache_creation_input_tokens) == (3, 4)
    assert provider_usage_fields(response) == ("output_tokens", "cache_read_input_tokens", "cache_write_input_tokens")


@pytest.mark.parametrize("total_key", ["prompt_tokens", "input_tokens"])
def test_conflicting_canonical_aliases_keep_one_read_write_value_and_mark_partial(total_key):
    response = SimpleNamespace(usage={total_key: 10000, "output_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 2},
        "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 3,
        "prompt_cache_hit_tokens": 9999})
    normalized = normalize_usage(response)
    assert normalized.cached_input_tokens == reported_cache_read_token_usage(response) == 0
    assert normalized.cache_creation_input_tokens == cache_creation_input_token_usage(response) == 2
    assert normalized.partial
