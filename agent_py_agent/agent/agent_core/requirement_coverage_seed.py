"""需求枚举项 → coverage 功能清单自动派生(A2 回炉:耐力机制"从没触发"的根治)。

真机实锤:coverage_incomplete_rework 机制单测过、能正确打回,但它挂在"模型自觉先立
功能清单"上——大体量建站任务两个用户 coverage.targets 计数全是 0,打回从没 fire,
代码量反掉一半。用一个不稳的模型行为(自觉立清单)去兜另一个不稳(耐力),等于没兜。

治本:需求原文里【列举出来的功能点/问题项】在 run 入口自动登记成 coverage.targets
——清单来自需求本身,不靠模型自觉,coverage-rework 从此有抓手,出口进展签名(又闭
环一项=真进展)也有燃料。两条通道取并集去重:①行首列表记号(-/*/1./一、/①/…);
②内联顿号串(一句话里"甲、乙、丙"并列 ≥3 短项——真实需求大量是内联写法,只认行首
时建站 11 功能/分析 5 项目一条没种,兜底全空转)。守铁律:纯字面记号解析,零语义
判断;条目真不真、要不要做,仍由模型对账时定(双出口:做完标 done / 不适用标
skipped 写原因)。非硬性配额:枚举不足 3 条不立账,清单可改可裁。只在前台创建路
(gateway/cli/chat,root_user_prompt=原始需求)种;后台唤醒轮的 prompt 是机器拼的整合
指令、绝不种(见 _seed)。已知边界:顿号在日文里是普通读点,日文散文可能误种(可裁,
故可接受);逗号绝不拆,见通道常量注释。

种子永不抛错——失败绝不影响 run 本身(与 dispatch_progress_seed 同一防御姿态)。
"""

from __future__ import annotations

import logging
import re
import unicodedata
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

# 内联枚举通道:顿号 `、` 是中文枚举专用分隔符,拆它是纯字面动作、零语义判断。
# ⚠️ 逗号(,/,)绝不当分隔符——散文里无处不在,盲拆会把任意句子拆成假需求。逗号和
# 其它标点只作【边界】限定顿号串两端(首项剥前缀如"功能要全:"、末项剥尾缀如"。帮我…"):
# 边界只裁不造,含逗号的普通句子切完每段零顿号,拆不出任何 item。
# 半角句点 . 不作边界:枚举项常含它(node.js / agentscope-main 类),拆了会腰斩英文项。
_INLINE_SEPARATOR = "、"
_INLINE_BOUNDARY_RE = re.compile(r"[。!?;:,!?;:,…—()()\[\]【】{}《》〈〉「」『』“”‘’\"']")
_INLINE_MIN_RUN = 3  # 同一串 ≥3 项才算枚举:两项顿号并列(如"苹果、香蕉都行")在散文里太常见
_INLINE_ITEM_MAX_WIDTH = 24.0  # 枚举项都短;"字"按东亚宽度计(全角=1/半角=0.5),24 字≈48 半角字符

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
    # 后台唤醒轮的 prompt 是机器拼的整合指令(如"1. 先看 Active Wake Signal…读取、整合、验证、
    # 收尾…"),不是用户需求;且 runtime_mixin 在种子挂钩前已把空 root_user_prompt 回填成本轮
    # user_prompt=指令,故靠"root_user_prompt 是否为空"分不出来(真机问候线实锤:种出 13 条指令
    # 假需求)。需求 coverage 只在前台创建路(gateway/cli/chat,root_user_prompt=原始需求)种;
    # 后台轮是续跑,只读已种清单驱动 rework,绝不按指令重种。判据=source,纯结构化零语义。
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
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
    """需求原文 → 枚举项标题列表(纯字面记号:跳过代码围栏,行首列表∪内联顿号串,去重截断)。"""
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
        # 行首列表行整行正文就是一项(行为与从前一字不动),不再内联拆——防同一行双记账。
        bodies = [body] if body is not None else _inline_enumeration_items(line)
        _extend_deduped(items, seen, bodies)
        if len(items) >= _MAX_ITEMS:
            break
    return items


def _extend_deduped(items: list[str], seen: set[str], bodies: list[str]) -> None:
    for body in bodies:
        key = " ".join(body.split()).casefold()
        if len(key) < 2 or key in seen or len(items) >= _MAX_ITEMS:
            continue
        seen.add(key)
        items.append(body[:_TITLE_CAP])


def _inline_enumeration_items(line: str) -> list[str]:
    """内联枚举通道:同一片段里 ≥3 个顿号并列短项才算(如"注册登录、增删改查、全文搜索")。"""
    items: list[str] = []
    for segment in _INLINE_BOUNDARY_RE.split(line):
        if segment.count(_INLINE_SEPARATOR) < _INLINE_MIN_RUN - 1:
            continue
        pieces = (piece.strip() for piece in segment.split(_INLINE_SEPARATOR))
        valid = [piece for piece in pieces if piece and _display_width(piece) <= _INLINE_ITEM_MAX_WIDTH]
        if len(valid) >= _INLINE_MIN_RUN:
            items.extend(valid)
    return items


def _display_width(text: str) -> float:
    # "短项 ≤24 字"按显示宽度算:CJK 全角记 1 字、半角记 0.5——纯汉字口径若按字符数算,
    # 英文包名(如 openai-agents-python-main = 25 个半角字符)会被误杀,真实分析用例断供。
    return sum(1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5 for char in text)


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
    # 种子只在前台创建路跑(background_main_agent 已在 _seed 前置拦截),账本键=本轮 run_id;
    # 与 task_progress 工具/收尾门同一套键语义(前台 gateway/cli run_id 即任务主账)。
    return str(getattr(params, "run_id", "") or "").strip()


__all__ = [
    "REQUIREMENT_COVERAGE_SEED_NOTE_TEMPLATE",
    "requirement_enumeration_items",
    "run_params_with_requirement_coverage_seed",
]
