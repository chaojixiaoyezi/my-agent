# LLM: 输出引用与工具共用可信 cwd；只解析路径，不重定位、不复制、不扩展权限。
# 模块用途: 生成子代理真实输出引用及任务锁审计，内部运行目录不是业务交付区。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...model_visible_refs import has_placeholder_path_segment
from ..context_bundle_refs import execution_cwd

OUTPUT_TARGET_LOCKED_CODE = "OUTPUT_TARGET_LOCKED"
_DECLARED_FIELDS = ("output_files", "output_refs")
# 与 context_bundle_contracts._SAFE_FILE_SUFFIX_RE 保持一致（导入会成环，正则单独镜像）
_SAFE_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


# LLM: 只读的真实输出路径和冲突信息，不包含搬运计划或额外授权。
# 类用途: 供子代理文件合同与结果交接共用的路径投影。
@dataclass(frozen=True)
class OutputAnchoring:
    """声明产物的真实路径和冲突警告（只读投影）。"""

    anchored_refs: list[str] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


# LLM: 输出声明不产生权限，继承 cwd 与绝对目标必须和文件工具相同。
# 函数用途: 解析显式产物引用，并保留任务锁冲突警告；不创建目录或搬运文件。
def anchored_output_refs(task: Any) -> OutputAnchoring:
    locked_paths = _resolved_roots(getattr(task, "locked_files", None) or [])
    refs: list[str] = []
    warnings: list[dict[str, str]] = []
    for ref, fieldname in _declared_path_refs(task):
        target = anchor_refs_for_execution(task, [ref])[0]
        if target not in refs:
            refs.append(target)
        _append_locked_warning(warnings, fieldname, target, locked_paths)
    return OutputAnchoring(anchored_refs=refs, warnings=warnings)


# LLM: 不从 run/output、权限根列表或宿主进程 cwd 猜目标；父目录引用保留真实目标，由工具判断权限。
# 函数用途: 相对路径基于可信 cwd，绝对路径不变；缺少 cwd 时保留原引用，不静默写到其它地方。
def anchor_refs_for_execution(task: Any, refs: list[str]) -> list[str]:
    cwd = execution_cwd(task)
    results: list[str] = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text:
            continue
        if looks_like_output_path(text):
            path = Path(text).expanduser()
            if not path.is_absolute() and cwd:
                text = str((Path(cwd) / path).resolve(strict=False))
        if text not in results:
            results.append(text)
    return results


# LLM: 锁矛盾只记结构化 warning 不硬拦：交付落点仍被 locked_files 盖住属于派工矛盾
#   的客观事实，必须暴露给模型与 watch 报告；缺 cwd 的相对引用不猜宿主地址。
# 函数用途: 落点被锁时追加 OUTPUT_TARGET_LOCKED 警告（去重）。
def _append_locked_warning(
    warnings: list[dict[str, str]],
    fieldname: str,
    anchored: str,
    locked_paths: list[Path],
) -> None:
    if not locked_paths:
        return
    target = Path(anchored).expanduser()
    if not target.is_absolute():
        return
    target = target.resolve(strict=False)
    if not any(target == lp or _is_relative_to(target, lp) for lp in locked_paths):
        return
    if any(w.get("ref") == anchored and w.get("code") == OUTPUT_TARGET_LOCKED_CODE for w in warnings):
        return
    warnings.append(
        {
            "code": OUTPUT_TARGET_LOCKED_CODE,
            "field": fieldname,
            "ref": anchored,
            "message_zh": "声明产物落点被 locked_files 锁定，子代理无法写入；请解锁或改派产物路径。",
        }
    )


