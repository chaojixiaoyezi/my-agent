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

纯结构化：无 NL 判断；产物/命令全部来自 contract 显式声明。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_VERIFY_DEFAULT_TIMEOUT_SECONDS = 120
_VERIFY_OUTPUT_MAX_CHARS = 4000


def delivery_verify_commands(contract: object) -> list[dict[str, object]]:
    """提取并校验 contract 的 verify_commands（结构不合法 → 空，fail-closed 不验证）。"""
    if not isinstance(contract, dict):
        return []
    raw = contract.get("verify_commands")
    if raw is None:
        return []
    if not isinstance(raw, list):
        LOGGER.warning("delivery contract verify_commands must be a list")
        return []
    commands: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            LOGGER.warning("delivery contract verify_commands[%d] must be an object", index)
            continue
        command = str(item.get("command") or "").strip()
        if not command:
            LOGGER.warning("delivery contract verify_commands[%d] missing command", index)
            continue
        commands.append(
            {
                "command": command,
                "cwd": str(item.get("cwd") or "").strip(),
                "timeout_seconds": int(item.get("timeout_seconds") or 0) or _VERIFY_DEFAULT_TIMEOUT_SECONDS,
            }
        )
    return commands


def _resolve_verify_cwd(cwd_text: str, workspace_root: Path | None) -> Path | None:
    if not cwd_text:
        return workspace_root
    candidate = Path(cwd_text).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
    if workspace_root is None:
        return None
    try:
        return (workspace_root / candidate).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _run_one_verify(item: dict[str, object], workspace_root: Path | None) -> dict[str, object]:
    """执行单条验证命令（subprocess+timeout，fail-closed：异常/超时=失败）。"""
    command = str(item.get("command") or "")
    timeout = int(item.get("timeout_seconds") or _VERIFY_DEFAULT_TIMEOUT_SECONDS)
    cwd = _resolve_verify_cwd(str(item.get("cwd") or ""), workspace_root)
    if cwd is None or not cwd.is_dir():
        return {
            "command": command,
            "ok": False,
            "exit_code": -1,
            "detail": "cwd 无法解析或不是目录",
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
) -> tuple[bool, list[dict[str, object]]]:
    """执行 contract 全部验证命令；无 verify_commands → (True, []) 不干预。"""
    contract = getattr(params, "delivery_contract", None)
    commands = delivery_verify_commands(contract)
    if not commands:
        return True, []
    results = [_run_one_verify(item, workspace_root) for item in commands]
    return all(bool(item.get("ok")) for item in results), results


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
