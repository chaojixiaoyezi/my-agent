# LLM: 层级子代理写根继承必须保持单调收窄；存在 exact_write_set 或 allowed_output_dirs 时绝不把父级宽工作目录并入。
# 模块用途: 计算层级调度子代理的写根范围与父级写入继承。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models import SubAgentTask


# LLM: 调度写根请求入参对象，保留角色与叶子写入意图。
# 类用途: 传递层级写根计算的请求上下文。
@dataclass(frozen=True)
class ScheduledWriteRootRequest:
    role: str
    spec_role: str
    requested_roots: list[str]
    leaf_write_intent: bool


# LLM: 子代理写根继承请求，包含父任务与显式声明根。
# 类用途: 为下级子代理提供父级任务与显式授权写根。
@dataclass(frozen=True)
class ChildWriteRootRequest:
    parent: SubAgentTask
    spec_goal: str
    explicit_roots: list[str]
    spec_attributes: dict[str, object] | None = None


# LLM: 规范化请求的写根列表并保持出现顺序。
# 函数用途: 去重并返回调度子代理显式写根。
def scheduled_child_extra_write_roots(request: ScheduledWriteRootRequest) -> list[str]:
    return list(dict.fromkeys(request.requested_roots))


# LLM: 判定请求是否包含精确文件写集合；若是则必须隔离父级宽目录。
# 函数用途: 检查是否存在 exact_write_set 或具体文件写限制。
def _has_exact_write_set(request: ChildWriteRootRequest) -> bool:
    parent_attrs = getattr(request.parent, "attributes", None)
    if isinstance(parent_attrs, dict):
        constraints = parent_attrs.get("delegation_constraints")
        if isinstance(constraints, dict) and (
            constraints.get("exact_write_set") or constraints.get("allowed_output_dirs")
        ):
            return True

    spec_attrs = request.spec_attributes
    if isinstance(spec_attrs, dict):
        constraints = spec_attrs.get("delegation_constraints")
        if isinstance(constraints, dict) and (
            constraints.get("exact_write_set") or constraints.get("allowed_output_dirs")
        ):
            return True

    if request.explicit_roots:
        for r in request.explicit_roots:
            p = Path(r)
            if p.suffix or (p.exists() and p.is_file()):
                return True
    return False


# LLM: 父级已授权产品根与显式指定写根结合；当指定 exact_write_set 时，绝对不把父级宽工作区目录混入，确保只允许列出的具体文件。
# 函数用途: 计算层级子代理的最终请求写根。
def requested_child_write_roots(request: ChildWriteRootRequest) -> list[str]:
    roots: list[str] = []
    if _has_exact_write_set(request):
        for item in request.explicit_roots:
            text = str(item or "").rstrip("/")
            if text and text not in roots:
                roots.append(text)
        return roots

    for item in [
        *request.explicit_roots,
        *inherited_extra_write_roots(request.parent),
    ]:
        text = str(item or "").rstrip("/")
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: 计算父代理允许传承给后代的产品写根，排除任务内部目录。
# 函数用途: 提取父任务的有效额外写根。
def inherited_extra_write_roots(parent: SubAgentTask) -> list[str]:
    parent_task_dir = str(parent.task_dir or "").rstrip("/")
    roots: list[str] = []
    authorized_roots = _non_task_allowed_roots(parent, parent_task_dir)
    for item in authorized_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(str(item))
    return roots


# LLM: 剔除任务内部工作目录，避免子代理把父代理私有状态误当产品目录。
# 函数用途: 过滤掉与 task_dir 相同的写根。
def _non_task_allowed_roots(parent: SubAgentTask, parent_task_dir: str) -> list[str]:
    roots: list[str] = []
    for item in parent.allowed_write_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(text)
    return roots


# LLM: 判定待测试候选路径是否已被某一个已授权根包含。
# 函数用途: 检查路径是否落在已有授权写根下。
def _covered_by_authorized_root(candidate: str, roots: list[str]) -> bool:
    if not candidate:
        return False
    candidate_path = Path(candidate).expanduser().resolve(strict=False)
    for raw in roots:
        root = Path(str(raw)).expanduser().resolve(strict=False)
        try:
            candidate_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
