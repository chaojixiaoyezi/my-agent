# LLM: 这里只定义老格式授权到 B7 规格构造器的接缝，不生成 Seatbelt/bwrap 规则。
# 模块用途: 等待唯一 OS 沙箱底座，未接入时不退回无沙箱启动。
from __future__ import annotations

from dataclasses import dataclass


# LLM: 基础根由运行时解析器提供，不能从插件参数猜 HOME 或程序前缀。
# 类用途: 给 B7 传固定插件、数据、解释器与系统目录。
@dataclass(frozen=True)
class SandboxBase:
    package_root: str
    data_root: str
    interpreter_prefix: str
    system_roots: tuple[str, ...]


# LLM: B7 消费固定基础根与授权增量；network 是诚实的总开关，不代表 host_api 专线。
# 类用途: 给统一规格构造器一个完整请求，不包含平台沙箱规则。
@dataclass(frozen=True)
class LegacySandboxRequest:
    base: SandboxBase
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    program_roots: tuple[str, ...]
    network: bool


# LLM: builder 必须是集成后的 B7 构造器；本接口不启动进程，不将请求当隔离证明。
# 函数用途: 将授权事实交给统一底座，底座缺失时明确拒绝。
def sandbox_for_permissions(details: dict, base: SandboxBase, builder=None):
    if details.get("kind") != "plugin_legacy_permissions" or details.get("policy_version") != 1:
        raise ValueError("沙箱授权版本无效")
    if details.get("mode") != "restricted":
        raise ValueError("宽权限/旧兼容模式不声称强制沙箱")
    if builder is None:
        raise RuntimeError("legacy_sandbox_pending")
    permissions = details["permissions"]
    return builder(LegacySandboxRequest(
        base, tuple(row["path"] for row in permissions["read_roots"]),
        tuple(row["path"] for row in permissions["write_roots"]),
        tuple(row["path"] for row in permissions["program_roots"]), permissions["network"],
    ))
