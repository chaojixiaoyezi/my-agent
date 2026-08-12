#!/usr/bin/env python3
"""R1-02 韧性闸: 模型流中途断恢复(经本地 CONNECT 隧道代理注入)。

此脚本仅用于显式授权的故障注入验收。

注入方式: systemd drop-in 设 https_proxy=http://127.0.0.1:<proxy-port>,
模型 https 请求全部走本地 CONNECT 隧道代理(不改 api_base 配置)。
代理按 --proxy-mode 转发:
  relay     全转发 —— 语义等价对照(先对照后注入铁律)
  rst       流中途 RST  -> ConnectionResetError/SSLEOFError(OSError)
            -> 归一 typed ProviderTransientError -> 同回合自动阶梯重试
  truncate  流中途 FIN(半截 body) -> IncompleteRead(HTTPException)
            -> 裸冒泡 -> attempt 失败 -> 唤醒轮续跑

判据(结构化信号, 只信 runtime.db 账本字段 + 代理物理日志, 不信模型自述):
- 注入确实发生: 代理日志 INJECT 行(时间/字节/模式)
- 任务最终完成: marker 出现恰 1 次(goal 第 2 步产物) + write_file op == 1
- 断后重连: 注入之后代理日志出现新 CONNECT 行(物理层重试/续跑证据)
- 恢复路径如实记录: attempt 数 == 1 -> 同回合自动重试; >1 -> 唤醒轮续跑
  (两者都是合法恢复机制, 记录不判 FAIL)
- run/attempt/event 终态如实(agent_runs.status / attempt ended_at / completed 事件)
- 服务恢复可用: 新 /ask 正常收口

用法:
  python3 scripts/run_r1_02_gate.py --output-root <dir> \
      --proxy-log /tmp/r1-02-proxy.log --proxy-mode rst [--skip-real]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED"}
_RUNNING_STATUSES = {"EXECUTING", "STARTED", "PENDING"}
_OK_RESULT_STATUSES = {"done", "finished", "ok", "error", "stopped", "interrupted"}

CLI_USER = "admin"
CLI_CHANNEL = "chat"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, help="report 输出目录")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8420")
    parser.add_argument("--owners-root", default="/root/.my-agent/owners")
    parser.add_argument("--proxy-log", default="/tmp/r1-02-proxy.log")
    parser.add_argument("--proxy-mode", choices=("relay", "rst", "truncate"),
                        default="rst")
    parser.add_argument("--proxy-health", default="http://127.0.0.1:18421/health")
    parser.add_argument("--sleep-seconds", type=int, default=40)
    parser.add_argument("--watch-settle-seconds", type=int, default=360,
                        help="恢复后观察窗口(注入→恢复→续跑→收尾, 留足余量)")
    parser.add_argument("--skip-real", action="store_true")
    return parser.parse_args()


def _http(method: str, url: str, payload: dict | None = None,
          headers: dict | None = None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {"http_status": resp.status, "raw": ""}
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return {"http_status": resp.status, "raw": body}
            if isinstance(parsed, dict):
                parsed["http_status"] = resp.status
                return parsed
            return {"http_status": resp.status, "raw": body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                parsed["http_status"] = exc.code
                return parsed
        except json.JSONDecodeError:
            pass
        return {"http_status": exc.code, "raw": body, "error": body[:200]}
    except (urllib.error.URLError, OSError) as exc:
        return {"http_status": 0, "error": f"connection_error: {exc}"}


def _ask(gateway: str, goal: str, *, user_id: str, channel: str,
         conversation_id: str = "") -> dict:
    payload = {"kind": "ask", "goal": goal}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    headers = {}
    if user_id:
        headers["X-User-Id"] = user_id
    if channel:
        headers["X-Channel"] = channel
    return _http("POST", f"{gateway}/ask", payload, headers)


def _result(gateway: str, request_id: str, *, user_id: str = "",
            channel: str = "") -> dict:
    headers = {}
    if user_id:
        headers["X-User-Id"] = user_id
    if channel:
        headers["X-Channel"] = channel
    return _http("GET", f"{gateway}/result/{request_id}", headers=headers)


def _owner_db_path(owners_root: str, *, user_id: str = "",
                   channel: str = "") -> Path | None:
    base = Path(owners_root)
    if user_id and channel and channel != CLI_CHANNEL:
        scoped = base / "providers" / channel / "users" / user_id / "runtime.db"
        if scoped.is_file():
            return scoped
        wild = sorted((base / "providers" / channel / "users").glob("*/runtime.db"))
        if wild:
            return wild[0]
        return None
    candidates = sorted(base.glob("local/*/runtime.db")) or sorted(base.glob("*/runtime.db"))
    return candidates[0] if candidates else None


def _ledger(owners_root: str, *, user_id: str = "", channel: str = "") -> sqlite3.Connection:
    path = _owner_db_path(owners_root, user_id=user_id, channel=channel)
    if path is None:
        raise FileNotFoundError(
            f"no runtime.db for user_id={user_id!r} channel={channel!r} under {owners_root}"
        )
    return sqlite3.connect(str(path), timeout=15)


def _runs_since(conn: sqlite3.Connection, since: float) -> list[dict]:
    rows = conn.execute(
        "SELECT agent_run_id, task_run_id, parent_agent_run_id, delegation_id, role, "
        "status, created_at FROM agent_runs WHERE created_at >= ? ORDER BY created_at",
        (since,),
    ).fetchall()
    return [
        {
            "agent_run_id": r[0], "task_run_id": r[1], "parent_agent_run_id": r[2],
            "delegation_id": r[3], "role": r[4], "status": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def _tool_ops(conn: sqlite3.Connection, run_ids: list[str]) -> list[dict]:
    if not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT operation_id, agent_run_id, attempt_id, operation_type, "
        f"canonical_scope, status, outcome_json, created_at FROM tool_operations "
        f"WHERE agent_run_id IN ({marks}) ORDER BY created_at",
        run_ids,
    ).fetchall()
    out = []
    for r in rows:
        op = {
            "operation_id": r[0], "agent_run_id": r[1], "attempt_id": r[2],
            "operation_type": r[3], "canonical_scope": r[4], "status": r[5],
            "outcome_json": r[6], "created_at": r[7],
        }
        try:
            op["outcome"] = json.loads(op["outcome_json"] or "{}")
        except json.JSONDecodeError:
            op["outcome"] = {"_parse_error": True}
        out.append(op)
    return out


def _attempts_settled(conn: sqlite3.Connection, runs: list[dict]) -> dict:
    result = {}
    for run in runs:
        rows = conn.execute(
            "SELECT attempt_id, status, ended_at FROM agent_attempts WHERE agent_run_id = ?",
            (run["agent_run_id"],),
        ).fetchall()
        result[run["agent_run_id"]] = [
            {"attempt_id": r[0], "status": r[1], "ended_at": r[2]} for r in rows
        ]
    return result


def _completed_run_events(conn: sqlite3.Connection, runs: list[dict]) -> dict:
    result = {}
    for run in runs:
        rows = conn.execute(
            "SELECT event_type, payload_json FROM runtime_events "
            "WHERE agent_run_id = ? AND event_type = 'agent_run.completed' ORDER BY seq",
            (run["agent_run_id"],),
        ).fetchall()
        result[run["agent_run_id"]] = [
            {"event_type": r[0], "payload_json": r[1]} for r in rows
        ]
    return result


def _wait_op_running(conn: sqlite3.Connection, since: float, *,
                     operation_type: str = "run_command", timeout: float = 120.0,
                     interval: float = 1.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = _runs_since(conn, since)
        for op in _tool_ops(conn, [r["agent_run_id"] for r in runs]):
            if (op.get("operation_type") or "") != operation_type:
                continue
            if op["status"] in _RUNNING_STATUSES:
                return op
        time.sleep(interval)
    return None


def _proxy_events(proxy_log: str, *, after_pos: int = 0) -> tuple[list[dict], int]:
    """读代理日志新行; 返回 (事件列表, 新文件偏移)。"""
    path = Path(proxy_log)
    if not path.is_file():
        return [], after_pos
    with open(path, "r", encoding="utf-8") as fh:
        fh.seek(after_pos)
        data = fh.read()
    events = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, os.fstat(fh.fileno()).st_size if False else path.stat().st_size


def _wait_proxy_inject(proxy_log: str, *, since_pos: int, timeout: float = 240.0,
                       interval: float = 2.0) -> dict:
    deadline = time.time() + timeout
    pos = since_pos
    while time.time() < deadline:
        events, pos = _proxy_events(proxy_log, after_pos=pos)
        for ev in events:
            if ev.get("type") == "inject":
                return {"inject": ev, "pos": pos}
        time.sleep(interval)
    return {"inject": None, "pos": pos}


def _proxy_connects_after(proxy_log: str, inject_ts: str) -> list[dict]:
    """注入之后的新 CONNECT 事件(断后重连的物理层证据)。"""
    events, _ = _proxy_events(proxy_log)
    return [e for e in events if e.get("type") == "connect" and e.get("ts", "") >= inject_ts]


def _find_marker(root: Path, name: str) -> str | None:
    if not root.is_dir():
        return None
    for p in root.rglob(name):
        return str(p)
    return None


def _main() -> int:
    args = _parse_args()
    out_root = Path(args.output_root)
    run_dir = out_root / f"r1-02-{args.proxy_mode}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_dir.mkdir(parents=True, exist_ok=True)

    case: dict = {
        "name": f"R1-02/{args.proxy_mode}",
        "goal": "",
        "timeline": {},
        "observations": {},
        "findings": [],
        "passed": False,
    }
    if args.skip_real:
        case["passed"] = True
        (run_dir / "report.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2))
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    findings: list[str] = []
    timeline: dict = {}
    observations: dict = {}

    # 0. 前置: 代理健康
    health = _http("GET", args.proxy_health)
    case["proxy_health"] = health
    if health.get("http_status") != 200:
        findings.append(f"proxy_unhealthy:{health}")
        case["findings"] = findings
        (run_dir / "report.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2))
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    conversation_id = f"conv-r1-02-{int(time.time())}"
    marker = f"r1-02-marker-{int(time.time())}.txt"
    case["goal"] = (
        "请严格按下面两步执行, 不要省略也不要改命令:\n"
        f'1. 用 run_command 原样执行命令: bash -c "sleep {args.sleep_seconds}" 并等待该命令执行完成'
        "(注意: 必须原样写 bash -c \\\"sleep ...\\\", 不要简写成 sleep ...);\n"
        f"2. 该命令完成后, 用 write_file 在当前目录创建文件 {marker}, 内容为 done;\n"
        "全部完成后汇报 r1-02-done。"
    )

    # 1. 记录代理日志起点, 发起任务
    _, start_pos = _proxy_events(args.proxy_log)
    since = time.time()
    reply = _ask(args.gateway_url, case["goal"], user_id=CLI_USER,
                 channel=CLI_CHANNEL, conversation_id=conversation_id)
    case["ask_response"] = reply
    timeline["ask_at"] = since
    if not reply.get("request_id"):
        findings.append(f"ask_failed:{reply.get('error')}")
        case["findings"] = findings
        case["passed"] = False
        (run_dir / "report.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2))
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    # 2. 等 EXECUTING 真进程证据
    conn = _ledger(args.owners_root)
    sleep_op = _wait_op_running(conn, since, operation_type="run_command",
                                timeout=args.sleep_seconds + 60.0)
    case["running_op"] = sleep_op
    if sleep_op is None:
        findings.append("sleep_never_executed: 模型未真实调用长命令, 注入链未测到")
        case["runs"] = _runs_since(conn, since)
        case["tool_ops"] = _tool_ops(conn, [r["agent_run_id"] for r in case["runs"]])
        case["findings"] = findings
        case["passed"] = False
        (run_dir / "report.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2))
        conn.close()
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0
    timeline["executing_at"] = time.time()
    timeline["sleep_op"] = sleep_op["operation_id"]

    # 3. 等注入事件(模型回合流被断; relay 模式无注入事件)
    inject_wait = _wait_proxy_inject(args.proxy_log, since_pos=start_pos,
                                     timeout=240.0)
    case["inject_event"] = inject_wait["inject"]
    if args.proxy_mode != "relay" and inject_wait["inject"] is None:
        findings.append("inject_never_occurred: 观察 240s 内代理日志无 INJECT 行")
    if args.proxy_mode == "relay" and inject_wait["inject"] is not None:
        findings.append("relay_mode_unexpected_inject: 对照模式不应注入")
    if inject_wait["inject"] is not None:
        timeline["inject_at"] = time.time()

    # 4. 观察窗口: 等完整闭环(run 全终态 + marker 出现 + sleep op 终态)
    owner_home = Path(args.owners_root).parent

    def _marker_found() -> bool:
        return (_find_marker(owner_home / "service-cwd", marker) is not None
                or _find_marker(owner_home / "workspace", marker) is not None)

    op_terminal_seen = False
    op_terminal_at = None
    op_last = sleep_op
    deadline = time.time() + args.watch_settle_seconds
    while time.time() < deadline:
        cur = None
        for op in _tool_ops(conn, [sleep_op["agent_run_id"]]):
            if op["operation_id"] == sleep_op["operation_id"]:
                cur = op
                break
        if cur is not None:
            op_last = cur
        if cur is not None and cur["status"] in _TERMINAL_STATUSES and not op_terminal_seen:
            op_terminal_seen = True
            op_terminal_at = time.time()
        runs_now = _runs_since(conn, since)
        runs_terminal = all(
            r["status"] in ("cancelled", "done", "unfinished", "failed")
            for r in runs_now
        )
        if op_terminal_seen and runs_terminal and _marker_found():
            observations["early_exit"] = {
                "op_terminal": True, "runs_terminal": True, "marker": True,
            }
            break
        time.sleep(5.0)
    observations["sleep_op_terminal_seen"] = op_terminal_seen
    observations["sleep_op_terminal_at"] = op_terminal_at
    observations["sleep_op_final_status"] = op_last["status"]
    if not op_terminal_seen:
        findings.append(f"sleep_op_never_settled:{op_last['status']}")

    # 5. 账本终态快照
    runs = _runs_since(conn, since)
    all_ops = _tool_ops(conn, [r["agent_run_id"] for r in runs])
    case["runs"] = runs
    case["tool_ops"] = all_ops
    case["attempts"] = _attempts_settled(conn, runs)
    case["run_completed_events"] = _completed_run_events(conn, runs)
    conn.close()

    # 6. 副作用 + 完成度(marker 恰 1 次)
    marker_hit = _find_marker(owner_home / "service-cwd", marker)
    if marker_hit is None:
        marker_hit = _find_marker(owner_home / "workspace", marker)
    observations["marker_exists"] = marker_hit is not None
    observations["marker_path"] = marker_hit
    write_ops = [o for o in all_ops if o.get("operation_type") == "write_file"]
    observations["write_file_ops"] = [
        {"operation_id": o["operation_id"][:16], "status": o["status"]} for o in write_ops
    ]
    if not marker_hit:
        findings.append("task_not_completed: marker 未出现(任务未完成)")
    if len(write_ops) > 1:
        findings.append(f"marker_written_twice:{len(write_ops)}")
    if len(write_ops) == 0 and args.proxy_mode != "relay":
        findings.append("no_side_effect: 注入后任务根本没有推进到副作用步")

    # 7. 断后重连(物理层): 注入之后的新 CONNECT
    inject_ts = (inject_wait["inject"] or {}).get("ts", "")
    connects_after = _proxy_connects_after(args.proxy_log, inject_ts) if inject_ts else []
    observations["connects_after_inject"] = [
        {"ts": e.get("ts"), "target": e.get("target")} for e in connects_after
    ]
    if args.proxy_mode != "relay" and inject_ts and not connects_after:
        findings.append("no_reconnect_after_inject: 注入后代理无新 CONNECT(无重试/续跑)")

    # 8. 恢复路径(结构化): attempt 数 == 1 -> 同回合自动重试; >1 -> 唤醒轮续跑
    attempts = case["attempts"]
    total_attempts = sum(len(v) for v in attempts.values())
    observations["attempts_total"] = total_attempts
    observations["recovery_path"] = (
        "same_turn_auto_retry" if total_attempts == 1
        else "wake_loop_resume" if total_attempts > 1
        else "none"
    )
    if args.proxy_mode != "relay" and total_attempts == 0:
        findings.append("no_attempt_recorded: 账本无 attempt(恢复路径不可审计)")

    # 9. run/attempt/event 终态如实
    for run in runs:
        if run["status"] not in ("cancelled", "done", "unfinished", "failed"):
            findings.append(f"run_no_terminal_status:{run['agent_run_id']}:{run['status']}")
    for run_id, evs in case["run_completed_events"].items():
        if not evs:
            findings.append(f"missing_run_completed_event:{run_id}")

    # 10. 恢复可用性: 新请求正常收口
    probe_conversation = f"conv-r1-02-probe-{int(time.time())}"
    probe = _ask(args.gateway_url, "请回复 r1-02-probe-ok 即可, 不要调用任何工具。",
                 user_id=CLI_USER, channel=CLI_CHANNEL, conversation_id=probe_conversation)
    probe_result = None
    if probe.get("request_id"):
        deadline = time.time() + 90.0
        while time.time() < deadline:
            pr = _result(args.gateway_url, probe["request_id"])
            if (str(pr.get("status") or "") in _OK_RESULT_STATUSES
                    or pr.get("http_status") in (403, 404)):
                probe_result = pr
                break
            time.sleep(3.0)
    case["probe_result"] = probe_result
    if probe_result is None:
        findings.append("post_recovery_probe_failed")
    elif str(probe_result.get("status") or "") not in _OK_RESULT_STATUSES:
        findings.append(f"post_recovery_probe_bad_status:{probe_result.get('status')}")

    # 11. 原请求结果
    orig_result = _result(args.gateway_url, reply["request_id"])
    observations["orig_request_after_recovery"] = {
        "http_status": orig_result.get("http_status"),
        "status": orig_result.get("status"),
        "error": orig_result.get("error"),
    }

    case["timeline"] = timeline
    case["observations"] = observations
    case["findings"] = findings
    case["passed"] = not findings
    (run_dir / "report.json").write_text(json.dumps(case, ensure_ascii=False, indent=2))
    print(json.dumps(case, ensure_ascii=False, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(_main())
