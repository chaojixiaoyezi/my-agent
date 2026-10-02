# LLM: 能力包 v2 块 3 的对外事实：从本 run 的核验账本生成 AgentRunResult.pack_verifications（→ channel_delivery.pack_verifications），
#   以及回合结束时宿主撰写的 HostNotice（source=pack_verification）。结论只来自账本里的检查程序结果，模型自述不算事实。
#   收尾检查跑过时只报最后一次收尾检查覆盖的结果（即交付时的内容状态）；没跑过（回合没正常收尾）就报每个目标最后一次写后结果，
#   并在通知里说明。条数和文字都有上限。改动同步 test_pack_verification_service.py 与 gateway 的提示/交付投影。
# 模块用途: 把宿主核验结果整理成最终交付事实和给用户看的一句话通知。

from __future__ import annotations

from .pack_verification_ledger import PackVerificationLedger, ledger_for_run
from .pack_verification_scope import host_verification_enabled

PACK_VERIFICATIONS_SCHEMA = "pack_verifications.v1"
NOTICE_SOURCE = "pack_verification"
NOTICE_CODE = "summary"
# 最终事实里最多保留的检查结果条数。
MAX_RUN_FACT_RESULTS_COUNT = 16
# 宿主提示里最多列出的检查结果条数。
MAX_NOTICE_RESULTS_COUNT = 4
# 宿主提示正文的字符上限（HostNotice 本身上限 500）。
MAX_NOTICE_TEXT_CHARS = 480


# LLM: 没有任何检查结果时返回 None（未钉包、开关关着或本回合没写交付物），对外事实保持不出现这个键。
# 函数用途: 从核验账本生成本 run 的结构化核验事实。
def pack_verification_facts(ledger: PackVerificationLedger | None) -> dict | None:
    records = ledger.records() if ledger is not None else []
    results = [row for row in records if row.get("kind") == "result" and isinstance(row.get("fact"), dict)]
    if not results:
        return None
    closeouts = [row for row in records if row.get("kind") == "closeout"]
    keys = set(closeouts[-1].get("keys") or []) if closeouts else None
    latest: dict[tuple, dict] = {}
    for row in results:
        fact = row["fact"]
        if keys is None or row.get("key") in keys:
            latest[(fact.get("package_id"), fact.get("verifier_id"), fact.get("target"))] = {
                **fact, "trigger": row.get("trigger", ""), "input_matches": dict(row.get("input_matches") or {})}
    baseline = next((row for row in records if row.get("kind") == "baseline"), {})
    return {"schema": PACK_VERIFICATIONS_SCHEMA, "closeout_checked": bool(closeouts),
            "closeout_truncated": bool(closeouts and closeouts[-1].get("truncated")),
            "current_truncated": bool(closeouts and closeouts[-1].get("current_truncated")),
            "uncertain_targets": list(closeouts[-1].get("uncertain_targets") or []) if closeouts else [],
            "baseline_truncated": bool((baseline.get("scan") or {}).get("truncated")),
            "rework_count": sum(1 for row in records if row.get("kind") == "rework"),
            "results": list(latest.values())[:MAX_RUN_FACT_RESULTS_COUNT]}


# LLM: 收尾阶段读同一本账：账本位置只靠任务存储根和 run_id；找不到账本就没有事实。
# 函数用途: 为最终交付结果取本 run 的核验事实。
def run_pack_verification_facts(agent: object, context: object) -> dict | None:
    from ..agent_core.run_task_workspace_writer import current_run_task_workspace_root

    # 开关关着就不去开账本（9b 建议）：未开启时每轮收尾零 I/O
    if not host_verification_enabled(agent):
        return None
    ledger = ledger_for_run(current_run_task_workspace_root(agent, context), str(getattr(context, "run_id", "") or ""))
    return pack_verification_facts(ledger)


# LLM: 文字只拼结构化事实：包 ID 和版本、检查程序成员、目标相对路径、有效与否、错误/警告条数、前几个错误码或原因码。
#   HostNotice 会把换行合并成空格，所以各条直接首尾相接（每条以句号结尾）。
# 函数用途: 生成给用户看的宿主核验提示正文。
def pack_verification_notice_text(facts: dict) -> str:
    lines = [_notice_line(item) for item in facts.get("results", [])[:MAX_NOTICE_RESULTS_COUNT]]
    if not facts.get("closeout_checked"):
        lines.append("本回合没有正常收尾，上面是写入时的检查结果。")
    text = "".join(lines)
    return text if len(text) <= MAX_NOTICE_TEXT_CHARS else text[: MAX_NOTICE_TEXT_CHARS - 1] + "…"


# 函数用途: 生成一条检查结果的提示行。
def _notice_line(item: dict) -> str:
    head = f"宿主检查（{item.get('package_id')} {item.get('package_version')}，原版 {item.get('member')}）：{item.get('target')}"
    errors = sum((item.get("error_counts") or {}).values())
    warnings = sum((item.get("warning_counts") or {}).values())
    if item.get("status") == "passed":
        return f"{head} 有效，警告 {warnings} 条。"
    if item.get("status") == "failed":
        codes = "、".join(sorted(item.get("error_counts") or {})[:3])
        return f"{head} 无效，错误 {errors} 条（{codes}），警告 {warnings} 条。"
    return f"{head} 未检查（{item.get('reason_code') or item.get('status')}）。"
