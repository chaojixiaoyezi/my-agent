# LLM: Runner ref helpers parse explicit input/output refs without gating runner startup.
# 模块用途: 只读取结构化路径字段，供提示、写根和报告使用；不得因为缺路径阻止子代理运行。

from __future__ import annotations

import re
from pathlib import Path

_FILE_REF_RE = re.compile(
    r"(?<![\w./-])(?:/|~/)?(?:[\w.-]+/)*[\w.-]+"
    r"\.[a-z0-9][a-z0-9._+-]{0,63}\b",
    re.IGNORECASE,
)
_INPUT_REF_FIELDS = frozenset({"required_read_paths", "input_refs", "input_files"})
_OUTPUT_REF_FIELDS = frozenset({"output_refs", "output_files", "artifact_refs"})
_TOOL_PATH_REF_RE = re.compile(r"^[A-Za-z_][\w.-]*:(?=/|~)")
_REF_LIKE_KEY_PARTS = frozenset({"file", "files", "path", "paths", "ref", "refs"})
_INPUT_KEY_PARTS = frozenset({
    "input",
    "inputs",
    "read",
    "source",
    "sources",
    "reference",
    "references",
    "material",
    "materials",
})
_OUTPUT_KEY_PARTS = frozenset({
    "output",
    "outputs",
    "artifact",
    "artifacts",
    "deliverable",
    "deliverables",
})


# LLM: _input_refs combines context_manifest required_read_paths with task attributes.
# 函数用途: 识别已持久化的输入文件机器字段，供提示和授权使用；普通自然语言派工不由代码猜。
def _input_refs(task: object) -> list[str]:
    refs: list[str] = []
    refs.extend(_manifest_required_paths(getattr(task, "context_manifest", None)))
    refs.extend(_attributes_refs(task, _INPUT_REF_FIELDS))
    return _unique_refs(refs)


# LLM: params_input_refs reads create/schedule tool parameters before task persistence.
# 函数用途: 从 required_read_paths/input_refs/input_files 等工具参数读取输入 refs，不解析 goal。
def params_input_refs(params: dict[str, object]) -> list[str]:
    manifest = params.get("context_manifest")
    refs = list(_manifest_input_refs(manifest))
    for field in _INPUT_REF_FIELDS:
        refs.extend(_explicit_field_refs(params.get(field)))
    return _unique_refs(refs)


# LLM: params_output_refs reads create/schedule tool parameters before task persistence.
# 函数用途: 从 output_refs/output_files/artifact_refs 等工具参数读取产物 refs，不解析 goal。
def params_output_refs(params: dict[str, object]) -> list[str]:
    manifest = params.get("context_manifest")
    refs = list(_manifest_output_refs(manifest))
    for field in _OUTPUT_REF_FIELDS:
        refs.extend(_explicit_field_refs(params.get(field)))
    return _unique_refs(refs)


# LLM: task_output_refs reads persisted output refs from task attributes.
# 函数用途: 子代理创建后统一从 attributes 读取产物 refs，供调度、上下文包和验收复用。
def task_output_refs(task: object) -> list[str]:
    return _attributes_refs(task, _OUTPUT_REF_FIELDS)


# LLM: _manifest_required_paths tolerates dataclass, namespace, and structured dict/list manifests.
# 函数用途: 从 context_manifest.required_read_paths 或纯 refs 载体读取结构化输入路径；普通说明文本不变成启动门。
def _manifest_required_paths(manifest: object) -> list[str]:
    if isinstance(manifest, dict):
        raw = manifest.get("required_read_paths")
    else:
        raw = getattr(manifest, "required_read_paths", None)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    refs = _manifest_input_refs(manifest)
    if refs:
        return refs
    return []


# LLM: _file_refs_from_value extracts file refs from one protocol value.
# 函数用途: 从结构化字段值中读取路径，不判断其业务含义。
def _file_refs_from_value(value: object) -> list[str]:
    refs: list[str] = []
    for match in _FILE_REF_RE.finditer(str(value or "")):
        ref = _normalize_file_ref(match.group())
        if _looks_like_dotted_numeric_identifier(ref):
            continue
        refs.append(ref)
    return refs


# LLM: _manifest_input_refs treats context_manifest as an open refs carrier with direction-aware keys.
# 函数用途: 只从输入语义字段提取文件 refs；输出字段和线索字段不进入 runner 启动判断。
def _manifest_input_refs(manifest: object) -> list[str]:
    if isinstance(manifest, dict):
        return _manifest_direction_refs(manifest, direction="input")
    if isinstance(manifest, list | tuple | set):
        return _string_refs(manifest)
    return _pure_ref_string_refs(manifest)


def _manifest_direction_refs(manifest: dict[str, object], *, direction: str) -> list[str]:
    refs: list[str] = []
    for key, value in manifest.items():
        if direction == "input" and _manifest_key_is_output(key):
            continue
        if _manifest_key_matches_direction(key, direction):
            refs.extend(_string_refs(value))
    return _unique_refs(refs)


def _manifest_key_matches_direction(key: object, direction: str) -> bool:
    if direction == "output":
        return _manifest_key_is_output(key)
    return _manifest_key_is_input(key)


