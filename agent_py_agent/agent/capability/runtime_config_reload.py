
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
