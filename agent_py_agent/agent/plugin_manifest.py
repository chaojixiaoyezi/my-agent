# LLM: 包描述是未授权的静态声明；不接受宿主身份、配置值或启用事实，命令和工具 schema 必须沿现有合同。
# 模块用途: 校验本地插件的内容声明（Python 包或 v6 非 Python 包），提供无需导入插件的帮助信息及不可变包元数据。

from __future__ import annotations

import json
import keyword
import re
from dataclasses import asdict, dataclass

from .command_arguments import CommandActionSpec
from .command_declarations import command_action_from_payload, declaration_list
from .plugin_commands import PluginCommandSpec
from .plugin_display.protocol import PanelDeclaration, validate_panels
from .plugin_entry import PluginEntry, PluginFile, validate_entry_files
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
PLUGIN_HOST_API_PERMISSIONS = ("read",)
# 观察候选数量的宿主硬上限；manifest 声明的 max_candidates 不能超过它
MAX_OBSERVATION_CANDIDATES = 64
_TARGET_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_OBSERVATION_PARAM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
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


# LLM: 观察声明只允许出现在 read_only 工具上（观察本身不能有副作用）；target_kind 是开放字符串，只校验形状，用于把观察和
#   动作配对；max_candidates 由宿主再夹一次上限。声明不授予任何权限，也不改变结果的信任级别。
# 类用途: 声明某只读工具的成功结果会带 my_agent_observation 候选载荷。
@dataclass(frozen=True)
class PluginToolObservation:
    target_kind: str
    max_candidates: int = MAX_OBSERVATION_CANDIDATES

    # 函数用途: 拒绝形状不合规的目标类型与越界的候选上限。
    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, str) or not _TARGET_KIND.fullmatch(self.target_kind):
            raise ValueError("观察目标类型无效")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= MAX_OBSERVATION_CANDIDATES:
            raise ValueError("观察候选上限无效")


# LLM: param 指向动作工具输入 schema 里一个可选的 string 参数，名字由插件自定，宿主只读这条映射；不能与 _meta 或宿主注入的
#   "__" 参数同名（形状正则已排除）。同一 manifest 里必须有同 target_kind 的观察工具与之配对。
# 类用途: 声明某动作工具可以接受同类观察的候选 ID。
@dataclass(frozen=True)
class PluginToolObservationRef:
    target_kind: str
    param: str

    # 函数用途: 拒绝形状不合规的目标类型与参数名。
    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, str) or not _TARGET_KIND.fullmatch(self.target_kind):
            raise ValueError("观察目标类型无效")
        if not isinstance(self.param, str) or not _OBSERVATION_PARAM.fullmatch(self.param):
            raise ValueError("候选参数名无效")


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


