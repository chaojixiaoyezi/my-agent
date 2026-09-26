# LLM: 本包仅归并用户提供的授权范围和既有证据；不扫描、不发网络请求、不验证或利用漏洞。
# 模块用途: 核对证据摘要、资产归属和引用，再按同一资产及内容摘要去重，输出待复核报告数据。

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


# LLM: 输入字段重复必须拒绝，避免授权范围和证据在不同解析器间产生歧义。
# 函数用途: 从 JSON 键值对构造不含重复字段的对象。
def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


# LLM: 本数据协议使用严格 JSON，不接受非标准数字。
# 函数用途: 拒绝 NaN 和 Infinity。
def reject_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非有限数：{value}")


# LLM: 只读取参数指向的数据文件，source_ref 和资产标识均不作为文件或网络入口。
# 函数用途: 有界读取已有证据，不跟随证据正文里的命令和地址。
def read_document(path: Path) -> object:
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("输入超过 4 MiB，请按资产拆分")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


# LLM: 证据与发现编号是输入内定位符，不充当宿主权限；重复编号会使后续对账失去唯一性。
# 函数用途: 核对数据行并按唯一编号建索引。
def index_rows(document: dict, key: str, errors: list[dict]) -> dict[str, dict]:
    rows = document.get(key)
    if not isinstance(rows, list):
        errors.append({"code": "list_required", "path": key})
        return {}
    result = {}
    for position, row in enumerate(rows):
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or identifier in result:
            errors.append({"code": "invalid_or_duplicate_id", "path": f"{key}[{position}]"})
        else:
            result[identifier] = row
    return result


# LLM: 范围仅核对输入的结构和资产集合，不能把授权引用的存在当成已验证授权。
# 函数用途: 检查本次资料限定为既有证据整理，并返回允许出现的资产编号。
def scope_assets(document: dict, errors: list[dict]) -> set[str]:
    scope = document.get("scope")
    if not isinstance(scope, dict):
        errors.append({"code": "scope_required", "path": "scope"})
        return set()
    reference = scope.get("authorization_ref")
    if not isinstance(reference, str) or not reference.strip():
        errors.append({"code": "authorization_reference_required", "path": "scope.authorization_ref"})
    if scope.get("allowed_operations") != ["review_existing_evidence"]:
        errors.append({"code": "unsupported_operation_scope", "path": "scope.allowed_operations"})
    assets = scope.get("asset_ids")
    if (not isinstance(assets, list) or not assets
            or any(not isinstance(asset, str) or not asset.strip() for asset in assets)):
        errors.append({"code": "asset_scope_required", "path": "scope.asset_ids"})
        return set()
    return set(assets)


# LLM: 哈希只证明输入字节一致，报告不会把它提升为漏洞已验证；去重键含资产，保留全部来源别名。
# 函数用途: 检查既有证据归属及引用，输出去重资料和明确尚未执行验证的发现条目。
def summarize(document: object) -> dict:
    errors = []
    if not isinstance(document, dict):
        return {"structure_valid": False, "errors": [{"code": "object_required", "path": "$"}]}
    if document.get("schema") != "security_evidence_input.v1":
        errors.append({"code": "unsupported_schema", "path": "schema"})
    assets = scope_assets(document, errors)
    evidence = index_rows(document, "evidence", errors)
    findings = index_rows(document, "findings", errors)
    for identifier, row in evidence.items():
        if not isinstance(row.get("asset_id"), str) or row["asset_id"] not in assets:
            errors.append({"code": "asset_outside_scope", "path": identifier})
        content = row.get("content")
        if not isinstance(content, str) or row.get("sha256") != hashlib.sha256(content.encode()).hexdigest():
            errors.append({"code": "evidence_digest_mismatch", "path": identifier})
        if not isinstance(row.get("source_ref"), str) or not row["source_ref"].strip():
            errors.append({"code": "source_reference_required", "path": identifier})
    for identifier, row in findings.items():
        for key in ("title", "claim", "recommendation"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append({"code": "finding_text_required", "path": f"{identifier}.{key}"})
        if not isinstance(row.get("asset_id"), str) or row["asset_id"] not in assets:
            errors.append({"code": "asset_outside_scope", "path": identifier})
        refs = row.get("evidence_ids")
        if not isinstance(refs, list) or not refs:
            errors.append({"code": "evidence_references_required", "path": identifier})
            continue
        for ref in refs:
            if not isinstance(ref, str) or ref not in evidence:
                errors.append({"code": "unknown_evidence", "path": identifier})
            elif evidence[ref].get("asset_id") != row.get("asset_id"):
                errors.append({"code": "cross_asset_evidence", "path": identifier})
    result = {"schema": "security_evidence_summary.v1", "structure_valid": not errors,
              "errors": errors, "authorization_verified": False, "network_actions": 0,
              "verification": "not_performed", "grouped_evidence": [], "findings": []}
    if errors:
        return result
    groups, aliases = {}, {}
    for identifier, row in sorted(evidence.items()):
        key = (row["asset_id"], row["sha256"])
        group = groups.setdefault(key, {"id": identifier, "asset_id": row["asset_id"],
                                   "sha256": row["sha256"], "aliases": [], "source_refs": []})
        group["aliases"].append(identifier)
        if row["source_ref"] not in group["source_refs"]:
            group["source_refs"].append(row["source_ref"])
        aliases[identifier] = group["id"]
    result["grouped_evidence"] = list(groups.values())
    result["findings"] = [{"id": identifier, "asset_id": row["asset_id"], "title": row.get("title", ""),
                           "claim": row["claim"],
                           "evidence_ids": sorted({aliases[ref] for ref in row["evidence_ids"]}),
                           "verification": "not_performed", "recommendation": row.get("recommendation", "")}
                          for identifier, row in findings.items()]
    return result


# LLM: 开发 CLI 只有数据读取与 stdout 输出；宿主使用时仍须沿当前权限及原操作记录，不执行证据正文。
# 函数用途: 把一份已有证据文件整理为待验证报告数据。
def main() -> int:
    parser = argparse.ArgumentParser(description="整理既有安全证据；不扫描、不验证漏洞")
    parser.add_argument("--input", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = summarize(read_document(arguments.input))
    except (OSError, ValueError) as exc:
        result = {"structure_valid": False, "errors": [{"code": "invalid_input", "message": str(exc)}]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
