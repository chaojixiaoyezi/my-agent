# LLM: 只读 Gateway 请求记录里的实验条目（原权威），按固定规则给出只读评估；不读设置正文、模型回答或任何自然语言。
#   规则常量属于已审计的证据合同，不是用户配置；Jev 的回答只经宿主投影成候选名单，永远不能直接改变评估结论。
#   调用方：gateway_parts.request_experiment_records（读路径与回合结束）与 request_experiment_promotion（自动晋升前复核）。
# 模块用途: 把最近的只观察实验样本换算成召回、节省与结算三项事实，决定是否提出 skill_tool off→apply 的建议。
from __future__ import annotations

EXPERIMENT_RECORD_SCHEMA = "decision_experiment_record.v1"
EVALUATION_SCHEMA = "decision_experiment_evaluation.v1"
# 至少 3 个可比较的已完成样本才可能提出建议：一两次命中可能是巧合，不足以证明短名单不会漏掉实际要用的工具。
MIN_COMPARABLE_SAMPLES = 3
# 可信指标是短名单召回：实际调用过、且属于本次快照的工具必须全部在候选短名单内；漏一个就意味着 apply 会让模型多绕一次搜索。
REQUIRED_RECALL = 1.0
# 只看最近 8 个已完成样本（与请求记录条目上限一致），更早的样本不再参与，避免很久以前的环境左右当前判断。
WINDOW_SAMPLES = 8
# 只有 charged 表示供应商实际输入已完整入账；usage_unknown/send_refused/gate_bypassed 等都不能当作晋升证据。
CHARGED_OUTCOME = "charged"
SKILL_TOOL_PROPOSAL = {"point": "skill_tool", "field": "points.skill_tool.mode", "from": "off", "to": "apply",
                       "scope": "thread"}


# LLM: 条目必须是本 schema 的 skill_tool 记录且 refs 的 owner/thread 与调用方给出的可信身份逐字相等；其它一律视为外来记录。
# 函数用途: 判断一条请求记录条目是否属于当前用户当前会话的实验样本。
def _owned_record(entry: object, owner_ref: str, thread_id: str) -> bool:
    refs = entry.get("refs") if isinstance(entry, dict) else None
    return (isinstance(refs, dict) and entry.get("schema") == EXPERIMENT_RECORD_SCHEMA and entry.get("point") == "skill_tool"
            and type(entry.get("record_id")) is str and bool(entry["record_id"])
            and refs.get("owner_ref") == owner_ref and refs.get("thread_id") == thread_id)


# LLM: 入参按最新在前排列；只保留写过实际用量（status=completed）的条目，按 record_id 去重后截到窗口，不修改原条目。
# 函数用途: 从请求记录条目里挑出参与评估的样本；外来 owner/线程、未完成或重复的条目都被忽略。
def completed_samples(entries: list, *, owner_ref: str, thread_id: str) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not _owned_record(entry, owner_ref, thread_id) or entry.get("status") != "completed" or entry["record_id"] in seen:
            continue
        seen.add(entry["record_id"])
        selected.append(entry)
    return selected[:WINDOW_SAMPLES]


# LLM: 名单只按结构化字段比较：命中=在短名单，漏掉=在延迟名单；两边都不在的是本次快照外的工具，不计入分母。
#   名单被截断且有工具无法归类时返回未知，不猜；没有可归类的实际工具时也返回未知（没有召回证据）。
# 函数用途: 计算一次样本的短名单召回、漏掉的工具数，以及无法计算时的结构化原因。
def shortlist_recall(candidate: dict, realized: dict) -> tuple[float | None, int, str]:
    if realized.get("known") is not True or candidate.get("status") != "projected":
        return None, 0, ""
    names = realized.get("tool_names")
    if realized.get("names_truncated") is not False or not isinstance(names, list):
        return None, 0, "realized_names_truncated"
    shortlist, deferred = set(candidate.get("shortlist_names") or ()), set(candidate.get("deferred_names") or ())
    hits = sum(name in shortlist for name in names)
    misses = sum(name in deferred and name not in shortlist for name in names)
    if len(names) > hits + misses and candidate.get("names_truncated") is not False:
        return None, 0, "candidate_names_truncated"
    if not hits + misses:
        return None, 0, "no_realized_tools"
    return round(hits / (hits + misses), 6), misses, ""


# LLM: 可比较=结算为 charged、实际用量已知、候选已投影且召回可算；原因码固定，不读取条目里的任何自由文本。
# 函数用途: 把一条已完成的实验记录换算成评估用的样本事实。
def sample_view(entry: dict) -> dict:
    settlement, candidate, realized = (entry.get(key) if isinstance(entry.get(key), dict) else {}
                                       for key in ("settlement", "candidate", "realized"))
    recall, misses, recall_reason = shortlist_recall(candidate, realized)
    reasons = [code for code, failed in (("not_charged", settlement.get("outcome") != CHARGED_OUTCOME),
                                         ("realized_unknown", realized.get("known") is not True),
                                         ("candidate_unavailable", candidate.get("status") != "projected"),
                                         (recall_reason, bool(recall_reason))) if failed]
    deferred = candidate.get("deferred_count")
    return {"record_id": entry["record_id"], "request_id": str(entry["refs"].get("request_id") or ""),
            "outcome": settlement.get("outcome"), "realized_known": realized.get("known") is True,
            "shortlist_recall": recall, "recall_misses": misses,
            "deferred_count": deferred if type(deferred) is int else 0, "comparable": not reasons, "reasons": reasons}


# LLM: 规则四条同时成立才提出建议：可比较样本≥3、窗口内每个样本都 charged、每个可比较样本召回=1.0 且有节省（延迟数>0）；
#   否则返回 keep_observing 与原因码。结果只读，是否写设置由宿主在用户显式 apply 授权内另行复核。
# 函数用途: 评估当前用户当前会话最近的 skill_tool 实验样本，返回建议或继续观察的结构化结论。
def evaluate_skill_tool_samples(entries: list, *, owner_ref: str, thread_id: str) -> dict:
    samples = [sample_view(entry) for entry in completed_samples(entries, owner_ref=owner_ref, thread_id=thread_id)]
    comparable = [sample for sample in samples if sample["comparable"]]
    reasons = [code for code, failed in (
        ("insufficient_samples", len(comparable) < MIN_COMPARABLE_SAMPLES),
        ("settlement_not_charged", any(sample["outcome"] != CHARGED_OUTCOME for sample in samples)),
        ("recall_below_one", any(sample["shortlist_recall"] < REQUIRED_RECALL for sample in comparable)),
        ("no_savings", any(sample["deferred_count"] <= 0 for sample in comparable)),
    ) if failed]
    return {"schema": EVALUATION_SCHEMA, "point": "skill_tool", "status": "keep_observing" if reasons else "proposal",
            "reasons": reasons, "sample_count": len(samples), "comparable_count": len(comparable),
            "rule": {"min_comparable_samples": MIN_COMPARABLE_SAMPLES, "required_recall": REQUIRED_RECALL,
                     "required_outcome": CHARGED_OUTCOME, "window_samples": WINDOW_SAMPLES},
            "proposal": None if reasons else dict(SKILL_TOOL_PROPOSAL), "samples": samples}


__all__ = ["EXPERIMENT_RECORD_SCHEMA", "SKILL_TOOL_PROPOSAL", "completed_samples", "evaluate_skill_tool_samples",
           "sample_view", "shortlist_recall"]
