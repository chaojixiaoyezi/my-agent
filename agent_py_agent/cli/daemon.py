
from __future__ import annotations

"""resolves daemon options and runs foreground recurring parent dispatch.

给人看的解释：
daemon 是'前台常驻调度器'：按间隔循环跑父代理 dispatch。
这里负责把配置和命令行参数合并成最终选项，并启动 watch_subagents。
"""

import json
import sys
from dataclasses import dataclass

from ..agent.agent_core.orchestration.dispatch.params import DispatchExecutionPlan, WatchParams
from ..agent.capability.config import load_capability_config
from ..agent.core import SimpleAgent
from .common import int_arg_or_default, make_agent, make_capability_router
from .models import DaemonOptions

# 参数减量第 3 批 B 组（2026-09-27）：前台 daemon 的默认策略不再是配置项（原 daemon_planner / daemon_interval / daemon_max_runners /
# daemon_limit / daemon_max_cycles / daemon_max_cards / daemon_probe / daemon_reviewer），值不变，命令行 flag 仍优先；
# daemon_mutate_state / daemon_start_runners / daemon_runner_instruction 仍是配置项（安全边界与用户文案）。
DAEMON_PLANNER = True  # 有待处理事项时调用父代理 LLM planner
# 每轮调度处理完后等待 30 秒再进下一轮；0 表示不等待
DAEMON_INTERVAL_SECONDS = 30  # 每轮调度结束后等多久；0 表示不等待
# 无配置时每轮最多推进 1 个 runner（原配置 auto 的保守映射）
DAEMON_DEFAULT_MAX_RUNNER_COUNT = 1  # 原配置 "auto" 的保守映射：每轮最多推进 1 个 runner
# 每个阶段最多处理 0 条记录；0 表示不限制
DAEMON_COUNT = 0  # 每个阶段最多处理多少条记录；0 表示不限制
# 最多循环 0 次；0 表示持续运行
DAEMON_MAX_CYCLE_COUNT = 0  # 最多循环次数；0 表示持续运行
# runner 最多注入 0 张能力卡；0 表示不限制
DAEMON_MAX_CARD_COUNT = 0  # runner 最多注入多少张能力卡；0 表示不限制
DAEMON_PROBE = True  # 执行 runner 前做通道健康检查
DAEMON_REVIEWER = "parent-daemon"  # patch/acceptance 审核者标识


@dataclass(frozen=True)
class DaemonNumberOptions:
    interval: float
    max_runners: int
    limit: int
    max_cycles: int
    max_cards: int


def cmd_daemon(args) -> int:

    agent = make_agent(args)
    try:
        options = _resolve_daemon_options(agent, args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    capability_config = load_capability_config(args.capability_config)
    agent.capability_config_path = args.capability_config
    router = make_capability_router(agent, capability_config, args.skill_dir)
    mode = "apply" if options.mutate_state else "dry-run"
    print("MY-AGENT DAEMON")
    print("mode=foreground")
    print(
        f"dispatch_mode={mode} planner={options.planner} start_runners={options.start_runners} "
        f"interval={options.interval} max_runners={options.max_runners} max_cycles={options.max_cycles}"
    )
    print("停止：Ctrl+C")
    try:
        report = agent.watch_subagents(
            router,
            capability_config,
            params=_daemon_watch_params(options),
        )
    except KeyboardInterrupt:
        print("\ndaemon stopped by Ctrl+C")
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("DAEMON EXITED")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    print(f"heartbeat: {agent.subagents.workspace / 'subagent_dispatch_watch_heartbeat.json'}")
    print(f"watch: {agent.subagents.workspace / 'SUBAGENT_DISPATCH_WATCH.md'}")
    if options.planner:
        print(f"planner: {agent.subagents.workspace / 'PARENT_PLANNER.md'}")
    return 0


def _daemon_watch_params(options: DaemonOptions) -> WatchParams:
    return WatchParams(
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=options.mutate_state,
            start_runners=options.start_runners,
            max_runners=options.max_runners,
        ),
        planner=options.planner,
        limit=options.limit,
        reviewer=options.reviewer,
        note=options.note,
        runner_instruction=options.instruction or "",
        max_cards=options.max_cards,
        probe=options.probe,
        take_over_by=options.take_over_by,
        locked_files=options.locked_files,
        interval=options.interval,
        max_cycles=options.max_cycles,
        advance=True,
        force_lock=options.force_lock,
    )


