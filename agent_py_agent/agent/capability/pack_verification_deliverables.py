# LLM: 能力包 v2 块 5：钉住包声明了 required 的交付物，本回合有没有真的写出来。只看结构化事实：本回合新建或改过的文件（块 3 基线
#   比对）、交付物的路径模式和字段匹配、文件能不能打开；不读模型文字、不按文件名语义猜。
#   触发条件（3a 定）：只在本回合改过工作区时才查（调用方保证有块 3 基线），纯问答和只读审稿的回合不触发；一个工具都不调、
#   直接在答复里贴内容的回合查不到，是已知限制。判定：有匹配（路径 + 字段）的文件就算交了；只有路径匹配、但按声明格式打不开的
#   记 DELIVERABLE_UNREADABLE；一个都没有记 DELIVERABLE_MISSING。路径匹配、能打开、字段不匹配的文件不是这个交付物。
#   改动同步 test_pack_verification_deliverables.py 与 docs/design/CAPABILITY_PACKS_V2.md 第 5 节。
# 模块用途: 找出本回合该交却没交（或交了打不开）的交付物，并生成返工提示。

from __future__ import annotations

from pathlib import Path

from .pack_verification_matching import file_matches, file_readable, path_matches

DELIVERABLE_MISSING = "DELIVERABLE_MISSING"
DELIVERABLE_UNREADABLE = "DELIVERABLE_UNREADABLE"
# 收尾发现缺交付物时最多给几次返工提示（3a 审定 2 次，沿用子代理交付闸的上限）。
MAX_DELIVERABLE_REWORK_COUNT = 2
# 返工提示里最多列几个缺的交付物。
MAX_DELIVERABLE_REWORK_ITEMS_COUNT = 5


# LLM: packages 是本任务钉住、可核验的包；changed 是本回合确定新建或改过的工作区相对路径；root 是工作区根。
#   每个 required 交付物给出一条结构化事实（缺或打不开），交了的不出现。
# 函数用途: 列出本回合缺的或打不开的必需交付物。
def missing_deliverables(packages: tuple, changed: list[str], root: Path) -> list[dict]:
    items = []
    for package in packages:
        required = [item for item in package.verification.deliverables if item.required]
        items.extend(fact for deliverable in required
                     for fact in _deliverable_issue(deliverable, changed, root, package.installation.manifest))
    return items


# 函数用途: 判断一个必需交付物的状态，交了返回空列表，否则返回一条事实。
def _deliverable_issue(deliverable: object, changed: list[str], root: Path, manifest: object) -> list[dict]:
    if any(file_matches(deliverable, relpath, root / relpath) for relpath in changed):
        return []
    by_path = [relpath for relpath in changed if any(path_matches(pattern, relpath) for pattern in deliverable.path_patterns)]
    unreadable = [relpath for relpath in by_path if not file_readable(root / relpath, deliverable.field_match)]
    fact = {"code": DELIVERABLE_UNREADABLE if unreadable else DELIVERABLE_MISSING, "package_id": manifest.plugin_id,
            "package_version": manifest.version, "deliverable_id": deliverable.id,
            "path_patterns": list(deliverable.path_patterns), "paths": unreadable[:MAX_DELIVERABLE_REWORK_ITEMS_COUNT]}
    if deliverable.field_match is not None:
        fact["field_match"] = deliverable.field_match.to_payload()
    return [fact]


# LLM: 只列结构化事实（包、交付物编号、路径模式和字段要求、打不开的文件），不替模型决定写什么；末尾说明只审不交付时怎么办。
# 函数用途: 生成一次缺交付物的返工提示。
def deliverable_rework_text(items: list[dict]) -> str:
    lines = ["宿主发现本回合改过工作区，但钉住的能力包要求的交付物还没交："]
    for item in items[:MAX_DELIVERABLE_REWORK_ITEMS_COUNT]:
        need = f"路径符合 {'、'.join(item['path_patterns'])}"
        if item.get("field_match"):
            match = item["field_match"]
            need += f"，{match['format']} 顶层字段 {match['field']} 为 {'或'.join(match['equals'])}"
        if item["code"] == DELIVERABLE_UNREADABLE:
            lines.append(f"- {item['package_id']} 的 {item['deliverable_id']}（{need}）：{'、'.join(item['paths'])} 打不开或解析不了。")
        else:
            lines.append(f"- {item['package_id']} 的 {item['deliverable_id']}（{need}）：本回合没有写出。")
    lines.append("请把交付物写成工作区里的文件，并在答复里写明路径。如果用户这次只要审阅、不要交付物，在答复里说明即可。")
    return "\n".join(lines)
