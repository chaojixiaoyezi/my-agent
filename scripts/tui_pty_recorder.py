#!/usr/bin/env python3
# LLM: 本模块是 TUI 黑盒验收的确定性 PTY 录制器；只记录子进程终端字节、脱敏动作元数据和尺寸，不读取或保存环境变量值。
# 模块用途: 按时间表向真实终端程序发送按键、粘贴和 resize，并产出原始 ANSI、事件账和可核验 manifest。

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SENSITIVE_ARG_RE = re.compile(r"(?i)(api[-_]?key|token|password|secret|credential)")
KEY_BYTES = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "backtab": b"\x1b[Z",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    **{f"ctrl-{letter}": bytes([ord(letter) - 96]) for letter in "abcdefghijklmnopqrstuvwxyz"},
}


# LLM: PtyAction 是输入时间线的版本内存模型；payload 只在执行期含正文，事件账不得原样持久化。
# 类用途: 表示在启动后某个毫秒点执行的按键、输入、粘贴、resize 或检查点。
@dataclass(frozen=True)
class PtyAction:
    at_ms: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    label: str = ""


# LLM: RecorderConfig 固定一次录制的进程、尺寸、时间界限和输出目录，不接受 shell 字符串。
# 类用途: 汇总一次 PTY 录制所需的公开配置。
@dataclass(frozen=True)
class RecorderConfig:
    output_dir: Path
    name: str
    command: tuple[str, ...]
    actions: tuple[PtyAction, ...] = ()
    cwd: Path | None = None
    rows: int = 36
    cols: int = 120
    timeout_ms: int = 30_000
    terminate_grace_ms: int = 1_000
    term: str = "xterm-256color"
    locale: str = "C.UTF-8"

    # LLM: 配置校验必须在创建目录和进程前完成，避免路径穿越、空命令和失控超时。
    # 函数用途: 拒绝不安全的名称、尺寸、动作顺序和时间参数。
    def __post_init__(self) -> None:
        if not SAFE_NAME_RE.fullmatch(str(self.name or "")):
            raise ValueError("recording name must be a safe filename component")
        if not self.command or not str(self.command[0]).strip():
            raise ValueError("PTY recorder requires a command argv")
        if self.rows <= 0 or self.cols <= 0:
            raise ValueError("terminal rows and cols must be positive")
        if self.timeout_ms <= 0 or self.terminate_grace_ms < 0:
            raise ValueError("timeout must be positive and grace must be nonnegative")
        if tuple(sorted(self.actions, key=lambda item: item.at_ms)) != self.actions:
            raise ValueError("PTY actions must be sorted by at_ms")


# LLM: RecorderResult 只返回公开产物路径和进程终态；原始字节留在 evidence 文件中。
# 类用途: 告诉调用方录制是否超时、退出码和三个证据文件的位置。
@dataclass(frozen=True)
class RecorderResult:
    raw_path: Path
    events_path: Path
    manifest_path: Path
    exit_code: int
    timed_out: bool
    byte_count: int


# LLM: _CaptureState 是单次 loop 的内部可变计数器，不跨录制共享也不作为产品状态源。
# 类用途: 跟踪输出偏移、hash、动作位置和超时结果。
@dataclass
class _CaptureState:
    started_monotonic: float
    digest: Any = field(default_factory=hashlib.sha256)
    byte_count: int = 0
    next_action: int = 0
    timed_out: bool = False


