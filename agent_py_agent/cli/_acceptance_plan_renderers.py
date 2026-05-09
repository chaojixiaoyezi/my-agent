# LLM: Parent acceptance CLI render helpers stay refs-only and side-effect free.
# 模块用途: 承接 subagents-acceptance-plan 的 JSON 转换和人类输出，避免命令入口继续膨胀。

from __future__ import annotations


# LLM: decision_to_dict keeps CLI JSON robust for dataclass and test double decisions.
# 函数用途: 把父级验收决策转成 JSON 友好字典；测试替身和真实 dataclass 共用同一输出路径。
def decision_to_dict(decision) -> dict:
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(decision, "run_id", ""),
        "decision": getattr(decision, "decision", ""),
        "reason": getattr(decision, "reason", ""),
        "risk_level": getattr(decision, "risk_level", ""),
        "requires_human_confirmation": bool(getattr(decision, "requires_human_confirmation", False)),
    }


# LLM: result_to_dict keeps apply output robust for dataclasses and test doubles.
# 函数用途: 把父级验收 apply 结果转换成 JSON 友好字典，保持 refs-only。
def result_to_dict(result) -> dict:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(result, "run_id", ""),
        "applied": bool(getattr(result, "applied", False)),
        "parent_decision": getattr(result, "parent_decision", ""),
        "acceptance_decision": getattr(result, "acceptance_decision", ""),
        "message": getattr(result, "message", ""),
        "decision_ref": getattr(result, "decision_ref", ""),
        "apply_ref": getattr(result, "apply_ref", ""),
        "acceptance_review_ref": getattr(result, "acceptance_review_ref", ""),
    }


# LLM: action_to_dict keeps next-action output robust for dataclasses and test doubles.
# 函数用途: 把父级下一动作建议转换成 JSON 友好字典，保持 refs-only。
def action_to_dict(action) -> dict:
    to_dict = getattr(action, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(action, "run_id", ""),
        "action": getattr(action, "action", ""),
        "reason": getattr(action, "reason", ""),
        "command": getattr(action, "command", ""),
        "mutates_task_state": bool(getattr(action, "mutates_task_state", False)),
    }


# LLM: policy_to_dict keeps auto-policy output robust for dataclasses and test doubles.
# 函数用途: 把父级自动策略 dry-run 结果转换成 JSON 字典；不展开引用文件正文。
def policy_to_dict(policy) -> dict:
    to_dict = getattr(policy, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(policy, "run_id", ""),
        "action": getattr(policy, "action", ""),
        "decision": getattr(policy, "decision", ""),
        "dry_run": bool(getattr(policy, "dry_run", True)),
        "would_execute": bool(getattr(policy, "would_execute", False)),
        "executed": bool(getattr(policy, "executed", False)),
    }


# LLM: execution_to_dict keeps auto-execution output robust for dataclasses and test doubles.
# 函数用途: 把自动执行 dry-run facade 结果转换成 JSON 字典；不展开引用文件正文。
def execution_to_dict(result) -> dict:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(result, "run_id", ""),
        "mode": getattr(result, "mode", ""),
        "status": getattr(result, "status", ""),
        "execution_allowed": bool(getattr(result, "execution_allowed", False)),
        "executed": bool(getattr(result, "executed", False)),
    }


# LLM: followup_control_to_dict keeps follow-up control JSON robust for dataclasses and test doubles.
# 函数用途: 把 follow-up 预览或控制结果转成 JSON 字典；保持 refs-only。
def followup_control_to_dict(result) -> dict:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "run_id": getattr(result, "run_id", ""),
        "status": getattr(result, "status", ""),
        "action": getattr(result, "action", ""),
        "applied": bool(getattr(result, "applied", False)),
        "recommended_command": getattr(result, "recommended_command", ""),
    }


