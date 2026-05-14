# LLM: Acceptance alignment normalizes parent-test/follow-up facts before dispatch exposes them.
# 模块用途: 父级真实验收 tests 或 follow-up 要求救援时，统一把验收记录改成拒绝口径。

from __future__ import annotations

from dataclasses import replace

from ..subagents.reports import AcceptanceReviewRecord


# LLM: align_acceptance_record_with_parent_tests makes dispatch facts single-source after real tests fail.
# 函数用途: 显式父级验收 tests/follow-up 要求 rescue 时，把 acceptance record 改成拒绝修复口径。
def align_acceptance_record_with_parent_tests(
    record: AcceptanceReviewRecord,
    policy_summary: dict[str, object],
) -> AcceptanceReviewRecord:
    if not parent_tests_require_rescue(policy_summary):
        return record
    return replace(
        record,
        ok=False,
        decision="REJECT",
        message=parent_acceptance_rescue_message(policy_summary),
        parent_conclusions=[
            *list(getattr(record, "parent_conclusions", []) or []),
            "父级真实验收测试失败或未能执行，不能标记为验收通过。",
        ],
    )


# LLM: parent_acceptance_rescue_message avoids saying tests failed when they were blocked or empty.
# 函数用途: 根据 test_total/test_failed/follow-up 原因生成给主代理看的准确中文结论。
def parent_acceptance_rescue_message(policy_summary: dict[str, object]) -> str:
    failed = int(policy_summary.get("parent_acceptance_auto_execution_test_failed") or 0)
    total = int(policy_summary.get("parent_acceptance_auto_execution_test_total") or 0)
    reason = str(policy_summary.get("parent_acceptance_followup_reason") or "").strip()
    if failed > 0:
        message = f"父级真实验收测试失败：total={total} failed={failed}；需要修复/接管。"
    elif total <= 0:
        message = "父级验收未产生可执行测试或测试执行被拦截；不能标记为通过，需要修复/接管。"
    else:
        message = f"父级验收 follow-up 要求修复/接管：total={total} failed={failed}。"
    if reason:
        message = f"{message} reason={reason}"
    return message


# LLM: parent_tests_require_rescue recognizes failed real acceptance tests from execution/follow-up refs.
# 函数用途: 判断本轮父级真实测试是否要求 rescue，避免旧 acceptance 文案误导主代理继续宣称完成。
def parent_tests_require_rescue(policy_summary: dict[str, object]) -> bool:
    if policy_summary.get("parent_acceptance_auto_execution_executed") is not True:
        return False
    failed = int(policy_summary.get("parent_acceptance_auto_execution_test_failed") or 0)
    if failed > 0:
        return True
    return (
        policy_summary.get("parent_acceptance_followup_status") == "needs_manual_rescue"
        or policy_summary.get("parent_acceptance_followup_action") == "plan_rescue"
    )
