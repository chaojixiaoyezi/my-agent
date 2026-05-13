# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""local doctor and local rebuild CLI commands.

给人看的解释：
这两个命令从 local_commands.py 拆出来，让那个文件不超 400 行。
local-doctor 诊断 LocalStore、gateway 队列和 subagent 文件事实源的一致性。
local-rebuild 从文件事实源重建 LocalStore 索引。
"""

import json
import sys

from ..agent.gateway import (
    gateway_paths,
    recover_gateway_processing_requests,
)
from .common import make_agent
from .local_doctor import build_local_doctor_report, rebuild_local_store
from .models import LocalDoctorOptions, LocalRebuildOptions


# LLM: cmd_local_doctor 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_local_doctor(args) -> int:

    agent = make_agent(args)
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, "cli_local_doctor_limit", 20) or 0)
    options = _local_doctor_options(args)
    if options.repair:
        recovery = recover_gateway_processing_requests(
            gateway_paths(agent),
            startup=False,
            max_attempts=agent.config.gateway_request_max_attempts,
            timeout_seconds=agent.config.gateway_processing_timeout_seconds,
            agent=agent,
        )
    else:
        recovery = {}
    report = build_local_doctor_report(agent, limit=options.limit)
    if recovery:
        report["repair"] = {"gateway_processing_recovery": recovery}
    if options.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1

    print("MY-AGENT LOCAL DOCTOR")
    print(f"ok={report['ok']}")
    print("stats=" + json.dumps(report["stats"], ensure_ascii=False, sort_keys=True))
    print("source_counts=" + json.dumps(report["source_counts"], ensure_ascii=False, sort_keys=True))
    if recovery:
        print("repair=" + json.dumps(report["repair"], ensure_ascii=False, sort_keys=True))
    for check in report["checks"]:
        mark = "OK" if check["ok"] else check["severity"]
        print(f"- [{mark}] {check['name']}: {check['message']}")
    if report["suggestions"]:
        print("")
        print("Suggested Actions")
        for item in report["suggestions"]:
            print(f"- {item}")
    return 0 if report["ok"] else 1


# LLM: cmd_local_rebuild 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_local_rebuild(args) -> int:

    agent = make_agent(args)
    options = _local_rebuild_options(args)
    requested = set(options.sources)
    if "all" in requested:
        requested = {"memory", "gateway", "subagent", "fts"}
    allowed = {"memory", "gateway", "subagent", "fts"}
    unknown = sorted(requested - allowed)
    if unknown:
        print(f"未知 source: {', '.join(unknown)}", file=sys.stderr)
        return 2
    result = rebuild_local_store(agent, sources=requested, reset=options.reset)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# LLM: _local_doctor_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _local_doctor_options(args) -> LocalDoctorOptions:
    return LocalDoctorOptions(
        repair=bool(args.repair),
        limit=int(args.limit or 0),
        json=bool(args.json),
    )


# LLM: _local_rebuild_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _local_rebuild_options(args) -> LocalRebuildOptions:
    return LocalRebuildOptions(
        sources=set(args.source or ["memory", "gateway", "subagent", "fts"]),
        reset=bool(args.reset),
    )