# LLM: 数字项现在只来自命令行（缺省是本模块常量），错误文案只提 flag；0 是显式策略值，负数才拒绝。
# 函数用途: 校验 daemon 数字参数，返回给用户看的错误文案，空串表示合法。
def _validate_daemon_numbers(numbers: DaemonNumberOptions) -> str:
    if numbers.interval < 0:
        return "--interval 不能小于 0；0 表示每轮之间不等待，通常只用于测试。"
    if numbers.max_runners < 0:
        return "--max-runners 不能小于 0；0 表示本轮不执行 runner。"
    if numbers.limit < 0:
        return "--limit 不能小于 0；0 表示不限制记录条数。"
    if numbers.max_cycles < 0:
        return "--max-cycles 不能小于 0；0 表示持续运行。"
    if numbers.max_cards < 0:
        return "--max-cards 不能小于 0；0 表示不限制。"
    return ""


# LLM: 只解析命令行 --max-runners（整数或 auto）；缺省时调用方直接用 DAEMON_DEFAULT_MAX_RUNNER_COUNT，不经过这里。
# 函数用途: 把 --max-runners 的文本转成整数，auto/空串按保守值 1。
def _resolve_daemon_max_runners(value: object) -> int:

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            # 当前 daemon 还没有后台 worker pool；auto 先映射成保守的一轮 1 个 runner。
            return 1
        try:
            return int(normalized)
        except ValueError as exc:
            raise ValueError("--max-runners 必须是整数或 auto。") from exc
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--max-runners 必须是整数或 auto。") from exc


# LLM: 只有 daemon_mutate_state / daemon_start_runners / daemon_runner_instruction 还从配置读；其余默认值是本模块常量，
#   命令行 flag 一律优先。改这里要同步 test_startup_commands 与 CLI_REFERENCE 的 daemon 表。
# 函数用途: 把配置里的安全开关、代码默认值和命令行参数合并成 daemon 运行选项。
def _resolve_daemon_options(agent: SimpleAgent, args) -> DaemonOptions:

    cfg = agent.config
    mutate_state = cfg.daemon_mutate_state if getattr(args, "apply", None) is None else args.apply
    start_runners = (
        cfg.daemon_start_runners
        if getattr(args, "start_runners", None) is None
        else args.start_runners
    )
    planner = DAEMON_PLANNER if getattr(args, "planner", None) is None else args.planner
    if start_runners and not mutate_state:
        raise ValueError("daemon_start_runners / --start-runners 必须和 daemon_mutate_state / --apply 一起使用。")

    interval, max_runners, limit, max_cycles, max_cards = _daemon_numeric_options(args)
    reviewer = getattr(args, "reviewer", None) or DAEMON_REVIEWER
    instruction = getattr(args, "instruction", None)
    instruction = cfg.daemon_runner_instruction if instruction is None else instruction
    probe = False if getattr(args, "no_probe", False) else DAEMON_PROBE

    invalid_number = _validate_daemon_numbers(DaemonNumberOptions(interval, max_runners, limit, max_cycles, max_cards))
    if invalid_number:
        raise ValueError(invalid_number)

    note, take_over_by, locked_files, force_lock = _daemon_boundary_options(args)
    return DaemonOptions(
        mutate_state=mutate_state,
        start_runners=start_runners,
        planner=planner,
        interval=interval,
        max_runners=max_runners,
        limit=limit,
        max_cycles=max_cycles,
        max_cards=max_cards,
        reviewer=reviewer,
        instruction=instruction,
        probe=probe,
        note=note,
        take_over_by=take_over_by,
        locked_files=locked_files,
        force_lock=force_lock,
    )


# LLM: 数字项只有命令行一个来源，缺省用本模块常量；不要在这里再读配置。
# 函数用途: 合并命令行数字参数与代码默认值（间隔、每轮 runner 数、记录条数、循环次数、能力卡数）。
def _daemon_numeric_options(args) -> tuple[float, int, int, int, int]:
    interval = getattr(args, "interval", None)
    interval = DAEMON_INTERVAL_SECONDS if interval is None else interval
    raw_max_runners = getattr(args, "max_runners", None)
    max_runners = DAEMON_DEFAULT_MAX_RUNNER_COUNT if raw_max_runners is None else _resolve_daemon_max_runners(raw_max_runners)
    limit = int_arg_or_default(args, "limit", DAEMON_COUNT)
    max_cycles = int_arg_or_default(args, "max_cycles", DAEMON_MAX_CYCLE_COUNT)
    max_cards = int_arg_or_default(args, "max_cards", DAEMON_MAX_CARD_COUNT)
    return interval, max_runners, limit, max_cycles, max_cards


def _daemon_boundary_options(args) -> tuple[str, str, list[str], bool]:
    return (
        getattr(args, "note", None) or "",
        getattr(args, "take_over_by", None) or "",
        getattr(args, "locked_file", None) or [],
        bool(getattr(args, "force_lock", False)),
    )
