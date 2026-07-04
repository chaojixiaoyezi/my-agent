# LLM: 交付保障收口层(底座提升 A1,头号):任务收口前的【确定性自检】——活干完了,
#   绝不空手送给用户。真机实锤(四类测试):大文件分析答案全对但最终响应 text 长度=0;
#   多项目分析主报告落 work/ 没进 output/;建站成果在 output/ 但收尾汇总为空。全链没有
#   任何一层校验"最终响应非空/交付物真在 output/"(finalize 无条件取 final_response.text)。
#   本层是【兜底不是限制】:非空文本一字不改;只在响应空白时从结构化记录(task_progress
#   账本/findings 结论账/output 清单/closeout 报告)确定性合成收尾汇总,并把【声明过的】
#   交付物从工作区归集进 output/。守铁律:零自然语言判断,只查空白与落盘事实;合成文本
#   是结构化事实的拼装,不是内容生成。对标 终端应用/会话运行时:任务必以面向用户的收尾
#   汇总结束、绝不空手返回。
# 模块用途: finalize 唯一漏斗上的交付保障自检:响应空白→合成收尾汇总;声明交付物
#   不在 output/→归集;两者都有账可查、可核验。
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...task_progress import read_task_progress, task_progress_summary
from ..run_task_workspace_writer import current_run_task_workspace_root
from .task_progress_gate import _progress_root, _run_id

_LOGGER = logging.getLogger(__name__)

SYNTHESIZED_MARKER = "[delivery-assurance-synthesized]"
_COLLECT_MAX_FILES = 100
_COLLECT_MAX_FILE_BYTES = 64 * 1024 * 1024
_OUTPUT_LIST_CAP = 40
_FINDINGS_TAIL_COUNT = 8
_FINDINGS_TAIL_READ_BYTES = 256 * 1024
_FINDINGS_COUNT_MAX_BYTES = 1024 * 1024
_CLAIM_PREVIEW_CHARS = 200
_RECENT_DONE_SHOWN = 6
_SKIP_DIR_NAMES = frozenset({".agent_delivery", "node_modules", "__pycache__", ".git"})
_MAIN_SCOPES = frozenset({"", "default"})


# 函数用途: finalize 前的交付保障总入口——归集声明交付物 + 空响应时合成收尾汇总;
#   任何内部异常都不打断 finalize(兜底层自己绝不能成为新故障点)。
def apply_delivery_assurance(agent: object, ctx: Any) -> Any:
    try:
        return _apply(agent, ctx)
    except Exception:
        _LOGGER.warning("delivery assurance failed (run=%s)", getattr(ctx, "run_id", ""), exc_info=True)
        return ctx


def _apply(agent: object, ctx: Any) -> Any:
    if str(getattr(ctx, "context_scope", "") or "default").strip().lower() not in _MAIN_SCOPES:
        # 子代理(task_local)/planner 等有各自的收尾修复链,本层只管主代理面向用户的响应。
        return ctx
    shim = _params_shim(ctx)
    task_root = current_run_task_workspace_root(agent, shim)
    collected = _collect_declared_deliverables(_declared_refs(agent, ctx, shim), task_root)
    text = str(getattr(ctx.final_response, "text", "") or "")
    if text.strip():
        if collected:
            return _with_text(ctx, text.rstrip() + "\n\n" + _collected_note(collected))
        return ctx
    synthesized = _synthesize_closeout_text(agent, ctx, shim, (task_root, collected))
    if not synthesized:
        return ctx
    return _with_text(ctx, synthesized)


def _with_text(ctx: Any, text: str) -> Any:
    return replace(ctx, final_response=_response_with_text(ctx.final_response, text))


def _response_with_text(response: Any, text: str) -> Any:
    """换掉响应文本,保留 backend/runtime 元数据;响应对象不是 dataclass 时重建
    ModelResponse(测试/旧调用方会传 SimpleNamespace,兜底层不能因此静默失效)。"""
    try:
        return replace(response, text=text)
    except TypeError:
        from ...backends import ModelResponse

        return ModelResponse(
            text=text,
            backend=str(getattr(response, "backend", "") or ""),
            runtime_status=str(getattr(response, "runtime_status", "ok") or "ok"),
            runtime_reason=str(getattr(response, "runtime_reason", "") or ""),
        )


