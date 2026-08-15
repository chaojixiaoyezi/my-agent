from __future__ import annotations

"""统一 Memory v2 管理命令的 argparse 注册。"""

# LLM: 管理员入口只暴露一个 `memory` 命令树；旧 flat 命令不得发展成第二套候选或保留期主链。
# 模块用途: 为 Candidate、Curator、Retention、Doctor 和 Migration 提供中文参数说明。

import argparse

from ..agent.memory_api import (
    CANDIDATE_STATUSES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
)
from .memory_admin_commands import (
    cmd_memory_candidates_list,
    cmd_memory_candidates_promote,
    cmd_memory_candidates_review,
    cmd_memory_curator_recover,
    cmd_memory_curator_run,
    cmd_memory_curator_status,
    cmd_memory_migrate,
    cmd_memory_retention_apply,
    cmd_memory_retention_plan,
    cmd_memory_v2_doctor,
)

_CANDIDATE_STATUS_HELP = (
    "状态：observed=已观察，pending_review=待审核，approved=已批准，promoted=已晋升；"
    "rejected=已拒绝，superseded=已被替代，expired=已过期，"
    "blocked_missing_evidence=缺少证据，blocked_conflict=存在冲突。"
)
_ACTION_HELP = "动作：add=新增，replace=替换，remove=删除，merge=合并，none=不处理。"
_TARGET_HELP = (
    "落点：long_term=正式长期记忆，user=用户画像，lesson=正式教训，hot=高频规则，"
    "soul=AI 人格，agents=长期协作约定，none=不晋升。"
)


# LLM: 顶层 `memory` 只负责分派到同一组 v2 Service，不直接读写任何 Memory 文件。
# 函数用途: 注册统一 Memory 管理命令树。
def add_memory_admin_subcommands(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "memory",
        help="统一管理 Memory v2 候选、后台策展、保留期、诊断与迁移",
        description=(
            "统一 Memory v2 管理入口。所有子命令复用正式 Service，"
            "不会创建第二套候选账本或长期记忆写入路径。"
        ),
    )
    areas = parser.add_subparsers(dest="memory_area", required=True)
    _add_candidate_commands(areas)
    _add_curator_commands(areas)
    _add_retention_commands(areas)
    _add_doctor_command(areas)
    _add_migrate_command(areas)


# LLM: Candidate 的所有状态修改必须按精确 candidate_id 进入唯一状态机。
# 函数用途: 注册候选列出、审核和正式晋升命令。
def _add_candidate_commands(areas: argparse._SubParsersAction) -> None:
    parser = areas.add_parser(
        "candidates",
        help="查看和审核 owner 级唯一候选账本",
        description="memory/candidates.jsonl 是唯一候选事实源。" + _CANDIDATE_STATUS_HELP,
    )
    actions = parser.add_subparsers(dest="memory_candidates_action", required=True)

    listing = actions.add_parser("list", help="按状态列出候选")
    listing.add_argument(
        "--status",
        action="append",
        choices=sorted(CANDIDATE_STATUSES),
        help="按候选状态过滤，可重复传入。" + _CANDIDATE_STATUS_HELP,
    )
    listing.add_argument("--limit", type=int, default=50, help="最多返回多少条；0 表示不限制，默认 50")
    listing.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    listing.set_defaults(func=cmd_memory_candidates_list)

    review = actions.add_parser(
        "review",
        help="审核一条精确候选；不能按正文模糊选择",
        description=(
            "审核决定：approve=批准，reject=拒绝，reopen=从阻塞态退回待审核，"
            "expire=标记过期，supersede=标记已被替代。"
        ),
    )
    review.add_argument("candidate_id", help="候选稳定编号 candidate_id")
    review.add_argument(
        "--decision",
        required=True,
        choices=["approve", "reject", "reopen", "expire", "supersede"],
        help="本次审核决定；批准不等于已经写入正式 Memory",
    )
    _add_review_metadata(review)
    review.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    review.set_defaults(func=cmd_memory_candidates_review)

    promote = actions.add_parser("promote", help="经唯一 PromotionService 晋升已批准候选")
    promote.add_argument("candidate_id", help="候选稳定编号 candidate_id")
    promote.add_argument("--reviewer", default="memory-admin-cli", help="审核者标识，默认 memory-admin-cli")
    promote.add_argument(
        "--automatic",
        action="store_true",
        help="按 conservative_v1 自动策略核验；不放宽证据或冲突规则",
    )
    promote.add_argument(
        "--confirmed",
        action="store_true",
        help="确认受保护 Persona 变更；SOUL/AGENTS 仍需此前存在用户明确确认",
    )
    promote.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    promote.set_defaults(func=cmd_memory_candidates_promote)


