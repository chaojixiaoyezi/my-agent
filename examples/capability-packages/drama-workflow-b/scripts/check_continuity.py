# LLM: 能力包 B 的私有领域检查；只核对资料间的编号关系，不推断媒体存在、不启动模型或工具。
# 模块用途: 检查大纲、人物、美术、场次和分镜的连续性，输出 JSON 或已转义的静态核对报告。

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path


# LLM: 重复字段不能被最后一项悄悄覆盖；不接受非标准 JSON 常量。
# 函数用途: 严格解析对象成员，避免资料中同一字段有两个值。
def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


# LLM: NaN 或 Infinity 不能成为分镜时长事实。
# 函数用途: 拒绝非有限 JSON 数字。
def reject_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非有限数：{value}")


# LLM: 输入路径来自显式参数，资料内引用只作数据，不触发读文件或网络请求。
# 函数用途: 有界读取一份工作流资料，限制为 4 MiB。
def read_document(path: Path) -> object:
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("输入超过 4 MiB，请按剧集拆分")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


# LLM: 编号属于本包资料，不改变宿主身份或完成状态；任何坏行都保留结构化错误。
# 函数用途: 为每类资料建立唯一编号索引。
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


# LLM: 依赖只按显式 ID 核对，不根据人名、文件名或故事正文猜测。
# 函数用途: 核对场次角色、道具、节拍与参考资料等多值外键。
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


# LLM: 方法只验证跨表关系；参考图状态仍是声明，不是文件、画面或视频已经通过的证据。
# 函数用途: 找出剧集、场次、人物、道具、节拍与镜头之间的断链。
def check_project(project: object) -> dict:
    errors, warnings = [], []
    if not isinstance(project, dict):
        return {"structure_valid": False, "errors": [{"code": "object_required", "path": "$"}]}
    if project.get("schema") != "drama_workflow_project.v1":
        errors.append({"code": "unsupported_schema", "path": "schema"})
    catalog = {key: index_rows(project, key, errors) for key in
               ("episodes", "characters", "locations", "props", "scenes", "shots", "references")}
    for key in ("episodes", "scenes", "shots"):
        if not catalog[key]:
            errors.append({"code": "nonempty_required", "path": key})
    beat_catalog, scene_characters = {}, {}
    for identifier, scene in catalog["scenes"].items():
        for key, target in (("episode_id", "episodes"), ("location_id", "locations")):
            value = scene.get(key)
            if not isinstance(value, str) or value not in catalog[target]:
                errors.append({"code": "unknown_reference", "path": f"{identifier}.{key}"})
        scene_characters[identifier] = references(scene, "character_ids", catalog["characters"], identifier, errors)
        references(scene, "prop_ids", catalog["props"], identifier, errors)
        beats = index_rows(scene, "beats", errors)
        beat_catalog[identifier] = beats
        for beat_id, beat in beats.items():
            if not isinstance(beat.get("text"), str) or not beat["text"].strip():
                errors.append({"code": "beat_text_required", "path": beat_id})
            if beat.get("kind") == "dialogue":
                speaker = beat.get("character_id")
                if not isinstance(speaker, str) or speaker not in scene_characters[identifier]:
                    errors.append({"code": "speaker_outside_scene", "path": beat_id})
            elif beat.get("kind") != "action":
                errors.append({"code": "unknown_beat_kind", "path": beat_id})
    covered, total_seconds = {}, 0.0
    for identifier, shot in catalog["shots"].items():
        scene_id = shot.get("scene_id")
        if not isinstance(scene_id, str) or scene_id not in beat_catalog:
            errors.append({"code": "unknown_scene", "path": identifier})
        else:
            refs = references(shot, "beat_ids", beat_catalog[scene_id], identifier, errors)
            if not refs:
                errors.append({"code": "beat_reference_required", "path": identifier})
            covered.setdefault(scene_id, set()).update(refs)
        references(shot, "reference_ids", catalog["references"], identifier, errors)
        value = shot.get("seconds")
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            errors.append({"code": "positive_seconds_required", "path": identifier})
        else:
            total_seconds += value
    for scene_id, beats in beat_catalog.items():
        for beat_id in sorted(set(beats) - covered.get(scene_id, set())):
            errors.append({"code": "uncovered_beat", "path": f"{scene_id}.{beat_id}"})
    for identifier, reference in catalog["references"].items():
        kind, subject = reference.get("kind"), reference.get("subject_id")
        target = {"character": "characters", "location": "locations", "prop": "props"}.get(kind) if isinstance(kind, str) else None
        if target is None or not isinstance(subject, str) or subject not in catalog[target]:
            errors.append({"code": "unknown_reference_subject", "path": identifier})
        if reference.get("state") not in ("planned", "provided"):
            errors.append({"code": "invalid_reference_state", "path": identifier})
        warnings.append({"code": "reference_media_not_verified", "path": identifier})
    warnings.append({"code": "creative_quality_and_media_not_checked"})
    if not math.isfinite(total_seconds):
        errors.append({"code": "duration_sum_overflow", "path": "shots"})
        total_seconds = None
    return {"schema": "drama_continuity_check.v1", "structure_valid": not errors, "errors": errors,
            "warnings": warnings, "metrics": {"episodes": len(catalog["episodes"]),
            "scenes": len(catalog["scenes"]), "shots": len(catalog["shots"]), "shot_seconds": total_seconds}}


# LLM: 报告只展示输入及确定性核对结果，所有文本转义；不是浏览器执行容器或生产媒体。
# 函数用途: 生成可保存为 HTML 的静态资料核对页，不运行正文中的 HTML 或脚本。
def render_report(project: object, result: dict) -> str:
    title = project.get("title", "短剧资料") if isinstance(project, dict) else "无效输入"
    payload = json.dumps({"project": project, "check": result}, ensure_ascii=False, indent=2, allow_nan=False)
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<title>短剧连续性核对</title><body><h1>" + html.escape(str(title)) + "</h1>"
            "<p>这是资料结构核对。图片、声音、成片和创作质量尚未验证。</p><pre>"
            + html.escape(payload) + "</pre></body></html>")


# LLM: 只读指定 JSON，JSON/HTML 均写 stdout；产品自动物化与执行须另沿宿主授权接线。
# 函数用途: 运行结构核对，按所选格式输出静态报告。
def main() -> int:
    parser = argparse.ArgumentParser(description="核对短剧工作流资料的跨表连续性")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "html"), default="json")
    arguments = parser.parse_args()
    try:
        project = read_document(arguments.project)
        result = check_project(project)
    except (OSError, ValueError) as exc:
        project = None
        result = {"structure_valid": False, "errors": [{"code": "invalid_input", "message": str(exc)}]}
    print(render_report(project, result) if arguments.format == "html" else
          json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
