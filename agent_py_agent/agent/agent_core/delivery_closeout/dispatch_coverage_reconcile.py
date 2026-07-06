"""派工路 coverage 结构对账(§11.1 + P1 covers 绑定):子代理交付后,把父 coverage 里
【有结构化证据】的项标 done——补 solo 路早有、派工路却整个失效的"完整性兜底网"。

真机实锤(u-g6a2 派工分析):子代理把活干完、报告实覆盖 5/5,但父 run 的 requirement
coverage 停在 0/pending——solo 路模型边做边标 done,派工路把活甩给子代理后不回来逐项标,
父账本空转。后果两头堵:交付其实做全了、账本却显示没做(误报"不完整");真没做全时 rework
又分不清"没做"还是"做了没回填"——大工程失去"逼你做全"的推力,冲不到底。

治本 = 建【子代理产物 ↔ 父 coverage 结构化对账环】,判据全用结构信号,不靠模型自觉回填。
两道判据,从强到弱:

第一道【covers=id 绑定】(P1 主修,治上一棒"猜文件名太脆"):派工时模型在 create_subagents
的 item 里声明 covers=[清单项 id](语义绑定由模型在派工时完成——它知道"建 X 模块"对应哪一项;
代码只载运 id)。子代理 DONE 后按 id 精确打勾,零猜测:
- 任何 open 项都可被 covers credit(绑定是派工方的显式声明,不限自动种的项);
- 但项上还有未闭环 checks 的不动(checks 是模型自定义的细粒度维度,不代填)。

第二道【路径段证据】(§11.1 原兜底,没绑 covers 时仍生效):
- 只 credit【自动种的需求枚举项】(coverage_kind=requirement_item / source_ref 以
  auto:requirement 开头);模型自立的 coverage 由模型自己管,不碰。
- 逐项判据 = 交付产物里【出现了指向该项的路径证据】:父 run 的最终交付产物(报告)正文/
  路径 + 各已完成子代理声明/交付的产物路径,拆成【路径段/文件名主干】;父 coverage 项标题
  若本身是【路径式标识符】(项目名/包名如 agentscope-main)且等于其中某个路径段,即判
  "有产物证据"标 done。纯字面路径段匹配,零自然语言/关键词语义判断——铁律。
- 标题不是路径式标识符(纯功能名/自然语言,如"注册登录")且没被 covers 绑定的项,结构上
  无法证明已交付,一律【不 credit】、仍 open 交给 coverage-incomplete rework 兜(真不完整
  照样打回)。诚实边界:功能名类需求的对账靠派工时 covers 结构化点名,不靠收尾猜。

两道判据的证据源都是【整棵后代树】的 DONE 非占位后代(own_done_children,P-bigbuild 树
归并):大工程模型递归乱派、层层转包时,covers 绑定与产物声明大量落在孙代理层——孙代理
在它自己的派工小现场绑的主清单 covers(见 dispatch_progress_seed 的主账本回落)、声明的
产物路径,收口/读账对账时一并归并回父清单,不再只认"直接子代理带 covers"。

共同姿态:
- 只在【派工路】跑:本 run 有已完成的自家后代(own_done_children 非空,天然排占位空壳)
  才动;solo 路(无子代理)一字不动。
- 只增不减:只把 open 项标 done(附证据),已 done 的不动,没证据的不碰。
- 永不抛错:对账是增强,失败绝不影响 closeout(与 requirement_coverage_seed /
  dispatch_progress_seed 同一防御姿态)。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ...task_progress import read_task_progress, task_progress_status_is_closed, write_task_progress
from .subagent_aggregation import own_done_children

# 自动种的需求枚举项标记(与 requirement_coverage_seed 同源:coverage_kind / source_ref)。
_REQUIREMENT_COVERAGE_KIND = "requirement_item"
_REQUIREMENT_SOURCE_PREFIX = "auto:requirement"
_RECONCILE_SOURCE = "auto:dispatch-coverage-reconcile"
_COVERS_SOURCE = "auto:dispatch-covers-binding"
_MAX_COVERS_EVIDENCE_CHILDREN = 3

# 交付产物里"路径形态"的 token:含 `/` 或以合法扩展名收尾(纯 ASCII——散文/中文不当路径,
# 防把普通句子误当路径拆段)。与 task_progress_gate._EVIDENCE_TOKEN_RE 同类结构口径。
# 注:前后界只排除【标识符字符】不排除 `/`——绝对路径("/x/openai-agents-python-main/…")
# 的首段紧跟前导斜杠,把 `/` 也排除会让它整条匹配不上(单测实锤)。
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.@-])"
    r"(?P<tok>[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@/-]+|\.[A-Za-z][A-Za-z0-9]{0,8}))"
    r"(?![A-Za-z0-9_.@-])"
)
# 路径式标识符(项目名/包名):≥6 位 ASCII,且【含分隔符 -_. 或纯字母数字≥8 位】——
# 偏向具体复合标识符,把"index""main"这类通用短词挡在外(防误 credit)。
_PATHLIKE_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{5,}")
_ARTIFACT_TEXT_CAP = 200_000
_MAX_ARTIFACT_FILES = 16
_DECLARED_FIELDS = ("output_files", "output_refs")


def reconcile_dispatch_coverage(closeout: object, report: dict[str, Any] | None, root: Path | None, run_id: str) -> list[str]:
    """派工路 coverage 结构对账入口:credit 有产物证据的 open 需求项。返回被 credit 的 id 列表。"""
    try:
        return _reconcile(closeout, report or {}, root, run_id)
    except Exception:  # noqa: BLE001 - 对账是增强,失败绝不影响 closeout
        logging.getLogger(__name__).warning("dispatch coverage reconcile failed", exc_info=True)
        return []


def _reconcile(closeout: object, report: dict[str, Any], root: Path | None, run_id: str) -> list[str]:
    if root is None or not run_id:
        return []
    # 先查最便宜的账本侧闸(1 次读):没有任何 open coverage 项(多数任务/纯问答)→ 直接收手,
    # 连子代理 canonical 文件都不用扫。
    open_targets = _open_coverage_targets(read_task_progress(root, run_id))
    if not open_targets:
        return []
    children = own_done_children(closeout)
    if not children:  # 无已完成子代理 = 非派工路(或子代理还没交付)→ solo 路一字不动
        return []
    credited: dict[str, dict[str, Any]] = {}
    _credit_covers_bindings(credited, open_targets, children)
    _credit_path_segment_evidence(credited, open_targets, report, children)
    if not credited:
        return []
    write_task_progress(root, run_id, {"coverage": {"targets": list(credited.values())}})
    return list(credited)


def _open_coverage_targets(progress: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = progress.get("coverage") if isinstance(progress, dict) else {}
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    return [
        target
        for target in (targets if isinstance(targets, list) else [])
        if isinstance(target, dict)
        and str(target.get("id") or "").strip()
        and not task_progress_status_is_closed(target.get("status"))
    ]


def _credit_covers_bindings(
    credited: dict[str, dict[str, Any]],
    open_targets: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """第一道:派工时的 covers=id 绑定 → 子代理 DONE 即按 id 打勾(纯 id 对账,零猜测)。

    绑定是派工方的显式声明,任何 open 项都可被 credit;但项上还有未闭环 checks 的不动
    (checks 是模型自定义的细粒度维度,只标 status 会留下半完成假象,更不能代填 checks)。
    """
    covered_by = _covers_index(children)
    if not covered_by:
        return
    for target in open_targets:
        target_id = str(target.get("id") or "").strip()
        if target_id in credited or target_id not in covered_by or _has_open_checks(target):
            continue
        credited[target_id] = {
            "id": target_id,
            "status": "done",
            "evidence": _covers_evidence(covered_by[target_id]),
            "source_ref": _COVERS_SOURCE,
            "notes": "派工时 covers 绑定该项的子代理已 DONE,按 id 结构对账标 done",
        }


def _covers_index(children: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """清单项 id → 声明 covers 它且已 DONE 的子代理列表(children 已是 own+DONE+非占位)。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for item in children:
        for target_id in _child_covers(item):
            index.setdefault(target_id, []).append(item)
    return index


