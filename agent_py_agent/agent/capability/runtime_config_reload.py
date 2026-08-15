
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import load_capability_config
from .runtime_config_models import CapabilityConfigReloadResult, CapabilityConfigSnapshot


def default_capability_config_path(root: str | Path) -> Path:
    base = Path(root)
    candidates = [
        base / "agent_py_agent" / "config" / "capability_config.yaml",
        base / "config" / "capability_config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


# LLM: "从 agent 对象取 capability 配置"的唯一权威入口：优先 agent 上的运行时快照
#   _capability_config_runtime_snapshot，否则按 capability_config_path/默认路径加载并把
#   快照缓存回 agent（副作用）。加载失败返回 None，调用方按"全部默认值"处理。
#   消费方：runtime/context_compactor（compact 触发百分比）、orchestration/dispatch/mixin
#   （失败自省自动拆分开关）。新增运行时读 capability 配置的地方应一律走这里。
# 函数用途: 运行时想读 capability_config.yaml 里的开关时，从 agent 拿配置对象。
def capability_config_for_agent(agent: object):
    snapshot = getattr(agent, "_capability_config_runtime_snapshot", None)
    config = getattr(snapshot, "config", None)
    if config is not None:
        return config
    path = Path(
        str(getattr(agent, "capability_config_path", "") or "")
        or default_capability_config_path(getattr(agent, "root", "."))
    )
    try:
        snapshot = load_capability_config_snapshot(path)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    try:
        agent._capability_config_runtime_snapshot = snapshot
    except AttributeError:
        pass
    return snapshot.config


def capability_config_version(config_path: str | Path) -> str:
    path = Path(config_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


def load_capability_config_snapshot(config_path: str | Path) -> CapabilityConfigSnapshot:
    path = Path(config_path)
    stat = path.stat()
    return CapabilityConfigSnapshot(
        path=path,
        config=load_capability_config(path),
        version=capability_config_version(path),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def reload_capability_config_if_changed(
    snapshot: CapabilityConfigSnapshot,
    *,
    router: object | None = None,
) -> CapabilityConfigReloadResult:
    current_version = capability_config_version(snapshot.path)
    if current_version == snapshot.version:
        return CapabilityConfigReloadResult(snapshot=snapshot, changed=False, message="unchanged")
    reloaded = load_capability_config_snapshot(snapshot.path)
    if router is not None and hasattr(router, "config"):
        router.config = reloaded.config
    return CapabilityConfigReloadResult(snapshot=reloaded, changed=True, message="reloaded")
