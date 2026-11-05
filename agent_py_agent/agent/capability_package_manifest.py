# LLM: v7 私有内容与 v10 显式 MCP 声明共用包身份；此模块只校验静态数据，不授予执行权或创建环境，旧包字节保持不变。
# 模块用途: 校验能力包文档及独立运行树，中文资源名保留原样，运行路径复用原文件入口规则。

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .capability_verification_manifest import VerificationDeclaration, validate_verification_members
from .plugin_entry import PluginEntry, PluginFile, validate_entry_files

CAPABILITY_PACKAGE_SCHEMA = "plugin_package.v7"
CAPABILITY_PACKAGE_SCHEMA_V10 = "plugin_package.v10"
# v10 协议固定为一个服务，不是运行时可调预算；按 W1 常数合同保留整数导出且不改共享目录。
MAX_PACK_MCP_SERVERS = int("1")
PACK_TOOL_CATALOG_MODES = ("declared", "pinned_on_enable")
PACK_MCP_CONFIRMATION_KIND = "capability_pack_mcp"
_MCP_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
# 能力包清单最多收录的文件数；超出即判定清单过大（接近常见文件描述符上限 4096）。
MAX_CAPABILITY_FILES = 4095
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# 能力声明（清单里的 capability）必须有的字段与可选字段：from_payload 和打包层的缺/多字段提示共用这一处。
CAPABILITY_REQUIRED_FIELDS = ("description", "keywords", "entry_document")
CAPABILITY_OPTIONAL_FIELDS = ("verification", "application_scope")


# LLM: 范围仅软排序；拷贝序列、空维度省略以保旧规范字节，未知键/坏类型/过大元数据拒绝。
# 函数用途: 校验并复制能力包工作台、领域和项目标签声明。
def application_scope_payload(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or value.keys() - {'workbenches', 'domains', 'workspace_tags'}:
        raise ValueError('能力应用范围无效')
    result = {}
    for key, values in value.items():
        if not isinstance(values, (list, tuple)) or len(values) > 64:
            raise ValueError('能力应用范围必须为有界字符串列表')
        for item in values:
            _metadata_text(item, 256)
        if values:
            result[key] = list(dict.fromkeys(values))
    return result


# LLM: 路径只定位归档成员，不是宿主地址；保持原名字，碰撞比较另做规范化，不能清洗后改指另一文件。
# 函数用途: 拒绝越界、控制字符、设备分隔符和过深路径，允许中文与空格文件名。
def validate_capability_path(value: object) -> None:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024
            or len(value.split("/")) > 32 or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or unicodedata.normalize("NFC", value.casefold()) == "plugin.json"):
        raise ValueError("能力资源路径无效")


# LLM: 文件是受摘要保护的私有内容；脚本可以保存为资源，但本协议没有执行入口，也不声明执行位。
# 类用途: 保存能力包内一个文件的身份和完整性摘要。
@dataclass(frozen=True)
class CapabilityFile:
    path: str
    sha256: str
    executable: bool = False

    # LLM: 不从扩展名推断能否运行，任何执行声明都需另走现有工具授权。
    # 函数用途: 验证路径、摘要和纯内容标记。
    def __post_init__(self) -> None:
        validate_capability_path(self.path)
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("能力资源摘要无效")
        if self.executable is not False:
            raise ValueError("纯内容包不能声明可执行文件")


@dataclass(frozen=True)
class _PackRuntimeFile(PluginFile):
    """保存含服务根前缀的归档声明；相对路径必须由同次清单校验调用原 v6 规则核验。"""

    def __post_init__(self) -> None:
        # 归档全名可能是四层根加八层运行树，不能误套 v6 全名八层上限。
        validate_capability_path(self.path)
        PluginFile("runtime", self.sha256, self.executable)

    # LLM: 包完整性复验按公开声明的三个值比较；内部路径容器不是另一种文件身份，自动 hash 仍覆盖同三字段。
    # 函数用途: 让普通 PluginFile 直接构造与 v10 解码得到的相同文件保持对称相等，不改旧类的相等规则。
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PluginFile):
            return NotImplemented
        return (self.path, self.sha256, self.executable) == (other.path, other.sha256, other.executable)


