# LLM: learnpack 打包回执里的版本事实与软提醒（第 6 步复审建议），全部不挡打包——版本号和文件清单是质量问题，铁律"硬门只守
#   安全和客观事实"。只读 learnpack 存储与安装表；安装表读不了就按"现在没装着"算，不让打包失败。提醒码登记在
#   contracts/error_taxonomy：
#   - PACKAGE_BUILD_ID_TAKEN：包名被别处的包占着（和安装时同一个裁决 learnpack_service.ownership_problem），这个包装不上，只能换
#     包名单独成包；这时不再给下面三种（版本和文件提醒会把她带去绕一圈，复审建议）；
#   - PACKAGE_BUILD_VERSION_REUSED：和她装上过的某一版同号、字节不同（没装过的同号重打是正常迭代，不提醒）；
#   - PACKAGE_BUILD_VERSION_OLDER：新旧版本号都是纯数字点分、且比现在装着的旧（装上去版本号会倒退）；
#   - PACKAGE_BUILD_FILES_DROPPED：比上一版少了文件（上一版取现在装着的、她做的那一版；没有就取她上一次打的），合并时旧方向的
#     文件没带过来就会这样；
#   - PACKAGE_BUILD_SAME_DOMAIN：新能力包和她自己做的、装着的另一个能力包声明的关键词有重合（learnpack 生产测试：学第二个短剧
#     原版时她没问就另起了一个包），提醒她按学习流程先问用户并进去还是单独成包；
#   - PACKAGE_BUILD_KEYWORDS_SCRIPT：能力包说明里有非拉丁文字（如中文），关键词却全是拉丁字母（learnpack 生产测试：小说包关键词
#     全是英文，中文提问一次都推荐不到，两遍小说题都没用上包）。
#   改规则要同步 tooling/package_build_tool 的回执与 test_learnpack_merge。
# 模块用途: 打包后告诉她现在装着哪一版、她做过哪些版本，以及版本号重用、倒退、比上一版少了文件、已经装着同领域能力包或关键词
#   和说明不是同一种文字的提醒。
from __future__ import annotations

import re
import unicodedata

from ..plugin_install_store import PluginInstallStore
from ..plugin_manifest import PluginPackageError
from ..plugin_package import inspect_plugin_package
from .learnpack_service import (
    installed_entry,
    installed_versions,
    learnpack_owner,
    ownership_problem,
)
from .learnpack_store import BuildRecord, LearnpackStore
from .package_build import KIND_CAPABILITY_PACK

# 文件少了时回执里最多列出的路径个数。
_DROPPED_LIST_LIMIT_COUNT = 10
# 同领域提醒里最多列出的已装包个数。
_SAME_DOMAIN_LIST_LIMIT_COUNT = 3
# 同领域提醒里每个包最多列出的共同关键词个数。
_SHARED_KEYWORD_LIMIT_COUNT = 5
# 能比较先后的版本号：纯数字点分（如 0.2.0）；其它写法不比较、不提醒。
_NUMERIC_VERSION = re.compile(r"\d+(?:\.\d+)*")