def _params_shim(ctx: Any) -> SimpleNamespace:
    """workspace/账本解析用的 params 形状(getattr 协议,与 tool loop params 同名字段)。"""
    return SimpleNamespace(
        task_attributes=getattr(ctx, "task_attributes", None),
        delivery_contract=getattr(ctx, "delivery_contract", None),
        run_id=str(getattr(ctx, "run_id", "") or ""),
        task_id=str(getattr(ctx, "task_id", "") or ""),
        source=str(getattr(ctx, "source", "") or ""),
    )


# ---------------------------------------------------------------- 合成收尾汇总


def _synthesize_closeout_text(agent: object, ctx: Any, shim: SimpleNamespace, staged: tuple) -> str:
    """从结构化记录确定性拼装收尾汇总(零语义生成);全部来源为空时返回 ""(纯聊天
    空响应不编造,交上游空响应重试/报错链处置)。"""
    task_root, collected = staged
    sections: list[str] = []
    sections.extend(_progress_section(agent, ctx, shim))
    sections.extend(_findings_section(task_root))
    sections.extend(_output_section(task_root, collected))
    sections.extend(_closeout_report_section(task_root))
    if not sections:
        sections.extend(_tool_trace_section(ctx))
    if not sections:
        return ""
    status = str(getattr(ctx.final_response, "runtime_status", "ok") or "ok")
    header = [
        "[收尾汇总|系统自检合成]",
        "本轮模型最终回复为空;以下内容由交付保障层从任务的结构化记录中确定性汇总。",
    ]
    if status not in ("", "ok"):
        header.append(f"运行状态: {status}")
    return "\n".join([*header, "", *sections, "", SYNTHESIZED_MARKER])


def _progress_section(agent: object, ctx: Any, shim: SimpleNamespace) -> list[str]:
    root = _progress_root(SimpleNamespace(agent=agent, params=shim))
    run_id = _run_id(SimpleNamespace(agent=agent, params=shim))
    if not root or not run_id:
        return []
    progress = read_task_progress(root, run_id)
    if not isinstance(progress, dict) or not progress.get("items"):
        return []
    summary = task_progress_summary(progress)
    counts = dict(summary.get("counts") or {})
    lines = [f"进度账本: {counts.get('done', 0)}/{counts.get('total', 0)} 项已完成 (open={counts.get('open', 0)})"]
    coverage = dict(summary.get("coverage") or {})
    cov_counts = dict(coverage.get("counts") or {})
    if cov_counts.get("targets_total"):
        lines.append(
            f"覆盖目标: {cov_counts.get('targets_done', 0)}/{cov_counts.get('targets_total', 0)} 个已闭环"
        )
    for item in list(summary.get("recent_done_items") or [])[-_RECENT_DONE_SHOWN:]:
        title = str((item or {}).get("title") or (item or {}).get("id") or "").strip()
        if title:
            lines.append(f"- [done] {title}")
    return lines


def _findings_section(task_root: Path | None) -> list[str]:
    if task_root is None:
        return []
    records, total_hint = _findings_tail_records(_findings_path(task_root))
    claims = [
        claim
        for record in records
        if (claim := str(record.get("claim") or "").strip()[:_CLAIM_PREVIEW_CHARS])
    ][-_FINDINGS_TAIL_COUNT:]
    if not claims:
        return []
    lines = [f"结论账 (findings.jsonl): {total_hint},最近 {len(claims)} 条如下"]
    lines.extend(f"- {claim}" for claim in claims)
    return lines


def _findings_path(task_root: Path) -> Path:
    return task_root / "work" / "shared" / "findings.jsonl"


