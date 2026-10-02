#!/usr/bin/env python3
# LLM: 决策质量基准（J12）运行器：固定的中文用例（decision_quality/cases/*.json）经 decision_quality_adapters 生成与产品同形的材料，
#   用产品的决策后端（decision_backend_from_profile）直接问决策模型，按每题可接受答案集合打分，与 thresholds.json 比对。
#   --check 只离线核对（不发请求）；真实运行要显式给 --provider（测试方准备的连接配置文件，0600，用完删），本脚本不打印密钥。
#   --record 把各点位汇总写进 decision_quality/results.json；gate_failures 是“点位默认打开的前提”，由
#   test_decision_quality_bench.py 对随包默认模式不是 off 的点位强制检查。改字段要同步 README、测试与 results 结构。
# 模块用途: 跑决策模型的中文质量基准，给出每个点位的准确率、是否达到阈值，并能把结果登记进仓库。
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for _path in (REPO, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
BENCH = HERE / "decision_quality"
RESULTS_SCHEMA = "decision_quality_results.v1"
# 真实运行时每次调用的等待上限：质量基准不测时延，给足时间，免得把慢回答记成答错。
CALL_DEADLINE_SECONDS = 20.0


# LLM: 只读 cases/<point>.json；摘要按规范 JSON（键排序、无空白）算，用例改一个字都会变。
# 函数用途: 读出一个点位的共享数据、用例列表和用例摘要。
def load_point(point: str) -> tuple[dict, list, str]:
    data = json.loads((BENCH / "cases" / f"{point}.json").read_text(encoding="utf-8"))
    if data.get("schema") != "decision_quality_cases.v1" or data.get("point") != point:
        raise ValueError(f"{point}: 用例文件结构或点位不对")
    return data.get("shared", {}), data["cases"], _digest(data)


# LLM: 逐用例调适配函数并核对：期望题号必须存在，期望答案必须是该题的候选键，请求能被产品协议构造（大小与题型合法）。
#   返回的材料摘要覆盖全部 state/questions：构造函数行为一变，旧结果就对不上（gate_failures 据此判旧结果失效）。
# 函数用途: 生成一个点位全部用例的真实材料与打分口径，并给出材料摘要。
def build_point(point: str) -> tuple[list, str, str]:
    from decision_quality_adapters import ADAPTERS

    from agent_py_agent.agent.backends.decision_protocol import DecisionBinding, DecisionRequest

    shared, cases, cases_digest = load_point(point)
    built = []
    for case in cases:
        state, questions, expected = ADAPTERS[point](case, shared)
        bad = [question_id for question_id, values in expected.items() if not _accepted_in(questions, question_id, values)]
        if bad:
            raise ValueError(f"{point}/{case['id']}: 期望 {bad} 不在题目或候选里")
        DecisionRequest(DecisionBinding(point, "bench", f"bench:{case['id']}", "bench", "bench"), state, questions)
        built.append((case["id"], state, questions, expected))
    return built, cases_digest, _digest([[case_id, state, questions] for case_id, state, questions, _ in built])


# 函数用途: 判断一组期望答案非空、且都是该题的候选键。
def _accepted_in(questions: dict, question_id: str, values: set) -> bool:
    criteria = questions.get(question_id, {}).get("criteria")
    return isinstance(criteria, dict) and bool(values) and values <= set(criteria)


# LLM: 只认 error_code 为空且 value 在可接受集合里为通过；单题错误、缺答都算未通过，不改称弃权。
# 函数用途: 给一次回答的每道计分题打分。
def score(expected: dict, answers: tuple) -> list[dict]:
    by_id = {answer.question_id: answer for answer in answers}
    rows = []
    for question_id, values in sorted(expected.items()):
        answer = by_id.get(question_id)
        value = getattr(answer, "value", None)
        error = (answer.error_code or "") if answer else "missing_answer"
        rows.append({"question": question_id, "expected": sorted(values), "answer": value, "error_code": error,
                     "pass": not error and value in values})
    return rows


# LLM: 每个用例每遍发一次请求（无重试）；调用失败时该用例全部计分题记未通过，并计一次调用失败。
#   只写结构化行（点位、用例、题号、期望、回答、是否通过、耗时、实际模型），不含材料正文与密钥。
# 函数用途: 对一个点位的全部用例跑若干遍，返回逐题记录。
def run_point(point: str, backend: object, reps: int) -> list[dict]:
    built, _cases_digest, _material_digest = build_point(point)
    rows = []
    for rep in range(1, reps + 1):
        for item in built:
            rows.extend(_ask(point, backend, item, rep))
    return rows


# LLM: 发一次请求（无重试）；任何异常都只记异常类型，该用例全部计分题记未通过。
# 函数用途: 问一次决策模型并给这个用例的计分题打分。
def _ask(point: str, backend: object, item: tuple, rep: int) -> list[dict]:
    from agent_py_agent.agent.backends.decision_protocol import DecisionBinding, DecisionRequest

    case_id, state, questions, expected = item
    binding = DecisionBinding(point, "bench", f"bench:{case_id}:{rep}:{time.time_ns()}", "bench", _digest([state, questions]))
    started = time.monotonic()
    try:
        response = backend.decide(DecisionRequest(binding, state, questions), deadline=time.monotonic() + CALL_DEADLINE_SECONDS)
        scored, model, call_error = score(expected, response.answers), response.model, ""
    except Exception as exc:  # noqa: BLE001 基准要把失败类型记下来，不中断其它用例
        scored, model, call_error = score(expected, ()), "", type(exc).__name__
    meta = {"point": point, "case": case_id, "rep": rep, "model": model, "call_error": call_error,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return [{**meta, **row} for row in scored]


# LLM: 纯函数；调用数按（用例, 遍）去重计；准确率 = 通过题数 / 计分题数；met 由阈值三项共同决定。
# 函数用途: 把逐题记录汇总成一个点位的成绩，并对照阈值给出是否达标。
def summarize(rows: list[dict], threshold: dict) -> dict:
    calls = {(row["case"], row["rep"]): row for row in rows}
    failures = sum(1 for row in calls.values() if row["call_error"])
    passed = sum(1 for row in rows if row["pass"])
    elapsed = [row["elapsed_ms"] for row in calls.values() if not row["call_error"]]
    summary = {"calls": len(calls), "call_failures": failures, "scored_questions": len(rows), "passed": passed,
               "accuracy": round(passed / len(rows), 4) if rows else 0.0,
               "models": sorted({row["model"] for row in calls.values() if row["model"]}),
               "elapsed_ms_median": statistics.median(elapsed) if elapsed else None,
               "elapsed_ms_max": max(elapsed) if elapsed else None}
    summary["met"] = _meets(summary, threshold)
    return summary


# LLM: 三项都要满足：准确率不低于 min_accuracy、计分题数不少于 min_scored_questions、调用失败率不高于 max_call_failure_rate。
# 函数用途: 判断一个点位的汇总是否达到它的阈值。
def _meets(summary: dict, threshold: dict) -> bool:
    calls = summary["calls"] or 1
    return (summary["accuracy"] >= threshold["min_accuracy"]
            and summary["scored_questions"] >= threshold["min_scored_questions"]
            and summary["call_failures"] / calls <= threshold["max_call_failure_rate"])


# LLM: 随包默认值：AgentConfig / MemorySettings / CapabilityConfig 数据类默认值与随包 YAML 都看，任一处不是 off 都算默认打开。
# 函数用途: 列出随包默认就会调用决策模型的点位。
def default_on_points() -> list[str]:
    from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
    from agent_py_agent.agent.settings._memory_types import MemorySettings
    from agent_py_agent.agent.settings.config import AgentConfig, load_config
    from agent_py_agent.agent.settings.decision_settings_defaults import decision_config_fields
    from agent_py_agent.agent.settings.user_config_capability import packaged_config_path

    packaged = load_config(packaged_config_path())
    capability_yaml = REPO / "agent_py_agent" / "config" / "capability_config.yaml"
    sources = {"agent": (AgentConfig(), packaged), "memory": (MemorySettings(), packaged),
               "capability": (CapabilityConfig(), load_capability_config(capability_yaml))}
    modes = [(path.split(".")[1], domain, attr) for path, (domain, attr) in decision_config_fields().items()
             if path.startswith("points.") and path.endswith(".mode")]
    return sorted(point for point, domain, attr in modes
                  if any(getattr(source, attr, "off") != "off" for source in sources[domain]))


# LLM: 纯函数；“点位默认打开”的前提：有阈值、有登记的成绩、成绩的用例摘要与材料摘要都和当前一致、且达标。
#   current 是 {点位: (用例摘要, 材料摘要)}，没有用例的点位传不进来，即视为没有基准。返回中文失败原因列表，空表示通过。
# 函数用途: 检查默认打开的点位是否都有当前有效且达标的质量基准成绩。
def gate_failures(default_on: list, thresholds: dict, results: dict, current: dict) -> list[str]:
    reasons = (_gate_failure(thresholds.get(point), results.get(point), current.get(point)) for point in default_on)
    return [f"{point}: {reason}" for point, reason in zip(default_on, reasons, strict=True) if reason]


# LLM: 纯函数；按“缺东西 → 摘要过期 → 未达标”的顺序给出第一条原因，全部满足返回空串。
# 函数用途: 判断一个默认打开的点位为什么不满足前提。
def _gate_failure(threshold: dict | None, result: dict | None, digests: tuple | None) -> str:
    if threshold is None or digests is None or result is None:
        return "默认打开但缺阈值、用例或登记成绩"
    if (result.get("cases_digest"), result.get("material_digest")) != tuple(digests):
        return "登记成绩对应的用例或材料已经变了，需要重跑基准"
    if not _meets(result, threshold):
        return f"登记成绩未达阈值（准确率 {result.get('accuracy')}）"
    return ""


# LLM: 读写仓库内 results.json（只含汇总数字、模型名、摘要与时间）；同一点位新成绩覆盖旧成绩。
# 函数用途: 把本次各点位汇总登记进结果文件。
def record(summaries: dict) -> None:
    path = BENCH / "results.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": RESULTS_SCHEMA, "points": {}}
    data["points"].update(summaries)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# 函数用途: 规范 JSON 的 sha256，作用例与材料摘要。
def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=sorted)
                          .encode("utf-8")).hexdigest()


