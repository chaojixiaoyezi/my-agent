# LLM: 正在运行的 my-agent 安装目录写保护的唯一实现，由 tool_runtime_ledger.write_boundary_with_runtime_ledger 在合并
#   写边界的最后调用：安装目录对任何代理、任何模式（含 Full Access）都只读。事实放进独立键 runtime_install_roots：
#   文件工具在 tooling/write_boundary 里无条件先查，Shell/终端由 tooling/registry_invoke 并入沙箱只读路径。
#   不能借用 forbidden_write_roots：它属于写入范围键，会让 Full Access（本来就没有 allowed_write_roots）变成全部拒写。
#   开关 protect_running_runtime（默认开，边界项）。只改传入的 boundary 字典，不读模型参数与自然语言。
#   改动须同步 test_runtime_write_guards.py 与 docs/modules/gateway/04-structure.md 的 Full Access 一节。
# 模块用途: 保护运行中的 my-agent 安装不被任何工具改写，部署是唯一的更新方式。
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from ..tooling.write_boundary import RUNTIME_INSTALL_ROOTS_KEY

_INSTALLED_PACKAGE_PARENTS = frozenset({"site-packages", "dist-packages"})


# LLM: 纯函数，只按传入的进程事实判定：虚拟环境（prefix 与 base_prefix 不同）保护整个环境；否则包装在
#   site-packages/dist-packages 下时只保护包目录；从源码检出运行返回空（那是开发者自己的工作树，不能锁）。
# 函数用途: 由解释器前缀和包位置算出需要整体只读的安装目录。
def install_roots_for(prefix: Path, base_prefix: Path, package_root: Path) -> tuple[Path, ...]:
    resolved_prefix = prefix.resolve(strict=False)
    if resolved_prefix != base_prefix.resolve(strict=False):
        return (resolved_prefix,)
    resolved_package = package_root.resolve(strict=False)
    if resolved_package.parent.name in _INSTALLED_PACKAGE_PARENTS:
        return (resolved_package,)
    return ()


# LLM: 进程内缓存；包根是本文件向上两级的 agent_py_agent 目录。只读 sys 与文件路径，不访问磁盘内容。
# 函数用途: 返回当前 Gateway 进程正在运行的 my-agent 安装目录。
@lru_cache(maxsize=1)
def running_install_roots() -> tuple[Path, ...]:
    return install_roots_for(Path(sys.prefix), Path(sys.base_prefix), Path(__file__).resolve().parents[2])


# LLM: 开关读本片配置（默认开）；只写独立键 runtime_install_roots，不触碰 allowed/forbidden_write_roots，
#   所以不会改变原有的写入范围判定，只额外拒绝落在安装目录里的写入。
# 函数用途: 把正在运行的安装目录标成只读，交给文件工具与 Shell 沙箱执行。
def attach_running_install_guard(boundary: dict[str, object], config: object) -> None:
    if not bool(getattr(config, "protect_running_runtime", True)):
        return
    roots = running_install_roots()
    if roots:
        boundary[RUNTIME_INSTALL_ROOTS_KEY] = [str(root) for root in roots]


__all__ = [
    "attach_running_install_guard",
    "install_roots_for",
    "running_install_roots",
]