def _findings_tail_records(path: Path) -> tuple[list[dict[str, Any]], str]:
    """有界读结论账尾部记录:小文件全量计数,大文件只读尾块(如实报账本体量,不硬扫)。"""
    size, raw_lines = _tail_lines(path)
    if size < 0:
        return [], ""
    if size > _FINDINGS_TAIL_READ_BYTES:
        raw_lines = raw_lines[1:]  # 掉头半行
    records = [record for line in raw_lines if isinstance((record := _record_of(line)), dict)]
    if size <= _FINDINGS_COUNT_MAX_BYTES:
        return records, f"共 {len(records)} 条"
    return records, f"账本 {size} 字节"


def _tail_lines(path: Path) -> tuple[int, list[str]]:
    """读文件尾块(超限只读最后一段)。失败返回 (-1, [])。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _FINDINGS_TAIL_READ_BYTES))
            return size, handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return -1, []


def _record_of(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _output_section(task_root: Path | None, collected: list[str]) -> list[str]:
    if task_root is None:
        return []
    files = _output_files(task_root / "output")
    lines: list[str] = []
    if files:
        lines.append(f"交付物 (output/): {len(files)} 个文件")
        lines.extend(f"- output/{rel} ({size} 字节)" for rel, size in files[:_OUTPUT_LIST_CAP])
        if len(files) > _OUTPUT_LIST_CAP:
            lines.append(f"- …另有 {len(files) - _OUTPUT_LIST_CAP} 个文件")
    if collected:
        lines.append(_collected_note(collected))
    return lines


def _output_files(output_dir: Path) -> list[tuple[str, int]]:
    try:
        if not output_dir.is_dir():
            return []
        listed = [_output_file_row(output_dir, item) for item in sorted(output_dir.rglob("*"))]
        return [row for row in listed if row is not None][: _COLLECT_MAX_FILES * 2]
    except OSError:
        return []


def _output_file_row(output_dir: Path, item: Path) -> tuple[str, int] | None:
    if not item.is_file() or _SKIP_DIR_NAMES.intersection(item.relative_to(output_dir).parts):
        return None
    return (str(item.relative_to(output_dir)), item.stat().st_size)


def _closeout_report_section(task_root: Path | None) -> list[str]:
    if task_root is None:
        return []
    path = task_root / ".agent_delivery" / "closeout.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(report, dict):
        return []
    return [f"交付验收: ok={report.get('ok')} mode={report.get('delivery_mode') or ''} (报告: .agent_delivery/closeout.json)"]


def _tool_trace_section(ctx: Any) -> list[str]:
    executed = [str(tool) for tool in list(getattr(ctx, "executed_tools", None) or []) if str(tool)]
    if not executed:
        return []
    distinct = sorted(set(executed))
    return [f"执行痕迹: 本轮共 {len(executed)} 次工具调用 (工具: {', '.join(distinct[:12])})"]


def _collected_note(collected: list[str]) -> str:
    shown = ", ".join(f"output/{rel}" for rel in collected[:8])
    more = f" 等 {len(collected)} 个" if len(collected) > 8 else ""
    return f"[交付归集] 已把声明过的交付物从工作区归集进 output/: {shown}{more}"


# ---------------------------------------------------------------- 归集声明交付物


def _declared_refs(agent: object, ctx: Any, shim: SimpleNamespace) -> list[str]:
    """交付物的【声明性】来源(只认声明过的路径,绝不猜哪个文件像交付物):
    ①delivery contract artifacts;②task_progress 账本的 items/coverage evidence 与
    expected_outputs pattern;③findings 结论账的 evidence_refs。"""
    refs: list[str] = []
    contract = _contract_of(ctx)
    for item in contract.get("artifacts") or []:
        if isinstance(item, dict):
            refs.append(str(item.get("preferred_path") or item.get("path") or ""))
    refs.extend(_progress_declared_refs(agent, shim))
    refs.extend(_findings_evidence_refs(current_run_task_workspace_root(agent, shim)))
    return [text for ref in refs if (text := str(ref or "").strip()) and "://" not in text]


def _contract_of(ctx: Any) -> dict[str, Any]:
    contract = getattr(ctx, "delivery_contract", None)
    if isinstance(contract, dict) and contract:
        return contract
    attrs = getattr(ctx, "task_attributes", None)
    value = attrs.get("delivery_contract") if isinstance(attrs, dict) else None
    return value if isinstance(value, dict) else {}


def _progress_declared_refs(agent: object, shim: SimpleNamespace) -> list[str]:
    root = _progress_root(SimpleNamespace(agent=agent, params=shim))
    run_id = _run_id(SimpleNamespace(agent=agent, params=shim))
    if not root or not run_id:
        return []
    progress = read_task_progress(root, run_id)
    if not isinstance(progress, dict):
        return []
    refs: list[str] = []
    for item in list(progress.get("items") or []) + list((progress.get("coverage") or {}).get("targets") or []):
        if isinstance(item, dict):
            refs.extend(_string_items(item.get("evidence")))
    for entry in progress.get("expected_outputs") or []:
        if isinstance(entry, dict):
            refs.append(str(entry.get("pattern") or ""))
    return refs


def _findings_evidence_refs(task_root: Path | None) -> list[str]:
    if task_root is None:
        return []
    records, _hint = _findings_tail_records(_findings_path(task_root))
    refs: list[str] = []
    for record in records[-200:]:
        refs.extend(_string_items(record.get("evidence_refs")))
    return refs


def _string_items(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _collect_declared_deliverables(refs: list[str], task_root: Path | None) -> list[str]:
    """把声明过、实存于任务区内但不在 output/ 的文件复制进 output/(保结构、防覆盖、
    有界)。返回归集后的 output 相对路径清单。"""
    if task_root is None or not refs:
        return []
    output_dir = (task_root / "output").resolve(strict=False)
    collected: list[str] = []
    seen: set[str] = set()
    sources = (source for ref in refs for source in _resolve_declared_files(ref, task_root, output_dir))
    for source in sources:
        key = str(source)
        if key in seen or len(collected) >= _COLLECT_MAX_FILES:
            continue
        seen.add(key)
        rel = _collect_one(source, task_root, output_dir)
        if rel:
            collected.append(rel)
    return collected


def _resolve_declared_files(ref: str, task_root: Path, output_dir: Path) -> list[Path]:
    """一条声明 → 实存文件列表:绝对/相对路径直接解析;带通配符按 task_root 与 work/
    双锚点 glob(expected_outputs pattern 相对交付目录声明,成果误落工作区时同名寻回)。"""
    if any(char in ref for char in "*?["):
        matches = [path for base in (task_root, task_root / "work") for path in _safe_glob(base, ref)]
        return [path for path in matches[:20] if _collectable(path, task_root, output_dir)]
    candidate = Path(ref).expanduser()
    paths = [candidate] if candidate.is_absolute() else [task_root / ref, task_root / "work" / ref]
    return [path.resolve(strict=False) for path in paths if _collectable(path.resolve(strict=False), task_root, output_dir)]


def _safe_glob(base: Path, pattern: str) -> list[Path]:
    try:
        return list(base.glob(pattern))
    except (OSError, ValueError):
        return []


def _collectable(path: Path, task_root: Path, output_dir: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > _COLLECT_MAX_FILE_BYTES:
            return False
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(task_root) or resolved.is_relative_to(output_dir):
            return False
        return not _SKIP_DIR_NAMES.intersection(resolved.relative_to(task_root).parts)
    except OSError:
        return False


def _collect_one(source: Path, task_root: Path, output_dir: Path) -> str:
    rel_parts = source.resolve(strict=False).relative_to(task_root).parts
    if rel_parts and rel_parts[0] == "work":
        rel_parts = rel_parts[1:]
    if not rel_parts:
        return ""
    dest = output_dir.joinpath(*rel_parts)
    try:
        if dest.exists():
            return ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    except OSError:
        return ""
    return str(Path(*rel_parts))


__all__ = ["SYNTHESIZED_MARKER", "apply_delivery_assurance"]