# LLM: 审核元数据只补充 typed action/target，不允许用自由文本覆盖状态机。
# 函数用途: 注册审核者、说明、动作和精确目标参数。
def _add_review_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reviewer", default="memory-admin-cli", help="审核者标识，默认 memory-admin-cli")
    parser.add_argument("--note", default="", help="人类审核说明；不参与机器控制流")
    parser.add_argument(
        "--proposed-action",
        choices=sorted(PROPOSED_ACTIONS),
        help="可选：修正正式动作。" + _ACTION_HELP,
    )
    parser.add_argument(
        "--target-entry-id",
        help="replace/remove/merge 必须精确指定的正式 entry_id",
    )
    parser.add_argument(
        "--promotion-target",
        choices=sorted(PROMOTION_TARGETS),
        help="可选：修正正式落点。" + _TARGET_HELP,
    )


# LLM: CLI 的显式触发也必须先写同一 state.json 请求，再由同一 CuratorService 执行。
# 函数用途: 注册后台策展状态和管理员触发命令。
def _add_curator_commands(areas: argparse._SubParsersAction) -> None:
    parser = areas.add_parser(
        "curator",
        help="查看或显式运行后台 Memory Curator",
        description=(
            "Curator 只输出严格 daily_events/candidates；provider=后台服务商，"
            "model=后台模型，游标、lease、失败码均来自 memory/curator/state.json。"
        ),
    )
    actions = parser.add_subparsers(dest="memory_curator_action", required=True)
    status = actions.add_parser("status", help="查看配置、游标、待处理 reason 和 active lease")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_memory_curator_status)
    run = actions.add_parser("run", help="登记 admin reason 并立即尝试执行一个有界批次")
    run.add_argument(
        "--force",
        action="store_true",
        help="即使 memory_curator_enabled=false 也执行；仍遵守 Schema、证据和权限边界",
    )
    run.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    run.set_defaults(func=cmd_memory_curator_run)
    recover = actions.add_parser(
        "recover",
        help="人工恢复损坏隔离的 Curator state（凭权威审计链重建游标）",
        description=(
            "结构化恢复通道: 校验隔离副本 hash 与哨兵一致、run_log 存在成功审计且"
            "游标未分裂后才执行; 恢复事件落 run_log 审计。任一校验失败拒绝执行,"
            "不伪造自愈成功。不能由普通文本或手动删哨兵触发。"
        ),
    )
    recover.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    recover.set_defaults(func=cmd_memory_curator_recover)


# LLM: plan/apply 是同一 RetentionService 的只读和执行阶段，不能保留另一套 scanner。
# 函数用途: 注册保留期预览与显式应用命令。
def _add_retention_commands(areas: argparse._SubParsersAction) -> None:
    parser = areas.add_parser(
        "retention",
        help="按结构化 v2 策略预览或执行保留期清理",
        description=(
            "plan=只读计划；apply=锁内重新规划并逐项重验证后执行。"
            "legal hold、非终态任务和损坏状态都会关闭式阻断删除。"
        ),
    )
    actions = parser.add_subparsers(dest="memory_retention_action", required=True)
    plan = actions.add_parser("plan", help="只读生成清理计划，不修改任何文件")
    plan.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    plan.set_defaults(func=cmd_memory_retention_plan)
    apply = actions.add_parser("apply", help="显式执行当前计划；目标会在执行前重新验证")
    apply.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    apply.set_defaults(func=cmd_memory_retention_apply)


# LLM: doctor 汇总正式仓库健康，不修复、不迁移也不执行 retention。
# 函数用途: 注册 Memory v2 只读诊断命令。
def _add_doctor_command(areas: argparse._SubParsersAction) -> None:
    parser = areas.add_parser("doctor", help="只读诊断 Candidate、Curator、迁移和保留期状态")
    parser.add_argument("--index", help="可选：指定 routing/INDEX.md；相对路径按工作区解析")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=cmd_memory_v2_doctor)


# LLM: migrate 默认必须 dry-run，只有显式 --apply 才备份并迁移旧数据。
# 函数用途: 注册一次性 Memory v2 迁移命令。
def _add_migrate_command(areas: argparse._SubParsersAction) -> None:
    parser = areas.add_parser(
        "migrate",
        help="检测或安全迁移旧 Memory 数据；默认只读",
        description="默认 dry-run；--apply 会先生成完整备份和 manifest，失败时回滚。",
    )
    parser.add_argument("--apply", action="store_true", help="显式应用迁移；不传时只生成报告")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=cmd_memory_migrate)


__all__ = ["add_memory_admin_subcommands"]
