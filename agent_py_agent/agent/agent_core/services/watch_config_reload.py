
from __future__ import annotations

import json
import time as time_module
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ...capability.runtime_config import (
    CapabilityConfigSnapshot,
    default_capability_config_path,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)


@dataclass(frozen=True)
class WatchRuntimeConfigRequest:
    agent: object
    current_cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


@dataclass(frozen=True)
class WatchRuntimeConfigResult:
    cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


def initial_watch_config_snapshot(
    agent: object,
    cfg: CapabilityConfig,
    router: CapabilityRouter,
) -> CapabilityConfigSnapshot | None:
    path = Path(
        getattr(agent, "capability_config_path", "")
        or default_capability_config_path(getattr(agent, "root", "."))
    )
    if not path.exists():
        router.config = cfg
        return None
    return _load_initial_snapshot(agent, cfg, router, path)


def watch_runtime_config(request: WatchRuntimeConfigRequest) -> WatchRuntimeConfigResult:
    if request.snapshot is None:
        return WatchRuntimeConfigResult(request.current_cfg, request.router, None)
    try:
        result = reload_capability_config_if_changed(request.snapshot, router=request.router)
    except (FileNotFoundError, OSError, ValueError):
        return WatchRuntimeConfigResult(request.current_cfg, request.router, request.snapshot)
    if result.changed:
        request.agent._capability_config_runtime_snapshot = result.snapshot
        _write_watch_config_reload(request.agent, result.snapshot)
    return WatchRuntimeConfigResult(result.snapshot.config, request.router, result.snapshot)


def _load_initial_snapshot(
    agent: object,
    cfg: CapabilityConfig,
    router: CapabilityRouter,
    path: Path,
) -> CapabilityConfigSnapshot | None:
    try:
        snapshot = load_capability_config_snapshot(path)
    except (FileNotFoundError, OSError, ValueError):
        router.config = cfg
        return None
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = snapshot
    router.config = snapshot.config
    return snapshot


def _write_watch_config_reload(agent: object, snapshot: CapabilityConfigSnapshot) -> None:
    path = agent.subagents.workspace / "capability_config_hot_reload.jsonl"
    payload = {
        "kind": "capability_config_hot_reload",
        "version": snapshot.version,
        "path": str(snapshot.path),
        "mtime_ns": snapshot.mtime_ns,
        "size": snapshot.size,
        "ts": time_module.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
