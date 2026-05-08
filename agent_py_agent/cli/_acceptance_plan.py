# LLM: Parent acceptance plan CLI renderer; keep decisions refs-only and apply only through explicit safe bridge.
# 模块用途: 提供父级验收 dry-run 决策查看/审计落盘命令；显式 apply 只放行 inspect_only，不执行 tests、不读取 artifact 正文。

from __future__ import annotations

import json
from pathlib import Path

from .common import make_agent


# LLM: cmd_subagents_acceptance_plan prints, writes, or explicitly applies a safe parent-controller decision.
# 函数用途: 查看或审计落盘父代理验收下一步计划；显式 apply 只桥接 inspect_only，不自动执行 tests。
def cmd_subagents_acceptance_plan(args) -> int:
    agent = make_agent(args)
    run_id = str(getattr(args, "run_id", "") or "")
    if bool(getattr(args, "auto_policy", False)):
        policy = agent.subagents.plan_parent_acceptance_auto_policy(run_id)
        if bool(getattr(args, "json", False)):
            print(json.dumps(_policy_to_dict(policy), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        _print_acceptance_auto_policy(policy)
        return 0
    if bool(getattr(args, "next_action", False)):
        action = agent.subagents.plan_parent_acceptance_next_action(run_id)
        if bool(getattr(args, "json", False)):
            print(json.dumps(_action_to_dict(action), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        _print_acceptance_next_action(action)
        return 0
    if bool(getattr(args, "apply", False)):
        result = agent.subagents.apply_parent_acceptance_decision(
            run_id,
            reviewer=str(getattr(args, "reviewer", "") or "parent"),
            note=str(getattr(args, "note", "") or ""),
        )
        if bool(getattr(args, "json", False)):
            print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        _print_acceptance_apply(result)
        return 0
    write = bool(getattr(args, "write", False))
    decision = (
        agent.subagents.write_parent_acceptance_decision(run_id)
        if write
        else agent.subagents.plan_parent_acceptance(run_id)
    )
    if bool(getattr(args, "json", False)):
        payload = _decision_to_dict(decision)
        if write:
            payload["decision_ref"] = str(_decision_file_path(agent, run_id))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_plan(decision)
    if write:
        print(f"written={_decision_file_path(agent, run_id)}")
    return 0


# LLM: _decision_to_dict keeps CLI JSON robust for dataclass and test double decisions.
# 函数用途: 把父级验收决策转成 JSON 友好字典；测试替身和真实 dataclass 共用同一输出路径。
def _decision_to_dict(decision) -> dict:
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


# LLM: _result_to_dict keeps apply output robust for dataclasses and test doubles.
# 函数用途: 把父级验收 apply 结果转换成 JSON 友好字典，保持 refs-only。
def _result_to_dict(result) -> dict:
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


# LLM: _action_to_dict keeps next-action output robust for dataclasses and test doubles.
# 函数用途: 把父级下一动作建议转换成 JSON 友好字典，保持 refs-only。
def _action_to_dict(action) -> dict:
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


# LLM: _policy_to_dict keeps auto-policy output robust for dataclasses and test doubles.
# 函数用途: 把父级自动策略 dry-run 结果转换成 JSON 字典；不展开引用文件正文。
def _policy_to_dict(policy) -> dict:
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


# LLM: _print_acceptance_auto_policy renders policy gating without executing the recommendation.
# 函数用途: 打印父级自动策略 dry-run 结果、建议命令和审计引用。
def _print_acceptance_auto_policy(policy) -> None:
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
    print(f"reason={getattr(policy, 'reason', '')}")
    recommended_command = str(getattr(policy, "recommended_command", "") or "")
    if recommended_command:
        print(f"recommended_command={recommended_command}")
    command = str(getattr(policy, "command", "") or "")
    if command:
        print(f"command={command}")
    for name in ("next_action_ref", "decision_ref", "apply_ref"):
        value = str(getattr(policy, name, "") or "")
        if value:
            print(f"{name}={value}")


# LLM: _print_acceptance_next_action renders the scheduler-facing recommendation without executing it.
# 函数用途: 打印父级下一动作建议、命令入口和审计引用；不读取引用文件正文。
def _print_acceptance_next_action(action) -> None:
    print("SUBAGENT ACCEPTANCE NEXT ACTION")
    print(
        f"run_id={getattr(action, 'run_id', '')} action={getattr(action, 'action', '')} "
        f"mutates_task_state={bool(getattr(action, 'mutates_task_state', False))}"
    )
    print(f"reason={getattr(action, 'reason', '')}")
    command = str(getattr(action, "command", "") or "")
    if command:
        print(f"command={command}")
    for name in (
        "decision_ref",
        "apply_ref",
        "test_execution_ref",
        "failure_handoff_ref",
        "takeover_readiness_ref",
    ):
        value = str(getattr(action, name, "") or "")
        if value:
            print(f"{name}={value}")


# LLM: _print_acceptance_apply renders the explicit apply result without reading referenced files.
# 函数用途: 打印父级验收 apply 的审计摘要；只展示引用路径和结果，不展开报告正文。
def _print_acceptance_apply(result) -> None:
    print("SUBAGENT ACCEPTANCE APPLY")
    print(
        f"run_id={getattr(result, 'run_id', '')} applied={bool(getattr(result, 'applied', False))} "
        f"parent_decision={getattr(result, 'parent_decision', '')}"
    )
    acceptance_decision = str(getattr(result, "acceptance_decision", "") or "")
    if acceptance_decision:
        print(f"acceptance_decision={acceptance_decision}")
    print(f"message={getattr(result, 'message', '')}")
    for name in ("decision_ref", "apply_ref", "acceptance_review_ref"):
        value = str(getattr(result, name, "") or "")
        if value:
            print(f"{name}={value}")


# LLM: _print_acceptance_plan renders refs-only human output for the parent acceptance plan command.
# 函数用途: 打印父级验收计划摘要、事实源引用和下一步；不展开 artifact 或测试输出正文。
def _print_acceptance_plan(decision) -> None:
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
    for name in ("test_execution_ref", "failure_handoff_ref", "takeover_readiness_ref"):
        value = str(getattr(decision, name, "") or "")
        if value:
            print(f"{name}={value}")


# LLM: _decision_file_path reports the audit file location without reading its contents.
# 函数用途: 计算 parent_acceptance_decision.json 路径，给 CLI 输出引用用。
def _decision_file_path(agent, run_id: str) -> Path:
    task = agent.subagents.load(run_id)
    return Path(task.reports_dir) / "parent_acceptance_decision.json"
