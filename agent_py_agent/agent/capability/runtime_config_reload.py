
# LLM: 能力配置的文件快照、agent 缓存读取和显式重载共用本模块；缓存须为 CapabilityConfig，失败不伪造配置。
#   agent 读取会缓存快照，显式重载可能更新 router.config；改动须复核编排、Compact 与运行配置测试。
# 模块用途: 读取并按文件版本重载能力配置，保持真实配置优先级，不让占位对象意外开启可选能力。

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import CapabilityConfig, load_capability_config
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
#   快照缓存回 agent（副作用）。缓存里不是 CapabilityConfig 的对象一律不认，
#   改走文件加载，防止非配置对象的属性被当成开关（2026-09-27 能力包合入时发现）。
#   文件不存在是常态（生产 Gateway 的 owner home 下通常没有这份配置）：返回 CapabilityConfig() 默认实例且不缓存，
#   之后建好文件时下一次调用就能读到；只有读取失败或格式错误才返回 None，限流类调用方仍要自己落到默认值。
#   新增运行时读 capability 配置的地方应一律走这里，不要从 agent.config 或 capability_router.config 上找。
# 函数用途: 运行时想读 capability_config.yaml 里的开关时，从 agent 拿配置对象；没有配置文件时给默认值。
def capability_config_for_agent(agent: object):
    snapshot = getattr(agent, "_capability_config_runtime_snapshot", None)
    config = getattr(snapshot, "config", None)
    # 只认真正的 CapabilityConfig：替身对象上自动生成的属性不是配置，不能让它的“真值”打开能力开关。
    if isinstance(config, CapabilityConfig):
        return config
    path = Path(
        str(getattr(agent, "capability_config_path", "") or "")
        or default_capability_config_path(getattr(agent, "root", "."))
    )
    try:
        snapshot = load_capability_config_snapshot(path)
    except FileNotFoundError:
        return CapabilityConfig()
    except (OSError, TypeError, ValueError):
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