# LLM: 观察只能挂只读工具；观察引用要求 param 在 schema.properties 里、type 为 string、不在 required 里；两者互斥。
# 函数用途: 校验一个工具声明里的观察/观察引用与其输入 schema 是否一致。
def _validate_observation_declaration(tool: PluginToolDeclaration, canonical: object) -> None:
    if tool.observation is not None and tool.observation_ref is not None:
        raise ValueError("观察工具不能同时是动作工具")
    if tool.observation is not None and (not isinstance(tool.observation, PluginToolObservation)
                                         or tool.requested_effect != "read_only"):
        raise ValueError("只有只读工具可以声明观察")
    if tool.observation_ref is None:
        return
    if not isinstance(tool.observation_ref, PluginToolObservationRef):
        raise ValueError("观察引用无效")
    properties = canonical.get("properties") if isinstance(canonical, dict) else None
    required = canonical.get("required") if isinstance(canonical, dict) else None
    spec = properties.get(tool.observation_ref.param) if isinstance(properties, dict) else None
    if (not isinstance(spec, dict) or spec.get("type") != "string"
            or (isinstance(required, list) and tool.observation_ref.param in required)):
        raise ValueError("候选参数必须是输入 schema 里可选的 string 参数")


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
    # v6 非 Python 入口；为 None 时是 Python 包（entry_module/entry_wheel/wheels 生效）
    entry: PluginEntry | None = None
    files: tuple[PluginFile, ...] = ()
    platforms: tuple[str, ...] = ()

    # LLM: 直接构造与 JSON 读取共用约束；只开放工具动作和指向本包面板的展示动作，不接受包指定管理操作。
    #   有面板的纯展示包可以没有工具；两者都没有则拒绝。
    # 函数用途: 在包进入候选安装前拒绝重复身份、缺失入口及动作指向未声明的工具或面板。
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
        if (not isinstance(self.skills, tuple) or len(self.skills) > MAX_PLUGIN_SKILLS
                or len(set(self.skills)) != len(self.skills)
                or any(not isinstance(name, str) or not _SKILL_NAME.fullmatch(name) for name in self.skills)):
            raise ValueError("随包 Skill 名单无效")
        # 入口校验排在 Skill 名单之后：v6 要拿已校验的 Skill 名单核对随包 SKILL.md
        if self.entry is None:
            self._validate_python_entry()
        else:
            self._validate_file_entry()
        if (not isinstance(self.host_api, tuple) or len(set(self.host_api)) != len(self.host_api)
                or any(item not in PLUGIN_HOST_API_PERMISSIONS for item in self.host_api)):
            raise ValueError("宿主 API 权限名单无效")
        tool_names = {tool.name for tool in self.tools}
        panel_ids = {panel.id for panel in self.panels}
        if len(tool_names) != len(self.tools) or not (tool_names or panel_ids):
            raise ValueError("工具重名，或既没有工具也没有面板")
        _validate_observation_pairs(self.tools)
        for action in self.actions:
            targets = tool_names if action.kind == "tool" else panel_ids if action.kind == "display" else set()
            if action.target not in targets or action.available is not True:
                raise ValueError("动作必须引用本包已声明的工具或面板")
        _ = self.command_spec
        canonical = canonicalize_tool_input_schema(json.loads(self.settings_schema_json))
        object.__setattr__(self, "settings_schema_json", _json(canonical))

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

    # LLM: v6 字段全部显式输出（面板、Skill、宿主 API 可为空列表），不与 v1–v5 的逐级升版规则混用。
    # 函数用途: 生成非 Python 包的静态包描述。
    def _v6_payload(self) -> dict:
        return json.loads(_json({
            "schema_version": PLUGIN_PACKAGE_SCHEMA_V6, "plugin_id": self.plugin_id, "version": self.version,
            "summary": self.summary, "entry": self.entry.to_payload(),
            "files": [asdict(item) for item in self.files], "platforms": list(self.platforms),
            "actions": [asdict(action) for action in self.actions], "default_action": self.default_action,
            "tools": self._tool_payloads(), "settings_schema": self.settings_schema,
            "panels": [panel.to_payload() for panel in self.panels], "skills": list(self.skills),
            "host_api": list(self.host_api),
        }))

    # LLM: 输出只包含协议声明，不含私有运行字段；JSON 形态由命令 dataclass 和原 schema 投影产生。
    # 函数用途: 生成可复读的静态包描述。
    def to_payload(self) -> dict:
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

    # LLM: 严格字段和有限 UTF-8 JSON 阻断包伪造宿主状态或夹带不可传输文本；不创建目录、不导入实现。
    # 函数用途: 从 JSON 对象恢复经过公共合同验证的包描述。
    @classmethod
    def from_payload(cls, payload: object) -> PluginManifest:
        try:
            _json(payload)
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if version == PLUGIN_PACKAGE_SCHEMA_V6:
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
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise PluginPackageError("invalid_manifest", "插件包描述无效。") from exc

    # LLM: v6 字段集合固定（面板、Skill、宿主 API 可为空列表）；Python 入口字段在 v6 里不存在，构造时置空。
    # 函数用途: 从 v6 JSON 恢复非 Python 包描述，错误由 from_payload 统一转为包描述无效。
    @classmethod
    def _from_v6(cls, payload: dict) -> PluginManifest:
        row = _fields(payload, _V6_FIELDS)
        panels, skills, host_api = _extension_fields(PLUGIN_PACKAGE_SCHEMA_V6, row)
        files = tuple(PluginFile(**_fields(item, "path sha256 executable")) for item in declaration_list(row["files"]))
        return cls(
            plugin_id=row["plugin_id"], version=row["version"], summary=row["summary"],
            entry_module="", entry_wheel="", wheels=(),
            actions=tuple(command_action_from_payload(item) for item in declaration_list(row["actions"])),
            default_action=row["default_action"], tools=_tools_from_payload(row["tools"], PLUGIN_PACKAGE_SCHEMA_V6),
            settings_schema_json=_json(row["settings_schema"]), panels=panels, skills=skills, host_api=host_api,
            entry=PluginEntry.from_payload(row["entry"]), files=files,
            platforms=tuple(declaration_list(row["platforms"])),
        )


_V6_FIELDS = ("schema_version plugin_id version summary entry files platforms actions default_action tools "
              "settings_schema panels skills host_api")
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


# LLM: 工具项字段严格：v1–v4 只允许四个基础字段；v5 另允许两个可选观察字段。未知键不能静默丢弃。
# 函数用途: 按协议版本检查一个工具声明项的字段集合。
def _tool_fields(value: object, version: str) -> dict:
    optional = (_TOOL_V5_OPTIONAL_FIELDS if version in {PLUGIN_PACKAGE_SCHEMA_V5, PLUGIN_PACKAGE_SCHEMA_V6}
                else frozenset())
    if (not isinstance(value, dict) or not set(value) >= _TOOL_REQUIRED_FIELDS
            or set(value) - _TOOL_REQUIRED_FIELDS - optional):
        raise ValueError("包描述字段不完整或存在未知字段")
    return value


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
    return PluginToolObservation(value["target_kind"], value.get("max_candidates", MAX_OBSERVATION_CANDIDATES))


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
