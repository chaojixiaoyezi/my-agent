
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .runtime_config_models import CapabilityConfigPatchRequest, CapabilityConfigPatchResult


def replace_yaml_fields(text: str, changes: dict[str, object]) -> str:
    found: set[str] = set()
    rendered: list[str] = []
    for raw in text.splitlines():
        key = _top_level_yaml_key(raw)
        if key in changes:
            rendered.append(f"{key}: {_serialize_yaml_value(changes[key])}{_inline_comment(raw)}")
            found.add(key)
            continue
        rendered.append(raw)
    return _append_missing_yaml_fields(rendered, changes, found)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_config_patch_audit(
    request: CapabilityConfigPatchRequest,
    result: CapabilityConfigPatchResult,
) -> None:
    if request.audit_path is None:
        return
    path = Path(request.audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_audit_payload(request, result), ensure_ascii=False, sort_keys=True) + "\n")


def write_config_patch_notice(
    request: CapabilityConfigPatchRequest,
    result: CapabilityConfigPatchResult,
) -> None:
    if request.notice_path is None:
        return
    path = Path(request.notice_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_notice_line(request, result))


def _append_missing_yaml_fields(rendered: list[str], changes: dict[str, object], found: set[str]) -> str:
    missing = [key for key in changes if key not in found]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Managed by capability config patch service")
        for key in missing:
            rendered.append(f"{key}: {_serialize_yaml_value(changes[key])}")
    return "\n".join(rendered).rstrip() + "\n"


def _top_level_yaml_key(raw: str) -> str:
    stripped = raw.strip()
    if not stripped or raw[:1].isspace() or stripped.startswith("#") or ":" not in stripped:
        return ""
    return stripped.split(":", 1)[0].strip()


def _inline_comment(raw: str) -> str:
    if "#" not in raw:
        return ""
    return " #" + raw.split("#", 1)[1]


def _serialize_yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _audit_payload(
    request: CapabilityConfigPatchRequest,
    result: CapabilityConfigPatchResult,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "capability_config_patch_service",
        "created_at": time.time(),
        "actor": request.actor,
        "reason": request.reason,
        "config_path": str(Path(request.config_path)),
        "changed_fields": result.changed_fields,
        "version_before": result.version_before,
        "version_after": result.version_after,
        "scope": request.scope,
    }


def _notice_line(
    request: CapabilityConfigPatchRequest,
    result: CapabilityConfigPatchResult,
) -> str:
    return (
        f"- config_changed fields={','.join(result.changed_fields)} "
        f"version={result.version_after} actor={request.actor} reason={request.reason}\n"
    )