def _child_covers(item: dict[str, Any]) -> list[str]:
    attrs = item.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    return [str(raw).strip() for raw in _list(attrs.get("covers")) if str(raw or "").strip()]


def _covers_evidence(children: list[dict[str, Any]]) -> list[str]:
    evidence: list[str] = []
    for item in children[:_MAX_COVERS_EVIDENCE_CHILDREN]:
        run_id = str(item.get("run_id") or "").strip()
        if run_id:
            evidence.append(f"subagent-done:{run_id}")
        evidence.extend(_child_declared_paths(item)[:2])
    return evidence


def _credit_path_segment_evidence(
    credited: dict[str, dict[str, Any]],
    open_targets: list[dict[str, Any]],
    report: dict[str, Any],
    children: list[dict[str, Any]],
) -> None:
    """第二道(§11.1 原兜底):自动种的需求项、标题是路径式标识符、交付产物含其路径段证据。"""
    eligible = _eligible_open_requirement_targets(open_targets, credited)
    if not eligible:
        return
    segments = _delivered_path_segments(report, children)
    if not segments:
        return
    for identifier, target in eligible:
        source = segments.get(identifier)
        if source:
            credited[str(target["id"])] = {
                "id": target["id"],
                "status": "done",
                "evidence": [source],
                "source_ref": _RECONCILE_SOURCE,
                "notes": "派工子代理交付产物含该项路径证据,结构对账标 done",
            }


