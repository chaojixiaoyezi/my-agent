# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""subagents subcommand registration helpers.

给人看的解释：
这个文件包含子代理相关子命令的注册函数。
从 parser_subcommands.py 拆分而来，保持原有逻辑不变。
"""

import argparse

from .common import DEFAULT_CAPABILITY_CONFIG
from .subagents import (
    cmd_spawn,
    cmd_subagent_context,
    cmd_subagent_detail,
    cmd_subagent_run,
    cmd_subagents,
    cmd_subagents_acceptance,
    cmd_subagents_acceptance_plan,
    cmd_subagents_apply_actions,
    cmd_subagents_dispatch,
    cmd_subagents_due_check,
    cmd_subagents_memory_gate,
    cmd_subagents_patches,
    cmd_subagents_plan_actions,
    cmd_subagents_probe,
    cmd_subagents_route_capabilities,
    cmd_subagents_tests,
    cmd_subagents_workflow_plan,
)


# LLM: _add_capability_config_arg 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_capability_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )


# LLM: _add_agents_basic_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_agents_basic_subcommands(sub):
    spawn = sub.add_parser("spawn-subagents", help="拆分并创建 subagent 任务记录")
    spawn.add_argument("goal", help="要拆分的目标")
    spawn.add_argument("--count", type=int, default=3, help="子代理数量")
    spawn.set_defaults(func=cmd_spawn)

    subagents = sub.add_parser("subagents", help="查看 subagent 红绿灯看板")
    subagents.add_argument("--all", action="store_true", help="显示全部记录，而不是默认的红灯/最近记录")
    subagents.add_argument("--status", help="按状态过滤，如 BLOCKED/DONE/TAKEN_OVER")
    subagents.add_argument("--owner", help="按 owner/supervisor/final_owner 过滤")
    subagents.add_argument("--root-id", help="按根任务 ID 过滤")
    subagents.add_argument("--limit", type=int, default=20, help="最多显示多少条")
    subagents.set_defaults(func=cmd_subagents)

    workflow_plan = sub.add_parser("subagents-workflow-plan", help="Preview automatic subagent workflow routing")
    workflow_plan.add_argument("goal", help="Parent goal to route into a workflow")
    workflow_plan.add_argument("--template-id", help="Force a workflow template id for the preview")
    workflow_plan.add_argument("--output-dir", help="Write JSON and Markdown dry-run previews to this directory")
    workflow_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    workflow_plan.set_defaults(func=cmd_subagents_workflow_plan)

    due_check = sub.add_parser("subagents-due-check", help="巡检 subagent 并输出父代理待处理项")
    _add_capability_config_arg(due_check)
    due_check.add_argument("--all", action="store_true", help="显示全部问题，而不是按 limit 截断")
    due_check.add_argument("--limit", type=int, default=20, help="最多显示多少条问题")
    due_check.set_defaults(func=cmd_subagents_due_check)

    probe = sub.add_parser("subagents-probe", help="检查 subagent 通道健康状态")
    probe.add_argument("run_id", nargs="*", help="子代理运行 ID；不传则检查最近记录")
    probe.add_argument("--limit", type=int, default=20, help="不指定 run_id 时最多检查多少条")
    probe.set_defaults(func=cmd_subagents_probe)

    action_plan = sub.add_parser("subagents-plan-actions", help="根据 due-check 生成 dry-run 动作计划")
    _add_capability_config_arg(action_plan)
    action_plan.add_argument("--all", action="store_true", help="显示全部动作，而不是按 limit 截断")
    action_plan.add_argument("--limit", type=int, default=20, help="最多显示多少条动作")
    action_plan.set_defaults(func=cmd_subagents_plan_actions)


# LLM: _add_agents_action_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_agents_action_subcommands(sub):
    apply_actions = sub.add_parser("subagents-apply-actions", help="执行或 dry-run 执行 action plan")
    _add_capability_config_arg(apply_actions)
    apply_actions.add_argument("--dry-run", action="store_false", dest="apply", help="只预览动作，不修改记录")
    apply_actions.add_argument("--apply", action="store_true", help="真正执行低风险动作")
    apply_actions.add_argument("--action", help="只处理指定动作，如 reopen_for_evidence")
    apply_actions.add_argument("--run-id", help="只处理指定子代理运行 ID")
    apply_actions.add_argument("--limit", type=int, default=20, help="最多处理多少条动作")
    apply_actions.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    apply_actions.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    apply_actions.set_defaults(func=cmd_subagents_apply_actions, apply=False)

    route = sub.add_parser("subagents-route-capabilities", help="路由 OPEN capability request")
    _add_capability_config_arg(route)
    route.add_argument("--dry-run", action="store_false", dest="apply", help="只预览路由，不生成 grant/gap")
    route.add_argument("--apply", action="store_true", help="真正生成 capability grant 或 gap")
    route.add_argument("--run-id", nargs="*", help="只处理指定子代理运行 ID")
    route.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    route.add_argument("--limit", type=int, default=20, help="最多处理多少条 request")
    route.set_defaults(func=cmd_subagents_route_capabilities, apply=False)

    _add_agents_acceptance_subcommand(sub)
    _add_agents_tests_subcommand(sub)
    _add_agents_patch_subcommand(sub)


# LLM: _add_agents_acceptance_subcommand keeps acceptance-specific argparse wiring isolated.
# 函数用途: 注册父级验收命令和真实测试执行覆盖参数。
def _add_agents_acceptance_subcommand(sub):
    acceptance = sub.add_parser("subagents-acceptance", help="验收等待验收的 subagent")
    acceptance.add_argument("--dry-run", action="store_false", dest="apply", help="只生成验收报告，不修改记录")
    acceptance.add_argument("--apply", action="store_true", help="验收通过时标记 DONE/VERIFIED，失败时标记 BLOCKED/FAILED")
    acceptance.add_argument("--run-id", nargs="*", help="只验收指定子代理运行 ID")
    acceptance.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    acceptance.add_argument("--reviewer", default="parent", help="验收者标识")
    acceptance.add_argument("--note", help="写入验收记录的备注")
    acceptance_tests = acceptance.add_mutually_exclusive_group()
    acceptance_tests.add_argument(
        "--execute-tests",
        action="store_true",
        dest="execute_tests",
        default=None,
        help="本次验收显式执行 output.json 里的 tests",
    )
    acceptance_tests.add_argument(
        "--no-execute-tests",
        action="store_false",
        dest="execute_tests",
        help="本次验收显式不执行 tests，覆盖配置默认值",
    )
    acceptance.add_argument("--test-timeout", type=float, default=None, help="真实执行 tests 时单条测试超时秒数")
    acceptance.set_defaults(func=cmd_subagents_acceptance, apply=False)

    # LLM: acceptance-plan exposes the parent controller decision and an explicit inspect_only apply bridge.
    # 函数用途: 注册父级验收计划查看命令；默认只展示决策，--apply 仅放行低风险 inspect_only。
    acceptance_plan = sub.add_parser("subagents-acceptance-plan", help="查看单个 subagent 的父级验收 dry-run 决策")
    acceptance_plan.add_argument("run_id", help="子代理运行 ID")
    acceptance_plan.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    acceptance_plan.add_argument("--write", action="store_true", help="写入 refs-only 父级验收决策审计文件")
    acceptance_plan.add_argument("--apply", action="store_true", help="显式应用 inspect_only 低风险父级验收决策")
    acceptance_plan.add_argument("--next-action", action="store_true", help="查看父级下一步显式动作建议，不执行动作")
    acceptance_plan.add_argument("--auto-policy", action="store_true", help="查看父级自动策略 dry-run，不执行动作")
    acceptance_plan.add_argument("--auto-execution", action="store_true", help="查看父级自动执行 dry-run facade，不执行动作")
    acceptance_plan.add_argument("--execute-auto-tests", action="store_true", help="与 --auto-execution 一起显式执行 run_tests，不 apply")
    acceptance_plan.add_argument("--reviewer", default="parent", help="apply 时写入验收记录的 reviewer")
    acceptance_plan.add_argument("--note", default="", help="apply 时写入验收记录的备注")
    acceptance_plan.set_defaults(func=cmd_subagents_acceptance_plan)


# LLM: _add_agents_tests_subcommand registers the explicit test-report inspection command.
# 函数用途: 注册测试执行记录查看和手动重跑命令。
def _add_agents_tests_subcommand(sub):
    tests = sub.add_parser("subagents-tests", help="查看或显式重跑 subagent 真实测试执行记录")
    tests.add_argument("run_id", help="子代理运行 ID")
    tests.add_argument("--re-run", action="store_true", help="显式重新执行 output.json 里的 tests 并写入报告")
    tests.add_argument("--timeout", type=float, default=120.0, help="re-run 时单条测试超时秒数")
    tests.set_defaults(func=cmd_subagents_tests)


# LLM: _add_agents_patch_subcommand keeps patch review/apply argparse wiring out of the action hub.
# 函数用途: 注册 patch 审核、预演和 apply 命令参数。
def _add_agents_patch_subcommand(sub):
    patches = sub.add_parser("subagents-patches", help="审核 runner 输出里的 patch 记录")
    patch_action = patches.add_mutually_exclusive_group()
    patch_action.add_argument("--dry-run", action="store_const", const="review_dry_run", dest="patch_action", help="只生成 patch 审核报告，不修改记录")
    patch_action.add_argument("--review-apply", action="store_const", const="review_apply", dest="patch_action", help="写回 patch 审核状态，但不真正 apply 文件")
    patch_action.add_argument("--apply-dry-run", action="store_const", const="apply_dry_run", dest="patch_action", help="展示将要 apply 的 diff，不真正写文件")
    patch_action.add_argument("--apply", action="store_const", const="apply", dest="patch_action", help="真正 apply patch、跑 allowlist 测试并记录审计日志")
    patches.add_argument("--run-id", nargs="*", help="只审核指定子代理运行 ID")
    patches.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    patches.add_argument("--reviewer", default="parent", help="审核者标识")
    patches.add_argument("--note", help="写入 patch 审核记录的备注")
    patches.set_defaults(func=cmd_subagents_patches, patch_action="review_dry_run")

# LLM: _add_agents_memory_gate_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_agents_memory_gate_subcommands(sub):
    memory_gate = sub.add_parser("subagents-memory-gate", help="查看或写回 memory gate review decision")
    memory_gate.add_argument("run_id", help="子代理运行 ID")
    memory_gate_mode = memory_gate.add_mutually_exclusive_group()
    memory_gate_mode.add_argument("--retention-dry-run", action="store_true", help="只生成 retention 清理计划")
    memory_gate_mode.add_argument("--retention-apply", action="store_true", help="应用 retention：压缩 active review queue，但保留审计日志")
    memory_gate_mode.add_argument("--export-memory", action="store_true", help="把 approve_memory 候选显式导出到主 memory JSONL")
    memory_gate_mode.add_argument("--export-skill", action="store_true", help="把 approve_skill 候选显式导出为 skill draft")
    memory_gate_mode.add_argument("--verify", action="store_true", help="检查 memory gate 没有自动提升或边界破坏")
    memory_gate.add_argument("--candidate-id", help="要写回 review decision 的候选 ID；不传则只列出候选")
    memory_gate.add_argument(
        "--decision",
        choices=["approve_memory", "approve_skill", "reject", "needs_evidence"],
        default="needs_evidence",
        help="候选 review decision；只写 gate 状态，不执行提升",
    )
    memory_gate.add_argument("--reviewer", default="parent", help="reviewer 标识")
    memory_gate.add_argument("--note", help="写入 decision log 的备注")
    memory_gate.add_argument("--memory-path", help="export-memory 的目标 JSONL；默认使用当前 agent memory_path")
    memory_gate.add_argument("--skill-output-dir", help="export-skill 的草稿输出目录；默认写入 run memory_gate/skill_drafts")
    memory_gate.add_argument("--limit", type=int, default=20, help="列表模式最多显示多少条候选")
    memory_gate.set_defaults(func=cmd_subagents_memory_gate)


# LLM: _add_agents_dispatch_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_agents_dispatch_subcommands(sub):
    dispatch = sub.add_parser("subagents-dispatch", help="执行一轮父代理调度，默认 dry-run")
    _add_capability_config_arg(dispatch)
    dispatch.add_argument("--dry-run", action="store_false", dest="apply", help="只生成调度报告，不修改记录")
    dispatch.add_argument("--apply", action="store_true", help="执行低风险调度动作并写审计日志")
    dispatch.add_argument("--execute-runners", action="store_true", help="配合 --apply 调用真实模型执行 runner")
    dispatch.add_argument("--planner", action="store_true", help="有待处理事项时调用父代理 LLM planner，禁止空心 HEARTBEAT_OK")
    dispatch.add_argument("--workflow-mode", choices=["off", "plan", "auto"], default="off", help="dispatch 前对父任务执行 workflow 规划；plan 只写计划，auto 还会自动派工")
    dispatch.add_argument("--max-runners", type=int, default=1, help="本轮最多推进多少个 runner，0 表示不执行 runner")
    dispatch.add_argument("--limit", type=int, default=20, help="每个阶段最多处理多少条记录，0 表示不限制")
    dispatch.add_argument("--watch", action="store_true", help="持续循环执行 dispatch")
    dispatch.add_argument("--interval", type=float, default=30.0, help="watch 模式每轮间隔秒数，0 表示不等待")
    dispatch.add_argument("--max-cycles", type=int, default=0, help="watch 模式最多循环次数，0 表示持续运行")
    dispatch.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    dispatch.add_argument("--reviewer", default="parent-dispatch", help="patch/acceptance 审核者标识")
    dispatch.add_argument("--note", help="写入调度关联审核记录的备注")
    dispatch.add_argument("--instruction", help="给本轮 runner 的额外指令")
    dispatch.add_argument("--max-cards", type=int, default=0, help="runner 最多注入多少张能力卡，0 表示不限制")
    dispatch.add_argument("--no-probe", action="store_true", help="执行 runner 前不做通道健康检查")
    dispatch.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    dispatch.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    dispatch.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    dispatch.set_defaults(func=cmd_subagents_dispatch, apply=False)


# LLM: _add_agents_context_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_agents_context_subcommands(sub):
    subagent_context = sub.add_parser("subagent-context", help="生成单个 subagent 执行上下文包")
    subagent_context.add_argument("run_id", help="子代理运行 ID")
    subagent_context.add_argument("--max-cards", type=int, default=0, help="最多注入多少张能力卡，0 表示不限制")
    subagent_context.set_defaults(func=cmd_subagent_context)

    subagent_run = sub.add_parser("subagent-run", help="按执行上下文运行一个 subagent，默认 dry-run")
    subagent_run.add_argument("run_id", help="子代理运行 ID")
    subagent_run.add_argument("--dry-run", action="store_false", dest="execute", help="只生成 prompt 和报告，不调用模型")
    subagent_run.add_argument("--execute", action="store_true", help="真正调用模型执行，可能消耗 API")
    subagent_run.add_argument("--instruction", help="给本次 runner 的额外指令")
    subagent_run.add_argument("--max-cards", type=int, default=0, help="最多注入多少张能力卡，0 表示不限制")
    subagent_run.add_argument("--no-probe", action="store_true", help="执行前不做通道健康检查")
    subagent_run.set_defaults(func=cmd_subagent_run, execute=False)

    subagent = sub.add_parser("subagent", help="查看单个 subagent 运行详情")
    subagent.add_argument("run_id", help="子代理运行 ID")
    subagent.set_defaults(func=cmd_subagent_detail)


# LLM: add_subagents_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_subagents_subcommands(sub: argparse._SubParsersAction) -> None:
    _add_agents_basic_subcommands(sub)
    _add_agents_action_subcommands(sub)
    _add_agents_memory_gate_subcommands(sub)
    _add_agents_dispatch_subcommands(sub)
    _add_agents_context_subcommands(sub)
