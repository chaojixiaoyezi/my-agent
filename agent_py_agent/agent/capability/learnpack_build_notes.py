# LLM: learnpack 打包回执里的版本事实与软提醒（第 6 步复审建议），全部不挡打包——版本号和文件清单是质量问题，铁律"硬门只守
#   安全和客观事实"。只读 learnpack 存储与安装表；安装表读不了就按"现在没装着"算，不让打包失败。提醒码登记在
#   contracts/error_taxonomy：
#   - PACKAGE_BUILD_ID_TAKEN：包名被别处的包占着（和安装时同一个裁决 learnpack_service.ownership_problem），这个包装不上，只能换
#     包名单独成包；这时不再给下面三种（版本和文件提醒会把她带去绕一圈，复审建议）；
#   - PACKAGE_BUILD_VERSION_REUSED：和她装上过的某一版同号、字节不同（没装过的同号重打是正常迭代，不提醒）；
#   - PACKAGE_BUILD_VERSION_OLDER：新旧版本号都是纯数字点分、且比现在装着的旧（装上去版本号会倒退）；
#   - PACKAGE_BUILD_FILES_DROPPED：比上一版少了文件（上一版取现在装着的、她做的那一版；没有就取她上一次打的），合并时旧方向的
#     文件没带过来就会这样。
#   改规则要同步 tooling/package_build_tool 的回执与 test_learnpack_merge。
# 模块用途: 打包后告诉她现在装着哪一版、她做过哪些版本，以及版本号重用、倒退或比上一版少了文件的提醒。
from __future__ import annotations

import re

from ..plugin_install_store import PluginInstallStore
from .learnpack_service import (
    installed_entry,
    installed_versions,
    learnpack_owner,
    ownership_problem,
)
from .learnpack_store import BuildRecord, LearnpackStore

# 文件少了时回执里最多列出的路径个数。
_DROPPED_LIST_LIMIT_COUNT = 10
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
    return {**facts, "warnings": [*_version_warnings(store, record, installed), *_dropped_files(store, record, base)]}


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
