
from __future__ import annotations

"""implements CLI commands for subagent boards, due-checks, actions, routing, reviews, dispatch, and runner entrypoints.

给人看的解释：
这个文件只管用户在命令行里怎么操作子代理。
实际状态机和文件写入仍然交给 SimpleAgent/SubAgentManager，这里主要做参数转发和结果打印。
最终收口计划入口也只导出只读 dry-run 命令，避免 CLI 聚合层隐式触发 tests 或状态写回。
"""

import argparse

from agent_py_agent.cli._actions import (
    cmd_subagents_apply_actions,
    cmd_subagents_plan_actions,
    cmd_subagents_route_capabilities,
)
from agent_py_agent.cli._board import cmd_spawn, cmd_subagent_detail, cmd_subagents
from agent_py_agent.cli._dispatch import (
    cmd_subagent_run,
    cmd_subagents_dispatch,
)
from agent_py_agent.cli._hierarchy import cmd_subagents_hierarchy, cmd_subagents_recovery_tree
from agent_py_agent.cli._inspection import (
    cmd_subagent_context,
    cmd_subagents_budget,
    cmd_subagents_due_check,
    cmd_subagents_probe,
)
from agent_py_agent.cli._leadership import (
    cmd_subagents_leadership_recovery_apply,
    cmd_subagents_leadership_recovery_plan,
)
from agent_py_agent.cli._review import (
    cmd_subagents_patches,
    cmd_subagents_tests,
)
from agent_py_agent.cli.common import DEFAULT_CAPABILITY_CONFIG


def _add_capability_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )


def _add_agents_basic_subcommands(sub):
    spawn = sub.add_parser("spawn-subagents", help="拆分并创建 subagent 任务记录")
    spawn.add_argument("goal", help="要拆分的目标")
    spawn.add_argument("--count", type=int, default=None, help="子代理数量；默认读配置")
    spawn.add_argument("--role", default="worker", help="创建角色，如 worker/coordinator")
    spawn.add_argument("--agent-name", default="", help="显式 agent 名称，常用于 root/coordinator E2E")
    spawn.set_defaults(func=cmd_spawn)

    subagents = sub.add_parser("subagents", help="查看 subagent 红绿灯看板")
    subagents.add_argument("--all", action="store_true", help="显示全部记录，而不是默认的红灯/最近记录")
    subagents.add_argument("--status", help="按状态过滤，如 BLOCKED/DONE/TAKEN_OVER")
    subagents.add_argument("--owner", help="按 owner/supervisor/final_owner 过滤")
    subagents.add_argument("--root-id", help="按根任务 ID 过滤")
    subagents.add_argument("--limit", type=int, default=None, help="最多显示多少条；默认读配置")
    subagents.set_defaults(func=cmd_subagents)

    add_agents_leadership_subcommands(sub)
    add_agents_hierarchy_subcommands(sub)
    _add_agents_monitoring_subcommands(sub)


def add_agents_leadership_subcommands(sub):
    recovery_plan = sub.add_parser(
        "subagents-leadership-recovery-plan",
        help="Preview batch stale-coordinator leader handoffs",
    )
    _add_capability_config_arg(recovery_plan)
    recovery_plan.add_argument("--root-id", required=True, help="只规划指定 root subagent 任务树")
    recovery_plan.add_argument("--leader", action="append", required=True, help="候选新 leader run_id，可重复")
    recovery_plan.add_argument(
        "--max-children-per-leader",
        type=int,
        default=None,
        help="每个候选 leader 最多接多少个直接孩子；默认读配置",
    )
    recovery_plan.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    recovery_plan.set_defaults(func=cmd_subagents_leadership_recovery_plan)

    recovery_apply = sub.add_parser("subagents-leadership-recovery-apply", help="Apply one explicit child-subset leader handoff")
    recovery_apply.add_argument("--dry-run", action="store_false", dest="apply", help="只预览，不修改任务树")
    recovery_apply.add_argument("--apply", action="store_true", help="真正重挂指定 child 子树")
    recovery_apply.add_argument("--root-id", help="限制在指定 root subagent 任务树内")
    recovery_apply.add_argument("--coordinator", required=True, help="旧 coordinator run_id")
    recovery_apply.add_argument("--leader", required=True, help="新 leader run_id")
    recovery_apply.add_argument("--child-run-id", action="append", required=True, help="要重挂的直接 child run_id，可重复")
    recovery_apply.add_argument("--max-children-per-leader", type=int, default=None, help="可选 leader 直接 child 容量上限；0 表示不检查；默认读配置")
    recovery_apply.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    recovery_apply.set_defaults(func=cmd_subagents_leadership_recovery_apply, apply=False)


