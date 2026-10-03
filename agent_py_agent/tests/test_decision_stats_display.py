"""决策统计的展示与汇总口径（2026-09-28）：讲清 TUI 里的“缺报”，把“没发出去”和“发出去了但超时”分开。

起因：用户看到“决策模型成功 140 多、失败 63，还有一些缺报”。超时调用在用量里是 0 token，被一并算作缺报；
pre_recall 与 recall 共用阶段预算，排在后面的 pre_recall 常常一开始就 budget_exhausted（1–4 毫秒返回、请求根本没发出），
却被算成 Jev 超时，拉低成功率。

本文件锁定（只改展示与汇总，不改记账）：
- TUI 决策段三种数据来源分开标注：供应商有回报（已报）、只有估算（估算 N token（未完成））、两者都没有（作为括号说明挂在
  所属的成功/失败次数上，不另起一个会被加总的“缺报”数；ae 复审建议）；
- 一次 HTTP 尝试都没有的失败单列“未发出”，不计入失败；
- 非决策调用（LLM 段）用同一组函数、同一口径，数字 = 全部用途 − 决策分区，同一次调用不在两段重复出现；
- 拆分用跨事件累加后的原始次数推导（迟到的尝试落在后一条事件里也不会算错），旧账分不清是否发出时整体仍算失败；
- 结果日志按点位汇总时，没发出去的失败类结果单列 not_sent，有链路事实的行按有没有 HTTP 尝试判定；
- 审计用量行给出与 TUI 同口径的 jev_failures / not_sent_calls；
- 失败构成出现前写下的旧快照（没有 decision_unknown_failures 键）按旧账规则把失败记为分不清是否发出，
  不能被补成 0 后说成“未发出”（9b 复审）。下面手写的新格式快照都显式带上失败构成两键。
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.call_runtime import (
    ModelCallTimeoutFacts,
    model_call_summary,
    record_model_call_finished,
    record_model_call_timeout,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.decision_audit import decision_usage_summary
from agent_py_agent.agent.conversation.decision_outcome_log import SCHEMA, decision_outcome_summary
from agent_py_agent.agent.conversation.model_metrics import (
    model_metrics_from_thread,
    public_model_metrics,
    publish_model_metrics,
    split_unsent_failures,
    unfinished_usage_facts,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics


# 函数用途: 用给定字段渲染一条统计行并取出纯文本（宽度足够，不裁剪）。
def _line(**fields) -> str:
    metrics = {"schema": "model_runtime_metrics.v1", "totals_known": True, **fields}
    return "".join(part[1] for line in render_model_metrics(metrics, 400) for part in line)


def test_provider_reported_calls_show_only_the_reported_figure():
    text = _line(decision_call_count=1, decision_success_count=1, decision_input_reported_calls=1,
                 decision_input_tokens=120, decision_unfinished_calls=0, decision_unknown_failures=0)
    assert "决策 已报 120 token · 成功 1 · 失败 0" in text
    assert "估算" not in text and "缺报" not in text and "未发出" not in text


def test_partially_reported_success_is_extrapolated_without_a_missing_note():
    # 有一部分成功调用回报了输入：按已报平均值外推并带 ≈，不挂“未回报用量”说明（那只留给一次都没回报的情况）
    text = _line(decision_call_count=3, decision_success_count=3, decision_input_reported_calls=1, decision_input_tokens=50,
                 decision_unfinished_calls=0, decision_unknown_failures=0)
    assert "决策 已报 ≈150 token · 成功 3 · 失败 0" in text and "未回报" not in text


def test_estimate_only_is_labelled_unfinished_and_is_not_reported_as_missing():
    text = _line(failure_count=1, unfinished_calls=1, unfinished_tokens=321, decision_call_count=1,
                 decision_failure_count=1, decision_unfinished_calls=1, decision_unknown_failures=0,
                 decision_estimated_tokens=321)
    assert "决策 估算 321 token（未完成） · 成功 0 · 失败 1" in text
    assert "缺报" not in text and "LLM" not in text, "有估算的超时调用不算缺报，决策调用也不在 LLM 段重复出现"


def test_calls_without_any_data_are_explained_inside_their_own_count():
    # 次数互不重叠：分不清是否发出的旧账失败只作为“失败”的括号说明，不再另起一个会与失败重复计数的“缺报”
    legacy = _line(failure_count=3, unknown_failures=3, decision_call_count=4, decision_success_count=1,
                   decision_input_reported_calls=1, decision_input_tokens=80, decision_failure_count=3,
                   decision_unfinished_calls=0, decision_unknown_failures=3)
    assert "决策 已报 80 token · 成功 1 · 失败 3（其中 3 次分不清是否发出）" in legacy
    assert "缺报" not in legacy and "LLM" not in legacy, "同一次决策调用只在决策段讲一次"
    unreported = _line(decision_call_count=2, decision_success_count=2, decision_unfinished_calls=0,
                       decision_unknown_failures=0)
    assert "决策 成功 2（其中 2 次未回报用量） · 失败 0" in unreported and "缺报" not in unreported


def test_unsent_failures_are_listed_apart_and_not_counted_as_failures():
    text = _line(failure_count=2, decision_call_count=3, decision_success_count=1, decision_input_reported_calls=1,
                 decision_input_tokens=50, decision_failure_count=2, decision_unfinished_calls=0,
                 decision_unknown_failures=0)
    assert "决策 已报 50 token · 成功 1 · 失败 0 · 未发出 2" in text and "LLM" not in text


def test_llm_calls_use_the_same_rule_estimate_unsent_and_missing():
    # 非决策调用（主模型、辅助调用）与决策段同一口径：发出去之后超时的显示估算，没发出去的单列，分不清的旧账才算缺报
    sent = _line(failure_count=1, unfinished_calls=1, unfinished_tokens=5000)
    assert "LLM 估算 5.0k token（未完成）" in sent and "缺报" not in sent and "未发出" not in sent
    assert "LLM 未发出 2" in _line(failure_count=2)
    legacy = _line(failure_count=2, unknown_failures=2)
    assert "LLM 缺报 2" in legacy and "未发出" not in legacy
    finished = _line(estimated_tokens=40)
    assert "会话累计 ~40" in finished and "缺报" not in finished, "成功但没回报的已按本地估算计入会话累计，不再算缺报"


def test_llm_part_excludes_the_decision_share():
    # 四项都要减去决策分区；数值取不会被 k 单位抹平的，漏减任何一项都看得出来
    text = _line(failure_count=6, unfinished_calls=2, unfinished_tokens=542, unknown_failures=2, decision_call_count=3,
                 decision_failure_count=3, decision_unfinished_calls=1, decision_estimated_tokens=42,
                 decision_unknown_failures=1)
    assert "决策 估算 42 token（未完成） · 成功 0 · 失败 2（其中 1 次分不清是否发出） · 未发出 1" in text
    assert "LLM 估算 500 token（未完成） · 未发出 1 · 缺报 1" in text


def test_split_uses_totals_across_events_and_keeps_legacy_failures_as_failures():
    first = {"status_counts": {"timed_out": 1}, "usage_breakdown": {"estimated": {"unfinished_call_count": 0}}}
    late = {"status_counts": {}, "usage_breakdown": {"estimated": {"unfinished_call_count": 1, "unfinished_input_tokens": 42}}}
    facts = [unfinished_usage_facts(item) for item in (first, late)]
    totals = {key: sum(item[key] for item in facts) for key in facts[0]}
    assert split_unsent_failures(totals["failures"], totals["unfinished_calls"], totals["unknown_failures"]) == (1, 0)
    # 逐条算会把第一条事件误判为“没发出去”：所以只累加原始次数、在展示/汇总时推导
    assert split_unsent_failures(facts[0]["failures"], facts[0]["unfinished_calls"], facts[0]["unknown_failures"]) == (0, 1)
    legacy = {"status_counts": {"failed": 2}, "usage_breakdown": {"estimated": {"call_count": 0}}}
    assert unfinished_usage_facts(legacy) == {"failures": 2, "unfinished_calls": 0, "unfinished_tokens": 0,
                                              "unknown_failures": 2}
    assert split_unsent_failures(2, 0, 2) == (2, 0), "旧账分不清是否发出，整体仍算失败"


# 函数用途: 写一份结果日志（每行一条结构化结果，无正文）。
def _outcomes(tmp_path, rows) -> SimpleNamespace:
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    path.write_text("".join(json.dumps({"schema": SCHEMA, "created_at": now, **row}) + "\n" for row in rows),
                    encoding="utf-8")
    return SimpleNamespace(owner_decision_outcomes_jsonl=path)


def test_outcome_summary_lists_unsent_results_apart_from_jev_timeouts(tmp_path):
    sent, unsent = {"attempts": [{"status": "failed"}]}, {"attempts": []}
    home = _outcomes(tmp_path, [
        {"point": "recall", "status": "success", "transport": {"attempts": [{"status": "response_opened"}]}},
        {"point": "recall", "status": "deadline", "reason": "provider_failed", "transport": sent},
        {"point": "recall", "status": "deadline", "reason": "provider_failed", "transport": unsent},
        {"point": "recall", "status": "error", "reason": "admission_busy", "transport": unsent},
        {"point": "recall", "status": "error", "reason": "admission_busy"},
        {"point": "recall", "status": "deadline", "reason": "provider_failed"},
        {"point": "pre_recall", "status": "deadline", "reason": "budget_exhausted"},
        {"point": "pre_recall", "status": "cooldown", "reason": "point_backoff"},
        {"point": "curator", "status": "configuration_required", "reason": "connection_backoff"},
        {"point": "curator", "status": "configuration_required", "reason": "provider_failed", "transport": sent},
        {"point": "planning", "status": "skipped", "reason": "privacy_url"},
        {"point": "planning", "status": "stale", "reason": "host_shutdown"},
    ])
    summary = decision_outcome_summary(home, since=0)

    # 发出去之后超时/失败的才留在 points，算 Jev 超时率与失败率；旧行没有链路事实时按原因码认出没发出去的
    assert summary["points"] == {"recall": {"success": 1, "deadline": 2}, "curator": {"configuration_required": 1},
                                 "planning": {"skipped": 1, "stale": 1}}
    assert summary["not_sent"] == {"recall": {"provider_failed": 1, "admission_busy": 2},
                                   "pre_recall": {"budget_exhausted": 1, "point_backoff": 1},
                                   "curator": {"connection_backoff": 1}}
    assert len(summary["recent"]) == 12, "最近行照原样保留，不因拆分丢行"


# LLM: 用真实原账本生成一个用途分区（purpose 走结构化 metadata，与宿主一致）：成功带已报输入；sent 次超时前记过
#   HTTP 尝试，unsent 次一次尝试都没有；每次调用的发送前估算都是 42。
# 函数用途: 生成一份 model_calls 摘要，默认是决策调用。
def _ledger_calls(*, finished: int, sent: int, unsent: int, purpose: str = "decision") -> dict:
    ledger = ModelCallLedger()
    for index in range(finished + sent + unsent):
        call_id = f"{purpose}-{index}"
        ledger.started(ModelCallStartedParams(call_id, "typesafe_decision", "jev", 42, request_id="req", run_id="run",
                                              metadata={"purpose": purpose, "logical_call_id": call_id}))
        if index < finished:
            record_model_call_finished(ledger, call_id, SimpleNamespace(usage={"input_tokens": 100}))
            continue
        if index < finished + sent:
            ledger.provider_attempt(ModelCallProviderAttemptParams(call_id, f"http-{index}", "started"))
        record_model_call_timeout(
            ledger,
            ModelCallTimeoutFacts(call_id=call_id, timeout_seconds=1.0, timeout_stage="wall_clock"),
        )
    return model_call_summary(SimpleNamespace(_model_call_ledger=ledger), request_id="req", run_id="run")


# 函数用途: 去掉摘要里全部 unfinished 键，模拟链路计时上线前写下的旧用量事件。
def _legacy(summary: dict) -> dict:
    legacy = deepcopy(summary)
    buckets = [legacy] + [row for key, row in legacy["purpose_breakdown"].items() if key != "schema"]
    for bucket in buckets:
        for key in ("unfinished_input_tokens", "unfinished_call_count"):
            bucket["usage_breakdown"]["estimated"].pop(key)
    return legacy


def test_audit_usage_reports_jev_failures_and_unsent_calls_like_the_tui(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "u", "owner_id": "u"})
    store.model_usage.append_snapshot_once({"thread_id": thread.thread_id, "request_id": "r1", "run_id": "run",
        "task_id": "t", "source": "test", "model_calls": _ledger_calls(finished=2, sent=1, unsent=2)})
    store.model_usage.append_once({"event_id": "legacy-1", "thread_id": thread.thread_id, "request_id": "old",
        "run_id": "old", "source": "old", "model_calls": _legacy(_ledger_calls(finished=0, sent=2, unsent=0))})

    usage = decision_usage_summary(store, [thread.thread_id], since=0)

    assert (usage["finished"], usage["timed_out"], usage["failed"]) == (2, 5, 0), "原始计数不变"
    assert (usage["jev_failures"], usage["not_sent_calls"], usage["failures_send_unknown"]) == (3, 2, 2)
    assert (usage["estimated_unfinished_calls"], usage["input_tokens_estimated_unfinished"]) == (1, 42)
    assert usage["input_tokens_reported"] == 200
    row = usage["threads"][0]
    assert (row["jev_failures"], row["not_sent_calls"]) == (3, 2)


# 生产版（step15t，main 6c2fad4da 及以前）落在 thread.model_metrics 里的快照形状：决策只有三项计数，没有失败构成两键
_PRODUCTION_SNAPSHOT = {
    "schema": "model_runtime_metrics.v1", "model_rounds": 3, "retry_count": 0, "input_tokens": 9000,
    "output_tokens": 800, "estimated_tokens": 0, "unreported_calls": 4, "sampled_at_ns": 1, "tool_count": 2,
    "cache_percent": 50.0, "output_tps": None, "pending": False, "totals_known": True,
    "decision_call_count": 5, "decision_input_tokens": 300, "decision_input_reported_calls": 3,
    "decision_success_count": 3, "decision_failure_count": 2,
}


def test_production_shape_snapshot_keeps_old_failures_as_failures(tmp_path):
    # 9b 复审：旧快照缺失败构成两键时，若补成 0，2 次旧失败会显示成“失败 0 · 未发出 2”（说成根本没发出去）
    public = public_model_metrics(dict(_PRODUCTION_SNAPSHOT))
    assert (public["decision_unfinished_calls"], public["decision_unknown_failures"]) == (0, 2), "与旧用量行同一规则"
    text = _line(**_PRODUCTION_SNAPSHOT)
    assert "决策 已报 300 token · 成功 3 · 失败 2（其中 2 次分不清是否发出）" in text
    assert "未发出" not in text and "LLM" not in text, "旧快照没有全部用途的失败构成，LLM 段留空而不是乱报"

    # 重连、切入子代理页面读回的是落盘的原始旧快照，同样按旧账口径显示
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "u", "owner_id": "u"})
    store.model_usage._update_thread_atomic(thread.thread_id,
                                            lambda item: replace(item, model_metrics=dict(_PRODUCTION_SNAPSHOT)))
    persisted = "".join(part[1] for line in render_model_metrics(
        model_metrics_from_thread(store, thread.thread_id), 400) for part in line)
    assert "失败 2（其中 2 次分不清是否发出）" in persisted and "未发出" not in persisted


def test_llm_segment_flows_from_ledger_events_through_the_publisher(tmp_path):
    # 主模型 1 次发出去后超时（有估算）、1 次没发出去；旧账主模型 2 次失败分不清是否发出；决策 1 次发出去后超时，只在决策段讲
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "u", "owner_id": "u"})
    events = (("main", _ledger_calls(finished=0, sent=1, unsent=1, purpose="main")),
              ("decision", _ledger_calls(finished=0, sent=1, unsent=0)),
              ("legacy", _legacy(_ledger_calls(finished=0, sent=2, unsent=0, purpose="main"))))
    for event_id, calls in events:
        store.model_usage.append_once({"event_id": event_id, "thread_id": thread.thread_id, "request_id": event_id,
                                       "run_id": event_id, "source": "test", "model_calls": calls})
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ModelCallLedger())
    params = SimpleNamespace(request_id="now", run_id="now", live_archive_state={},
                             task_attributes={"conversation_thread_id": thread.thread_id})

    metrics = publish_model_metrics(agent, params, pending=False)
    text = "".join(part[1] for line in render_model_metrics(metrics, 400) for part in line)

    assert "决策 估算 42 token（未完成） · 成功 0 · 失败 1" in text
    assert "LLM 估算 42 token（未完成） · 未发出 1 · 缺报 2" in text
    # MU1（9b 复审）：未完成调用的发送前估算只在估算段显示，不能算进会话累计
    assert metrics["unfinished_tokens"] == 84 and "会话累计 0 · " in text
