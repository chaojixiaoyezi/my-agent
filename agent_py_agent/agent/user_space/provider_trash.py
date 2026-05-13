# LLM: Provider trash helpers keep destructive actions recoverable and scoped without growing provider_space.py.
# 模块用途: 管理外部用户/群空间的 trash 路径、移动、审计和保留期清理。

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# LLM: ProviderTrashResult records non-destructive deletion moves for audit and recovery.
# 类用途: 描述一次移动到 trash 的结果，方便后续写审计日志或恢复。
@dataclass(frozen=True)
class ProviderTrashResult:
    original: Path
    trashed: Path
    moved: bool


# LLM: ProviderTrashRequest keeps provider destructive actions behind a typed bundle.
# 类用途: 打包一次 provider 空间删除到 trash 的输入，避免业务接口继续增加散乱参数。
@dataclass(frozen=True)
class ProviderTrashRequest:
    paths: Any
    target: Path
    actor_id: str = ""
    reason: str = ""
    date: str | None = None


# LLM: trash_target_for keeps destructive actions recoverable and scoped to the same external space.
# 函数用途: 为删除目标生成同空间 trash/date 下的恢复路径。
def trash_target_for(paths: Any, target: str | Path, *, date: str | None = None) -> Path:
    target_path = Path(target)
    name = _safe_trash_name(target_path.name)
    trash_date = date or date_type.today().isoformat()
    candidate = paths.trash_dir / trash_date / name
    counter = 1
    while candidate.exists():
        candidate = paths.trash_dir / trash_date / f"{target_path.stem}-{counter}{target_path.suffix}"
        counter += 1
    return candidate


# LLM: move_to_space_trash implements non-destructive delete without crossing provider boundaries.
# 函数用途: 把 provider 空间内的文件或目录移动到本空间 trash，替代直接删除。
def move_to_space_trash(request: ProviderTrashRequest) -> ProviderTrashResult:
    source = Path(request.target)
    if not _path_is_within_space(source, request.paths):
        raise ValueError(f"target is outside provider space: {source}")
    destination = trash_target_for(request.paths, source, date=request.date)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    result = ProviderTrashResult(original=source, trashed=destination, moved=True)
    record_provider_space_event(
        request.paths,
        event_type="move_to_trash",
        actor_id=request.actor_id,
        payload={"original": str(source), "trashed": str(destination), "reason": request.reason},
    )
    return result


# LLM: provider_trash_retention_days_from_agent_config keeps cleanup retention user-configurable.
# 函数用途: 从 AgentConfig 或兼容对象读取 provider trash 保留天数。
def provider_trash_retention_days_from_agent_config(config: object) -> int:
    return max(0, int(getattr(config, "provider_space_trash_retention_days", 30) or 0))


# LLM: provider_destructive_actions_use_trash_from_agent_config exposes the safety switch without hard-coding callers.
# 函数用途: 从配置读取外部用户/群空间破坏性操作是否走 trash。
def provider_destructive_actions_use_trash_from_agent_config(config: object) -> bool:
    return bool(getattr(config, "provider_space_destructive_actions_use_trash", True))


# LLM: purge_provider_trash removes only dated trash folders inside one provider space.
# 函数用途: 按保留天数清理当前外部用户/群空间的旧 trash 日期目录。
def purge_provider_trash(paths: Any, *, retention_days: int, today: str | date_type | None = None) -> list[Path]:
    days = max(0, int(retention_days or 0))
    if days == 0 or not paths.trash_dir.exists():
        return []
    cutoff = (_coerce_today(today) or date_type.today()) - timedelta(days=days - 1)
    deleted: list[Path] = []
    for child in sorted(paths.trash_dir.iterdir()):
        if _should_purge_trash_folder(child, paths, cutoff):
            shutil.rmtree(child)
            deleted.append(child)
    return deleted


# LLM: provider_audit_log_path scopes provider audit events to the same user/group space.
# 函数用途: 返回外部用户/群空间自己的审计流水文件路径。
def provider_audit_log_path(paths: Any) -> Path:
    return paths.data_dir / "audit.jsonl"


# LLM: record_provider_space_event writes refs-only provider events without touching owner global logs.
# 函数用途: 向外部用户/群空间自己的审计文件追加一条 JSONL 事件。
def record_provider_space_event(
    paths: Any,
    *,
    event_type: str,
    actor_id: str = "",
    payload: dict[str, object] | None = None,
) -> None:
    provider_audit_log_path(paths).parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "provider": paths.identity.provider,
        "space_type": paths.identity.space_type,
        "space_id": paths.identity.space_id,
        "actor_id": actor_id,
        "payload": payload or {},
    }
    with provider_audit_log_path(paths).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: _should_purge_trash_folder keeps purge_provider_trash shallow and auditable.
# 函数用途: 判断一个 trash 子目录是否在当前空间内且已超过保留期。
def _should_purge_trash_folder(path: Path, paths: Any, cutoff: date_type) -> bool:
    if not path.is_dir() or not _path_is_within_space(path, paths):
        return False
    folder_date = _date_from_folder(path)
    return bool(folder_date is not None and folder_date < cutoff)


# LLM: _path_is_within_space guards provider trash helpers without importing provider_space.py.
# 函数用途: 判断目标路径是否仍在当前 provider user/group 空间内。
def _path_is_within_space(path: str | Path, paths: Any) -> bool:
    try:
        Path(path).resolve().relative_to(paths.root_dir.resolve())
        return True
    except ValueError:
        return False


# LLM: _safe_trash_name preserves human-readable names while preventing empty trash paths.
# 函数用途: 生成 trash 里的安全文件名。
def _safe_trash_name(value: str) -> str:
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in str(value or ""))
    result = result.strip(".-_/")
    return result or "item"


# LLM: _coerce_today lets retention tests and future schedulers use deterministic dates.
# 函数用途: 解析 retention 清理传入的 today 覆盖值。
def _coerce_today(value: str | date_type | None) -> date_type | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        try:
            return date_type.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# LLM: _date_from_folder treats only YYYY-MM-DD trash folders as retention-managed.
# 函数用途: 从 trash 子目录名解析日期，非日期目录不自动删除。
def _date_from_folder(path: Path) -> date_type | None:
    try:
        return date_type.fromisoformat(path.name)
    except ValueError:
        return None
