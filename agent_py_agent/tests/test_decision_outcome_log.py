"""决策结果日志：decide() 的每个结果按接入点落一行结构化记录（无正文），审计按点位汇总。

2026-09-26 真机：TUI 里的模型用 audit_records 只看到选模型的观察，就断言其余接入点"没接线"。实际是其余点位的结果没有
任何持久记录，冷却跳过更不留痕；选模型每次最先超时又把整条连接冷却到 300 秒。本测试锁定：成功、超时、点位冷却都会落日志，
日志不含状态、题目或候选正文，审计的 decision 主题按点位给出次数。
"""
import json
import time
from types import SimpleNamespace

from agent_py_agent.agent.conversation import decision_outcome_log, decision_service
from agent_py_agent.agent.conversation.decision_outcome_log import (
    SCHEMA,
    append_decision_outcome,
    decision_outcome_row,
    decision_outcome_summary,
)
from agent_py_agent.agent.tooling.audit_records_tool import AuditQuery, _decision_owner_report
from agent_py_agent.tests.test_decision_curator_plugin_concurrency import (  # noqa: F401  复用本地假决策服务与宿主夹具
    configured,
    decide_background,
    decide_foreground,
    fresh_host,
    lanes,
)


# 函数用途: 造一个只有结构化字段的决策结果替身。
def _outcome(status="success", reason=""):
    return SimpleNamespace(mode="observe", status=status, reason=reason)


# 函数用途: 造一个决策阶段身份替身。
def _stage():
    return SimpleNamespace(scope="thread", thread_id="thread-1", run_id="run-1", task_id="task-1", experiment=False)


def test_row_keeps_only_structured_facts():
    row = decision_outcome_row(_stage(), "recall", _outcome("deadline", "provider_failed"), 1.2345)

    assert set(row) == {"schema", "created_at", "point", "scope", "mode", "status", "reason", "elapsed_ms",
                        "thread_id", "run_id", "task_id", "experiment"}
    assert (row["point"], row["status"], row["reason"], row["elapsed_ms"]) == ("recall", "deadline", "provider_failed", 1234)


def test_append_writes_only_the_owner_path_and_stays_bounded(tmp_path, monkeypatch):
    path = tmp_path / "data" / "decision" / "outcomes.jsonl"
    # 没有规范路径的宿主（旧替身、无 owner 的入口）不写任何文件。
    append_decision_outcome(SimpleNamespace(home_paths=SimpleNamespace()), decision_outcome_row(_stage(), "recall", _outcome(), 0))
    assert not path.exists()

    monkeypatch.setattr(decision_outcome_log, "_MAX_RECORDS", 3)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=path))
    for index in range(5):
        append_decision_outcome(agent, decision_outcome_row(_stage(), f"p{index}", _outcome(), 0))

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["point"] for row in rows] == ["p2", "p3", "p4"]


def test_summary_counts_by_point_inside_the_window_and_counts_bad_rows(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    rows = [{"created_at": now - 10, "point": "recall", "status": "success"},
            {"created_at": now - 5, "point": "recall", "status": "cooldown"},
            {"created_at": now - 7200, "point": "planning", "status": "success"}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, **row}) + "\n" for row in rows) + "{not json\n", encoding="utf-8")

    summary = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 3600)

    assert summary["points"] == {"recall": {"success": 1, "cooldown": 1}}
    assert summary["unreadable_rows"] == 1 and len(summary["recent"]) == 2
    assert decision_outcome_summary(SimpleNamespace(), since=0)["available"] is False


def test_decide_logs_success_timeout_and_point_backoff_and_audit_reports_points(tmp_path, lanes):  # noqa: F811
    env = configured(tmp_path, lanes, background_timeout=0.2)
    path = tmp_path / "owner-data" / "decision" / "outcomes.jsonl"
    env.host.home_paths.owner_decision_outcomes_jsonl = path
    lanes.blocked = {"curator"}

    decide_background(env)
    decide_foreground(env)
    env.bg_stage = decision_service.begin_decision_stage(env.host, env.background, operation_id="curator-lease-2",
                                                         scope="owner_background")
    decide_background(env)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(row["point"], row["status"], row["reason"]) for row in rows] == [
        ("curator", "deadline", "provider_failed"), ("skill_tool", "success", ""), ("curator", "cooldown", "point_backoff")]
    # 请求材料里的 lane 标记只在发给决策服务的请求体里，日志里不能出现。
    assert "lane" not in path.read_text(encoding="utf-8")

    query = AuditQuery(topic="decision", scope="owner", thread_id="", since=0.0, limit=20)
    report = _decision_owner_report("alice", env.host, [env.thread.thread_id], query)
    assert report["points"]["points"] == {"curator": {"deadline": 1, "cooldown": 1}, "skill_tool": {"success": 1}}