def add_agents_hierarchy_subcommands(sub):
    hierarchy = sub.add_parser("subagents-hierarchy", help="Preview or create child/grandchild subagent runs")
    hierarchy.add_argument("run_id", help="父级 subagent 运行 ID")
    hierarchy.add_argument(
        "--child",
        action="append",
        required=True,
        help="待创建子任务，格式 ROLE:AGENT_NAME:GOAL；可重复",
    )
    hierarchy.add_argument("--apply", action="store_true", help="真正创建 child runs；默认只 dry-run")
    hierarchy.add_argument("--requested-by", default="parent", help="调度请求来源，用于审计摘要")
    hierarchy.add_argument("--max-children", type=int, default=0, help="父级最多 child 数；0 表示不限制")
    hierarchy.add_argument("--max-depth", type=int, default=None, help="允许创建的最大层级深度；默认读配置")
    hierarchy.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    hierarchy.set_defaults(func=cmd_subagents_hierarchy)

    recovery = sub.add_parser("subagents-recovery-tree", help="查询 root subagent 的多层恢复交接包")
    _add_capability_config_arg(recovery)
    recovery.add_argument("run_id", help="根 subagent 运行 ID")
    recovery.add_argument("--hide-healthy", action="store_true", help="只展示 root 和需要恢复的节点")
    recovery.add_argument("--requested-by", default="parent", help="查询请求来源，用于审计摘要")
    recovery.add_argument("--max-nodes", type=int, default=None, help="最多扫描多少个子树节点；默认读配置")
    recovery.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    recovery.set_defaults(func=cmd_subagents_recovery_tree)


def _add_agents_monitoring_subcommands(sub):
    due_check = sub.add_parser("subagents-due-check", help="巡检 subagent 并输出父代理待处理项")
    _add_capability_config_arg(due_check)
    due_check.add_argument("--root-id", help="只巡检指定 root subagent 任务树")
    due_check.add_argument("--all", action="store_true", help="显示全部问题，而不是按 limit 截断")
    due_check.add_argument("--limit", type=int, default=None, help="最多显示多少条问题；默认读配置")
    due_check.set_defaults(func=cmd_subagents_due_check)

    budget = sub.add_parser("subagents-budget", help="汇总 subagent runner 调用、工具轮数和 token 预算")
    budget.add_argument("--root-id", help="只统计指定 root subagent 任务树")
    budget.add_argument("--max-model-calls", type=int, default=0, help="模型调用数阈值；0 表示不检查")
    budget.add_argument("--max-tool-rounds", type=int, default=0, help="工具轮数阈值；0 表示不检查")
    budget.add_argument(
        "--max-prompt-response-tokens",
        type=int,
        default=0,
        help="prompt+response 粗略 token 阈值；0 表示不检查",
    )
    budget.add_argument("--include-dry-runs", action="store_true", help="把 dry-run prompt 也计入记录")
    budget.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    budget.set_defaults(func=cmd_subagents_budget)

    probe = sub.add_parser("subagents-probe", help="检查 subagent 通道健康状态")
    probe.add_argument("run_id", nargs="*", help="子代理运行 ID；不传则检查最近记录")
    probe.add_argument("--limit", type=int, default=None, help="不指定 run_id 时最多检查多少条；默认读配置")
    probe.set_defaults(func=cmd_subagents_probe)

    action_plan = sub.add_parser("subagents-plan-actions", help="根据 due-check 生成 dry-run 动作计划")
    _add_capability_config_arg(action_plan)
    action_plan.add_argument("--all", action="store_true", help="显示全部动作，而不是按 limit 截断")
    action_plan.add_argument("--limit", type=int, default=None, help="最多显示多少条动作；默认读配置")
    action_plan.add_argument("--root-id", help="只为指定 root subagent 任务树生成动作计划")
    action_plan.set_defaults(func=cmd_subagents_plan_actions)


def _add_agents_action_subcommands(sub):
    apply_actions = sub.add_parser("subagents-apply-actions", help="执行或 dry-run 执行 action plan")
    _add_capability_config_arg(apply_actions)
    apply_actions.add_argument("--dry-run", action="store_false", dest="apply", help="只预览动作，不修改记录")
    apply_actions.add_argument("--apply", action="store_true", help="真正执行低风险动作")
    apply_actions.add_argument("--action", help="只处理指定动作，如 reopen_for_evidence")
    apply_actions.add_argument("--run-id", help="只处理指定子代理运行 ID")
    apply_actions.add_argument("--limit", type=int, default=None, help="最多处理多少条动作；默认读配置")
    apply_actions.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    apply_actions.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    apply_actions.set_defaults(func=cmd_subagents_apply_actions, apply=False)

    route = sub.add_parser("subagents-route-capabilities", help="路由 OPEN capability request")
    _add_capability_config_arg(route)
    route.add_argument("--dry-run", action="store_false", dest="apply", help="只预览路由，不生成 grant/gap")
    route.add_argument("--apply", action="store_true", help="真正生成 capability grant 或 gap")
    route.add_argument("--run-id", nargs="*", help="只处理指定子代理运行 ID")
    route.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    route.add_argument("--limit", type=int, default=None, help="最多处理多少条 request；默认读配置")
    route.set_defaults(func=cmd_subagents_route_capabilities, apply=False)

    _add_agents_tests_subcommand(sub)
    _add_agents_patch_subcommand(sub)


