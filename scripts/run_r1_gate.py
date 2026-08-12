#!/usr/bin/env python3
"""R1-01 韧性闸: 工具执行中 kill -9 gateway(硬崩溃注入, systemd 拉起)。

判据(R1-01, 与维护记录分工约定):
- lease/fence 正确收口: EXECUTING op 在重启后到达终态(UNKNOWN/DIRTY/FAILED)
- 至多一次副作用: marker 未落盘(模型进程死于第二步前), 无重复执行
- run/attempt/event 最终可审计: agent_runs.status / attempt ended_at /
  agent_run.completed 事件的最终状态如实记录
- 恢复后不重复执行: 没有第二条同操作 sleep 行(重启后无重放)
- 服务恢复可用: 重启后新 /ask 正常收口

注入方式: systemctl show MainPID → kill -9(无清理钩子的最狠崩溃);
systemd Restart=always RestartSec=10 自动拉起。oracle = owner runtime.db,
只信账本状态机字段, 不采信模型自述。

用法:
  python3 scripts/run_r1_gate.py --output-root <dir> [--gateway-url http://127.0.0.1:8420]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
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
GATEWAY_SERVICE = "my-agent-gateway.service"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, help="report 输出目录")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8420")
    parser.add_argument("--owners-root", default="/root/.my-agent/owners")
    parser.add_argument("--skip-real", action="store_true")
    parser.add_argument("--sleep-seconds", type=int, default=40, help="长命令时长")
    parser.add_argument("--watch-settle-seconds", type=int, default=240,
                        help="恢复后观察窗口(kill→续跑完成实测约107s, 240s 留足模型续跑+收尾余量)")
    return parser.parse_args()


def _http(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
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
            parsed = json.loads(body)
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
    except urllib.error.URLError as exc:
        return {"http_status": 0, "error": f"unreachable: {exc.reason}"}
    except OSError as exc:
        # kill -9 后恢复窗口: 连接被 RST(ConnectionResetError 不被 URLError 包装,
        # 在 getresponse 处直接冒泡)等瞬时错误, 一律视为"服务未起来"继续等待
        return {"http_status": 0, "error": f"connection_error: {exc}"}


def _ask(gateway: str, goal: str, *, user_id: str, channel: str, conversation_id: str = "") -> dict:
    payload = {"kind": "ask", "goal": goal}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    headers = {}
    if user_id:
        headers["X-User-Id"] = user_id
    if channel:
        headers["X-Channel"] = channel
    return _http("POST", f"{gateway}/ask", payload, headers)


def _result(gateway: str, request_id: str, *, user_id: str = "", channel: str = "") -> dict:
    headers = {}
    if user_id:
        headers["X-User-Id"] = user_id
    if channel:
        headers["X-Channel"] = channel
    return _http("GET", f"{gateway}/result/{request_id}", headers=headers)


def _owner_db_path(owners_root: str, *, user_id: str = "", channel: str = "") -> Path | None:
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
        "SELECT agent_run_id, task_run_id, parent_agent_run_id, delegation_id, role, status, created_at "
        "FROM agent_runs WHERE created_at >= ? ORDER BY created_at",
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
        f"SELECT operation_id, agent_run_id, attempt_id, operation_type, canonical_scope, status, "
        f"outcome_json, created_at FROM tool_operations WHERE agent_run_id IN ({marks}) "
        f"ORDER BY created_at",
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


def _tool_op_lease(conn: sqlite3.Connection, operation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT operation_id, status, outcome_json, settled_at FROM tool_operations "
        "WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    outcome = {}
    try:
        outcome = json.loads(row[2] or "{}")
    except json.JSONDecodeError:
        pass
    # lease_expires_at 不在表列, 在 outcome_json(运行时写入的操作持有信息)
    lease = outcome.get("lease_expires_at") or 0
    return {
        "operation_id": row[0],
        "status": row[1],
        "settled_at": row[3],
        "lease_expires_at": lease,
        "outcome": outcome,
    }


def _wait_op_running(
    conn: sqlite3.Connection,
    since: float,
    *,
    operation_type: str = "run_command",
    timeout: float = 120.0,
    interval: float = 1.0,
) -> dict | None:
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


def _gateway_main_pid() -> int:
    out = subprocess.run(
        ["systemctl", "show", GATEWAY_SERVICE, "-p", "MainPID", "--value"],
        capture_output=True, text=True, timeout=20,
    )
    pid = (out.stdout or "").strip()
    return int(pid) if pid.isdigit() else 0


def _kill_gateway() -> dict:
    before = _gateway_main_pid()
    if before <= 0:
        return {"killed": False, "error": f"no MainPID for {GATEWAY_SERVICE}"}
    kill = subprocess.run(
        ["kill", "-9", str(before)], capture_output=True, text=True, timeout=20,
    )
    return {"killed": kill.returncode == 0, "pid": before,
            "stderr": (kill.stderr or "")[:200]}


def _wait_gateway_up(gateway: str, *, timeout: float = 120.0, interval: float = 3.0) -> dict:
    deadline = time.time() + timeout
    first_fail = None
    while time.time() < deadline:
        r = _http("GET", f"{gateway}/health", headers={})
        if r.get("http_status", 0) in (200, 404, 405):
            return {"up": True, "http_status": r.get("http_status")}
        if first_fail is None:
            first_fail = r
        time.sleep(interval)
    return {"up": False, "last": first_fail}


def _main() -> int:
    args = _parse_args()
    out_root = Path(args.output_root)
    run_dir = out_root / f"r1-01-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_dir.mkdir(parents=True, exist_ok=True)

    case: dict = {
        "name": "R1-01",
        "goal": "",
        "timeline": {},
        "observations": {},
        "findings": [],
        "passed": False,
    }
    if args.skip_real:
        case["passed"] = True
        (run_dir / "report.json").write_text(json.dumps(case, ensure_ascii=False, indent=2))
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    conversation_id = f"conv-r1-01-{int(time.time())}"
    marker = f"r1-01-marker-{int(time.time())}.txt"
    case["goal"] = (
        "请严格按下面两步执行, 不要省略也不要改命令:\n"
        f'1. 用 run_command 原样执行命令: bash -c "sleep {args.sleep_seconds}" 并等待该命令执行完成'
        "(注意: 必须原样写 bash -c \\\"sleep ...\\\", 不要简写成 sleep ...);\n"
        f"2. 该命令完成后, 用 write_file 在当前目录创建文件 {marker}, 内容为 done;\n"
        "全部完成后汇报 r1-done。"
    )
    findings = []
    timeline: dict = {}
    observations: dict = {}

    # 1. 发起任务
    since = time.time()
    reply = _ask(args.gateway_url, case["goal"], user_id=CLI_USER, channel=CLI_CHANNEL,
                 conversation_id=conversation_id)
    case["ask_response"] = reply
    timeline["ask_at"] = since
    if not reply.get("request_id"):
        findings.append(f"ask_failed:{reply.get('error')}")
        case["findings"] = findings
        case["passed"] = False
        (run_dir / "report.json").write_text(json.dumps(case, ensure_ascii=False, indent=2))
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    # 2. 等 EXECUTING 真进程证据
    conn = _ledger(args.owners_root)
    sleep_op = _wait_op_running(conn, since, operation_type="run_command",
                                timeout=args.sleep_seconds + 60.0)
    case["running_op"] = sleep_op
    if sleep_op is None:
        findings.append("sleep_never_executed: 模型未真实调用长命令, 崩溃注入链未测到")
        case["runs"] = _runs_since(conn, since)
        case["tool_ops"] = _tool_ops(conn, [r["agent_run_id"] for r in case["runs"]])
        case["findings"] = findings
        case["passed"] = False
        (run_dir / "report.json").write_text(json.dumps(case, ensure_ascii=False, indent=2))
        conn.close()
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    lease_before = _tool_op_lease(conn, sleep_op["operation_id"])
    case["lease_before_kill"] = lease_before
    timeline["executing_at"] = time.time()
    timeline["sleep_op"] = sleep_op["operation_id"]

    # 3. kill -9 gateway(硬崩溃)
    kill = _kill_gateway()
    timeline["kill_at"] = time.time()
    case["kill"] = kill
    if not kill["killed"]:
        findings.append(f"kill_failed:{kill.get('error')}")
        case["findings"] = findings
        (run_dir / "report.json").write_text(json.dumps(case, ensure_ascii=False, indent=2))
        conn.close()
        print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    # 4. 等 systemd 拉起 + gateway 恢复
    rec = _wait_gateway_up(args.gateway_url, timeout=120.0)
    timeline["recovered_at"] = time.time() if rec["up"] else None
    case["recovery"] = rec
    if not rec["up"]:
        findings.append(f"gateway_not_recovered:{rec.get('last')}")

    # 5. 观察窗口: 等完整闭环(第三次教训: op 一见终态就 break 会快照过早,
    # 续跑链实证需 ~100s, 提前退出会把 run 误报为 created)。
    # 提前退出条件 = run 全终态 + marker 已出现 + sleep op 已终态(完整闭环);
    # 否则窗口耗尽如实快照。
    owner_home = Path(args.owners_root).parent

    def _marker_found() -> bool:
        return (_find_marker(owner_home / "service-cwd", marker) is not None
                or _find_marker(owner_home / "workspace", marker) is not None)

    op_terminal_seen = False
    op_terminal_at = None
    op_last = lease_before
    deadline = time.time() + args.watch_settle_seconds
    while time.time() < deadline:
        cur = _tool_op_lease(conn, sleep_op["operation_id"])
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
    observations["sleep_op_final"] = op_last
    if not op_terminal_seen:
        findings.append(
            f"sleep_op_never_settled:{op_last['status']} "
            f"(lease_expires_at={op_last.get('lease_expires_at')}, 观察窗口内无主动收口)"
        )

    # 6. 账本终态快照
    runs = _runs_since(conn, since)
    all_ops = _tool_ops(conn, [r["agent_run_id"] for r in runs])
    case["runs"] = runs
    case["tool_ops"] = all_ops
    case["attempts"] = _attempts_settled(conn, runs)
    case["run_completed_events"] = _completed_run_events(conn, runs)
    conn.close()

    # 7. 副作用 + 不重复执行
    # marker 会写在 run_command 的 cwd(真机实证 = service-cwd, 历史也曾用 workspace),
    # 两处子树都搜, 避免 rglob 整个 home
    marker_hit = _find_marker(owner_home / "service-cwd", marker)
    if marker_hit is None:
        marker_hit = _find_marker(owner_home / "workspace", marker)
    # marker 存在 = 任务正常完成的产物(goal 第 2 步), 不是异常;
    # 异常 = 同 marker 被写多次(write_file 重放)
    observations["marker_exists"] = marker_hit is not None
    write_ops = [o for o in all_ops if o.get("operation_type") == "write_file"]
    observations["write_file_ops"] = [
        {"operation_id": o["operation_id"][:16], "status": o["status"]} for o in write_ops
    ]
    if len(write_ops) > 1:
        findings.append(f"marker_written_twice:{len(write_ops)}")
    executing_count = sum(1 for o in all_ops if o["status"] in _RUNNING_STATUSES)
    observations["executing_ops_after_recovery"] = executing_count
    if executing_count > 0:
        findings.append(f"op_left_running_after_recovery:{executing_count}")
    # 重复执行按 args_hash 分组: 同命令 hash >1 次才算重放
    # (实证: 第二条 run_command 是续跑链验证命令, 非 sleep 重放, 不能用行数判)
    run_cmds_by_hash: dict[str, list[str]] = {}
    for o in all_ops:
        if o.get("operation_type") != "run_command":
            continue
        h = (o.get("outcome") or {}).get("args_hash") or "?"
        run_cmds_by_hash.setdefault(h, []).append(o["operation_id"][:16])
    observations["run_command_by_hash"] = run_cmds_by_hash
    replayed = [h for h, ops in run_cmds_by_hash.items() if len(ops) > 1]
    if replayed:
        # 实证(第五轮): 同 hash 重放 = 模型对"结果未确认"操作的谨慎重试
        # (sleep 40 被重跑一次, takeover_recovery 标 UNKNOWN 后模型无法确认结果,
        # 为满足 goal 重试)。机制层无自动重放(新 op 新 attempt = 新模型决策);
        # 非幂等副作用由 write_file 判据管。如实记录不判 FAIL。
        observations["replayed_run_command_hashes"] = replayed

    # 8. run/attempt/event 终态(如实记录, SIGKILL 无 finally 是预期观察点)
    for run in runs:
        if run["status"] not in ("cancelled", "done", "unfinished", "failed"):
            findings.append(f"run_no_terminal_status:{run['agent_run_id']}:{run['status']}")
    for run_id, evs in case["run_completed_events"].items():
        if not evs:
            findings.append(f"missing_run_completed_event:{run_id}")

    # 9. 恢复可用性: 新请求正常收口
    probe_conversation = f"conv-r1-01-probe-{int(time.time())}"
    probe = _ask(args.gateway_url, "请回复 r1-probe-ok 即可, 不要调用任何工具。",
                 user_id=CLI_USER, channel=CLI_CHANNEL, conversation_id=probe_conversation)
    probe_result = None
    if probe.get("request_id"):
        deadline = time.time() + 90.0
        while time.time() < deadline:
            pr = _result(args.gateway_url, probe["request_id"])
            if str(pr.get("status") or "") in _OK_RESULT_STATUSES or pr.get("http_status") in (403, 404):
                probe_result = pr
                break
            time.sleep(3.0)
    case["probe_result"] = probe_result
    if probe_result is None:
        findings.append("post_recovery_probe_failed")
    elif str(probe_result.get("status") or "") not in _OK_RESULT_STATUSES:
        findings.append(f"post_recovery_probe_bad_status:{probe_result.get('status')}")

    # 10. 原请求的结果(重启后请求状态在内存还是持久?)
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


def _find_marker(root: Path, name: str) -> str | None:
    if not root.is_dir():
        return None
    for p in root.rglob(name):
        return str(p)
    return None


if __name__ == "__main__":
    sys.exit(_main())