def _eligible_open_requirement_targets(
    open_targets: list[dict[str, Any]],
    credited: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """路径段兜底可对账的项:自动种的需求枚举项、尚未被 credit、标题是路径式标识符。"""
    eligible: list[tuple[str, dict[str, Any]]] = []
    for target in open_targets:
        if str(target.get("id") or "") in credited or not _is_auto_requirement_target(target):
            continue
        identifier = _pathlike_identifier(str(target.get("title") or target.get("id") or ""))
        if identifier:
            eligible.append((identifier, target))
    return eligible


def _has_open_checks(target: dict[str, Any]) -> bool:
    checks = target.get("checks")
    if not isinstance(checks, dict):
        return False
    return any(not task_progress_status_is_closed(status) for status in checks.values())


def _is_auto_requirement_target(target: dict[str, Any]) -> bool:
    if str(target.get("coverage_kind") or "").strip() == _REQUIREMENT_COVERAGE_KIND:
        return True
    return str(target.get("source_ref") or "").strip().startswith(_REQUIREMENT_SOURCE_PREFIX)


def _pathlike_identifier(title: str) -> str | None:
    ident = " ".join(str(title or "").split()).casefold()
    if not _PATHLIKE_IDENTIFIER_RE.fullmatch(ident):
        return None
    if not (any(sep in ident for sep in "-_.") or len(ident) >= 8):
        return None
    return ident


def _delivered_path_segments(report: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, str]:
    """交付产物里所有【路径段/文件名主干】→ 代表性来源路径 token(casefold 键)。"""
    segments: dict[str, str] = {}
    for raw in _delivered_path_sources(report, children):
        for match in _PATH_TOKEN_RE.finditer(raw):
            _add_path_segments(segments, match.group("tok"))
    return segments


def _delivered_path_sources(report: dict[str, Any], children: list[dict[str, Any]]) -> list[str]:
    """证据文本来源:父最终交付产物(路径+正文)+ 各已完成子代理声明/交付的产物路径。"""
    sources: list[str] = []
    for path in _artifact_paths(report):
        sources.append(path)
        text = _artifact_file_text(Path(path))
        if text:
            sources.append(text)
    for item in children:
        sources.extend(_child_declared_paths(item))
    return sources


def _artifact_paths(report: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    for item in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        path = str(item.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= _MAX_ARTIFACT_FILES:
            break
    return paths


def _artifact_file_text(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:_ARTIFACT_TEXT_CAP]
    except OSError:
        return ""


def _child_declared_paths(item: dict[str, Any]) -> list[str]:
    attrs = item.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    paths: list[str] = []
    for field_name in _DECLARED_FIELDS:
        paths.extend(str(text) for text in _list(attrs.get(field_name)) if str(text or "").strip())
    paths.extend(str(text) for text in _list(item.get("artifact_refs")) if str(text or "").strip())
    return paths


def _add_path_segments(segments: dict[str, str], token: str) -> None:
    for part in token.replace("\\", "/").split("/"):
        segment = part.strip().casefold()
        if len(segment) < 2:
            continue
        segments.setdefault(segment, token)
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        if stem and stem != segment and len(stem) >= 2:
            segments.setdefault(stem, token)


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


__all__ = ["reconcile_dispatch_coverage"]
