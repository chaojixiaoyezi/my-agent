# LLM: v7 能力内容只声明私有资源和包级发现信息，不授予执行权、不创建 Skill 或环境；修改须联测旧包往返和资源读取。
# 模块用途: 校验无需启动进程的能力包，中文资源名保留原样，包内方法只在选中后读取。

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .capability_verification_manifest import VerificationDeclaration, validate_verification_members

CAPABILITY_PACKAGE_SCHEMA = "plugin_package.v7"
# 能力包清单最多收录的文件数；超出即判定清单过大（接近常见文件描述符上限 4096）。
MAX_CAPABILITY_FILES = 4095
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


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


# LLM: 描述与关键词仅供模型软选择，不能用于权限或状态判断；入口文档必须属于同一包的已声明资源。
#   verification 是能力包 v2 的可选核验声明（交付物、检查程序、输入策略）；为 None 时序列化与旧包逐字节一致。
# 类用途: 保存一个能力包对外公开的少量元数据。
@dataclass(frozen=True)
class CapabilityDeclaration:
    description: str
    keywords: tuple[str, ...]
    entry_document: str
    verification: VerificationDeclaration | None = None

    # LLM: 元数据有界，正文与包内方法不能塞进公开摘要；集合冻结供逐轮快照使用。
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

    # LLM: 序列化只包含公开内容声明，不混入用户、激活或本地资源地址；没有核验声明时不输出该键，旧包字节不变。
    # 函数用途: 生成固定字段的能力元数据。
    def to_payload(self) -> dict:
        payload = {"description": self.description, "keywords": list(self.keywords), "entry_document": self.entry_document}
        if self.verification is not None:
            payload["verification"] = self.verification.to_payload()
        return payload

    # LLM: 缺字段或未知键直接拒绝，不能把未来协议静默当作当前协议读取。
    # 函数用途: 从静态描述恢复能力声明。
    @classmethod
    def from_payload(cls, value: object) -> CapabilityDeclaration:
        base = {"description", "keywords", "entry_document"}
        if (not isinstance(value, dict) or not base <= set(value) or not set(value) <= base | {"verification"}
                or not isinstance(value["keywords"], list)):
            raise ValueError("能力声明字段无效")
        verification = (VerificationDeclaration.from_payload(value["verification"])
                        if "verification" in value else None)
        return cls(value["description"], tuple(value["keywords"]), value["entry_document"], verification)


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
