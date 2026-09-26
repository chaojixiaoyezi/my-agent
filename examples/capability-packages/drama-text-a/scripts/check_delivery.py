# LLM: 能力包 A 的私有校验资源，核对原文字节、引用及声明时长；变更须同步包方法和 duration/examples 组件测试，不改变宿主任务状态。
# 模块用途: 只读原文与交付，向标准输出报告编号和时长对账问题；来源语义、创作质量及真实媒体仍需独立审阅。

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable
from pathlib import Path

MAX_INPUT_BYTES = 4 * 1024 * 1024
DURATION_REL_TOL = 1e-12


# LLM: 保留唯一 JSON 键，避免重复字段在模型输出与校验器之间产生不同含义。
# 函数用途: 拒绝同一对象中的重复字段。
def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


# LLM: JSON 非有限数不能成为时长或计量事实。
# 函数用途: 拒绝 NaN 和 Infinity。
def reject_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非有限数：{value}")


# LLM: 只读取显式输入，返回同次读取字节供来源摘要核对；不写文件或展开其中路径。
# 函数用途: 有界读取一份严格 JSON 输入。
def read_document(path: Path) -> tuple[object, bytes]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("输入超过 4 MiB，请先按章节拆分")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant), raw


# LLM: ID 只在这份领域资料中定位，不充当宿主 task/run 身份；坏行不参与后续引用解析。
# 函数用途: 收集有唯一编号的条目，并记录类型和重复编号问题。
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


# LLM: 引用必须显式存在，不从正文中猜测角色、场次或原文编号。
# 函数用途: 核对一组外键并返回有效引用，供覆盖统计使用。
def references(row: dict, key: str, known: dict, at: str, errors: list[dict]) -> set[str]:
    values = row.get(key)
    if not isinstance(values, list):
        errors.append({"code": "references_required", "path": f"{at}.{key}"})
        return set()
    valid = set()
    for value in values:
        if not isinstance(value, str) or value not in known:
            errors.append({"code": "unknown_reference", "path": f"{at}.{key}"})
        else:
            valid.add(value)
    return valid