# LLM: 返回 {"installed_version": 现在装着的同名包版本（没装为空串）, "installed_by_me": 装着的那个是不是她做的（字节在
#   learnpack 存储里有记录，和 /plugins# 列表"她做的"同一判断）, "previous_versions": 她以前做过的这个包名的各版本（按打包先后
#   去重，不含这次）, "warnings": [{"code", "message"}]}。只读。
# 函数用途: 生成打包回执里的版本事实和提醒。
def build_notes(agent: object, store: LearnpackStore, record: BuildRecord) -> dict[str, object]:
    owner, entries = _install_table(agent)
    installed = installed_entry(entries, record.package_id)
    mine = store.build(installed.package_sha256) if installed is not None else None
    earlier = [item for item in store.builds_for(record.package_id) if item.sha256 != record.sha256]
    facts = {"installed_version": installed.manifest.version if installed is not None else "",
             "installed_by_me": mine is not None, "previous_versions": list(dict.fromkeys(item.version for item in earlier))}
    if ownership_problem(store, owner, record, entries) is not None:
        return {**facts, "warnings": [_note(
            "PACKAGE_BUILD_ID_TAKEN", f"包名 {record.package_id} 被别处的包占着（同名或只差大小写的包，或别处插件卸载后留下的数据），"
            "这个包装不上；同领域的内容只能换个包名单独成包。")]}
    base = mine or (earlier[-1] if earlier else None)
    capability = _capability(store, record.sha256) if record.kind == KIND_CAPABILITY_PACK else None
    return {**facts, "warnings": [*_version_warnings(store, record, installed), *_dropped_files(store, record, base),
                                  *_same_domain(store, capability, record, entries), *_keyword_script(capability)]}


# LLM: 安装表读不了（损坏、权限）就当一个都没装；owner 照常解析。只读。
# 函数用途: 取当前 owner 和整张安装表快照。
def _install_table(agent: object) -> tuple[object, tuple]:
    owner = learnpack_owner(agent)
    try:
        return owner, tuple(PluginInstallStore(owner).snapshot())
    except (OSError, ValueError):
        return owner, ()


# LLM: 同号只和她装上过的版本比（installed_versions 按 learnpack 安装记录取）；倒退只和现在装着的比，且两边都得是纯数字点分。只读。
# 函数用途: 生成版本号重用与倒退的提醒。
def _version_warnings(store: LearnpackStore, record: BuildRecord, installed: object | None) -> list[dict[str, str]]:
    notes = []
    reused = [item for item in installed_versions(store, record.package_id)
              if item.version == record.version and item.sha256 != record.sha256]
    if reused:
        notes.append(_note("PACKAGE_BUILD_VERSION_REUSED",
                           f"{record.package_id} {record.version} 以前装上过内容不同的一版（指纹 {reused[-1].sha256[:12]}）；"
                           "改版或合并要把 declaration.version 升一级再打包，不然列表和退回里只能靠指纹区分。"))
    current = installed.manifest.version if installed is not None else ""
    if current and _older(record.version, current):
        notes.append(_note("PACKAGE_BUILD_VERSION_OLDER",
                           f"现在装着 {record.package_id} {current}，这次打的 {record.version} 比它旧，装上去版本号会倒退；"
                           f"改版或合并要用比 {current} 大的版本号。"))
    return notes


# LLM: 比较两份打包产物里的文件名（同一套读法，与包内布局无关）；上一版就是这一份或没有上一版时不提醒。只读。
# 函数用途: 生成"比上一版少了文件"的提醒。
def _dropped_files(store: LearnpackStore, record: BuildRecord, base: BuildRecord | None) -> list[dict[str, str]]:
    if base is None or base.sha256 == record.sha256:
        return []
    missing = sorted(set(store.build_files(base.sha256)) - set(store.build_files(record.sha256)))
    if not missing:
        return []
    shown = "、".join(missing[:_DROPPED_LIST_LIMIT_COUNT]) + ("…" if len(missing) > _DROPPED_LIST_LIMIT_COUNT else "")
    return [_note("PACKAGE_BUILD_FILES_DROPPED",
                  f"比上一版 {base.version}（指纹 {base.sha256[:12]}）少了 {len(missing)} 个文件：{shown}。合并或改版要把旧方向的"
                  "文件原样带上；非文本文件带不过来就如实告诉用户，确实要删的不用管。")]


