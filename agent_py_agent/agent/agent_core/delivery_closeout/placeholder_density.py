"""交付占位密度结构闸(P3):收尾时统计交付代码里的占位记号密度,明显过高打回一次。

真机形态(难点大工程,同任务同模型):u-code-a 1.5 万行只 64 处占位(≈4/KLOC,基本真实现);
u-code-b 1.25 万行 421 处(≈34/KLOC,大量 TODO/空壳)——尽管任务明写"别用占位"。提示词拦不住
生成层方差,这里加一道【纯结构计数】的质检门:

- 判据零语义(铁律):只数字面占位记号。`TODO`/`FIXME` 只认【注释语境】(记号前有注释符)——
  防 todo 应用的标识符/界面文案被误算("todoList"/"TODO List" 标题不算,`# TODO` 才算);
  中文占位词(此处省略/待实现/待补充/占位)任意位置计数。
- 宽松双阈值(别误伤合理 TODO):总数 >= 50 【且】密度 >= 12/KLOC 才打回——u-code-b(34/KLOC)
  拦住,u-code-a(4/KLOC)与正常工程注释远够不着;小交付(几处 TODO)被绝对阈值天然放过。
- R9-safe:幂等一次(marker 进 tool_context,二次同形态放行)+ 双出口(把占位补成真实现,
  或确认合理就如实说明/清掉死占位再交)——绝不死锁,不设"必须 N 行"式硬规则。
- 只扫【代码扩展名】文件(报告/文档里的 TODO 章节是合法散文,不沾);文件数/字节有上限,
  失败永不抛错、绝不影响收尾。根子是模型生成质量,这道闸只拦"密度明显过高",不指望清零。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

_PLACEHOLDER_DENSITY_MARKER = "[placeholder-density-rework]"
_MIN_TOTAL_MARKERS = 50
_MIN_DENSITY_PER_KLOC = 12.0
_MAX_FILES = 400
_MAX_TOTAL_BYTES = 8_000_000
_TOP_FILES_LISTED = 8

_CODE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html", ".java", ".js", ".jsx",
        ".kt", ".mjs", ".cjs", ".php", ".py", ".rb", ".rs", ".scala", ".sh", ".sql", ".swift",
        ".ts", ".tsx", ".vue", ".svelte",
    }
)
# TODO/FIXME 的注释语境:记号之前出现过注释符(#、//、/*、<!--、行首 * 续行、;、--)。
_COMMENT_PREFIX_RE = re.compile(r"(#|//|/\*|<!--|^\s*\*|;|--)")
_TODO_RE = re.compile(r"\b(?:TODO|FIXME)\b")
_ZH_MARKERS = ("此处省略", "待实现", "待补充", "占位")


def placeholder_density_rework(request: object, report: dict[str, Any]) -> bool:
    """占位密度闸入口:命中则把结构化打回指令写进 tool_context 并返回 True。永不抛错。"""
    try:
        return _rework(request, report)
    except Exception:  # noqa: BLE001 - 质检闸是增强,失败绝不影响收尾
        logging.getLogger(__name__).warning("placeholder density rework failed", exc_info=True)
        return False


def _rework(request: object, report: dict[str, Any]) -> bool:
    params = getattr(request, "params", None)
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or any(_PLACEHOLDER_DENSITY_MARKER in str(item) for item in context):
        return False  # 无处投递 / 一次性额度已花(幂等,二次同形态放行)
    stats = _delivery_placeholder_stats(_delivery_code_files(params, report))
    if stats["markers"] < _MIN_TOTAL_MARKERS or stats["density_per_kloc"] < _MIN_DENSITY_PER_KLOC:
        return False
    payload = {
        "placeholder_markers": stats["markers"],
        "code_lines": stats["lines"],
        "density_per_kloc": stats["density_per_kloc"],
        "files_scanned": stats["files"],
        "top_files": stats["top_files"],
        "instruction": (
            "交付代码里的占位记号密度明显过高(注释语境 TODO/FIXME + 此处省略/待实现/待补充/占位,"
            "纯字面计数,见 top_files)。这不符合『交付即可用』:二选一后再提交——"
            "①把占位/空壳补成真实现(优先补 top_files 里最密的);"
            "②确认某些 TODO 合理保留,就清掉死占位、并在最终交付说明里如实写明剩余 TODO 及原因。"
            "只此一次提醒,不会反复打回;但别把占位当交付。"
        ),
    }
    context.append(_PLACEHOLDER_DENSITY_MARKER + "\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return True


def _delivery_code_files(params: object, report: dict[str, Any]) -> list[Path]:
    """交付代码文件集:任务交付目录(output/)递归 + 报告 ok 产物,只留代码扩展名,去重限量。"""
    files: list[Path] = []
    seen: set[str] = set()
    for path in _candidate_paths(params, report):
        if len(files) >= _MAX_FILES:
            break
        key = str(path)
        if key in seen or path.suffix.lower() not in _CODE_SUFFIXES or not path.is_file():
            continue
        seen.add(key)
        files.append(path)
    return files


def _candidate_paths(params: object, report: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    output_dir = _task_output_dir(params)
    if output_dir is not None and output_dir.is_dir():
        paths.extend(sorted(item for item in output_dir.rglob("*") if item.is_file())[: _MAX_FILES * 2])
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    for item in artifacts if isinstance(artifacts, list) else []:
        if isinstance(item, dict) and item.get("ok") is True and str(item.get("path") or "").strip():
            paths.append(Path(str(item["path"])).expanduser())
    return paths


def _task_output_dir(params: object) -> Path | None:
    attrs = getattr(params, "task_attributes", None)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    if text:
        return Path(text).expanduser().resolve(strict=False)
    root = str(workspace.get("task_root") or "").strip()
    return (Path(root).expanduser().resolve(strict=False) / "output") if root else None


def _delivery_placeholder_stats(files: list[Path]) -> dict[str, Any]:
    total_markers = 0
    total_lines = 0
    budget = _MAX_TOTAL_BYTES
    per_file: list[tuple[str, int]] = []
    for path in files:
        text = _read_capped(path, budget)
        budget -= len(text)
        if not text:
            continue
        lines = text.splitlines()
        count = sum(_line_marker_count(line) for line in lines)
        total_lines += len(lines)
        total_markers += count
        if count:
            per_file.append((str(path), count))
        if budget <= 0:
            break
    density = (total_markers * 1000.0 / total_lines) if total_lines else 0.0
    per_file.sort(key=lambda item: -item[1])
    return {
        "markers": total_markers,
        "lines": total_lines,
        "density_per_kloc": round(density, 1),
        "files": len(files),
        "top_files": [{"path": path, "markers": count} for path, count in per_file[:_TOP_FILES_LISTED]],
    }


def _read_capped(path: Path, budget: int) -> str:
    if budget <= 0:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:budget]
    except OSError:
        return ""


def _line_marker_count(line: str) -> int:
    count = sum(line.count(marker) for marker in _ZH_MARKERS)
    for match in _TODO_RE.finditer(line):
        if _COMMENT_PREFIX_RE.search(line[: match.start()]):
            count += 1
    return count


__all__ = ["placeholder_density_rework"]
