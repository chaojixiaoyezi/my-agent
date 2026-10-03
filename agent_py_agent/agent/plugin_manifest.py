# LLM: 包描述是未授权的静态声明；不接受宿主身份、配置值或启用事实，命令和工具 schema 必须沿现有合同。
#   v8 沿 v6 文件入口且可只贡献订阅；联测旧 v1–v7 固定字节、贡献门和启用确认码，不在此授予权限。
# 模块用途: 校验 Python、非 Python、纯内容能力包及 v8 订阅，提供不导入插件的帮助及不可变元数据。

from __future__ import annotations

import json
import keyword
import re
from dataclasses import asdict, dataclass

from .capability_package_manifest import (
    CAPABILITY_PACKAGE_SCHEMA,
    CapabilityDeclaration,
    CapabilityFile,
    validate_capability_files,
)
from .command_arguments import CommandActionSpec
from .command_declarations import command_action_from_payload, declaration_list
from .plugin_commands import PluginCommandSpec
from .plugin_display.protocol import PanelDeclaration, validate_panels
from .plugin_entry import PluginEntry, PluginFile, validate_entry_files
from .plugin_events.declarations import (
    PluginEventDeclaration,
    PluginEventPermissions,
    PluginToolGateDeclaration,
    subscriptions_from_payload,
    validate_subscriptions,
)
from .plugin_observation import (
    MAX_CANDIDATE_COUNT,
    PluginToolObservation,
    PluginToolObservationRef,
    validate_observation_declaration,
)
from .tooling.input_schema import canonicalize_tool_input_schema, validate_tool_input

PLUGIN_PACKAGE_SCHEMA = "plugin_package.v1"
# v2 只比 v1 多一个 panels 字段；无面板的包仍按 v1 序列化，保证已安装包的描述字节不变
PLUGIN_PACKAGE_SCHEMA_V2 = "plugin_package.v2"
# v3 在 v2 基础上增加随包 Skill 名单（面板可为空）；v1/v2 包的读写字节保持不变
PLUGIN_PACKAGE_SCHEMA_V3 = "plugin_package.v3"
# v4 在 v3 基础上增加宿主 API 权限名单（目前只有 "read"：只读查询宿主运行状态）；v1–v3 包的读写字节保持不变
PLUGIN_PACKAGE_SCHEMA_V4 = "plugin_package.v4"
# v5 在 v4 基础上允许工具项多两个可选字段：只读工具的 observation（结果带候选观察）与动作工具的 observation_ref
# （接受同类观察的候选 ID）；面板/Skill/宿主 API 可为空。v1–v4 包的读写字节保持不变
PLUGIN_PACKAGE_SCHEMA_V5 = "plugin_package.v5"
# v6 是非 Python 插件：用结构化 entry（随包可执行文件 / 系统解释器加随包脚本）与 files、platforms 取代 Python 模块和 wheel；
# 面板/Skill/宿主 API/观察字段照旧可选。Python 包继续用 v1–v5，读写字节不变
PLUGIN_PACKAGE_SCHEMA_V6 = "plugin_package.v6"
# v8 只在 v6 文件入口上新增事件/收紧/权限；没有订阅的包仍用 v6，不改变旧版本输出字节。
PLUGIN_PACKAGE_SCHEMA_V8 = "plugin_package.v8"
PLUGIN_HOST_API_PERMISSIONS = ("read",)
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# 单个插件最多可挂载的 skill 数；防止插件注入海量 skill 占用命名空间。
MAX_PLUGIN_SKILL_COUNT = 8
# 插件设置项总大小的最大字节数；超出视为清单超限，防止巨型设置拖慢解析。
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
#   observation / observation_ref 是 v5 的可选声明：前者只能挂在 read_only 工具上，后者要求 param 是输入 schema 里可选的
#   string 参数；一个工具不能同时观察与动作。
# 类用途: 保存一个工具的公开描述，避免安装后被可变字典悄悄改写。
@dataclass(frozen=True)
class PluginToolDeclaration:
    name: str
    description: str
    input_schema_json: str
    requested_effect: str
    observation: PluginToolObservation | None = None
    observation_ref: PluginToolObservationRef | None = None

    # LLM: 所有 schema 通过原工具合同，不建立包专属类型系统；效果只接受现行执行协议值。
    # 函数用途: 验证工具身份、用途、输入描述与观察声明，并固定规范形式。
    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("工具名称无效")
        _text(self.description)
        if self.requested_effect not in {"read_only", "mutating", "dangerous"}:
            raise ValueError("工具效果分类无效")
        canonical = canonicalize_tool_input_schema(json.loads(self.input_schema_json))
        object.__setattr__(self, "input_schema_json", _json(canonical))
        _validate_observation_declaration(self, canonical)

    # LLM: 修改返回值不能改变包快照，后续 ModelSpec 仍应使用原 schema 校验器。
    # 函数用途: 为目录或工具适配器提供独立的输入 schema。
    @property
    def input_schema(self) -> dict:
        return json.loads(self.input_schema_json)


