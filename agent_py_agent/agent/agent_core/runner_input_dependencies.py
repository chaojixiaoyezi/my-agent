# LLM: Runner input dependency gates keep natural pipeline tasks from racing missing file refs.
# 模块用途: 根据任务 goal/context_manifest 里的输入文件引用判断 runner 是否可以启动。

from __future__ import annotations

import re
from pathlib import Path

_FILE_REF_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)+[\w.-]+\."
    r"(?:json|md|csv|txt|xlsx|xls|pdf|html|htm|py|yaml|yml)\b",
    re.IGNORECASE,
)
_READ_MARKERS = (
    "读取",
    "读",
    "基于",
    "根据",
    "依赖",
    "输入",
    "汇总",
    "整理",
    "分析",
    "read",
    "from",
    "input",
    "using",
    "load",
    "source",
)
_WRITE_MARKERS = (
    "输出",
    "写入",
    "生成",
    "创建",
    "保存",
    "产出",
    "write",
    "output",
    "create",
    "generate",
    "save",
    "export",
)


# LLM: input_dependency_ready_candidates filters runners that clearly need files not written yet.
# 函数用途: 对 runner 候选做 refs-first 输入依赖闸门；缺输入文件的下游任务等待下一轮 dispatch。
def input_dependency_ready_candidates(candidates: list) -> list:
    return [task for task in candidates if not missing_input_dependencies(task)]


# LLM: missing_input_dependencies extracts explicit and natural input refs and checks them against task roots.
# 函数用途: 返回当前 runner 缺失的输入文件引用；没有可判断根目录时不误拦截。
def missing_input_dependencies(task: object) -> list[str]:
    refs = _input_refs(task)
    if not refs:
        return []
    roots = _dependency_roots(task)
    if not roots:
        return []
    return [ref for ref in refs if not _ref_exists(ref, roots)]


# LLM: _input_refs combines structured required_read_paths with "读取 data/x.json" prose fallbacks.
# 函数用途: 优先识别机器字段，兼容模型自然语言派工里的输入文件提示。
def _input_refs(task: object) -> list[str]:
    refs: list[str] = []
    refs.extend(_manifest_required_paths(getattr(task, "context_manifest", None)))
    goal = str(getattr(task, "goal", "") or "")
    for match in _FILE_REF_RE.finditer(goal):
        if _path_ref_is_input(goal, match.start()):
            refs.append(match.group())
    return _unique_refs(refs)


# LLM: _manifest_required_paths tolerates dataclass, namespace, and dict context manifests.
# 函数用途: 从 context_manifest.required_read_paths 读取结构化输入路径。
def _manifest_required_paths(manifest: object) -> list[str]:
    if isinstance(manifest, dict):
        raw = manifest.get("required_read_paths")
    else:
        raw = getattr(manifest, "required_read_paths", None)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    return []


# LLM: _path_ref_is_input keeps output targets like "生成 data/out.json" from blocking their producer.
# 函数用途: 只把 read/from/input 等上下文中的文件当作输入依赖，写入上下文中的文件不拦截。
def _path_ref_is_input(text: str, start: int) -> bool:
    prefix = str(text[max(0, start - 24):start]).lower()
    if any(marker in prefix for marker in _WRITE_MARKERS):
        return False
    return any(marker in prefix for marker in _READ_MARKERS)


# LLM: _dependency_roots uses task allowed_write_roots plus task_dir as bounded lookup roots.
# 函数用途: 限制输入依赖存在性检查在当前任务已授权的工作目录内完成。
def _dependency_roots(task: object) -> list[Path]:
    roots: list[Path] = []
    for raw in [getattr(task, "task_dir", ""), *(getattr(task, "allowed_write_roots", []) or [])]:
        if not isinstance(raw, str | Path) or not str(raw).strip():
            continue
        root = Path(raw).expanduser().resolve(strict=False)
        if root not in roots:
            roots.append(root)
    return roots


# LLM: _ref_exists resolves absolute refs directly and relative refs below dependency roots.
# 函数用途: 判断输入文件是否存在；URL/协议引用交给 runner 自己处理，不在文件闸门里阻塞。
def _ref_exists(ref: str, roots: list[Path]) -> bool:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return True
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.exists()
    return any((root / path).exists() for root in roots)


# LLM: _unique_refs preserves first occurrence order for stable dispatch behavior.
# 函数用途: 输入依赖路径去重，避免同一缺失文件重复出现在诊断里。
def _unique_refs(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
