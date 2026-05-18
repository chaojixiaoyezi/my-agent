# LLM: Runner input dependency gates keep structured pipeline tasks from racing missing file refs.
# 模块用途: 根据 context_manifest 和 task.attributes 里的机器字段判断 runner 是否可以启动，不从自然语言里猜输入/输出。

from __future__ import annotations

import re
from pathlib import Path

from ..subagents.dependency_artifact_refs import ref_satisfied_by_dependency_artifact
from ..subagents.workspace_roots import derived_workspace_roots_from_subagent_path

_FILE_REF_RE = re.compile(
    r"(?<![\w./-])(?:/|~/)?(?:[\w.-]+/)*[\w.-]+\."
    r"(?:json|md|csv|txt|xlsx|xls|pdf|html|htm|py|yaml|yml)\b",
    re.IGNORECASE,
)
_INPUT_REF_FIELDS = frozenset({"required_read_paths", "input_refs", "input_files"})
_OUTPUT_REF_FIELDS = frozenset({"output_refs", "output_files", "artifact_refs"})


# LLM: input_dependency_ready_candidates filters runners that clearly need files not written yet.
# 函数用途: 对 runner 候选做 refs-first 输入依赖闸门；缺输入文件的下游任务等待下一轮 dispatch。
def input_dependency_ready_candidates(candidates: list, *, dependency_tasks: list | None = None) -> list:
    return [task for task in candidates if not missing_input_dependencies(task, dependency_tasks=dependency_tasks)]


# LLM: missing_input_dependencies extracts structured input refs and checks them against task roots.
# 函数用途: 返回当前 runner 缺失的输入文件引用；没有可判断根目录时不误拦截。
def missing_input_dependencies(task: object, *, dependency_tasks: list | None = None) -> list[str]:
    refs = _input_refs(task)
    if not refs:
        return []
    roots = _dependency_roots(task)
    if not roots:
        return []
    return [
        ref for ref in refs
        if (
            not _ref_exists(ref, roots)
            and not ref_satisfied_by_dependency_artifact(ref, task, dependency_tasks)
        )
    ]


# LLM: _input_refs combines context_manifest required_read_paths with task attributes.
# 函数用途: 识别已持久化的输入文件机器字段；普通自然语言派工不由代码猜。
def _input_refs(task: object) -> list[str]:
    refs: list[str] = []
    refs.extend(_manifest_required_paths(getattr(task, "context_manifest", None)))
    refs.extend(_attributes_refs(task, _INPUT_REF_FIELDS))
    return _unique_refs(refs)


# LLM: params_input_refs reads create/schedule tool parameters before task persistence.
# 函数用途: 从 required_read_paths/input_refs/input_files 等工具参数读取输入 refs，不解析 goal。
def params_input_refs(params: dict[str, object]) -> list[str]:
    manifest = params.get("context_manifest")
    manifest_refs = manifest.get("required_read_paths") if isinstance(manifest, dict) else None
    refs = [manifest_refs, *(params.get(field) for field in _INPUT_REF_FIELDS)]
    return _unique_refs([item for value in refs for item in _string_refs(value)])


# LLM: params_output_refs reads create/schedule tool parameters before task persistence.
# 函数用途: 从 output_refs/output_files/artifact_refs 等工具参数读取产物 refs，不解析 goal。
def params_output_refs(params: dict[str, object]) -> list[str]:
    return _unique_refs([item for field in _OUTPUT_REF_FIELDS for item in _string_refs(params.get(field))])


# LLM: task_output_refs reads persisted output refs from task attributes.
# 函数用途: 子代理创建后统一从 attributes 读取产物 refs，供调度、上下文包和验收复用。
def task_output_refs(task: object) -> list[str]:
    return _attributes_refs(task, _OUTPUT_REF_FIELDS)


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


# LLM: _file_refs_from_value extracts file refs from one protocol value.
# 函数用途: 从结构化字段值中读取路径，不判断其业务含义。
def _file_refs_from_value(value: object) -> list[str]:
    return [match.group() for match in _FILE_REF_RE.finditer(str(value or ""))]


# LLM: _attributes_refs reads persisted machine refs without touching task prose.
# 函数用途: 从 task.attributes 里的路径字段抽取文件 refs；非 dict attributes 按空处理。
def _attributes_refs(task: object, fields: frozenset[str]) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    return _unique_refs([item for field in fields for item in _string_refs(attrs.get(field))])


# LLM: _string_refs normalizes ref parameter values that are already structured fields.
# 函数用途: 支持字符串、列表和元组；不从普通描述段落里抽路径。
def _string_refs(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [item for raw in value for item in _string_refs(raw)]
    text = str(value or "").strip()
    return _file_refs_from_value(text) if text else []


# LLM: _dependency_roots uses private run dirs plus derived project roots as bounded lookup roots.
# 函数用途: 限制输入依赖存在性检查在 task_dir、allowed_write_roots 和受控推导出的项目根内完成。
def _dependency_roots(task: object) -> list[Path]:
    roots: list[Path] = []
    for raw in [getattr(task, "task_dir", ""), *(getattr(task, "allowed_write_roots", []) or [])]:
        if not isinstance(raw, str | Path) or not str(raw).strip():
            continue
        root = Path(raw).expanduser().resolve(strict=False)
        _append_root(roots, root)
        for workspace_root in derived_workspace_roots_from_subagent_path(root):
            _append_root(roots, workspace_root)
    return roots


# LLM: _append_root keeps dependency lookup roots deduplicated and directory-shaped.
# 函数用途: 追加存在或未创建的目录候选；如果传入文件路径则用父目录作为查找根。
def _append_root(roots: list[Path], value: Path) -> None:
    root = value if value.suffix == "" else value.parent
    if root not in roots:
        roots.append(root)


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