# LLM: 过滤规则与 context_bundle_contracts._looks_like_output_path 同语义：
#   只锚"路径形态"的声明；逻辑名（source_file 这类无斜杠无后缀的标识）、URL、
#   占位路径段（[任务目录] 等）一律不进锚定，避免把非路径声明翻译成假文件目标。
# 函数用途: 取 attributes 里路径形态的声明产物。
def _declared_path_refs(task: Any) -> list[tuple[str, str]]:
    attrs = getattr(task, "attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    refs: list[tuple[str, str]] = []
    for fieldname in _DECLARED_FIELDS:
        refs.extend((text, fieldname) for text in _path_texts(attrs.get(fieldname)))
    return refs


# 函数用途: 滤出一个声明字段里路径形态的字符串条目。
def _path_texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if text and looks_like_output_path(text):
            texts.append(text)
    return texts


# 函数用途: 判定一条声明是不是文件路径形态（绝对路径 / 含目录分隔 / 带合法文件后缀）。
def looks_like_output_path(value: str) -> bool:
    text = value.replace("\\", "/")
    if "://" in text or has_placeholder_path_segment(text):
        return False
    path = Path(text)
    has_safe_suffix = bool(path.name and _SAFE_FILE_SUFFIX_RE.fullmatch(path.suffix.lower()))
    return path.is_absolute() or "/" in text or has_safe_suffix


# 函数用途: 把可写根字符串列表解析成绝对 Path 列表。
def _resolved_roots(raw_roots: list[object]) -> list[Path]:
    roots: list[Path] = []
    for raw in raw_roots:
        text = str(raw or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve(strict=False))
    return roots


# 函数用途: Path.relative_to 的布尔包装。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# 自锁剔除账本在 attributes 里的键名与容量上限(防失控循环撑爆 attributes)。
LOCKED_FILES_SANITIZED_ATTR = "locked_files_sanitized"
LOCKED_FILES_CHANGES_ATTR = "locked_files_changes"
_LOCK_LEDGER_MAX_ENTRIES = 20


# LLM: 任务完成力底座 P3-1 的客观矛盾门(R5a 实锤:13 子代理 23 次 WRITE_FORBIDDEN,
#   锁住的正是各自的声明交付目标)。规则:task 自己的声明交付目标(anchored refs 与
#   cwd 解析结果)不得出现在它自己的 locked_files——这是派工矛盾的客观事实,无论
#   锁来自模型参数、takeover 透传还是 save 合并,一律剔除并结构化留痕
#   (attributes.locked_files_sanitized)。锁条目为目录且覆盖目标时同样剔除。
#   兄弟任务目标的锁不在此处处理(可能是合法防冲突)。本函数由 persistence save
#   唯一权威口调用,改动时同步 tests/test_subagent_lock_lifecycle.py。
# 函数用途: 防止"派给你的活,又把你要交的文件锁死"——自锁矛盾在落盘前被消掉。
def sanitize_self_locked_delivery_targets(task: Any, now: float) -> list[str]:
    locked = [str(item or "").strip() for item in (getattr(task, "locked_files", None) or []) if str(item or "").strip()]
    if not locked:
        return []
    targets = _self_delivery_targets(task)
    if not targets:
        return []
    kept, removed = _split_self_locked(locked, targets)
    if not removed:
        return []
    task.locked_files = kept
    _record_sanitized_locks(task, removed, now)
    return removed


# LLM: 自锁纠错仅覆盖已知绝对目标；缺少 cwd 时不得用宿主进程目录推导目标或剔除锁。
# 函数用途: 读取本任务的精确输出路径，供现有任务锁审计使用。
def _self_delivery_targets(task: Any) -> list[Path]:
    paths = [Path(ref).expanduser() for ref in anchored_output_refs(task).anchored_refs]
    return [path.resolve(strict=False) for path in paths if path.is_absolute()]


# 函数用途: 把锁清单分成"保留"与"命中自身目标须剔除"两份(锁覆盖目标即命中)。
def _split_self_locked(locked: list[str], targets: list[Path]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    removed: list[str] = []
    for item in locked:
        lock_path = Path(item).expanduser().resolve(strict=False)
        covers = any(target == lock_path or _is_relative_to(target, lock_path) for target in targets)
        (removed if covers else kept).append(item)
    return kept, removed


# LLM: 剔除留痕(P3-1 锁账本):结构化记录被剔条目与原因,环形上限防膨胀。
# 函数用途: 把"哪些自锁被系统剔掉了"写进任务属性,供报告与排查。
def _record_sanitized_locks(task: Any, removed: list[str], now: float) -> None:
    attrs = task.attributes if isinstance(getattr(task, "attributes", None), dict) else {}
    entries = attrs.get(LOCKED_FILES_SANITIZED_ATTR)
    entries = entries if isinstance(entries, list) else []
    entries.append(
        {
            "code": OUTPUT_TARGET_LOCKED_CODE,
            "removed": removed[:10],
            "removed_count": len(removed),
            "reason_zh": "锁条目覆盖本任务自己的声明交付目标(派工矛盾),已在落盘前剔除。",
            "at": now,
        }
    )
    attrs[LOCKED_FILES_SANITIZED_ATTR] = entries[-_LOCK_LEDGER_MAX_ENTRIES:]
    task.attributes = attrs


# LLM: P3-1 锁变更账本:对比上一份落盘状态与本次的 locked_files,增删都记
#   (谁在什么时候改锁,从此有据可查;R5a"运行中途被加锁、事后被清"的取证盲区
#   就是缺这本账)。环形上限,纯 attributes 记录,不做门。
# 函数用途: 给锁清单的每次变化记一笔流水账。
def record_locked_files_change(task: Any, previous: list[str] | None, now: float) -> None:
    before = sorted({str(item or "").strip() for item in (previous or []) if str(item or "").strip()})
    after = sorted({str(item or "").strip() for item in (getattr(task, "locked_files", None) or []) if str(item or "").strip()})
    added = [item for item in after if item not in before]
    removed = [item for item in before if item not in after]
    if not added and not removed:
        return
    attrs = task.attributes if isinstance(getattr(task, "attributes", None), dict) else {}
    entries = attrs.get(LOCKED_FILES_CHANGES_ATTR)
    entries = entries if isinstance(entries, list) else []
    entries.append({"added": added[:10], "removed": removed[:10], "at": now})
    attrs[LOCKED_FILES_CHANGES_ATTR] = entries[-_LOCK_LEDGER_MAX_ENTRIES:]
    task.attributes = attrs


__all__ = [
    "LOCKED_FILES_CHANGES_ATTR",
    "LOCKED_FILES_SANITIZED_ATTR",
    "OUTPUT_TARGET_LOCKED_CODE",
    "OutputAnchoring",
    "anchor_refs_for_execution",
    "anchored_output_refs",
    "looks_like_output_path",
    "record_locked_files_change",
    "sanitize_self_locked_delivery_targets",
]
