# LLM: 激活是唯一安装表的子记录，不是执行器或操作账；原进程格式绑定环境，显式新内容格式没有环境或 OS 退出结论。
# 模块用途: 保留进程激活原格式并识别内容变体，让准备、发布、撤销不能换绑身份。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields

from .common.strict_json import load_strict_json
from .plugin_content_activation import CONTENT_ACTIVATION_SCHEMA, PluginContentActivation
from .plugin_environment_plan import PluginEnvironmentPlan


# LLM: 同一 plan 决定唯一代次，权限写进原激活且不随配置热变；旧 None 保留原字节，撤销不证明资源退出。
# 类用途: 保存一个插件版本的激活身份和发布状态，让不同进程复核同一安装事实。
@dataclass(frozen=True)
class PluginActivation:
    plan: PluginEnvironmentPlan
    phase: str
    catalog_sha256: str = ""
    permission_json: str | None = field(default=None, repr=False)

    # LLM: 状态按有限枚举，授权按固定计划；准备/授权事实都不是 OS 隔离或目录验收证明。
    # 函数用途: 拒绝错误计划、状态和不完整的发布记录。
    def __post_init__(self) -> None:
        if not isinstance(self.plan, PluginEnvironmentPlan) or self.phase not in {"preparing", "active", "revoked"}:
            raise ValueError("插件激活计划或状态无效")
        if (not isinstance(self.catalog_sha256, str)
                or (self.catalog_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.catalog_sha256))
                or (self.phase == "preparing" and self.catalog_sha256)
                or (self.phase == "active" and not self.catalog_sha256)):
            raise ValueError("插件激活目录摘要无效")
        from .plugin_permissions.records import validate_activation_permission

        validate_activation_permission(self)

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

    # LLM: 原字段在权限 None 时字节保持；完整授权路径只存私有安装表，不直接输出到模型或普通公开目录。
    # 函数用途: 生成可以在唯一安装表中原子保存的完整激活对象。
    def to_payload(self) -> dict:
        value = {"plan": asdict(self.plan), "phase": self.phase, "catalog_sha256": self.catalog_sha256}
        if self.permission_json is not None:
            value["permission_grant"] = load_strict_json(self.permission_json)
        return value

    # LLM: 内容仍走原合同；新进程授权字段严格验证，旧字段无权限时保持原摘要，不能缺字段补兼容。
    # 函数用途: 从持久记录恢复一个不可变激活对象。
    @classmethod
    def from_payload(cls, value: object) -> PluginActivation | PluginContentActivation:
        if isinstance(value, dict) and value.get("schema_version") == CONTENT_ACTIVATION_SCHEMA:
            return PluginContentActivation.from_payload(value)
        old = {"plan", "phase", "catalog_sha256"}
        if not isinstance(value, dict) or set(value) not in (old, old | {"permission_grant"}):
            raise ValueError("插件激活字段无效")
        plan = value["plan"]
        if not isinstance(plan, dict) or set(plan) != {field.name for field in fields(PluginEnvironmentPlan)}:
            raise ValueError("插件激活计划字段无效")
        permission_json = (json.dumps(value["permission_grant"], ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False) if "permission_grant" in value else None)
        return cls(PluginEnvironmentPlan(**plan), value["phase"], value["catalog_sha256"], permission_json)


# LLM: 摘要只描述静态完整工具清单，不能证明服务实际返回了这些工具；发布方仍必须比对同一候选 MCP 目录。
#   这个摘要被持久写进每条激活记录并在每次读安装表时复核，所以它必须跨运行时版本稳定：只哈希有值的字段，
#   声明类新增的可选字段（如 v5 的 observation/observation_ref）为 None 时不进入摘要。2026-09-25 真实 owner 上的教训：
#   直接哈希 asdict(tool) 让升级后所有既有激活的摘要变化，整张安装表被判"不可读"，插件工具/Skill/管理全部消失。
#   以后给 PluginToolDeclaration 加字段必须默认 None/空，并跑 test_plugin_catalog_digest_stability.py。
# 函数用途: 为激活回执绑定原 manifest 的工具名称、说明、输入和效果声明；旧安装升级后仍能对上原摘要。
def plugin_catalog_digest(manifest) -> str:
    rows = sorted(
        ({key: value for key, value in asdict(tool).items() if value is not None} for tool in manifest.tools),
        key=lambda row: row["name"],
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
