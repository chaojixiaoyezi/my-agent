# LLM: Runtime config patch IO keeps YAML rewrites, audit, and notices out of policy code.
# 模块用途: 提供 capability_config 补丁的窄 YAML 写入、审计 JSONL 和通知记录。

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .runtime_config_models import CapabilityConfigPatchRequest, CapabilityConfigPatchResult


# LLM: replace_yaml_fields performs narrow top-level scalar replacements while preserving comments.
# 函数用途: 只替换 capability_config 顶层字段行；缺失字段追加到文件末尾的托管区。
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


# LLM: atomic_write_text avoids partially written config files on interruption.
# 函数用途: 先写临时文件再原子替换目标文件，降低配置写一半的风险。
def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# LLM: write_config_patch_audit appends a durable JSONL record outside the config body.
# 函数用途: 记录谁因为什么改了哪些字段，方便后续排错和架构评审。
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


# LLM: write_config_patch_notice leaves a lightweight human/agent-readable broadcast trail.
# 函数用途: 写入配置变化通知，后续 watch/父代理/子代理可把它转发到 shared board 或 direct message。
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


# LLM: _append_missing_yaml_fields appends managed fields absent from the source file.
# 函数用途: 补写配置文件里没有的安全字段，保持新字段升级兼容。
def _append_missing_yaml_fields(rendered: list[str], changes: dict[str, object], found: set[str]) -> str:
    missing = [key for key in changes if key not in found]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Managed by capability config patch service")
        for key in missing:
            rendered.append(f"{key}: {_serialize_yaml_value(changes[key])}")
    return "\n".join(rendered).rstrip() + "\n"


# LLM: _top_level_yaml_key detects simple top-level key/value lines only.
# 函数用途: 返回 YAML 顶层字段名；注释、缩进行、空行和列表项都返回空字符串。
def _top_level_yaml_key(raw: str) -> str:
    stripped = raw.strip()
    if not stripped or raw[:1].isspace() or stripped.startswith("#") or ":" not in stripped:
        return ""
    return stripped.split(":", 1)[0].strip()


# LLM: _inline_comment carries an existing line comment to the rewritten scalar line.
# 函数用途: 保留 `key: value # comment` 里的注释；当前配置值都是简单标量，足够安全。
def _inline_comment(raw: str) -> str:
    if "#" not in raw:
        return ""
    return " #" + raw.split("#", 1)[1]


# LLM: _serialize_yaml_value keeps written values compatible with load_simple_yaml.
# 函数用途: 把 Python 值转成简化 YAML 标量；布尔使用小写，列表使用 JSON 行内格式。
def _serialize_yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


# LLM: _audit_payload keeps audit JSON shape stable for review and debugging.
# 函数用途: 生成 capability config patch 服务审计记录，不包含大正文。
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


# LLM: _notice_line renders one bounded config-change message.
# 函数用途: 生成配置变化通知行，供 markdown/blackboard/广播后续复用。
def _notice_line(
    request: CapabilityConfigPatchRequest,
    result: CapabilityConfigPatchResult,
) -> str:
    return (
        f"- config_changed fields={','.join(result.changed_fields)} "
        f"version={result.version_after} actor={request.actor} reason={request.reason}\n"
    )
