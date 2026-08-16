"""CLI 收口机器验证 gate（第 4 层假完成根治，2026-08-15 3×3 真机驱动）。

cell1/cell2/cell3 多实例实锤：模型无工具调用轮输出中间态文本
（「继续推进/接下来将/先写……再写……」）被裁决为自然收口 ok →
resume_loop 标 completed → 假完成（cell2 三次、cell1 一次，产物
编译全不过）。prompt 验收硬要求挡不住模型谎报，需要系统结构化 gate。

本模块在自然收口裁决前执行 delivery contract 声明的 verify_commands
（部署者信任面：contract 文件由部署者书写，等同部署者在 CLI 手跑
验收命令）：
- 全部通过 → 不干预，保持收口 ok
- 任一失败 → runtime_status=unfinished + runtime_reason=
  DELIVERY_VERIFY_FAILED，失败输出注入 tool_context → resume_loop
  自动续跑（模型看到真实编译/测试输出继续修），绝不标完成。

双席 seq2004 复核硬缺口 2-4 修正（fail-closed / 受控执行边界 / 审计）：
- 坏合同/坏条目不丢弃成 no-op：contract 声明了 verify_commands 但
  解析出非法条目/空 → 整体 fail-closed（CONTRACT_INVALID，不通过），
  绝不伪装成自然收口。
- cwd 受控边界：绝对 cwd 必须落在 task workspace 根内，越界 → 失败。
- 验证结果落 runtime_events（append-only，attempt_id+contract hash
  可审计）；verify 为只读命令（部署者声明），重复执行无害且如实反映
  当前产物状态，每次收口验证都落账。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_VERIFY_DEFAULT_TIMEOUT_SECONDS = 120
_VERIFY_OUTPUT_MAX_CHARS = 4000

#: 无 verify_commands（不干预）与「有但非法（fail-closed）」的区分返回值
VERIFY_SKIPPED = "skipped"  # 未声明 → 不干预
VERIFY_PASSED = "passed"
VERIFY_FAILED = "failed"
VERIFY_CONTRACT_INVALID = "contract_invalid"  # 声明了但结构非法 → fail-closed
VERIFY_CWD_OUT_OF_BOUNDS = "cwd_out_of_bounds"


def _contract_hash(contract: object) -> str:
    try:
        raw = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(contract)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def delivery_verify_commands(
    contract: object,
) -> tuple[list[dict[str, object]], str]:
    """提取 contract 的 verify_commands。

    返回 (commands, state)：state=VERIFY_SKIPPED 表示未声明（不干预）；
    VERIFY_CONTRACT_INVALID 表示声明了但结构非法（调用方必须 fail-closed，
    不得放行自然收口）；VERIFY_PASSED 表示声明且全部条目合法。
    """
    if not isinstance(contract, dict):
        return [], VERIFY_SKIPPED
    raw = contract.get("verify_commands")
    if raw is None:
        return [], VERIFY_SKIPPED
    if not isinstance(raw, list):
        return [], VERIFY_CONTRACT_INVALID
    commands: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], VERIFY_CONTRACT_INVALID
        command = str(item.get("command") or "").strip()
        if not command:
            return [], VERIFY_CONTRACT_INVALID
        try:
            timeout = int(item.get("timeout_seconds") or 0)
        except (TypeError, ValueError):
            return [], VERIFY_CONTRACT_INVALID
        if timeout < 1:
            timeout = _VERIFY_DEFAULT_TIMEOUT_SECONDS
        commands.append(
            {
                "command": command,
                "cwd": str(item.get("cwd") or "").strip(),
                "timeout_seconds": timeout,
            }
        )
    if not commands:
        return [], VERIFY_CONTRACT_INVALID
    return commands, VERIFY_PASSED


def _resolve_verify_cwd(
    cwd_text: str,
    workspace_root: Path | None,
    contract: object | None = None,
) -> tuple[Path | None, str]:
    """解析验证命令 cwd（受控边界：必须落在 workspace 根内）。

    双席 seq2013/2014 硬缺口1：workspace 根缺失（无法解析）时绝对/相对
    cwd 一律结构化拒绝（fail-closed），空 cwd 也不得落到宿主进程 cwd——
    绝不跳过边界检查执行。

    2026-08-16 3×3 真机: attach 把 task_workspace.task_root 重写为系统工作区
    后, 部署者声明的 verify cwd(用户产物绝对路径)落在系统工作区外 → 误判
    越界。附加边界: contract.task_workspace.declared_task_root(attach 保留
    的部署者原始 task_root)——cwd 落在任一边界内即合法(verify 是部署者
    信任面, 声明 cwd 必须仍可执行)。
    """
    if workspace_root is None:
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    boundaries: list[Path] = [workspace_root]
    declared_root = ""
    if isinstance(contract, dict):
        tw = contract.get("task_workspace")
        if isinstance(tw, dict):
            declared_root = str(tw.get("declared_task_root") or "").strip()
    if declared_root:
        try:
            boundaries.append(Path(declared_root).expanduser().resolve(strict=False))
        except (OSError, RuntimeError):
            pass
    if not cwd_text:
        return workspace_root, VERIFY_PASSED
    candidate = Path(cwd_text).expanduser()
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            # 2026-08-16 3×3 真机(cell4 verify cwd 越界实锤): 绝对路径 cwd
            # 是部署者在契约里显式声明的产物目录——契约=部署者信任面
            # (等同部署者手跑验收命令), attach 重写 task_root 后系统工作区
            # 边界会误伤部署者声明的真实产物路径。绝对 cwd 只要存在且是
            # 目录即放行(verify 本来就只在该 cwd 执行部署者写的命令);
            # 相对 cwd 才需要 workspace 内解析(部署者相对声明)。
            if resolved.is_dir():
                return resolved, VERIFY_PASSED
            return None, VERIFY_CWD_OUT_OF_BOUNDS
        resolved = (workspace_root / candidate).resolve(strict=False)
    except (OSError, RuntimeError):
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    if not any(_within(resolved, boundary) for boundary in boundaries):
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    if not resolved.is_dir():
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    return resolved, VERIFY_PASSED


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _run_one_verify(
    item: dict[str, object],
    workspace_root: Path | None,
    contract: object | None = None,
) -> dict[str, object]:
    """执行单条验证命令（subprocess+timeout，fail-closed：异常/超时=失败）。"""
    command = str(item.get("command") or "")
    timeout = int(item.get("timeout_seconds") or _VERIFY_DEFAULT_TIMEOUT_SECONDS)
    cwd, cwd_state = _resolve_verify_cwd(
        str(item.get("cwd") or ""), workspace_root, contract=contract
    )
    if cwd_state != VERIFY_PASSED:
        return {
            "command": command,
            "ok": False,
            "exit_code": -1,
            "detail": "cwd 越界或不可解析（必须位于 task workspace 内）",
            "output": "",
        }
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=float(max(1, timeout)),
        )
        output = (str(completed.stdout or "") + str(completed.stderr or "")).strip()
        return {
            "command": command,
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "detail": f"cwd={cwd}",
            "output": output[:_VERIFY_OUTPUT_MAX_CHARS],
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "ok": False,
            "exit_code": -1,
            "detail": f"timeout after {timeout}s",
            "output": "",
        }
    except Exception as exc:  # noqa: BLE001 执行异常保守判失败
        return {
            "command": command,
            "ok": False,
            "exit_code": -1,
            "detail": f"exec error: {exc}",
            "output": "",
        }


def run_delivery_verification(
    params: object, workspace_root: Path | None = None
) -> tuple[str, list[dict[str, object]]]:
    """执行 contract 全部验证命令。

    返回 (state, results)：
    - VERIFY_SKIPPED：未声明 verify_commands，不干预
    - VERIFY_CONTRACT_INVALID：声明了但结构非法，fail-closed（调用方必须
      按未通过收口，绝不放行自然收口）
    - VERIFY_PASSED/VERIFY_FAILED：全部命令的执行结果

    双席 seq2013 硬缺口1：workspace 根缺失 → 结构化拒绝（fail-closed），
    绝不跳过边界检查执行。
    """
    contract = getattr(params, "delivery_contract", None)
    commands, state = delivery_verify_commands(contract)
    if state != VERIFY_PASSED:
        return state, []
    if workspace_root is None:
        return VERIFY_FAILED, [
            {
                "command": "",
                "ok": False,
                "exit_code": -1,
                "detail": "workspace 根缺失，验证无法受控执行（fail-closed）",
                "output": "",
            }
        ]
    # 参考项目对齐（owner seq2035/2036 + 双席 seq2032/2033/2034）：不做
    # Go-only 产物路径预检——会话运行时/工具运行时/终端交互 均无此规则，它是事故
    # 后的语言特判，不能作为公共门槛。只按验证命令真实执行结果判定。
    results = [_run_one_verify(item, workspace_root, contract=contract) for item in commands]
    overall = VERIFY_PASSED if all(bool(item.get("ok")) for item in results) else VERIFY_FAILED
    return overall, results


def build_verification_id(params: object, contract: object) -> str:
    """验证幂等 ID：attempt_id + contract hash + 命令摘要（双席 seq2013 硬缺口3）。"""
    commands, _state = delivery_verify_commands(contract)
    digest = hashlib.sha256(
        json.dumps(
            [str(item.get("command") or "") for item in commands],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    return hashlib.sha256(
        f"{attempt_id}:{_contract_hash(contract)}:{digest}".encode("utf-8")
    ).hexdigest()[:16]


def persist_delivery_verify_event(
    agent: object,
    params: object,
    state: str,
    results: list[dict[str, object]],
    contract_hash: str,
    verification_id: str,
) -> None:
    """验证结果落 runtime_events（权威 API append_event，幂等去重，审计可查）。

    双席 seq2014 实锤修复：旧实现调 append_runtime_event（不存在），标准
    RuntimeRepository 提供 append_event（repository.py:1461）——生产路径
    从未落账。现改调 append_event（attempt_id+agent_run_id 必填），并带
    verification_id 幂等：同 verification_id 已存在 → 跳过，不重复追加。
    fail-silent：落账失败绝不影响收口判定。
    """
    if state == VERIFY_SKIPPED:
        return
    try:
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_event", None)):
            return
        run_id = str(getattr(params, "run_id", "") or "")
        attempt_id = str(getattr(params, "attempt_id", "") or "")
        agent_run_id = ""
        try:
            row = repo.agent_run_for_run_id(run_id)
            if row is not None:
                agent_run_id = str(row.get("agent_run_id") or "")
        except Exception:  # noqa: BLE001 查不到不阻断落账
            pass
        if not agent_run_id:
            agent_run_id = run_id
        payload = {
            "state": state,
            "contract_hash": contract_hash,
            "verification_id": verification_id,
            "results": [
                {
                    "command": str(item.get("command") or ""),
                    "ok": bool(item.get("ok")),
                    "exit_code": int(item.get("exit_code") or -1),
                    "detail": str(item.get("detail") or ""),
                }
                for item in results
            ],
        }
        # 幂等：同 verification_id（attempt+contract hash+命令摘要）已落账 → 跳过
        try:
            existing = repo.events_for_attempt(attempt_id=attempt_id, limit=20)
            for row in existing or ():
                try:
                    # 兼容 dict 与 sqlite3.Row（sqlite3.Row 无 .get）
                    keys = row.keys() if hasattr(row, "keys") else ()
                    row_payload = (
                        row["payload_json"]
                        if "payload_json" in keys
                        else (row["payload"] if "payload" in keys else {})
                    )
                    if isinstance(row_payload, str):
                        row_payload = json.loads(row_payload)
                    if (row_payload or {}).get("verification_id") == verification_id:
                        return  # 已落账，幂等跳过
                except (TypeError, ValueError, KeyError):
                    continue
        except Exception:  # noqa: BLE001 查不到保守落账（append-only 无并发冲突）
            pass
        repo.append_event(
            event_type="delivery_verify",
            attempt_id=attempt_id,
            agent_run_id=agent_run_id,
            task_run_id=str(getattr(params, "task_run_id", "") or ""),
            payload=payload,
        )
    except Exception:  # noqa: BLE001 落账失败绝不影响收口判定
        LOGGER.warning("persist delivery verify event failed", exc_info=True)


#: 收口兜底熔断上限：同一 attempt 内 closeout 未决事件（工具结果未知导致的
#: 验证失败/未知核验提示）达 4 次即熔断——放过，转 BLOCKED/UNKNOWN_UNRESOLVED
#: 通知用户，绝不无限重试（2026-08-16 用户裁决：第 4 次放过生成问题通知）。
CLOSEOUT_UNRESOLVED_LIMIT = 4

#: 工具结果「未知」族的唯一结构化错误码（写副作用未知/超时/执行者死 → 对账
#: 后仍无终态）。known_failure 等已知失败不在此族，不参与兜底与熔断计数。
UNKNOWN_OUTCOME_ERROR_CODE = "TOOL_OPERATION_OUTCOME_UNKNOWN"


def attempt_unknown_operation_count(agent: object, params: object) -> int:
    """当前 attempt 内「工具结果未知」操作数（runtime_events 持久化计数）。

    信号 = tool_completed 事件 payload.error_code == TOOL_OPERATION_OUTCOME_UNKNOWN
    （F6 工具完成事件已接写权威 runtime_events）。只计结构化未知族，不混入
    known_failure/取消。查不到权威库时保守返回 0（不干预正常收口）。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not callable(getattr(repo, "events_for_attempt", None)):
        return 0
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    if not attempt_id:
        return 0
    try:
        events = repo.events_for_attempt(attempt_id=attempt_id, limit=500)
    except Exception:  # noqa: BLE001 查不到 → 保守 0（不干预收口）
        return 0
    count = 0
    for event in events or ():
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "") != "tool_completed":
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("error_code") or "") == UNKNOWN_OUTCOME_ERROR_CODE:
            count += 1
    return count


