# LLM: 子代理"产物落点"与"最终交付意图"的对齐投影层，属于 subagents/services。
#   契约：纯函数、不修改 task。默认落点是**任务交付区**（用户主目录下
#   tasks/<日期>/<任务>/output/，即 delivery_root），用户/主代理显式指定别的目录时用
#   指定的；子代理直接写交付区，用户拿走即可，不再经"子代理家→搬运"两段式。
#   消费方：context_bundle_contracts（执行合同把目标 refs 翻译成交付区落点 +
#   把交付区授权进 allowed_write_roots）、runner_context_service（写边界授权）、
#   result_artifact_evidence（仅兜底搬运任务目录外的声明）、subagent_aggregation
#   （声明对账复用 looks_like_output_path）。改动时同步检查
#   tests/test_subagent_output_alignment.py 与 docs/audits/R4-goattack-20260611.md。
# 模块用途: 修 R4 根因——主代理派工声明的 output_files 通常已指向任务 output/，但
#   子代理写权限不含该目录、全部写入被拒、任务死锁。这里把声明产物统一锚定到任务
#   交付区并授权子代理写入；只有声明落在任务目录之外时才记 delivery_map 走兜底搬运。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...model_visible_refs import has_placeholder_path_segment

OUTPUT_TARGET_LOCKED_CODE = "OUTPUT_TARGET_LOCKED"
DEFAULT_DELIVERY_SUBDIR = "output"
_DECLARED_FIELDS = ("output_files", "output_refs")
# 与 context_bundle_contracts._SAFE_FILE_SUFFIX_RE 保持一致（导入会成环，正则单独镜像）
_SAFE_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


@dataclass(frozen=True)
class OutputAnchoring:
    """声明产物 → 任务交付区落点的对齐结果（投影，不落盘）。"""

    anchored_refs: list[str] = field(default_factory=list)
    delivery_map: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


# LLM: 任务交付区的权威解析。优先级：① attributes.run_workspace.output_dir
#   （主代理/用户显式指定的交付目录）；② task_workspace_dir/output（默认，即用户
#   主目录下 tasks/<日期>/<任务>/output）；③ 兜底 task.output_dir（极端缺字段时）。
#   不创建目录、不落盘。
# 函数用途: 算出子代理产物该交付到哪个目录（用户拿走的东西）。
def delivery_root(task: Any) -> str:
    explicit = _attr_run_workspace_output_dir(task)
    if explicit:
        return explicit
    workspace = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if workspace:
        return str(Path(workspace) / DEFAULT_DELIVERY_SUBDIR)
    return str(getattr(task, "output_dir", "") or "").strip()


