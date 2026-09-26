# LLM: 能力包快照保存原安装代次的公开摘要和私有声明；读取建议携带同代身份，不转换全局 Skill 或授予执行权。
# 模块用途: 为发现、授权和按需读取保留不可变包身份，共用准确读取参数，不另建安装或任务状态。
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..capability_package_manifest import CapabilityFile


# LLM: 读取建议只来自已校验的当前包引用，携带原内容与激活代次；调用方仍须经过原工具授权、晋升和鲜活复核。
# 函数用途: 让发现卡、错误恢复和资源列表共用准确读取参数，防止旧建议跨轮读到同名新版本。
def package_read_parameters(reference: Mapping[str, str], resource_path: str = "") -> dict[str, object]:
    params: dict[str, object] = {
        "action": "get", "package_id": reference["package_id"],
        "expected_package_sha256": reference["content_sha256"],
        "expected_activation_id": reference["activation_id"],
    }
    if resource_path:
        params["resource_path"] = resource_path
    return params


# LLM: reader 由宿主绑定原 owner 与完整安装记录，并须在读取前后验证代次；不得由包或模型提供回调。
# 类用途: 一份已启用能力包的冻结摘要和私有文件清单，正文只在明确选择成员时读取。
@dataclass(frozen=True)
class CapabilityPackageSnapshot:
    package_id: str
    version: str
    summary: str
    description: str
    keywords: tuple[str, ...]
    entry_document: str
    package_sha256: str
    activation_id: str
    members: tuple[CapabilityFile, ...]
    reader: Callable[[str], bytes] = field(repr=False, compare=False)

    # LLM: 安装声明的深层校验归 PluginManifest；这里只拒绝可变成员、重复键和缺入口，防止快照出现歧义。
    # 函数用途: 确认包快照能按唯一成员路径读取，且具有宿主固定的内容与激活身份。
    def __post_init__(self) -> None:
        if not self.package_id or not self.package_sha256 or not self.activation_id:
            raise ValueError("CAPABILITY_PACKAGE_IDENTITY_REQUIRED")
        if not isinstance(self.members, tuple) or not all(isinstance(item, CapabilityFile) for item in self.members):
            raise ValueError("CAPABILITY_PACKAGE_MEMBERS_INVALID")
        paths = {item.path for item in self.members}
        if len(paths) != len(self.members) or self.entry_document not in paths:
            raise ValueError("CAPABILITY_PACKAGE_ENTRY_INVALID")

    # LLM: 稳定引用标识包的公开能力，版本与撤销身份必须另查 to_ref，不能只靠这个名称续用新安装。
    # 函数用途: 返回可保存在现有 allowed_skills 中的包级引用。
    @property
    def stable_id(self) -> str:
        return f"capability:{self.package_id}"

    # LLM: 与原引用合同共用 name 字段，名称只用于展示，不由模型文本决定授权。
    # 函数用途: 为公开包卡返回稳定的包名称。
    @property
    def name(self) -> str:
        return self.package_id

    # LLM: 包不是个人或公共 Skill 来源；保留独立类型，禁止据此提升 SkillGuard 信任等级。
    # 函数用途: 标明公开引用属于独立能力包。
    @property
    def source(self) -> str:
        return "capability_package"

    # LLM: 现有引用哈希槽对包固定整个归档摘要，覆盖入口、方法、脚本和所有声明文件。
    # 函数用途: 让原任务引用校验同时适用于公开 Skill 和完整能力包。
    @property
    def content_sha256(self) -> str:
        return self.package_sha256

    # LLM: v7 内容包不声明或授予工具；需要工具时继续沿既有工具权限和能力申请。
    # 函数用途: 保持原展示消费者接口，但不由私有资源隐式扩展工具列表。
    @property
    def tools_required(self) -> tuple[str, ...]:
        return ()

    # LLM: 此记录进入原 skill_snapshot_refs，只有包身份和内容代次，没有私有文件清单或宿主路径。
    # 函数用途: 为子代理、调度与续跑保存可精确复核的包引用。
    def to_ref(self) -> dict[str, str]:
        return {
            "kind": "capability_package", "stable_id": self.stable_id, "name": self.name,
            "source": self.source, "content_sha256": self.package_sha256,
            "package_id": self.package_id, "activation_id": self.activation_id,
        }

    # LLM: 只接受声明的精确成员路径；相似名、全局 Skill 名或 .. 不产生候选。
    # 函数用途: 在一个明确包中找到文件声明，空路径指向包入口文档。
    def resolve(self, member_path: str = "") -> CapabilityFile | None:
        path = member_path or self.entry_document
        return next((item for item in self.members if item.path == path), None)

    # LLM: reader 负责鲜活安装复核，本层再核对返回字节的声明摘要；不执行、不物化文件，也不猜文本编码。
    # 函数用途: 按需读取一个私有成员并拒绝篡改、未声明文件或错误返回类型。
    def read(self, member_path: str = "") -> bytes:
        member = self.resolve(member_path)
        if member is None:
            raise ValueError("CAPABILITY_RESOURCE_NOT_AVAILABLE")
        content = self.reader(member.path)
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != member.sha256:
            raise ValueError("CAPABILITY_RESOURCE_DIGEST_MISMATCH")
        return content