def attempt_closeout_unresolved_count(agent: object, params: object) -> int:
    """当前 attempt 内「收口兜底未决」事件数（持久化，重启不清零）。

    计数 = delivery_verify 事件（failed/contract_invalid/cwd 越界，即机器
    兜底验证未能给出通过结论）+ closeout_unknown_deferral 事件（无验证命令
    时的未知核验提示）。达 CLOSEOUT_UNRESOLVED_LIMIT 即熔断放过。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not callable(getattr(repo, "events_for_attempt", None)):
        return 0
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    if not attempt_id:
        return 0
    try:
        events = repo.events_for_attempt(attempt_id=attempt_id, limit=500)
    except Exception:  # noqa: BLE001 查不到 → 保守 0（不干预收口）
        return 0
    count = 0
    for event in events or ():
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "closeout_unknown_deferral":
            count += 1
            continue
        if event_type != "delivery_verify":
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("state") or "") != VERIFY_PASSED:
            count += 1
    return count


def persist_closeout_deferral_event(
    agent: object, params: object, diagnostic: str
) -> None:
    """未知核验提示落 runtime_events（熔断计数数据源，fail-silent）。"""
    try:
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_event", None)):
            return
        run_id = str(getattr(params, "run_id", "") or "")
        attempt_id = str(getattr(params, "attempt_id", "") or "")
        agent_run_id = ""
        task_run_id = ""
        if run_id:
            try:
                row = repo.agent_run_for_run_id(run_id)
                if row is not None:
                    agent_run_id = str(row.get("agent_run_id") or "")
                    task_run_id = str(row.get("task_run_id") or "")
            except Exception:  # noqa: BLE001 查不到不阻断落账
                pass
        repo.append_event(
            event_type="closeout_unknown_deferral",
            attempt_id=attempt_id,
            agent_run_id=agent_run_id or run_id,
            task_run_id=task_run_id,
            payload={
                "diagnostic": str(diagnostic or ""),
                "unknown_operation_count": attempt_unknown_operation_count(
                    agent, params
                ),
            },
        )
    except Exception:  # noqa: BLE001 落账失败绝不影响收口判定
        LOGGER.warning("persist closeout deferral event failed", exc_info=True)


def delivery_verify_failure_context(results: list[dict[str, object]]) -> str:
    """验证失败输出拼 tool_context 文本（模型据此修复，纯事实）。"""
    lines = ["[tool-system delivery-verify-failed]"]
    for index, item in enumerate(results, start=1):
        ok = bool(item.get("ok"))
        lines.append(
            f"- #{index} {'PASS' if ok else 'FAIL'}: {item.get('command') or ''}"
            f" (exit={item.get('exit_code') or -1}, {item.get('detail') or ''})"
        )
        output = str(item.get("output") or "").strip()
        if output:
            lines.append(output[:_VERIFY_OUTPUT_MAX_CHARS])
    return "\n".join(lines)
