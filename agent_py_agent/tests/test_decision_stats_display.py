"""决策统计的展示与汇总口径（2026-09-28）：讲清 TUI 里的“缺报”，把“没发出去”和“发出去了但超时”分开。

起因：用户看到“决策模型成功 140 多、失败 63，还有一些缺报”。超时调用在用量里是 0 token，被一并算作缺报；
pre_recall 与 recall 共用阶段预算，排在后面的 pre_recall 常常一开始就 budget_exhausted（1–4 毫秒返回、请求根本没发出），
却被算成 Jev 超时，拉低成功率。

本文件锁定（只改展示与汇总，不改记账）：
- TUI 决策段三种数据来源分开标注：供应商有回报（已报）、只有估算（估算 N token（未完成））、两者都没有（缺报）；
- 一次 HTTP 尝试都没有的失败单列“未发出”，不计入失败；总行“缺报”不再重复计决策调用；
- 拆分用跨事件累加后的原始次数推导（迟到的尝试落在后一条事件里也不会算错），旧账分不清是否发出时整体仍算失败；
- 结果日志按点位汇总时，没发出去的失败类结果单列 not_sent，有链路事实的行按有没有 HTTP 尝试判定；
- 审计用量行给出与 TUI 同口径的 jev_failures / not_sent_calls。
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.call_runtime import (
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
                 decision_input_tokens=120)
    assert "决策 已报 120 token · 成功 1 · 失败 0" in text
    assert "估算" not in text and "缺报" not in text and "未发出" not in text


def test_estimate_only_is_labelled_unfinished_and_is_not_reported_as_missing():
    text = _line(decision_call_count=1, decision_failure_count=1, decision_unfinished_calls=1,
                 decision_estimated_tokens=321, unreported_calls=1, decision_unreported_calls=1)
    assert "决策 估算 321 token（未完成） · 成功 0 · 失败 1" in text
    assert "缺报" not in text, "有估算的超时调用不能再算缺报，总行也不能重复计"


def test_calls_with_neither_report_nor_estimate_keep_missing_once():
    text = _line(decision_call_count=1, decision_failure_count=1, decision_unknown_failures=1,
                 unreported_calls=1, decision_unreported_calls=1)
    assert "决策 缺报 1 · 成功 0 · 失败 1" in text
    assert text.count("缺报") == 1, "决策段已讲清，总行不再重复计同一次决策调用"


def test_unsent_failures_are_listed_apart_and_not_counted_as_failures():
    text = _line(decision_call_count=3, decision_success_count=1, decision_input_reported_calls=1,
                 decision_input_tokens=50, decision_failure_count=2)
    assert "决策 已报 50 token · 成功 1 · 失败 0 · 未发出 2" in text


def test_other_models_keep_their_missing_count_without_the_decision_share():
    text = _line(unreported_calls=3, decision_unreported_calls=1, decision_call_count=1, decision_failure_count=1,
                 decision_unfinished_calls=1, decision_estimated_tokens=7)
    assert "决策 估算 7 token（未完成） · 成功 0 · 失败 1" in text and " · 缺报 2" in text
    assert "缺报 2" in _line(unreported_calls=2), "没有决策调用时总行口径与原来相同"


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


# LLM: 用真实原账本生成决策用途分区：成功带已报输入；sent 次超时前记过 HTTP 尝试，unsent 次一次尝试都没有。
# 函数用途: 生成一份决策调用的 model_calls 摘要。
def _decision_calls(*, finished: int, sent: int, unsent: int) -> dict:
    ledger = ModelCallLedger()
    for index in range(finished + sent + unsent):
        call_id = f"decision-{index}"
        ledger.started(ModelCallStartedParams(call_id, "typesafe_decision", "jev", 42, request_id="req", run_id="run",
                                              metadata={"purpose": "decision", "logical_call_id": call_id}))
        if index < finished:
            record_model_call_finished(ledger, call_id, SimpleNamespace(usage={"input_tokens": 100}))
            continue
        if index < finished + sent:
            ledger.provider_attempt(ModelCallProviderAttemptParams(call_id, f"http-{index}", "started"))
        record_model_call_timeout(ledger=ledger, call_id=call_id, timeout_seconds=1.0, timeout_stage="wall_clock")
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
        "task_id": "t", "source": "test", "model_calls": _decision_calls(finished=2, sent=1, unsent=2)})
    store.model_usage.append_once({"event_id": "legacy-1", "thread_id": thread.thread_id, "request_id": "old",
        "run_id": "old", "source": "old", "model_calls": _legacy(_decision_calls(finished=0, sent=2, unsent=0))})

    usage = decision_usage_summary(store, [thread.thread_id], since=0)

    assert (usage["finished"], usage["timed_out"], usage["failed"]) == (2, 5, 0), "原始计数不变"
    assert (usage["jev_failures"], usage["not_sent_calls"], usage["failures_send_unknown"]) == (3, 2, 2)
    assert (usage["estimated_unfinished_calls"], usage["input_tokens_estimated_unfinished"]) == (1, 42)
    assert usage["input_tokens_reported"] == 200
    row = usage["threads"][0]
    assert (row["jev_failures"], row["not_sent_calls"]) == (3, 2)
