# LLM: 只由宿主授权后的 registry 组装写入快照；边界字段用 write_boundary 的同一解析函数取得，不从模型参数扩权。
#   有任务写入范围时与内置写工具同根；没有时只允许 cwd（比内置工具更严）。修改须同步 test_workspace_write_context 的逐项比对。
# 模块用途: 把本次调用的写入边界冻结成插件可消费的不可变写入上下文。

from __future__ import annotations

from pathlib import Path

from ..path_access_policy import PathAccessPolicy
from ..workspace_write_context import WorkspaceWriteContext
from .write_boundary import _boundary_enforces_write_scope, _boundary_paths


# LLM: 解析与 validate_write_boundary 相同：相对路径按 cwd 解析并 resolve；无法解析的条目丢弃（与原实现一致）。
# 函数用途: 固定一次工具调用的可写根、禁止根、锁定文件与内部结果文件约束。
def build_workspace_write_context(
    *, cwd: Path, write_boundary: dict[str, object] | None, path_policy: PathAccessPolicy,
) -> WorkspaceWriteContext:
    cwd = cwd.resolve(strict=False)
    boundary = write_boundary or {}
    if _boundary_enforces_write_scope(boundary):
        roots = tuple(dict.fromkeys(_boundary_paths(boundary.get("allowed_write_roots"), cwd)))
        forbidden = tuple(dict.fromkeys(_boundary_paths(boundary.get("forbidden_write_roots"), cwd)))
        locked = tuple(dict.fromkeys(_boundary_paths(boundary.get("locked_files"), cwd)))
        outputs = _boundary_paths([boundary.get("output_json")], cwd)
        products = tuple(dict.fromkeys(_boundary_paths(boundary.get("product_write_roots"), cwd)))
    else:
        roots, forbidden, locked, outputs, products = (cwd,), (), (), [], ()
    return WorkspaceWriteContext(cwd, roots, forbidden, locked, outputs[0] if outputs else None, products, path_policy)
