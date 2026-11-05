"""从父级冻结能力快照投影包工具候选；不读盘、不访问注册表、不独立授予执行权。"""
from __future__ import annotations

from collections.abc import Iterable

from .package_snapshot import CapabilityPackageSnapshot


def pack_linked_tool_names(snapshot: object, references: Iterable[str]) -> tuple[str, ...]:
    """按引用顺序去重联动工具；调用方必须再与直属父级当前工具权限取交集。"""
    names: dict[str, None] = {}
    for reference in references:
        if not isinstance(reference, str) or not reference.startswith("capability:"):
            continue
        package = snapshot.resolve_reference(reference)
        if isinstance(package, CapabilityPackageSnapshot) and package.mcp_runtime == "on_demand":
            names.update(dict.fromkeys(package.mcp_tool_names))
    return tuple(names)
