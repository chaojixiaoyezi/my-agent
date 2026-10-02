# LLM: 能力包 B 的独立单文件检查器；只按显式 CLI 文件授权核对结构、时长、handoff.v2 及可选基线差异，不启动模型/资源或写宿主状态；同步包方法及组件测试。
# 模块用途: 只读制作资料、明确绑定的交接文件和可选基线项目，用同次字节核对摘要、对象地址与逐 ID 差异，向 stdout 输出分项结果或已转义的静态报告。

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import stat
from pathlib import Path

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 32
MAX_ROWS = 4096
MAX_JSON_DEPTH = 64
MAX_POINTER_CHARS = 2048
MAX_POINTER_PARTS = 64
PACKAGE_ID = "drama-workflow-b"
PACKAGE_VERSION = "0.2.0"
PROJECT_TABLES = ("episodes", "characters", "locations", "props", "scenes", "shots", "references")
MAX_DIFF_ITEMS = 100


# LLM: 只携带确定错误码，不回显文件正文、任意异常或凭据；CLI 将其归入实际检查范围。
# 类用途: 区分授权、I/O、格式及预算失败，避免一个模糊失败掩盖真正原因。
class InputProblem(ValueError):
    # LLM: code 只能由检查器内部指定，不能取自输入文案。
    # 函数用途: 创建可以安全放进结构化报告的失败。
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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


# LLM: JSON 的指数写法也能产生无穷值；parse_constant 单独使用不能拦住这种情况。
# 函数用途: 解析有限小数，拒绝把溢出的数字当作来源事实。
def finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON 小数超出有限范围")
    return result


# LLM: 深度门在 JSON 解码前执行，字符串中的括号不算嵌套；不修复或重解释畸形 JSON。
# 函数用途: 有界解析 UTF-8 资料，拒绝重复键、非有限数及过深输入。
def parse_document(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise InputProblem("input_depth_limit")
            elif character in "]}":
                depth -= 1
        return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant,
                          parse_float=finite_float)
    except InputProblem:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputProblem("input_invalid_json") from exc


# LLM: 仅用于声明与绑定的词法比较，不能作为快照身份；不解析符号链接、不展开环境变量，不授权或打开 JSON 中的路径。
# 函数用途: 让 CLI 绑定与交接路径在本次 cwd 下使用同一种表示。
def normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


