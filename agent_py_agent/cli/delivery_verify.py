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
    cwd_text: str, workspace_root: Path | None
) -> tuple[Path | None, str]:
    """解析验证命令 cwd（受控边界：必须落在 workspace 根内）。

    返回 (path, state)：path=None + VERIFY_CWD_OUT_OF_BOUNDS 表示越界/不可解析。
    """
    if not cwd_text:
        return workspace_root, VERIFY_PASSED
    candidate = Path(cwd_text).expanduser()
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        elif workspace_root is not None:
            resolved = (workspace_root / candidate).resolve(strict=False)
        else:
            return None, VERIFY_CWD_OUT_OF_BOUNDS
    except (OSError, RuntimeError):
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    if workspace_root is not None:
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            return None, VERIFY_CWD_OUT_OF_BOUNDS
    if not resolved.is_dir():
        return None, VERIFY_CWD_OUT_OF_BOUNDS
    return resolved, VERIFY_PASSED


def _run_one_verify(item: dict[str, object], workspace_root: Path | None) -> dict[str, object]:
    """执行单条验证命令（subprocess+timeout，fail-closed：异常/超时=失败）。"""
    command = str(item.get("command") or "")
    timeout = int(item.get("timeout_seconds") or _VERIFY_DEFAULT_TIMEOUT_SECONDS)
    cwd, cwd_state = _resolve_verify_cwd(str(item.get("cwd") or ""), workspace_root)
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
    """
    contract = getattr(params, "delivery_contract", None)
    commands, state = delivery_verify_commands(contract)
    if state != VERIFY_PASSED:
        return state, []
    results = [_run_one_verify(item, workspace_root) for item in commands]
    overall = VERIFY_PASSED if all(bool(item.get("ok")) for item in results) else VERIFY_FAILED
    return overall, results


def persist_delivery_verify_event(
    agent: object,
    params: object,
    state: str,
    results: list[dict[str, object]],
    contract_hash: str,
) -> None:
    """验证结果落 runtime_events（append-only，审计可查；fail-silent）。"""
    if state == VERIFY_SKIPPED:
        return
    try:
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_runtime_event", None)):
            return
        from ..agent.conversation.runtime import durable_task_id

        repo.append_runtime_event(
            task_id=durable_task_id(params),
            run_id=str(getattr(params, "run_id", "") or ""),
            attempt_id=str(getattr(params, "attempt_id", "") or ""),
            event_type="delivery_verify",
            detail=json.dumps(
                {
                    "state": state,
                    "contract_hash": contract_hash,
                    "results": [
                        {
                            "command": str(item.get("command") or ""),
                            "ok": bool(item.get("ok")),
                            "exit_code": int(item.get("exit_code") or -1),
                            "detail": str(item.get("detail") or ""),
                        }
                        for item in results
                    ],
                },
                ensure_ascii=False,
            ),
        )
    except Exception:  # noqa: BLE001 落账失败绝不影响收口判定
        LOGGER.warning("persist delivery verify event failed", exc_info=True)


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
