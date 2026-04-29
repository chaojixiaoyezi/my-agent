from __future__ import annotations

"""LLM contract: CLI command wiring and process orchestration entrypoint.

Human version:
这个文件只负责“命令怎么进来、参数怎么组装、结果怎么打印、后台线程怎么启动”。
具体协议细节要放到对应模块里，比如 gateway 文件队列已经拆到 `agent.gateway`。
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .agent.backend import ModelResponse
from .agent.capabilities import CapabilityRouter
from .agent.capability_config import load_capability_config
from .agent.config import load_config
from .agent.core import SimpleAgent
from .agent.gateway import (
    AdapterPaths,
    GatewayPaths,
    _handle_gateway_request,
    _process_gateway_requests,
    adapter_paths,
    gateway_paths,
    gateway_request_counts,
    gateway_response_path,
    gateway_running,
    gateway_stale_processing,
    is_pid_alive,
    log_gateway_event,
    new_gateway_request_id,
    print_gateway_response,
    process_file_adapter_once,
    read_json_file,
    read_pid,
    rebuild_gateway_index,
    recover_gateway_processing_requests,
    render_gateway_status,
    requeue_gateway_processing_requests,
    submit_gateway_ask,
    tail_lines,
    terminate_pid,
    wait_for_gateway_response,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_json_file,
)
from .agent.skills import SkillRegistry
from .agent.subagent import VerificationEvidence, filter_board_items

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover - 让项目在无额外依赖时仍能跑
    PromptSession = None
    patch_stdout = None

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"
DEFAULT_CAPABILITY_CONFIG = ROOT / "config" / "capability_config.yaml"
CHAT_PROMPT = "你> "
FALLBACK_CHAT_PROMPT = "user> "


@dataclass
class ChatJob:
    """chat 模式里排队执行的一条模型请求。"""

    user: str
    show_prompt: bool
    inject: list[str]
    prompt_files: list[str]


@dataclass
class DaemonOptions:
    """daemon/gateway 共享的调度选项。"""

    apply: bool
    execute_runners: bool
    planner: bool
    interval: float
    max_runners: int
    limit: int
    max_cycles: int
    max_cards: int
    reviewer: str
    instruction: str
    probe: bool


def configure_stdio() -> None:
    """把标准输出尽量固定到 UTF-8。

    这样做主要是为了避免 Windows 终端在打印模型返回内容时再次乱码。
    说白了，就是先把“字能不能正常显示”这个基础问题兜住。
    """

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"stdio reconfigure failed stream={stream_name} error_code={type(exc).__name__} error={exc}",
                    file=sys.__stderr__,
                )


def make_agent(args) -> SimpleAgent:
    """根据配置创建一个可直接运行的智能体实例。"""

    config = load_config(args.config)
    return SimpleAgent(config, resolve_workspace_root(config, args.config))


def resolve_workspace_root(config, config_path: str | Path) -> Path:
    """解析本次运行实际使用的工作区根目录。

    默认仍然使用包目录 `agent_py_agent`，保持之前行为不变。配置里写了
    `workspace_root` 时，memory、gateway、subagent 账本和文件工具都会落在该目录下。
    场景测试会利用这个开关把真实 API 任务关进临时 fixture，避免碰当前开发仓库。
    """

    raw = str(getattr(config, "workspace_root", "") or "").strip()
    if not raw:
        return ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(config_path).expanduser().resolve().parent / candidate
    return candidate.resolve()


def make_capability_router(agent: SimpleAgent, capability_config, skill_dirs: list[str] | None):
    """创建 capability router，合并当前工具和可选 skill 目录。"""

    default_skill_dirs = [ROOT.parent / "skills", ROOT / "skills"]
    dirs = [Path(item).expanduser() for item in skill_dirs] if skill_dirs else default_skill_dirs
    skills = SkillRegistry(dirs)
    skills.scan()
    return CapabilityRouter(
        config=capability_config,
        skill_registry=skills,
        tool_specs=agent.tools.specs(),
    )


def format_local_time(timestamp: float) -> str:
    """把 Unix 时间戳格式化成人能扫一眼的本地时间。"""

    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _memory_record_count(agent: SimpleAgent) -> int:
    try:
        return len(agent.memory.all())
    except Exception as exc:
        print(
            f"memory count failed error_code={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
        return 0


def _add_doctor_check(
    checks: list[dict],
    *,
    name: str,
    ok: bool,
    severity: str,
    message: str,
    details: dict | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "ok": ok,
            "severity": "ok" if ok else severity,
            "message": message,
            "details": details or {},
        }
    )



def build_local_doctor_report(agent: SimpleAgent, *, limit: int = 20) -> dict:
    """生成本地事实源/gateway/subagent 的诊断报告。"""

    paths = gateway_paths(agent)
    checks: list[dict] = []
    suggestions: list[str] = []
    stats = agent.local_store.stats()
    source_counts = agent.local_store.source_counts()
    memory_count = _memory_record_count(agent)
    memory_indexed = source_counts.get("memory", 0)
    _add_doctor_check(
        checks,
        name="local_store_open",
        ok=True,
        severity="P0",
        message="LocalStore SQLite 可以打开。",
        details=stats,
    )
    for label, path in {
        "memory_path": agent.memory.path,
        "local_store_db": Path(stats["db_path"]),
        "local_store_files_dir": Path(stats["files_dir"]),
        "local_store_events_path": Path(stats["events_path"]),
        "gateway_workspace": paths.root,
        "subagent_workspace": agent.subagents.workspace,
    }.items():
        exists = path.exists()
        _add_doctor_check(
            checks,
            name=f"path_{label}",
            ok=exists or label in {"memory_path", "gateway_workspace", "subagent_workspace"},
            severity="P1",
            message=f"{label} {'存在' if exists else '尚未创建'}: {path}",
            details={"path": str(path), "exists": exists},
        )
    memory_ok = memory_count == memory_indexed
    _add_doctor_check(
        checks,
        name="memory_index",
        ok=memory_ok,
        severity="P1",
        message=f"JSONL 记忆 {memory_count} 条，LocalStore memory 索引 {memory_indexed} 条。",
        details={"memory_jsonl": memory_count, "memory_indexed": memory_indexed},
    )
    if not memory_ok:
        suggestions.append("运行 `my-agent local-rebuild --source memory` 补齐记忆索引。")

    missing_files = agent.local_store.missing_content_files(limit=limit)
    _add_doctor_check(
        checks,
        name="local_store_content_files",
        ok=not missing_files,
        severity="P1",
        message=f"LocalStore 正文文件缺失 {len(missing_files)} 条。",
        details={"missing": missing_files},
    )
    if missing_files:
        suggestions.append("运行 `my-agent local-rebuild --reset` 从原始文件事实源重建索引。")

    counts = gateway_request_counts(paths)
    stale_processing = gateway_stale_processing(paths, agent.config.gateway_processing_timeout_seconds)
    _add_doctor_check(
        checks,
        name="gateway_queue",
        ok=not stale_processing,
        severity="P1",
        message=f"gateway 队列 {json.dumps(counts, ensure_ascii=False, sort_keys=True)}；stale processing={len(stale_processing)}。",
        details={"counts": counts, "stale_processing": stale_processing},
    )
    if stale_processing:
        suggestions.append("运行 `my-agent gateway restart --force` 或 `my-agent local-doctor --repair` 处理卡住的 processing 请求。")

    invalid_work_orders = []
    for task in agent.subagents.list_runs():
        validation = agent.subagents.validate_work_order(task.id)
        if not validation.ok:
            invalid_work_orders.append(
                {"run_id": task.id, "missing": validation.missing, "warnings": validation.warnings}
            )
            if len(invalid_work_orders) >= limit:
                break
    _add_doctor_check(
        checks,
        name="subagent_work_orders",
        ok=not invalid_work_orders,
        severity="P1",
        message=f"subagent 工单缺失关键文件 {len(invalid_work_orders)} 条。",
        details={"invalid": invalid_work_orders},
    )
    if invalid_work_orders:
        suggestions.append("运行 `my-agent subagents-apply-actions --apply --action repair_work_order` 修复缺失工单文件。")

    if stats["record_count"] == 0 and (memory_count or agent.subagents.list_runs() or any(counts.values())):
        suggestions.append("LocalStore 为空但磁盘上已有事实源，建议运行 `my-agent local-rebuild`。")
    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "stats": stats,
        "source_counts": source_counts,
        "memory_count": memory_count,
        "gateway_counts": counts,
        "checks": checks,
        "suggestions": suggestions,
    }



def rebuild_subagent_index(agent: SimpleAgent) -> int:
    """从 subagent 工单目录重建主要 LocalStore 记录。"""

    count = 0
    for task in agent.subagents.list_runs():
        agent.subagents._index_task(task)
        count += 1
        task_dir = Path(task.task_dir)
        for rel_path, source_type in {
            "WORK_LOG.md": "subagent_work_log",
            "execution_context.json": "subagent_execution_context",
            "reports/runner_result.json": "subagent_runner_result",
            "reports/acceptance_review.json": "subagent_acceptance_review",
            "reports/patch_review.json": "subagent_patch_review",
        }.items():
            path = task_dir / rel_path
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            source_id = task.id if rel_path == "execution_context.json" else f"{task.id}:{rel_path}"
            agent.local_store.log_record(
                source_type=source_type,
                source_id=source_id,
                title=f"{source_type} {task.id}",
                content=content,
                metadata={"run_id": task.id, "path": str(path), "rebuilt": True},
                event_type=f"{source_type}_rebuilt",
            )
            count += 1
    return count


def rebuild_local_store(agent: SimpleAgent, *, sources: set[str], reset: bool = False) -> dict:
    """按来源重建 LocalStore。"""

    if reset:
        agent.local_store.reset()
    result = {
        "reset": reset,
        "sources": sorted(sources),
        "memory_indexed": 0,
        "gateway_indexed": 0,
        "subagent_indexed": 0,
        "fts_rebuilt": 0,
    }
    if "memory" in sources:
        result["memory_indexed"] = agent.memory.index_all()
    if "gateway" in sources:
        result["gateway_indexed"] = rebuild_gateway_index(agent)
    if "subagent" in sources:
        result["subagent_indexed"] = rebuild_subagent_index(agent)
    if "fts" in sources or reset:
        result["fts_rebuilt"] = agent.local_store.rebuild_fts()
    result["stats"] = agent.local_store.stats()
    result["source_counts"] = agent.local_store.source_counts()
    return result


def build_status_suggestions(agent: SimpleAgent, payload: dict) -> list[str]:
    """根据 status payload 给出下一步建议。"""

    suggestions: list[str] = []
    gateway = payload["gateway"]
    counts = gateway["request_counts"]
    local_store = payload["local_store"]
    hot_count = int(payload["subagents"]["hot_count"])
    if gateway["status"] == "stopped" and (counts.get("pending", 0) or counts.get("processing", 0)):
        suggestions.append("gateway 停止但队列里还有请求：运行 `my-agent gateway start`。")
    elif gateway["status"] == "stopped":
        suggestions.append("需要后台值班时运行 `my-agent gateway start`，或直接执行 `my-agent`。")
    if gateway["status"] == "stale":
        suggestions.append("gateway heartbeat 已过期：运行 `my-agent gateway restart --force`。")
    if counts.get("failed", 0):
        suggestions.append("gateway failed 队列非空：运行 `my-agent gateway logs` 和 `my-agent timeline --source-type gateway_request` 排查。")
    if local_store["record_count"] == 0 and (_memory_record_count(agent) or agent.subagents.list_runs() or any(counts.values())):
        suggestions.append("LocalStore 为空但已有文件事实源：运行 `my-agent local-rebuild`。")
    if hot_count:
        suggestions.append("存在红灯 subagent：运行 `my-agent subagents-due-check`，必要时再 `my-agent subagents-dispatch --apply`。")
    if not payload["timeline"] and local_store["record_count"]:
        suggestions.append("LocalStore 有记录但 timeline 为空：运行 `my-agent local-rebuild --reset` 从文件事实源重建事件索引。")
    return suggestions


def cmd_status(args) -> int:
    """显示 my-agent 当前全局状态。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    local_stats = agent.local_store.stats()
    board = agent.subagents.build_board(recent_limit=args.limit)
    timeline = agent.local_store.timeline(limit=args.limit)
    pid, alive = gateway_running(paths)
    gateway_state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    heartbeat_age = time.time() - heartbeat_at if heartbeat_at else 0
    gateway_status = "running" if alive else gateway_state.get("status", "stopped")
    if alive and heartbeat_at and heartbeat_age > agent.config.gateway_stale_seconds:
        gateway_status = "stale"

    payload = {
        "agent_name": agent.config.agent_name,
        "workspace_root": str(agent.root),
        "gateway": {
            "status": gateway_status,
            "pid": pid,
            "alive": alive,
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_at else 0,
            "request_counts": gateway_request_counts(paths),
            "workspace": str(paths.root),
        },
        "local_store": local_stats,
        "subagents": {
            "summary": board.summary,
            "hot_count": len(board.hot_list),
            "recent_count": len(board.recent),
            "hot": [item.__dict__ for item in board.hot_list[: args.limit]],
            "recent": [item.__dict__ for item in board.recent[: args.limit]],
        },
        "timeline": [item.__dict__ for item in timeline],
    }
    payload["suggested_actions"] = build_status_suggestions(agent, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT STATUS")
    print(f"agent={agent.config.agent_name}")
    print(f"workspace={agent.root}")
    print("")
    print("Gateway")
    print(f"- status={gateway_status} pid={pid if pid else '-'} alive={alive}")
    if heartbeat_at:
        print(f"- heartbeat_age_seconds={heartbeat_age:.1f}")
    print("- requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True))
    print(f"- workspace={paths.root}")
    print("")
    print("Local Store")
    print(f"- records={local_stats['record_count']} events={local_stats['event_count']} fts5={local_stats['fts5_enabled']}")
    print(f"- db={local_stats['db_path']}")
    print("")
    print("Subagents")
    print("- summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if board.hot_list:
        print(f"- hot={len(board.hot_list)}")
        for item in board.hot_list[: args.limit]:
            flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
            print(f"  - {item.id} {item.status}/{item.verification_status} flags={flags} :: {item.goal}")
    else:
        print("- hot=0")
    if args.recent:
        print("- recent:")
        for item in board.recent[: args.limit]:
            print(f"  - {item.id} {item.status}/{item.verification_status} :: {item.goal}")
    print("")
    print("Timeline")
    if not timeline:
        print("- 暂无事件")
    for item in timeline:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {item.title}")
    print("")
    print("Suggested Actions")
    if payload["suggested_actions"]:
        for item in payload["suggested_actions"]:
            print(f"- {item}")
    else:
        print("- 暂无，当前没有明显需要立刻处理的事项。")
    return 0


def cmd_timeline(args) -> int:
    """显示 LocalStore 最近事件。"""

    agent = make_agent(args)
    items = agent.local_store.timeline(
        limit=args.limit,
        source_type=args.source_type,
        event_type=args.event_type,
    )
    if args.json:
        print(json.dumps([item.__dict__ for item in items], ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT TIMELINE")
    if args.source_type:
        print(f"source_type={args.source_type}")
    if args.event_type:
        print(f"event_type={args.event_type}")
    if not items:
        print("暂无事件。")
        return 0
    for item in items:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        title = item.title or "-"
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {title}")
        if args.details:
            print("  payload=" + json.dumps(item.payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_run(args) -> int:
    """执行一次单轮请求。"""

    agent = make_agent(args)
    result = agent.run(
        args.prompt,
        inject=args.inject or [],
        prompt_files=args.prompt_file or [],
        save=args.save,
    )
    if args.show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    print(result.response)
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}]"
    )
    return 0


def cmd_remember(args) -> int:
    """手动写一条记忆。"""

    agent = make_agent(args)
    rec = agent.remember(args.content, kind=args.kind)
    print(json.dumps(rec.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_list(args) -> int:
    """列出最近几条记忆。"""

    agent = make_agent(args)
    records = agent.memory.all()[-args.limit :]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_memory_search(args) -> int:
    """按智能体自己的检索规则搜索记忆。"""

    agent = make_agent(args)
    for rec in agent.recall(args.query, args.limit):
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_local_store_status(args) -> int:
    """显示本地事实源状态。"""

    agent = make_agent(args)
    print(json.dumps(agent.local_store.stats(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_local_search(args) -> int:
    """搜索本地事实源。"""

    agent = make_agent(args)
    hits = agent.local_store.search(
        args.query,
        limit=args.limit,
        source_type=args.source_type,
        visibility=args.visibility,
    )
    for hit in hits:
        payload = hit.__dict__.copy()
        if args.preview_chars >= 0:
            payload["content"] = payload["content"][: args.preview_chars]
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_local_index_memory(args) -> int:
    """把现有 JSONL 记忆补建到本地事实源索引。"""

    agent = make_agent(args)
    count = agent.memory.index_all()
    print(
        json.dumps(
            {
                "indexed": count,
                "stats": agent.local_store.stats(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_local_doctor(args) -> int:
    """诊断 LocalStore、gateway 队列和 subagent 文件事实源的一致性。"""

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
    """从文件事实源重建 LocalStore 索引。"""

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


def cmd_spawn(args) -> int:
    """生成子任务记录。"""

    agent = make_agent(args)
    tasks = agent.spawn_subagents(args.goal, args.count)
    for task in tasks:
        print(json.dumps(task.__dict__, ensure_ascii=False))
    return 0


def cmd_subagents(args) -> int:
    """显示子代理红绿灯看板。"""

    agent = make_agent(args)
    board = agent.subagents.write_board(recent_limit=args.limit)
    items = filter_board_items(
        board.items if args.all else board.hot_list or board.recent,
        status=args.status or "",
        owner=args.owner or "",
        root_id=args.root_id or "",
    )
    print("SUBAGENT BOARD")
    print(f"total={board.summary.get('total', 0)} hot={len(board.hot_list)}")
    print("summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if not items:
        print("没有匹配的子代理记录。")
        return 0
    for item in items[: args.limit]:
        flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"channel={item.channel_status} depth={item.depth} "
            f"owner={item.owner or 'none'} final={item.final_owner or 'none'} "
            f"evidence={item.evidence_count} requests={item.open_request_count} "
            f"gaps={item.open_gap_count} flags={flags} :: {item.goal}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_board.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_BOARD.md'}")
    return 0


def cmd_subagents_due_check(args) -> int:
    """巡检 subagent 状态，输出父代理需要处理的问题。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_due_check(capability_config)
    issues = report.issues if args.all else report.issues[: args.limit]
    print("SUBAGENT DUE CHECK")
    print(f"total_issues={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not issues:
        print("暂时没有需要父代理介入的问题。")
    for issue in issues:
        flags = ",".join(issue.risk_flags) if issue.risk_flags else "ok"
        print(
            f"- [{issue.severity}] {issue.run_id} kind={issue.kind} "
            f"status={issue.status} action={issue.suggested_action} "
            f"owner={issue.owner or 'none'} final={issue.final_owner or 'none'} "
            f"flags={flags} :: {issue.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_due_check.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DUE_CHECK.md'}")
    return 0


def cmd_subagents_probe(args) -> int:
    """检查 subagent 通道健康状态。"""

    agent = make_agent(args)
    run_ids = args.run_id or None
    report = agent.subagents.write_channel_probe_report(run_ids, limit=args.limit)
    print("SUBAGENT CHANNEL PROBE")
    print(f"total={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.results:
        print("暂时没有可检查的子代理记录。")
    for result in report.results:
        failed = [check for check in result.checks if not check.ok]
        print(
            f"- {result.run_id} channel={result.channel_status} "
            f"failed_checks={len(failed)} :: {result.goal}"
        )
        for check in failed[:3]:
            print(f"  [{check.severity}] {check.name}: {check.summary} {check.error}".rstrip())
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_channel_probe.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_CHANNEL_PROBE.md'}")
    return 0


def cmd_subagents_plan_actions(args) -> int:
    """根据 due-check 生成 dry-run 动作计划。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_action_plan(capability_config)
    actions = report.actions if args.all else report.actions[: args.limit]
    print("SUBAGENT ACTION PLAN")
    print(f"total_actions={report.summary.get('total', 0)} mode=dry-run")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not actions:
        print("暂时没有建议动作。")
    for action in actions:
        kinds = ",".join(action.source_issue_kinds)
        print(
            f"- [{action.severity}] {action.run_id} action={action.action} "
            f"priority={action.priority} sources={kinds} :: {action.reason}"
        )
        for command in action.suggested_commands[:3]:
            print(f"  $ {command}")
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_action_plan.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACTION_PLAN.md'}")
    return 0


def cmd_subagents_apply_actions(args) -> int:
    """执行或 dry-run 执行 action plan。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_action_apply_report(
        capability_config,
        apply=args.apply,
        action_filter=args.action or "",
        run_id=args.run_id or "",
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT ACTION APPLY")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有匹配的动作。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} action={record.action} "
            f"applied={record.applied} {record.before_status}->{record.after_status} :: "
            f"{record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_action_apply_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACTION_APPLY.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_action_apply_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACTION_APPLY_LOG.md'}")
    return 0


def cmd_subagents_route_capabilities(args) -> int:
    """路由 OPEN capability request，默认 dry-run。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    report = agent.subagents.write_capability_route_report(
        router,
        capability_config,
        apply=args.apply,
        run_ids=args.run_id or None,
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT CAPABILITY ROUTE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 OPEN capability request。")
    for record in report.records:
        cards = ", ".join(f"{item['kind']}:{item['name']}" for item in record.selected_cards) or "none"
        print(
            f"- [{record.status}] {record.run_id} request={record.request_id} "
            f"cards={cards} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_capability_route_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_CAPABILITY_ROUTE.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_capability_route_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'CAPABILITY_ROUTE_LOG.md'}")
    return 0


def cmd_subagents_acceptance(args) -> int:
    """验收等待验收的 subagent，默认 dry-run。"""

    agent = make_agent(args)
    report = agent.subagents.write_acceptance_review_report(
        run_ids=args.run_id or None,
        apply=args.apply,
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT ACCEPTANCE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有等待验收的 subagent。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"applied={record.applied} {record.before_status}/{record.before_verification_status}"
            f"->{record.after_status}/{record.after_verification_status} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_acceptance_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACCEPTANCE.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_acceptance_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACCEPTANCE_REVIEW_LOG.md'}")
    return 0


def cmd_subagents_patches(args) -> int:
    """审核 runner 输出里的 patch 记录，默认 dry-run。"""

    agent = make_agent(args)
    report = agent.subagents.write_patch_review_report(
        run_ids=args.run_id or None,
        apply=args.apply,
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT PATCH REVIEW")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 patch 需要审核。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} "
            f"blocked={record.blocked_count} applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_patch_review_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_PATCH_REVIEW.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_patch_review_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'PATCH_REVIEW_LOG.md'}")
    return 0


def cmd_subagents_dispatch(args) -> int:
    """执行一轮父代理调度，默认 dry-run。"""

    if args.execute_runners and not args.apply:
        print("--execute-runners 必须和 --apply 一起使用。", file=sys.stderr)
        return 2

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    if args.watch:
        try:
            report = agent.watch_subagents(
                router,
                capability_config,
                apply=args.apply,
                execute_runners=args.execute_runners,
                planner=args.planner,
                max_runners=args.max_runners,
                limit=args.limit,
                reviewer=args.reviewer,
                note=args.note or "",
                runner_instruction=args.instruction or "",
                max_cards=args.max_cards,
                probe=not args.no_probe,
                take_over_by=args.take_over_by or "",
                locked_files=args.locked_file or [],
                interval=args.interval,
                max_cycles=args.max_cycles,
                force_lock=args.force_lock,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        mode = "apply" if args.apply else "dry-run"
        print("SUBAGENT DISPATCH WATCH")
        print(
            f"mode={mode} planner={args.planner} execute_runners={args.execute_runners} "
            f"cycles={report.summary.get('total', 0)}"
        )
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            print(
                f"- [{status}] cycle={record.cycle} records={record.dispatch_record_count} "
                f":: {record.message}"
            )
        print(f"\n已写入: {agent.subagents.workspace / 'subagent_dispatch_watch_report.json'}")
        print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DISPATCH_WATCH.md'}")
        print(f"heartbeat: {agent.subagents.workspace / 'subagent_dispatch_watch_heartbeat.json'}")
        print(f"watch log: {agent.subagents.workspace / 'subagent_dispatch_watch_log.jsonl'}")
        print(f"watch log: {agent.subagents.workspace / 'DISPATCH_WATCH_LOG.md'}")
        if args.planner:
            print(f"planner: {agent.subagents.workspace / 'parent_planner_report.json'}")
            print(f"planner: {agent.subagents.workspace / 'PARENT_PLANNER.md'}")
        return 0

    report = agent.dispatch_subagents(
        router,
        capability_config,
        apply=args.apply,
        execute_runners=args.execute_runners,
        planner=args.planner,
        max_runners=args.max_runners,
        limit=args.limit,
        reviewer=args.reviewer,
        note=args.note or "",
        runner_instruction=args.instruction or "",
        max_cards=args.max_cards,
        probe=not args.no_probe,
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT DISPATCH")
    print(
        f"mode={mode} planner={args.planner} execute_runners={args.execute_runners} "
        f"total_records={report.summary.get('total', 0)}"
    )
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有调度动作。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_dispatch_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DISPATCH.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_dispatch_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'DISPATCH_LOG.md'}")
    if args.planner:
        print(f"planner: {agent.subagents.workspace / 'parent_planner_report.json'}")
        print(f"planner: {agent.subagents.workspace / 'PARENT_PLANNER.md'}")
    return 0


def cmd_daemon(args) -> int:
    """按配置启动前台常驻调度。"""

    agent = make_agent(args)
    try:
        options = _resolve_daemon_options(agent, args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    capability_config = load_capability_config(args.capability_config)
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
            apply=options.apply,
            execute_runners=options.execute_runners,
            planner=options.planner,
            max_runners=options.max_runners,
            limit=options.limit,
            reviewer=options.reviewer,
            note=args.note or "",
            runner_instruction=options.instruction or "",
            max_cards=options.max_cards,
            probe=options.probe,
            take_over_by=args.take_over_by or "",
            locked_files=args.locked_file or [],
            interval=options.interval,
            max_cycles=options.max_cycles,
            force_lock=args.force_lock,
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


def _validate_daemon_numbers(
    *,
    interval: float,
    max_runners: int,
    limit: int,
    max_cycles: int,
    max_cards: int,
) -> str:
    if interval < 0:
        return "daemon_interval / --interval 不能小于 0；0 表示每轮之间不等待，通常只用于测试。"
    if max_runners < 0:
        return "daemon_max_runners / --max-runners 不能小于 0；0 表示本轮不执行 runner。"
    if limit < 0:
        return "daemon_limit / --limit 不能小于 0；0 表示不限制记录条数。"
    if max_cycles < 0:
        return "daemon_max_cycles / --max-cycles 不能小于 0；0 表示持续运行。"
    if max_cards < 0:
        return "daemon_max_cards / --max-cards 不能小于 0；0 表示不限制。"
    return ""


def _resolve_daemon_max_runners(value: object) -> int:
    """把 daemon_max_runners 的 auto / 数字配置转成当前前台调度器可执行的整数。"""

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


def _resolve_daemon_options(agent: SimpleAgent, args) -> DaemonOptions:
    """合并配置和 CLI override，得到 daemon/gateway 运行参数。"""

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
    reviewer = getattr(args, "reviewer", None) or cfg.daemon_reviewer
    instruction = getattr(args, "instruction", None)
    instruction = cfg.daemon_runner_instruction if instruction is None else instruction
    probe = False if getattr(args, "no_probe", False) else cfg.daemon_probe

    invalid_number = _validate_daemon_numbers(
        interval=interval,
        max_runners=max_runners,
        limit=limit,
        max_cycles=max_cycles,
        max_cards=max_cards,
    )
    if invalid_number:
        raise ValueError(invalid_number)

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
    )


def cmd_gateway(args) -> int:
    """gateway 命令族入口。"""

    print("请指定 gateway 子命令：start / status / stop / restart / logs / ask / result。", file=sys.stderr)
    return 2


def ensure_gateway_started(args) -> int:
    """确保 gateway 后台进程正在运行；未运行时自动启动。

    这是 `my-agent` 无参数默认入口的核心：用户只敲命令名时，不应该先学习
    `gateway start`，程序会自己把后台值班进程拉起来。
    """

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = gateway_running(paths)
    if alive:
        return 0
    start_args = argparse.Namespace(
        config=args.config,
        force=False,
        force_lock=False,
    )
    code = cmd_gateway_start(start_args)
    if code:
        return code
    deadline = time.time() + 10
    while time.time() < deadline:
        pid, alive = gateway_running(paths)
        if alive:
            return 0
        time.sleep(0.2)
    print("gateway 已尝试启动，但未能确认存活。请运行 my-agent gateway status 查看。", file=sys.stderr)
    return 2


def cmd_default(args) -> int:
    """无子命令默认入口：自动启动 gateway，然后进入 gateway chat。"""

    code = ensure_gateway_started(args)
    if code:
        return code
    args.gateway = True
    args.gateway_timeout = None
    args.inject = None
    args.prompt_file = None
    args.memory_limit = 5
    args.no_save = False
    return cmd_chat(args)


def cmd_gateway_start(args) -> int:
    """启动第一版后台 gateway。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    pid = read_pid(paths.pid)
    if pid and is_pid_alive(pid) and not args.force:
        print(f"gateway 已在运行 pid={pid}")
        print(f"status: {paths.state}")
        return 0
    if pid and is_pid_alive(pid) and args.force:
        paths.stop_request.write_text(
            json.dumps({"requested_at": time.time(), "reason": "force restart before start"}, ensure_ascii=False),
            encoding="utf-8",
        )
        if not wait_for_pid_exit(pid, agent.config.gateway_stop_timeout):
            terminate_pid(pid)
            wait_for_pid_exit(pid, 5)

    try:
        paths.stop_request.unlink()
    except OSError:
        pass

    config_path = Path(args.config).resolve()
    command = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(config_path),
        "gateway",
        "run",
    ]
    if args.force_lock:
        command.append("--force-lock")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True

    with paths.log.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=ROOT.parent,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

    paths.pid.write_text(str(process.pid), encoding="utf-8")
    write_json_file(
        paths.state,
        {
            "status": "starting",
            "pid": process.pid,
            "started_at": time.time(),
            "command": command,
            "log": str(paths.log),
        },
    )
    wait_for_gateway_running(paths, timeout=10.0)
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_run(args) -> int:
    """内部命令：前台运行 gateway 后台循环。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    recovery = recover_gateway_processing_requests(
        paths,
        startup=True,
        max_attempts=agent.config.gateway_request_max_attempts,
        timeout_seconds=agent.config.gateway_processing_timeout_seconds,
        agent=agent,
    )
    requeued = recovery["requeued"]
    pid = os.getpid()
    paths.pid.write_text(str(pid), encoding="utf-8")
    log_gateway_event(
        agent,
        "gateway_run_started",
        {
            "status": "starting",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
        },
    )
    try:
        options = _resolve_daemon_options(agent, args)
    except ValueError as exc:
        write_json_file(paths.state, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_failed",
            {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()},
        )
        print(str(exc), file=sys.stderr)
        return 2

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_gateway_heartbeat_loop,
        args=(paths, agent, options, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()
    request_thread = threading.Thread(
        target=_gateway_request_loop,
        args=(args, paths, stop_event),
        daemon=True,
    )
    request_thread.start()
    write_json_file(
        paths.state,
        {
            "status": "running",
            "pid": pid,
            "started_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "apply": options.apply,
            "execute_runners": options.execute_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
            "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        },
    )
    log_gateway_event(
        agent,
        "gateway_run_running",
        {
            "status": "running",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "apply": options.apply,
            "execute_runners": options.execute_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
            "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        },
    )

    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    exit_code = 0
    try:
        report = agent.watch_subagents(
            router,
            capability_config,
            apply=options.apply,
            execute_runners=options.execute_runners,
            planner=options.planner,
            max_runners=options.max_runners,
            limit=options.limit,
            reviewer=options.reviewer,
            note=args.note or "",
            runner_instruction=options.instruction or "",
            max_cards=options.max_cards,
            probe=options.probe,
            take_over_by=args.take_over_by or "",
            locked_files=args.locked_file or [],
            interval=options.interval,
            max_cycles=options.max_cycles,
            force_lock=args.force_lock,
            stop_file=paths.stop_request,
        )
        final_status = "stopped" if paths.stop_request.exists() else "exited"
        write_json_file(
            paths.state,
            {
                "status": final_status,
                "pid": pid,
                "stopped_at": time.time(),
                "summary": report.summary,
            },
        )
        log_gateway_event(
            agent,
            "gateway_run_stopped",
            {"status": final_status, "pid": pid, "stopped_at": time.time(), "summary": report.summary},
        )
    except KeyboardInterrupt:
        write_json_file(paths.state, {"status": "interrupted", "pid": pid, "stopped_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_interrupted",
            {"status": "interrupted", "pid": pid, "stopped_at": time.time()},
        )
        exit_code = 130
    except Exception as exc:
        write_json_file(paths.state, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_failed",
            {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()},
        )
        print(str(exc), file=sys.stderr)
        exit_code = 2
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=2)
        request_thread.join(timeout=2)
        try:
            paths.pid.unlink()
        except OSError:
            pass
        try:
            paths.stop_request.unlink()
        except OSError:
            pass
        _write_gateway_heartbeat(paths, agent, options, status="stopped", pid=pid)
        log_gateway_event(
            agent,
            "gateway_run_cleanup",
            {"status": "cleanup", "pid": pid, "updated_at": time.time()},
        )
    return exit_code


def cmd_gateway_status(args) -> int:
    """显示 gateway 状态。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    print("MY-AGENT GATEWAY")
    print(f"status={status} pid={pid if pid else '-'} alive={alive}")
    if heartbeat_at:
        print(f"heartbeat_age_seconds={age:.1f}")
    if state:
        print("state=" + json.dumps(state, ensure_ascii=False, sort_keys=True))
    counts = gateway_request_counts(paths)
    print(
        "requests="
        + json.dumps(counts, ensure_ascii=False, sort_keys=True)
    )
    stale_processing = gateway_stale_processing(paths, agent.config.gateway_processing_timeout_seconds)
    if stale_processing:
        print("stale_processing=" + json.dumps(stale_processing, ensure_ascii=False, sort_keys=True))
    print(f"workspace: {paths.root}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_stop(args) -> int:
    """请求 gateway 正常停止。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid = read_pid(paths.pid)
    if not pid or not is_pid_alive(pid):
        try:
            paths.pid.unlink()
        except OSError:
            pass
        print("gateway 未在运行")
        return 0

    paths.root.mkdir(parents=True, exist_ok=True)
    paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": args.reason or "user stop"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_gateway_event(
        agent,
        "gateway_stop_requested",
        {"status": "stop_requested", "pid": pid, "reason": args.reason or "user stop", "updated_at": time.time()},
    )
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_stop_timeout
    if wait_for_pid_exit(pid, timeout):
        log_gateway_event(
            agent,
            "gateway_stopped",
            {"status": "stopped", "pid": pid, "updated_at": time.time()},
        )
        print(f"gateway stopped pid={pid}")
        return 0
    if args.kill:
        terminate_pid(pid)
        if wait_for_pid_exit(pid, 5):
            write_json_file(paths.state, {"status": "killed", "pid": pid, "stopped_at": time.time()})
            log_gateway_event(
                agent,
                "gateway_killed",
                {"status": "killed", "pid": pid, "updated_at": time.time()},
            )
            try:
                paths.pid.unlink()
            except OSError:
                pass
            print(f"gateway killed pid={pid}")
            return 0
    print(f"gateway stop requested but still running pid={pid}", file=sys.stderr)
    return 2


def cmd_gateway_restart(args) -> int:
    """重启 gateway。"""

    stop_args = argparse.Namespace(
        config=args.config,
        timeout=args.timeout,
        kill=args.force,
        reason="gateway restart",
    )
    stop_code = cmd_gateway_stop(stop_args)
    if stop_code not in {0}:
        return stop_code
    start_args = argparse.Namespace(
        config=args.config,
        force=True,
        force_lock=args.force_lock,
    )
    return cmd_gateway_start(start_args)


def cmd_gateway_logs(args) -> int:
    """输出 gateway 日志尾部。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    lines = tail_lines(paths.log, args.lines)
    if not lines:
        print(f"暂无 gateway 日志: {paths.log}")
        return 0
    for line in lines:
        print(line)
    return 0


def cmd_gateway_ask(args) -> int:
    """向正在运行的 gateway 投递一条聊天请求。

    这是未来聊天工具/TUI 的最小原型：
    CLI 只是客户端，把用户消息写进 pending；真正调用模型的是后台 gateway 进程。
    """

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = wait_for_gateway_running(paths, timeout=10.0)
    if not alive:
        print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
        return 2

    request_id, request_path, response_path = submit_gateway_ask(
        paths,
        prompt=args.prompt,
        inject=args.inject or [],
        prompt_files=args.prompt_file or [],
        save=not args.no_save,
        include_prompt=bool(args.show_prompt),
        agent=agent,
    )
    if args.no_wait:
        # 异步模式：只告诉用户“请求已放进队列”，不在当前终端等模型结果。
        print(f"queued request_id={request_id}")
        print(f"request: {request_path}")
        print(f"response: {response_path}")
        return 0

    # 同步模式：命令行阻塞等待 response 文件出现。聊天工具以后也可以用同样逻辑，
    # 或者只监听 responses 目录/数据库事件后主动推送消息给用户。
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    response = wait_for_gateway_response(paths, request_id, timeout)
    if not response:
        log_gateway_payload(
            agent,
            {
                "id": request_id,
                "kind": "ask",
                "status": "timeout",
                "ok": False,
                "error": f"timeout after {timeout}s",
                "created_at": 0,
                "ended_at": time.time(),
            },
            event_type="gateway_request_timeout",
            request_path=request_path,
            response_path=response_path,
        )
        print(f"gateway 请求等待超时: request_id={request_id} timeout={timeout}s", file=sys.stderr)
        print(f"response: {response_path}")
        return 2
    return print_gateway_response(response, json_mode=args.json, show_prompt=args.show_prompt)


def cmd_gateway_result(args) -> int:
    """读取某个 gateway 请求的结果。

    主要服务于 `gateway ask --no-wait`。普通用户以后在聊天工具里不需要手动查，
    聊天适配器会拿这个 response 再发回对应会话。
    """

    agent = make_agent(args)
    paths = gateway_paths(agent)
    payload = read_json_file(gateway_response_path(paths, args.request_id))
    if not payload:
        print(f"未找到 gateway 响应: {args.request_id}", file=sys.stderr)
        print(f"response: {gateway_response_path(paths, args.request_id)}")
        return 2
    return print_gateway_response(payload, json_mode=args.json, show_prompt=args.show_prompt)


def cmd_adapter(args) -> int:
    """adapter 命令族入口。"""

    print("请指定 adapter 子命令：file。", file=sys.stderr)
    return 2



def cmd_adapter_file(args) -> int:
    """文件协议 adapter：inbox JSON -> gateway -> outbox JSON。"""

    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    apaths = adapter_paths(agent)
    if args.root:
        root = Path(args.root).expanduser()
        apaths = AdapterPaths(
            root=root,
            inbox=root / "inbox",
            processing=root / "processing",
            done=root / "done",
            failed=root / "failed",
            outbox=root / "outbox",
        )
    if args.inbox:
        apaths.inbox = Path(args.inbox).expanduser()
    if args.outbox:
        apaths.outbox = Path(args.outbox).expanduser()

    if not args.no_start_gateway:
        code = ensure_gateway_started(args)
        if code:
            return code
    else:
        _, alive = gateway_running(gpaths)
        if not alive:
            print("gateway 未在运行，且指定了 --no-start-gateway。", file=sys.stderr)
            return 2

    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    total = 0
    while True:
        processed = process_file_adapter_once(
            agent,
            gateway_paths_obj=gpaths,
            adapter_paths_obj=apaths,
            timeout=timeout,
            limit=args.limit,
        )
        total += processed
        if args.once or not args.watch:
            break
        time.sleep(max(0.2, args.poll_interval))
    print(
        json.dumps(
            {
                "processed": total,
                "adapter_root": str(apaths.root),
                "inbox": str(apaths.inbox),
                "outbox": str(apaths.outbox),
                "gateway_workspace": str(gpaths.root),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


@dataclass
class ScenarioPaths:
    """一次隔离场景测试使用的目录集合。"""

    run_root: Path
    fixture_root: Path
    config: Path
    summary_json: Path
    summary_md: Path


def cmd_scenario_test(args) -> int:
    """跑一轮可观察、隔离的真实任务全流程。"""

    if args.case == "all":
        return run_scenario_suite(args)
    if args.case == "verification":
        return run_scenario_verification_case(args)
    if args.case == "gateway-restart":
        return run_scenario_gateway_restart_case(args)
    if args.case == "structured-repair":
        return run_scenario_structured_repair_case(args)
    if args.case == "runner-retry":
        return run_scenario_runner_retry_case(args)

    if args.count <= 0:
        print("--count 必须大于 0。", file=sys.stderr)
        return 2
    if args.max_runners <= 0 and not args.dry_run:
        print("--max-runners 必须大于 0；如果只想预览，请加 --dry-run。", file=sys.stderr)
        return 2
    if args.max_cycles <= 0:
        print("--max-cycles 必须大于 0。", file=sys.stderr)
        return 2

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")
    print("")

    prompt = build_scenario_prompt(args.count)
    created_via = "direct"
    gateway_payload: dict[str, object] = {}
    if args.direct:
        print_scenario_step(1, "主代理聊天派工（direct agent.run）")
        agent = load_scenario_agent(paths.config)
        result = agent.run(prompt, save=False)
        print(result.response)
        print(f"[backend={result.backend}; tool_rounds={result.tool_rounds}]")
    else:
        print_scenario_step(1, "主代理聊天派工（gateway ask）")
        created_via = "gateway"
        gateway_payload = run_scenario_gateway_ask(paths, prompt, timeout=args.timeout)
        if not gateway_payload.get("ok"):
            write_scenario_summary(paths, ok=False, reason="gateway ask failed", extra={"gateway": gateway_payload})
            return 2

    agent = load_scenario_agent(paths.config)
    tasks = agent.subagents.list_runs()
    print_scenario_step(2, "检查派工结果")
    print_scenario_board(agent, limit=args.count + 5)
    if len(tasks) < args.count:
        reason = f"期望至少创建 {args.count} 个子代理，实际只有 {len(tasks)} 个。"
        print(f"SCENARIO_FAIL: {reason}", file=sys.stderr)
        write_scenario_summary(paths, ok=False, reason=reason, extra={"created_via": created_via})
        return 2

    print_scenario_step(3, "父代理调度 runner 和验收")
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    dispatch_summaries: list[dict[str, object]] = []
    final_ok = False
    for cycle in range(1, args.max_cycles + 1):
        print(f"\n--- dispatch cycle {cycle}/{args.max_cycles} ---")
        report = agent.dispatch_subagents(
            router,
            capability_config,
            apply=True,
            execute_runners=not args.dry_run,
            planner=args.planner,
            max_runners=args.max_runners,
            limit=0,
            reviewer="scenario-test",
            note="isolated full-flow scenario test",
            runner_instruction=build_scenario_runner_instruction(),
            max_cards=0,
            probe=True,
        )
        dispatch_summaries.append(
            {
                "cycle": cycle,
                "summary": report.summary,
                "record_count": len(report.records),
                "ok": all(item.ok for item in report.records),
            }
        )
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            run = record.run_id or "global"
            print(
                f"- [{status}] {record.step}/{record.action} run={run} "
                f"applied={record.applied} :: {record.message}"
            )
        print_scenario_board(agent, limit=args.count + 5)
        final_ok = scenario_tasks_verified(agent, args.count)
        if final_ok:
            break

    print_scenario_step(4, "核对隔离文件和最终报告")
    report_files = sorted((paths.fixture_root / "scenario_outputs").glob("*.md"))
    if args.dry_run:
        files_ok = True
        print("dry_run=true，跳过 runner 写文件检查。")
    else:
        files_ok = len(report_files) >= args.count
        print(f"scenario_output_files={len(report_files)}")
        for item in report_files:
            print(f"- {item}")
    final_ok = final_ok and files_ok
    reason = "scenario passed" if final_ok else "scenario did not reach verified state"
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason=reason,
        extra={
            "created_via": created_via,
            "gateway": gateway_payload,
            "dispatch": dispatch_summaries,
            "report_files": [str(item) for item in report_files],
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_suite(args) -> int:
    """连续运行一组隔离场景。"""

    cases = ["verification", "gateway-restart", "structured-repair", "runner-retry", "happy"]
    results: list[dict[str, object]] = []
    for case in cases:
        print(f"\n######## SCENARIO CASE: {case} ########")
        case_args = argparse.Namespace(**vars(args))
        case_args.case = case
        code = cmd_scenario_test(case_args)
        results.append({"case": case, "ok": code == 0, "exit_code": code})
        if code != 0:
            print("SCENARIO_SUITE_FAIL")
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return code
    print("SCENARIO_SUITE_PASS")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def run_scenario_verification_case(args) -> int:
    """验证父代理不会接受伪造 artifact / 自称完成。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=verification")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    print_scenario_step(1, "构造伪造完成的子代理记录")
    task = agent.subagents.create_run(
        goal="极端场景：runner 声称写了 artifact，但文件实际不存在",
        thought="验证父代理验收不能只相信模型自称。",
        plan=["伪造 output.json", "触发验收", "确认验收拒绝"],
        allowed_tools=["read_file", "write_file"],
        acceptance_checks=["必须有 read_file 证据", "必须有 write_file 证据", "artifact 文件必须真实存在"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.used_tools = ["read_file", "write_file"]
    task.evidence.append(
        VerificationEvidence(
            kind="file_read",
            summary="伪造证据：声称 read_file 成功",
            path="README.md",
            ok=True,
            created_at=time.time(),
        )
    )
    task.evidence.append(
        VerificationEvidence(
            kind="file_write",
            summary="伪造证据：声称 write_file 写入 scenario_outputs/forged.md",
            path="scenario_outputs/forged.md",
            ok=True,
            created_at=time.time(),
        )
    )
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "artifacts": [
                    {
                        "path": "scenario_outputs/forged.md",
                        "kind": "report",
                        "summary": "这个文件被故意留空不存在，用来测试验收防作弊。",
                    }
                ],
                "tests": [{"name": "fake-test", "command": "echo ok", "ok": True}],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print_scenario_step(2, "执行父代理验收")
    report = agent.subagents.write_acceptance_review_report(
        run_ids=[task.id],
        apply=True,
        reviewer="scenario-verification",
        note="forged artifact must be rejected",
    )
    loaded = agent.subagents.load(task.id)
    for record in report.records:
        print(
            f"- decision={record.decision} ok={record.ok} applied={record.applied} "
            f"{record.before_status}/{record.before_verification_status}->"
            f"{record.after_status}/{record.after_verification_status}"
        )
        for finding in record.findings:
            if not finding.ok:
                print(f"  [finding:{finding.severity}] {finding.name}: {finding.message}")

    final_ok = (
        report.records
        and report.records[0].decision == "REJECT"
        and not report.records[0].ok
        and loaded.status == "BLOCKED"
        and loaded.verification_status == "FAILED"
        and any(
            item.name == "artifact_paths_exist" and not item.ok
            for item in report.records[0].findings
        )
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="verification guard passed" if final_ok else "verification guard failed",
        extra={
            "case": "verification",
            "run_id": task.id,
            "acceptance_report": str(agent.subagents.workspace / "subagent_acceptance_report.json"),
            "acceptance_md": str(agent.subagents.workspace / "SUBAGENT_ACCEPTANCE.md"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_restart_case(args) -> int:
    """验证 gateway 启动时会恢复遗留 processing 请求。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-restart")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    processing_path = gpaths.processing / f"{request_id}.json"
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": "这个请求模拟 gateway 崩溃时卡在 processing。",
        "inject": [],
        "prompt_files": [],
        "save": False,
        "include_prompt": False,
        "created_at": time.time(),
        "client_pid": os.getpid(),
    }
    write_json_file(processing_path, payload)

    print_scenario_step(1, "模拟旧 gateway 崩溃遗留 processing 请求")
    print(f"processing_before={processing_path.exists()} path={processing_path}")
    requeued = requeue_gateway_processing_requests(gpaths)
    pending_path = gpaths.inbox / processing_path.name
    print_scenario_step(2, "执行 gateway 启动恢复步骤")
    print(f"requeued={requeued}")
    print(f"processing_after={processing_path.exists()}")
    print(f"pending_after={pending_path.exists()} path={pending_path}")

    final_ok = requeued == 1 and not processing_path.exists() and pending_path.exists()
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway restart requeue passed" if final_ok else "gateway restart requeue failed",
        extra={
            "case": "gateway-restart",
            "request_id": request_id,
            "pending_path": str(pending_path),
            "processing_path": str(processing_path),
            "requeued": requeued,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def print_dispatch_report(report) -> None:
    """打印场景测试里的 dispatch 摘要。"""

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


class ScenarioStructuredRepairBackend:
    """场景测试用后端：第一次输出坏结果块，修复回合补齐 JSON。"""

    name = "scenario_structured_repair_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "我已经完成任务，但这次故意输出一个损坏的结构化结果块。\n"
                    "[SUBAGENT_RESULT]\n"
                    "{\n"
                    '  "status": "AWAITING_ACCEPTANCE",\n'
                    '  "summary": "这个 JSON 少了结尾，用来模拟模型输出损坏",\n'
                    '  "evidence": [\n'
                    '    {"kind": "note", "summary": "原始回复声称已有证据", "ok": true}\n'
                ),
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "结构化输出损坏后已通过修复回合补齐。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "修复回合生成了可解析证据", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "structured repair", "command": "", "ok": true, "summary": "坏 JSON 已修复"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["结构化输出损坏时先做格式修复，不新增事实"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_structured_repair_case(args) -> int:
    """验证 runner 坏结构化输出会进入修复回合并通过验收。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=structured-repair")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioStructuredRepairBackend()
    agent.backend = backend
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会输出坏 JSON 的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 输出损坏的 SUBAGENT_RESULT，父代理应触发修复回合",
        thought="验证结构化输出坏掉时不会直接把任务丢成无法验收。",
        plan=["输出损坏结果块", "修复结构化结果", "父代理验收"],
        acceptance_checks=["必须触发 structured repair", "修复后必须有证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "执行 dispatch：runner 输出坏 JSON 后修复并验收")
    report = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-structured-repair",
        note="structured output damage should be repaired",
    )
    print_dispatch_report(report)
    loaded = agent.subagents.load(task.id)
    runner = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))
    output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"backend_calls={backend.calls} repair_attempted={runner.get('structured_repair_attempted')} "
        f"repair_ok={runner.get('structured_repair_ok')}"
    )

    final_ok = (
        backend.calls == 2
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and runner.get("structured_output_found") is True
        and runner.get("structured_output_ok") is True
        and runner.get("structured_repair_attempted") is True
        and runner.get("structured_repair_ok") is True
        and output.get("structured_output", {}).get("repair_attempted") is True
        and any(item.step == "acceptance" and item.ok for item in report.records)
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="structured repair passed" if final_ok else "structured repair failed",
        extra={
            "case": "structured-repair",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "final_status": loaded.status,
            "structured_repair_attempted": runner.get("structured_repair_attempted"),
            "structured_repair_ok": runner.get("structured_repair_ok"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


class ScenarioRetryBackend:
    """场景测试用后端：第一次失败，第二次给出可验收结果。"""

    name = "scenario_retry_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "runner 在第二次尝试中完成，已生成可验收证据。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "第二次 runner 尝试成功", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "runner retry", "command": "", "ok": true, "summary": "第二次尝试通过"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["临时 runner 错误可以由父代理有限重试恢复"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_runner_retry_case(args) -> int:
    """验证临时 runner 失败会被下一轮 dispatch 自动重试。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=runner-retry")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioRetryBackend()
    agent.backend = backend
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会先失败一次的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 第一次调用模型失败，下一轮 dispatch 应自动重试",
        thought="验证临时模型/接口错误不会让任务永久卡死。",
        plan=["第一次 runner 失败", "下一轮自动重试", "成功后父代理验收"],
        acceptance_checks=["第二次 runner 必须生成证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "第一轮 dispatch：模拟 runner 临时失败")
    first = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="first attempt should fail",
    )
    print_dispatch_report(first)
    after_first = agent.subagents.load(task.id)
    print(
        f"after_first status={after_first.status} failure_type={after_first.failure_type} "
        f"attempts={after_first.runner_attempts}"
    )

    print_scenario_step(3, "第二轮 dispatch：自动重试并验收")
    second = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="retry should succeed",
    )
    print_dispatch_report(second)
    loaded = agent.subagents.load(task.id)
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"attempts={loaded.runner_attempts} backend_calls={backend.calls}"
    )

    first_runner = [item for item in first.records if item.step == "runner"]
    second_runner = [item for item in second.records if item.step == "runner"]
    final_ok = (
        first_runner
        and first_runner[0].action == "execute_runner"
        and not first_runner[0].ok
        and after_first.status == "BLOCKED"
        and after_first.failure_type == "runner_error"
        and after_first.runner_attempts == 1
        and second_runner
        and second_runner[0].action == "retry_runner"
        and second_runner[0].ok
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and loaded.runner_attempts == 2
        and backend.calls == 2
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="runner retry passed" if final_ok else "runner retry failed",
        extra={
            "case": "runner-retry",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "first_status": after_first.status,
            "final_status": loaded.status,
            "runner_attempts": loaded.runner_attempts,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def create_scenario_workspace(args) -> ScenarioPaths:
    """创建一次不会污染开发仓库的场景测试目录。"""

    parent = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path(tempfile.gettempdir()) / "my-agent-scenarios"
    )
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_root = parent / f"scenario-{stamp}-{uuid.uuid4().hex[:6]}"
    fixture_root = run_root / "fixture_project"
    fixture_root.mkdir(parents=True, exist_ok=True)
    write_scenario_fixture(fixture_root)
    config_path = run_root / "scenario_agent_config.yaml"
    write_scenario_config(
        source_config=Path(args.config),
        target_config=config_path,
        fixture_root=fixture_root,
        request_timeout=args.timeout,
        max_subagents=max(args.count, 1),
    )
    return ScenarioPaths(
        run_root=run_root,
        fixture_root=fixture_root,
        config=config_path,
        summary_json=run_root / "scenario_summary.json",
        summary_md=run_root / "SCENARIO_SUMMARY.md",
    )


def write_scenario_fixture(fixture_root: Path) -> None:
    """写一个足够小、可被真实 runner 安全读写的项目。"""

    (fixture_root / "README.md").write_text(
        "\n".join(
            [
                "# My Agent Scenario Fixture",
                "",
                "这是 my-agent 隔离全流程测试用的小项目。",
                "所有 runner 只能在这个目录里读写文件。",
                "",
                "## 验收目标",
                "",
                "- 子代理必须读取本 README。",
                "- 子代理必须在 scenario_outputs/ 里写入自己的报告。",
                "- 父代理必须完成 runner 调度和验收闭环。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (fixture_root / "notes").mkdir(parents=True, exist_ok=True)
    (fixture_root / "notes" / "input_a.md").write_text(
        "A 组素材：检查 fixture 的 README，并说明读写工具是否可用。\n",
        encoding="utf-8",
    )
    (fixture_root / "notes" / "input_b.md").write_text(
        "B 组素材：输出一份简短证据报告，证明任务只在隔离目录内运行。\n",
        encoding="utf-8",
    )
    (fixture_root / "scenario_outputs").mkdir(parents=True, exist_ok=True)


def write_scenario_config(
    *,
    source_config: Path,
    target_config: Path,
    fixture_root: Path,
    request_timeout: float,
    max_subagents: int,
) -> None:
    """基于当前配置写一份隔离配置，保留模型和 API 设置。"""

    base = source_config.read_text(encoding="utf-8")
    fixture = str(fixture_root).replace("\\", "/")
    overrides = f"""

# scenario-test isolation overrides
workspace_root: "{fixture}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
max_subagents: {max_subagents}
gateway_request_timeout: {int(request_timeout)}
gateway_request_poll_interval: 1
daemon_planner: false
daemon_apply: false
daemon_execute_runners: false
daemon_max_runners: 0
daemon_interval: 1
runner_failure_policy: "auto"
max_tool_rounds: 8
"""
    target_config.write_text(base + overrides, encoding="utf-8")


def load_scenario_agent(config_path: Path) -> SimpleAgent:
    """加载隔离配置对应的 agent。"""

    class Args:
        config = str(config_path)

    return make_agent(Args())


def build_scenario_prompt(count: int) -> str:
    """构建主代理派工 prompt，尽量让真实模型稳定调用派工工具。"""

    return (
        "这是 my-agent 隔离全流程场景测试。你必须通过工具创建子代理工单，"
        "不要自己直接完成任务。\n\n"
        "请只调用一次 create_subagents，参数必须满足：\n"
        f"- count: {count}\n"
        "- tool_preset: coding\n"
        "- goal: 在隔离 fixture 项目中读取 README.md，并在 scenario_outputs/ 写入自己的证据报告\n"
        "- acceptance_checks: 必须有 read_file 证据；必须有 write_file 证据；必须等待父代理验收\n"
        "- plan: 读取 README.md；写入 scenario_outputs/<run_id>.md；输出 SUBAGENT_RESULT；等待验收\n\n"
        "创建后可以调用 subagent_board 看一眼状态，然后用一句话汇报创建了几个子代理。"
    )


def build_scenario_runner_instruction() -> str:
    """给每个真实 runner 的稳定执行说明。"""

    return (
        "这是隔离全流程测试的 runner 阶段。你只能在当前 fixture 工作区内操作。\n"
        "必须严格按顺序完成，不允许跳步：\n"
        "1. 第一轮先只调用 read_file，payload 精确使用 {\"tool\":\"read_file\",\"path\":\"README.md\"}。\n"
        "2. 收到 read_file 成功结果后，从执行上下文 JSON 找到自己的 run_id。\n"
        "3. 第二轮只调用 write_file，path 使用 scenario_outputs/<run_id>.md，content 写一份 3-6 行中文报告，"
        "说明已读取 README.md，并注明这是隔离测试。\n"
        "4. 只有在你已经看到 write_file 成功结果后，才允许输出最终 [SUBAGENT_RESULT]。\n"
        "5. 最终回复只能包含一个 [SUBAGENT_RESULT] JSON 结果块，不要输出 Markdown 代码围栏。\n"
        "JSON 必须包含：status=AWAITING_ACCEPTANCE；summary；used_tools 至少包含 read_file 和 write_file；"
        "evidence 至少两条，分别证明 README.md 已读取、scenario_outputs/<run_id>.md 已写入；"
        "tests 至少一条 ok=true；artifacts 包含写入的报告路径；patches 为空数组。"
    )


def run_scenario_gateway_ask(paths: ScenarioPaths, prompt: str, *, timeout: float) -> dict[str, object]:
    """用隔离配置启动 gateway、投递一次 ask，然后关闭 gateway。"""

    def command(*parts: str) -> list[str]:
        return [sys.executable, "-m", "agent_py_agent", "--config", str(paths.config), *parts]

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    start = run_scenario_subprocess(command("gateway", "start", "--force"), env=env, timeout=60)
    if start.returncode != 0:
        return {"ok": False, "error": "gateway start failed", "stdout": start.stdout, "stderr": start.stderr}
    try:
        ask = run_scenario_subprocess(
            command("gateway", "ask", prompt, "--timeout", str(timeout), "--no-save", "--json"),
            env=env,
            timeout=timeout + 30,
        )
        if ask.returncode != 0:
            return {"ok": False, "error": "gateway ask failed", "stdout": ask.stdout, "stderr": ask.stderr}
        try:
            payload = json.loads(ask.stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"gateway response was not JSON: {exc}", "stdout": ask.stdout}
        return payload
    finally:
        run_scenario_subprocess(
            command("gateway", "stop", "--timeout", "10", "--kill", "--reason", "scenario-test done"),
            env=env,
            timeout=30,
        )


def run_scenario_subprocess(cmd: list[str], *, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """运行隔离场景里的 CLI 子命令，并把输出原样展示给用户观察。"""

    print("$", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=ROOT.parent,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr)
    return completed


def print_scenario_step(index: int, title: str) -> None:
    print(f"\n== {index}. {title} ==")


def print_scenario_board(agent: SimpleAgent, *, limit: int) -> None:
    """打印一份短看板，方便观察当前阶段。"""

    board = agent.subagents.write_board(recent_limit=limit)
    print("board_summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    for item in board.items[:limit]:
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"tools_evidence={item.evidence_count} flags={','.join(item.risk_flags) or 'ok'} :: {item.goal}"
        )
    print(f"board_json={agent.subagents.workspace / 'subagent_board.json'}")
    print(f"board_md={agent.subagents.workspace / 'SUBAGENT_BOARD.md'}")


def scenario_tasks_verified(agent: SimpleAgent, expected_count: int) -> bool:
    tasks = agent.subagents.list_runs()
    if len(tasks) < expected_count:
        return False
    return all(
        task.status == "DONE" and task.verification_status == "VERIFIED"
        for task in tasks[:expected_count]
    )


def write_scenario_summary(
    paths: ScenarioPaths,
    *,
    ok: bool,
    reason: str,
    extra: dict[str, object] | None = None,
) -> None:
    """写机器可读和人类可读的场景测试摘要。"""

    payload = {
        "ok": ok,
        "reason": reason,
        "run_root": str(paths.run_root),
        "fixture_root": str(paths.fixture_root),
        "config": str(paths.config),
        "summary_json": str(paths.summary_json),
        "summary_md": str(paths.summary_md),
        **(extra or {}),
    }
    paths.summary_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = [
        "# Scenario Test Summary",
        "",
        f"- ok: {ok}",
        f"- reason: {reason}",
        f"- run_root: {paths.run_root}",
        f"- fixture_root: {paths.fixture_root}",
        f"- config: {paths.config}",
    ]
    if extra:
        lines.extend(["", "## Extra", "", "```json", json.dumps(extra, ensure_ascii=False, indent=2), "```"])
    paths.summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _gateway_request_loop(args, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """后台处理 gateway inbox 请求。

    这个线程会按配置启动一个很保守的 worker pool。每个 worker 都有自己的
    `SimpleAgent` 实例，避免并发请求共享 backend / LocalStore 连接。
    """

    try:
        bootstrap_agent = make_agent(args)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        print(f"gateway request worker failed to initialize: {exc}", file=sys.stderr)
        return
    workers: list[threading.Thread] = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=_gateway_request_worker_loop,
            args=(args, paths, stop_event, index),
            daemon=True,
        )
        thread.start()
        workers.append(thread)
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for thread in workers:
        thread.join(timeout=2)


def _gateway_request_worker_loop(args, paths: GatewayPaths, stop_event: threading.Event, worker_index: int) -> None:
    """单个 gateway request worker。"""

    try:
        agent = make_agent(args)
    except Exception as exc:
        print(f"gateway request worker {worker_index} failed to initialize: {exc}", file=sys.stderr)
        return

    poll_interval = max(1, int(agent.config.gateway_request_poll_interval))
    while not stop_event.is_set():
        try:
            if worker_index == 0:
                recover_gateway_processing_requests(
                    paths,
                    startup=False,
                    max_attempts=agent.config.gateway_request_max_attempts,
                    timeout_seconds=agent.config.gateway_processing_timeout_seconds,
                    agent=agent,
                )
            processed = _process_gateway_requests(agent, paths, worker_id=f"gw-worker-{worker_index}")
        except Exception as exc:
            print(f"gateway request worker {worker_index} failed: {exc}", file=sys.stderr)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)



def _gateway_heartbeat_loop(paths: GatewayPaths, agent: SimpleAgent, options: DaemonOptions, stop_event: threading.Event) -> None:
    """定期写 gateway heartbeat。"""

    while not stop_event.is_set():
        _write_gateway_heartbeat(paths, agent, options, status="running", pid=os.getpid())
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
    options: DaemonOptions,
    *,
    status: str,
    pid: int,
) -> None:
    write_json_file(
        paths.heartbeat,
        {
            "status": status,
            "pid": pid,
            "updated_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "apply": options.apply,
            "execute_runners": options.execute_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "request_counts": gateway_request_counts(paths),
        },
    )


def cmd_subagent_context(args) -> int:
    """生成单个 subagent 的执行上下文包。"""

    agent = make_agent(args)
    context = agent.subagents.write_execution_context(args.run_id, max_cards=args.max_cards)
    print("SUBAGENT EXECUTION CONTEXT")
    print(
        f"run_id={context.run_id} skills={len(context.allowed_skills)} "
        f"tools={len(context.allowed_tools)} cards={len(context.granted_cards)}"
    )
    print(f"已写入: {context.execution_context_json}")
    print(f"已写入: {context.execution_context_file}")
    return 0


def cmd_subagent_run(args) -> int:
    """按 execution context 运行或 dry-run 一个 subagent。"""

    agent = make_agent(args)
    result = agent.run_subagent(
        args.run_id,
        instruction=args.instruction or "",
        dry_run=not args.execute,
        max_cards=args.max_cards,
        probe=not args.no_probe,
    )
    mode = "execute" if args.execute else "dry-run"
    status = "OK" if result.ok else "FAIL"
    print("SUBAGENT RUNNER")
    print(
        f"mode={mode} ok={status} run_id={result.run_id} "
        f"status={result.status} verify={result.verification_status}"
    )
    print(f"message={result.message}")
    print(f"已写入: {result.execution_context_json}")
    print(f"已写入: {result.result_json}")
    print(f"已写入: {result.result_file}")
    if result.prompt_file:
        print(f"prompt: {result.prompt_file}")
    if result.response_file:
        print(f"response: {result.response_file}")
    return 0 if result.ok else 1


def cmd_subagent_detail(args) -> int:
    """显示单个子代理运行详情。"""

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(json.dumps(task.__dict__, ensure_ascii=False, indent=2, default=lambda value: value.__dict__))
    return 0


def cmd_chat(args) -> int:
    """启动交互循环。"""

    agent = make_agent(args)
    use_gateway = bool(args.gateway)
    paths = gateway_paths(agent)
    if use_gateway:
        _, alive = wait_for_gateway_running(paths, timeout=10.0)
        if not alive:
            print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
            return 2
    print(
        f"{agent.config.agent_name} 交互循环已启动。"
        "输入 /help 查看命令，输入 /exit 或 /logout 退出，也可以直接按 Ctrl+C。"
    )
    if use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []
    jobs: queue.Queue[ChatJob] = queue.Queue()
    state_lock = threading.Lock()
    is_running = False
    pending_jobs = 0
    shutting_down = False
    running_prompt = ""
    running_started_at = 0.0
    prompt_session = (
        PromptSession()
        if PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty()
        else None
    )
    fallback_interactive = prompt_session is None and sys.stdin.isatty() and sys.stdout.isatty()
    fallback_waiting_for_input = False

    def bottom_toolbar() -> str:
        with state_lock:
            active_count = pending_jobs + (1 if is_running else 0)
            elapsed = time.perf_counter() - running_started_at if is_running else 0
        if not active_count:
            return ""
        if is_running:
            return f"思考中... {elapsed:.0f}s | 队列 {pending_jobs}"
        return f"等待处理 | 队列 {pending_jobs}"

    def redraw_fallback_prompt() -> None:
        """Redraw the plain input prompt after background output.

        prompt_toolkit handles this automatically. The stdlib input() fallback
        does not, so a background reply can leave the terminal without a visible
        `user> ` prompt even though input is still waiting.
        """

        if not fallback_interactive:
            return
        with state_lock:
            should_redraw = fallback_waiting_for_input and not shutting_down
        if should_redraw:
            print(FALLBACK_CHAT_PROMPT, end="", flush=True)

    def worker() -> None:
        nonlocal is_running, pending_jobs, running_prompt, running_started_at
        while True:
            job = jobs.get()
            with state_lock:
                pending_jobs -= 1
                is_running = True
                running_prompt = job.user
                running_started_at = time.perf_counter()
            try:
                started_at = running_started_at
                print(f"\n正在处理: {job.user}", flush=True)
                if use_gateway:
                    _, alive = gateway_running(paths)
                    if not alive:
                        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
                    request_id, _, response_path = submit_gateway_ask(
                        paths,
                        prompt=job.user,
                        inject=job.inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                        include_prompt=job.show_prompt,
                        agent=agent,
                    )
                    timeout = (
                        args.gateway_timeout
                        if args.gateway_timeout is not None
                        else agent.config.gateway_request_timeout
                    )
                    response = wait_for_gateway_response(paths, request_id, timeout)
                    elapsed = time.perf_counter() - started_at
                    if not response:
                        raise TimeoutError(
                            f"gateway 请求等待超时: request_id={request_id} response={response_path}"
                        )
                    if job.show_prompt and response.get("prompt"):
                        print("===== FINAL PROMPT =====")
                        print(response.get("prompt", ""))
                        print("===== RESPONSE =====")
                    print(
                        f"[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}]"
                    )
                    if response.get("ok"):
                        print(f"{agent.config.agent_name}> {response.get('response', '')}")
                    else:
                        print(f"错误: {response.get('error', 'gateway 请求失败')}")
                else:
                    result = agent.run(
                        job.user,
                        inject=job.inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                    )
                    elapsed = time.perf_counter() - started_at
                    if job.show_prompt:
                        print("===== FINAL PROMPT =====")
                        print(result.prompt)
                        print("===== RESPONSE =====")
                    print(f"[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}]")
                    print(f"{agent.config.agent_name}> {result.response}")
            except Exception as exc:
                print(f"错误: {exc}")
            finally:
                with state_lock:
                    is_running = False
                    running_prompt = ""
                    running_started_at = 0.0
                jobs.task_done()
                redraw_fallback_prompt()

    threading.Thread(target=worker, daemon=True).start()

    def enqueue_job(user: str, *, show_prompt: bool = False) -> None:
        nonlocal pending_jobs
        job = ChatJob(
            user=user,
            show_prompt=show_prompt,
            inject=list(runtime_inject),
            prompt_files=list(prompt_files),
        )
        with state_lock:
            active_count = pending_jobs + (1 if is_running else 0)
            pending_jobs += 1
        jobs.put(job)
        if active_count:
            print(f"已加入任务队列，前面还有 {active_count} 个任务。")
        else:
            if use_gateway:
                print("已发送到 gateway 后台，模型响应期间可以继续输入。")
            else:
                print("已发送到后台，模型响应期间可以继续输入。")

    output_context = patch_stdout() if prompt_session is not None else nullcontext()
    with output_context:
        while True:
            try:
                if prompt_session is not None:
                    user = prompt_session.prompt(
                        CHAT_PROMPT,
                        bottom_toolbar=bottom_toolbar,
                        refresh_interval=1,
                    ).strip()
                else:
                    if fallback_interactive:
                        print(FALLBACK_CHAT_PROMPT, end="", flush=True)
                        with state_lock:
                            fallback_waiting_for_input = True
                        try:
                            user = input().strip()
                        finally:
                            with state_lock:
                                fallback_waiting_for_input = False
                    else:
                        user = input(FALLBACK_CHAT_PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return 0
            if not user:
                continue
            if user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}:
                shutting_down = True
                with state_lock:
                    active_count = pending_jobs + (1 if is_running else 0)
                if active_count:
                    print(f"还有 {active_count} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
                    jobs.join()
                print("再见。")
                return 0
            if user == "/help":
                print(
                    """可用命令：
/help                         显示帮助
/status                       查看后台任务状态；gateway 模式会额外显示 gateway 状态
/exit                         退出
/logout                       退出
exit / logout                 兼容旧习惯
/memory [关键词]              搜索记忆；不带关键词显示最近记忆
/remember <内容>              手动写入记忆
/btw                         显示当前运行时 prompt 注入
/btw <内容>                   增加运行时 prompt 注入
/btw-clear                   清空运行时 prompt 注入
/prompt-file <路径>           增加动态 prompt 文件
/subagents <数量> <目标>      生成 subagent 任务记录
/show-prompt <问题>           显示最终 prompt 并回答
Ctrl+C                        退出
其他输入                       正常对话
"""
                )
                continue
            if user == "/status":
                with state_lock:
                    active_count = pending_jobs + (1 if is_running else 0)
                    prompt = running_prompt
                    elapsed = time.perf_counter() - running_started_at if is_running else 0
                if not active_count:
                    print("当前没有后台任务。")
                elif is_running:
                    print(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs} 个任务。")
                    print(f"当前任务: {prompt}")
                else:
                    print(f"当前没有运行中的任务；队列中还有 {pending_jobs} 个任务。")
                if use_gateway:
                    for line in render_gateway_status(agent, paths):
                        print(line)
                continue
            if user.startswith("/remember "):
                rec = agent.remember(user[len("/remember ") :], kind="note")
                print(f"已记忆: {rec.content}")
                continue
            if user.startswith("/memory"):
                query = user[len("/memory") :].strip()
                records = (
                    agent.recall(query, args.memory_limit)
                    if query
                    else agent.memory.all()[-args.memory_limit :]
                )
                if not records:
                    print("没有找到记忆。")
                for rec in records:
                    print(f"- [{rec.kind}] {rec.role}: {rec.content}")
                continue
            if user == "/btw":
                if not runtime_inject:
                    print("当前没有运行时 prompt 注入。")
                else:
                    print("当前运行时 prompt 注入：")
                    for index, item in enumerate(runtime_inject, 1):
                        print(f"{index}. {item}")
                continue
            if user.startswith("/btw "):
                runtime_inject.append(user[len("/btw ") :])
                print(f"已加入注入 prompt，当前 {len(runtime_inject)} 条。")
                continue
            if user == "/btw-clear":
                runtime_inject.clear()
                print("已清空运行时 prompt 注入。")
                continue
            if user.startswith("/prompt-file "):
                prompt_files.append(user[len("/prompt-file ") :].strip())
                print(f"已加入 prompt 文件，当前 {len(prompt_files)} 个。")
                continue
            if user.startswith("/subagents "):
                parts = user.split(maxsplit=2)
                if len(parts) < 3 or not parts[1].isdigit():
                    print("用法: /subagents <数量> <目标>")
                    continue
                tasks = agent.spawn_subagents(parts[2], int(parts[1]))
                for task in tasks:
                    print(f"- {task.id}: {task.goal}")
                continue

            show_prompt = False
            if user.startswith("/show-prompt "):
                show_prompt = True
                user = user[len("/show-prompt ") :]

            enqueue_job(user, show_prompt=show_prompt)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="my-agent",
        description="Simple Python3 CLI Agent with memory, dynamic prompt and subagents.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认使用 config/agent_config.yaml",
    )
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_default)

    status = sub.add_parser("status", help="查看 my-agent 全局状态")
    status.add_argument("--limit", type=int, default=5, help="最多显示多少条 hot/recent/timeline 项")
    status.add_argument("--recent", action="store_true", help="显示最近子代理列表")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_status)

    timeline = sub.add_parser("timeline", help="查看本地事实源最近事件")
    timeline.add_argument("--limit", type=int, default=20, help="最多显示多少条事件")
    timeline.add_argument("--source-type", help="按来源过滤，如 gateway_request/subagent_run")
    timeline.add_argument("--event-type", help="按事件类型过滤，如 gateway_request_completed")
    timeline.add_argument("--details", action="store_true", help="显示事件 payload 摘要")
    timeline.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    timeline.set_defaults(func=cmd_timeline)

    run = sub.add_parser("run", help="运行一次智能体对话")
    run.add_argument("prompt", help="用户任务 / prompt")
    run.add_argument("--inject", action="append", help="动态注入 prompt，可多次传入")
    run.add_argument("--prompt-file", action="append", help="额外动态 prompt 文件，可多次传入")
    run.add_argument("--save", action="store_true", default=None, help="保存本次对话到记忆")
    run.add_argument("--no-save", action="store_false", dest="save", help="不保存本次对话到记忆")
    run.add_argument("--show-prompt", action="store_true", help="打印最终拼装后的 prompt")
    run.set_defaults(func=cmd_run)

    remember = sub.add_parser("remember", help="手动写入一条记忆")
    remember.add_argument("content", help="记忆内容")
    remember.add_argument("--kind", default="note", help="记忆类型，如 note/preference/fact")
    remember.set_defaults(func=cmd_remember)

    memory_list = sub.add_parser("memory-list", help="列出最近记忆")
    memory_list.add_argument("--limit", type=int, default=20, help="最多显示条数")
    memory_list.set_defaults(func=cmd_memory_list)

    memory_search = sub.add_parser("memory-search", help="搜索记忆")
    memory_search.add_argument("query", help="搜索关键词")
    memory_search.add_argument("--limit", type=int, default=5, help="最多显示条数")
    memory_search.set_defaults(func=cmd_memory_search)

    local_store_status = sub.add_parser("local-store-status", help="查看本地事实源状态")
    local_store_status.set_defaults(func=cmd_local_store_status)

    local_search = sub.add_parser("local-search", help="搜索本地事实源 SQLite/FTS5 索引")
    local_search.add_argument("query", help="搜索关键词；为空时可用 local-store-status 看整体状态")
    local_search.add_argument("--limit", type=int, default=5, help="最多显示条数")
    local_search.add_argument("--source-type", help="按来源过滤，如 memory/gateway_request/subagent_run")
    local_search.add_argument("--visibility", help="按可见性过滤，默认不过滤")
    local_search.add_argument("--preview-chars", type=int, default=500, help="每条命中最多打印多少正文字符；-1 表示完整打印")
    local_search.set_defaults(func=cmd_local_search)

    local_index_memory = sub.add_parser("local-index-memory", help="把现有 JSONL 记忆补建到本地事实源")
    local_index_memory.set_defaults(func=cmd_local_index_memory)

    local_doctor = sub.add_parser("local-doctor", help="诊断 LocalStore、gateway 队列和 subagent 文件事实源")
    local_doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    local_doctor.add_argument("--repair", action="store_true", help="处理超时 processing gateway 请求")
    local_doctor.add_argument("--limit", type=int, default=20, help="每类问题最多显示多少条")
    local_doctor.set_defaults(func=cmd_local_doctor)

    local_rebuild = sub.add_parser("local-rebuild", help="从 memory/gateway/subagent 文件事实源重建 LocalStore")
    local_rebuild.add_argument(
        "--source",
        action="append",
        choices=["all", "memory", "gateway", "subagent", "fts"],
        help="只重建指定来源，可多次传入；默认 all",
    )
    local_rebuild.add_argument("--reset", action="store_true", help="先清空 LocalStore records/events/FTS 再重建")
    local_rebuild.set_defaults(func=cmd_local_rebuild)

    chat = sub.add_parser("chat", help="启动交互循环，反复与智能体交流")
    chat.add_argument("--inject", action="append", help="启动时注入 prompt，可多次传入")
    chat.add_argument("--prompt-file", action="append", help="启动时加载额外 prompt 文件，可多次传入")
    chat.add_argument("--memory-limit", type=int, default=5, help="交互中 /memory 默认显示条数")
    chat.add_argument("--no-save", action="store_true", help="交互对话不自动保存到记忆")
    chat.add_argument("--gateway", action="store_true", help="把普通聊天消息投递给后台 gateway，而不是在当前前台进程里调用模型")
    chat.add_argument("--gateway-timeout", type=float, help="gateway 模式等待单条响应的秒数，默认使用配置 gateway_request_timeout")
    chat.set_defaults(func=cmd_chat)

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

    due_check = sub.add_parser("subagents-due-check", help="巡检 subagent 并输出父代理待处理项")
    due_check.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    due_check.add_argument("--all", action="store_true", help="显示全部问题，而不是按 limit 截断")
    due_check.add_argument("--limit", type=int, default=20, help="最多显示多少条问题")
    due_check.set_defaults(func=cmd_subagents_due_check)

    probe = sub.add_parser("subagents-probe", help="检查 subagent 通道健康状态")
    probe.add_argument("run_id", nargs="*", help="子代理运行 ID；不传则检查最近记录")
    probe.add_argument("--limit", type=int, default=20, help="不指定 run_id 时最多检查多少条")
    probe.set_defaults(func=cmd_subagents_probe)

    action_plan = sub.add_parser("subagents-plan-actions", help="根据 due-check 生成 dry-run 动作计划")
    action_plan.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    action_plan.add_argument("--all", action="store_true", help="显示全部动作，而不是按 limit 截断")
    action_plan.add_argument("--limit", type=int, default=20, help="最多显示多少条动作")
    action_plan.set_defaults(func=cmd_subagents_plan_actions)

    apply_actions = sub.add_parser("subagents-apply-actions", help="执行或 dry-run 执行 action plan")
    apply_actions.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    apply_actions.add_argument("--dry-run", action="store_false", dest="apply", help="只预览动作，不修改记录")
    apply_actions.add_argument("--apply", action="store_true", help="真正执行低风险动作")
    apply_actions.add_argument("--action", help="只处理指定动作，如 reopen_for_evidence")
    apply_actions.add_argument("--run-id", help="只处理指定子代理运行 ID")
    apply_actions.add_argument("--limit", type=int, default=20, help="最多处理多少条动作")
    apply_actions.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    apply_actions.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    apply_actions.set_defaults(func=cmd_subagents_apply_actions, apply=False)

    route = sub.add_parser("subagents-route-capabilities", help="路由 OPEN capability request")
    route.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    route.add_argument("--dry-run", action="store_false", dest="apply", help="只预览路由，不生成 grant/gap")
    route.add_argument("--apply", action="store_true", help="真正生成 capability grant 或 gap")
    route.add_argument("--run-id", nargs="*", help="只处理指定子代理运行 ID")
    route.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    route.add_argument("--limit", type=int, default=20, help="最多处理多少条 request")
    route.set_defaults(func=cmd_subagents_route_capabilities, apply=False)

    acceptance = sub.add_parser("subagents-acceptance", help="验收等待验收的 subagent")
    acceptance.add_argument("--dry-run", action="store_false", dest="apply", help="只生成验收报告，不修改记录")
    acceptance.add_argument("--apply", action="store_true", help="验收通过时标记 DONE/VERIFIED，失败时标记 BLOCKED/FAILED")
    acceptance.add_argument("--run-id", nargs="*", help="只验收指定子代理运行 ID")
    acceptance.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    acceptance.add_argument("--reviewer", default="parent", help="验收者标识")
    acceptance.add_argument("--note", help="写入验收记录的备注")
    acceptance.set_defaults(func=cmd_subagents_acceptance, apply=False)

    patches = sub.add_parser("subagents-patches", help="审核 runner 输出里的 patch 记录")
    patches.add_argument("--dry-run", action="store_false", dest="apply", help="只生成 patch 审核报告，不修改记录")
    patches.add_argument("--apply", action="store_true", help="写回 patch 审核状态")
    patches.add_argument("--run-id", nargs="*", help="只审核指定子代理运行 ID")
    patches.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    patches.add_argument("--reviewer", default="parent", help="审核者标识")
    patches.add_argument("--note", help="写入 patch 审核记录的备注")
    patches.set_defaults(func=cmd_subagents_patches, apply=False)

    dispatch = sub.add_parser("subagents-dispatch", help="执行一轮父代理调度，默认 dry-run")
    dispatch.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    dispatch.add_argument("--dry-run", action="store_false", dest="apply", help="只生成调度报告，不修改记录")
    dispatch.add_argument("--apply", action="store_true", help="执行低风险调度动作并写审计日志")
    dispatch.add_argument("--execute-runners", action="store_true", help="配合 --apply 调用真实模型执行 runner")
    dispatch.add_argument("--planner", action="store_true", help="有待处理事项时调用父代理 LLM planner，禁止空心 HEARTBEAT_OK")
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

    daemon = sub.add_parser("daemon", help="按配置启动前台常驻调度")
    daemon.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    daemon.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    daemon.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    daemon.add_argument("--execute-runners", action="store_true", dest="execute_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    daemon.add_argument("--no-execute-runners", action="store_false", dest="execute_runners", help="覆盖配置：不调用真实模型执行 runner")
    daemon.add_argument("--planner", action="store_true", dest="planner", default=None, help="覆盖配置：启用父代理 LLM planner")
    daemon.add_argument("--no-planner", action="store_false", dest="planner", help="覆盖配置：关闭父代理 LLM planner")
    daemon.add_argument("--interval", type=float, help="覆盖配置：每轮间隔秒数，0 表示不等待")
    daemon.add_argument("--max-runners", help="覆盖配置：每轮最多推进多少个 runner；auto 表示保守自适应，0 表示不执行 runner")
    daemon.add_argument("--limit", type=int, help="覆盖配置：每个阶段最多处理多少条记录，0 表示不限制")
    daemon.add_argument("--max-cycles", type=int, help="覆盖配置：最多循环次数，0 表示持续运行")
    daemon.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    daemon.add_argument("--reviewer", help="覆盖配置：patch/acceptance 审核者标识")
    daemon.add_argument("--note", help="写入调度关联审核记录的备注")
    daemon.add_argument("--instruction", help="覆盖配置：给 runner 的额外指令")
    daemon.add_argument("--max-cards", type=int, help="覆盖配置：runner 最多注入多少张能力卡，0 表示不限制")
    daemon.add_argument("--no-probe", action="store_true", help="覆盖配置：执行 runner 前不做通道健康检查")
    daemon.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    daemon.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    daemon.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    daemon.set_defaults(func=cmd_daemon)

    scenario = sub.add_parser("scenario-test", help="跑一轮隔离的真实全流程任务测试")
    scenario.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    scenario.add_argument(
        "--case",
        choices=["happy", "verification", "gateway-restart", "structured-repair", "runner-retry", "all"],
        default="happy",
        help="场景类型：happy 跑真实全流程；verification 测验收防作弊；gateway-restart 测重启恢复；structured-repair 测坏结构化输出修复；runner-retry 测 runner 失败重试；all 连续运行",
    )
    scenario.add_argument("--workspace", help="保存场景测试结果的父目录；不传则使用系统临时目录")
    scenario.add_argument("--count", type=int, default=2, help="本场景创建多少个子代理")
    scenario.add_argument("--max-runners", type=int, default=2, help="每轮最多推进多少个 runner")
    scenario.add_argument("--max-cycles", type=int, default=3, help="最多执行多少轮 dispatch")
    scenario.add_argument("--timeout", type=float, default=300.0, help="gateway ask 等待响应的秒数")
    scenario.add_argument("--dry-run", action="store_true", help="只调度不执行真实 runner API")
    scenario.add_argument("--planner", action="store_true", help="dispatch 时启用父代理 planner")
    scenario.add_argument("--direct", action="store_true", help="不经过 gateway，直接用当前进程跑主代理派工")
    scenario.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    scenario.set_defaults(func=cmd_scenario_test)

    gateway = sub.add_parser("gateway", help="管理后台 gateway 进程")
    gateway_sub = gateway.add_subparsers(dest="gateway_command")
    gateway.set_defaults(func=cmd_gateway)

    gateway_start = gateway_sub.add_parser("start", help="启动后台 gateway")
    gateway_start.add_argument("--force", action="store_true", help="已有 gateway 运行时先尝试停止再启动")
    gateway_start.add_argument("--force-lock", action="store_true", help="传给内部 daemon，强制覆盖已有 dispatch watch lock")
    gateway_start.set_defaults(func=cmd_gateway_start)

    gateway_run = gateway_sub.add_parser("run", help="内部命令：前台运行 gateway 循环")
    gateway_run.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    gateway_run.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    gateway_run.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    gateway_run.add_argument("--execute-runners", action="store_true", dest="execute_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    gateway_run.add_argument("--no-execute-runners", action="store_false", dest="execute_runners", help="覆盖配置：不调用真实模型执行 runner")
    gateway_run.add_argument("--planner", action="store_true", dest="planner", default=None, help="覆盖配置：启用父代理 LLM planner")
    gateway_run.add_argument("--no-planner", action="store_false", dest="planner", help="覆盖配置：关闭父代理 LLM planner")
    gateway_run.add_argument("--interval", type=float, help="覆盖配置：每轮间隔秒数，0 表示不等待")
    gateway_run.add_argument("--max-runners", help="覆盖配置：每轮最多推进多少个 runner；auto 表示保守自适应，0 表示不执行 runner")
    gateway_run.add_argument("--limit", type=int, help="覆盖配置：每个阶段最多处理多少条记录，0 表示不限制")
    gateway_run.add_argument("--max-cycles", type=int, help="覆盖配置：最多循环次数，0 表示持续运行")
    gateway_run.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    gateway_run.add_argument("--reviewer", help="覆盖配置：patch/acceptance 审核者标识")
    gateway_run.add_argument("--note", help="写入调度关联审核记录的备注")
    gateway_run.add_argument("--instruction", help="覆盖配置：给 runner 的额外指令")
    gateway_run.add_argument("--max-cards", type=int, help="覆盖配置：runner 最多注入多少张能力卡，0 表示不限制")
    gateway_run.add_argument("--no-probe", action="store_true", help="覆盖配置：执行 runner 前不做通道健康检查")
    gateway_run.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    gateway_run.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    gateway_run.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    gateway_run.set_defaults(func=cmd_gateway_run)

    gateway_status = gateway_sub.add_parser("status", help="查看 gateway 状态")
    gateway_status.set_defaults(func=cmd_gateway_status)

    gateway_stop = gateway_sub.add_parser("stop", help="请求 gateway 停止")
    gateway_stop.add_argument("--timeout", type=float, help="等待正常停止的秒数，默认使用配置")
    gateway_stop.add_argument("--kill", action="store_true", help="超时后强制终止进程")
    gateway_stop.add_argument("--reason", help="写入 stop request 的原因")
    gateway_stop.set_defaults(func=cmd_gateway_stop)

    gateway_restart = gateway_sub.add_parser("restart", help="重启 gateway")
    gateway_restart.add_argument("--timeout", type=float, help="等待正常停止的秒数，默认使用配置")
    gateway_restart.add_argument("--force", action="store_true", help="停止超时后强制终止旧进程")
    gateway_restart.add_argument("--force-lock", action="store_true", help="传给内部 daemon，强制覆盖已有 dispatch watch lock")
    gateway_restart.set_defaults(func=cmd_gateway_restart)

    gateway_logs = gateway_sub.add_parser("logs", help="显示 gateway 日志尾部")
    gateway_logs.add_argument("--lines", type=int, default=80, help="显示最后多少行日志，0 表示全部")
    gateway_logs.set_defaults(func=cmd_gateway_logs)

    gateway_ask = gateway_sub.add_parser(
        "ask",
        help="向后台 gateway 投递一条聊天请求；未来聊天工具/TUI 会复用这条通道",
    )
    gateway_ask.add_argument("prompt", help="用户任务 / prompt")
    gateway_ask.add_argument("--inject", action="append", help="动态注入 prompt，可多次传入")
    gateway_ask.add_argument("--prompt-file", action="append", help="额外动态 prompt 文件，可多次传入")
    gateway_ask.add_argument("--no-save", action="store_true", help="不保存本次对话到记忆")
    gateway_ask.add_argument("--show-prompt", action="store_true", help="响应返回时打印最终 prompt")
    gateway_ask.add_argument("--timeout", type=float, help="等待 gateway 响应的秒数，默认使用配置")
    gateway_ask.add_argument("--no-wait", action="store_true", help="只投递请求并立即返回 request_id，适合长任务")
    gateway_ask.add_argument("--json", action="store_true", help="输出完整响应 JSON，方便脚本或聊天适配器读取")
    gateway_ask.set_defaults(func=cmd_gateway_ask)

    gateway_result = gateway_sub.add_parser("result", help="读取某个 gateway 请求结果，通常配合 ask --no-wait 使用")
    gateway_result.add_argument("request_id", help="gateway 请求 ID")
    gateway_result.add_argument("--show-prompt", action="store_true", help="打印响应中保存的最终 prompt")
    gateway_result.add_argument("--json", action="store_true", help="输出完整响应 JSON，方便脚本或聊天适配器读取")
    gateway_result.set_defaults(func=cmd_gateway_result)

    adapter = sub.add_parser("adapter", help="外部聊天工具 / TUI 适配器")
    adapter_sub = adapter.add_subparsers(dest="adapter_command")
    adapter.set_defaults(func=cmd_adapter)

    adapter_file = adapter_sub.add_parser("file", help="文件协议适配器：inbox JSON -> gateway -> outbox JSON")
    adapter_file.add_argument("--root", help="适配器根目录；默认使用配置 adapter_workspace")
    adapter_file.add_argument("--inbox", help="覆盖 inbox 目录")
    adapter_file.add_argument("--outbox", help="覆盖 outbox 目录")
    adapter_file.add_argument("--watch", action="store_true", help="持续轮询 inbox")
    adapter_file.add_argument("--once", action="store_true", help="只处理当前已有消息后退出")
    adapter_file.add_argument("--poll-interval", type=float, default=1.0, help="watch 模式轮询间隔秒数")
    adapter_file.add_argument("--limit", type=int, default=20, help="每轮最多处理多少条消息，0 表示不限制")
    adapter_file.add_argument("--timeout", type=float, help="等待 gateway 响应的秒数，默认使用配置 gateway_request_timeout")
    adapter_file.add_argument("--no-start-gateway", action="store_true", help="不自动启动 gateway；未运行时直接失败")
    adapter_file.set_defaults(func=cmd_adapter_file)

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
    return parser


def main() -> int:
    """程序入口。"""

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
