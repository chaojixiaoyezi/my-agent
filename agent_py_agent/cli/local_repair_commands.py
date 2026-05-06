from __future__ import annotations

"""LLM: local doctor and local rebuild CLI commands.

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


def cmd_local_doctor(args) -> int:

    agent = make_agent(args)
    if args.repair:
        recovery = recover_gateway_processing_requests(
            gateway_paths(agent),
            startup=False,
            max_attempts=agent.config.gateway_request_max_attempts,
            timeout_seconds=agent.config.gateway_processing_timeout_seconds,
            agent=agent,
        )
    else:
        recovery = {}
    report = build_local_doctor_report(agent, limit=args.limit)
    if recovery:
        report["repair"] = {"gateway_processing_recovery": recovery}
    if args.json:
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


def cmd_local_rebuild(args) -> int:

    agent = make_agent(args)
    requested = set(args.source or ["memory", "gateway", "subagent", "fts"])
    if "all" in requested:
        requested = {"memory", "gateway", "subagent", "fts"}
    allowed = {"memory", "gateway", "subagent", "fts"}
    unknown = sorted(requested - allowed)
    if unknown:
        print(f"未知 source: {', '.join(unknown)}", file=sys.stderr)
        return 2
    result = rebuild_local_store(agent, sources=requested, reset=args.reset)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