# LLM: _pure_ref_string_refs keeps natural-language context_manifest from becoming missing inputs.
# 函数用途: 字符串 context_manifest 只有整段就是路径/纯文件列表时才当 refs；“请阅读 source.txt” 这类说明不硬拦。
def _pure_ref_string_refs(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    lines = [line.strip("-* \t") for line in text.splitlines() if line.strip("-* \t")]
    if not lines:
        return []
    if not all(_pure_ref_list_line(line) for line in lines):
        return []
    return _string_refs(lines)


def _pure_ref_list_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text or not _file_refs_from_value(text):
        return False
    cleaned = _FILE_REF_RE.sub("", text)
    return all(ch in " \t,，、;；|[]()（）'\"`" for ch in cleaned)


# LLM: _manifest_output_refs mirrors input extraction without closed file-format or artifact-kind enums.
# 函数用途: 从 output/deliverable/artifact 语义字段提取产物 refs，供写根、幂等和验收复用。
def _manifest_output_refs(manifest: object) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    return _manifest_direction_refs(manifest, direction="output")


def _manifest_key_is_input(key: object) -> bool:
    text = str(key or "").strip().lower()
    if text in _INPUT_REF_FIELDS:
        return True
    parts = set(_key_parts(text))
    if parts.intersection(_OUTPUT_KEY_PARTS):
        return False
    return bool(parts.intersection(_INPUT_KEY_PARTS) or parts.intersection(_REF_LIKE_KEY_PARTS))


def _manifest_key_is_output(key: object) -> bool:
    text = str(key or "").strip().lower()
    if text in _OUTPUT_REF_FIELDS:
        return True
    parts = set(_key_parts(text))
    return bool(parts.intersection(_OUTPUT_KEY_PARTS))


def _key_parts(value: str) -> list[str]:
    return [part for part in re.split(r"[^a-z0-9]+", value) if part]


# LLM: _attributes_refs reads persisted machine refs without touching task prose.
# 函数用途: 从 task.attributes 里的路径字段抽取文件 refs；非 dict attributes 按空处理。
def _attributes_refs(task: object, fields: frozenset[str]) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    return _unique_refs([item for field in fields for item in _explicit_field_refs(attrs.get(field))])


# LLM: explicit create/schedule fields are machine refs, so extensionless dirs remain valid.
# 函数用途: input_refs/output_files/required_read_paths 这类字段不靠后缀白名单判断；普通 prompt 不走这里。
def _explicit_field_refs(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [item for raw in value for item in _explicit_field_refs(raw)]
    if isinstance(value, dict):
        refs: list[str] = []
        for raw in value.values():
            refs.extend(_explicit_field_refs(raw))
        return refs
    return []


# LLM: _string_refs normalizes ref parameter values that are already structured fields.
# 函数用途: 支持字符串、列表和元组；不从普通描述段落里抽路径。
def _string_refs(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [item for raw in value for item in _string_refs(raw)]
    text = str(value or "").strip()
    return _file_refs_from_value(text) if text else []


# LLM: _unique_refs preserves first occurrence order for stable prompt and report rendering.
# 函数用途: 路径 ref 去重，避免同一文件重复出现在提示或报告里。
def _unique_refs(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _normalize_file_ref(value)
        if text and text not in result:
            result.append(text)
    return _drop_redundant_relative_refs(result)


# LLM: _drop_redundant_relative_refs keeps explicit full paths from being shadowed by shorthand repeats.
# 函数用途: 当同一结构化字段里同时出现 /abs/path/file.ext 和 file.ext/source/file.ext 这类短写时，
# 保留完整 ref、丢弃可由完整 ref 覆盖的相对 ref，避免给模型重复或冲突的路径线索。
def _drop_redundant_relative_refs(values: list[str]) -> list[str]:
    absolute_refs = [Path(value).expanduser() for value in values if _is_absolute_file_ref(value)]
    if not absolute_refs:
        return values
    filtered: list[str] = []
    for value in values:
        if not _is_absolute_file_ref(value) and _covered_by_absolute_ref(value, absolute_refs):
            continue
        filtered.append(value)
    return filtered


def _is_absolute_file_ref(value: str) -> bool:
    text = _normalize_file_ref(value)
    return bool(text) and "://" not in text and Path(text).expanduser().is_absolute()


def _covered_by_absolute_ref(value: str, absolute_refs: list[Path]) -> bool:
    text = _normalize_file_ref(value)
    if not text or "://" in text:
        return False
    relative = Path(text)
    if relative.is_absolute():
        return False
    relative_parts = relative.parts
    if not relative_parts:
        return False
    return any(abs_ref.parts[-len(relative_parts):] == relative_parts for abs_ref in absolute_refs)


# LLM: _normalize_file_ref strips tool-action prefixes while preserving real URLs.
# 函数用途: 兼容 read_file:/abs/path、write_file:~/x.txt 这类工具引用写法；
# 路径 ref 只保留真实文件路径，不把工具名当作路径的一部分。
def _normalize_file_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    return _TOOL_PATH_REF_RE.sub("", text, count=1).strip()


def _looks_like_dotted_numeric_identifier(value: object) -> bool:
    text = _normalize_file_ref(value)
    return bool(re.fullmatch(r"\d+(?:\.\d+){2,}", text))