# LLM: print_acceptance_auto_policy renders policy gating without executing the recommendation.
# 函数用途: 打印父级自动策略 dry-run 结果、建议命令和审计引用。
def print_acceptance_auto_policy(policy) -> None:
    print("SUBAGENT ACCEPTANCE AUTO POLICY")
    print(
        f"run_id={getattr(policy, 'run_id', '')} action={getattr(policy, 'action', '')} "
        f"decision={getattr(policy, 'decision', '')} dry_run={bool(getattr(policy, 'dry_run', True))}"
    )
    print(
        f"would_execute={bool(getattr(policy, 'would_execute', False))} "
        f"executed={bool(getattr(policy, 'executed', False))}"
    )
    print(
        f"execution_mode={getattr(policy, 'execution_mode', 'manual_only')} "
        f"automatic_execution_allowed={bool(getattr(policy, 'automatic_execution_allowed', False))}"
    )
    print(
        f"preflight_status={getattr(policy, 'preflight_status', 'blocked')} "
        f"manual={bool(getattr(policy, 'ready_for_manual_execution', False))} "
        f"automatic={bool(getattr(policy, 'ready_for_automatic_execution', False))}"
    )
    _print_joined("preflight_blockers", getattr(policy, "preflight_blockers", []) or [])
    print(f"reason={getattr(policy, 'reason', '')}")
    _print_optional("recommended_command", getattr(policy, "recommended_command", ""))
    _print_optional("command", getattr(policy, "command", ""))
    _print_refs(policy, ("next_action_ref", "decision_ref", "apply_ref"))


# LLM: print_acceptance_auto_execution renders the dry-run executor facade without running commands.
# 函数用途: 打印父级自动执行计划、硬闸门和阻断原因；不会启动 recommended command。
def print_acceptance_auto_execution(result) -> None:
    print("SUBAGENT ACCEPTANCE AUTO EXECUTION")
    print(
        f"run_id={getattr(result, 'run_id', '')} mode={getattr(result, 'mode', '')} "
        f"status={getattr(result, 'status', '')}"
    )
    print(
        f"execution_allowed={bool(getattr(result, 'execution_allowed', False))} "
        f"executed={bool(getattr(result, 'executed', False))}"
    )
    print(f"guard_status={getattr(result, 'guard_status', '')}")
    _print_optional("command", getattr(result, "command", ""))
    _print_joined("blocked_by", getattr(result, "blocked_by", []) or [])
    _print_followup_summary(result)
    _print_refs(result, ("execution_ref",))


# LLM: print_acceptance_followup_control renders the manual follow-up gate result.
# 函数用途: 打印 follow-up 预览或显式处理结果；只展示 refs、命令和是否修改状态。
def print_acceptance_followup_control(result) -> None:
    print("SUBAGENT ACCEPTANCE FOLLOWUP")
    print(
        f"run_id={getattr(result, 'run_id', '')} status={getattr(result, 'status', '')} "
        f"action={getattr(result, 'action', '')} applied={bool(getattr(result, 'applied', False))}"
    )
    print(
        f"ok={bool(getattr(result, 'ok', False))} "
        f"mutates_task_state={bool(getattr(result, 'mutates_task_state', False))}"
    )
    print(f"message={getattr(result, 'message', '')}")
    _print_optional("recommended_command", getattr(result, "recommended_command", ""))
    _print_refs(result, ("followup_ref", "control_ref", "acceptance_apply_ref", "takeover_ref"))


# LLM: print_acceptance_next_action renders the scheduler-facing recommendation without executing it.
# 函数用途: 打印父级下一动作建议、命令入口和审计引用；不读取引用文件正文。
def print_acceptance_next_action(action) -> None:
    print("SUBAGENT ACCEPTANCE NEXT ACTION")
    print(
        f"run_id={getattr(action, 'run_id', '')} action={getattr(action, 'action', '')} "
        f"mutates_task_state={bool(getattr(action, 'mutates_task_state', False))}"
    )
    print(f"reason={getattr(action, 'reason', '')}")
    _print_optional("command", getattr(action, "command", ""))
    _print_refs(action, (
        "decision_ref",
        "apply_ref",
        "test_execution_ref",
        "failure_handoff_ref",
        "takeover_readiness_ref",
    ))


