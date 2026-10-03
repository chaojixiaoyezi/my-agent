# LLM: 能力包 v2 块 3 的对外事实：从本 run 的核验账本生成 AgentRunResult.pack_verifications（→ channel_delivery.pack_verifications），
#   以及回合结束时宿主撰写的 HostNotice（source=pack_verification）。结论只来自账本里的检查程序结果，模型自述不算事实。
#   收尾检查跑过时只报最后一次收尾检查覆盖的结果（即交付时的内容状态）；没跑过（回合没正常收尾）就报每个目标最后一次写后结果，
#   取消由账本 status/closeout.cancelled 表达，通知必须明确“被取消”，不能当检查通过。条数和文字有上限，联测 cancellation。
# 模块用途: 把宿主核验结果整理成最终交付事实和给用户看的一句话通知。

from __future__ import annotations

from .pack_verification_ledger import PackVerificationLedger, ledger_for_run, pack_verification_root
from .pack_verification_scope import host_verification_enabled, verification_owner

PACK_VERIFICATIONS_SCHEMA = "pack_verifications.v1"
NOTICE_SOURCE = "pack_verification"
NOTICE_CODE = "summary"
# 最终事实里最多保留的检查结果条数。
MAX_RUN_FACT_RESULTS_COUNT = 16
# 宿主提示里最多列出的检查结果条数。
MAX_NOTICE_RESULTS_COUNT = 4
# 宿主提示正文的字符上限（HostNotice 本身上限 500）。
MAX_NOTICE_TEXT_CHARS = 480


# LLM: 检查结果、被就地改的输入原件、缺的交付物、本回合被取消（块 6a）四样都没有时返回 None（未钉包、开关关着或本回合没改工作区），
#   对外事实保持不出现这个键。inputs_modified 取最后一次收尾的输入原件检查（块 4），deliverables_missing 取最后一次交付存在检查（块 5）；
#   取消只读账本里的 closeout.cancelled 和结果的 status（不依赖已经解绑的令牌），零目标的已取消收尾也如实保留。
#   没有收尾时每个目标取最后一次真实检查：写后反馈暂停行（复用键为空，见 service._paused_post_write）不算检查结果，
#   不能盖掉前面的真实失败（9b 复审 P1–P3 必须改 1）。
# 函数用途: 从核验账本生成本 run 的结构化核验与取消事实，不把没跑完的检查当成通过。
def pack_verification_facts(ledger: PackVerificationLedger | None) -> dict | None:
    records = ledger.records() if ledger is not None else []
    results = [row for row in records if row.get("kind") == "result" and isinstance(row.get("fact"), dict)]
    input_checks = [row for row in records if row.get("kind") == "input_check" and isinstance(row.get("items"), list)]
    inputs_modified = [dict(item) for item in input_checks[-1]["items"]] if input_checks else []
    deliverable_checks = [row for row in records if row.get("kind") == "deliverable_check" and isinstance(row.get("items"), list)]
    deliverables_missing = [dict(item) for item in deliverable_checks[-1]["items"]] if deliverable_checks else []
    closeouts = [row for row in records if row.get("kind") == "closeout"]
    cancelled = bool(closeouts and closeouts[-1].get("cancelled"))
    if not results and not inputs_modified and not deliverables_missing and not cancelled:
        return None
    keys = set(closeouts[-1].get("keys") or []) if closeouts else None
    latest: dict[tuple, dict] = {}
    for row in results:
        fact = row["fact"]
        if (row.get("key") in keys) if keys is not None else bool(row.get("key")):
            latest[(fact.get("package_id"), fact.get("verifier_id"), fact.get("target"))] = {
                **fact, "trigger": row.get("trigger", ""), "input_matches": dict(row.get("input_matches") or {})}
    baseline = next((row for row in records if row.get("kind") == "baseline"), {})
    return {"schema": PACK_VERIFICATIONS_SCHEMA, "closeout_checked": bool(closeouts),
            "cancelled": cancelled or any(item.get("status") == "cancelled" for item in latest.values()),
            "closeout_truncated": bool(closeouts and closeouts[-1].get("truncated")),
            "current_truncated": bool(closeouts and closeouts[-1].get("current_truncated")),
            "uncertain_targets": list(closeouts[-1].get("uncertain_targets") or []) if closeouts else [],
            "baseline_truncated": bool((baseline.get("scan") or {}).get("truncated")),
            "rework_count": sum(1 for row in records if row.get("kind") == "rework"),
            "input_rework_count": sum(1 for row in records if row.get("kind") == "input_rework"),
            "inputs_modified": inputs_modified[:MAX_RUN_FACT_RESULTS_COUNT],
            "deliverable_rework_count": sum(1 for row in records if row.get("kind") == "deliverable_rework"),
            "deliverables_missing": deliverables_missing[:MAX_RUN_FACT_RESULTS_COUNT],
            "results": list(latest.values())[:MAX_RUN_FACT_RESULTS_COUNT]}