def _add_agents_tests_subcommand(sub):
    tests = sub.add_parser("subagents-tests", help="查看或显式重跑 subagent 真实测试执行记录")
    tests.add_argument("run_id", help="子代理运行 ID")
    tests.add_argument("--re-run", action="store_true", help="显式重新执行 output.json 里的 tests 并写入报告")
    tests.add_argument("--timeout", type=float, default=120.0, help="re-run 时单条测试超时秒数")
    tests.set_defaults(func=cmd_subagents_tests)


def _add_agents_patch_subcommand(sub):
    patches = sub.add_parser("subagents-patches", help="审核 runner 输出里的 patch 记录")
    patch_action = patches.add_mutually_exclusive_group()
    patch_action.add_argument("--dry-run", action="store_const", const="review_dry_run", dest="patch_action", help="只生成 patch 审核报告，不修改记录")
    patch_action.add_argument("--review-apply", action="store_const", const="review_apply", dest="patch_action", help="写回 patch 审核状态，但不真正 apply 文件")
    patch_action.add_argument("--apply-dry-run", action="store_const", const="apply_dry_run", dest="patch_action", help="展示将要 apply 的 diff，不真正写文件")
    patch_action.add_argument("--apply", action="store_const", const="apply", dest="patch_action", help="真正 apply patch、跑 allowlist 测试并记录审计日志")
    patches.add_argument("--run-id", nargs="*", help="只审核指定子代理运行 ID")
    patches.add_argument("--limit", type=int, default=None, help="最多处理多少条记录；默认读配置")
    patches.add_argument("--reviewer", default="parent", help="审核者标识")
    patches.add_argument("--note", help="写入 patch 审核记录的备注")
    patches.set_defaults(func=cmd_subagents_patches, patch_action="review_dry_run")


def _add_agents_dispatch_subcommands(sub):
    dispatch = sub.add_parser("subagents-dispatch", help="执行一轮父代理调度，默认 dry-run")
    _add_capability_config_arg(dispatch)
    dispatch.add_argument("--dry-run", action="store_false", dest="apply", help="只生成调度报告，不修改记录")
    dispatch.add_argument("--apply", action="store_true", help="执行低风险调度动作并写审计日志")
    dispatch.add_argument("--start-runners", action="store_true", help="配合 --apply 调用真实模型执行 runner")
    dispatch.add_argument("--planner", action="store_true", help="有待处理事项时调用父代理 LLM planner，禁止空心 HEARTBEAT_OK")
    dispatch.add_argument("--max-runners", type=int, default=None, help="本轮最多推进多少个 runner，0 表示不执行 runner；默认读配置")
    dispatch.add_argument("--limit", type=int, default=None, help="每个阶段最多处理多少条记录，0 表示不限制；默认读配置")
    dispatch.add_argument("--watch", action="store_true", help="持续循环执行 dispatch")
    dispatch.add_argument("--advance", action="store_true", help="watch 模式显式推进 dispatch；不传则只读观察代理树")
    dispatch.add_argument("--interval", type=float, default=None, help="watch 模式每轮间隔秒数，0 表示不等待；默认读配置")
    dispatch.add_argument("--max-cycles", type=int, default=0, help="watch 模式最多循环次数，0 表示持续运行")
    dispatch.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    dispatch.add_argument("--reviewer", default="parent-dispatch", help="patch/acceptance 审核者标识")
    dispatch.add_argument("--note", help="写入调度关联审核记录的备注")
    dispatch.add_argument("--instruction", help="给本轮 runner 的额外指令")
    dispatch.add_argument("--run-id", action="append", default=[], help="只推进指定子代理 run_id；可多次传入")
    dispatch.add_argument("--background-launch-id", default="", help="内部字段：标记 create_subagents 后台启动生命周期")
    dispatch.add_argument("--workspace-root", default="", help=argparse.SUPPRESS)
    dispatch.add_argument("--max-cards", type=int, default=0, help="runner 最多注入多少张能力卡，0 表示不限制")
    dispatch.add_argument("--no-probe", action="store_true", help="执行 runner 前不做通道健康检查")
    dispatch.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    dispatch.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    dispatch.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    dispatch.set_defaults(func=cmd_subagents_dispatch, apply=False)


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


def add_subagents_subcommands(sub: argparse._SubParsersAction) -> None:
    _add_agents_basic_subcommands(sub)
    _add_agents_action_subcommands(sub)
    _add_agents_dispatch_subcommands(sub)
    _add_agents_context_subcommands(sub)