# LLM: print_acceptance_apply renders the explicit apply result without reading referenced files.
# 函数用途: 打印父级验收 apply 的审计摘要；只展示引用路径和结果，不展开报告正文。
def print_acceptance_apply(result) -> None:
    print("SUBAGENT ACCEPTANCE APPLY")
    print(
        f"run_id={getattr(result, 'run_id', '')} applied={bool(getattr(result, 'applied', False))} "
        f"parent_decision={getattr(result, 'parent_decision', '')}"
    )
    _print_optional("acceptance_decision", getattr(result, "acceptance_decision", ""))
    print(f"message={getattr(result, 'message', '')}")
    _print_refs(result, ("decision_ref", "apply_ref", "acceptance_review_ref"))


# LLM: print_acceptance_plan renders refs-only human output for the parent acceptance plan command.
# 函数用途: 打印父级验收计划摘要、事实源引用和下一步；不展开 artifact 或测试输出正文。
def print_acceptance_plan(decision) -> None:
    print("SUBAGENT ACCEPTANCE PLAN")
    print(
        f"run_id={getattr(decision, 'run_id', '')} decision={getattr(decision, 'decision', '')} "
        f"risk={getattr(decision, 'risk_level', '')} "
        f"human={bool(getattr(decision, 'requires_human_confirmation', False))}"
    )
    print(f"reason={getattr(decision, 'reason', '')}")
    _print_acceptance_plan_refs(decision)
    actions = list(getattr(decision, "next_actions", []) or [])
    if actions:
        print("next_actions:")
        for action in actions:
            print(f"- {action}")


# LLM: _print_acceptance_plan_refs emits only paths and summaries, never referenced file bodies.
# 函数用途: 展示父级决策用到的事实源引用，保持 refs-only 输出边界。
def _print_acceptance_plan_refs(decision) -> None:
    refs = list(getattr(decision, "evidence_refs", []) or [])
    if refs:
        print("refs:")
        for ref in refs:
            print(
                f"- {getattr(ref, 'kind', '')}: {getattr(ref, 'path', '')} "
                f"{getattr(ref, 'summary', '')}".rstrip()
            )
    _print_refs(decision, ("test_execution_ref", "failure_handoff_ref", "takeover_readiness_ref"))


# LLM: _print_followup_summary keeps auto-execution output compact when follow-up refs exist.
# 函数用途: 打印测试执行后的 follow-up 摘要，不读取 follow-up JSON 正文。
def _print_followup_summary(result) -> None:
    followup_ref = str(getattr(result, "followup_ref", "") or "")
    if not followup_ref:
        return
    print(
        f"followup_status={getattr(result, 'followup_status', '')} "
        f"followup_action={getattr(result, 'followup_action', '')}"
    )
    _print_optional("followup_command", getattr(result, "followup_command", ""))
    print(f"followup_ref={followup_ref}")


# LLM: _print_optional keeps empty fields out of human CLI output.
# 函数用途: 有值时打印 key=value；没有值就保持输出干净。
def _print_optional(name: str, value) -> None:
    text = str(value or "")
    if text:
        print(f"{name}={text}")


# LLM: _print_joined prints compact comma-separated list fields.
# 函数用途: 展示 blockers 这类短列表；空列表不输出。
def _print_joined(name: str, values) -> None:
    items = [str(item) for item in values or []]
    if items:
        print(f"{name}={','.join(items)}")


# LLM: _print_refs standardizes refs-only path output across acceptance subcommands.
# 函数用途: 只打印引用字段，不打开、不展开引用文件。
def _print_refs(obj, names: tuple[str, ...]) -> None:
    for name in names:
        _print_optional(name, getattr(obj, name, ""))