# LLM: 收尾阶段读同一本账：账本位置只靠规范任务根下的宿主核验目录和 run_id；找不到账本就没有事实。
# 函数用途: 为最终交付结果取本 run 的核验事实。
def run_pack_verification_facts(agent: object, context: object) -> dict | None:
    # 开关关着就不去开账本（9b 建议）：未开启时每轮收尾零 I/O
    if not host_verification_enabled(agent):
        return None
    owner = verification_owner(agent)
    root = pack_verification_root(agent, context, owner) if owner is not None else None
    return pack_verification_facts(ledger_for_run(root, str(getattr(context, "run_id", "") or "")))


# LLM: 文字只拼结构化事实：包 ID 和版本、检查程序成员、目标相对路径、有效与否、错误/警告条数、前几个错误码或原因码。
#   取消提示放在开头，不能被条数/字符上限截没；HostNotice 单行拼接，任何正文自述不决定取消。
# 函数用途: 生成宿主核验通知，明确说明被取消与没有正常收尾的边界。
def pack_verification_notice_text(facts: dict) -> str:
    lines = ["本回合核验被取消，未完成的检查不作为有效结论。"] if facts.get("cancelled") else []
    lines += [f"宿主检查：任务开始时的输入 {item.get('path')} 被就地改了"
              f"（{'原件副本已保存' if item.get('copy_path') else '没有原件副本'}）。"
              for item in facts.get("inputs_modified", [])[:MAX_NOTICE_RESULTS_COUNT]]
    lines += [f"宿主检查：{item.get('package_id')} 要求的交付物 {item.get('deliverable_id')} "
              f"{_missing_deliverable_text(item)}。"
              for item in facts.get("deliverables_missing", [])[:MAX_NOTICE_RESULTS_COUNT]]
    lines += [_notice_line(item) for item in facts.get("results", [])[:MAX_NOTICE_RESULTS_COUNT]]
    if not facts.get("closeout_checked"):
        lines.append("本回合没有正常收尾，上面是写入时的检查结果。")
    text = "".join(lines)
    return text if len(text) <= MAX_NOTICE_TEXT_CHARS else text[: MAX_NOTICE_TEXT_CHARS - 1] + "…"


# LLM: 缺交付物的一句话结论：打不开照旧；没匹配到的把声明的路径模式写出来——B8 重试点 A10 实测，
#   只说「本回合没有写出」会把「换了文件名」误报成「没写」，测试方也分不清两种情形。
# 函数用途: 生成一条缺交付物说明的结论短语。
def _missing_deliverable_text(item: dict) -> str:
    if item.get("code") == "DELIVERABLE_UNREADABLE":
        return "写出了但打不开"
    patterns = "、".join(item.get("path_patterns") or [])
    return f"没找到符合 {patterns} 的文件" if patterns else "没找到符合声明路径模式的文件"


# LLM: 单条取消是结构化 status，不读 reason/message 文本；与通过、质量失败明确分开。
# 函数用途: 生成一条核验事实的中文说明，取消不伪装成未支持或正常检查。
def _notice_line(item: dict) -> str:
    head = f"宿主检查（{item.get('package_id')} {item.get('package_version')}，原版 {item.get('member')}）：{item.get('target')}"
    errors = sum((item.get("error_counts") or {}).values())
    warnings = sum((item.get("warning_counts") or {}).values())
    if item.get("status") == "cancelled":
        return f"{head} 检查被取消。"
    if item.get("status") == "passed":
        return f"{head} 有效，警告 {warnings} 条。"
    if item.get("status") == "failed":
        codes = "、".join(sorted(item.get("error_counts") or {})[:3])
        return f"{head} 无效，错误 {errors} 条（{codes}），警告 {warnings} 条。"
    return f"{head} 未检查（{item.get('reason_code') or item.get('status')}）。"
