# LLM: 激活是唯一安装表的子记录，不是执行器或操作账；原进程格式绑定环境，显式新内容格式没有环境或 OS 退出结论。
# 模块用途: 保留进程激活原格式并识别内容变体，让准备、发布、撤销不能换绑身份。

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields

from .common.strict_json import load_strict_json
from .plugin_content_activation import CONTENT_ACTIVATION_SCHEMA, PluginContentActivation
from .plugin_environment_plan import PluginEnvironmentPlan
from .plugin_manifest import _TOOL_NAME, PluginToolDeclaration


# LLM: 同一 plan 决定唯一代次，权限写进原激活且不随配置热变；旧 None 保留原字节，撤销不证明资源退出。
# 类用途: 保存一个插件版本的激活身份和发布状态，让不同进程复核同一安装事实。
@dataclass(frozen=True)
class PluginActivation:
    plan: PluginEnvironmentPlan
    phase: str
    catalog_sha256: str = ""
    permission_json: str | None = field(default=None, repr=False)
    host_api_grant_json: str | None = field(default=None, repr=False)
    pinned_catalog_json: str | None = field(default=None, repr=False)

    # LLM: 状态按有限枚举，v9组合确认拆为独立OS/API授权；两者绑定固定计划，不是隔离或目录验收证明。
    # 函数用途: 拒绝错误计划、状态和不完整的发布记录。
    def __post_init__(self) -> None:
        if not isinstance(self.plan, PluginEnvironmentPlan) or self.phase not in {"preparing", "active", "revoked"}:
            raise ValueError("插件激活计划或状态无效")
        if (not isinstance(self.catalog_sha256, str)
                or (self.catalog_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.catalog_sha256))
                or (self.phase == "preparing" and self.catalog_sha256)
                or (self.phase == "active" and not self.catalog_sha256)):
            raise ValueError("插件激活目录摘要无效")
        # pin 只属于已发布或撤销的目录，不能承运或借用旧 OS/API 授权。
        if self.pinned_catalog_json is not None:
            if (self.phase not in {"active", "revoked"} or not self.catalog_sha256
                    or self.permission_json is not None or self.host_api_grant_json is not None):
                raise ValueError("固定目录的状态或授权组合无效")
            pinned_tool_declarations(self.pinned_catalog_json)
        # 归一化并核验插件激活记录的 OS 与宿主 API v2 授权集合。
        from .plugin_permissions.host_grants import normalize_activation_grant
        from .plugin_permissions.records import validate_activation_permission

        normalize_activation_grant(self)
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

    # LLM: 新字段为None时保持旧字节；完整OS/API授权仅存原安装表，不输出普通公开目录。
    # 函数用途: 生成可以在唯一安装表中原子保存的完整激活对象。
    def to_payload(self) -> dict:
        value = {"plan": asdict(self.plan), "phase": self.phase, "catalog_sha256": self.catalog_sha256}
        if self.permission_json is not None:
            value["permission_grant"] = load_strict_json(self.permission_json)
        if self.host_api_grant_json is not None:
            value["host_api_grant"] = load_strict_json(self.host_api_grant_json)
        if self.pinned_catalog_json is not None:
            value["pinned_catalog"] = json.loads(self.pinned_catalog_json)
        return value

    # LLM: 内容仍走原合同；API授权只可与OS授权成对出现，旧字段保持原摘要，不缺字段补兼容。
    # 函数用途: 从持久记录恢复一个不可变激活对象。
    @classmethod
    def from_payload(cls, value: object) -> PluginActivation | PluginContentActivation:
        if isinstance(value, dict) and value.get("schema_version") == CONTENT_ACTIVATION_SCHEMA:
            return PluginContentActivation.from_payload(value)
        old = {"plan", "phase", "catalog_sha256"}
        if not isinstance(value, dict) or set(value) not in (
                old, old | {"permission_grant"}, old | {"permission_grant", "host_api_grant"},
                old | {"pinned_catalog"}):
            raise ValueError("插件激活字段无效")
        plan = value["plan"]
        if not isinstance(plan, dict) or set(plan) != {field.name for field in fields(PluginEnvironmentPlan)}:
            raise ValueError("插件激活计划字段无效")
        permission_json = (json.dumps(value["permission_grant"], ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False) if "permission_grant" in value else None)
        host_json = (json.dumps(value["host_api_grant"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) if "host_api_grant" in value else None)
        pinned_json = (json.dumps(value["pinned_catalog"], ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False) if "pinned_catalog" in value else None)
        return cls(PluginEnvironmentPlan(**plan), value["phase"], value["catalog_sha256"], permission_json, host_json,
                   pinned_json)


# LLM: 摘要只描述静态完整工具清单，不能证明服务实际返回了这些工具；发布方仍必须比对同一候选 MCP 目录。
#   这个摘要被持久写进每条激活记录并在每次读安装表时复核，所以它必须跨运行时版本稳定：只哈希有值的字段，
#   声明类新增的可选字段（如 v5 的 observation/observation_ref）为 None 时不进入摘要。2026-09-25 真实 owner 上的教训：
#   直接哈希 asdict(tool) 让升级后所有既有激活的摘要变化，整张安装表被判"不可读"，插件工具/Skill/管理全部消失。
#   以后给 PluginToolDeclaration 加字段必须默认 None/空，并跑 test_plugin_catalog_digest_stability.py。
# 函数用途: 为激活回执绑定原 manifest 的工具名称、说明、输入和效果声明；旧安装升级后仍能对上原摘要。
def plugin_catalog_digest(manifest) -> str:
    return catalog_digest_of(manifest.tools)


def catalog_digest_of(tools: tuple[PluginToolDeclaration, ...]) -> str:
    """复用旧目录摘要公式；新增的 None 可选字段不改变持久激活的摘要。"""
    rows = sorted(
        ({key: value for key, value in asdict(tool).items() if value is not None} for tool in tools),
        key=lambda row: row["name"],
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pinned_catalog_json_from_tools(infos: Sequence[object]) -> str:
    """将有限 MCP 目录冻结为同一份 UTF-8 DTO；服务端注解不能降低 dangerous 效果。"""
    from .tooling.input_schema import canonicalize_tool_input_schema

    max_tools, max_bytes = 64, 262144
    try:
        if not isinstance(infos, Sequence) or not 1 <= len(infos) <= max_tools:
            raise ValueError("目录条数无效")
        rows = []
        names = set()
        for info in infos:
            name, description = info.name, info.description
            if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in names:
                raise ValueError("目录名称无效或重复")
            if not isinstance(description, str):
                raise ValueError("目录描述无效")
            names.add(name)
            description = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in description)
            description = re.sub(r" +", " ", description).strip() or f"工具 {name}"
            schema = canonicalize_tool_input_schema(info.input_schema)
            rows.append({"name": name, "description": description, "input_schema": schema,
                         "requested_effect": "dangerous"})
        rows.sort(key=lambda row: row["name"])
        text = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError("目录字节数超限")
        return text
    except (AttributeError, TypeError, ValueError, RecursionError, OverflowError):
        raise ValueError("MCP_PACK_CATALOG_INVALID") from None


def pinned_tool_declarations(pinned_catalog_json: str) -> tuple[PluginToolDeclaration, ...]:
    """严格恢复固定目录，拒绝额外字段、重复键及任何非规范字节，不修正持久 pin。"""
    max_tools, max_bytes = 64, 262144
    try:
        if not isinstance(pinned_catalog_json, str) or len(pinned_catalog_json.encode("utf-8")) > max_bytes:
            raise ValueError("目录文本无效")
        rows = load_strict_json(pinned_catalog_json)
        if not isinstance(rows, list) or not 1 <= len(rows) <= max_tools:
            raise ValueError("目录条数无效")
        tools = []
        for row in rows:
            if (not isinstance(row, dict)
                    or set(row) != {"name", "description", "input_schema", "requested_effect"}
                    or row["requested_effect"] != "dangerous"):
                raise ValueError("目录字段或效果无效")
            schema_json = json.dumps(row["input_schema"], ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False)
            tools.append(PluginToolDeclaration(row["name"], row["description"], schema_json, "dangerous"))
        result = tuple(tools)
        if pinned_catalog_json_from_tools(result) != pinned_catalog_json:
            raise ValueError("目录不是规范 JSON")
        return result
    except (AttributeError, TypeError, ValueError, RecursionError, OverflowError):
        raise ValueError("MCP_PACK_CATALOG_INVALID") from None


def installation_tool_declarations(installation: object) -> tuple[PluginToolDeclaration, ...]:
    """只投影安装快照的静态或固定目录，不读表、不启动服务，也不推导可调用权限。"""
    manifest, activation = installation.manifest, installation.activation
    servers = getattr(manifest, "mcp_servers", ())
    if servers and servers[0].tool_catalog == "pinned_on_enable":
        pin = getattr(activation, "pinned_catalog_json", None)
        return pinned_tool_declarations(pin) if pin is not None else ()
    if getattr(activation, "pinned_catalog_json", None) is not None:
        raise ValueError("声明目录不能承载固定目录")
    return manifest.tools


def activation_catalog_digest(manifest: object, activation: object) -> str:
    """表解码、激活转换与启用共用的摘要来源；只有显式 pinned 模式可读取 pin。"""
    servers = getattr(manifest, "mcp_servers", ())
    pin = getattr(activation, "pinned_catalog_json", None)
    if servers and servers[0].tool_catalog == "pinned_on_enable":
        return catalog_digest_of(pinned_tool_declarations(pin) if pin is not None else ())
    if pin is not None:
        raise ValueError("声明目录不能承载固定目录")
    return plugin_catalog_digest(manifest)
