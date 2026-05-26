# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""resolves daemon options and runs foreground recurring parent dispatch.

给人看的解释：
daemon 是'前台常驻调度器'：按间隔循环跑父代理 dispatch。
这里负责把配置和命令行参数合并成最终选项，并启动 watch_subagents。
"""

import json
import sys
from dataclasses import dataclass

from ..agent.agent_core.dispatch_params import WatchParams
from ..agent.capability_config import load_capability_config
from ..agent.core import SimpleAgent
from .common import make_agent, make_capability_router
from .models import DaemonOptions


# LLM: DaemonNumberOptions 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class DaemonNumberOptions:
    interval: float
    max_runners: int
    limit: int
    max_cycles: int
    max_cards: int


# LLM: cmd_daemon 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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
    mode = "apply" if options.apply else "dry-run"
    print("MY-AGENT DAEMON")
    print("mode=foreground")
    print(
        f"dispatch_mode={mode} planner={options.planner} execute_runners={options.execute_runners} "
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


# LLM: _daemon_watch_params 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _daemon_watch_params(options: DaemonOptions) -> WatchParams:
    return WatchParams(
        apply=options.apply,
        execute_runners=options.execute_runners,
        planner=options.planner,
        max_runners=options.max_runners,
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


# LLM: _validate_daemon_numbers 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _validate_daemon_numbers(numbers: DaemonNumberOptions) -> str:
    if numbers.interval < 0:
        return "daemon_interval / --interval 不能小于 0；0 表示每轮之间不等待，通常只用于测试。"
    if numbers.max_runners < 0:
        return "daemon_max_runners / --max-runners 不能小于 0；0 表示本轮不执行 runner。"
    if numbers.limit < 0:
        return "daemon_limit / --limit 不能小于 0；0 表示不限制记录条数。"
    if numbers.max_cycles < 0:
        return "daemon_max_cycles / --max-cycles 不能小于 0；0 表示持续运行。"
    if numbers.max_cards < 0:
        return "daemon_max_cards / --max-cards 不能小于 0；0 表示不限制。"
    return ""


# LLM: _resolve_daemon_max_runners 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_daemon_max_runners(value: object) -> int:

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            # 当前 daemon 还没有后台 worker pool；auto 先映射成保守的一轮 1 个 runner。
            return 1
        try:
            return int(normalized)
        except ValueError as exc:
            raise ValueError("daemon_max_runners / --max-runners 必须是整数或 auto。") from exc
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("daemon_max_runners / --max-runners 必须是整数或 auto。") from exc


# LLM: _resolve_daemon_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_daemon_options(agent: SimpleAgent, args) -> DaemonOptions:

    cfg = agent.config
    apply = cfg.daemon_apply if getattr(args, "apply", None) is None else args.apply
    execute_runners = (
        cfg.daemon_execute_runners
        if getattr(args, "execute_runners", None) is None
        else args.execute_runners
    )
    planner = cfg.daemon_planner if getattr(args, "planner", None) is None else args.planner
    if execute_runners and not apply:
        raise ValueError("daemon_execute_runners / --execute-runners 必须和 daemon_apply / --apply 一起使用。")

    interval, max_runners, limit, max_cycles, max_cards = _daemon_numeric_options(args, cfg)
    reviewer = getattr(args, "reviewer", None) or cfg.daemon_reviewer
    instruction = getattr(args, "instruction", None)
    instruction = cfg.daemon_runner_instruction if instruction is None else instruction
    probe = False if getattr(args, "no_probe", False) else cfg.daemon_probe

    invalid_number = _validate_daemon_numbers(DaemonNumberOptions(interval, max_runners, limit, max_cycles, max_cards))
    if invalid_number:
        raise ValueError(invalid_number)

    note, take_over_by, locked_files, force_lock = _daemon_boundary_options(args)
    return DaemonOptions(
        apply=apply,
        execute_runners=execute_runners,
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


# LLM: _daemon_numeric_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _daemon_numeric_options(args, cfg) -> tuple[float, int, int, int, int]:
    interval = getattr(args, "interval", None)
    interval = cfg.daemon_interval if interval is None else interval
    raw_max_runners = getattr(args, "max_runners", None)
    raw_max_runners = cfg.daemon_max_runners if raw_max_runners is None else raw_max_runners
    max_runners = _resolve_daemon_max_runners(raw_max_runners)
    limit = getattr(args, "limit", None)
    limit = cfg.daemon_limit if limit is None else limit
    max_cycles = getattr(args, "max_cycles", None)
    max_cycles = cfg.daemon_max_cycles if max_cycles is None else max_cycles
    max_cards = getattr(args, "max_cards", None)
    max_cards = cfg.daemon_max_cards if max_cards is None else max_cards
    return interval, max_runners, limit, max_cycles, max_cards


# LLM: _daemon_boundary_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _daemon_boundary_options(args) -> tuple[str, str, list[str], bool]:
    return (
        getattr(args, "note", None) or "",
        getattr(args, "take_over_by", None) or "",
        getattr(args, "locked_file", None) or [],
        bool(getattr(args, "force_lock", False)),
    )
