# LLM: 能力包 A 的单文件私有资源，核对 v3 声明并给出字面人物诊断；可选 lines/source_quotes/props/prop_states 只在出现时检查；
#   0.5.0 加占位文字、内嵌引文、改编原文引用、道具状态/来源、画外点名、过短镜头 7 项确定性检查，以及 --host-json 宿主核验输出；
#   同步 workflow、review-continuity、visible-characters 方法及 basis/duration/visibility/lines/v05/examples 测试，不改变宿主状态。
# 模块用途: 只读原文与交付，核对来源、时长、可见/画外声明、结构化台词与逐字引用、道具状态接续；名称出现不证明在场，
#   不替旧版本补字段或声称语义、媒体已验证。
# 名称算法改写自 drama-skills@0e8929881bb59248618c4f402707c64723adc017 的 creator_markdown_check.py；MIT 声明见 licenses/drama-skills-LICENSE，差异见 PROVENANCE.md。

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Iterator
from itertools import accumulate, groupby
from pathlib import Path

MAX_INPUT_BYTES = 4 * 1024 * 1024
DURATION_REL_TOL = 1e-12
SHOT_TEXT_FIELDS = ("start_state", "action", "end_state")
ASCII_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
MAX_NAME_SCAN_WORK = 2_000_000
MAX_NAME_WARNINGS = 100
PACKAGE_ID = "drama-text-a"
PACKAGE_VERSION = "0.5.0"
MIN_QUOTE_CHARS = 2
# 单镜秒数低于它只提醒（shot_too_short），可用 --min-shot-seconds 改；单位：秒。
MIN_SHOT_SECONDS_DEFAULT = 2.0
# 整段就是这些写法时算占位（不区分大小写）；尖括号包起来的整段（模板里的填写提示）和纯标点、符号也算。
PLACEHOLDER_TOKENS = frozenset({"{}", "[]", "()", "todo", "tbd", "tba", "fixme", "xxx", "n/a", "null", "none",
                                "待定", "待补", "待补充", "待填", "待填写", "占位", "同上", "略"})
PLACEHOLDER_PREFIX = re.compile(r"(todo|tbd|fixme)(?![a-z])")
HOST_RESULT_SCHEMA = "pack_verifier_result.v1"
# 宿主核验合同建议 metrics 平铺成“键 → 数字”且不超过 16 个键。
HOST_METRICS_MAX_COUNT = 16


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