# 函数用途: 读阈值文件。
def load_thresholds() -> dict:
    return json.loads((BENCH / "thresholds.json").read_text(encoding="utf-8"))["points"]


# LLM: 有副作用：真实模式会发网络请求、写 --out 下的逐题记录；--record 时改写 results.json。连接配置只在内存里用。
# 函数用途: 命令行入口：离线核对，或真实跑基准并汇报每个点位是否达标。
def main(argv: list[str] | None = None) -> int:
    from decision_quality_adapters import ADAPTERS

    parser = argparse.ArgumentParser(description="决策模型中文质量基准")
    parser.add_argument("--points", default=",".join(ADAPTERS), help="逗号分隔的点位，默认全部有用例的点位")
    parser.add_argument("--check", action="store_true", help="只离线生成材料并核对用例，不发请求")
    parser.add_argument("--provider", help="决策模型连接配置文件（decision_backend_from_profile 的字段，0600）")
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--out", help="逐题记录 JSONL 输出路径")
    parser.add_argument("--record", action="store_true", help="把汇总登记进 decision_quality/results.json")
    args = parser.parse_args(argv)
    points = [point for point in args.points.split(",") if point]
    if args.check:
        for point in points:
            built, cases_digest, material_digest = build_point(point)
            print(json.dumps({"point": point, "cases": len(built), "scored_questions": sum(len(row[3]) for row in built),
                              "cases_digest": cases_digest, "material_digest": material_digest}, ensure_ascii=False))
        return 0
    return _run_real(args, points)


# LLM: 真实模式主体：逐点位跑、写逐题记录、打印汇总；有任一点位未达标时退出码 1（--record 照样登记，未达标也如实写）。
# 函数用途: 真实调用决策模型跑选定点位的基准。
def _run_real(args: argparse.Namespace, points: list[str]) -> int:
    from agent_py_agent.agent.backends.typesafe_decision import decision_backend_from_profile

    backend = decision_backend_from_profile(json.loads(Path(args.provider).read_text(encoding="utf-8")))
    thresholds, summaries = load_thresholds(), {}
    with open(args.out, "a", encoding="utf-8") as sink:
        for point in points:
            rows = run_point(point, backend, args.reps)
            sink.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            _built, cases_digest, material_digest = build_point(point)
            summaries[point] = {**summarize(rows, thresholds[point]), "reps": args.reps, "cases_digest": cases_digest,
                                "material_digest": material_digest, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            print(json.dumps({"point": point, **summaries[point]}, ensure_ascii=False), flush=True)
    if args.record:
        record(summaries)
    return 0 if all(summary["met"] for summary in summaries.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
