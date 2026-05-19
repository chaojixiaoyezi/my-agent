# LLM: Parent acceptance workspace selection must follow artifact refs, not only the primary CLI root.
# 模块用途: 根据任务产物和授权写入根，选择父级验收使用的工作区根目录，避免多 workspace 配置下扫错项目。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .parsing import _dict_list


# LLM: acceptance_workspace_root_for_task keeps parent checks bounded to the workspace containing product refs.
# 函数用途: 多工作区时优先选择包含 artifacts/allowed_write_roots 的根目录；找不到时回到 manager 的旧主工作区。
def acceptance_workspace_root_for_task(
    manager: Any,
    *,
    task: Any | None = None,
    output: dict[str, object] | None = None,
) -> Path:
    """Return the workspace root that should own this task's acceptance checks."""

    roots = _manager_roots(manager)
    refs = _task_refs(task, output or {})
    scoped = _roots_containing_refs(roots, refs)
    if scoped:
        return scoped[0]
    if fallback := _fallback_root_from_refs(refs):
        return fallback
    return roots[0]


# LLM: _manager_roots reads the manager root list defensively for direct tests and older managers.
# 函数用途: 规整 workspace_root/workspace_roots/workspace，保持第一个元素是兼容 fallback。
def _manager_roots(manager: Any) -> list[Path]:
    roots: list[Path] = []
    for value in [
        getattr(manager, "workspace_root", None),
        *(getattr(manager, "workspace_roots", []) or []),
        getattr(manager, "workspace", None),
    ]:
        path = _path_or_none(value)
        if path is not None and path not in roots:
            roots.append(path)
    return roots or [Path.cwd().resolve()]


# LLM: _task_refs collects only path-like metadata already present in task/output facts.
# 函数用途: 从 output artifacts、task.artifact_refs 和 allowed_write_roots 收集候选产物路径，不读取文件正文。
def _task_refs(task: Any | None, output: dict[str, object]) -> list[Path]:
    refs: list[Path] = []
    _extend_path_refs(refs, _output_artifact_refs(output))
    if task is None:
        return refs
    task_dir = _path_or_none(getattr(task, "task_dir", None))
    _extend_path_refs(refs, getattr(task, "artifact_refs", []) or [])
    _extend_path_refs(refs, _product_write_refs(task, task_dir))
    return refs


# LLM: _output_artifact_refs reads path-like artifact refs from parsed runner output.
# 函数用途: 将 output artifacts 的兼容字段收成一层列表，保持 _task_refs 扁平。
def _output_artifact_refs(output: dict[str, object]) -> list[object]:
    return [item.get("path") or item.get("uri") or item.get("artifact_id") for item in _dict_list(output.get("artifacts", []))]


# LLM: _product_write_refs filters task-local runtime roots from allowed write roots.
# 函数用途: 只保留可能指向真实产物的写入边界，避免验收 cwd 选到子代理 runtime。
def _product_write_refs(task: Any, task_dir: Path | None) -> list[object]:
    return [value for value in getattr(task, "allowed_write_roots", []) or [] if not _runtime_ref(value, task_dir)]


# LLM: _extend_path_refs appends normalized absolute refs to an existing refs list.
# 函数用途: 封装路径追加循环，让上层 ref 汇总函数保持简单可读。
def _extend_path_refs(refs: list[Path], values: list[object]) -> None:
    for value in values:
        _append_path_ref(refs, value)


# LLM: _runtime_ref prevents task-local work-order dirs from overriding the product workspace.
# 函数用途: allowed_write_roots 常含子代理自己的 runtime/task_dir；这些不是业务产物根，不能用于父级验收 cwd。
def _runtime_ref(value: object, task_dir: Path | None) -> bool:
    if task_dir is None:
        return False
    path = _path_or_none(value)
    return bool(path and _path_under(path, task_dir))


# LLM: _roots_containing_refs prefers the most specific configured root that contains any product ref.
# 函数用途: 在多个 workspace root 中选择包含产物路径的根；不使用 glob，也不扩展目录正文。
def _roots_containing_refs(roots: list[Path], refs: list[Path]) -> list[Path]:
    matches = [root for root in roots if any(_path_under(ref, root) for ref in refs)]
    return sorted(matches, key=lambda item: len(str(item)), reverse=True)


# LLM: _fallback_root_from_refs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _fallback_root_from_refs(refs: list[Path]) -> Path | None:
    dirs = [_artifact_dir(ref) for ref in refs]
    dirs = [item for item in dirs if item is not None]
    if not dirs:
        return None
    common = Path(str(dirs[0]))
    for item in dirs[1:]:
        common = _common_parent(common, item)
    return common


# LLM: _artifact_dir is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _artifact_dir(path: Path) -> Path | None:
    if path.suffix:
        return path.parent
    return path


# LLM: _common_parent is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _common_parent(left: Path, right: Path) -> Path:
    left_parts = left.parts
    right_parts = right.parts
    shared: list[str] = []
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        if left_part != right_part:
            break
        shared.append(left_part)
    return Path(*shared) if shared else left.anchor and Path(left.anchor) or left


# LLM: _append_path_ref normalizes literal paths while ignoring empty or URL-like values.
# 函数用途: 只把本地文件路径加入候选，避免 http/mailto 等链接影响 workspace 选择。
def _append_path_ref(refs: list[Path], value: object) -> None:
    raw = str(value or "").strip()
    if not raw or "://" in raw or raw.startswith(("mailto:", "tel:", "#")):
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return
    if resolved not in refs:
        refs.append(resolved)


# LLM: _path_under checks literal ancestry with resolved paths and no filesystem walking.
# 函数用途: 判断 ref 是否在 root 下；失败时保守返回 False。
def _path_under(ref: Path, root: Path) -> bool:
    try:
        ref.relative_to(root)
    except ValueError:
        return False
    return True


# LLM: _path_or_none accepts path-like manager attributes without trusting MagicMock placeholders.
# 函数用途: 把真实路径转成 resolve 后的 Path；空值或异常值返回 None。
def _path_or_none(value: object) -> Path | None:
    if value in (None, ""):
        return None
    try:
        return Path(value).expanduser().resolve()
    except (TypeError, OSError, RuntimeError):
        return None
