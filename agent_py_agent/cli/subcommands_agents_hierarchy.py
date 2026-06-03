
from __future__ import annotations

from .common import DEFAULT_CAPABILITY_CONFIG
from .subagents import (
    cmd_subagents_hierarchy,
    cmd_subagents_leadership_recovery_apply,
    cmd_subagents_leadership_recovery_plan,
    cmd_subagents_recovery_tree,
)


def _add_capability_config_arg(p) -> None:
    p.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )


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
