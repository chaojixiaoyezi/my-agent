"""需求枚举项 → coverage 功能清单自动派生(A2 回炉:耐力机制"从没触发"的根治)。

真机实锤:coverage_incomplete_rework 机制单测过、能正确打回,但它挂在"模型自觉先立
功能清单"上——大体量建站任务两个用户 coverage.targets 计数全是 0,打回从没 fire,
代码量反掉一半。用一个不稳的模型行为(自觉立清单)去兜另一个不稳(耐力),等于没兜。

治本:需求原文里【列举出来的功能点/问题项】(列表字面记号:-/*/1./一、/①/…)在 run
入口自动登记成 coverage.targets——清单来自需求本身,不靠模型自觉,coverage-rework
从此有抓手,出口进展签名(又闭环一项=真进展)也有燃料。守铁律:纯字面记号解析,
零语义判断;条目真不真、要不要做,仍由模型对账时定(双出口:做完标 done / 不适用
标 skipped 写原因)。非硬性配额:枚举不足 3 条不立账,清单可改可裁。

种子永不抛错——失败绝不影响 run 本身(与 dispatch_progress_seed 同一防御姿态)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..task_progress import read_task_progress, write_task_progress

# 与 runtime/run_params 的内部 scope 判定同规:系统内部轮(task_local/control_plane)不立账。
_INTERNAL_SCOPES = frozenset({"task_local", "control_plane"})
_MIN_ITEMS = 3
_MAX_ITEMS = 40
_TITLE_CAP = 100
_SOURCE_REF = "auto:requirement-enumeration"

# 列表行字面记号(枚举=需求自带的结构):bullet / 阿拉伯序号 / 中文序号 / 带圈数字 / 括号序号。
_LIST_LINE_RE = re.compile(
    r"^\s{0,8}(?:"
    r"(?P<bullet>[-*+•·])\s+"
    r"|(?P<arabic>\d{1,3})[.)、．]\s*"
    r"|(?P<cjk>[一二三四五六七八九十]{1,3})[、.)]\s*"
    r"|(?P<circled>[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])\s*"
    r"|[(（](?P<paren>\d{1,3})[)）]\s*"
    r")(?P<body>\S.*)$"
)
_CHECKBOX_PREFIX_RE = re.compile(r"^\[[ xX]?\]\s*")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

REQUIREMENT_COVERAGE_SEED_NOTE_TEMPLATE = (
    "[requirement-coverage-seed] 需求原文里列举了 {count} 条待办项,已自动登记为 coverage "
    "功能清单(task_progress 可查可改)。逐项做完标 done 并附证据;确认不适用的项标 "
    "skipped 写明原因。大体量构建/分析任务按清单逐项闭环再收口,别凭感觉收工。"
)


def run_params_with_requirement_coverage_seed(agent: object, user_prompt: str, params: object) -> object:
    """run 入口挂载点:派生成功则在 params.inject 附一条结构化告知;失败原样返回。"""
    try:
        seeded = _seed(agent, user_prompt, params)
    except Exception:  # noqa: BLE001 - 账本种子是增强,失败绝不影响 run
        logging.getLogger(__name__).warning("requirement coverage seed failed", exc_info=True)
        return params
    if not seeded:
        return params
    note = REQUIREMENT_COVERAGE_SEED_NOTE_TEMPLATE.format(count=seeded["seeded"])
    return replace(params, inject=[*(getattr(params, "inject", None) or []), note])


def _seed(agent: object, user_prompt: str, params: object) -> dict[str, Any] | None:
    if str(getattr(params, "context_scope", "") or "").strip().lower() in _INTERNAL_SCOPES:
        return None
    prompt = str(getattr(params, "root_user_prompt", "") or "").strip() or str(user_prompt or "")
    items = requirement_enumeration_items(prompt)
    if len(items) < _MIN_ITEMS:
        return None
    root = _progress_root(agent)
    run_id = _ledger_run_id(params)
    if root is None or not run_id:
        return None
    existing = read_task_progress(root, run_id)
    coverage = existing.get("coverage") if isinstance(existing.get("coverage"), dict) else {}
    if coverage.get("targets"):
        return None  # 已有清单(模型自立/上一轮种子):不重复立,不覆盖
    targets = [
        {
            "id": f"req-{index:02d}",
            "title": title,
            "status": "pending",
            "coverage_kind": "requirement_item",
            "source_ref": _SOURCE_REF,
        }
        for index, title in enumerate(items, start=1)
    ]
    write_task_progress(
        root,
        run_id,
        {"coverage": {"goal": "需求枚举项对账(自动派生,可改可裁)", "targets": targets}},
    )
    return {"run_id": run_id, "seeded": len(targets)}


def requirement_enumeration_items(prompt: str) -> list[str]:
    """需求原文 → 枚举项标题列表(纯字面记号:跳过代码围栏,列表行取正文,去重截断)。"""
    items: list[str] = []
    seen: set[str] = set()
    in_fence = False
    for line in str(prompt or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        body = _list_item_body(line)
        if body is None:
            continue
        key = " ".join(body.split()).casefold()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        items.append(body[:_TITLE_CAP])
        if len(items) >= _MAX_ITEMS:
            break
    return items


def _list_item_body(line: str) -> str | None:
    match = _LIST_LINE_RE.match(line)
    if match is None:
        return None
    body = match.group("body").strip()
    if match.group("bullet"):
        body = _CHECKBOX_PREFIX_RE.sub("", body).strip()
    # "3.14 xyz" 这类行首小数不是序号:阿拉伯序号后正文以数字开头就不当列表行。
    if (match.group("arabic") or match.group("paren")) and body[:1].isdigit():
        return None
    return body or None


def _progress_root(agent: object) -> Path | None:
    # 与 task_progress_gate._progress_root / dispatch_progress_seed 同规:owner home 优先。
    owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _ledger_run_id(params: object) -> str:
    # 与 task_progress 工具/收尾门/派工种子同一套账本键语义:后台唤醒轮按 task_id 续主账。
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        task_id = str(getattr(params, "task_id", "") or "").strip()
        if task_id:
            return task_id
    return str(getattr(params, "run_id", "") or "").strip()


__all__ = [
    "REQUIREMENT_COVERAGE_SEED_NOTE_TEMPLATE",
    "requirement_enumeration_items",
    "run_params_with_requirement_coverage_seed",
]