# LLM: 规则本体在 plugin_observation.validate_observation_declaration（插件 manifest 与 MCP 声明表共用，错误是 ValueError 子类）；
#   这里只把工具项的结构化字段交过去，不另写一份规则。
# 函数用途: 校验一个工具声明里的观察/观察引用与其 effect、输入 schema 是否一致。
def _validate_observation_declaration(tool: PluginToolDeclaration, canonical: object) -> None:
    validate_observation_declaration(tool.observation, tool.observation_ref, effect=tool.requested_effect, input_schema=canonical)


# LLM: 这是包的不可变内容，不保存安装状态、owner 或激活代次；v8 权限对象显式区分新协议，旧包保持 None。
# 类用途: 汇集入口、依赖、命令、工具、订阅与设置，先验证声明再交给安装服务。
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
    # v6 非 Python 入口；为 None 时是 Python 包（entry_module/entry_wheel/wheels 生效）
    entry: PluginEntry | None = None
    files: tuple[PluginFile | CapabilityFile, ...] = ()
    platforms: tuple[str, ...] = ()
    capability: CapabilityDeclaration | None = None
    events: tuple[PluginEventDeclaration, ...] = ()
    tool_gates: tuple[PluginToolGateDeclaration, ...] = ()
    permissions: PluginEventPermissions | None = None

    # LLM: 直接构造与 JSON 同源；只有显式 v8 将非空订阅计贡献，旧协议原规则不变，host_api 仍与订阅互斥。
    # 函数用途: 安装前验证入口与贡献，允许纯订阅的 v8，但不授执行权或放宽旧包贡献门。
    def __post_init__(self) -> None:
        _text(self.version)
        _text(self.summary)
        for items, kind in (
            (self.wheels, PluginWheel),
            (self.actions, CommandActionSpec),
            (self.tools, PluginToolDeclaration),
        ):
            if not isinstance(items, tuple) or any(not isinstance(item, kind) for item in items):
                raise ValueError("包声明必须是不可变集合")
        validate_panels(self.panels)
        if (not isinstance(self.skills, tuple) or len(self.skills) > MAX_PLUGIN_SKILL_COUNT
                or len(set(self.skills)) != len(self.skills)
                or any(not isinstance(name, str) or not _SKILL_NAME.fullmatch(name) for name in self.skills)):
            raise ValueError("随包 Skill 名单无效")
        # 入口校验排在 Skill 名单之后：v6 要拿已校验的 Skill 名单核对随包 SKILL.md
        if self.is_content_only:
            self._validate_content_entry()
        elif self.entry is None:
            self._validate_python_entry()
        else:
            self._validate_file_entry()
        if (not isinstance(self.host_api, tuple) or len(set(self.host_api)) != len(self.host_api)
                or any(item not in PLUGIN_HOST_API_PERMISSIONS for item in self.host_api)):
            raise ValueError("宿主 API 权限名单无效")
        _validate_event_declarations(self)
        tool_names = {tool.name for tool in self.tools}
        panel_ids = {panel.id for panel in self.panels}
        # 版本门已验证，只有显式 v8 能把订阅算贡献；旧包连原拒绝文案也保持不变。
        v8_contributes = self.permissions is not None and bool(self.events or self.tool_gates)
        if len(tool_names) != len(self.tools) or not (tool_names or panel_ids or self.is_content_only or v8_contributes):
            raise ValueError("工具重名，或既没有工具也没有面板")
        _validate_observation_pairs(self.tools)
        for action in self.actions:
            targets = tool_names if action.kind == "tool" else panel_ids if action.kind == "display" else set()
            if action.target not in targets or action.available is not True:
                raise ValueError("动作必须引用本包已声明的工具或面板")
        _ = self.command_spec
        canonical = canonicalize_tool_input_schema(json.loads(self.settings_schema_json))
        object.__setattr__(self, "settings_schema_json", _json(canonical))

    # LLM: 类型由显式 v7 能力声明决定，不从文件名或缺少 MCP 入口猜测；调用方据此排除进程路径。
    # 函数用途: 区分无需环境和服务的纯内容包。
    @property
    def is_content_only(self) -> bool:
        return self.capability is not None

    # LLM: 首版能力包不混入执行入口、公开 Skill 或宿主授权，内部脚本只作为内容；运行继续经过原工具链。
    # 函数用途: 校验无进程能力包及其私有文件清单。
    def _validate_content_entry(self) -> None:
        if (self.entry is not None or self.entry_module or self.entry_wheel or self.wheels or self.platforms
                or self.tools or self.actions or self.default_action or self.panels or self.skills or self.host_api):
            raise ValueError("纯内容能力包不能声明执行入口或公开贡献")
        validate_capability_files(self.capability, self.files)

    # LLM: Python 包（v1–v5）必须有合法模块入口和包含入口 wheel 的 wheel 集合，且不能夹带 v6 字段。
    # 函数用途: 校验 Python 入口与 wheel 声明一致。
    def _validate_python_entry(self) -> None:
        if not isinstance(self.entry_module, str) or not _MODULE.fullmatch(self.entry_module):
            raise ValueError("Python 模块入口无效")
        if any(keyword.iskeyword(part) for part in self.entry_module.split(".")):
            raise ValueError("Python 模块入口含保留字")
        wheel_names = {wheel.path.casefold() for wheel in self.wheels}
        if len(wheel_names) != len(self.wheels) or self.entry_wheel not in {
            wheel.path for wheel in self.wheels
        }:
            raise ValueError("wheel 重名或入口 wheel 缺失")
        if self.files or self.platforms:
            raise ValueError("Python 包不能声明随包文件或平台")

    # LLM: v6 包不带 Python 入口与 wheel；入口、文件清单、平台与 Skill 文件的一致性统一由 plugin_entry 裁决。
    # 函数用途: 校验非 Python 入口声明。
    def _validate_file_entry(self) -> None:
        if not isinstance(self.entry, PluginEntry) or self.entry_module or self.entry_wheel or self.wheels:
            raise ValueError("非 Python 包不能声明 Python 入口或 wheel")
        validate_entry_files(self.entry, self.files, self.platforms, self.skills)

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

    # LLM: 只读 tools 的结构化声明，不看名字猜；供代理结果路径与安装校验共用。
    # 函数用途: 判断本包是否声明了任何观察或观察引用（决定协议版本 v5）。
    @property
    def observes(self) -> bool:
        return any(tool.observation is not None or tool.observation_ref is not None for tool in self.tools)

    # LLM: 工具项投影在各协议版本间共用；观察字段只在声明时出现，保证旧版本包的字节不变。
    # 函数用途: 生成包描述里的 tools 列表。
    def _tool_payloads(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "requested_effect": tool.requested_effect,
                **({"observation": asdict(tool.observation)} if tool.observation is not None else {}),
                **({"observation_ref": asdict(tool.observation_ref)} if tool.observation_ref is not None else {}),
            }
            for tool in self.tools
        ]

    # LLM: v6 的字段和字节保持不变；只有显式 v8 权限对象才加入订阅，沿同一入口/文件协议序列化。
    # 函数用途: 生成非 Python 包及事件插件的静态描述，不混入启用或沙箱运行状态。
    def _v6_payload(self) -> dict:
        return json.loads(_json({
            "schema_version": PLUGIN_PACKAGE_SCHEMA_V8 if self.permissions is not None else PLUGIN_PACKAGE_SCHEMA_V6,
            "plugin_id": self.plugin_id, "version": self.version,
            "summary": self.summary, "entry": self.entry.to_payload(),
            "files": [asdict(item) for item in self.files], "platforms": list(self.platforms),
            "actions": [asdict(action) for action in self.actions], "default_action": self.default_action,
            "tools": self._tool_payloads(), "settings_schema": self.settings_schema,
            "panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
            "host_api": list(self.host_api),
            **({"events": [event.to_payload() for event in self.events],
                "tool_gates": [gate.to_payload() for gate in self.tool_gates],
                "permissions": self.permissions.to_payload()} if self.permissions is not None else {}),
        }))

    # LLM: 输出只包含协议声明，不含私有运行字段；JSON 形态由命令 dataclass 和原 schema 投影产生。
    # 函数用途: 生成可复读的静态包描述。
    def to_payload(self) -> dict:
        if self.is_content_only:
            return {"schema_version": CAPABILITY_PACKAGE_SCHEMA, "package_kind": "capability",
                    "plugin_id": self.plugin_id, "version": self.version, "summary": self.summary,
                    "capability": self.capability.to_payload(), "files": [asdict(item) for item in self.files],
                    "settings_schema": self.settings_schema}
        if self.entry is not None:
            return self._v6_payload()
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
                    "tools": self._tool_payloads(),
                    "settings_schema": self.settings_schema,
                    **({"panels": [panel.to_payload() for panel in self.panels],
                        "schema_version": PLUGIN_PACKAGE_SCHEMA_V2} if self.panels else {}),
                    **({"panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
                        "schema_version": PLUGIN_PACKAGE_SCHEMA_V3} if self.skills else {}),
                    **({"panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
                        "host_api": list(self.host_api), "schema_version": PLUGIN_PACKAGE_SCHEMA_V4}
                       if self.host_api else {}),
                    **({"panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
                        "host_api": list(self.host_api), "schema_version": PLUGIN_PACKAGE_SCHEMA_V5}
                       if self.observes else {}),
                }
            )
        )

    # LLM: 严格版本字段阻断旧包夹带 v8 声明；保留已分类的安全冲突原因，其余错误统一脱敏，不执行插件。
    # 函数用途: 从 JSON 恢复经过合同验证的包，旧 v1–v7 的读取路径保持不变。
    @classmethod
    def from_payload(cls, payload: object) -> PluginManifest:
        try:
            _json(payload)
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if version == CAPABILITY_PACKAGE_SCHEMA:
                return cls._from_content(payload)
            if version in (PLUGIN_PACKAGE_SCHEMA_V6, PLUGIN_PACKAGE_SCHEMA_V8):
                return cls._from_v6(payload)
            if version not in _SCHEMA_FIELDS:
                raise ValueError("包协议版本无效")
            row = _fields(payload, _BASE_FIELDS + _SCHEMA_FIELDS[version])
            panels, skills, host_api = _extension_fields(version, row)
            wheels = tuple(
                PluginWheel(**_fields(item, "path sha256"))
                for item in declaration_list(row["wheels"])
            )
            tools = _tools_from_payload(row["tools"], version)
            if version == PLUGIN_PACKAGE_SCHEMA_V5 and not any(
                tool.observation is not None or tool.observation_ref is not None for tool in tools
            ):
                raise ValueError("包协议版本声明的新能力为空")
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
        except PluginPackageError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise PluginPackageError("invalid_manifest", "插件包描述无效。") from exc

    # LLM: v7 的 package_kind 必须显式为 capability；旧运行字段不能借空值混入新协议，缺省仅存在于内存投影。
    # 函数用途: 恢复纯内容能力声明，不创建环境或 Skill 贡献。
    @classmethod
    def _from_content(cls, payload: dict) -> PluginManifest:
        row = _fields(payload, "schema_version package_kind plugin_id version summary capability files settings_schema")
        if row["package_kind"] != "capability":
            raise ValueError("能力包类型无效")
        return cls(plugin_id=row["plugin_id"], version=row["version"], summary=row["summary"],
                   entry_module="", entry_wheel="", wheels=(), actions=(), default_action="", tools=(),
                   settings_schema_json=_json(row["settings_schema"]),
                   capability=CapabilityDeclaration.from_payload(row["capability"]),
                   files=tuple(CapabilityFile(**_fields(item, "path sha256 executable"))
                               for item in declaration_list(row["files"])))

    # LLM: v6/v8 共用固定文件入口字段；v8 的三项新增声明显式必填，不向旧协议注入默认字段。
    # 函数用途: 恢复非 Python 包，并只在 v8 读取订阅与权限；错误由公共入口脱敏。
    @classmethod
    def _from_v6(cls, payload: dict) -> PluginManifest:
        is_v8 = payload["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V8
        row = _fields(payload, _V8_FIELDS if is_v8 else _V6_FIELDS)
        panels, skills, host_api = _extension_fields(PLUGIN_PACKAGE_SCHEMA_V6, row)
        files = tuple(PluginFile(**_fields(item, "path sha256 executable")) for item in declaration_list(row["files"]))
        return cls(
            plugin_id=row["plugin_id"], version=row["version"], summary=row["summary"],
            entry_module="", entry_wheel="", wheels=(),
            actions=tuple(command_action_from_payload(item) for item in declaration_list(row["actions"])),
            default_action=row["default_action"], tools=_tools_from_payload(row["tools"], row["schema_version"]),
            settings_schema_json=_json(row["settings_schema"]), panels=panels, skills=skills, host_api=host_api,
            entry=PluginEntry.from_payload(row["entry"]), files=files,
            platforms=tuple(declaration_list(row["platforms"])),
            **(subscriptions_from_payload(row) if is_v8 else {}),
        )


_V6_FIELDS = ("schema_version plugin_id version summary entry files platforms actions default_action tools "
              "settings_schema panels skills host_api")
_V8_FIELDS = _V6_FIELDS + " events tool_gates permissions"
_BASE_FIELDS = "schema_version plugin_id version summary entry_module entry_wheel wheels actions default_action tools settings_schema"
# 各协议版本在基础字段之外必须出现的字段；每个新版本声明的新能力不能为空（否则应使用更低版本）
_SCHEMA_FIELDS = {
    PLUGIN_PACKAGE_SCHEMA: "",
    PLUGIN_PACKAGE_SCHEMA_V2: " panels",
    PLUGIN_PACKAGE_SCHEMA_V3: " panels skills",
    PLUGIN_PACKAGE_SCHEMA_V4: " panels skills host_api",
    PLUGIN_PACKAGE_SCHEMA_V5: " panels skills host_api",
}
_TOOL_REQUIRED_FIELDS = frozenset({"name", "description", "input_schema", "requested_effect"})
_TOOL_V5_OPTIONAL_FIELDS = frozenset({"observation", "observation_ref"})


# LLM: v8 沿 v6 支持 v5 观察字段，旧 v1–v4 不放宽；未知字段不能静默丢弃。
# 函数用途: 按显式版本检查一个工具声明项的字段集合。
def _tool_fields(value: object, version: str) -> dict:
    optional = (_TOOL_V5_OPTIONAL_FIELDS if version in {PLUGIN_PACKAGE_SCHEMA_V5, PLUGIN_PACKAGE_SCHEMA_V6, PLUGIN_PACKAGE_SCHEMA_V8}
                else frozenset())
    if (not isinstance(value, dict) or not set(value) >= _TOOL_REQUIRED_FIELDS
            or set(value) - _TOOL_REQUIRED_FIELDS - optional):
        raise ValueError("包描述字段不完整或存在未知字段")
    return value


# LLM: 包层唯一 v8 分类门：权限对象必须显式、必须是文件入口且有订阅；host_api 冲突以设计指定码返回。
# 函数用途: 防止直接构造、轮子包或内容包绕过 v8 订阅约束，不读取 owner 或运行状态。
def _validate_event_declarations(manifest: PluginManifest) -> None:
    validate_subscriptions(manifest.events, manifest.tool_gates)
    if manifest.permissions is None:
        if manifest.events or manifest.tool_gates:
            raise ValueError("事件订阅必须声明 v8 权限")
        return
    if (not isinstance(manifest.permissions, PluginEventPermissions) or manifest.entry is None
            or manifest.is_content_only or not (manifest.events or manifest.tool_gates)):
        raise ValueError("v8 必须使用文件入口并声明订阅")
    if manifest.host_api:
        raise PluginPackageError("events_with_host_api_unsupported", "第一期事件插件不能同时声明宿主 API。")


# LLM: 工具项在各版本共用同一构造；观察字段是否允许由 _tool_fields 按版本裁决。
# 函数用途: 从包描述 JSON 恢复工具声明元组。
def _tools_from_payload(value: object, version: str) -> tuple[PluginToolDeclaration, ...]:
    tools = []
    for item in declaration_list(value):
        tool = _tool_fields(item, version)
        tools.append(PluginToolDeclaration(
            tool["name"], tool["description"], _json(tool["input_schema"]), tool["requested_effect"],
            observation=_observation_from_payload(tool.get("observation")),
            observation_ref=_observation_ref_from_payload(tool.get("observation_ref")),
        ))
    return tuple(tools)


# 函数用途: 从 JSON 对象恢复观察声明；缺 max_candidates 按宿主上限，未知键拒绝。
def _observation_from_payload(value: object) -> PluginToolObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not {"target_kind"} <= set(value) or set(value) - {"target_kind", "max_candidates"}:
        raise ValueError("观察声明字段无效")
    return PluginToolObservation(value["target_kind"], value.get("max_candidates", MAX_CANDIDATE_COUNT))


# 函数用途: 从 JSON 对象恢复观察引用声明；两个字段都必须出现。
def _observation_ref_from_payload(value: object) -> PluginToolObservationRef | None:
    if value is None:
        return None
    return PluginToolObservationRef(**_fields(value, "target_kind param"))


# LLM: 配对是双向的：每个 observation_ref 的目标类型都要有同类观察工具，每个观察目标类型也要至少有一个动作工具引用，
#   否则候选的 actions 永远无法合规。只读结构化声明，不看名字猜。
# 函数用途: 校验同一包内观察工具与动作工具按 target_kind 成对出现。
def _validate_observation_pairs(tools: tuple[PluginToolDeclaration, ...]) -> None:
    observed = {tool.observation.target_kind for tool in tools if tool.observation is not None}
    referenced = {tool.observation_ref.target_kind for tool in tools if tool.observation_ref is not None}
    if referenced - observed:
        raise ValueError("动作工具引用的观察目标类型没有对应的观察工具")
    if observed - referenced:
        raise ValueError("观察目标类型没有任何动作工具引用")


# LLM: 只做版本相关的非空约束与集合读取，具体取值校验在 PluginManifest.__post_init__ 统一进行。
# 函数用途: 读取面板、随包 Skill、宿主 API 权限三项扩展声明，并检查所声明版本对应的新能力不为空。
def _extension_fields(version: str, row: dict) -> tuple[tuple, tuple, tuple]:
    panels = tuple(PanelDeclaration.from_payload(item) for item in declaration_list(row.get("panels", [])))
    skills = tuple(declaration_list(row.get("skills", [])))
    host_api = tuple(declaration_list(row.get("host_api", [])))
    required = {PLUGIN_PACKAGE_SCHEMA_V2: panels, PLUGIN_PACKAGE_SCHEMA_V3: skills, PLUGIN_PACKAGE_SCHEMA_V4: host_api}
    if version in required and not required[version]:
        raise ValueError("包协议版本声明的新能力为空")
    return panels, skills, host_api


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
