"""决策质量基准（J12）：固定中文用例经各点位真实材料构造生成请求，按可接受答案集合打分，对照每个点位的阈值。

锁定：
1. 阈值覆盖全部决策点位；有用例的点位离线都能生成与产品同形、协议合法的材料，期望答案都是题目里的候选；
2. 打分与汇总：单题错误、缺答、调用失败都算未通过；达标要准确率、计分题数、调用失败率三项同时满足；
3. “点位默认打开的前提”：随包默认模式不是 off 的点位，必须有用例与材料摘要都和当前一致、且达标的登记成绩（真实检查）；
4. 运行器经假后端走完整打分链路，不发网络请求。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH_DIR = Path(__file__).resolve().parents[2] / "scripts" / "bench"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import decision_quality_bench as bench  # noqa: E402
from decision_quality_adapters import ADAPTERS  # noqa: E402

from agent_py_agent.agent.backends.decision_protocol import DecisionAnswer  # noqa: E402
from agent_py_agent.agent.settings.decision_settings_schema import POINTS  # noqa: E402

THRESHOLD = {"min_accuracy": 0.8, "min_scored_questions": 4, "max_call_failure_rate": 0.1}


# 函数用途: 生成全部有用例点位的当前（用例摘要, 材料摘要）。
def _current() -> dict:
    result = {}
    for point in ADAPTERS:
        _built, cases_digest, material_digest = bench.build_point(point)
        result[point] = (cases_digest, material_digest)
    return result


def test_every_point_has_a_threshold_and_every_case_builds_real_material():
    thresholds = bench.load_thresholds()
    assert set(thresholds) == set(POINTS), "每个决策点位都要有阈值（没有用例的点位因此不能默认打开）"
    assert set(ADAPTERS) <= set(POINTS)
    for point in ADAPTERS:
        built, cases_digest, material_digest = bench.build_point(point)
        assert built and len(cases_digest) == len(material_digest) == 64
        assert all(expected for _case, _state, _questions, expected in built), f"{point}: 每个用例至少一道计分题"
        assert bench.build_point(point)[2] == material_digest, "同样的用例两次生成的材料必须逐字节相同"


def test_default_on_points_have_a_current_passing_benchmark():
    results = json.loads((bench.BENCH / "results.json").read_text(encoding="utf-8"))
    assert results["schema"] == bench.RESULTS_SCHEMA and set(results["points"]) <= set(POINTS)
    failures = bench.gate_failures(bench.default_on_points(), bench.load_thresholds(), results["points"], _current())
    assert failures == [], failures


def test_gate_failures_cover_missing_stale_and_below_threshold_results():
    current = {"recall": ("cases", "material")}
    passing = {"cases_digest": "cases", "material_digest": "material", "accuracy": 0.9, "scored_questions": 10,
               "calls": 10, "call_failures": 0}
    thresholds = {"recall": THRESHOLD, "planning": THRESHOLD}
    assert bench.gate_failures(["recall"], thresholds, {"recall": passing}, current) == []
    assert bench.gate_failures([], thresholds, {}, current) == [], "没有默认打开的点位就没有要求"
    assert bench.gate_failures(["planning"], thresholds, {"planning": passing}, current), "没有用例的点位不能默认打开"
    assert bench.gate_failures(["recall"], thresholds, {}, current), "没有登记成绩"
    assert bench.gate_failures(["recall"], {}, {"recall": passing}, current), "没有阈值"
    for stale in ({"cases_digest": "old"}, {"material_digest": "old"}):
        assert bench.gate_failures(["recall"], thresholds, {"recall": {**passing, **stale}}, current), stale
    for below in ({"accuracy": 0.79}, {"scored_questions": 3}, {"call_failures": 2}):
        assert bench.gate_failures(["recall"], thresholds, {"recall": {**passing, **below}}, current), below


def test_score_counts_only_valid_accepted_answers():
    expected = {"q1": {"a"}, "q2": {"b", "c"}, "q3": {"a"}, "q4": {"a"}}
    answers = (DecisionAnswer("q1", "choice", "a"), DecisionAnswer("q2", "choice", "c"),
               DecisionAnswer("q3", "choice", "a", error_code="invalid_answer"), DecisionAnswer("q9", "choice", "a"))
    rows = {row["question"]: row for row in bench.score(expected, answers)}
    assert [rows[key]["pass"] for key in ("q1", "q2", "q3", "q4")] == [True, True, False, False]
    assert rows["q4"]["error_code"] == "missing_answer" and rows["q3"]["error_code"] == "invalid_answer"


def test_summarize_requires_accuracy_count_and_call_health():
    rows = [{"case": f"c{index}", "rep": 1, "pass": index != 0, "call_error": "", "model": "jev-1.13.0", "elapsed_ms": 100}
            for index in range(5)]
    summary = bench.summarize(rows, THRESHOLD)
    assert (summary["calls"], summary["passed"], summary["accuracy"], summary["met"]) == (5, 4, 0.8, True)
    assert summary["models"] == ["jev-1.13.0"]
    assert not bench.summarize(rows[1:2], THRESHOLD)["met"], "计分题太少不算达标"
    failed = [*rows, {"case": "c9", "rep": 1, "pass": False, "call_error": "ProviderTimeoutError", "model": "", "elapsed_ms": 9}]
    assert bench.summarize(failed, {**THRESHOLD, "min_accuracy": 0.5})["met"] is False, "调用失败率超过上限"


def test_run_point_scores_each_question_through_a_fake_backend():
    built = {case_id: expected for case_id, _state, _questions, expected in bench.build_point("curator_relation")[0]}

    class Backend:
        calls = 0

        def decide(self, request, *, deadline):
            self.calls += 1
            case_id = request.binding.operation_id.split(":")[1]
            answers = tuple(DecisionAnswer(key, "choice", sorted(values)[0]) for key, values in built[case_id].items())
            return SimpleNamespace(answers=answers, model="fake-jev")

    backend = Backend()
    rows = bench.run_point("curator_relation", backend, 2)
    assert backend.calls == 2 * len(built) and all(row["pass"] for row in rows)
    assert {row["model"] for row in rows} == {"fake-jev"}

    class Broken:
        def decide(self, request, *, deadline):
            raise TimeoutError("期限到")

    broken = bench.run_point("curator_relation", Broken(), 1)
    assert broken and not any(row["pass"] for row in broken) and {row["call_error"] for row in broken} == {"TimeoutError"}


def test_default_on_points_reads_packaged_mode_defaults(monkeypatch):
    assert bench.default_on_points() == [], "当前所有点位随包默认都是 off"
    from agent_py_agent.agent.settings import config

    monkeypatch.setattr(config, "load_config", lambda _path: SimpleNamespace(decision_planning_mode="observe"))
    assert bench.default_on_points() == ["planning"]


@pytest.mark.parametrize("point", sorted(ADAPTERS))
def test_case_files_declare_their_point_and_unique_ids(point):
    _shared, cases, _digest = bench.load_point(point)
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)) and all(case.get("expected") for case in cases)


def test_an_expected_answer_outside_the_criteria_is_rejected_offline(monkeypatch):
    shared, cases, digest = bench.load_point("curator")
    broken = [{**cases[0], "expected": {"0": {"tag": ["important"]}}}]
    monkeypatch.setattr(bench, "load_point", lambda _point: (shared, broken, digest))
    with pytest.raises(ValueError, match="期望"):
        bench.build_point("curator")


def test_calls_are_counted_once_per_case_and_rep_even_with_many_questions():
    rows = [{"case": case, "rep": 1, "pass": True, "call_error": "", "model": "m", "elapsed_ms": 10}
            for case in ("a", "b") for _question in range(3)]
    rows += [{"case": "c", "rep": 1, "pass": False, "call_error": "TimeoutError", "model": "", "elapsed_ms": 5}] * 3
    summary = bench.summarize(rows, {**THRESHOLD, "max_call_failure_rate": 0.34, "min_accuracy": 0.6})
    assert (summary["calls"], summary["call_failures"], summary["scored_questions"]) == (3, 1, 9)
    assert summary["met"] is True, "一次调用失败只算一次，3 次调用里 1 次失败不超过 0.34"