# LLM: 正数转换只服务本包计量，布尔值、非有限数和无法用浮点表示的大整数均无有效秒数。
# 函数用途: 读取可计算的正时长，非法输入返回 None，避免数值溢出变成 traceback。
def positive_seconds(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and result > 0 else None


# LLM: 保留原 positive_seconds_required 错误合同；非法项只在失败报告求和时贡献零，不能当作已验证零秒。
# 函数用途: 校验场次或镜头的 seconds，向当前报告记录类型或数值错误。
def seconds(row: dict, at: str, errors: list[dict]) -> float:
    value = positive_seconds(row.get("seconds"))
    if value is None:
        errors.append({"code": "positive_seconds_required", "path": at})
        return 0.0
    return value


# LLM: 各项已由 seconds 校验；汇总溢出保留原结构错误，不输出 JSON Infinity 或推断目标已达成。
# 函数用途: 稳定求和并将无法表示的总时长标成未知。
def duration_sum(values: Iterable[float], at: str, errors: list[dict]) -> float | None:
    try:
        return math.fsum(values)
    except OverflowError:
        errors.append({"code": "duration_sum_overflow", "path": at})
        return None


# LLM: 只比较唯一资料中的显式秒数；容差仅吸收数值舍入，不按故事题目放宽目标，也不验证对白或媒体时长。
# 函数用途: 按场次汇总真实镜头条目，并对照场次声明及原文里实际提供的目标时长。
def duration_metrics(source: dict, scenes: dict, shots: dict, errors: list[dict]) -> dict:
    scene_values = {identifier: seconds(row, identifier, errors) for identifier, row in scenes.items()}
    shot_values = {identifier: seconds(row, identifier, errors) for identifier, row in shots.items()}
    grouped = {identifier: [] for identifier in scenes}
    for identifier, shot in shots.items():
        scene_id = shot.get("scene_id")
        if isinstance(scene_id, str) and scene_id in grouped:
            grouped[scene_id].append(shot_values[identifier])
    per_scene = {identifier: duration_sum(values, f"{identifier}.shots", errors)
                 for identifier, values in grouped.items()}
    for identifier, actual in per_scene.items():
        declared = scene_values[identifier]
        if declared > 0 and actual is not None and not math.isclose(
            actual, declared, rel_tol=DURATION_REL_TOL, abs_tol=0.0,
        ):
            errors.append({"code": "scene_duration_mismatch", "path": f"{identifier}.seconds",
                           "declared_seconds": declared, "shot_seconds": actual})
    scene_total = duration_sum(scene_values.values(), "scenes", errors)
    shot_total = duration_sum(shot_values.values(), "shots", errors)
    target_declared = "target_seconds" in source
    target = positive_seconds(source.get("target_seconds")) if target_declared else None
    if target_declared and target is None:
        errors.append({"code": "positive_target_seconds_required", "path": "source.target_seconds"})
    if target is not None and shot_total is not None and not math.isclose(
        shot_total, target, rel_tol=DURATION_REL_TOL, abs_tol=0.0,
    ):
        errors.append({"code": "target_duration_mismatch", "path": "source.target_seconds",
                       "target_seconds": target, "shot_seconds": shot_total})
    return {"scene_seconds": scene_total, "shot_seconds": shot_total, "scene_shot_seconds": per_scene,
            "target_declared": target_declared, "target_seconds": target,
            "target_delta_seconds": shot_total - target if shot_total is not None and target is not None else None}


# LLM: 本包只核对交付结构、原文字节、引用覆盖及声明时长；真实运行结果由调用方如实报告，不能升级为内容或宿主完成授权。
# 函数用途: 查找漏依据、串角色、坏场次引用与时长账不一致，创作新增是否忠实仍交给独立阅读。
def check_delivery(source: object, delivery: object, source_sha256: str) -> dict:
    errors, warnings = [], []
    if not isinstance(source, dict) or not isinstance(delivery, dict):
        return {"structure_valid": False, "errors": [{"code": "object_required", "path": "$"}]}
    if source.get("schema") != "drama_text_source.v1" or delivery.get("schema") != "drama_text_delivery.v1":
        errors.append({"code": "unsupported_schema", "path": "schema"})
    if delivery.get("source_sha256") != source_sha256:
        errors.append({"code": "source_digest_mismatch", "path": "source_sha256"})
    passages = index_rows(source, "passages", errors)
    cast = index_rows(delivery, "cast", errors)
    scenes = index_rows(delivery, "scenes", errors)
    shots = index_rows(delivery, "shots", errors)
    for name, rows in (("passages", passages), ("scenes", scenes), ("shots", shots)):
        if not rows:
            errors.append({"code": "nonempty_required", "path": name})
    for identifier, row in passages.items():
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            errors.append({"code": "source_text_required", "path": identifier})
    covered, scene_characters = set(), {}
    for identifier, row in scenes.items():
        refs = references(row, "source_ids", passages, identifier, errors)
        if not refs:
            errors.append({"code": "source_reference_required", "path": identifier})
        covered.update(refs)
        scene_characters[identifier] = references(row, "character_ids", cast, identifier, errors)
    filmed = set()
    for identifier, row in shots.items():
        scene_id = row.get("scene_id")
        if not isinstance(scene_id, str) or scene_id not in scenes:
            errors.append({"code": "unknown_scene", "path": identifier})
        else:
            filmed.add(scene_id)
            references(row, "visible_character_ids", {x: {} for x in scene_characters[scene_id]}, identifier, errors)
        for key in ("start_state", "action", "end_state"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append({"code": "shot_state_required", "path": f"{identifier}.{key}"})
    omissions = delivery.get("omitted_passages", [])
    if not isinstance(omissions, list):
        errors.append({"code": "list_required", "path": "omitted_passages"})
        omissions = []
    omitted = set()
    for row in omissions:
        identifier = row.get("source_id") if isinstance(row, dict) else None
        if (not isinstance(identifier, str) or identifier not in passages or identifier in omitted
                or identifier in covered or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            errors.append({"code": "invalid_omission", "path": "omitted_passages"})
        else:
            omitted.add(identifier)
    for identifier in sorted(set(passages) - covered - omitted):
        errors.append({"code": "unaccounted_source", "path": identifier})
    for identifier in sorted(set(scenes) - filmed):
        errors.append({"code": "unfilmed_scene", "path": identifier})
    if omitted:
        warnings.append({"code": "explicit_omissions_need_review", "count": len(omitted)})
    durations = duration_metrics(source, scenes, shots, errors)
    warnings.append({"code": "creative_quality_and_media_not_checked"})
    return {"schema": "drama_text_check.v1", "structure_valid": not errors, "errors": errors,
            "warnings": warnings, "metrics": {"passages": len(passages), "covered_passages": len(covered),
            "omitted_passages": len(omitted), "scenes": len(scenes), "shots": len(shots), **durations}}


# LLM: 开发组件入口只读两份明确输入并打印报告；正式产品调用须由宿主物化和原工具授权接线。
# 函数用途: 从命令行运行文本资料检查，退出码仅表示本脚本结构检查结果。
def main() -> int:
    parser = argparse.ArgumentParser(description="核对短剧文本依据与分镜引用，不评价成片质量")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        source, raw = read_document(arguments.source)
        delivery, _ = read_document(arguments.delivery)
        result = check_delivery(source, delivery, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError) as exc:
        result = {"structure_valid": False, "errors": [{"code": "invalid_input", "message": str(exc)}]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
