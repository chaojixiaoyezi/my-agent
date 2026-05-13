# LLM: Watch config reload helpers keep the dispatch watch loop thin.
# 模块用途: 为 watch_subagents 提供 capability_config 热加载和记录，不碰 runner 执行逻辑。

from __future__ import annotations

import json
import time as time_module
from dataclasses import dataclass
from pathlib import Path

from ...capabilities import CapabilityRouter
from ...capability.runtime_config import (
    CapabilityConfigSnapshot,
    default_capability_config_path,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)
from ...capability_config import CapabilityConfig


# LLM: WatchRuntimeConfigRequest bundles hot-reload inputs for one watch cycle.
# 类用途: 保存 watch 热加载所需状态，避免循环层散传 cfg/router/snapshot。
@dataclass(frozen=True)
class WatchRuntimeConfigRequest:
    agent: object
    fallback_cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


# LLM: WatchRuntimeConfigResult returns the config/router/snapshot trio for a cycle.
# 类用途: 返回 watch 本轮应该使用的能力配置和新的快照。
@dataclass(frozen=True)
class WatchRuntimeConfigResult:
    cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


# LLM: initial_watch_config_snapshot captures the file version used by the first watch cycle.
# 函数用途: watch 启动时加载 capability_config 快照；找不到文件时继续使用调用方传入配置。
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


# LLM: watch_runtime_config hot-reloads config for future cycles without mutating live runners.
# 函数用途: 每轮 watch 开始前检测 capability_config 是否变化；变更只影响接下来 dispatch 的配置。
def watch_runtime_config(request: WatchRuntimeConfigRequest) -> WatchRuntimeConfigResult:
    if request.snapshot is None:
        return WatchRuntimeConfigResult(request.fallback_cfg, request.router, None)
    try:
        result = reload_capability_config_if_changed(request.snapshot, router=request.router)
    except (FileNotFoundError, OSError, ValueError):
        return WatchRuntimeConfigResult(request.fallback_cfg, request.router, request.snapshot)
    if result.changed:
        request.agent._capability_config_runtime_snapshot = result.snapshot
        _write_watch_config_reload(request.agent, result.snapshot)
    return WatchRuntimeConfigResult(result.snapshot.config, request.router, result.snapshot)


# LLM: _load_initial_snapshot shields watch startup from broken config files.
# 函数用途: 读取初始快照并写回 agent/router；失败时使用传入配置继续运行。
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


# LLM: _write_watch_config_reload leaves a small trail when watch notices config changes.
# 函数用途: 写入 hot reload JSONL，方便后续排查哪个 watch 轮次开始使用新配置。
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