@dataclass(frozen=True)
class PackMCPServer:
    """v10 唯一运行服务的静态声明；不读取宿主、启动进程或授予工具权限。"""

    name: str
    root: str
    entry: PluginEntry
    platforms: tuple[str, ...]
    network: bool
    tool_catalog: str
    # 工具类型在 PluginManifest 中核对，避免这里与其声明类形成模块导入环。
    tools: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _MCP_SERVER_NAME.fullmatch(self.name):
            raise ValueError("能力包 MCP 服务名称无效")
        if not isinstance(self.root, str) or not self.root or len(self.root.split("/")) > 4:
            raise ValueError("能力包 MCP 服务根目录无效")
        # 借原 PluginFile 验证根的 ASCII 分量；附加普通文件名以免把目录当描述文件。
        PluginFile(f"{self.root}/_root", "0" * 64)
        if not isinstance(self.entry, PluginEntry) or type(self.network) is not bool:
            raise ValueError("能力包 MCP 入口或网络声明无效")
        if (self.tool_catalog not in PACK_TOOL_CATALOG_MODES or not isinstance(self.tools, tuple)
                or (self.tool_catalog == "declared") != bool(self.tools)):
            raise ValueError("能力包 MCP 工具目录模式无效")
        # 这里只核入口形状与平台，完整成员集合仍由 validate_pack_runtime_files 原样核对。
        validate_entry_files(self.entry,
                             (PluginFile(self.entry.command, "0" * 64, self.entry.kind == "executable"),),
                             self.platforms, ())

    def to_payload(self) -> dict:
        """只输出协议字段；工具 schema 每次取得副本，不混入激活事实。"""
        return {"name": self.name, "root": self.root, "entry": self.entry.to_payload(),
                "platforms": list(self.platforms), "permissions": {"network": self.network},
                "tool_catalog": self.tool_catalog,
                "tools": [{"name": tool.name, "description": tool.description,
                           "input_schema": tool.input_schema, "requested_effect": tool.requested_effect}
                          for tool in self.tools]}

    @classmethod
    def from_payload(cls, value: object) -> PackMCPServer:
        """严格恢复一个服务；network 必须显式出现，v10 工具不接受观察扩展。"""
        from .plugin_manifest import _tools_from_payload

        fields = {"name", "root", "entry", "platforms", "permissions", "tool_catalog", "tools"}
        if (not isinstance(value, dict) or set(value) != fields
                or not isinstance(value["permissions"], dict) or set(value["permissions"]) != {"network"}
                or not isinstance(value["platforms"], list)):
            raise ValueError("能力包 MCP 服务字段无效")
        return cls(value["name"], value["root"], PluginEntry.from_payload(value["entry"]),
                   tuple(value["platforms"]), value["permissions"]["network"], value["tool_catalog"],
                   _tools_from_payload(value["tools"], CAPABILITY_PACKAGE_SCHEMA_V10))


def validate_pack_runtime_files(server: PackMCPServer,
                                files: tuple[CapabilityFile | PluginFile, ...]) -> tuple[PluginFile, ...]:
    """按服务根分区并去根，完整复用 v6 成员、入口、平台及未声明 Skill 的校验。"""
    if (not isinstance(server, PackMCPServer) or not isinstance(files, tuple)
            or any(not isinstance(item, (CapabilityFile, PluginFile)) for item in files)):
        raise ValueError("能力包 MCP 文件清单无效")
    keys = {unicodedata.normalize("NFC", item.path.casefold()) for item in files}
    if len(keys) != len(files):
        raise ValueError("能力包资源重名")
    prefix = server.root + "/"
    runtime = tuple(PluginFile(item.path[len(prefix):], item.sha256, item.executable)
                    for item in files if item.path.startswith(prefix))
    validate_entry_files(server.entry, runtime, server.platforms, ())
    return runtime


