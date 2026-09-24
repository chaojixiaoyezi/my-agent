# LLM: 包描述是未授权的静态声明；不接受宿主身份、配置值或启用事实，命令和工具 schema 必须沿现有合同。
# 模块用途: 校验本地 Python 插件的内容声明，提供无需导入插件的帮助信息及不可变包元数据。

from __future__ import annotations

import json
import keyword
import re
from dataclasses import asdict, dataclass

from .command_arguments import CommandActionSpec
from .command_declarations import command_action_from_payload, declaration_list
from .plugin_commands import PluginCommandSpec
from .plugin_display.protocol import PanelDeclaration, validate_panels
from .tooling.input_schema import canonicalize_tool_input_schema, validate_tool_input

PLUGIN_PACKAGE_SCHEMA = "plugin_package.v1"
# v2 只比 v1 多一个 panels 字段；无面板的包仍按 v1 序列化，保证已安装包的描述字节不变
PLUGIN_PACKAGE_SCHEMA_V2 = "plugin_package.v2"
# v3 在 v2 基础上增加随包 Skill 名单（面板可为空）；v1/v2 包的读写字节保持不变
PLUGIN_PACKAGE_SCHEMA_V3 = "plugin_package.v3"
# v4 在 v3 基础上增加宿主 API 权限名单（目前只有 "read"：只读查询宿主运行状态）；v1–v3 包的读写字节保持不变
PLUGIN_PACKAGE_SCHEMA_V4 = "plugin_package.v4"
PLUGIN_HOST_API_PERMISSIONS = ("read",)
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_PLUGIN_SKILLS = 8
PLUGIN_SETTINGS_BYTES = 64 * 1024
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_WHEEL_PATH = re.compile(r"wheels/[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl\Z")
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


# LLM: reason 供机器分类；错误正文不能泄露来源路径、包正文或配置值，也不能反推提交状态。
# 类用途: 将包格式、预算和摘要错误区分为可展示的失败。
class PluginPackageError(ValueError):
    # LLM: 本异常只描述校验失败，不表示安装是否提交；写入服务须另带操作回执。
    # 函数用途: 保存稳定错误码及中文说明。
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# LLM: 路径仅表示 ZIP 内固定 wheel，不是宿主路径或可执行命令；摘要必须精确匹配真实字节。
# 类用途: 声明一个已构建依赖包，供读取器核对完整性。
@dataclass(frozen=True)
class PluginWheel:
    path: str
    sha256: str

    # LLM: 入口 wheel 和依赖使用同一约束，拒绝任意目录、大小写摘要及隐式路径改写。
    # 函数用途: 在读取归档前检查 wheel 的名称和摘要格式。
    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _WHEEL_PATH.fullmatch(self.path):
            raise ValueError("wheel 路径无效")
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("wheel 摘要无效")


# LLM: schema 用规范 JSON 冻结，读取时返回独立副本；requested_effect 不是宿主授予的权限或沙箱保证。
# 类用途: 保存一个工具的公开描述，避免安装后被可变字典悄悄改写。
@dataclass(frozen=True)
class PluginToolDeclaration:
    name: str
    description: str
    input_schema_json: str
    requested_effect: str

    # LLM: 所有 schema 通过原工具合同，不建立包专属类型系统；效果只接受现行执行协议值。
    # 函数用途: 验证工具身份、用途及输入描述，并固定规范形式。
    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("工具名称无效")
        _text(self.description)
        if self.requested_effect not in {"read_only", "mutating", "dangerous"}:
            raise ValueError("工具效果分类无效")
        canonical = canonicalize_tool_input_schema(json.loads(self.input_schema_json))
        object.__setattr__(self, "input_schema_json", _json(canonical))

    # LLM: 修改返回值不能改变包快照，后续 ModelSpec 仍应使用原 schema 校验器。
    # 函数用途: 为目录或工具适配器提供独立的输入 schema。
    @property
    def input_schema(self) -> dict:
        return json.loads(self.input_schema_json)


