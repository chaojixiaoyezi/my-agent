# LLM: Collection mapping checks verify source items appear in final artifacts.
# 模块用途: 校验 source JSON 中的结构化条目是否能按 key_fields 映射到最终产物文本，避免“源数据有了但成品漏项”。

from __future__ import annotations

import json
import re
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding
from .artifact_structured_contracts import positive_int, string_list

_COVERAGE_LEVELS = {
    "missing": -1,
    "metadata_only": 0,
    "summary": 1,
    "body": 2,
    "translated_body": 3,
    "tabular_row": 4,
}


# LLM: mapping_findings 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 mapping findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
def mapping_findings(
    items: list[object],
    contract: dict[str, object],
    source_ref: str,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    mapping = contract.get("mapping")
    if not isinstance(mapping, dict):
        return []
    artifact_ref = str(mapping.get("artifact_ref") or "").strip()
    key_fields = string_list(mapping.get("key_fields"))
    if not artifact_ref or not key_fields:
        return [_finding("ARTIFACT_MAPPING_CONTRACT_INVALID", "mapping requires artifact_ref and key_fields.", source_ref)]
    artifact_path = _workspace_path(artifact_ref, workspace_root)
    if not _inside_workspace(artifact_path, workspace_root):
        return [_finding("ARTIFACT_MAPPING_OUTSIDE_WORKSPACE", "mapping artifact is outside workspace_root.", artifact_ref)]
    try:
        text = artifact_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [_finding("ARTIFACT_MAPPING_TARGET_MISSING", "mapping artifact does not exist.", artifact_ref)]
    mapped_items = [item for item in items if isinstance(item, dict) and _item_mapped(item, key_fields, text)]
    required = positive_int(mapping.get("min_mapped_items")) or len(items)
    if len(mapped_items) >= required:
        coverage_finding = _coverage_depth_finding(items, key_fields, text, mapping, artifact_ref)
        return [coverage_finding] if coverage_finding is not None else []
    missing_keys = [
        _item_key_values(item, key_fields)
        for item in items
        if isinstance(item, dict) and not _item_mapped(item, key_fields, text)
    ]
    return [
        _finding(
            "ARTIFACT_MAPPING_MISSING",
            f"artifact maps {len(mapped_items)} source items, expected at least {required}.",
            artifact_ref,
            _compact_json({"mapped": len(mapped_items), "required": required, "missing_keys": missing_keys[:10]}),
        )
    ]


# LLM: _item_mapped 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item mapped 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_mapped(item: dict[str, object], key_fields: list[str], text: str) -> bool:
    keys = [str(item.get(field) or "").strip() for field in key_fields]
    return bool(keys) and all(key and key in text for key in keys)


# LLM: _coverage_depth_finding upgrades title matching into declared source coverage levels.
# 函数用途: 当合同要求 body/translated_body/tabular_row 时，不让 key 字符串出现冒充正文覆盖。
def _coverage_depth_finding(
    items: list[object],
    key_fields: list[str],
    text: str,
    mapping: dict[str, object],
    artifact_ref: str,
) -> ArtifactFinding | None:
    required_level = str(mapping.get("min_coverage_level") or "metadata_only").strip()
    required_rank = _COVERAGE_LEVELS.get(required_level, 0)
    if required_rank <= _COVERAGE_LEVELS["metadata_only"]:
        return None
    min_body_chars = positive_int(mapping.get("min_body_chars")) or 120
    min_summary_chars = positive_int(mapping.get("min_summary_chars")) or 40
    shallow = [
        _coverage_record(item, key_fields, text, min_summary_chars=min_summary_chars, min_body_chars=min_body_chars)
        for item in items
        if isinstance(item, dict)
    ]
    failed = [record for record in shallow if _COVERAGE_LEVELS.get(str(record.get("coverage_kind")), -1) < required_rank]
    if not failed:
        return None
    return _finding(
        "ARTIFACT_MAPPING_COVERAGE_TOO_SHALLOW",
        "artifact source coverage is shallower than the declared contract.",
        artifact_ref,
        _compact_json(
            {
                "required_coverage_level": required_level,
                "failed_items": failed[:10],
            }
        ),
    )


# LLM: _coverage_record computes one source item's current coverage without guessing task semantics.
# 函数用途: 基于 key 字段附近的结构文本量和目标语言字符统计，输出 metadata/summary/body 等机器级别。
def _coverage_record(
    item: dict[str, object],
    key_fields: list[str],
    text: str,
    *,
    min_summary_chars: int,
    min_body_chars: int,
) -> dict[str, object]:
    keys = [str(item.get(field) or "").strip() for field in key_fields if str(item.get(field) or "").strip()]
    source_key = "|".join(keys)
    if not keys or not all(key in text for key in keys):
        return {
            "source_key": source_key,
            "coverage_kind": "missing",
            "status": "missing",
            "current_state": {"matched_keys": [key for key in keys if key in text]},
        }
    body = _text_after_first_key(text, keys)
    chars = _meaningful_chars(body)
    cjk_chars = _cjk_count(body)
    kind = "metadata_only"
    if chars >= min_summary_chars:
        kind = "summary"
    if chars >= min_body_chars:
        kind = "body"
    if kind == "body" and cjk_chars >= min(20, max(4, min_body_chars // 4)):
        kind = "translated_body"
    return {
        "source_key": source_key,
        "coverage_kind": kind,
        "status": "covered" if kind in {"body", "translated_body", "tabular_row"} else "partial",
        "current_state": {"body_chars": chars, "cjk_chars": cjk_chars},
        "required_state": {"min_summary_chars": min_summary_chars, "min_body_chars": min_body_chars},
    }


def _text_after_first_key(text: str, keys: list[str]) -> str:
    positions = [text.find(key) for key in keys if text.find(key) >= 0]
    if not positions:
        return ""
    start = min(positions)
    window = text[start: start + 800]
    following_heading = re.search(r"\n\s*#{1,6}\s+", window[1:])
    if following_heading:
        window = window[: following_heading.start() + 1]
    for key in keys:
        window = window.replace(key, " ")
    return window


def _meaningful_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace() and char not in "#*_`|-")


def _cjk_count(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


# LLM: _item_key_values 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item key values 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_key_values(item: dict[str, object], key_fields: list[str]) -> dict[str, str]:
    return {field: str(item.get(field) or "").strip() for field in key_fields if str(item.get(field) or "").strip()}


# LLM: _workspace_path 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 workspace path 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _workspace_path(ref: str, workspace_root: Path) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else (workspace_root / path).resolve()


# LLM: _inside_workspace 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 inside workspace 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _inside_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace_root.resolve(strict=False))
        return True
    except ValueError:
        return False


# LLM: _finding 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 finding 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


# LLM: _compact_json 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 compact json 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["mapping_findings"]
