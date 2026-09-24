"""F1a 证据评估：只读请求记录里的实验条目，按固定规则给出 skill_tool off→apply 建议或继续观察；
证据链只沿授权回执指针回读原请求记录，读路径经 user_config decision_read 只读暴露。无网络、无真实模型。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.decision_experiment_evaluation import (
    evaluate_skill_tool_samples,
    shortlist_recall,
)
from agent_py_agent.agent.gateway_parts import request_experiment_records as records
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_decision_experiment_promotion import (
    lane_at,
    prior_sample,
    start_turn,
)

OWNER, THREAD = "owner-ref", "thread-1"


# LLM: 与生产 decision_experiment_record.v1 同形的已完成样本；只替换评估需要的结构化字段。
# 函数用途: 生成一条已补写实际用量的合成实验记录。
def done(record_id, *, outcome="charged", tools=("presentation_optional_a",), deferred=("presentation_optional_b",)):
    entry = {"schema": "decision_experiment_record.v1", "record_id": record_id, "status": "completed", "point": "skill_tool",
             "refs": {"owner_ref": OWNER, "thread_id": THREAD, "request_id": "req-" + record_id},
             "settlement": {"outcome": outcome},
             "candidate": {"status": "projected", "shortlist_names": ["presentation_optional_a", "read_file"],
                           "deferred_names": list(deferred), "deferred_count": len(deferred), "names_truncated": False},
             "realized": {"known": True, "tool_names": list(tools), "names_truncated": False}}
    return entry


def evaluate(entries):
    return evaluate_skill_tool_samples(entries, owner_ref=OWNER, thread_id=THREAD)


def good(count):
    return [done(f"s{index}") for index in range(count)]


def test_three_comparable_charged_samples_with_full_recall_and_savings_propose_apply():
    result = evaluate(good(3))
    assert result["status"] == "proposal" and result["reasons"] == []
    assert result["proposal"] == {"point": "skill_tool", "field": "points.skill_tool.mode", "from": "off", "to": "apply",
                                  "scope": "thread"}
    assert (result["sample_count"], result["comparable_count"]) == (3, 3)
    assert all(sample["shortlist_recall"] == 1.0 and sample["comparable"] for sample in result["samples"])


@pytest.mark.parametrize("extra,reasons", [
    ([], ["insufficient_samples"]),
    ([done("miss", tools=("presentation_optional_b",))], ["recall_below_one"]),
    ([done("half", tools=("presentation_optional_a", "presentation_optional_b"))], ["recall_below_one"]),
    ([done("flat", deferred=())], ["no_savings"]),
    ([done("unknown", outcome="usage_unknown")], ["settlement_not_charged"]),
    ([done("refused", outcome="send_refused")], ["settlement_not_charged"]),
    ([done("bypass", outcome="gate_bypassed")], ["settlement_not_charged"]),
    ([done("failed", outcome="settlement_failed")], ["settlement_not_charged"]),
])
def test_rule_matrix_keeps_observing_with_structured_reasons(extra, reasons):
    base = good(2) if not extra else good(3)
    result = evaluate([*extra, *base])
    assert result["status"] == "keep_observing" and result["reasons"] == reasons and result["proposal"] is None


@pytest.mark.parametrize("sample,reason", [
    ({**done("u"), "realized": {"known": False, "reason": "turn_not_completed"}}, "realized_unknown"),
    (done("t", tools=()), "no_realized_tools"),
    (done("o", tools=("outside_tool",)), "no_realized_tools"),
    ({**done("r"), "candidate": {"status": "retained", "reason": "retained:abstain"}}, "candidate_unavailable"),
    ({**done("c", tools=("zz_tool",)), "candidate": {"status": "projected", "shortlist_names": ["a"], "deferred_names": ["b"],
                                                    "deferred_count": 300, "names_truncated": True}}, "candidate_names_truncated"),
    ({**done("n"), "realized": {"known": True, "tool_names": ["a"], "names_truncated": True}}, "realized_names_truncated"),
])
def test_non_comparable_samples_neither_count_nor_block(sample, reason):
    result = evaluate([sample, *good(2)])
    [view] = [item for item in result["samples"] if item["record_id"] == sample["record_id"]]
    assert view["comparable"] is False and reason in view["reasons"]
    assert result["reasons"] == ["insufficient_samples"] and result["comparable_count"] == 2
    assert evaluate([sample, *good(3)])["status"] == "proposal"


def test_tools_outside_the_snapshot_do_not_dilute_recall():
    assert shortlist_recall(done("x")["candidate"], {"known": True, "tool_names": ["presentation_optional_a", "outside"],
                                                     "names_truncated": False}) == (1.0, 0, "")
    candidate = {**done("x")["candidate"], "names_truncated": True}
    assert shortlist_recall(candidate, {"known": True, "tool_names": ["presentation_optional_a", "outside"],
                                        "names_truncated": False}) == (None, 0, "candidate_names_truncated")
    assert shortlist_recall(candidate, {"known": True, "tool_names": ["presentation_optional_a"],
                                        "names_truncated": False}) == (1.0, 0, "")


@pytest.mark.parametrize("change", [{"refs": {"owner_ref": "other-owner", "thread_id": THREAD}},
                                    {"refs": {"owner_ref": OWNER, "thread_id": "other-thread"}},
                                    {"point": "planning"}, {"schema": "decision_experiment_record.v0"},
                                    {"status": "observed"}, {"record_id": ""}])
def test_evaluator_ignores_foreign_incomplete_or_malformed_entries(change):
    foreign = [{**done(f"f{index}"), **change} for index in range(3)]
    result = evaluate([*foreign, *good(2)])
    assert result["status"] == "keep_observing" and result["sample_count"] == 2


def test_duplicate_records_count_once_and_window_keeps_the_newest_eight():
    duplicated = evaluate([done("same"), done("same"), done("same")])
    assert duplicated["sample_count"] == 1 and duplicated["reasons"] == ["insufficient_samples"]
    windowed = evaluate([*good(8), done("old", outcome="usage_unknown")])
    assert windowed["sample_count"] == 8 and windowed["status"] == "proposal"


# LLM: 真实原请求目录；每条请求记录只含本测试需要的授权回执与实验记录块。
# 函数用途: 在指定目录写一份带授权回执与实验条目的请求记录。
def put(folder, request_id, *, previous="", entries=()):
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "experiment_grant": {"status": "granted", "thread_id": THREAD, "operations": ["observe"],
                                                      "previous_request_id": previous},
               "experiment_records": {"entries": list(entries)}}
    (folder / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_chain_follows_grant_pointers_across_request_folders_newest_first(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    put(paths.failed, "req-a", entries=[done("a")])
    put(paths.done, "req-b", previous="req-a", entries=[done("b")])
    put(paths.terminal, "req-c", previous="req-b", entries=[done("c1"), done("c2")])
    head = put(paths.processing, "req-d", previous="req-c", entries=[done("d")])
    ids = [entry["record_id"] for entry in records.experiment_chain_entries(paths, head, thread_id=THREAD)]
    assert ids == ["d", "c2", "c1", "b", "a"]


@pytest.mark.parametrize("pointer", ["../req-a", "../terminal/req-a", ".hidden", "", "req-missing"])
def test_chain_stops_on_invalid_or_missing_pointer(tmp_path, pointer):
    paths = gateway_paths_from_root(tmp_path)
    put(paths.terminal, "req-a", entries=[done("a")])
    # 目录外放一份 id 恰好等于越界指针的记录：若编号校验失效，证据链会越出请求目录把它读进来。
    outside = put(paths.terminal.parent, "req-a", entries=[done("outside")])
    (paths.terminal.parent / "req-a.json").write_text(json.dumps({**outside, "id": pointer}), encoding="utf-8")
    (paths.terminal / ".hidden.json").write_text(json.dumps({**outside, "id": ".hidden"}), encoding="utf-8")
    head = put(paths.processing, "req-b", previous=pointer, entries=[done("b")])
    assert [entry["record_id"] for entry in records.experiment_chain_entries(paths, head, thread_id=THREAD)] == ["b"]
    # 读路径的链头来自授权信封来源编号，加载器自身也必须拒绝越界编号，不能只靠链指针校验。
    assert records.load_gateway_request_record(paths, pointer) is None


def test_chain_stops_on_cross_thread_grant_cycles_and_its_bound(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    foreign = put(paths.terminal, "req-x", previous="req-y", entries=[done("x")])
    foreign["experiment_grant"]["thread_id"] = "other-thread"
    (paths.terminal / "req-x.json").write_text(json.dumps(foreign), encoding="utf-8")
    put(paths.terminal, "req-y", entries=[done("y")])
    head = put(paths.processing, "req-z", previous="req-x", entries=[done("z")])
    assert [e["record_id"] for e in records.experiment_chain_entries(paths, head, thread_id=THREAD)] == ["z"]
    assert records.experiment_chain_entries(paths, head, thread_id="other-thread") == []
    loop = put(paths.terminal, "req-l", previous="req-l", entries=[done("l")])
    assert [e["record_id"] for e in records.experiment_chain_entries(paths, loop, thread_id=THREAD)] == ["l"]
    for index in range(20):
        put(paths.terminal, f"req-{index}", previous=f"req-{index - 1}" if index else "", entries=[done(f"n{index}")])
    chained = records.experiment_chain_entries(paths, put(paths.processing, "req-top", previous="req-19"), thread_id=THREAD)
    assert len(chained) == 15 and chained[0]["record_id"] == "n19"


# LLM: 用 user_config 原 decision_read 路径与真实设置服务；宿主运行参数携带请求写入器（与 Gateway 主轮同形）。
# 函数用途: 以已授权会话执行一次 decision_read 并返回解析后的输出。
def read_through_tool(host, thread_id, writer):
    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": thread_id},
                                               conversation_task_binding_callback=writer)
    outcome = UserConfigTool(host).execute({"action": "decision_read", "scope": "thread"})
    assert outcome.ok, outcome.output
    return outcome.output


def test_decision_read_exposes_read_only_evaluation_after_the_authorization(tmp_path):
    lane = lane_at(tmp_path)
    for index in range(3):
        prior_sample(lane, f"req-{index}", "observe")
    writer = start_turn(lane, "req-now", "observe").writer
    output = read_through_tool(lane.host, lane.thread.thread_id, writer)
    report = json.loads(output)
    keys = list(report)
    assert keys[keys.index("experiment_authorization") + 1] == "experiment_evaluation"
    evaluation = report["experiment_evaluation"]
    assert evaluation["status"] == "proposal" and evaluation["comparable_count"] == 3
    assert evaluation["authorized_operations"] == ["observe"] and evaluation["latest_promotion"] is None
    assert report["effective"]["points"]["skill_tool"]["effective_mode"] == "off", "读取不能触发晋升"


def test_decision_read_without_authorization_or_reader_is_unchanged(tmp_path):
    lane = lane_at(tmp_path)
    thread_id = lane.thread.thread_id
    plain = read_through_tool(lane.host, thread_id, None)
    assert "experiment_evaluation" not in plain
    broken = SimpleNamespace(decision_experiment_evaluation=lambda *_a: pytest.fail("无授权时不能读取证据"))
    assert read_through_tool(lane.host, thread_id, broken) == plain


def test_decision_read_reports_unreadable_evidence_without_failing(tmp_path):
    lane = lane_at(tmp_path)
    start_turn(lane, "req-now", "observe")

    def broken(*_args):
        raise OSError("磁盘不可读")

    report = json.loads(read_through_tool(lane.host, lane.thread.thread_id, SimpleNamespace(decision_experiment_evaluation=broken)))
    assert report["experiment_evaluation"] == {"status": "unavailable", "reasons": ["evidence_unreadable"]}
