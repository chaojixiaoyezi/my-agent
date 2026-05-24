# LLM: delivery repair declared-read allowance bounds inspection of machine-declared targets.
# 模块用途: 对 closeout 恢复动作列出的 checkpoint/artifact 读取做一次性额度管理，防止反复读旧文件不写入。

from __future__ import annotations

import json
from pathlib import Path

from .tool_delivery_repair_paths import call_path, repair_target_values, same_path_ref

_STATE_FILE = "delivery_repair_declared_reads.json"


# LLM: declared_repair_reads_exhausted returns True when all declared target reads were already granted for the same snapshots.
# 函数用途: 用 required_actions 和 repair_target_snapshots 作为机器签名；同一目标同一快照重复读不再算修复推进。
def declared_repair_reads_exhausted(
    agent: object,
    payload: dict[str, object],
    calls: list[dict[str, object]],
    required_actions: list[dict[str, object]],
) -> bool:
    read_keys = [_declared_repair_read_key(call, required_actions) for call in calls]
    read_keys = [key for key in read_keys if key]
    if not read_keys:
        return False
    signature = _declared_read_signature(payload)
    state = _load_state(agent)
    if str(state.get("signature") or "") != signature:
        state = {"seen": [], "signature": signature}
    seen = {str(item) for item in state.get("seen", []) if str(item)} if isinstance(state.get("seen"), list) else set()
    if all(key in seen for key in read_keys):
        _write_state(agent, {"seen": sorted(seen), "signature": signature})
        return True
    seen.update(read_keys)
    _write_state(agent, {"seen": sorted(seen), "signature": signature})
    return False


def declared_repair_reads_exhausted_for_all_calls(
    agent: object,
    payload: dict[str, object],
    calls: list[dict[str, object]],
    required_actions: list[dict[str, object]],
) -> bool:
    read_keys = [_declared_repair_read_key(call, required_actions) for call in calls]
    if not read_keys or not all(read_keys):
        return False
    return declared_repair_reads_exhausted(agent, payload, calls, required_actions)


def exhausted_declared_repair_read_keys(
    agent: object,
    payload: dict[str, object],
    calls: list[dict[str, object]],
    required_actions: list[dict[str, object]],
) -> set[str]:
    read_keys = [declared_repair_read_key(call, required_actions) for call in calls]
    read_keys = [key for key in read_keys if key]
    if not read_keys:
        return set()
    signature = _declared_read_signature(payload)
    state = _load_state(agent)
    if str(state.get("signature") or "") != signature:
        state = {"seen": [], "signature": signature}
    seen = {str(item) for item in state.get("seen", []) if str(item)} if isinstance(state.get("seen"), list) else set()
    exhausted = {key for key in read_keys if key in seen}
    seen.update(read_keys)
    _write_state(agent, {"seen": sorted(seen), "signature": signature})
    return exhausted


def reset_declared_read_allowance(agent: object) -> None:
    path = _state_path(agent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _declared_repair_read_key(call: dict[str, object], required_actions: list[dict[str, object]]) -> str:
    if str(call.get("tool") or "").strip() != "read_file":
        return ""
    path = call_path(call)
    if not path:
        return ""
    for target in repair_target_values(required_actions):
        if same_path_ref(path, target):
            return target.replace("\\", "/").strip("/")
    return ""


def declared_repair_read_key(call: dict[str, object], required_actions: list[dict[str, object]]) -> str:
    return _declared_repair_read_key(call, required_actions)


def _declared_read_signature(payload: dict[str, object]) -> str:
    value = {
        "required_actions": payload.get("required_actions", []),
        "repair_target_snapshots": payload.get("repair_target_snapshots", []),
        "strict_write_required": bool(payload.get("strict_write_required")),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _state_path(agent: object) -> Path:
    tools = getattr(agent, "tools", None)
    workspace = getattr(tools, "workspace_root", None)
    return Path(workspace or getattr(agent, "root", ".")).resolve() / ".agent_delivery" / _STATE_FILE


def _load_state(agent: object) -> dict[str, object]:
    path = _state_path(agent)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(agent: object, payload: dict[str, object]) -> None:
    path = _state_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


__all__ = [
    "declared_repair_reads_exhausted",
    "declared_repair_reads_exhausted_for_all_calls",
    "declared_repair_read_key",
    "exhausted_declared_repair_read_keys",
    "reset_declared_read_allowance",
]