# LLM: 这是包的不可变内容，不保存安装状态、owner 或激活代次；先校验声明再允许安装服务持久保存。
# 类用途: 汇集入口、依赖、命令、工具和设置 schema，帮助可直接从这里生成。
@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    summary: str
    entry_module: str
    entry_wheel: str
    wheels: tuple[PluginWheel, ...]
    actions: tuple[CommandActionSpec, ...]
    default_action: str
    tools: tuple[PluginToolDeclaration, ...]
    settings_schema_json: str
    panels: tuple[PanelDeclaration, ...] = ()
    # 随包 Skill 名单：文件在入口包的 skills/<名称>/SKILL.md，启用期间由宿主作为最低优先级 Skill 根暴露
    skills: tuple[str, ...] = ()
    # 宿主 API 权限：声明 "read" 的插件启动时获得只读宿主 API 地址与令牌（见 plugin_host_api）
    host_api: tuple[str, ...] = ()

    # LLM: 直接构造与 JSON 读取共用约束；只开放工具动作和指向本包面板的展示动作，不接受包指定管理操作。
    #   有面板的纯展示包可以没有工具；两者都没有则拒绝。
    # 函数用途: 在包进入候选安装前拒绝重复身份、缺失入口及动作指向未声明的工具或面板。
    def __post_init__(self) -> None:
        _text(self.version)
        _text(self.summary)
        if not isinstance(self.entry_module, str) or not _MODULE.fullmatch(self.entry_module):
            raise ValueError("Python 模块入口无效")
        if any(keyword.iskeyword(part) for part in self.entry_module.split(".")):
            raise ValueError("Python 模块入口含保留字")
        for items, kind in (
            (self.wheels, PluginWheel),
            (self.actions, CommandActionSpec),
            (self.tools, PluginToolDeclaration),
        ):
            if not isinstance(items, tuple) or any(not isinstance(item, kind) for item in items):
                raise ValueError("包声明必须是不可变集合")
        wheel_names = {wheel.path.casefold() for wheel in self.wheels}
        if len(wheel_names) != len(self.wheels) or self.entry_wheel not in {
            wheel.path for wheel in self.wheels
        }:
            raise ValueError("wheel 重名或入口 wheel 缺失")
        validate_panels(self.panels)
        if (not isinstance(self.skills, tuple) or len(self.skills) > MAX_PLUGIN_SKILLS
                or len(set(self.skills)) != len(self.skills)
                or any(not isinstance(name, str) or not _SKILL_NAME.fullmatch(name) for name in self.skills)):
            raise ValueError("随包 Skill 名单无效")
        if (not isinstance(self.host_api, tuple) or len(set(self.host_api)) != len(self.host_api)
                or any(item not in PLUGIN_HOST_API_PERMISSIONS for item in self.host_api)):
            raise ValueError("宿主 API 权限名单无效")
        tool_names = {tool.name for tool in self.tools}
        panel_ids = {panel.id for panel in self.panels}
        if len(tool_names) != len(self.tools) or not (tool_names or panel_ids):
            raise ValueError("工具重名，或既没有工具也没有面板")
        for action in self.actions:
            targets = tool_names if action.kind == "tool" else panel_ids if action.kind == "display" else set()
            if action.target not in targets or action.available is not True:
                raise ValueError("动作必须引用本包已声明的工具或面板")
        _ = self.command_spec
        canonical = canonicalize_tool_input_schema(json.loads(self.settings_schema_json))
        object.__setattr__(self, "settings_schema_json", _json(canonical))

    # LLM: 包始终投影为停用且无激活代次，宿主必须从自己的权威记录另行发布实际状态。
    # 函数用途: 复用原命令声明生成帮助，不能借此将包加入模型工具目录。
    @property
    def command_spec(self) -> PluginCommandSpec:
        return PluginCommandSpec(
            self.plugin_id,
            self.summary,
            self.actions,
            default_action=self.default_action,
            package_version=self.version,
        )

    # LLM: 设置描述不是配置值或授权；返回副本以保持候选包内容固定。
    # 函数用途: 提供供后续配置验证使用的原设置 schema。
    @property
    def settings_schema(self) -> dict:
        return json.loads(self.settings_schema_json)

    # LLM: 输出只包含协议声明，不含私有运行字段；JSON 形态由命令 dataclass 和原 schema 投影产生。
    # 函数用途: 生成可复读的静态包描述。
    def to_payload(self) -> dict:
        return json.loads(
            _json(
                {
                    "schema_version": PLUGIN_PACKAGE_SCHEMA,
                    "plugin_id": self.plugin_id,
                    "version": self.version,
                    "summary": self.summary,
                    "entry_module": self.entry_module,
                    "entry_wheel": self.entry_wheel,
                    "wheels": [asdict(wheel) for wheel in self.wheels],
                    "actions": [asdict(action) for action in self.actions],
                    "default_action": self.default_action,
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                            "requested_effect": tool.requested_effect,
                        }
                        for tool in self.tools
                    ],
                    "settings_schema": self.settings_schema,
                    **({"panels": [panel.to_payload() for panel in self.panels],
                        "schema_version": PLUGIN_PACKAGE_SCHEMA_V2} if self.panels else {}),
                    **({"panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
                        "schema_version": PLUGIN_PACKAGE_SCHEMA_V3} if self.skills else {}),
                    **({"panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
                        "host_api": list(self.host_api), "schema_version": PLUGIN_PACKAGE_SCHEMA_V4}
                       if self.host_api else {}),
                }
            )
        )

    # LLM: 严格字段和有限 UTF-8 JSON 阻断包伪造宿主状态或夹带不可传输文本；不创建目录、不导入实现。
    # 函数用途: 从 JSON 对象恢复经过公共合同验证的包描述。
    @classmethod
    def from_payload(cls, payload: object) -> PluginManifest:
        try:
            _json(payload)
            names = "schema_version plugin_id version summary entry_module entry_wheel wheels actions default_action tools settings_schema"
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if version == PLUGIN_PACKAGE_SCHEMA_V2:
                names += " panels"
            elif version == PLUGIN_PACKAGE_SCHEMA_V3:
                names += " panels skills"
            elif version == PLUGIN_PACKAGE_SCHEMA_V4:
                names += " panels skills host_api"
            elif version != PLUGIN_PACKAGE_SCHEMA:
                raise ValueError("包协议版本无效")
            row = _fields(payload, names)
            panels = tuple(PanelDeclaration.from_payload(item) for item in declaration_list(row.get("panels", [])))
            if version == PLUGIN_PACKAGE_SCHEMA_V2 and not panels:
                raise ValueError("v2 包必须声明面板")
            skills = tuple(declaration_list(row.get("skills", [])))
            if version == PLUGIN_PACKAGE_SCHEMA_V3 and not skills:
                raise ValueError("v3 包必须声明随包 Skill")
            host_api = tuple(declaration_list(row.get("host_api", [])))
            if version == PLUGIN_PACKAGE_SCHEMA_V4 and not host_api:
                raise ValueError("v4 包必须声明宿主 API 权限")
            wheels = tuple(
                PluginWheel(**_fields(item, "path sha256"))
                for item in declaration_list(row["wheels"])
            )
            tools = []
            for item in declaration_list(row["tools"]):
                tool = _fields(item, "name description input_schema requested_effect")
                tools.append(
                    PluginToolDeclaration(
                        tool["name"],
                        tool["description"],
                        _json(tool["input_schema"]),
                        tool["requested_effect"],
                    )
                )
            return cls(
                plugin_id=row["plugin_id"],
                version=row["version"],
                summary=row["summary"],
                entry_module=row["entry_module"],
                entry_wheel=row["entry_wheel"],
                wheels=wheels,
                actions=tuple(
                    command_action_from_payload(item) for item in declaration_list(row["actions"])
                ),
                default_action=row["default_action"],
                tools=tuple(tools),
                settings_schema_json=_json(row["settings_schema"]),
                panels=panels,
                skills=skills,
                host_api=host_api,
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise PluginPackageError("invalid_manifest", "插件包描述无效。") from exc


# LLM: 接受当前完整协议，未知键不能静默丢弃；不从文字或默认值推断机器字段。
# 函数用途: 检查静态记录字段是否齐全。
def _fields(value: object, names: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("包描述字段不完整或存在未知字段")
    return value


# LLM: 描述及版本只是元数据，不用于路径拼接；拒绝空白或控制字符，防止终端输出产生控制效果。
# 函数用途: 验证包的可展示字符串。
def _text(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("包描述文本无效")
    value.encode("utf-8")


# LLM: 非有限数不能进入内容摘要或跨进程 schema；输出顺序固定但不是签名或来源认证。
# 函数用途: 将已验证声明冻结为稳定 JSON 文本。
def _json(value: object) -> str:
    result = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    result.encode("utf-8")
    return result


# LLM: 配置使用原工具 schema 校验器，严格拒绝而不猜类型或注入默认值；错误不能携带私有值、动态键或 schema 内容。
# 函数用途: 验证并冻结一份完整配置，供私有安装表保存；不执行插件，不把值放进公共目录。
def canonical_plugin_settings(value: object, schema: dict) -> str:
    try:
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > PLUGIN_SETTINGS_BYTES or not validate_tool_input(value, schema).ok:
            raise ValueError("配置不满足声明")
        return encoded
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError("插件配置格式或内容不符合声明。") from exc
