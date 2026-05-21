# LLM: delivery repair path helpers keep guard logic scoped to structured refs.
# 模块用途: 提供 staged-delivery 修复用的路径比较、命令提取和 repair target 快照工具。

from __future__ import annotations

from pathlib import Path


# LLM: call_path reads only structured path fields from a tool call.
# 函数用途: 从工具调用里的 path/file_path/target_path 提取目标路径，不解析普通自然语言。
def call_path(call: dict[str, object]) -> str:
    for key in ("path", "file_path", "target_path"):
        if value := str(call.get(key) or "").strip():
            return value
    return ""


# LLM: same_path_ref compares runtime call paths with contract refs using path syntax only.
# 函数用途: 判断工具调用路径和合同 ref 是否指向同一相对目标。
def same_path_ref(path: str, ref: str) -> bool:
    normalized_path = path.replace("\\", "/").rstrip("/")
    normalized_ref = ref.replace("\\", "/").strip("/")
    return (
        normalized_path == normalized_ref
        or normalized_path.endswith(f"/{normalized_ref}")
        or normalized_ref.endswith(f"/{normalized_path}")
    )


# LLM: call_command reads command fields from structured run_command payloads.
# 函数用途: 从 run_command 的 command/shell.command 字段提取命令，用于副作用分类。
def call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""


# LLM: command_references_ref checks shell mutation target against declared writer refs.
# 函数用途: 判断命令文本是否显式引用了合同声明的输出 ref。
def command_references_ref(command: str, ref: str) -> bool:
    normalized_command = command.replace("\\", "/")
    normalized_ref = ref.replace("\\", "/").strip("/")
    return normalized_ref in normalized_command


# LLM: repair_target_values collects candidate files from structured recovery actions.
# 函数用途: 只读取 repair_targets/checkpoint_ref/artifact_path 机器字段，不解析提示词或日志正文。
def repair_target_values(required_actions: list[dict[str, object]]) -> list[str]:
    values = [
        value
        for action in required_actions
        for value in _action_repair_targets(action)
    ]
    return list(dict.fromkeys(values))


# LLM: _action_repair_targets normalizes target fields from one recovery action.
# 函数用途: 展平单个 recovery action 的 repair_targets、checkpoint_ref 和 artifact_path。
def _action_repair_targets(action: dict[str, object]) -> list[str]:
    targets = action.get("repair_targets")
    values = [str(item) for item in targets if str(item)] if isinstance(targets, list) else []
    return [
        *values,
        *[
            value
            for key in ("checkpoint_ref", "artifact_path")
            for value in [str(action.get(key) or "").strip()]
            if value
        ],
    ]


# LLM: repair_target_snapshots gives bounded file facts when read_file is rejected during repair.
# 函数用途: 对 required repair_targets 读取小预览，避免模型为了获取补丁上下文反复调用检查工具。
def repair_target_snapshots(
    required_actions: list[dict[str, object]],
    agent_root: Path,
) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    budget = 6000
    for target in repair_target_values(required_actions)[:4]:
        snapshot = repair_target_snapshot(target, agent_root, max_chars=min(2400, budget))
        if snapshot:
            budget -= len(str(snapshot.get("preview") or ""))
            snapshots.append(snapshot)
        if budget <= 0:
            break
    return snapshots


# LLM: repair_target_snapshot reads a bounded text preview for one local repair target.
# 函数用途: 快照只面向小型文本文件；目录、大文件和二进制文件只返回存在性事实。
def repair_target_snapshot(target: str, agent_root: Path, *, max_chars: int) -> dict[str, object]:
    path = resolve_repair_target(target, agent_root)
    if not path.exists():
        return {"path": target, "exists": False}
    if path.is_dir():
        return {"path": target, "exists": True, "kind": "directory"}
    snapshot = _file_snapshot_base(path, target)
    if not snapshot.get("readable", True) or int(snapshot.get("size_bytes") or 0) > 12000:
        return snapshot
    return _attach_text_preview(snapshot, path, max_chars=max_chars)


def _file_snapshot_base(path: Path, target: str) -> dict[str, object]:
    try:
        size = path.stat().st_size
    except OSError:
        return {"path": target, "exists": True, "readable": False}
    return {"path": target, "exists": True, "kind": "file", "size_bytes": size}


def _attach_text_preview(snapshot: dict[str, object], path: Path, *, max_chars: int) -> dict[str, object]:
    try:
        snapshot["preview"] = path.read_text(encoding="utf-8")[:max(0, max_chars)]
    except UnicodeDecodeError:
        snapshot["text"] = False
    except OSError:
        snapshot["readable"] = False
    return snapshot


# LLM: resolve_repair_target keeps snapshot reads scoped to the current agent root for relative refs.
# 函数用途: 支持 absolute repair_targets，同时让相对 ref 按 workspace root 解析。
def resolve_repair_target(target: str, agent_root: Path) -> Path:
    path = Path(target).expanduser()
    return path if path.is_absolute() else agent_root / path


__all__ = [
    "call_command",
    "call_path",
    "command_references_ref",
    "repair_target_snapshots",
    "repair_target_values",
    "same_path_ref",
]
