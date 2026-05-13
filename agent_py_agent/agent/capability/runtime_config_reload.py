# LLM: Runtime config reload helpers keep watch/dispatch hot reload small and testable.
# 模块用途: 处理 capability_config 默认路径、内容版本、快照加载和 router 热更新。

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import load_capability_config
from .runtime_config_models import CapabilityConfigReloadResult, CapabilityConfigSnapshot


# LLM: default_capability_config_path lets tools find the project config without asking users.
# 函数用途: 根据 SimpleAgent root 推断 capability_config.yaml 默认路径，找不到时返回最常见仓库路径。
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


# LLM: capability_config_version returns a content hash used as the concurrency token.
# 函数用途: 计算配置文件内容版本，补丁写入前用它避免覆盖用户或其他 agent 的并行修改。
def capability_config_version(config_path: str | Path) -> str:
    path = Path(config_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


# LLM: load_capability_config_snapshot reads both config values and a stable file version.
# 函数用途: 加载 capability_config 及文件元信息，供 watch/dispatch 后续判断是否需要热加载。
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


# LLM: reload_capability_config_if_changed refreshes config and router cache for future dispatch cycles only.
# 函数用途: 如果配置文件内容变化，则重新加载并更新 router.config；不会修改已经运行中的子代理上下文。
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
