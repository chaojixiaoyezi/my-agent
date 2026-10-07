# LLM: learnpack "同领域"与"她做的"两件事的唯一判断处：同领域 = 她自己做的（装着的字节在 learnpack 存储里有打包记录）、装着的、
#   包名不同的能力包，声明的关键词与给定关键词按 casefold 整词重合；她做的 = 装着的字节在 learnpack 存储里有打包记录（与
#   /plugins# 列表"她做的"同一口径）。打包提醒（learnpack_build_notes）、安装回执（tooling/package_install_tool）、确认与自动装回执
#   （learnpack_service）、/plugins# 列表（pack_commands）和 skill_search 的 made_by_me 都调这里，不另抄判断。只读结构化字段
#   （声明关键词、包名、版本、字节摘要），不解析正文；别处装的包不能并入，所以不算同领域。改规则要同步 test_learnpack_same_domain
#   与 test_learnpack_merge。
# 模块用途: 回答"她已经装着哪些同领域的能力包"和"哪些装着的包是她做的"，并给出给用户看的一句提醒。
from __future__ import annotations

from dataclasses import dataclass

from ..plugin_install_store import PluginInstallStore
from ..plugin_manifest import PluginPackageError
from ..plugin_package import inspect_plugin_package
from .learnpack_store import BuildRecord, LearnpackStore
from .package_build import KIND_CAPABILITY_PACK

# 同领域提醒里最多列出的已装包个数。
SAME_DOMAIN_LIST_LIMIT_COUNT = 3
# 同领域提醒里每个包最多列出的共同关键词个数。
SHARED_KEYWORD_LIMIT_COUNT = 5


# LLM: 只读快照；shared_keywords 已按 casefold 排序去重。to_payload 是回执里的结构化字段。
# 类用途: 一个和新包同领域的、她做的已装能力包。
@dataclass(frozen=True)
class SameDomainPack:
    package_id: str
    version: str
    shared_keywords: tuple[str, ...]

    # LLM: 回执字段，键名稳定（test_learnpack_same_domain 锁定）。纯函数。
    # 函数用途: 转成回执里的字典。
    def to_payload(self) -> dict[str, object]:
        return {"package_id": self.package_id, "version": self.version, "shared_keywords": list(self.shared_keywords)}


# LLM: 从她打的包里读能力声明（同一个读包校验器）；读不了或不是能力包返回 None。只读。
# 函数用途: 取某个打包产物的能力声明（说明、关键词）。
def build_capability(store: LearnpackStore, sha256: str) -> object | None:
    try:
        return inspect_plugin_package(store.build_path(sha256).read_bytes()).manifest.capability
    except (OSError, ValueError, PluginPackageError):
        return None


# LLM: 装着的条目里，字节摘要在 learnpack 存储里有打包记录的包名。只读。
# 函数用途: 列出装着的包里哪些是她做的。
def self_made_package_ids(store: LearnpackStore, entries: tuple) -> set[str]:
    return {row.manifest.plugin_id for row in entries if store.build(row.package_sha256) is not None}


# LLM: keywords 是新包声明的关键词（任意大小写）；只比她做的、装着的、包名不同的能力包；装着的包的关键词取安装表里的清单
#   （和读包校验器同一份）。按包名排序，结果稳定。只读。
# 函数用途: 找出和新包同领域的、她做的已装能力包。
def same_domain_packs(store: LearnpackStore, entries: tuple, package_id: str,
                      keywords: object) -> list[SameDomainPack]:
    words = {str(word).casefold() for word in keywords or ()}
    found = []
    for row in sorted(entries, key=lambda item: item.manifest.plugin_id):
        capability = getattr(row.manifest, "capability", None)
        if not words or capability is None or row.manifest.plugin_id == package_id:
            continue
        shared = tuple(sorted(words & {word.casefold() for word in capability.keywords}))
        if shared and store.build(row.package_sha256) is not None:
            found.append(SameDomainPack(row.manifest.plugin_id, row.manifest.version, shared))
    return found


# LLM: 只拼文字："X 1.0.0（共同关键词：短剧、分镜）；Y …"，按上面的个数上限截断。纯函数。
# 函数用途: 把同领域的包排成一句里的列表。
def same_domain_text(packs: list[SameDomainPack]) -> str:
    return "；".join(f"{pack.package_id} {pack.version}（共同关键词：{'、'.join(pack.shared_keywords[:SHARED_KEYWORD_LIMIT_COUNT])}）"
                    for pack in packs[:SAME_DOMAIN_LIST_LIMIT_COUNT])


# LLM: 给用户看的一句提醒（宿主回执原样带上，也给她照抄）：已经装着哪些同领域的包、想合并怎么说；installed=False（还在等确认）
#   时说"要单独装再发确认行"，installed=True（已经装上）时说"不想要怎么删"。packs 为空时返回空串。纯函数。
# 函数用途: 生成"已经装着同领域能力包"的用户提醒。
def same_domain_user_notice(packs: list[SameDomainPack], package_id: str, *, installed: bool) -> str:
    if not packs:
        return ""
    merge = f"想合并成一个，跟她说“把 {package_id} 并进 {packs[0].package_id}”"
    rest = f"不想要 {package_id}，发 /plugins#{package_id} 删除" if installed else f"要单独装 {package_id}，再发确认行"
    return f"提醒：已经装着她做的同领域能力包 {same_domain_text(packs)}。{merge}；{rest}。"


# LLM: 能力包刚装上（用户确认安装单、或开关开着自动装）以后给用户的提醒：现读安装表，和她做的其它已装能力包比声明关键词；
#   插件、读不了安装表都不提醒（返回空串）。只读。
# 函数用途: 生成"装好了，但还装着同领域的包"的用户提醒。
def installed_notice(store: LearnpackStore, owner: object, record: BuildRecord) -> str:
    if record.kind != KIND_CAPABILITY_PACK:
        return ""
    try:
        entries = tuple(PluginInstallStore(owner).snapshot())
    except (OSError, ValueError):
        return ""
    capability = build_capability(store, record.sha256)
    packs = same_domain_packs(store, entries, record.package_id, capability.keywords if capability is not None else ())
    return same_domain_user_notice(packs, record.package_id, installed=True)
