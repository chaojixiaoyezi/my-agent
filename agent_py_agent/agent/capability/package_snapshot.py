# LLM: 能力包快照保存原安装代次的公开摘要和私有声明；读取建议携带同代身份，不转换全局 Skill 或授予执行权。
# 模块用途: 为发现、授权和按需读取保留不可变包身份，共用准确读取参数，不另建安装或任务状态。
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..capability_package_manifest import CapabilityFile, application_scope_payload


# LLM: 只按已声明成员的明确目录/SKILL.md归类展示，同名不同路径保留相对路径消歧，不读正文或授执行权。
# 函数用途: 汇总包内技能、脚本和工作流资源的稳定名称与准确数量。
def package_assets(members):
    raw = {'skills': [], 'scripts': [], 'workflows': []}
    for member in members:
        path = PurePosixPath(member.path)
        canon = path.as_posix()
        if path.name == 'SKILL.md':
            short = path.parent.name or path.stem
            parent = path.parent.as_posix()
            full = canon if parent == '.' else parent
            raw['skills'].append((canon, short, full))
        for kind in ('scripts', 'workflows'):
            if kind in path.parts[:-1]:
                idx = path.parts.index(kind)
                following = path.parts[idx + 1:]
                short = following[0] if len(following) > 1 else path.stem
                key_path = f"{PurePosixPath(*path.parts[:idx + 2]).as_posix()}/" if len(following) > 1 else canon
                full = PurePosixPath(*path.parts[:idx], short).as_posix() if idx > 0 else key_path
                raw[kind].append((key_path, short, full))
    result = {}
    for key, rows in raw.items():
        unique = list({canon: (canon, short, full) for canon, short, full in rows}.values())
        tiers = [(short, full, canon) for canon, short, full in unique]
        levels = [0] * len(tiers)
        while True:
            chosen = [tiers[idx][levels[idx]] for idx in range(len(tiers))]
            counts = {name: chosen.count(name) for name in set(chosen)}
            nxt = [min(2, lvl + (1 if counts[chosen[idx]] > 1 else 0)) for idx, lvl in enumerate(levels)]
            if nxt == levels:
                break
            levels = nxt
        result[key] = sorted(tiers[idx][levels[idx]] for idx in range(len(tiers)))
    return result


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
    installation_scope: str = "private"
    application_scope: dict = field(default_factory=dict)

    # LLM: 安装深校验归Manifest；复制范围并校验唯一资源/入口，范围和资源名仅展示，不授执行权。
    # 函数用途: 确认包快照能按唯一成员路径读取，且具有宿主固定的内容与激活身份。
    def __post_init__(self) -> None:
        if not self.package_id or not self.package_sha256 or not self.activation_id:
            raise ValueError("CAPABILITY_PACKAGE_IDENTITY_REQUIRED")
        if self.installation_scope not in {"private", "public"} or (
                self.installation_scope == "public" and not self.package_id.startswith("public/")):
            raise ValueError("CAPABILITY_PACKAGE_SCOPE_INVALID")
        if not isinstance(self.members, tuple) or not all(isinstance(item, CapabilityFile) for item in self.members):
            raise ValueError("CAPABILITY_PACKAGE_MEMBERS_INVALID")
        paths = {item.path for item in self.members}
        if len(paths) != len(self.members) or self.entry_document not in paths:
            raise ValueError("CAPABILITY_PACKAGE_ENTRY_INVALID")
        object.__setattr__(self, 'application_scope', application_scope_payload(self.application_scope))

    # LLM: 卡片不读私有正文或暴露本机路径；资源清单只供展示，引用身份仍沿to_ref和原激活核验。
    # 函数用途: 输出范围和包内资源索引的发现卡。
    def to_card(self):
        assets = package_assets(self.members)
        return {'package_id': self.package_id, 'name': self.name, 'version': self.version, 'summary': self.summary,
                'installation_scope': self.installation_scope, 'application_scope': application_scope_payload(self.application_scope),
                'assets': assets, 'asset_counts': {key: len(value) for key, value in assets.items()},
                'digest': self.package_sha256, 'enabled': True}

    # LLM: 详情仅加公开声明，不预读入口正文或把资源转全局Skill。
    # 函数用途: 生成能力包公开详情。
    def to_detail(self):
        return {**self.to_card(), 'description': self.description, 'keywords': list(self.keywords), 'entry_document': self.entry_document}

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