# LLM: 动作文件解析只接受 schema=1 的 JSON 数组或带 actions 的对象，禁止执行未知动作。
# 函数用途: 从磁盘加载并验证按绝对毫秒排序的 PTY 动作。
def load_actions(path: Path) -> tuple[PtyAction, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if int(payload.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError("unsupported PTY action schema")
        payload = payload.get("actions")
    if not isinstance(payload, list):
        raise ValueError("PTY action file must contain a JSON array")
    actions = tuple(_parse_action(item) for item in payload)
    if tuple(sorted(actions, key=lambda item: item.at_ms)) != actions:
        raise ValueError("PTY actions must be sorted by at_ms")
    return actions


# LLM: 单动作校验采用开放 payload、封闭执行 kind；未知字段不会自动变成终端字节。
# 函数用途: 将一个 JSON 对象转换为经过基本约束的动作。
def _parse_action(value: Any) -> PtyAction:
    if not isinstance(value, dict):
        raise ValueError("each PTY action must be an object")
    at_ms = int(value.get("at_ms", -1))
    kind = str(value.get("type") or value.get("kind") or "").strip().lower()
    if at_ms < 0:
        raise ValueError("PTY action at_ms must be nonnegative")
    if kind not in {"write", "key", "paste", "bytes_base64", "resize", "checkpoint"}:
        raise ValueError(f"unsupported PTY action type: {kind}")
    payload = {key: item for key, item in value.items() if key not in {"at_ms", "type", "kind", "label"}}
    action = PtyAction(at_ms=at_ms, kind=kind, payload=payload, label=str(value.get("label") or ""))
    _validate_action_payload(action)
    return action


# LLM: payload 校验只验证动作执行所需结构，不把 write/paste 正文复制到错误或 manifest。
# 函数用途: 提前拒绝未知按键、非法 base64 和无效 resize 尺寸。
def _validate_action_payload(action: PtyAction) -> None:
    if action.kind in {"write", "paste"} and not isinstance(action.payload.get("text"), str):
        raise ValueError(f"PTY {action.kind} action requires text")
    if action.kind == "key" and str(action.payload.get("key") or "").lower() not in KEY_BYTES:
        raise ValueError("PTY key action requires a supported key")
    if action.kind == "bytes_base64":
        base64.b64decode(str(action.payload.get("data") or ""), validate=True)
    if action.kind == "resize":
        if int(action.payload.get("rows", 0)) <= 0 or int(action.payload.get("cols", 0)) <= 0:
            raise ValueError("PTY resize requires positive rows and cols")


# LLM: 主录制入口先建立独占证据文件再启动 argv 进程；异常时也必须关闭 PTY 并回收子进程组。
# 函数用途: 执行一次录制并写出 raw ANSI、JSONL 事件和 manifest。
def record_pty_session(config: RecorderConfig) -> RecorderResult:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{config.name}.raw.ansi"
    events_path = config.output_dir / f"{config.name}.events.jsonl"
    manifest_path = config.output_dir / f"{config.name}.manifest.json"
    master_fd, slave_fd = os.openpty()
    _set_winsize(master_fd, config.rows, config.cols)
    started_at = datetime.now(timezone.utc).isoformat()
    process = _spawn_process(config, slave_fd)
    os.close(slave_fd)
    state = _CaptureState(started_monotonic=time.monotonic())
    try:
        with _open_private_binary(raw_path) as raw_handle, _open_private_text(events_path) as events_handle:
            _capture_loop(process, master_fd, raw_handle, events_handle, config, state)
    finally:
        os.close(master_fd)
        if process.poll() is None:
            _terminate_process_group(process, config.terminate_grace_ms)
    exit_code = int(process.wait())
    _write_manifest(config, state, started_at, exit_code, raw_path, events_path, manifest_path)
    return RecorderResult(raw_path, events_path, manifest_path, exit_code, state.timed_out, state.byte_count)


# LLM: 子进程必须接收 argv 而非 shell，且只注入可公开 TERM/locale；父进程完整环境值永不写 manifest。
# 函数用途: 在新进程组中把 stdin、stdout 和 stderr 全部连接到 PTY slave。
def _spawn_process(config: RecorderConfig, slave_fd: int) -> subprocess.Popen[bytes]:
    child_env = os.environ.copy()
    child_env["TERM"] = config.term
    child_env["LANG"] = config.locale
    child_env["LC_ALL"] = config.locale
    return subprocess.Popen(
        config.command,
        cwd=str(config.cwd) if config.cwd else None,
        env=child_env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
    )


# LLM: capture loop 按 monotonic 时间执行动作并持续 drain 输出；进程结束或 deadline 后不无限等待。
# 函数用途: 驱动动作时间线，写入原始字节和脱敏事件索引。
def _capture_loop(
    process: subprocess.Popen[bytes],
    master_fd: int,
    raw_handle: Any,
    events_handle: Any,
    config: RecorderConfig,
    state: _CaptureState,
) -> None:
    deadline = state.started_monotonic + config.timeout_ms / 1000.0
    os.set_blocking(master_fd, False)
    while True:
        now = time.monotonic()
        _run_due_actions(master_fd, events_handle, config.actions, state, now)
        _read_available(master_fd, raw_handle, events_handle, state, wait_seconds=_next_wait(config, state, now))
        if process.poll() is not None:
            _read_available(master_fd, raw_handle, events_handle, state, wait_seconds=0.05)
            _write_event(
                events_handle,
                {"type": "process_exit", "at_ms": _elapsed_ms(state), "exit_code": process.returncode},
            )
            return
        if time.monotonic() >= deadline:
            state.timed_out = True
            _write_event(events_handle, {"type": "timeout", "at_ms": _elapsed_ms(state)})
            _terminate_process_group(process, config.terminate_grace_ms)
            _read_available(master_fd, raw_handle, events_handle, state, wait_seconds=0.1)
            _write_event(
                events_handle,
                {"type": "process_exit", "at_ms": _elapsed_ms(state), "exit_code": process.returncode},
            )
            return


# LLM: 调度器严格按动作表顺序执行到当前时刻，记录正文长度而不记录正文内容。
# 函数用途: 执行所有已到期动作并推进动作 cursor。
def _run_due_actions(
    master_fd: int,
    events_handle: Any,
    actions: tuple[PtyAction, ...],
    state: _CaptureState,
    now: float,
) -> None:
    elapsed_ms = int((now - state.started_monotonic) * 1000)
    while state.next_action < len(actions) and actions[state.next_action].at_ms <= elapsed_ms:
        action = actions[state.next_action]
        metadata = _execute_action(master_fd, action)
        metadata["offset"] = state.byte_count
        _write_event(
            events_handle,
            {"type": "action", "at_ms": elapsed_ms, "scheduled_ms": action.at_ms, **metadata},
        )
        state.next_action += 1


# LLM: 动作执行是唯一把脚本数据写进 PTY 的入口；检查点只记 offset，resize 只修改内核窗口尺寸。
# 函数用途: 执行一个动作并返回不含正文的审计元数据。
def _execute_action(master_fd: int, action: PtyAction) -> dict[str, Any]:
    metadata: dict[str, Any] = {"action": action.kind, "label": action.label}
    if action.kind == "resize":
        rows = int(action.payload["rows"])
        cols = int(action.payload["cols"])
        _set_winsize(master_fd, rows, cols)
        metadata.update({"rows": rows, "cols": cols})
        return metadata
    if action.kind == "checkpoint":
        return metadata
    data = _action_bytes(action)
    _write_all(master_fd, data)
    metadata.update({"byte_count": len(data), "char_count": _action_char_count(action)})
    return metadata


# LLM: 字节编码只处理已验证动作，bracketed paste 必须作为一个原子帧写入。
# 函数用途: 将 write、key、paste 或 base64 动作编码为终端字节。
def _action_bytes(action: PtyAction) -> bytes:
    if action.kind == "key":
        return KEY_BYTES[str(action.payload["key"]).lower()]
    if action.kind == "bytes_base64":
        return base64.b64decode(str(action.payload.get("data") or ""), validate=True)
    text = str(action.payload.get("text") or "").encode("utf-8")
    if action.kind == "paste":
        return b"\x1b[200~" + text + b"\x1b[201~"
    return text


# LLM: 输出读取把 raw bytes 原样落盘，事件账仅保存偏移和长度，避免复制敏感终端正文。
# 函数用途: 等待并 drain 当前可读 PTY 数据。
def _read_available(
    master_fd: int,
    raw_handle: Any,
    events_handle: Any,
    state: _CaptureState,
    *,
    wait_seconds: float,
) -> None:
    readable, _, _ = select.select([master_fd], [], [], max(0.0, wait_seconds))
    if not readable:
        return
    while True:
        try:
            chunk = os.read(master_fd, 65_536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not chunk:
            return
        offset = state.byte_count
        raw_handle.write(chunk)
        raw_handle.flush()
        state.digest.update(chunk)
        state.byte_count += len(chunk)
        _write_event(
            events_handle,
            {"type": "output", "at_ms": _elapsed_ms(state), "offset": offset, "length": len(chunk)},
        )


# LLM: 写 PTY 必须处理短写和 EINTR，不能悄悄截断粘贴或控制序列。
# 函数用途: 将完整动作字节写入 master fd。
def _write_all(master_fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(master_fd, view)
        except InterruptedError:
            continue
        view = view[written:]


# LLM: resize 使用内核 winsize 并向前台进程组发 SIGWINCH；尚无前台组时只保留已设置尺寸。
# 函数用途: 修改 PTY 行列数并通知运行中的 TUI 重排。
def _set_winsize(master_fd: int, rows: int, cols: int) -> None:
    packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    try:
        foreground_group = os.tcgetpgrp(master_fd)
        os.killpg(foreground_group, signal.SIGWINCH)
    except OSError:
        return


# LLM: 超时回收只针对刚创建的子进程组，先 TERM 再在有界 grace 后 KILL，不触碰外部 tmux 或 Gateway。
# 函数用途: 安全终止录制命令及其子进程。
def _terminate_process_group(process: subprocess.Popen[bytes], grace_ms: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.0, grace_ms / 1000.0))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


# LLM: manifest 只含脱敏 argv、公开尺寸/TERM、hash 和进程终态；环境变量与动作正文永不落入此文件。
# 函数用途: 写出可复核的一次录制摘要。
def _write_manifest(
    config: RecorderConfig,
    state: _CaptureState,
    started_at: str,
    exit_code: int,
    raw_path: Path,
    events_path: Path,
    manifest_path: Path,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": config.name,
        "command": _redacted_argv(config.command),
        "cwd": str(config.cwd) if config.cwd else None,
        "initial_size": {"rows": config.rows, "cols": config.cols},
        "term": config.term,
        "locale": config.locale,
        "started_at": started_at,
        "duration_ms": _elapsed_ms(state),
        "exit_code": exit_code,
        "timed_out": state.timed_out,
        "action_count": len(config.actions),
        "actions_executed": state.next_action,
        "byte_count": state.byte_count,
        "sha256": state.digest.hexdigest(),
        "raw_file": raw_path.name,
        "events_file": events_path.name,
    }
    with _open_private_text(manifest_path) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# LLM: evidence 文件从创建时即为 owner-only；即使目标已存在也必须收紧旧 mode，不能等录制完成后再 chmod。
# 函数用途: 以 0600 原子打开或截断一个二进制证据文件。
def _open_private_binary(path: Path) -> Any:
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


# LLM: 文本 evidence 与 raw ANSI 使用相同 owner-only 边界，统一以 UTF-8 和 Unix 换行写入。
# 函数用途: 以 0600 原子打开或截断一个文本证据文件。
def _open_private_text(path: Path) -> Any:
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


# LLM: argv 脱敏既处理敏感 flag 的下一值，也处理 KEY=value 和常见秘密前缀；仅用于证据展示。
# 函数用途: 返回不会暴露常见 key、token 或 password 的命令参数副本。
def _redacted_argv(argv: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for raw in argv:
        token = str(raw)
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if "=" in token and SENSITIVE_ARG_RE.search(token.split("=", 1)[0]):
            result.append(token.split("=", 1)[0] + "=<redacted>")
            continue
        if token.startswith("-") and SENSITIVE_ARG_RE.search(token):
            result.append(token)
            redact_next = True
            continue
        result.append("<redacted>" if _looks_like_secret(token) else token)
    return result


# LLM: 秘密启发式只做 manifest 最后一层保护，不作为权限、安全或配置判断依据。
# 函数用途: 识别常见长 token 前缀，避免它们出现在录制摘要。
def _looks_like_secret(token: str) -> bool:
    lowered = token.lower()
    return len(token) >= 20 and lowered.startswith(("sk-", "api_", "token_", "bearer "))


# LLM: 调度等待上限保持很短，确保 resize、中断时间线和 deadline 都不会被长阻塞漂移。
# 函数用途: 计算下次 select 最多等待多久。
def _next_wait(config: RecorderConfig, state: _CaptureState, now: float) -> float:
    waits = [0.05, state.started_monotonic + config.timeout_ms / 1000.0 - now]
    if state.next_action < len(config.actions):
        due = state.started_monotonic + config.actions[state.next_action].at_ms / 1000.0
        waits.append(due - now)
    return max(0.0, min(waits))


# LLM: 动作字符数仅用于审计规模，base64 raw bytes 没有可靠字符语义。
# 函数用途: 返回动作的公开字符计数。
def _action_char_count(action: PtyAction) -> int | None:
    if action.kind in {"write", "paste"}:
        return len(str(action.payload.get("text") or ""))
    return None


# LLM: 事件写入统一使用单行排序 JSON，调用方不得在 payload 中传终端正文。
# 函数用途: 追加并立即刷新一条录制事件。
def _write_event(handle: Any, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


# LLM: elapsed 只使用 monotonic clock，避免系统时间跳变破坏动作与输出的相对顺序。
# 函数用途: 返回录制开始至今的非负毫秒数。
def _elapsed_ms(state: _CaptureState) -> int:
    return max(0, int((time.monotonic() - state.started_monotonic) * 1000))


# LLM: CLI parser 只组装录制配置，不解释 shell；`--` 后所有 token 都是 argv。
# 函数用途: 定义 PTY 录制器命令行参数。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="录制可回放的 TUI PTY/ANSI 黑盒证据")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--actions", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--rows", type=int, default=36)
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--terminate-grace-ms", type=int, default=1_000)
    parser.add_argument("--term", default="xterm-256color")
    parser.add_argument("--locale", default="C.UTF-8")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


# LLM: main 去除 argparse 保留的单个 `--` 并以结构化 manifest 作为唯一成功摘要。
# 函数用途: 执行命令行录制并打印 manifest 路径。
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = tuple(args.command[1:] if args.command[:1] == ["--"] else args.command)
    actions = load_actions(args.actions) if args.actions else ()
    config = RecorderConfig(
        output_dir=args.output_dir,
        name=args.name,
        command=command,
        actions=actions,
        cwd=args.cwd,
        rows=args.rows,
        cols=args.cols,
        timeout_ms=args.timeout_ms,
        terminate_grace_ms=args.terminate_grace_ms,
        term=args.term,
        locale=args.locale,
    )
    result = record_pty_session(config)
    print(result.manifest_path)
    return 124 if result.timed_out else (result.exit_code if result.exit_code >= 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