# LLM: 只看能力包（capability 为 None 就不提醒）：她自己做的（字节在 learnpack 存储里有记录）、装着的、包名不同的能力包，声明的
#   关键词和新包有重合（不分大小写的整词相同）就提醒。关键词是声明里的结构化字段，不解析正文；别处装的包不能并入，不提醒。只读。
# 函数用途: 生成"已经装着同领域能力包，先问用户并进去还是单独成包"的提醒。
def _same_domain(store: LearnpackStore, capability: object | None, record: BuildRecord,
                 entries: tuple) -> list[dict[str, str]]:
    words = {word.casefold() for word in capability.keywords} if capability is not None else set()
    hits = []
    for row in entries:
        capability = getattr(row.manifest, "capability", None)
        if not words or capability is None or row.manifest.plugin_id == record.package_id:
            continue
        shared = sorted(words & {word.casefold() for word in capability.keywords})
        if shared and store.build(row.package_sha256) is not None:
            hits.append(f"{row.manifest.plugin_id} {row.manifest.version}（共同关键词："
                        f"{'、'.join(shared[:_SHARED_KEYWORD_LIMIT_COUNT])}）")
    if not hits:
        return []
    return [_note("PACKAGE_BUILD_SAME_DOMAIN",
                  f"你已经装着自己做的同领域能力包：{'；'.join(hits[:_SAME_DOMAIN_LIST_LIMIT_COUNT])}。按学习流程先问用户："
                  "并进已有的包（加一个方向、升版本），还是单独成包；用户说单独成包再装。")]


# LLM: 宿主按"用户的话里整词出现关键词"推荐能力包（router.has_strong_match），所以说明里有非拉丁文字（按字符的 Unicode 名称判断，
#   如中文、西里尔文）、关键词里却一个这样的字都没有时，用户用说明那种文字提问就推荐不到这个包。只看字符属性，不理解语义；没有
#   关键词或读不了包就不提醒。纯函数。
# 函数用途: 生成"关键词和说明不是同一种文字"的提醒。
def _keyword_script(capability: object | None) -> list[dict[str, str]]:
    if capability is None or not capability.keywords or not _non_latin(capability.description):
        return []
    if any(_non_latin(word) for word in capability.keywords):
        return []
    return [_note("PACKAGE_BUILD_KEYWORDS_SCRIPT",
                  "包的说明用的不是拉丁字母（比如中文），关键词却全是拉丁字母（比如英文）。宿主只在用户的话里整词出现关键词时才推荐"
                  "这个包，用户用说明那种语言提问时会一直推荐不到；加几个用户真会用那种语言说的领域短词。")]


# LLM: 字母（str.isalpha）的 Unicode 名称不以 LATIN 开头就算非拉丁文字；数字、标点、空格不算。纯函数。
# 函数用途: 判断一段文字里有没有非拉丁字母的文字。
def _non_latin(text: str) -> bool:
    return any(char.isalpha() and not unicodedata.name(char, "LATIN").startswith("LATIN") for char in text)


# LLM: 从她打的包里读能力声明（同一个读包校验器）；读不了或不是能力包返回 None。只读。
# 函数用途: 取某个打包产物的能力声明（说明、关键词）。
def _capability(store: LearnpackStore, sha256: str) -> object | None:
    try:
        return inspect_plugin_package(store.build_path(sha256).read_bytes()).manifest.capability
    except (OSError, ValueError, PluginPackageError):
        return None


# LLM: 纯数字点分才比较，末尾的 0 不算（1.0 与 1.0.0 相同）；其它写法一律按"比不出"处理。纯函数。
# 函数用途: 判断新版本号是否比现在装着的旧。
def _older(version: str, current: str) -> bool:
    if _NUMERIC_VERSION.fullmatch(version) is None or _NUMERIC_VERSION.fullmatch(current) is None:
        return False
    return _numeric_key(version) < _numeric_key(current)


# LLM: 只给 _older 用，调用前已确认是纯数字点分。纯函数。
# 函数用途: 把版本号转成去掉末尾 0 的整数元组。
def _numeric_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


# LLM: 结构化提醒，code 给机器、message 给她照着改。纯组装。
# 函数用途: 生成一条提醒。
def _note(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


__all__ = ["build_notes"]
