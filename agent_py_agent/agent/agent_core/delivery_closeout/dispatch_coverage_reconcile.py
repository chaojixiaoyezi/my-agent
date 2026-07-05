"""派工路 coverage 结构对账(§11.1):子代理交付后,把父 requirement coverage 里【有结构化
产物证据】的项标 done——补 solo 路早有、派工路却整个失效的"完整性兜底网"。

真机实锤(u-g6a2 派工分析):子代理把活干完、报告实覆盖 5/5,但父 run 的 requirement
coverage 停在 0/pending——solo 路模型边做边标 done,派工路把活甩给子代理后不回来逐项标,
父账本空转。后果两头堵:交付其实做全了、账本却显示没做(误报"不完整");真没做全时 rework
又分不清"没做"还是"做了没回填"。

治本 = 建【子代理产物 ↔ 父 coverage 结构化对账环】,判据全用结构信号,不靠模型自觉回填:
- 只在【派工路】跑:本 run 有已完成的自家子代理(own_done_children 非空)才动;
  solo 路(无子代理)一字不动。
- 只 credit【自动种的需求枚举项】(coverage_kind=requirement_item / source_ref 以
  auto:requirement 开头);模型自立的 coverage 由模型自己管,不碰。
- 逐项判据 = 交付产物里【出现了指向该项的路径证据】:父 run 的最终交付产物(报告)正文/
  路径 + 各已完成子代理声明/交付的产物路径,拆成【路径段/文件名主干】;父 coverage 项标题
  若本身是【路径式标识符】(项目名/包名如 agentscope-main、openai-agents-python-main)
  且等于其中某个路径段,即判"有产物证据"标 done。纯字面路径段匹配(与
  task_progress_gate._artifact_evidence_projection 的"路径 token 出现即证据"同口径),
  零自然语言/关键词语义判断——铁律。
- 【故意窄】:标题不是路径式标识符(纯功能名/自然语言,如"注册登录""全文搜索")的项,
  结构上无法证明每项已交付,一律【不 credit】、仍 open 交给 coverage-incomplete rework 兜
  (真不完整照样打回)。这是诚实边界:治得了"项目名类需求"的假不完整,治不了"功能名类
  需求"的深层对账(那要靠模型/派工时结构化点名,非本环职责)。
- 只增不减:只把 open 项标 done(附匹配到的路径证据),已 done 的不动,没证据的不碰。
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
    # 先查最便宜的账本侧闸(1 次读):没有可对账的 open 需求项(多数任务/纯问答)→ 直接收手,
    # 连子代理 canonical 文件都不用扫。
    eligible = _eligible_open_requirement_targets(read_task_progress(root, run_id))
    if not eligible:
        return []
    children = own_done_children(closeout)
    if not children:  # 无已完成子代理 = 非派工路(或子代理还没交付)→ solo 路一字不动
        return []
    segments = _delivered_path_segments(report, children)
    if not segments:
        return []
    credited: list[dict[str, Any]] = []
    for identifier, target in eligible:
        source = segments.get(identifier)
        if source:
            credited.append(
                {
                    "id": target["id"],
                    "status": "done",
                    "evidence": [source],
                    "source_ref": _RECONCILE_SOURCE,
                    "notes": "派工子代理交付产物含该项路径证据,结构对账标 done",
                }
            )
    if not credited:
        return []
    write_task_progress(root, run_id, {"coverage": {"targets": credited}})
    return [target["id"] for target in credited]


def _eligible_open_requirement_targets(progress: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """可对账的项:自动种的需求枚举项、当前 open、标题是路径式标识符。返回 (标识符, target)。"""
    coverage = progress.get("coverage") if isinstance(progress, dict) else {}
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    eligible: list[tuple[str, dict[str, Any]]] = []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or not _is_auto_requirement_target(target):
            continue
        if task_progress_status_is_closed(target.get("status")):
            continue
        identifier = _pathlike_identifier(str(target.get("title") or target.get("id") or ""))
        if identifier:
            eligible.append((identifier, target))
    return eligible


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
