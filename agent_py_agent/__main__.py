from __future__ import annotations

"""简单 Python 智能体的 CLI 入口。"""

import argparse
import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .agent.capabilities import CapabilityRouter
from .agent.capability_config import load_capability_config
from .agent.config import load_config
from .agent.core import SimpleAgent
from .agent.skills import SkillRegistry
from .agent.subagent import filter_board_items

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
class GatewayPaths:
    """gateway 第一版控制面使用的本地文件。

    大白话：
    - pid/state/heartbeat/stop_request/log 负责“后台进程活没活、怎么停、日志在哪”。
    - inbox/processing/done/responses/history 负责“客户端发来的消息怎么交给后台 gateway”。

    当前先用文件队列，而不是 HTTP server 或数据库，是为了跨平台、好调试。
    后续可以把这组路径背后的实现换成 SQLite / WebSocket，但上层命令可以保持不变。
    """

    root: Path
    pid: Path
    state: Path
    heartbeat: Path
    stop_request: Path
    log: Path
    inbox: Path
    processing: Path
    done: Path
    responses: Path
    history: Path


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
            except Exception:
                pass


def make_agent(args) -> SimpleAgent:
    """根据配置创建一个可直接运行的智能体实例。"""

    config = load_config(args.config)
    return SimpleAgent(config, ROOT)


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


def gateway_paths(agent: SimpleAgent) -> GatewayPaths:
    """返回 gateway 控制面文件路径。

    `agent.root` 通常是包目录 `agent_py_agent`，所以默认运行数据会落到
    `agent_py_agent/data/gateway`。测试里会通过配置覆盖到临时目录，避免污染真实数据。
    """

    root = agent.root / agent.config.gateway_workspace
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