# LLM: 名称身份仅来自显式 v3 声明；不从展示名推代称，不解析退出理由，同词多 owner 留到诊断报告歧义。
# 函数用途: 校验人物名称声明，建立精确字面到全部角色的临时索引，收集明确退出的角色。
def cast_text_names(cast: dict, errors: list[dict]) -> tuple[dict[str, set[str]], list[str]]:
    owners, skipped = {}, []
    for identifier, row in cast.items():
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            errors.append({"code": "character_name_required", "path": f"{identifier}.name"})
        names, reason = row.get("text_names"), row.get("text_match_skip_reason", "")
        if not isinstance(reason, str):
            errors.append({"code": "text_skip_reason_type", "path": f"{identifier}.text_match_skip_reason"})
        if not isinstance(names, list):
            errors.append({"code": "text_names_required", "path": f"{identifier}.text_names"})
            continue
        if not names:
            if isinstance(reason, str) and reason.strip():
                skipped.append(identifier)
            else:
                errors.append({"code": "text_skip_reason_required", "path": identifier})
        elif isinstance(reason, str) and reason.strip():
            errors.append({"code": "conflicting_text_match_declaration", "path": identifier})
        seen = set()
        for position, name in enumerate(names):
            at = f"{identifier}.text_names[{position}]"
            if (not isinstance(name, str) or len(name) < 2 or name != name.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                errors.append({"code": "invalid_text_name", "path": at})
            elif name in seen:
                errors.append({"code": "duplicate_text_name", "path": at})
            else:
                seen.add(name)
                owners.setdefault(name, set()).add(identifier)
    return owners, sorted(skipped)


# LLM: 人物列表在 v3 显式要求无重复，原来源引用仍沿 references 合同；此处不扩张可见角色的场次范围。
# 函数用途: 核对人物外键并额外报告重复项，不让 set 去重掩盖坏声明。
def character_references(row: dict, key: str, known: dict, at: str, errors: list[dict]) -> set[str]:
    valid = references(row, key, known, at, errors)
    values = row.get(key)
    if isinstance(values, list):
        seen = set()
        for value in values:
            if isinstance(value, str):
                if value in seen:
                    errors.append({"code": "duplicate_character_reference", "path": f"{at}.{key}"})
                seen.add(value)
    return valid


# LLM: 可见与画外按整镜互斥；画外可引用全 cast，可见仍限本场，不判断作者声明是否符合实际画面。
# 函数用途: 检查每镜的两份人物列表，返回用于字面覆盖的声明并集。
def shot_characters(row: dict, scene_cast: dict, cast: dict, at: str, errors: list[dict]) -> set[str]:
    visible = character_references(row, "visible_character_ids", scene_cast, at, errors)
    offscreen = character_references(row, "offscreen_character_ids", cast, at, errors)
    if visible & offscreen:
        errors.append({"code": "character_visibility_overlap", "path": at,
                       "character_ids": sorted(visible & offscreen)})
    return visible | offscreen


# LLM: 字界仅按明确的 ASCII 邻接规则；不做分词、大小写折叠、否定或引号语义判断。
# 函数用途: 排除英文编号中粘连的名字片段，同时保留中文相邻字的原样命中。
def valid_name_boundary(text: str, start: int, end: int) -> bool:
    return not (
        start > 0 and text[start] in ASCII_NAME_CHARS and text[start - 1] in ASCII_NAME_CHARS
        or end < len(text) and text[end - 1] in ASCII_NAME_CHARS and text[end] in ASCII_NAME_CHARS
    )


# LLM: 改写上游长名优先，只有字界有效命中占位；每长度组共享旧前缀，等长和部分相交不互相吞掉，调用前须通过扫描预算。
# 函数用途: 逐字段产出原字符区间；临时前缀最大值避免每个命中与全部既有区间逐对比较。
def literal_name_matches(text: str, names: list[str]) -> Iterator[tuple[str, int, int]]:
    covered_ends = [0] * len(text)
    for _, group in groupby(names, key=len):
        longer_ends = list(accumulate(covered_ends, max))
        for name in group:
            start = text.find(name)
            while start >= 0:
                end = start + len(name)
                if valid_name_boundary(text, start, end):
                    if longer_ends[start] < end:
                        yield name, start, end
                    covered_ends[start] = max(covered_ends[start], end)
                start = text.find(name, start + 1)


# LLM: 未完成诊断不提供零计数；该报告不更改原结构检查、宿主状态或创建第二份验收记录。
# 函数用途: 给解析、结构、空名称和预算未检查情况提供一致的明确回执。
def unchecked_name_diagnostics(reason: str) -> dict:
    return {"scope": "declared_names_in_shot_states", "status": "not_checked", "reason": reason,
            "checked_field_count": 0, "match_count": None, "warning_count": None,
            "emitted_warning_count": None, "omitted_warning_count": None, "warnings_truncated": None}


# LLM: 调用方已确认是未覆盖/歧义且在输出上限内才构造明细；名字出现只作 warning，位置保持原 Unicode 字符下标。
# 函数用途: 为需要保留的命中创建有限名称预览和候选列表，裁剪后的命中不再重复排序大候选集合。
def name_warning(match: tuple[str, int, int], candidates: set[str], declared: set[str], at: str) -> dict:
    name, start, end = match
    return {"code": "ambiguous_character_name" if len(candidates) > 1 else "named_character_unaccounted",
            "path": at, "start": start, "end": end, "text_name_preview": name[:80],
            "text_name_length": len(name), "candidate_character_ids": sorted(candidates),
            "declared_candidate_ids": sorted(candidates & declared)}


# LLM: 原结构有效才扫描三字段；成本估算含名称长度，超额不给部分零计数；裁剪后继续计数但不构造明细，不能提前停止扫描。
# 函数用途: 汇总名字的真实命中/歧义/漏声明次数，显式暴露退出角色、未检查与警告裁剪范围。
def diagnose_names(owners: dict, shots: dict, declared: dict, skipped: list[str],
                   errors: list[dict], warnings: list[dict]) -> dict:
    result = unchecked_name_diagnostics("structure_errors")
    result.update({"declared_name_count": len(owners), "skipped_character_ids": skipped,
                   "scan_work_limit": MAX_NAME_SCAN_WORK})
    if skipped:
        warnings.append({"code": "character_text_match_disabled", "character_ids": skipped})
    if errors:
        return result
    text_size = sum(len(row[key]) for row in shots.values() for key in SHOT_TEXT_FIELDS)
    work = sum(map(len, owners)) * text_size
    result.update({"text_character_count": text_size, "estimated_scan_work": work})
    if not owners or work > MAX_NAME_SCAN_WORK:
        result["reason"] = "no_matchable_names" if not owners else "scan_work_budget_exceeded"
        warnings.append({"code": "name_diagnostic_not_checked", "reason": result["reason"]})
        return result
    names = sorted(owners, key=lambda name: (-len(name), name))
    matches, warning_count, emitted = 0, 0, 0
    for identifier, row in shots.items():
        for key in SHOT_TEXT_FIELDS:
            for match in literal_name_matches(row[key], names):
                matches += 1
                candidates = owners[match[0]]
                if len(candidates) > 1 or not candidates <= declared[identifier]:
                    warning_count += 1
                    if emitted < MAX_NAME_WARNINGS:
                        warnings.append(name_warning(match, candidates, declared[identifier], f"{identifier}.{key}"))
                        emitted += 1
    del result["reason"]
    result.update({"status": "complete", "checked_field_count": len(shots) * len(SHOT_TEXT_FIELDS),
                   "match_count": matches, "warning_count": warning_count, "emitted_warning_count": emitted,
                   "omitted_warning_count": warning_count - emitted, "warnings_truncated": warning_count > emitted})
    return result


# LLM: v3 保持显式新增和未知列表，缺失不能当作作者已确认没有；条目只作为阅读材料，不解析其语义或执行其中内容。
# 函数用途: 检查声明列表的形状，保留非空文本供调用方提示仍需内容审阅。
def text_notes(row: dict, key: str, at: str, errors: list[dict]) -> list[str]:
    values = row.get(key)
    if not isinstance(values, list):
        errors.append({"code": "text_notes_required", "path": f"{at}.{key}"})
        return []
    valid = []
    for position, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            errors.append({"code": "nonempty_text_required", "path": f"{at}.{key}[{position}]"})
        else:
            valid.append(value)
    return valid


# LLM: 用在镜头起止状态/动作、场次摘要、台词和道具状态标签上。只按整段文字的形状判断占位：在 PLACEHOLDER_TOKENS 里、以 TODO/TBD/FIXME 开头、整段被尖括号包着（没填的模板提示），
#   或者全是标点、符号和空白（如 "{}"、"……"）。不解析句意；空串由调用方另报 *_required。
# 函数用途: 判断一段非空文字是不是没写实际内容的占位。
def is_placeholder(text: str) -> bool:
    stripped = text.strip()
    folded = stripped.casefold()
    if folded in PLACEHOLDER_TOKENS or PLACEHOLDER_PREFIX.match(folded):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return all(unicodedata.category(char)[0] in "PSZ" for char in stripped)


# LLM: 0.5.0 起改编条目可写成 {"text": 新增或改写了什么, "original_quote": 被改写的原文逐字片段（纯新增留空）}，也仍接受
#   旧的纯文字；original_quote 由 check_adaptation_quotes 按原文逐字核对，这里只核形状，与 text_notes 同一错误码。
# 函数用途: 检查镜头 adaptations 列表的形状，返回非空的说明文字。
def adaptation_notes(row: dict, at: str, errors: list[dict]) -> list[str]:
    values = row.get("adaptations")
    if not isinstance(values, list):
        errors.append({"code": "text_notes_required", "path": f"{at}.adaptations"})
        return []
    valid = []
    for position, value in enumerate(values):
        text = value.get("text") if isinstance(value, dict) else value
        quote = value.get("original_quote", "") if isinstance(value, dict) else ""
        if not isinstance(text, str) or not text.strip() or not isinstance(quote, str):
            errors.append({"code": "nonempty_text_required", "path": f"{at}.adaptations[{position}]"})
        else:
            valid.append(text)
    return valid


# LLM: 镜头只可声明本场已有原文编号；新增/未知不借用别场编号掩盖。此入口仅校验作者声明的关系，不证明文字确实支持镜头。
# 函数用途: 核对一个镜头的依据载体，并把尚需审阅的新增和未知逐镜头列入警告。
def check_shot_basis(row: dict, identifier: str, scene_sources: set[str], errors: list[dict],
                     warnings: list[dict]) -> None:
    sources = references(row, "source_ids", {key: {} for key in scene_sources}, identifier, errors)
    adaptations = adaptation_notes(row, identifier, errors)
    unresolved = text_notes(row, "unresolved", identifier, errors)
    if not sources and not adaptations and not unresolved:
        errors.append({"code": "shot_basis_required", "path": identifier})
    for key, values, code in (("adaptations", adaptations, "shot_adaptations_need_review"),
                              ("unresolved", unresolved, "shot_basis_unresolved")):
        if values:
            warnings.append({"code": code, "path": f"{identifier}.{key}", "count": len(values)})


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


# LLM: 报告的检查器身份按本文件实际字节计算，不写死摘要；只帮审阅区分原包程序与自写脚本，不是安全控制或执行证明。
# 函数用途: 给每份报告附上包 ID、版本和脚本 sha256。
def checker_identity() -> dict:
    return {"package_id": PACKAGE_ID, "package_version": PACKAGE_VERSION,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


# LLM: “逐字”只按原文段落的连续子串判断，不做空白、标点、全半角或 Unicode 归一化；来源必须是本镜已声明的段落。
#   code 区分整句逐字（quote_not_verbatim）和台词里的内嵌引文（embedded_quote_not_verbatim）。
# 函数用途: 核对一条被标成原文逐字的文字，不符时报错并列出它逐字出现的其他段落帮助定位。
def check_quote(source_id: object, text: str, at: str, passages: dict, shot_sources: set[str], errors: list[dict],
                code: str = "quote_not_verbatim") -> None:
    if not isinstance(source_id, str) or source_id not in shot_sources:
        errors.append({"code": "quote_source_not_in_shot", "path": at})
        return
    if len(text) < MIN_QUOTE_CHARS:
        errors.append({"code": "quote_too_short", "path": at})
        return
    passage = passages[source_id].get("text")
    if not isinstance(passage, str) or text not in passage:
        found = sorted(key for key, row in passages.items() if isinstance(row.get("text"), str) and text in row["text"])
        errors.append({"code": code, "path": at, "source_id": source_id,
                       "text_length": len(text), "found_in_source_ids": found})


# LLM: lines 是可选结构化台词；说话人取 cast ID 或 null（旁白/字幕），必须在本镜可见或画外声明里；不评价台词质量或字数。
# 函数用途: 校验一镜的台词条目并核对标了 verbatim_source_id 的逐字台词，返回合格条目；没有 lines 键返回 None。
def check_shot_lines(row: dict, identifier: str, context: dict, shot_sources: set[str], errors: list[dict]) -> list[dict] | None:
    if "lines" not in row:
        return None
    values = row["lines"]
    if not isinstance(values, list):
        errors.append({"code": "list_required", "path": f"{identifier}.lines"})
        return []
    valid = []
    for position, line in enumerate(values):
        at = f"{identifier}.lines[{position}]"
        if not isinstance(line, dict) or not isinstance(line.get("text"), str) or not line["text"].strip():
            errors.append({"code": "line_text_required", "path": at})
            continue
        if is_placeholder(line["text"]):
            errors.append({"code": "placeholder_text", "path": f"{at}.text"})
        speaker = line.get("speaker_id")
        if speaker is not None and (not isinstance(speaker, str) or speaker not in context["cast"]):
            errors.append({"code": "unknown_line_speaker", "path": f"{at}.speaker_id"})
            continue
        if speaker is not None and speaker not in context["declared"][identifier]:
            errors.append({"code": "line_speaker_undeclared", "path": f"{at}.speaker_id", "character_id": speaker})
        if "verbatim_source_id" in line:
            check_quote(line["verbatim_source_id"], line["text"], at, context["passages"], shot_sources, errors)
        if "embedded_quotes" in line:
            check_embedded_quotes(line, at, context["passages"], shot_sources, errors)
        valid.append(line)
    return valid


# LLM: 台词或字幕里只有一部分是原文时，作者在这条台词的 embedded_quotes 里逐条声明 {source_id, text}：每条必须是这句台词的
#   连续子串，并且是本镜所引段落的逐字子串。不扫描台词里的引号，没声明的部分不查。
# 函数用途: 核对一条台词里声明的内嵌原文引文。
def check_embedded_quotes(line: dict, at: str, passages: dict, shot_sources: set[str], errors: list[dict]) -> None:
    values = line["embedded_quotes"]
    if not isinstance(values, list):
        errors.append({"code": "list_required", "path": f"{at}.embedded_quotes"})
        return
    for position, quote in enumerate(values):
        where = f"{at}.embedded_quotes[{position}]"
        if not isinstance(quote, dict) or not isinstance(quote.get("text"), str) or not quote["text"].strip():
            errors.append({"code": "quote_text_required", "path": where})
        elif quote["text"] not in line["text"]:
            errors.append({"code": "embedded_quote_not_in_line", "path": where})
        else:
            check_quote(quote.get("source_id"), quote["text"], where, passages, shot_sources, errors,
                        code="embedded_quote_not_verbatim")


# LLM: 改编条目的 original_quote 是作者声明“原文里被改写的那句”，必须是原文某一段的逐字子串（不限本镜段落，改编常跨段）；
#   留空表示纯新增，不查。不判断改编本身是否合适，也不判断“原文已有却标成改编”（那要读懂内容）。
# 函数用途: 核对改编条目引用的原文确实存在于本次原文里。
def check_adaptation_quotes(row: dict, identifier: str, passages: dict, errors: list[dict]) -> None:
    values = row.get("adaptations") if isinstance(row.get("adaptations"), list) else []
    texts = [passage["text"] for passage in passages.values() if isinstance(passage.get("text"), str)]
    for position, value in enumerate(values):
        quote = value.get("original_quote") if isinstance(value, dict) else None
        if isinstance(quote, str) and quote.strip() and not any(quote in text for text in texts):
            errors.append({"code": "adaptation_original_not_in_source",
                           "path": f"{identifier}.adaptations[{position}].original_quote"})


# LLM: source_quotes 是作者显式声明的原文短句依据，同样只做逐字子串核对；不扫描散文里的引号。
# 函数用途: 核对一镜的原文引用条目，返回条目数。
def check_source_quotes(row: dict, identifier: str, passages: dict, shot_sources: set[str], errors: list[dict]) -> int:
    if "source_quotes" not in row:
        return 0
    values = row["source_quotes"]
    if not isinstance(values, list):
        errors.append({"code": "list_required", "path": f"{identifier}.source_quotes"})
        return 0
    for position, quote in enumerate(values):
        at = f"{identifier}.source_quotes[{position}]"
        if not isinstance(quote, dict) or not isinstance(quote.get("text"), str) or not quote["text"].strip():
            errors.append({"code": "quote_text_required", "path": at})
            continue
        check_quote(quote.get("source_id"), quote["text"], at, passages, shot_sources, errors)
    return len(values)


# LLM: 道具状态是作者自定的短标签，只比较标签和持有人，不读正文；持有人必须在本镜声明里，在画外只提醒。
# 函数用途: 校验一个时刻（start/end）的道具状态列表，返回 道具 ID → (持有人, 标签)。
def moment_prop_states(entries: object, at: str, shot: dict, context: dict, errors: list[dict], warnings: list[dict]) -> dict:
    if not isinstance(entries, list):
        errors.append({"code": "list_required", "path": at})
        return {}
    states = {}
    for position, entry in enumerate(entries):
        where = f"{at}[{position}]"
        prop, holder, state = (entry.get(key) for key in ("prop_id", "holder_id", "state")) if isinstance(entry, dict) else (None,) * 3
        if not isinstance(prop, str) or prop not in context["props"] or prop in states:
            errors.append({"code": "invalid_or_duplicate_prop_state", "path": where})
        elif holder is not None and (not isinstance(holder, str) or holder not in context["cast"]):
            errors.append({"code": "unknown_reference", "path": f"{where}.holder_id"})
        elif not isinstance(state, str) or not state.strip():
            errors.append({"code": "prop_state_required", "path": f"{where}.state"})
        elif is_placeholder(state):
            errors.append({"code": "placeholder_text", "path": f"{where}.state"})
        else:
            if holder is not None and holder not in context["declared"][shot["id"]]:
                errors.append({"code": "prop_holder_undeclared", "path": f"{where}.holder_id", "character_id": holder})
            elif holder is not None and holder in shot["offscreen"]:
                warnings.append({"code": "prop_holder_offscreen", "path": f"{where}.holder_id", "character_id": holder})
            states[prop] = (holder, state)
    return states


# LLM: 数组顺序即叙事顺序；上一镜终点和下一镜起点都声明了同一道具时，持有人或标签不同且下一镜没写 continuity_break 才提醒。
# 函数用途: 逐对比较相邻镜头的道具状态，返回比较过的对数。
def prop_continuity(states: list[tuple[str, dict, dict]], warnings: list[dict]) -> int:
    pairs = 0
    for (previous_id, _, previous), (identifier, row, current) in zip(states, states[1:]):
        declared_break = isinstance(row.get("continuity_break"), str) and row["continuity_break"].strip()
        for prop in sorted(previous.get("end", {}).keys() & current.get("start", {}).keys()):
            pairs += 1
            before, after = previous["end"][prop], current["start"][prop]
            if before != after and not declared_break:
                warnings.append({"code": "prop_state_discontinuity", "path": identifier, "prop_id": prop,
                                 "previous_shot": previous_id, "previous_end": {"holder_id": before[0], "state": before[1]},
                                 "start": {"holder_id": after[0], "state": after[1]}})
    return pairs


# LLM: 道具表与镜头 prop_states 都是可选的显式声明；continuity_break 只看是否填写，不解析内容。
# 函数用途: 校验一镜的 prop_states 与 continuity_break，返回该镜的 start/end 状态字典。
def shot_prop_states(row: dict, identifier: str, context: dict, errors: list[dict], warnings: list[dict]) -> dict:
    if "continuity_break" in row and not isinstance(row["continuity_break"], str):
        errors.append({"code": "continuity_break_type", "path": f"{identifier}.continuity_break"})
    if "prop_states" not in row:
        return {}
    value = row["prop_states"]
    if not isinstance(value, dict) or not set(value) <= {"start", "end"}:
        errors.append({"code": "prop_states_shape", "path": f"{identifier}.prop_states"})
        return {}
    offscreen = row.get("offscreen_character_ids")
    shot = {"id": identifier, "offscreen": {x for x in offscreen if isinstance(x, str)} if isinstance(offscreen, list) else set()}
    return {moment: moment_prop_states(value[moment], f"{identifier}.prop_states.{moment}", shot, context, errors, warnings)
            for moment in ("start", "end") if moment in value}


# LLM: 已登记道具的名字（≥2 字）按字面出现在本镜 action 里，而本镜 prop_states 的 start/end 都没写这个道具时提醒；
#   只比字面，不推断道具是否真在画面里，也不要求没出现在动作里的道具写状态。
# 函数用途: 提醒作者给动作里用到的已登记道具写状态。
def prop_mention_warnings(props: dict, shots: dict, warnings: list[dict]) -> None:
    for identifier, row in shots.items():
        action = row.get("action") if isinstance(row.get("action"), str) else ""
        states = row.get("prop_states") if isinstance(row.get("prop_states"), dict) else {}
        entries = [entry for moment in ("start", "end") if isinstance(states.get(moment), list) for entry in states[moment]]
        declared = {entry.get("prop_id") for entry in entries if isinstance(entry, dict)}
        for prop_id, prop in props.items():
            name = prop.get("name").strip() if isinstance(prop.get("name"), str) else ""
            if len(name) >= 2 and name in action and prop_id not in declared:
                warnings.append({"code": "prop_states_missing", "path": f"{identifier}.prop_states", "prop_id": prop_id})


# LLM: 按 shots 数组顺序找道具第一次出现在 prop_states 的时刻（同一镜先 start 后 end）：那一刻已经有持有人，而道具表没写
#   origin（来源或交接说明）时提醒。只看 origin 是否填写，不解析内容；第一次出现时无人持有（例如放在地上）不提醒。
# 函数用途: 提醒作者交代道具第一次出现时从哪来、谁交给谁。
def prop_origin_warnings(props: dict, states: list[tuple[str, dict, dict]], warnings: list[dict]) -> None:
    first: dict[str, tuple[str, object]] = {}
    for identifier, _, moments in states:
        for moment in ("start", "end"):
            for prop_id, (holder, _label) in moments.get(moment, {}).items():
                first.setdefault(prop_id, (f"{identifier}.prop_states.{moment}", holder))
    for prop_id, (where, holder) in first.items():
        origin = props.get(prop_id, {}).get("origin")
        if holder is not None and not (isinstance(origin, str) and origin.strip()):
            warnings.append({"code": "prop_origin_unstated", "path": where, "prop_id": prop_id})


# LLM: 可选扩展字段统一在原结构检查后核对；不出现就不查，并用计数（0）和 dialogue_not_structured 让“没查”可见。
# 函数用途: 核对台词、逐字引用、道具表与道具状态接续，返回报告 metrics 增量。
def check_extended_fields(delivery: dict, context: dict, shots: dict, scene_sources: dict,
                          errors: list[dict], warnings: list[dict]) -> dict:
    props = index_rows(delivery, "props", errors) if "props" in delivery else {}
    for identifier, row in props.items():
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            errors.append({"code": "prop_name_required", "path": f"{identifier}.name"})
    context = {**context, "props": props}
    lines_total, by_speaker, shots_with_lines, structured, quotes, states = 0, {}, 0, False, 0, []
    for identifier, row in shots.items():
        scene_id = row.get("scene_id")
        allowed = scene_sources.get(scene_id, set()) if isinstance(scene_id, str) else set()
        declared_sources = row.get("source_ids") if isinstance(row.get("source_ids"), list) else []
        shot_sources = {value for value in declared_sources if isinstance(value, str)} & allowed
        lines = check_shot_lines(row, identifier, context, shot_sources, errors)
        if lines is not None:
            structured = True
            shots_with_lines += bool(lines)
            lines_total += len(lines)
            for line in lines:
                speaker = line.get("speaker_id") or "_narration"
                by_speaker[speaker] = by_speaker.get(speaker, 0) + 1
        quotes += check_source_quotes(row, identifier, context["passages"], shot_sources, errors)
        check_adaptation_quotes(row, identifier, context["passages"], errors)
        states.append((identifier, row, shot_prop_states(row, identifier, context, errors, warnings)))
    pairs = prop_continuity(states, warnings)
    prop_mention_warnings(props, shots, warnings)
    prop_origin_warnings(props, states, warnings)
    if not structured:
        warnings.append({"code": "dialogue_not_structured"})
    elif not lines_total:
        warnings.append({"code": "no_dialogue_lines"})
    return {"dialogue_lines": lines_total, "dialogue_lines_by_speaker": by_speaker, "shots_with_lines": shots_with_lines,
            "source_quotes": quotes, "props": len(props), "prop_state_pairs_checked": pairs}


# LLM: 名字诊断完整扫描后才看：唯一归属的名字字面出现在本镜 start/action/end，而这个人物被列在本镜 offscreen 时提醒
#   （背影、手部入画仍算可见）。只按显式 text_names 命中，不判断叙述视角；条数受 MAX_NAME_WARNINGS 限制。
# 函数用途: 提醒作者检查被点名却声明为画外的人物。
def offscreen_name_warnings(owners: dict, shots: dict, diagnostics: dict, warnings: list[dict]) -> None:
    if diagnostics.get("status") != "complete":
        return
    names = sorted(owners, key=lambda name: (-len(name), name))
    found = []
    for identifier, row in shots.items():
        offscreen = set(row["offscreen_character_ids"])
        for key in SHOT_TEXT_FIELDS:
            found.extend({"code": "named_character_offscreen", "path": f"{identifier}.{key}", "start": start, "end": end,
                          "character_id": min(owners[name])}
                         for name, start, end in literal_name_matches(row[key], names)
                         if len(owners[name]) == 1 and owners[name] <= offscreen)
    warnings.extend(found[:MAX_NAME_WARNINGS])


# LLM: 只比显式秒数和下限（默认 2 秒，可用 --min-shot-seconds 改）；只提醒，不判断动作能否在这么短时间里完成。
# 函数用途: 提醒单镜时长低于下限的镜头。
def short_shot_warnings(shots: dict, min_seconds: float, warnings: list[dict]) -> None:
    for identifier, row in shots.items():
        value = positive_seconds(row.get("seconds"))
        if value is not None and value < min_seconds:
            warnings.append({"code": "shot_too_short", "path": f"{identifier}.seconds", "seconds": value,
                             "min_seconds": min_seconds})


# LLM: 本包只核对显式 v3 声明及字面诊断，covered_passages 仍指场次采用；旧 schema 不自动升级，不能据此授予宿主完成状态。
# 函数用途: 查找来源、人物和时长声明缺项，保留名字、改编和未知警告；创作忠实、在场与接续真实性仍交独立阅读。
def check_delivery(source: object, delivery: object, source_sha256: str,
                   min_shot_seconds: float = MIN_SHOT_SECONDS_DEFAULT) -> dict:
    errors, warnings = [], []
    if not isinstance(source, dict) or not isinstance(delivery, dict):
        return {"schema": "drama_text_check.v3", "structure_valid": False, "checker": checker_identity(),
                "errors": [{"code": "object_required", "path": "$"}],
                "name_diagnostics": unchecked_name_diagnostics("structure_errors")}
    if source.get("schema") != "drama_text_source.v1" or delivery.get("schema") != "drama_text_delivery.v3":
        errors.append({"code": "unsupported_schema", "path": "schema"})
    if delivery.get("source_sha256") != source_sha256:
        errors.append({"code": "source_digest_mismatch", "path": "source_sha256"})
    passages = index_rows(source, "passages", errors)
    cast = index_rows(delivery, "cast", errors)
    name_owners, skipped = cast_text_names(cast, errors)
    scenes = index_rows(delivery, "scenes", errors)
    shots = index_rows(delivery, "shots", errors)
    for name, rows in (("passages", passages), ("scenes", scenes), ("shots", shots)):
        if not rows:
            errors.append({"code": "nonempty_required", "path": name})
    for identifier, row in passages.items():
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            errors.append({"code": "source_text_required", "path": identifier})
    covered, scene_characters, scene_sources = set(), {}, {}
    for identifier, row in scenes.items():
        refs = references(row, "source_ids", passages, identifier, errors)
        if not refs:
            errors.append({"code": "source_reference_required", "path": identifier})
        covered.update(refs)
        scene_sources[identifier] = refs
        scene_characters[identifier] = references(row, "character_ids", cast, identifier, errors)
        summary = row.get("summary")
        if isinstance(summary, str) and summary.strip() and is_placeholder(summary):
            errors.append({"code": "placeholder_text", "path": f"{identifier}.summary"})
    filmed, declared = set(), {}
    for identifier, row in shots.items():
        scene_id = row.get("scene_id")
        if not isinstance(scene_id, str) or scene_id not in scenes:
            errors.append({"code": "unknown_scene", "path": identifier})
        else:
            filmed.add(scene_id)
        scene_cast = scene_characters.get(scene_id, set()) if isinstance(scene_id, str) else set()
        declared[identifier] = shot_characters(row, {x: {} for x in scene_cast}, cast, identifier, errors)
        check_shot_basis(row, identifier, scene_sources.get(scene_id, set()) if isinstance(scene_id, str) else set(),
                         errors, warnings)
        for key in SHOT_TEXT_FIELDS:
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append({"code": "shot_state_required", "path": f"{identifier}.{key}"})
            elif is_placeholder(row[key]):
                errors.append({"code": "placeholder_text", "path": f"{identifier}.{key}"})
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
    short_shot_warnings(shots, min_shot_seconds, warnings)
    diagnostics = diagnose_names(name_owners, shots, declared, skipped, errors, warnings)
    offscreen_name_warnings(name_owners, shots, diagnostics, warnings)
    extended = check_extended_fields(delivery, {"cast": cast, "declared": declared, "passages": passages}, shots,
                                     scene_sources, errors, warnings)
    warnings.append({"code": "creative_quality_and_media_not_checked"})
    return {"schema": "drama_text_check.v3", "structure_valid": not errors, "checker": checker_identity(),
            "errors": errors, "warnings": warnings, "name_diagnostics": diagnostics,
            "metrics": {"passages": len(passages), "covered_passages": len(covered),
            "omitted_passages": len(omitted), "scenes": len(scenes), "shots": len(shots), **durations, **extended,
            "source_sha256_actual": source_sha256}}


# LLM: 宿主核验合同 pack_verifier_result.v1 的条目只带 code 和 location（沿本报告的 path，资料内 ID 路径；没有 path 记 "$"），
#   不带 message 和其它细节。
# 函数用途: 把报告里的错误或警告列表投影成宿主读的条目。
def host_items(rows: list[dict]) -> list[dict]:
    return [{"code": str(row.get("code") or ""), "location": str(row.get("path") or "$")} for row in rows]


# LLM: --host-json 时输出宿主核验合同 pack_verifier_result.v1（ae 块 2/3 定）：valid 必须等于“errors 为空”，宿主会硬校验；
#   errors/warnings 只有 code 和 location；metrics 只留数字（不含布尔、逐场字典、摘要字符串），最多 16 个键。宿主只读这些
#   结构化字段，不解释说明文字；不加参数时原 drama_text_check.v3 报告逐字节不变。
# 函数用途: 把检查报告投影成宿主读的结构化结果。
def host_result(report: dict) -> dict:
    numbers = [(key, value) for key, value in report.get("metrics", {}).items()
               if isinstance(value, (int, float)) and not isinstance(value, bool)]
    metrics = dict(numbers[:HOST_METRICS_MAX_COUNT])
    return {"schema": HOST_RESULT_SCHEMA, "valid": not report.get("errors"),
            "errors": host_items(report.get("errors", [])), "warnings": host_items(report.get("warnings", [])),
            "metrics": metrics}


# LLM: 下限必须是有限正数，坏值由 argparse 报参数错误（退出码 2），不静默改成默认。
# 函数用途: 解析 --min-shot-seconds。
def positive_float(text: str) -> float:
    value = positive_seconds(float(text)) if text.strip() else None
    if value is None:
        raise argparse.ArgumentTypeError("需要有限正数")
    return value


# LLM: 开发组件入口只读两份明确输入并打印 v3 报告，解析失败将名字诊断标为未检查；正式调用沿宿主原物化和执行链。
#   --host-json 时（宿主用钉住的原件跑，交付物是 --delivery 的 {target}，原文由宿主按声明的 task_input 交给 --source）：
#   只要写出了 v1 就退 0，有效与否只看 valid；读不了交付物记 target_unreadable，读不了原文记 source_unreadable。
# 函数用途: 从命令行检查文本资料并报告稳定错误格式；普通模式退出码表示结构检查结果，宿主模式写出结果即退 0。
def main() -> int:
    parser = argparse.ArgumentParser(description="核对短剧文本依据与分镜引用，不评价成片质量")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--host-json", action="store_true", help="按宿主核验合同 pack_verifier_result.v1 输出")
    parser.add_argument("--min-shot-seconds", type=positive_float, default=MIN_SHOT_SECONDS_DEFAULT,
                        help="单镜秒数低于它时提醒 shot_too_short（默认 2）")
    arguments = parser.parse_args()
    stage = "source"
    try:
        source, raw = read_document(arguments.source)
        stage = "delivery"
        delivery, _ = read_document(arguments.delivery)
        result = check_delivery(source, delivery, hashlib.sha256(raw).hexdigest(), arguments.min_shot_seconds)
    except (OSError, ValueError) as exc:
        result = {"schema": "drama_text_check.v3", "structure_valid": False, "checker": checker_identity(),
                  "errors": [{"code": "invalid_input", "message": str(exc)}],
                  "name_diagnostics": unchecked_name_diagnostics("invalid_input")}
        if arguments.host_json:
            result["errors"] = [{"code": "target_unreadable" if stage == "delivery" else "source_unreadable", "path": "$"}]
    print(json.dumps(host_result(result) if arguments.host_json else result, ensure_ascii=False, allow_nan=False))
    if arguments.host_json:
        return 0
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