# LLM: 描述与关键词仅供模型软选择，不能用于权限或状态判断；入口文档必须属于同一包的已声明资源。
#   verification 是能力包 v2 的可选核验声明（交付物、检查程序、输入策略）；为 None 时序列化与旧包逐字节一致。
# 类用途: 保存一个能力包对外公开的少量元数据。
@dataclass(frozen=True)
class CapabilityDeclaration:
    description: str
    keywords: tuple[str, ...]
    entry_document: str
    verification: VerificationDeclaration | None = None
    application_scope: dict | None = None

    # LLM: 元数据有界且范围复制；应用范围只是软排序，不改变包权限或旧包规范字节。
    # 函数用途: 拒绝空描述、不合法关键词及不安全的文档地址。
    def __post_init__(self) -> None:
        _metadata_text(self.description, 4096)
        if (not isinstance(self.keywords, tuple) or len(self.keywords) > 32
                or any(not isinstance(word, str) for word in self.keywords)
                or len(set(self.keywords)) != len(self.keywords)):
            raise ValueError("能力关键词无效")
        for word in self.keywords:
            _metadata_text(word, 128)
        validate_capability_path(self.entry_document)
        if self.verification is not None and not isinstance(self.verification, VerificationDeclaration):
            raise ValueError("能力核验声明无效")
        if self.application_scope is not None:
            object.__setattr__(self, 'application_scope', application_scope_payload(self.application_scope))

    # LLM: 不混入用户/激活/路径；空范围和无核验均不输出新增键，旧包摘要不变，嵌套序列每次复制。
    # 函数用途: 生成固定字段的能力元数据。
    def to_payload(self) -> dict:
        payload = {"description": self.description, "keywords": list(self.keywords), "entry_document": self.entry_document}
        if self.verification is not None:
            payload["verification"] = self.verification.to_payload()
        if self.application_scope:
            payload['application_scope'] = application_scope_payload(self.application_scope)
        return payload

    # LLM: 缺字段或未知键直接拒绝，不能把未来协议静默当作当前协议读取。
    # 函数用途: 从静态描述恢复能力声明。
    @classmethod
    def from_payload(cls, value: object) -> CapabilityDeclaration:
        base = set(CAPABILITY_REQUIRED_FIELDS)
        if (not isinstance(value, dict) or not base <= set(value) or not set(value) <= base | set(CAPABILITY_OPTIONAL_FIELDS)
                or not isinstance(value["keywords"], list)):
            raise ValueError("能力声明字段无效")
        verification = (VerificationDeclaration.from_payload(value["verification"])
                        if "verification" in value else None)
        return cls(value["description"], tuple(value["keywords"]), value["entry_document"], verification,
                   value.get('application_scope'))


# LLM: 列表只定义包内资源，SKILL.md 同样是私有文件；去重与 ZIP 使用同一 NFC/大小写折叠口径。
#   有核验声明时，检查程序引用的成员也必须是这里声明过的文件。
# 函数用途: 验证非空文件集合、数量预算、入口文档归属及检查程序成员归属。
def validate_capability_files(capability: CapabilityDeclaration, files: tuple[CapabilityFile, ...]) -> None:
    if (not isinstance(capability, CapabilityDeclaration) or not isinstance(files, tuple)
            or not 1 <= len(files) <= MAX_CAPABILITY_FILES
            or any(not isinstance(item, CapabilityFile) for item in files)):
        raise ValueError("能力资源清单无效")
    keys = {unicodedata.normalize("NFC", item.path.casefold()) for item in files}
    if len(keys) != len(files) or capability.entry_document not in {item.path for item in files}:
        raise ValueError("能力资源重名或入口文档缺失")
    if capability.verification is not None:
        validate_verification_members(capability.verification, {item.path for item in files})


# LLM: 展示文字有界且不允许控制字符，不依据文字内容做路由或授权。
# 函数用途: 验证用于检索的简短中文或其他语言说明。
def _metadata_text(value: object, limit: int) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("能力说明无效")