def write_json_file(path: Path, payload: dict) -> None:
    """写一个简单 JSON 文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def read_json_file(path: Path) -> dict:
    """读取 JSON 文件；不存在或损坏时返回空 dict。"""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_pid(path: Path) -> int:
    """读取 pid 文件。"""

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def is_pid_alive(pid: int) -> bool:
    """跨平台检查进程是否仍在运行。"""

    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:
    """请求终止一个进程。"""

    if pid <= 0:
        return
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    """等待进程退出。"""

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)


def tail_lines(path: Path, line_count: int) -> list[str]:
    """读取日志末尾若干行。"""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


def new_gateway_request_id() -> str:
    """生成本地 gateway 请求 ID。

    格式里带时间戳，方便人眼粗略判断请求时间；再加随机片段，避免同一秒内冲突。
    """

    return f"gwreq-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:
    """返回某个 gateway 请求的响应文件路径。"""

    return paths.responses / f"{request_id}.json"


def gateway_request_counts(paths: GatewayPaths) -> dict[str, int]:
    """统计 gateway 本地请求队列数量。

    `status` 命令会显示这些数字：
    - pending：还没开始处理的请求。
    - processing：正在处理的请求。
    - done：已经处理完并归档的请求原件。
    - responses：已经写出的响应数量。
    """

    def count_json(path: Path) -> int:
        try:
            return len([item for item in path.glob("*.json") if item.is_file()])
        except OSError:
            return 0

    return {
        "pending": count_json(paths.inbox),
        "processing": count_json(paths.processing),
        "done": count_json(paths.done),
        "responses": count_json(paths.responses),
    }


def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:
    """把一条请求原子写入 gateway inbox。

    先写 `.tmp`，再 rename 到正式文件名。这样 gateway worker 不会读到半截 JSON。
    这是文件队列里很重要的小细节。
    """

    request_id = str(payload["id"])
    paths.inbox.mkdir(parents=True, exist_ok=True)
    target = paths.inbox / f"{request_id}.json"
    tmp = paths.inbox / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:
    """追加 gateway 请求处理历史。

    JSONL 一行一条，方便以后按时间追踪请求，也方便后续迁移到 SQLite。
    """

    paths.history.parent.mkdir(parents=True, exist_ok=True)
    with paths.history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:
    """gateway 启动时把上次崩溃遗留的 processing 请求退回 pending。

    如果 gateway 刚把请求从 pending 移到 processing 就崩了，这个请求既没有 response，
    也不会再被 pending 扫描到。启动时退回 pending，等于告诉系统：“上次没办完，重新排队。”
    """

    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    count = 0
    for request_path in sorted(paths.processing.glob("*.json")):
        try:
            request_path.replace(paths.inbox / request_path.name)
        except OSError:
            continue
        count += 1
    return count


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    """等待 gateway 写出响应 JSON。

    `gateway ask` 默认走同步模式：CLI 写入请求后，就在这里轮询 response 文件。
    `--no-wait` 会跳过等待，用户之后用 `gateway result <request_id>` 再读取。
    """

    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_json_file(path)
        if payload:
            return payload
        time.sleep(0.2)
    return {}


def print_gateway_response(payload: dict, *, json_mode: bool = False, show_prompt: bool = False) -> int:
    """按 CLI 习惯输出 gateway 响应。

    默认输出给人看；`--json` 输出完整机器结果，方便脚本或未来聊天适配器复用。
    """

    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 2

    if show_prompt and payload.get("prompt"):
        print("===== FINAL PROMPT =====")
        print(payload.get("prompt", ""))
        print("===== RESPONSE =====")

    response = str(payload.get("response", "") or "")
    if response:
        print(response)
    else:
        print(str(payload.get("error", "gateway 请求没有返回内容。") or "gateway 请求没有返回内容。"))

    print(
        f"\n[request_id={payload.get('id', '-')}; status={payload.get('status', '-')}; "
        f"backend={payload.get('backend', '-')}; tool_rounds={payload.get('tool_rounds', 0)}]"
    )
    return 0 if payload.get("ok") else 2


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    prompt: str,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool = True,
    include_prompt: bool = False,
) -> tuple[str, Path, Path]:
    """把一条 ask 请求写进 gateway inbox，并返回请求 ID 和文件路径。

    `gateway ask` 和 `chat --gateway` 都走这个函数。这样以后把底层从文件队列
    换成 SQLite/HTTP 时，只需要换这一层，CLI 和 chat 的用户体验可以保持稳定。
    """

    request_id = new_gateway_request_id()
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": prompt,
        "inject": inject or [],
        "prompt_files": prompt_files or [],
        "save": save,
        "include_prompt": include_prompt,
        "created_at": time.time(),
        "client_pid": os.getpid(),
    }
    request_path = write_gateway_request(paths, payload)
    return request_id, request_path, gateway_response_path(paths, request_id)


def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    """返回 gateway pid 和存活状态。"""

    pid = read_pid(paths.pid)
    return pid, bool(pid and is_pid_alive(pid))


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    """生成 gateway 状态摘要，供 `gateway status` 和 `chat --gateway /status` 复用。"""

    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    lines = [
        f"gateway status={status} pid={pid if pid else '-'} alive={alive}",
        "gateway requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True),
    ]
    if heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={age:.1f}")
    return lines


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
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_run(args) -> int:
    """内部命令：前台运行 gateway 后台循环。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    for path in (paths.inbox, paths.processing, paths.done, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    requeued = requeue_gateway_processing_requests(paths)
    pid = os.getpid()
    paths.pid.write_text(str(pid), encoding="utf-8")
    try:
        options = _resolve_daemon_options(agent, args)
    except ValueError as exc:
        write_json_file(paths.state, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
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
    except KeyboardInterrupt:
        write_json_file(paths.state, {"status": "interrupted", "pid": pid, "stopped_at": time.time()})
        exit_code = 130
    except Exception as exc:
        write_json_file(paths.state, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
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
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_stop_timeout
    if wait_for_pid_exit(pid, timeout):
        print(f"gateway stopped pid={pid}")
        return 0
    if args.kill:
        terminate_pid(pid)
        if wait_for_pid_exit(pid, 5):
            write_json_file(paths.state, {"status": "killed", "pid": pid, "stopped_at": time.time()})
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
    pid, alive = gateway_running(paths)
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


def _gateway_request_loop(args, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """后台处理 gateway inbox 请求。

    这个 worker 和 dispatch watch 在同一个 gateway 进程里并行：
    - dispatch watch 负责定时巡检任务树。
    - request worker 负责响应用户/客户端即时消息。

    第一版只串行处理请求，先保证正确落盘和可恢复；真正并发 worker pool 后续再接。
    """

    try:
        agent = make_agent(args)
    except Exception as exc:
        print(f"gateway request worker failed to initialize: {exc}", file=sys.stderr)
        return

    poll_interval = max(1, int(agent.config.gateway_request_poll_interval))
    while not stop_event.is_set():
        try:
            processed = _process_gateway_requests(agent, paths)
        except Exception as exc:
            print(f"gateway request worker failed: {exc}", file=sys.stderr)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)


def _process_gateway_requests(agent: SimpleAgent, paths: GatewayPaths) -> int:
    """处理当前所有待处理 gateway 请求。

    文件流转：
    pending -> processing -> responses + done

    用 rename/move 表达状态变化，方便人直接看目录也能知道请求卡在哪一步。
    """

    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)
    processed = 0
    for request_path in sorted(paths.inbox.glob("*.json")):
        processing_path = paths.processing / request_path.name
        try:
            request_path.replace(processing_path)
        except OSError:
            continue
        response = _handle_gateway_request(agent, processing_path)
        response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
        write_json_file(response_path, response)
        append_gateway_history(paths, response)
        try:
            processing_path.replace(paths.done / processing_path.name)
        except OSError:
            pass
        processed += 1
    return processed


def _handle_gateway_request(agent: SimpleAgent, request_path: Path) -> dict:
    """执行单条 gateway 请求，并返回响应 payload。

    目前只支持 `kind=ask`，也就是“一条用户消息 -> 一次完整 agent.run()”。
    后续可以继续扩展：
    - kind=create_subagents
    - kind=dispatch_once
    - kind=resume_task
    - kind=external_chat_message
    """

    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip()
    started_at = time.time()
    response = {
        "id": request_id,
        "kind": kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
    }
    try:
        if kind != "ask":
            raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
        prompt = str(request.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("gateway ask prompt 不能为空。")
        result = agent.run(
            prompt,
            inject=[str(item) for item in request.get("inject", [])],
            prompt_files=[str(item) for item in request.get("prompt_files", [])],
            save=bool(request.get("save", True)),
        )
        response.update(
            {
                "ok": True,
                "status": "done",
                "response": result.response,
                "backend": result.backend,
                "used_memories": result.used_memories,
                "tool_rounds": result.tool_rounds,
                "prompt": result.prompt if request.get("include_prompt") else "",
            }
        )
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    ended_at = time.time()
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - started_at, 3)
    return response


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
        _, alive = gateway_running(paths)
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

    def bottom_toolbar() -> str:
        with state_lock:
            active_count = pending_jobs + (1 if is_running else 0)
            elapsed = time.perf_counter() - running_started_at if is_running else 0
        if not active_count:
            return ""
        if is_running:
            return f"思考中... {elapsed:.0f}s | 队列 {pending_jobs}"
        return f"等待处理 | 队列 {pending_jobs}"

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
    sub = parser.add_subparsers(dest="command", required=True)

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