# LLM: 读取的都是显式参数路径；拒绝叶子链接/非普通文件，比较打开前后身份与改写信息；摘要与解析必须复用返回的同一份字节。
# 函数用途: 有界取得文件快照，避免 FIFO 阻塞、读取期间替换或把 I/O 失败误记为格式错误。
def read_snapshot(path: Path, *, byte_budget: int | None = None) -> tuple[bytes, object]:
    limit = MAX_DOCUMENT_BYTES if byte_budget is None else min(MAX_DOCUMENT_BYTES, byte_budget)
    size_code = "input_size_limit" if limit == MAX_DOCUMENT_BYTES else "input_total_size_limit"
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise InputProblem("input_not_authorized")
        if not stat.S_ISREG(before.st_mode):
            raise InputProblem("input_not_regular")
        if before.st_size > limit:
            raise InputProblem(size_code)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            current = path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(current.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                raise InputProblem("input_changed")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > limit:
            raise InputProblem(size_code)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise InputProblem("input_changed")
        return raw, parse_document(raw)
    except FileNotFoundError as exc:
        raise InputProblem("input_not_found") from exc
    except PermissionError as exc:
        raise InputProblem("input_permission_denied") from exc
    except OSError as exc:
        raise InputProblem("input_io_error") from exc


# LLM: 只读取显式参数，调用方若还需摘要应直接使用 read_snapshot，避免重新打开同一资料。
# 函数用途: 保留独立脚本的文档读取入口，使用相同的格式和容量边界。
def read_document(path: Path) -> object:
    return read_snapshot(path)[1]


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


# LLM: 目标与镜头共用有限正数边界；布尔值、字符串及超出浮点计量范围的整数不能成为秒数事实。
# 函数用途: 在不转换输入字段的情况下判断秒数是否可安全计量。
def positive_seconds(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


# LLM: 各项已验证为有限正数；稳定求和保留大量小项的贡献，溢出仍是结构错误，不能输出 Infinity。
# 函数用途: 合计某集或全片的真实镜头秒数，不让逐项舍入吞掉合法时长。
def duration_sum(values: list[float], at: str, errors: list[dict]) -> float | None:
    try:
        return math.fsum(values)
    except OverflowError:
        errors.append({"code": "duration_sum_overflow", "path": at})
        return None


# LLM: 仅按已校验的 scene.episode_id 归集，不猜别名或倒推；容差只吸收相对舍入，不能把合法极小目标的倍数差异视为一致。
# 函数用途: 比较每集实际镜头合计与显式目标，保留缺失、非法和数值溢出的区别，返回可展示的分集秒数。
def check_episode_seconds(catalog: dict, errors: list[dict], warnings: list[dict]) -> dict[str, float | None]:
    grouped = {identifier: [] for identifier in catalog["episodes"]}
    for shot in catalog["shots"].values():
        scene_id = shot.get("scene_id")
        scene = catalog["scenes"].get(scene_id) if isinstance(scene_id, str) else None
        episode_id = scene.get("episode_id") if scene else None
        if not isinstance(episode_id, str) or episode_id not in grouped:
            continue
        value = shot.get("seconds")
        if not positive_seconds(value):
            grouped[episode_id] = None
        elif grouped[episode_id] is not None:
            grouped[episode_id].append(value)
    totals = {identifier: duration_sum(values, f"{identifier}.shots", errors) if values is not None else None
              for identifier, values in grouped.items()}
    for identifier, episode in catalog["episodes"].items():
        actual = totals[identifier]
        if "target_seconds" not in episode:
            warnings.append({"code": "episode_target_missing", "path": f"{identifier}.target_seconds"})
        elif not positive_seconds(episode["target_seconds"]):
            errors.append({"code": "positive_target_seconds_required", "path": f"{identifier}.target_seconds"})
        elif actual is not None and not math.isclose(actual, episode["target_seconds"], rel_tol=1e-12, abs_tol=0.0):
            errors.append({"code": "episode_duration_mismatch", "path": identifier,
                           "target_seconds": episode["target_seconds"], "shot_seconds": actual})
    return totals


# LLM: 只验证跨表关系及显式时长对账；不改输入或宿主状态，结构通过不代表内容、真实媒体或创作节奏通过。
# 函数用途: 找出剧集、场次、人物、道具、节拍与镜头的断链，并逐集报告镜头合计与目标不一致。
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
    covered, shot_seconds = {}, []
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
        if not positive_seconds(value):
            errors.append({"code": "positive_seconds_required", "path": identifier})
        else:
            shot_seconds.append(value)
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
    total_seconds = duration_sum(shot_seconds, "shots", errors)
    episode_seconds = check_episode_seconds(catalog, errors, warnings)
    return {"schema": "drama_continuity_check.v1", "structure_valid": not errors, "errors": errors,
            "warnings": warnings, "metrics": {"episodes": len(catalog["episodes"]),
            "scenes": len(catalog["scenes"]), "shots": len(catalog["shots"]), "shot_seconds": total_seconds,
            "episode_seconds": episode_seconds}}


# LLM: v2 的字段名是显式协议，额外别名不能悄悄替代当前字段；调用方只在返回真时取字段。
# 函数用途: 检查一行资料是否有且只有约定字段。
def check_fields(row: object, required: set[str], at: str, errors: list[dict], optional: set[str] = frozenset()) -> bool:
    if not isinstance(row, dict):
        errors.append({"code": "object_required", "path": at})
        return False
    for key in sorted(required - row.keys()):
        errors.append({"code": "field_required", "path": f"{at}.{key}"})
    for key in sorted(row.keys() - required - optional):
        errors.append({"code": "unexpected_field", "path": f"{at}.{key}"})
    return required <= row.keys() and row.keys() <= required | optional


# LLM: 只验证声明非空，不从文字推导故事、权限、质量或完成状态。
# 函数用途: 检查用途、理由与复核说明是否实际填写。
def check_text(value: object, at: str, errors: list[dict]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append({"code": "text_required", "path": at})


# LLM: 交接 ID 是精确字符串，不猜名称或把空白修成另一个身份。
# 函数用途: 在读文件前限制交接列表大小，并验证行字段、唯一编号和待填占位。
def handoff_rows(document: dict, key: str, fields: set[str], errors: list[dict]) -> list[dict]:
    rows = document.get(key)
    if not isinstance(rows, list):
        errors.append({"code": "list_required", "path": key})
        return []
    if len(rows) > (MAX_FILES if key == "files" else MAX_ROWS):
        errors.append({"code": "file_count_limit" if key == "files" else "row_count_limit", "path": key})
        return []
    if key in ("files", "stages") and not rows:
        errors.append({"code": "nonempty_required", "path": key})
    valid, identifiers = [], set()
    for position, row in enumerate(rows):
        at = f"{key}[{position}]"
        if not check_fields(row, fields, at, errors):
            continue
        if "id" in fields:
            identifier = row["id"]
            if (not isinstance(identifier, str) or not identifier.strip() or identifier != identifier.strip()
                    or len(identifier) > 128 or identifier in identifiers):
                errors.append({"code": "invalid_or_duplicate_id", "path": at})
                continue
            identifiers.add(identifier)
        valid.append(row)
    return valid


# LLM: 文件集合来自 files 的精确 ID；列表可空但不能重复，不根据后续映射反推阶段输入输出。
# 函数用途: 核对阶段明确声明的文件关系。
def file_references(values: object, known: dict, at: str, errors: list[dict]) -> set[str]:
    if not isinstance(values, list):
        errors.append({"code": "references_required", "path": at})
        return set()
    if len(values) > MAX_FILES:
        errors.append({"code": "reference_count_limit", "path": at})
        return set()
    result = set()
    for value in values:
        if not isinstance(value, str) or value not in known:
            errors.append({"code": "unknown_reference", "path": at})
        elif value in result:
            errors.append({"code": "duplicate_reference", "path": at})
        else:
            result.add(value)
    return result


# LLM: JSON Pointer 只按 RFC 6901 的字符地址解释，不接受 URI fragment、表达式、通配符或列表别名。
# 函数用途: 检查有界地址并解码斜线/波浪号转义；空字符串明确指向整份 JSON。
def pointer_parts(pointer: object) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise InputProblem("invalid_pointer")
    if len(pointer) > MAX_POINTER_CHARS or pointer.count("/") > MAX_POINTER_PARTS:
        raise InputProblem("pointer_limit")
    if not pointer:
        return []
    parts = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise InputProblem("invalid_pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


# LLM: 只遍历已获授权并校验摘要的 JSON；地址不能打开文件、执行代码或扩展读取范围。
# 函数用途: 定位一个实际对象或标量，数组只接受规范十进制下标。
def pointer_value(document: object, parts: list[str]) -> object:
    value = document
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise InputProblem("invalid_pointer")
            position = int(part)
            if position >= len(value):
                raise InputProblem("pointer_not_found")
            value = value[position]
        else:
            raise InputProblem("pointer_not_found")
    return value


# LLM: 先验证地址的文件权限和语法；documents=None 只检查声明，不读取正文或替模型猜对象。
# 函数用途: 核对一个来源/目标地址，第二阶段再验证实际对象及可选 ID。
def check_address(reference: object, allowed: set[str], files: dict, documents: dict | None,
                  at: str, errors: list[dict]) -> None:
    if not check_fields(reference, {"file_id", "pointer"}, at, errors, {"object_id"}):
        return
    identifier = reference["file_id"]
    if not isinstance(identifier, str) or identifier not in files:
        errors.append({"code": "unknown_reference", "path": f"{at}.file_id"})
        return
    if identifier not in allowed:
        errors.append({"code": "file_outside_stage", "path": f"{at}.file_id"})
        return
    if "object_id" in reference:
        check_text(reference["object_id"], f"{at}.object_id", errors)
    try:
        parts = pointer_parts(reference["pointer"])
        if documents is not None and identifier in documents:
            value = pointer_value(documents[identifier], parts)
            if "object_id" in reference and (not isinstance(value, dict) or value.get("id") != reference["object_id"]):
                raise InputProblem("object_id_mismatch")
    except InputProblem as exc:
        errors.append({"code": exc.code, "path": at})


# LLM: 每个转换显式隶属一个阶段，来源只取该阶段输入、目标只取输出；语义理由和内容真伪不在此判定。
# 函数用途: 统一核对映射、省略、新增及未决差异的结构化地址。
def check_handoff_relations(rows: dict, files: dict, stages: dict, documents: dict | None, errors: list[dict]) -> None:
    for section in ("object_mappings", "omissions", "additions", "unresolved_differences"):
        for position, row in enumerate(rows[section]):
            at = f"{section}[{position}]"
            stage_id = row["stage_id"]
            if not isinstance(stage_id, str) or stage_id not in stages:
                errors.append({"code": "unknown_stage", "path": at})
                continue
            inputs, outputs = stages[stage_id]
            if section == "unresolved_differences":
                for field in ("difference", "next_step"):
                    check_text(row[field], f"{at}.{field}", errors)
                refs = row["refs"]
                if not isinstance(refs, list) or not refs or len(refs) > MAX_ROWS:
                    errors.append({"code": "bounded_references_required", "path": f"{at}.refs"})
                    continue
                for index, reference in enumerate(refs):
                    check_address(reference, inputs | outputs, files, documents, f"{at}.refs[{index}]", errors)
            else:
                check_text(row["reason"], f"{at}.reason", errors)
                for field, allowed in (("source", inputs), ("target", outputs)):
                    if field in row:
                        check_address(row[field], allowed, files, documents, f"{at}.{field}", errors)


# LLM: CLI 的 file_id=path 才是文件授权；任何 JSON 路径、摘要或未知 ID 都不能扩大集合；全量预检失败即零文件读取。
# 函数用途: 在读取前对齐交接清单与显式绑定，拒绝漏项、多余绑定、路径替换及无效摘要。
def check_file_grants(files: dict, bindings: dict[str, Path], errors: list[dict]) -> None:
    for identifier in files.keys() - bindings.keys():
        errors.append({"code": "input_not_authorized", "path": f"files[{identifier}]"})
    for identifier in bindings.keys() - files.keys():
        errors.append({"code": "unknown_binding", "path": f"bindings[{identifier}]"})
    for identifier, row in files.items():
        at = f"files[{identifier}]"
        path = row["path"]
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            errors.append({"code": "invalid_path", "path": at})
        elif identifier in bindings and normalized_path(path) != normalized_path(bindings[identifier]):
            errors.append({"code": "input_not_authorized", "path": at})
        digest = row["sha256"]
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            errors.append({"code": "invalid_sha256", "path": at})


# LLM: 只读完整授权预检通过的绑定，摘要与 JSON 共用字节；只复用相同 Path 表达，禁止词法折叠 .. 后合并快照；首个快照失败即停。
# 函数用途: 收集摘要匹配的有界 JSON；不同绑定路径各走原 reader，避免缺目录或中间链接被缓存掩盖，失败读取不能绕过预算。
def load_handoff_documents(files: dict, bindings: dict[str, Path], snapshots: dict, errors: list[dict]) -> dict:
    documents, total = {}, 0
    for identifier, row in files.items():
        key = bindings[identifier]
        try:
            if key not in snapshots:
                snapshots[key] = read_snapshot(bindings[identifier], byte_budget=MAX_TOTAL_BYTES - total)
            raw, document = snapshots[key]
            if len(raw) > MAX_TOTAL_BYTES - total:
                raise InputProblem("input_total_size_limit")
            total += len(raw)
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise InputProblem("sha256_mismatch")
            documents[identifier] = document
        except InputProblem as exc:
            errors.append({"code": exc.code, "path": f"files[{identifier}]"})
            break
    return documents


# LLM: handoff.v2 是包内制作资料；只核机械关系，结果不能证明实际生产阶段发生或语义正确，不写任务/授权/采用账。
# 函数用途: 两阶段验证交接，先拒绝畸形声明与越权文件，再检查原字节摘要和真实对象地址。
def check_handoff(handoff: object, bindings: dict[str, Path], snapshots: dict | None = None) -> dict:
    errors, warnings = [], [{"code": "handoff_semantics_and_execution_not_checked"}]
    fields = {
        "files": {"id", "path", "sha256"},
        "stages": {"id", "scope", "input_file_ids", "output_file_ids", "review_notes"},
        "object_mappings": {"stage_id", "source", "target", "reason"},
        "omissions": {"stage_id", "source", "reason"},
        "additions": {"stage_id", "target", "reason"},
        "unresolved_differences": {"stage_id", "refs", "difference", "next_step"},
    }
    files, stages, documents = {}, {}, {}
    if check_fields(handoff, {"schema", *fields}, "$", errors):
        if handoff["schema"] != "drama_workflow_handoff.v2":
            errors.append({"code": "unsupported_schema", "path": "schema"})
        rows = {key: handoff_rows(handoff, key, required, errors) for key, required in fields.items()}
        files = {row["id"]: row for row in rows["files"]}
        check_file_grants(files, bindings, errors)
        for row in rows["stages"]:
            identifier = row["id"]
            for field in ("scope", "review_notes"):
                check_text(row[field], f"{identifier}.{field}", errors)
            inputs = file_references(row["input_file_ids"], files, f"{identifier}.input_file_ids", errors)
            outputs = file_references(row["output_file_ids"], files, f"{identifier}.output_file_ids", errors)
            if not outputs:
                errors.append({"code": "nonempty_required", "path": f"{identifier}.output_file_ids"})
            stages[identifier] = (inputs, outputs)
        check_handoff_relations(rows, files, stages, None, errors)
        if rows["unresolved_differences"]:
            warnings.append({"code": "unresolved_differences_present", "count": len(rows["unresolved_differences"])})
        if not errors:
            documents = load_handoff_documents(files, bindings, snapshots if snapshots is not None else {}, errors)
            check_handoff_relations(rows, files, stages, documents, errors)
        if not errors:
            errors.extend(false_change_claims(rows, stages, documents))
            undeclared = undeclared_changes(rows, stages, documents)
            warnings.extend(undeclared[:MAX_DIFF_ITEMS])
            if len(undeclared) > MAX_DIFF_ITEMS:
                warnings.append({"code": "change_not_declared_warnings_truncated", "omitted": len(undeclared) - MAX_DIFF_ITEMS})
    return {"structure_valid": not errors, "errors": errors, "warnings": warnings,
            "metrics": {"files_declared": len(files), "files_checked": len(documents), "stages": len(stages)}}


# LLM: 报告的检查器身份按本文件实际字节计算，不写死摘要；它只帮助审阅区分原包程序与自写脚本，不是安全控制或执行证明。
# 函数用途: 给每份报告附上包 ID、版本和脚本 sha256。
def checker_identity() -> dict:
    return {"package_id": PACKAGE_ID, "package_version": PACKAGE_VERSION,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


# LLM: 只取形状合格且 ID 首次出现的行，重复/坏行留给 check_project 报错；不修复、不排序原资料。
# 函数用途: 把项目七张表整理成 表名 → {ID: 行}，并给出每行在原数组里的下标供地址使用。
def project_objects(project: object) -> dict[str, dict[str, tuple[int, dict]]]:
    result = {}
    for table in PROJECT_TABLES:
        rows = project.get(table) if isinstance(project, dict) else None
        index = {}
        for position, row in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] not in index:
                index[row["id"]] = (position, row)
        result[table] = index
    return result


# LLM: 节拍只在所属场次内按 (场次 ID, 节拍 ID) 定位，不跨场次合并同名节拍。
# 函数用途: 列出每个节拍的稳定键、JSON 地址和原行。
def project_beats(objects: dict) -> dict[tuple[str, str], tuple[str, dict]]:
    beats = {}
    for scene_id, (scene_position, scene) in objects["scenes"].items():
        rows = scene.get("beats")
        for position, beat in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(beat, dict) and isinstance(beat.get("id"), str) and (scene_id, beat["id"]) not in beats:
                beats[(scene_id, beat["id"])] = (f"/scenes/{scene_position}/beats/{position}", beat)
    return beats


# LLM: 场次比较不含 beats（节拍另按自身地址比较），避免一个节拍改动把整场都算成未声明修改。
# 函数用途: 去掉场次里的节拍列表，只比较场次自身字段。
def comparable(table: str, row: dict) -> dict:
    return {key: value for key, value in row.items() if not (table == "scenes" and key == "beats")}


# LLM: 只按稳定 ID 与字段值逐字比较，不判断改动是否合理；地址是各自文件里的 JSON Pointer。
# 函数用途: 列出两份项目之间新增、删除和修改的对象，供基线对比与交接覆盖共用。
def project_changes(before: object, after: object) -> list[dict]:
    old, new = project_objects(before), project_objects(after)
    changes = []
    for table in PROJECT_TABLES:
        for identifier in sorted(new[table].keys() - old[table].keys()):
            changes.append({"change": "added", "table": table, "id": identifier,
                            "pointer": f"/{table}/{new[table][identifier][0]}", "side": "after"})
        for identifier in sorted(old[table].keys() - new[table].keys()):
            changes.append({"change": "removed", "table": table, "id": identifier,
                            "pointer": f"/{table}/{old[table][identifier][0]}", "side": "before"})
        for identifier in sorted(old[table].keys() & new[table].keys()):
            if comparable(table, old[table][identifier][1]) != comparable(table, new[table][identifier][1]):
                changes.append({"change": "modified", "table": table, "id": identifier,
                                "pointer": f"/{table}/{new[table][identifier][0]}", "side": "after"})
    old_beats, new_beats = project_beats(old), project_beats(new)
    for key in sorted(new_beats.keys() - old_beats.keys()):
        changes.append({"change": "added", "table": "beats", "id": ".".join(key), "pointer": new_beats[key][0], "side": "after"})
    for key in sorted(old_beats.keys() - new_beats.keys()):
        changes.append({"change": "removed", "table": "beats", "id": ".".join(key), "pointer": old_beats[key][0], "side": "before"})
    for key in sorted(old_beats.keys() & new_beats.keys()):
        if old_beats[key][1] != new_beats[key][1]:
            changes.append({"change": "modified", "table": "beats", "id": ".".join(key), "pointer": new_beats[key][0], "side": "after"})
    return changes


# LLM: 说话人与节拍类型只做逐字比较：同一节拍键说话人或类型不同，或同场一句逐字相同的台词换了说话人；用户确有要求时仍只是 warning。
# 函数用途: 找出“重新编号顺手换了说话人”和节拍类型被改的情况。
def beat_warnings(before: object, after: object) -> list[dict]:
    old_beats, new_beats = project_beats(project_objects(before)), project_beats(project_objects(after))
    warnings, flagged = [], set()
    for key in sorted(old_beats.keys() & new_beats.keys()):
        old, new = old_beats[key][1], new_beats[key][1]
        if old.get("kind") != new.get("kind"):
            warnings.append({"code": "beat_kind_changed", "path": ".".join(key),
                             "baseline_kind": old.get("kind"), "kind": new.get("kind")})
        elif old.get("kind") == "dialogue" and old.get("character_id") != new.get("character_id"):
            flagged.add(key)
            warnings.append({"code": "dialogue_speaker_changed", "path": ".".join(key),
                             "baseline_character_ids": [old.get("character_id")], "character_id": new.get("character_id")})
    lines = {}
    for (scene_id, _), (_, beat) in old_beats.items():
        if beat.get("kind") == "dialogue" and isinstance(beat.get("text"), str):
            lines.setdefault((scene_id, beat["text"]), set()).add(str(beat.get("character_id")))
    for key, (_, beat) in sorted(new_beats.items()):
        speakers = lines.get((key[0], beat.get("text"))) if beat.get("kind") == "dialogue" else None
        if key not in flagged and speakers and str(beat.get("character_id")) not in speakers:
            warnings.append({"code": "dialogue_speaker_changed", "path": ".".join(key),
                             "baseline_character_ids": sorted(speakers), "character_id": beat.get("character_id")})
    return warnings


# LLM: 计数总是完整的，明细按类别各最多 MAX_DIFF_ITEMS 条并给出省略数；不回显对象正文。
# 函数用途: 汇总基线对比结果，返回报告 metrics 与 warning 列表。
def compare_with_baseline(baseline: object, project: object, raw: bytes) -> tuple[dict, list[dict]]:
    changes = project_changes(baseline, project)
    metrics = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    for kind in ("added", "removed", "modified"):
        rows = [f"{row['table']}:{row['id']}" for row in changes if row["change"] == kind]
        metrics[kind] = {"count": len(rows), "items": rows[:MAX_DIFF_ITEMS], "omitted": max(0, len(rows) - MAX_DIFF_ITEMS)}
    removed = [row for row in changes if row["change"] == "removed"]
    warnings = [{"code": "baseline_object_removed", "path": f"{row['table']}:{row['id']}"} for row in removed[:MAX_DIFF_ITEMS]]
    return metrics, warnings + beat_warnings(baseline, project)


# LLM: 只有两端都是本包项目 v1 时才比较；地址覆盖按同文件且相等、在下层或在非根上层判断，根地址不算覆盖全部改动。
# 函数用途: 判断一处真实改动是否被交接里某个地址点到。
def address_covers(addresses: set[tuple[str, str]], file_id: str, pointer: str) -> bool:
    return any(owner == file_id and (declared == pointer or declared.startswith(pointer + "/")
                                     or (declared and pointer.startswith(declared + "/")))
               for owner, declared in addresses)


# LLM: 交接行的全部结构化地址都参与覆盖判断；只读已校验通过的行，不解析 reason 等说明文字。
# 函数用途: 收集某个阶段交接行里声明过的 (文件, 地址)。
def stage_addresses(rows: dict, stage_id: str) -> set[tuple[str, str]]:
    addresses = set()
    for section in ("object_mappings", "omissions", "additions", "unresolved_differences"):
        for row in rows[section]:
            if row.get("stage_id") != stage_id:
                continue
            references = row.get("refs") if section == "unresolved_differences" else [row.get("source"), row.get("target")]
            for reference in references if isinstance(references, list) else []:
                if isinstance(reference, dict) and isinstance(reference.get("pointer"), str):
                    addresses.add((reference.get("file_id"), reference["pointer"]))
    return addresses


# LLM: 交接合同不要求列全映射，所以未覆盖改动只给 warning；只比较同一阶段里都是项目 v1 的输入/输出，摘要已先核对。
# 函数用途: 找出项目真实改了却没有任何交接行点到的地方。
def undeclared_changes(rows: dict, stages: dict, documents: dict) -> list[dict]:
    warnings = []
    for stage_id, (inputs, outputs) in sorted(stages.items()):
        addresses = stage_addresses(rows, stage_id)
        for before_id in sorted(inputs):
            for after_id in sorted(outputs):
                before, after = documents.get(before_id), documents.get(after_id)
                if not all(isinstance(doc, dict) and doc.get("schema") == "drama_workflow_project.v1" for doc in (before, after)):
                    continue
                for change in project_changes(before, after):
                    file_id = after_id if change["side"] == "after" else before_id
                    if not address_covers(addresses, file_id, change["pointer"]):
                        warnings.append({"code": "change_not_declared_in_handoff", "path": stage_id,
                                         "file_id": file_id, "pointer": change["pointer"], "change": change["change"],
                                         "object": f"{change['table']}:{change['id']}"})
    return warnings


# LLM: 只认 /<表>/<下标> 形式的整对象地址；内容逐字段相同才算“本来就有/仍然存在”，同 ID 改过内容不报。
# 函数用途: 取地址指向的项目对象所在表名与对象本身，形状不符返回 None。
def table_object(document: object, pointer: str) -> tuple[str, dict] | None:
    parts = pointer.split("/")
    if (not isinstance(document, dict) or document.get("schema") != "drama_workflow_project.v1" or len(parts) != 3
            or parts[1] not in PROJECT_TABLES or not re.fullmatch(r"0|[1-9][0-9]*", parts[2])):
        return None
    rows = document.get(parts[1])
    position = int(parts[2])
    if not isinstance(rows, list) or position >= len(rows) or not isinstance(rows[position], dict):
        return None
    return parts[1], rows[position]


# LLM: 交接声称新增的整对象在本阶段输入里原样已存在、声称省略的整对象在输出里原样仍在，都是客观矛盾，报 error。
# 函数用途: 核对带 object_id 的新增/省略声明确实发生了。
def false_change_claims(rows: dict, stages: dict, documents: dict) -> list[dict]:
    errors = []
    for section, field, code, other in (("additions", "target", "declared_addition_already_present", 0),
                                        ("omissions", "source", "declared_omission_still_present", 1)):
        for position, row in enumerate(rows[section]):
            reference, stage = row.get(field), stages.get(row.get("stage_id"))
            if not (isinstance(reference, dict) and "object_id" in reference and stage):
                continue
            located = table_object(documents.get(reference.get("file_id")), reference.get("pointer", ""))
            if located is None:
                continue
            table, row_object = located
            for file_id in sorted(stage[other]):
                peers = project_objects(documents.get(file_id))[table]
                if peers.get(row_object.get("id"), (0, None))[1] == row_object:
                    errors.append({"code": code, "path": f"{section}[{position}].{field}", "file_id": file_id})
    return errors


# LLM: 绑定来自重复 CLI 参数，不读取或推断 handoff 中的路径；重复绑定不得采用最后一个值。
# 函数用途: 解析明确的 file_id=path 授权，保留非法与重复输入错误。
def parse_bindings(values: list[str], errors: list[dict]) -> dict[str, Path]:
    bindings = {}
    if len(values) > MAX_FILES:
        errors.append({"code": "file_count_limit", "path": "--input-file"})
        return bindings
    for value in values:
        identifier, separator, path = value.partition("=")
        if (not separator or not identifier.strip() or identifier != identifier.strip() or len(identifier) > 128
                or not path.strip() or "\x00" in path):
            errors.append({"code": "invalid_binding", "path": "--input-file"})
        elif identifier in bindings:
            errors.append({"code": "duplicate_binding", "path": f"bindings[{identifier}]"})
        else:
            bindings[identifier] = Path(path)
    return bindings


# LLM: 基线是另一份显式 CLI 授权的项目 v1，同一 reader 与上限读取；只对比、不改判 project；项目读不出时标 not_checked 而不是通过。
# 函数用途: 读取可选基线并与本次项目逐 ID 对比，返回分项结果与状态。
def evaluate_baseline(baseline_path: Path | None, project: object) -> tuple[dict, str]:
    if baseline_path is None:
        return {"errors": [], "warnings": [], "metrics": {}}, "not_requested"
    try:
        raw, baseline = read_snapshot(baseline_path)
    except InputProblem as exc:
        return {"errors": [{"code": "invalid_input", "cause": exc.code, "path": "--baseline-project"}]}, "failed"
    if not isinstance(baseline, dict) or baseline.get("schema") != "drama_workflow_project.v1":
        return {"errors": [{"code": "unsupported_schema", "path": "--baseline-project"}]}, "failed"
    if not isinstance(project, dict):
        return {"errors": [], "warnings": [{"code": "baseline_not_compared"}], "metrics": {}}, "not_checked"
    metrics, warnings = compare_with_baseline(baseline, project, raw)
    return {"errors": [], "warnings": warnings, "metrics": metrics}, "passed"


# LLM: CLI 聚合只形成一份报告；project 原函数语义不变，handoff 未请求不等于通过；本次缓存仅按原 Path 绑定复用，不能折叠 ..；
#   基线差异只给 warning，报告附检查器身份与项目摘要。
# 函数用途: 沿显式文件参数完成项目、可选交接和可选基线检查，分别标出范围和失败原因。
def evaluate_inputs(project_path: Path, handoff_path: Path | None, binding_values: list[str],
                    baseline_path: Path | None = None) -> tuple[object, dict]:
    project, snapshots, binding_errors, project_sha256 = None, {}, [], None
    bindings = parse_bindings(binding_values, binding_errors)
    try:
        snapshot = read_snapshot(project_path)
        snapshots[project_path] = snapshot
        project = snapshot[1]
        project_sha256 = hashlib.sha256(snapshot[0]).hexdigest()
        project_check = check_project(project)
    except InputProblem as exc:
        project_check = {"structure_valid": False, "errors": [{"code": "invalid_input", "cause": exc.code, "path": "--project"}]}
    handoff_check = {"errors": [], "warnings": [{"code": "handoff_not_checked"}], "metrics": {}}
    handoff_status = "not_requested"
    if binding_values and handoff_path is None:
        binding_errors.append({"code": "handoff_required", "path": "--handoff"})
    if binding_errors:
        handoff_check = {"errors": binding_errors}
        handoff_status = "failed"
    elif handoff_path is not None:
        try:
            handoff = read_document(handoff_path)
            handoff_check = check_handoff(handoff, bindings, snapshots)
        except InputProblem as exc:
            handoff_check = {"errors": [{"code": "invalid_input", "cause": exc.code, "path": "--handoff"}]}
        handoff_status = "failed" if handoff_check["errors"] else "passed"
    baseline_check, baseline_status = evaluate_baseline(baseline_path, project)
    scopes = (("project", project_check), ("handoff", handoff_check), ("baseline", baseline_check))
    errors = [{**item, "scope": scope} for scope, check in scopes for item in check.get("errors", [])]
    warnings = [{**item, "scope": scope} for scope, check in scopes for item in check.get("warnings", [])]
    return project, {"schema": "drama_workflow_check.v2", "structure_valid": not errors, "checker": checker_identity(),
                     "checks": {"project": "passed" if project_check["structure_valid"] else "failed", "handoff": handoff_status,
                                "baseline": baseline_status},
                     "errors": errors, "warnings": warnings,
                     "metrics": {**project_check.get("metrics", {}), "project_sha256": project_sha256},
                     "handoff_metrics": handoff_check.get("metrics", {}), "baseline_metrics": baseline_check.get("metrics", {})}


# LLM: 报告只展示输入及确定性核对结果，所有文本转义；不是浏览器执行容器或生产媒体。
# 函数用途: 生成保留项目/交接检查范围和未决警告的静态资料核对页，不运行正文中的 HTML 或脚本。
def render_report(project: object, result: dict) -> str:
    title = project.get("title", "短剧资料") if isinstance(project, dict) else "无效输入"
    payload = json.dumps({"project": project, "check": result}, ensure_ascii=False, indent=2, allow_nan=False)
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<title>短剧连续性核对</title><body><h1>" + html.escape(str(title)) + "</h1>"
            "<p>这是资料结构核对。图片、声音、成片和创作质量尚未验证。</p><pre>"
            + html.escape(payload) + "</pre></body></html>")


# LLM: 只读指定 JSON、--input-file 的显式绑定与可选 --baseline-project，JSON/HTML 写 stdout；handoff 路径不授予读取其它文件或执行资源的权限。
# 函数用途: 运行项目与可选交接核对，按所选格式输出新版本分项报告，任何所请求检查失败均退出 1。
def main() -> int:
    parser = argparse.ArgumentParser(description="核对短剧工作流资料的跨表连续性")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, help="可选 handoff.v2 交接资料")
    parser.add_argument("--input-file", action="append", default=[], metavar="FILE_ID=PATH", help="明确授权读取的交接文件，可重复")
    parser.add_argument("--baseline-project", type=Path, help="可选：改动前的项目 v1，用于逐 ID 对比（只给 warning）")
    parser.add_argument("--format", choices=("json", "html"), default="json")
    arguments = parser.parse_args()
    project, result = evaluate_inputs(arguments.project, arguments.handoff, arguments.input_file, arguments.baseline_project)
    print(render_report(project, result) if arguments.format == "html" else
          json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
