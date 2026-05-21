# LLM: Structured JSON merge helpers preserve machine checkpoint evidence across repair writes.
# 模块用途: 为 write_structured_json 提供 rows/sheets/source_refs/claims 的结构化合并能力。

from __future__ import annotations

from collections.abc import Callable

IdentityFn = Callable[[object], object | None]


# LLM: merge_json_objects gives checkpoint writes append semantics for tabular data.
# 函数用途: 合并 metadata 时保留旧字段；rows/sheets 用追加/同名 sheet 合并，证据列表按机器身份去重。
def merge_json_objects(existing: dict[str, object], value: dict[str, object]) -> dict[str, object]:
    merged = {**existing, **value}
    if isinstance(existing.get("rows"), list) and isinstance(value.get("rows"), list):
        merged["rows"] = [*existing["rows"], *value["rows"]]
    if isinstance(existing.get("sheets"), list) and isinstance(value.get("sheets"), list):
        merged["sheets"] = _merge_sheet_lists(existing["sheets"], value["sheets"])
    if isinstance(existing.get("source_refs"), list) and isinstance(value.get("source_refs"), list):
        merged["source_refs"] = _merge_identity_lists(
            existing["source_refs"],
            value["source_refs"],
            identity=_source_ref_identity,
        )
    if isinstance(existing.get("claims"), list) and isinstance(value.get("claims"), list):
        merged["claims"] = _merge_identity_lists(existing["claims"], value["claims"], identity=_claim_identity)
    return merged


# LLM: _merge_identity_lists preserves earlier evidence and applies later structured refinements by identity.
# 函数用途: 合并 source_refs/claims 这类证据列表，避免 evidence repair 覆盖已有机器证据。
def _merge_identity_lists(
    existing: list[object],
    incoming: list[object],
    *,
    identity: IdentityFn,
) -> list[object]:
    merged = [_copied_mapping(item) for item in existing]
    by_key = _identity_index(merged, identity)
    for item in incoming:
        _upsert_identity_item(merged, by_key, _copied_mapping(item), identity)
    return merged


# LLM: _identity_index builds stable list positions from machine identity keys.
# 函数用途: 建立 source_id、claim_id 等结构字段到列表下标的映射。
def _identity_index(items: list[object], identity: IdentityFn) -> dict[object, int]:
    index: dict[object, int] = {}
    for position, item in enumerate(items):
        key = identity(item)
        if key is not None:
            index[key] = position
    return index


# LLM: _upsert_identity_item updates an existing evidence item or appends a new one.
# 函数用途: 按机器身份做 upsert；dict 项合并字段，非 dict 项整体替换或追加。
def _upsert_identity_item(
    merged: list[object],
    by_key: dict[object, int],
    item: object,
    identity: IdentityFn,
) -> None:
    key = identity(item)
    if key is None:
        merged.append(item)
        return
    if key not in by_key:
        by_key[key] = len(merged)
        merged.append(item)
        return
    current = merged[by_key[key]]
    merged[by_key[key]] = {**current, **item} if isinstance(current, dict) and isinstance(item, dict) else item


# LLM: _copied_mapping avoids mutating caller-owned evidence dicts.
# 函数用途: dict 项浅拷贝，其他 JSON 值保持原值。
def _copied_mapping(item: object) -> object:
    return dict(item) if isinstance(item, dict) else item


# LLM: _source_ref_identity treats structured source_id as the stable source reference key.
# 函数用途: 为 source_refs 合并生成机器身份，不从自然语言描述中推断。
def _source_ref_identity(item: object) -> object | None:
    if not isinstance(item, dict):
        return None
    source_id = str(item.get("source_id") or "").strip()
    if source_id:
        return ("source_id", source_id)
    uri = str(item.get("uri") or "").strip()
    return ("uri", uri) if uri else None


# LLM: _claim_identity uses claim_id when present, otherwise field/value/source_ids form a stable claim key.
# 函数用途: 为 claims 合并生成机器身份，支持补证据时追加缺失字段并去重。
def _claim_identity(item: object) -> object | None:
    if not isinstance(item, dict):
        return None
    claim_id = str(item.get("claim_id") or "").strip()
    if claim_id:
        return ("claim_id", claim_id)
    return _claim_fallback_identity(item)


# LLM: _claim_fallback_identity creates a deterministic claim key from structured fields.
# 函数用途: 没有 claim_id 时，用 field/value/source_ids 组成机器身份。
def _claim_fallback_identity(item: dict[str, object]) -> object | None:
    field = str(item.get("field") or "").strip()
    value = str(item.get("value") or "").strip()
    sources = item.get("source_ids")
    source_ids = tuple(str(source_id).strip() for source_id in sources if str(source_id).strip()) if isinstance(sources, list) else ()
    if not field and not value and not source_ids:
        return None
    return ("claim", field, value, source_ids)


# LLM: _merge_sheet_lists uses sheet names as stable structural IDs when available.
# 函数用途: 不解析普通文本；只按 sheet.name 结构字段合并同名表，否则追加新表。
def _merge_sheet_lists(existing: list[object], incoming: list[object]) -> list[object]:
    merged = [_copied_mapping(sheet) for sheet in existing]
    by_name = _sheet_name_index(merged)
    for sheet in incoming:
        _upsert_sheet(merged, by_name, sheet)
    return merged


# LLM: _sheet_name_index maps declared sheet names to positions.
# 函数用途: 只用 sheet.name 结构字段，不猜测表格语义。
def _sheet_name_index(items: list[object]) -> dict[str, int]:
    return {
        name: index
        for index, sheet in enumerate(items)
        if isinstance(sheet, dict) and (name := str(sheet.get("name") or "").strip())
    }


# LLM: _upsert_sheet merges same-name sheet chunks and appends unnamed/new sheets.
# 函数用途: 支持分批写同一 sheet，同时避免跨 sheet 误合并。
def _upsert_sheet(merged: list[object], by_name: dict[str, int], sheet: object) -> None:
    if not isinstance(sheet, dict):
        merged.append(sheet)
        return
    name = str(sheet.get("name") or "").strip()
    if name and name in by_name and isinstance(merged[by_name[name]], dict):
        merged[by_name[name]] = _merge_sheet(merged[by_name[name]], sheet)
        return
    if name:
        by_name[name] = len(merged)
    merged.append(dict(sheet))


# LLM: _merge_sheet appends rows while allowing later chunks to refresh columns/metadata.
# 函数用途: 同一 sheet 分批写入时追加 rows，其他结构字段按后来的 chunk 覆盖。
def _merge_sheet(existing: dict[str, object], incoming: dict[str, object]) -> dict[str, object]:
    merged = {**existing, **incoming}
    if isinstance(existing.get("rows"), list) and isinstance(incoming.get("rows"), list):
        merged["rows"] = [*existing["rows"], *incoming["rows"]]
    return merged


__all__ = ["merge_json_objects"]
