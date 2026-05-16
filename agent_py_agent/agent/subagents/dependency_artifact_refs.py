# LLM: Dependency artifact refs connect workflow edges to concrete upstream files.
# 模块用途: 下游子代理依赖上游 run 时，把上游已验收 artifact refs 当作可读输入事实。

from __future__ import annotations

from pathlib import Path


# LLM: dependency_artifact_refs returns completed upstream product refs for a task.
# 函数用途: 根据 workflow_depends_on 找到已完成上游 run 的 artifact_refs，供调度闸门和执行上下文复用。
def dependency_artifact_refs(task: object, dependency_tasks: list | None) -> list[str]:
    deps = _dependency_ids(task)
    if not deps or not dependency_tasks:
        return []
    parent = _text_attr(task, "workflow_parent_run_id")
    refs: list[str] = []
    for candidate in dependency_tasks:
        if parent and _text_attr(candidate, "workflow_parent_run_id") != parent:
            continue
        phase = _text_attr(candidate, "workflow_phase_id") or _text_attr(candidate, "id")
        if phase not in deps and _text_attr(candidate, "id") not in deps:
            continue
        if not _dependency_completed(candidate):
            continue
        refs.extend(_string_list(getattr(candidate, "artifact_refs", []) or []))
    return _unique(refs)


# LLM: dependency_artifact_refs_for_required_paths narrows upstream refs to requested file names when possible.
# 函数用途: 下游 required_read_paths 写短文件名时，补入上游 run 目录里的真实同名 artifact 路径。
def dependency_artifact_refs_for_required_paths(task: object, dependency_tasks: list | None) -> list[str]:
    refs = dependency_artifact_refs(task, dependency_tasks)
    required = _required_read_paths(task)
    if not required:
        return refs
    return [artifact for artifact in refs if any(artifact_matches_ref(ref, artifact) for ref in required)]


# LLM: ref_satisfied_by_dependency_artifact lets input gates accept completed upstream artifacts.
# 函数用途: required_read_paths 的短路径在当前根目录不存在时，检查已完成上游是否产出了同名 artifact。
def ref_satisfied_by_dependency_artifact(ref: str, task: object, dependency_tasks: list | None) -> bool:
    return any(artifact_matches_ref(ref, artifact) for artifact in dependency_artifact_refs(task, dependency_tasks))


# LLM: artifact_matches_ref keeps matching conservative but friendly to short filenames.
# 函数用途: 支持 `data_collection.md` 匹配上游 run 目录里的 `/.../data_collection.md`。
def artifact_matches_ref(ref: str, artifact: str) -> bool:
    ref_text = str(ref or "").strip()
    artifact_text = str(artifact or "").strip()
    if not ref_text or not artifact_text:
        return False
    if artifact_text == ref_text or artifact_text.endswith("/" + ref_text):
        return True
    return Path(artifact_text).name == Path(ref_text).name


# LLM: _required_read_paths reads manifest shapes without importing heavy model modules.
# 函数用途: 兼容 dict、dataclass、namespace 形式的 context_manifest.required_read_paths。
def _required_read_paths(task: object) -> list[str]:
    manifest = getattr(task, "context_manifest", None)
    if isinstance(manifest, dict):
        raw = manifest.get("required_read_paths")
    else:
        raw = getattr(manifest, "required_read_paths", None)
    return _string_list(raw)


# LLM: _dependency_ids normalizes workflow phase/run ids for downstream lookup.
# 函数用途: 获取当前任务声明依赖的上游 phase id 或 run id，保持顺序和去重。
def _dependency_ids(task: object) -> list[str]:
    return _unique(_string_list(getattr(task, "workflow_depends_on", []) or []))


# LLM: _dependency_completed mirrors workflow phase readiness semantics.
# 函数用途: 只有上游进入等待验收/已完成/已验证时，才把 artifact_refs 提供给下游。
def _dependency_completed(task: object) -> bool:
    status = _text_attr(task, "status").upper()
    verification = _text_attr(task, "verification_status").upper()
    return status in {"AWAITING_ACCEPTANCE", "DONE"} or verification in {"NEEDS_ACCEPTANCE", "VERIFIED"}


# LLM: _text_attr avoids MagicMock truthiness leaking into dependency matching.
# 函数用途: 安全读取对象字符串字段；非字符串一律当空。
def _text_attr(obj: object, name: str) -> str:
    value = getattr(obj, name, "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _string_list normalizes shallow string lists.
# 函数用途: 把单个字符串或列表统一成干净字符串列表。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


# LLM: _unique preserves first-seen order for stable prompts and tests.
# 函数用途: 字符串去重但保留顺序。
def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
