# LLM: Parent acceptance plan CLI renderer; keep decisions refs-only and apply only through explicit safe bridge.
# 模块用途: 提供父级验收 dry-run 决策查看/审计落盘命令；显式 apply 只放行 inspect_only，不执行 tests、不读取 artifact 正文。

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
)
from agent_py_agent.agent.subagents.parent_acceptance_followup_control import (
    ParentAcceptanceFollowUpControlOptions,
)

from ._acceptance_plan_renderers import (
    action_to_dict as _action_to_dict,
)
from ._acceptance_plan_renderers import (
    decision_to_dict as _decision_to_dict,
)
from ._acceptance_plan_renderers import (
    execution_to_dict as _execution_to_dict,
)
from ._acceptance_plan_renderers import (
    followup_control_to_dict as _followup_control_to_dict,
)
from ._acceptance_plan_renderers import (
    policy_to_dict as _policy_to_dict,
)
from ._acceptance_plan_renderers import (
    print_acceptance_apply as _print_acceptance_apply,
)
from ._acceptance_plan_renderers import (
    print_acceptance_auto_execution as _print_acceptance_auto_execution,
)
from ._acceptance_plan_renderers import (
    print_acceptance_auto_policy as _print_acceptance_auto_policy,
)
from ._acceptance_plan_renderers import (
    print_acceptance_followup_control as _print_acceptance_followup_control,
)
from ._acceptance_plan_renderers import (
    print_acceptance_next_action as _print_acceptance_next_action,
)
from ._acceptance_plan_renderers import (
    print_acceptance_plan as _print_acceptance_plan,
)
from ._acceptance_plan_renderers import (
    result_to_dict as _result_to_dict,
)
from .common import make_agent


# LLM: cmd_subagents_acceptance_plan prints, writes, or explicitly applies a safe parent-controller decision.
# 函数用途: 查看或审计落盘父代理验收下一步计划；显式 apply 只桥接 inspect_only，不自动执行 tests。
def cmd_subagents_acceptance_plan(args) -> int:
    agent = make_agent(args)
    run_id = str(getattr(args, "run_id", "") or "")
    if bool(getattr(args, "auto_policy", False)):
        return _handle_auto_policy(agent, run_id, json_output=bool(getattr(args, "json", False)))
    if bool(getattr(args, "auto_execution", False)):
        return _handle_auto_execution(agent, args, run_id, json_output=bool(getattr(args, "json", False)))
    if bool(getattr(args, "apply_followup", False)):
        return _handle_apply_followup(agent, args, run_id, json_output=bool(getattr(args, "json", False)))
    if bool(getattr(args, "followup", False)):
        return _handle_followup(agent, run_id, json_output=bool(getattr(args, "json", False)))
    if bool(getattr(args, "next_action", False)):
        return _handle_next_action(agent, run_id, json_output=bool(getattr(args, "json", False)))
    if bool(getattr(args, "apply", False)):
        return _handle_apply(agent, args, run_id, json_output=bool(getattr(args, "json", False)))
    return _handle_default_plan(agent, run_id, args, json_output=bool(getattr(args, "json", False)))


# LLM: _handle_auto_policy keeps the top-level CLI dispatcher below size limits.
# 函数用途: 渲染父级 auto-policy dry-run；只打印或输出 JSON，不执行建议命令。
def _handle_auto_policy(agent, run_id: str, *, json_output: bool) -> int:
    policy = agent.subagents.plan_parent_acceptance_auto_policy(run_id)
    if json_output:
        print(json.dumps(_policy_to_dict(policy), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_auto_policy(policy)
    return 0


# LLM: _handle_auto_execution renders dry-run by default and passes explicit test-execution confirmation as a bundle.
# 函数用途: 渲染父级自动执行 facade；只有 --execute-auto-tests 才跑 tests，始终不 apply、不改任务状态。
def _handle_auto_execution(agent, args, run_id: str, *, json_output: bool) -> int:
    options = ParentAcceptanceAutoExecutionOptions(
        execute_tests=bool(getattr(args, "execute_auto_tests", False)),
        timeout_seconds=float(getattr(args, "timeout", 120.0) or 120.0),
    )
    result = agent.subagents.plan_parent_acceptance_auto_execution(
        run_id,
        options=options,
    )
    if json_output:
        print(json.dumps(_execution_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_auto_execution(result)
    return 0


# LLM: _handle_followup renders post-test semi-auto guidance without executing it.
# 函数用途: 展示 `parent_acceptance_auto_followup.json` 的下一步受控入口建议；不写状态。
def _handle_followup(agent, run_id: str, *, json_output: bool) -> int:
    result = agent.subagents.plan_parent_acceptance_followup(run_id)
    if json_output:
        print(json.dumps(_followup_control_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_followup_control(result)
    return 0


# LLM: _handle_apply_followup is the explicit manual gate after guarded tests.
# 函数用途: 显式处理 follow-up；通过 tests 时 apply，失败 tests 时需要 take-over-by 才接管。
def _handle_apply_followup(agent, args, run_id: str, *, json_output: bool) -> int:
    result = agent.subagents.apply_parent_acceptance_followup(
        run_id,
        options=ParentAcceptanceFollowUpControlOptions(
            apply=True,
            reviewer=str(getattr(args, "reviewer", "") or "parent"),
            note=str(getattr(args, "note", "") or ""),
            take_over_by=str(getattr(args, "take_over_by", "") or ""),
            locked_files=list(getattr(args, "locked_file", []) or []),
        ),
    )
    if json_output:
        print(json.dumps(_followup_control_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_followup_control(result)
    return 0


# LLM: _handle_next_action renders the scheduler-facing recommendation.
# 函数用途: 渲染父级下一动作建议；只展示 refs 和命令，不执行。
def _handle_next_action(agent, run_id: str, *, json_output: bool) -> int:
    action = agent.subagents.plan_parent_acceptance_next_action(run_id)
    if json_output:
        print(json.dumps(_action_to_dict(action), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_next_action(action)
    return 0


# LLM: _handle_apply contains the explicit apply bridge while keeping unsafe decisions blocked.
# 函数用途: 处理显式 apply 分支；只有底层安全桥允许的 inspect_only 会写回状态。
def _handle_apply(agent, args, run_id: str, *, json_output: bool) -> int:
    result = agent.subagents.apply_parent_acceptance_decision(
        run_id,
        reviewer=str(getattr(args, "reviewer", "") or "parent"),
        note=str(getattr(args, "note", "") or ""),
    )
    if json_output:
        print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_apply(result)
    return 0


# LLM: _handle_default_plan renders or writes the parent dry-run decision.
# 函数用途: 处理默认父级验收计划和 `--write` 审计分支；不执行 tests。
def _handle_default_plan(agent, run_id: str, args, *, json_output: bool) -> int:
    write = bool(getattr(args, "write", False))
    decision = (
        agent.subagents.write_parent_acceptance_decision(run_id)
        if write
        else agent.subagents.plan_parent_acceptance(run_id)
    )
    if json_output:
        payload = _decision_to_dict(decision)
        if write:
            payload["decision_ref"] = str(_decision_file_path(agent, run_id))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_acceptance_plan(decision)
    if write:
        print(f"written={_decision_file_path(agent, run_id)}")
    return 0


# LLM: _decision_file_path reports the audit file location without reading its contents.
# 函数用途: 计算 parent_acceptance_decision.json 路径，给 CLI 输出引用用。
def _decision_file_path(agent, run_id: str) -> Path:
    task = agent.subagents.load(run_id)
    return Path(task.reports_dir) / "parent_acceptance_decision.json"
