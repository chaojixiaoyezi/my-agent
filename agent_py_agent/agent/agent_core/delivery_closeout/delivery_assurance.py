# LLM: 交付保障只处理结构化产物归集，绝不代替模型撰写用户回复。
#   普通聊天、任务回执、进度和最终交付都必须是模型根据结构化事实写出的真实话语；
#   如果模型两次仍给出空白或内部协议，上游会标记 user_reply_unavailable 并抑制展示，
#   不能再用“正在处理”或“系统自检合成”之类固定文字冒充模型回复。
# 模块用途: finalize 前把已由 contract/progress/findings 声明的真实产物归集到
#   output/；不读、不改、不追加 final_response.text。
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...task_progress import read_task_progress
from ..run_task_workspace_writer import current_run_task_workspace_root
from .task_progress_gate import _progress_root, _run_id

_LOGGER = logging.getLogger(__name__)

_COLLECT_MAX_FILES = 100
_COLLECT_MAX_FILE_BYTES = 64 * 1024 * 1024
_FINDINGS_TAIL_READ_BYTES = 256 * 1024
_FINDINGS_COUNT_MAX_BYTES = 1024 * 1024
_SKIP_DIR_NAMES = frozenset({".agent_delivery", "node_modules", "__pycache__", ".git"})
_MAIN_SCOPES = frozenset({"", "default"})


# 函数用途: finalize 前归集声明交付物；任何内部异常都不打断 finalize。
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
    _collect_declared_deliverables(_declared_refs(agent, ctx, shim), task_root)
    return ctx


def _params_shim(ctx: Any) -> SimpleNamespace:
    """workspace/账本解析用的 params 形状(getattr 协议,与 tool loop params 同名字段)。"""
    return SimpleNamespace(
        task_attributes=getattr(ctx, "task_attributes", None),
        delivery_contract=getattr(ctx, "delivery_contract", None),
        run_id=str(getattr(ctx, "run_id", "") or ""),
        task_id=str(getattr(ctx, "task_id", "") or ""),
        source=str(getattr(ctx, "source", "") or ""),
    )


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


__all__ = ["apply_delivery_assurance"]