# 函数用途: 读 attributes.run_workspace.output_dir（主代理透传的显式交付目录）。
def _attr_run_workspace_output_dir(task: Any) -> str:
    attrs = getattr(task, "attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    run_workspace = attrs.get("run_workspace")
    if isinstance(run_workspace, dict):
        return str(run_workspace.get("output_dir") or "").strip()
    return ""


# LLM: 子代理为了写交付产物需要被授权的目录（写边界 + 模型可见 allowed_write_roots
#   都要并入这个）。授权交付区根即覆盖其下所有声明子路径。空交付区返回空。
# 函数用途: 算出该授权给子代理写的交付区目录。
def output_write_grant_roots(task: Any) -> list[str]:
    root = delivery_root(task)
    return [root] if root else []


# LLM: 声明产物目录的写授权（R8 接力实锤修复:delivery_root 依赖 run_workspace/
#   task_workspace_dir 等环境字段,创建链没透传它们时为空——子代理写 output_files
#   声明的位置被系统 WRITE_FORBIDDEN,R8a 实测 17 次拒、需 capreq 救场。声明驱动
#   的对偶:声明了产物要交到哪,哪里的父目录就该可写）。围栏是客观事实硬门:
#   声明目录必须位于 fence_roots（任务工作区/task_dir/manager workspace 根,与
#   capability grant 的 _safe_grant_roots 同款基准）任一之内,围栏外的声明不并入
#   （防声明任意系统路径自我扩权,仍走 capreq 流程）。纯函数零副作用。
# 函数用途: 把"声明产物所在目录"算成可授权的写根,孤立环境下交付不再被自家边界拦。
def declared_output_write_roots(task: Any, fence_roots: list[str]) -> list[str]:
    fences = _resolved_roots([root for root in fence_roots if str(root or "").strip()])
    if not fences:
        return []
    roots: list[str] = []
    for ref, _fieldname in _declared_path_refs(task):
        candidate = Path(ref).expanduser()
        if not candidate.is_absolute():
            continue
        parent = candidate.resolve(strict=False).parent
        if not any(parent == fence or fence in parent.parents for fence in fences):
            continue
        text = str(parent)
        if text not in roots:
            roots.append(text)
    return roots


# LLM: 给声明产物（output_files/output_refs）算交付区落点。已在交付区/已授权目录内的
#   绝对路径原样保留（R4 主形态：声明本就指向 output/，授权后直接可写）；其余锚到
#   交付区，delivery_map 只记真正被重定位（落点≠声明）的条目，供兜底搬运。纯函数。
# 函数用途: 计算子代理声明产物的交付落点 + 需要搬运的映射 + 锁冲突警告。
def anchored_output_refs(task: Any) -> OutputAnchoring:
    root = delivery_root(task)
    declared = _declared_path_refs(task)
    if not declared or not root:
        return OutputAnchoring(anchored_refs=[ref for ref, _ in declared])
    write_roots = _resolved_roots([root, *(getattr(task, "allowed_write_roots", None) or [])])
    locked_paths = _resolved_roots(getattr(task, "locked_files", None) or [])
    anchored_refs: list[str] = []
    delivery_map: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for ref, fieldname in declared:
        anchored, reason = _anchored_ref(task, ref, root, write_roots)
        if anchored not in anchored_refs:
            anchored_refs.append(anchored)
        # delivery_map 只记"被真正重定位"的声明（落在交付区外、需要兜底搬回意图位置）；
        # 相对声明锚到交付区、或已指向交付区的绝对声明，落点即意图，不需搬运。
        if reason.startswith("relocated"):
            delivery_map.append({"field": fieldname, "from": anchored, "to": ref, "reason": reason})
        _append_locked_warning(warnings, fieldname, anchored, locked_paths)
    return OutputAnchoring(anchored_refs=anchored_refs, delivery_map=delivery_map, warnings=warnings)


# LLM: 给合同投影层用的批量锚定：把任意 ref 列表（required_file_refs 等）逐条翻译成
#   交付区落点，顺序与去重保持稳定。规则与 anchored_output_refs 同一权威。
# 函数用途: 执行合同里的文件 refs 统一走这里翻译，保证子代理只被指示写交付区。
def anchor_refs_for_execution(task: Any, refs: list[str]) -> list[str]:
    root = delivery_root(task)
    if not root:
        return [str(ref) for ref in refs]
    write_roots = _resolved_roots([root, *(getattr(task, "allowed_write_roots", None) or [])])
    anchored_list: list[str] = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text or not looks_like_output_path(text):
            anchored = text
        else:
            anchored, _ = _anchored_ref(task, text, root, write_roots)
        if anchored and anchored not in anchored_list:
            anchored_list.append(anchored)
    return anchored_list


# LLM: 单条锚定裁决；返回 (落点, reason)。已在交付区或已授权写根内的绝对路径原样
#   保留；其余锚到交付区。相对路径直接锚到交付区下保结构，../ 逃逸回退 basename。
# 函数用途: 算一条声明产物的实际交付落点。
def _anchored_ref(task: Any, ref: str, root: str, write_roots: list[Path]) -> tuple[str, str]:
    path = Path(ref.replace("\\", "/"))
    delivery = Path(root)
    if path.is_absolute():
        if _inside_any(path, write_roots):
            return ref, "inside_writable_root"
        return _relocate_absolute(task, path, delivery)
    candidate = delivery / path
    if _resolves_inside(candidate, delivery):
        return str(candidate), "relative_to_delivery_root"
    return str(delivery / path.name), "relative_escape_to_basename"


# LLM: 交付区/授权区之外的绝对声明重定位规则：任务工作区内的取相对任务根尾部锚到
#   交付区（保结构，去掉首段重复的交付子目录名避免 output/output）；完全外部的去掉
#   根锚点保结构。这些都会进 delivery_map，由兜底搬运处理。
# 函数用途: 把交付区外的绝对声明翻译到交付区下。
def _relocate_absolute(task: Any, path: Path, delivery: Path) -> tuple[str, str]:
    workspace = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if workspace:
        try:
            tail = path.resolve(strict=False).relative_to(Path(workspace).resolve(strict=False))
            return str(delivery / _strip_leading_segment(tail, delivery.name)), "relocated_in_task_workspace"
        except (OSError, RuntimeError, ValueError):
            pass
    tail_parts = path.parts[1:] if len(path.parts) > 1 else (path.name,)
    return str(delivery.joinpath(*tail_parts)), "relocated_external"


# 函数用途: 去掉相对路径首段若与交付子目录同名（防止 output/output 重叠）。
def _strip_leading_segment(tail: Path, segment: str) -> Path:
    parts = tail.parts
    if parts and parts[0] == segment and len(parts) > 1:
        return Path(*parts[1:])
    return tail


# LLM: 锁矛盾只记结构化 warning 不硬拦：交付落点仍被 locked_files 盖住属于派工矛盾
#   的客观事实，必须暴露给模型与 watch 报告，不允许静默。
# 函数用途: 落点被锁时追加 OUTPUT_TARGET_LOCKED 警告（去重）。
def _append_locked_warning(
    warnings: list[dict[str, str]],
    fieldname: str,
    anchored: str,
    locked_paths: list[Path],
) -> None:
    if not locked_paths:
        return
    target = Path(anchored).expanduser().resolve(strict=False)
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


# 函数用途: 判断 path 是否落在任一根内（含等于根）。
def _inside_any(path: Path, roots: list[Path]) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return any(resolved == root or _is_relative_to(resolved, root) for root in roots)


# 函数用途: 判断候选路径 resolve 后是否仍在 root 内（拦 ../ 逃逸）。
def _resolves_inside(candidate: Path, root: Path) -> bool:
    return _is_relative_to(candidate.resolve(strict=False), root.resolve(strict=False))


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
#   delivery_root)不得出现在它自己的 locked_files——这是派工矛盾的客观事实,无论
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


# 函数用途: 收集本任务自己的交付目标(声明锚定落点 + 交付区根)的已解析路径集合。
def _self_delivery_targets(task: Any) -> list[Path]:
    targets = [Path(ref).expanduser().resolve(strict=False) for ref in anchored_output_refs(task).anchored_refs]
    root = delivery_root(task)
    if root:
        targets.append(Path(root).expanduser().resolve(strict=False))
    return targets


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
    "DEFAULT_DELIVERY_SUBDIR",
    "LOCKED_FILES_CHANGES_ATTR",
    "LOCKED_FILES_SANITIZED_ATTR",
    "OUTPUT_TARGET_LOCKED_CODE",
    "OutputAnchoring",
    "anchor_refs_for_execution",
    "anchored_output_refs",
    "delivery_root",
    "looks_like_output_path",
    "output_write_grant_roots",
    "record_locked_files_change",
    "sanitize_self_locked_delivery_targets",
]
