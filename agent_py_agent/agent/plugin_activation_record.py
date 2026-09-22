# LLM: 激活是唯一安装表的子记录，不是执行器或操作账；字段只绑定原环境计划和发布事实，不保存配置正文或 OS 退出结论。
# 模块用途: 为准备、已发布和已撤销的同一代提供严格身份，旧客户端不能换绑另一个环境。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields

from .plugin_environment_plan import PluginEnvironmentPlan


# LLM: 同一 plan 决定唯一代次；revoked 只证明执行权关闭，资源清理必须另查原资源账，不得据此删除环境。
# 类用途: 保存一个插件版本的激活身份和发布状态，让不同进程复核同一安装事实。
@dataclass(frozen=True)
class PluginActivation:
    plan: PluginEnvironmentPlan
    phase: str
    catalog_sha256: str = ""

    # LLM: 状态只用当前结构化枚举；准备没有目录证明，发布必须有完整目录摘要，撤销保留原目录身份。
    # 函数用途: 拒绝错误计划、状态和不完整的发布记录。
    def __post_init__(self) -> None:
        if not isinstance(self.plan, PluginEnvironmentPlan) or self.phase not in {"preparing", "active", "revoked"}:
            raise ValueError("插件激活计划或状态无效")
        if (not isinstance(self.catalog_sha256, str)
                or (self.catalog_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.catalog_sha256))
                or (self.phase == "preparing" and self.catalog_sha256)
                or (self.phase == "active" and not self.catalog_sha256)):
            raise ValueError("插件激活目录摘要无效")

    # LLM: 代次只由原计划计算，不随 phase 改变；它不是授权，调用仍须读当前权威并登记原资源。
    # 函数用途: 为同次准备、发布、撤销和旧快照生成稳定身份。
    @property
    def activation_id(self) -> str:
        return hashlib.sha256(("plugin_activation.v1\0" + self.plan.resource_scope).encode()).hexdigest()

    # LLM: 摘要覆盖完整计划与本次状态，只用于最后提交回执；不能用它代替原操作账或 OS 清理回执。
    # 函数用途: 让安装提交与保存的激活内容互相核对。
    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # LLM: 安装表只存相对环境身份和版本；不接受新的个人路径、配置值、进程 PID 或客户端自行指定的 owner。
    # 函数用途: 生成可以在唯一安装表中原子保存的完整激活对象。
    def to_payload(self) -> dict:
        return {"plan": asdict(self.plan), "phase": self.phase, "catalog_sha256": self.catalog_sha256}

    # LLM: 严格字段集合与原计划校验共用；未知协议字段不能丢弃后重写成旧版本。
    # 函数用途: 从持久记录恢复一个不可变激活对象。
    @classmethod
    def from_payload(cls, value: object) -> PluginActivation:
        if not isinstance(value, dict) or set(value) != {"plan", "phase", "catalog_sha256"}:
            raise ValueError("插件激活字段无效")
        plan = value["plan"]
        if not isinstance(plan, dict) or set(plan) != {field.name for field in fields(PluginEnvironmentPlan)}:
            raise ValueError("插件激活计划字段无效")
        return cls(PluginEnvironmentPlan(**plan), value["phase"], value["catalog_sha256"])


# LLM: 摘要只描述静态完整工具清单，不能证明服务实际返回了这些工具；发布方仍必须比对同一候选 MCP 目录。
# 函数用途: 为激活回执绑定原 manifest 的工具名称、说明、输入和效果声明。
def plugin_catalog_digest(manifest) -> str:
    rows = sorted((asdict(tool) for tool in manifest.tools), key=lambda row: row["name"])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
