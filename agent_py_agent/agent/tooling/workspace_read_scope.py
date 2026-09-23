# LLM: 只由宿主授权后的 registry 组装读取快照；精确范围与墙外授权仍来自各自原事实源，不从模型参数扩权。
# 模块用途: 将本次 cwd、读取限制和路径策略组合成隔离插件可以消费的不可变上下文。

from __future__ import annotations

from pathlib import Path

from ..path_access_policy import PathAccessPolicy
from ..workspace_read_context import WorkspaceReadContext
from .runtime_boundary import exact_read_roots


# LLM: roots 只是当前 cwd 内的附加上界；原 owner/path policy 仍逐项裁决，冻结外部策略不得在子进程重算。
# 函数用途: 固定一次工具调用的只读工作区，空交集保持拒绝全部。
def build_workspace_read_context(
    *, cwd: Path, write_boundary: dict[str, object] | None,
    path_policy: PathAccessPolicy, granted_external_roots: tuple[Path, ...],
) -> WorkspaceReadContext:
    cwd = cwd.resolve(strict=False)
    exact = exact_read_roots(write_boundary, cwd)
    roots = (cwd,) if exact is None else _intersect_roots((cwd,), exact)
    return WorkspaceReadContext(
        cwd, roots, path_policy, _intersect_roots(roots, granted_external_roots),
        PathAccessPolicy.from_values(mode=path_policy.mode, dangerous_roots=path_policy.dangerous_roots),
    )


# LLM: 子树交集只能收窄，不能把授权文件的父目录加入范围；不读取目录内容或推测文件类型。
# 函数用途: 求两个根集合共有的路径子树并移除重复项。
def _intersect_roots(left: tuple[Path, ...], right: tuple[Path, ...]) -> tuple[Path, ...]:
    result = []
    for first in left:
        for second in right:
            candidate = first if first.is_relative_to(second) else second if second.is_relative_to(first) else None
            if candidate is not None and candidate not in result:
                result.append(candidate)
    return tuple(result)
