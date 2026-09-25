"""媒体压缩策略片 C：含图回合分组、按预算与次数上限打包看图小请求、部分成功与 typed 失败的结构化结果、checkpoint 部分计数。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_media_digest import (
    MEDIA_DIGEST_PURPOSE,
    MediaDigestContext,
    digest_media_decision,
    generate_media_digest,
    largest_digest_request_tokens,
    media_turn_groups,
    plan_media_digest,
    summarize_media_turns,
)
from agent_py_agent.agent.conversation.compact_media_policy import (
    COMPACT_VISION_SUMMARY_FAILED,
    MEDIA_REASON_DIGEST_PARTIAL,
    CompactMediaDecision,
    media_archive_facts,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
    conversation_compact_provider_source,
)
from agent_py_agent.tests.test_compact_media_vision import (  # noqa: F401
    _append,
    _compact,
    vision_case,
)

SHA_ONE, SHA_TWO = "c" * 64, "d" * 64
SURFACE = ConversationCompactProviderSurface("稳定前缀", None, "摘要系统")


# 函数用途: 造一个 canonical local_file 图块；sha 由字符重复而来，便于断言前缀。
def _image(sha: str, name: str) -> dict:
    return {"type": "image", "source": {"type": "local_file", "path": "/owner/attachments/" + sha, "sha256": sha,
                                        "media_type": "image/png", "size_bytes": 321, "name": name}}


# 函数用途: 两个含图回合夹着纯文字回合的历史；只有带 native 信封的回合才有媒体块。
def _two_media_turns(case):
    first = [{"role": "user", "content": [{"type": "text", "text": "看第一张"}, _image(SHA_ONE, "one.png")]},
             {"role": "assistant", "content": [{"type": "text", "text": "第一张是柱状图"}]}]
    second = [{"role": "user", "content": [{"type": "text", "text": "看第二张"}, _image(SHA_TWO, "two.png")]},
              {"role": "assistant", "content": [{"type": "text", "text": "第二张是折线图"}]}]
    return [
        _append(case, "user", "旧问题", "turn-text"), _append(case, "assistant", "旧回答", "turn-text"),
        _append(case, "user", "看第一张", "turn-m1"), _append(case, "assistant", "第一张是柱状图", "turn-m1", native=first),
        _append(case, "user", "中间问题", "turn-mid"), _append(case, "assistant", "中间回答", "turn-mid"),
        _append(case, "user", "看第二张", "turn-m2"), _append(case, "assistant", "第二张是折线图", "turn-m2", native=second),
        _append(case, "user", "下一个问题", "turn-later"), _append(case, "assistant", "下一个回答", "turn-later"),
    ]


# 函数用途: 与被测模块同一来源构造方式算一组回合会变成几条 provider 消息，用来推导测试里的预算数字。
def _messages(rows) -> int:
    return len(list(conversation_compact_provider_source("", 0, tuple(rows), volatile_sections=())))


# 函数用途: 让估算与消息条数成正比（每条 40），这样打包结果由预算决定而不是常数。
@pytest.fixture
def proportional_estimate(monkeypatch):
    monkeypatch.setattr(budget_module, "compact_request_tokens", lambda _request, source=None: 40 * len(list(source)))


def test_media_turn_groups_keeps_only_turns_with_media_in_order(vision_case):  # noqa: F811
    groups = media_turn_groups(_two_media_turns(vision_case))
    assert [row.metadata["conversation_request_id"] for group in groups for row in group] == ["turn-m1", "turn-m1", "turn-m2", "turn-m2"]
    assert [media_archive_facts(group).refs for group in groups] == [(SHA_ONE,), (SHA_TWO,)]


def test_plan_packs_turns_by_budget_and_caps_requests(vision_case, proportional_estimate):  # noqa: F811
    case = vision_case
    groups = media_turn_groups(_two_media_turns(case))
    context = MediaDigestContext(surface=SURFACE)
    per_group = 40 * _messages(groups[0]) + 50  # 每回合一个图块 × 预留 50
    assert largest_digest_request_tokens(case.agent, context, _two_media_turns(case)) == per_group

    both = plan_media_digest(case.agent, context, groups, budget=10_000, max_requests=4)
    assert len(both.chunks) == 1 and both.chunks[0].facts.blocks == 2 and both.skipped_blocks == 0

    split = plan_media_digest(case.agent, context, groups, budget=per_group + 30, max_requests=4)
    assert [chunk.facts.refs for chunk in split.chunks] == [(SHA_ONE,), (SHA_TWO,)] and split.skipped_blocks == 0

    capped = plan_media_digest(case.agent, context, groups, budget=per_group + 30, max_requests=1)
    assert [chunk.facts.refs for chunk in capped.chunks] == [(SHA_ONE,)]
    assert (capped.skipped_blocks, capped.skipped_reason) == (1, "vision_digest_cap")

    starved = plan_media_digest(case.agent, context, groups, budget=per_group - 1, max_requests=4)
    assert starved.chunks == () and (starved.skipped_blocks, starved.skipped_reason) == (2, "summary_budget_exceeded")


def test_generate_media_digest_labels_chunks_and_stops_at_the_first_typed_failure(vision_case, proportional_estimate, monkeypatch):  # noqa: F811
    case = vision_case
    groups = media_turn_groups(_two_media_turns(case))
    context = MediaDigestContext(surface=SURFACE, request_id="req-1", thread_id="thread-1")
    plan = plan_media_digest(case.agent, context, groups, budget=40 * _messages(groups[0]) + 80, max_requests=4)
    assert len(plan.chunks) == 2
    outcomes = [SimpleNamespace(text="第一张要点", tool_use_blocks=[], truncated=False),
                ConversationCompactError("随图摘要请求失败：InputMediaError", code=COMPACT_VISION_SUMMARY_FAILED)]
    seen = []

    def fake_bounded(request, *, message_source=None, vision_summary=False, media_reserve_tokens=0, **_kwargs):
        seen.append({"purpose": request.purpose, "request_id": request.request_id, "thread_id": request.thread_id,
                     "vision_summary": vision_summary, "media_reserve_tokens": media_reserve_tokens,
                     "text": json.dumps(list(message_source), ensure_ascii=False)})
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(budget_module, "generate_bounded_compact_response", fake_bounded)
    outcome = generate_media_digest(case.agent, context, plan)
    assert (outcome.summarized_blocks, outcome.requests, outcome.failure_code) == (1, 2, COMPACT_VISION_SUMMARY_FAILED)
    assert f"sha256:{SHA_ONE[:12]}" in outcome.text and "第一张要点" in outcome.text and SHA_TWO[:12] not in outcome.text
    assert [item["purpose"] for item in seen] == [MEDIA_DIGEST_PURPOSE] * 2 and seen[0]["request_id"] == "req-1"
    assert all(item["vision_summary"] and item["media_reserve_tokens"] == 50 for item in seen)
    assert SHA_ONE in seen[0]["text"] and SHA_TWO not in seen[0]["text"], "每个小请求只带自己那组回合的图块"
    decision = digest_media_decision(CompactMediaDecision("vision_summary", "declared"), plan, outcome)
    assert decision == CompactMediaDecision("vision_summary", "declared", MEDIA_REASON_DIGEST_PARTIAL, summarized_blocks=1)

    nothing = digest_media_decision(CompactMediaDecision("vision_summary", "declared"), plan,
                                    generate_media_digest.__globals__["MediaDigestOutcome"](failure_code=COMPACT_VISION_SUMMARY_FAILED))
    assert nothing == CompactMediaDecision("archived_refs", "declared", COMPACT_VISION_SUMMARY_FAILED), "一次都没成功整体回落并保留失败码"


def test_generate_media_digest_propagates_non_typed_errors_and_treats_empty_replies_as_failure(vision_case, proportional_estimate, monkeypatch):  # noqa: F811
    case = vision_case
    groups = media_turn_groups(_two_media_turns(case))
    context = MediaDigestContext(surface=SURFACE)
    plan = plan_media_digest(case.agent, context, groups, budget=10_000, max_requests=4)
    monkeypatch.setattr(budget_module, "generate_bounded_compact_response",
                        lambda *_a, **_k: (_ for _ in ()).throw(ConversationCompactError("来源变化", code="COMPACT_SOURCE_CHANGED")))
    with pytest.raises(ConversationCompactError) as failure:
        generate_media_digest(case.agent, context, plan)
    assert failure.value.code == "COMPACT_SOURCE_CHANGED", "非 typed 失败原样上抛"
    monkeypatch.setattr(budget_module, "generate_bounded_compact_response",
                        lambda *_a, **_k: SimpleNamespace(text="", tool_use_blocks=[{"name": "x"}], truncated=False))
    outcome = generate_media_digest(case.agent, context, plan)
    assert (outcome.summarized_blocks, outcome.failure_code) == (0, COMPACT_VISION_SUMMARY_FAILED), "工具调用/空回复算失败"


def test_partial_digest_writes_both_counts_and_partial_reason_to_checkpoint(vision_case, proportional_estimate, monkeypatch):  # noqa: F811
    case = vision_case
    rows = _two_media_turns(case)
    groups = media_turn_groups(rows)
    per_group = 40 * _messages(groups[0]) + 50
    monkeypatch.setattr(budget_module, "compact_summary_budget", lambda _agent: per_group + 30)
    case.agent.config.compact_vision_digest_max_requests = 1
    result = _compact(case, rows)
    assert result.compacted
    digest_calls = [call for call in case.calls if call["vision_summary"]]
    assert len(digest_calls) == 1 and SHA_ONE in json.dumps(digest_calls[0]["messages"]) and SHA_TWO not in json.dumps(digest_calls[0]["messages"])
    summary = case.calls[-1]
    assert summary["vision_summary"] is False and f"sha256:{SHA_ONE[:12]}" in summary["prompt"]
    summary_text = json.dumps(summary["messages"], ensure_ascii=False)
    assert summary_text.count("附件引用") == 2 and "local_file" not in summary_text, "两个图块在文字摘要里都是归档引用"
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["media_policy"] == "vision_summary" and checkpoint["media_policy_reason"] == MEDIA_REASON_DIGEST_PARTIAL
    assert (checkpoint["media_blocks_summarized"], checkpoint["media_blocks_archived"]) == (1, 1)
    assert checkpoint["media_refs"] == [SHA_ONE, SHA_TWO]


def test_summarize_media_turns_is_a_noop_without_media_or_without_a_vision_decision(vision_case, monkeypatch):  # noqa: F811
    case = vision_case
    context = MediaDigestContext(surface=SURFACE)
    monkeypatch.setattr(budget_module, "generate_bounded_compact_response", lambda *_a, **_k: pytest.fail("不该发请求"))
    text_only = [_append(case, "user", "问", "turn-a"), _append(case, "assistant", "答", "turn-a")]
    decision = CompactMediaDecision("vision_summary", "declared")
    assert summarize_media_turns(case.agent, context, text_only, decision, max_requests=4) == (decision, "")
    archived = CompactMediaDecision("archived_refs", "declared")
    assert summarize_media_turns(case.agent, context, _two_media_turns(case), archived, max_requests=4) == (archived, "")
