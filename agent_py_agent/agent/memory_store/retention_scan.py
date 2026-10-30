# LLM: 本模块只读类型化策略、会话、任务及子代理状态和文件事实，不执行删除；来源或状态未知时必须保守保护并报告错误。
# 模块用途: 为每个 owner 生成可在执行前重验证的留存 dry-run 计划，统一保护引用与恢复材料。
from __future__ import annotations

"""Memory v2 retention 的只读扫描与计划生成。"""

import hashlib
import io
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report, read_jsonl_objects_report
from ..conversation.models import ConversationThread
from ..conversation.workspace_paths import canonical_task_root, durable_work_root
from .candidate_models import MemoryCandidate
from .candidates import (
    RETENTION_ELIGIBLE_CANDIDATE_STATUSES,
    CandidateService,
    CandidateStoreCorruptError,
    _hydrate_candidate,
    _is_binding_tombstone,
    candidate_retention_eligible,
)
from .curator_run_log import _CURSOR_CHECKPOINT, _CURSOR_DELTA
from .reference_pins import target_pin_read, target_pin_read_enabled, watch_pin_source
from .retention_models import (
    TASK_TERMINAL_STATUSES,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionPolicy,
    MemoryRetentionReport,
    retention_action_id,
)

# 一天按 86400 秒算：保留期换算成秒的统一口径。
_DAY_SECONDS = 86_400
_SUBAGENT_SCRATCH_PATHS = (
    Path("inbox"),
    Path("outbox"),
    Path("compactions"),
    Path("artifacts") / "tool_outputs",
)
# 口径与 tool_output_days_after_terminal / trash_days 一致；用户嫌参数多，所以写代码常量、不新增配置项。
# 快照与工具清单的保留期：30 天（诊断和人工排查够用）。
_CONTEXT_BUNDLE_SNAPSHOT_DAYS = 30
# 日期目录里的 latest 副本永远不动：它们是"最近一份快照"的入口，删了会让排查没有起点。
_CONTEXT_BUNDLE_LATEST_PREFIX = "latest_context_bundle"


# LLM: plan纯读取；发现源不可枚举须结构化失败，策略摘要与所有动作经过统一指纹门。
# 函数用途: 扫描完整会话、审计、Daily、工具大输出、候选、Curator、Compact 和终态任务。
def build_retention_plan(
    *,
    home: object,
    candidates: CandidateService,
    conversation_roots: tuple[Path, ...] = (),
    now: datetime | None = None,
) -> MemoryRetentionReport:
    current = _normalize_now(now)
    policy_path = Path(home.owner_retention_json)
    policy, policy_error = _load_policy(policy_path)
    fingerprint = file_fingerprint(policy_path)
    if not valid_fingerprint(fingerprint) and policy_error is None:
        policy_error = _state_error("MEMORY_RETENTION_POLICY_UNREADABLE", policy_path)
    if policy_error is not None or policy is None:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            errors=((policy_error,) if policy_error is not None else ()),
            policy_fingerprint=fingerprint,
        )
    if policy.legal_hold:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            legal_hold=True,
            policy_fingerprint=fingerprint,
        )
    try:
        roots = _conversation_roots(home, conversation_roots)
    except OSError as exc:
        return MemoryRetentionReport(applied=False, actions=(), policy_fingerprint=fingerprint,
            errors=(MemoryRetentionError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE",
                    str(getattr(exc, "filename", "") or ""), type(exc).__name__),))
    context = _RetentionScanContext(
        home=home,
        candidates=candidates,
        policy=policy,
        conversation_roots=roots,
        now=current,
    )
    actions = _validated_actions(_collect_retention_actions(context), context.errors)
    unique = {action.action_id: action for action in actions}
    ordered = tuple(sorted(unique.values(), key=lambda item: (item.category, str(item.path))))
    return MemoryRetentionReport(
        applied=False,
        actions=ordered,
        errors=tuple(context.errors),
        policy_fingerprint=fingerprint,
    )


# LLM: 一次扫描上下文为所有类别共享同一类型化策略与关闭式错误集合，不能各自放宽失败规则。
# 类用途: 汇总一次只读 retention 计划所需的 owner、策略、会话根和扫描时间。
@dataclass
class _RetentionScanContext:
    home: object
    candidates: CandidateService
    policy: MemoryRetentionPolicy
    conversation_roots: tuple[Path, ...]
    now: datetime
    errors: list[MemoryRetentionError] = field(default_factory=list)

    # LLM: 法律保留任务 ID 从已解析策略转成统一集合，供所有扫描器按精确身份复用。
    # 函数用途: 返回禁止清理的任务编号集合。
    @property
    def held_task_ids(self) -> frozenset[str]:
        return frozenset(self.policy.legal_hold_task_ids)


# LLM: 所有类别共用一次严格pin构建及其错误集合；目标分域仍沿原完整性门，不新增清理权威。
# 函数用途: 按任务保护关系组合会话、候选、运行记录、回收站与过期快照/工具清单的清理动作。
def _collect_retention_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    task_actions, selected_task_roots = _task_actions(
        home=context.home,
        policy=context.policy,
        held_task_ids=context.held_task_ids,
        now=context.now,
        errors=context.errors,
    )
    pin_errors: list[MemoryRetentionError] = []
    protection = current_retention_pins(context.home, context.conversation_roots, pin_errors, now=context.now)
    actions = list(task_actions)
    actions.extend(_task_tmp_actions(context, selected_task_roots, protection))
    actions.extend(_run_scratch_actions(context, selected_task_roots, protection))
    actions.extend(_protected_runtime_actions(context, selected_task_roots))
    # 保留原类别错误顺序：任务权威错误在前，统一来源错误只发表一次。
    context.errors.extend(pin_errors)
    actions.extend(_aged_file_actions(context, selected_task_roots, protection))
    actions.extend(_context_bundle_actions(context))
    actions.extend(
        _trash_actions(
            home=context.home,
            days=context.policy.trash_days,
            now=context.now,
            errors=context.errors,
            conversation_roots=context.conversation_roots,
            protection=protection,
        )
    )
    return actions


# LLM: 依赖任务或会话权威的运行产物共用同一法律保留和终态输入，不能单独按文件年龄授权清理。
# 函数用途: 汇总工具正文、子代理临时目录、候选和完整会话动作。
def _protected_runtime_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
) -> list[MemoryRetentionAction]:
    common = {
        "home": context.home,
        "policy": context.policy,
        "held_task_ids": context.held_task_ids,
        "selected_task_roots": selected_task_roots,
        "now": context.now,
        "errors": context.errors,
    }
    actions = _tool_output_actions(**common)
    actions.extend(_subagent_scratch_actions(**common))
    actions.extend(
        _candidate_actions(
            candidates=context.candidates,
            policy=context.policy,
            now=context.now,
            errors=context.errors,
        )
    )
    actions.extend(
        _conversation_actions(
            home=context.home,
            policy=context.policy,
            held_task_ids=context.held_task_ids,
            roots=context.conversation_roots,
            now=context.now,
            errors=context.errors,
        )
    )
    return actions


# LLM: 消费同次规划的严格pin快照；引用来源读全才规划Daily/Compact/Curator，保留原回收合同。
# 函数用途: 汇总按龄动作并保护引用；安全审计按记录计龄，原始audit继续禁止删除。
def _aged_file_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
    protection: tuple[_RetentionPins, bool],
) -> list[MemoryRetentionAction]:
    home = context.home
    policy = context.policy
    pins, complete = protection
    categories = (
        # LLM: audit 类别本次一律跳过。类别名与实际数据不符——O/audit/*.jsonl 是主代理每轮的记忆归档
        #   原始事件，Curator 要读，不是审计日志；直接删不可恢复。等存储登记表定了语义再处理
        #   （dev 2026-09-28 裁定）。
        # ("audit", Path(home.owner_audit_dir), policy.audit_days, "*.jsonl"),
        ("daily", Path(home.owner_memory_daily_dir), policy.daily_days, "*.jsonl"),
        ("curator_run", Path(home.owner_memory_curator_runs_dir), policy.curator_run_days, "*.jsonl"),
        ("compact", Path(home.owner_compact_dir), policy.compact_days, "*"),
        ("cache", Path(home.owner_cache_dir), policy.cache_days, "*"),
        ("tmp", Path(home.owner_tmp_dir), policy.tmp_days, "*"),
    )
    actions: list[MemoryRetentionAction] = []
    for category, root, days, pattern in categories:
        if category in {"daily", "compact", "curator_run"} and not complete:
            continue
        actions.extend(
            _expired_file_actions(
                category=category,
                root=root,
                days=days,
                pattern=pattern,
                now=context.now,
                excluded_roots=selected_task_roots,
                errors=context.errors,
                protection=(pins, home) if category in {"daily", "compact", "curator_run"} else None,
            )
        )
    actions.extend(_security_audit_actions(home, policy.security_audit_days, context.now, context.errors))
    return actions


# LLM: 仅处理原写入器的两个owner内规范安全审计文件；不扫描raw audit/，不按文件mtime删除整份混龄账本。
# 函数用途: 为存在过期记录的安全审计账本规划有指纹保护的记录级整理。
def _security_audit_actions(home, days, now, errors):
    if days <= 0:
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions = []
    for path in security_audit_paths(home):
        try:
            if not path.exists():
                continue
            digest = file_fingerprint(path)
            _content, removed = security_audit_content(path, cutoff)
            if not removed:
                continue
            actions.append(_action(category="security_audit", operation="security_audit_compact", path=path,
                reason=f"older_than_{days}_days", proof=_ActionProof(
                    source_fingerprint=digest, cutoff_timestamp=cutoff)))
        except (OSError, ValueError):
            errors.append(_state_error("MEMORY_RETENTION_SECURITY_AUDIT_UNREADABLE", path))
    return actions


# LLM: 路径与runtime_paths、interaction_actor_audit现有生产写入器一致；自定义外部audit路径不由retention猜测或扫描。
# 函数用途: 返回默认特权工具与交互事件的安全审计文件，不混入策展原料。
def security_audit_paths(home):
    return (Path(home.owner_logs_dir) / "audit" / "audit.jsonl", Path(home.owner_audit_log_jsonl))


# LLM: 保留未知/坏JSON行及原字节排版；仅严格可判定的旧timestamp可删，首末FD/路径变化拒绝整批。
# 函数用途: 计算安全审计保留内容与实际删除行数，扫描和锁内执行共用。
def security_audit_content(path, cutoff):
    kept, removed = [], 0
    with _fingerprint_source(path) as source:
        before = _file_identity(os.fstat(source.fileno()))
        for raw in source:
            text = raw.decode("utf-8")
            try:
                row = json.loads(text)
            except ValueError:
                row = None
            stamp = _timestamp(row.get("timestamp")) if isinstance(row, dict) else None
            if stamp is not None and stamp < cutoff:
                removed += 1
            else:
                kept.append(text)
        if before != _file_identity(os.fstat(source.fileno())):
            raise OSError("security audit changed while reading")
    if before != _file_identity(path.stat(follow_symlinks=False)):
        raise OSError("security audit replaced while reading")
    return "".join(kept), removed


# LLM: 快照清理只接进现有 retention 流程（回收站 / legal hold / 执行前复核 / 报告都复用），不另造清理器。
#   快照与工具清单一起规划：清单"还有没有人用"只由本轮不会被移走的剩余 .json 快照摘要里的
#   manifest_sha256（结构化字段）决定。先删快照、再按剩余引用删清单的次序由类别名保证——计划最后按
#   (category, path) 排序，context_bundle_snapshot 排在 tool_manifest 前面，执行时快照先走。
# 函数用途: 规划过期快照与无人引用的旧工具清单的回收动作（两者都走可恢复回收站）。
def _context_bundle_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    base = Path(context.home.owner_home_dir) / "memory_archive" / "snapshots"
    actions, kept = _snapshot_retention_actions(context, base / "context_bundles")
    actions.extend(_manifest_retention_actions(context, base / "tool_manifests", kept))
    return actions


# LLM: 快照范围 = memory_archive/snapshots/context_bundles/<日期>/ 下的 <请求>.json 与 .md；latest 副本
#   永不参与删除（但它的引用照算）。30 天内改过的都保留，进行中回合的快照天然安全；命中 30 天的交给
#   trash_file 移进回收站，不做永久删除。清单引用只从"不会被移走的剩余 .json"里读，被移走的那批不算。
# 函数用途: 扫描过期快照文件，返回动作与剩余 .json 快照（供清单引用判断）。
def _snapshot_retention_actions(
    context: _RetentionScanContext,
    root: Path,
) -> tuple[list[MemoryRetentionAction], tuple[Path, ...]]:
    if not root.exists():
        return [], ()
    cutoff = context.now.timestamp() - _CONTEXT_BUNDLE_SNAPSHOT_DAYS * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    kept: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            context.errors.append(_symlink_error(path))
            continue
        if path.is_dir() or path.suffix not in {".json", ".md"} or not path.is_file():
            continue
        if _keep_snapshot(context, kept, path, cutoff):
            continue
        actions.append(
            _action(
                category="context_bundle_snapshot",
                operation="trash_file",
                path=path,
                reason=f"older_than_{_CONTEXT_BUNDLE_SNAPSHOT_DAYS}_days",
                destination=_trash_destination(
                    context.home,
                    category="context_bundle_snapshot",
                    path=path,
                    now=context.now,
                ),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions, tuple(kept)


# LLM: latest、30 天内修改和显式 legal_hold_task_ids 的快照必须保留；Markdown 通过同名 JSON 的 scope 关联。
#   保留的 .json 要作为"剩余快照"参与清单引用判断；.md 不含结构化字段，不参与。
# 函数用途: 判断一个快照文件是否保留；是的话把 .json 记入引用来源并返回 True。
def _keep_snapshot(
    context: _RetentionScanContext, kept: list[Path], path: Path, cutoff: float
) -> bool:
    keep = (
        path.name.startswith(_CONTEXT_BUNDLE_LATEST_PREFIX)
        or _mtime(path) >= cutoff
        or _snapshot_task_held(context, path)
    )
    if keep and path.suffix == ".json":
        kept.append(path)
    return keep


# LLM: 任务保护只读快照 scope.task_id 与 typed policy 的精确交集；Markdown 不解析正文，读取同名 JSON。
#   保护策略存在而快照无法读取时关闭式保留并报错，不让损坏记录绕过 hold。
# 函数用途: 判断某个旧快照是否属于显式禁止清理的任务。
def _snapshot_task_held(context: _RetentionScanContext, path: Path) -> bool:
    if not context.held_task_ids:
        return False
    metadata_path = path.with_suffix(".json")
    report = read_json_object_report(metadata_path, context="memory_retention.context_bundle_hold")
    if report.load_error is not None:
        context.errors.append(MemoryRetentionError(
            "MEMORY_RETENTION_CONTEXT_BUNDLE_INVALID", str(metadata_path),
            "snapshot task scope is unreadable; legal hold cannot be excluded",
        ))
        return True
    scope = report.payload.get("scope")
    task_id = scope.get("task_id") if isinstance(scope, dict) else None
    return isinstance(task_id, str) and task_id in context.held_task_ids


# LLM: 清单同时满足"修改时间超过 30 天"和"没有任何剩余快照引用"才回收；读不懂任何一份剩余快照时
#   算不出完整引用集合，本轮不动任何清单（关闭式失败，只报告结构化错误）。
# 函数用途: 规划无人引用的旧工具清单回收动作。
def _manifest_retention_actions(
    context: _RetentionScanContext,
    root: Path,
    kept: tuple[Path, ...],
) -> list[MemoryRetentionAction]:
    if not root.exists():
        return []
    referenced, complete = _referenced_manifest_shas(kept, context.errors)
    if not complete:
        return []
    cutoff = context.now.timestamp() - _CONTEXT_BUNDLE_SNAPSHOT_DAYS * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            context.errors.append(_symlink_error(path))
            continue
        if not path.is_file() or path.stem in referenced:
            continue
        if _mtime(path) >= cutoff:
            continue
        actions.append(
            _action(
                category="tool_manifest",
                operation="trash_file",
                path=path,
                reason=f"unreferenced_older_than_{_CONTEXT_BUNDLE_SNAPSHOT_DAYS}_days",
                destination=_trash_destination(
                    context.home,
                    category="tool_manifest",
                    path=path,
                    now=context.now,
                ),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 引用只认结构化字段：manifest_sha256 与 manifest_ref 都非空（与读取端一致——ref 空表示未持久化，
#   读取端根本不会碰磁盘）；不按文件名猜引用。任一份剩余快照读不懂时返回 complete=False，调用方关闭式跳过。
# 函数用途: 汇总剩余快照引用的清单 sha 集合与"引用集合是否完整"。
def _referenced_manifest_shas(
    kept: tuple[Path, ...],
    errors: list[MemoryRetentionError],
) -> tuple[frozenset[str], bool]:
    shas: set[str] = set()
    complete = True
    for path in kept:
        report = read_json_object_report(path, context="memory_retention.context_bundle_snapshot")
        if report.load_error is not None:
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CONTEXT_BUNDLE_INVALID",
                    str(path),
                    "snapshot summary is unreadable; tool manifests stay untouched this run",
                )
            )
            complete = False
            continue
        manifest = report.payload.get("tool_manifest")
        if not isinstance(manifest, dict):
            continue
        sha = str(manifest.get("manifest_sha256") or "").strip()
        ref = str(manifest.get("manifest_ref") or "").strip()
        if sha and ref:
            shas.add(sha)
    return frozenset(shas), complete


# LLM: 执行端在快照动作结束后重读当前引用；返回值仅限本次 apply 使用，不持久化为第二份权威索引。
# 函数用途: 汇总磁盘上仍存在的快照引用，保护移动失败、规划后变化或新增的快照所用清单。
def current_tool_manifest_references(home: object) -> tuple[frozenset[str], bool]:
    root = Path(home.owner_home_dir) / "memory_archive" / "snapshots" / "context_bundles"
    if not root.exists():
        return frozenset(), True
    paths = tuple(root.rglob("*.json"))
    if root.is_symlink() or any(path.is_symlink() for path in paths):
        return frozenset(), False
    errors: list[MemoryRetentionError] = []
    return _referenced_manifest_shas(paths, errors)


# LLM: 符号链接错误采用统一机器码；不读取链接目标，也不靠错误文案决定后续动作。
# 函数用途: 构造一条"符号链接被跳过"的结构化错误（retention 从不跟随符号链接）。
def _symlink_error(path: Path) -> MemoryRetentionError:
    return MemoryRetentionError(
        "MEMORY_RETENTION_SYMLINK_SKIPPED",
        str(path),
        "retention never follows symbolic links",
    )


# LLM: runtime 只接受当前 schema；旧 v1 key 由 MemoryMigrationService 一次性转换。
# 函数用途: 严格读取 retention.json 并给损坏返回稳定错误。
def _load_policy(
    path: Path,
) -> tuple[MemoryRetentionPolicy | None, MemoryRetentionError | None]:
    report = read_json_object_report(path, context="memory_retention.policy")
    if report.load_error is not None:
        return None, MemoryRetentionError(
            "MEMORY_RETENTION_POLICY_UNREADABLE",
            str(path),
            str(report.load_error.get("error_type") or "unreadable JSON"),
        )
    try:
        return MemoryRetentionPolicy.from_payload(report.payload), None
    except (TypeError, ValueError) as exc:
        return None, MemoryRetentionError(
            "MEMORY_RETENTION_POLICY_INVALID",
            str(path),
            str(exc),
        )


# LLM: 内部只读借用不修改模型，保留list/load覆盖；仍只选合法未晋升且严格早于cutoff，指纹供执行CAS。
# 函数用途: 不复制全账本地规划过期候选动作，仓库损坏仍阻断。
def _candidate_actions(
    *,
    candidates: CandidateService,
    policy: MemoryRetentionPolicy,
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.rejected_candidate_days <= 0:
        return []
    cutoff = now.timestamp() - policy.rejected_candidate_days * _DAY_SECONDS
    try:
        records = getattr(candidates, "readonly_rows", candidates.list)(statuses=RETENTION_ELIGIBLE_CANDIDATE_STATUSES)
    except Exception as exc:  # noqa: BLE001 - convert repository corruption to stable plan error.
        errors.append(
            MemoryRetentionError(
                "MEMORY_RETENTION_CANDIDATES_UNREADABLE",
                str(candidates.path),
                f"{type(exc).__name__}: {exc}",
            )
        )
        return []
    actions: list[MemoryRetentionAction] = []
    authority_fingerprint = file_fingerprint(candidates.path)
    for record in records:
        if not candidate_retention_eligible(record):
            continue
        from .candidates import candidate_retention_time

        updated = _timestamp(candidate_retention_time(record))
        if updated is None:
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CANDIDATE_TIMESTAMP_INVALID",
                    str(candidates.path),
                    f"candidate {record.candidate_id} has invalid updated_at",
                )
            )
            continue
        if updated >= cutoff:
            continue
        actions.append(
            _action(
                category="rejected_candidate",
                operation="candidate_delete",
                path=candidates.path,
                reason=f"terminal_candidate_older_than_{policy.rejected_candidate_days}_days",
                proof=_ActionProof(
                    authority_path=candidates.path,
                    authority_fingerprint=authority_fingerprint,
                    record_id=record.candidate_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 恢复材料有两个规范根：旧版 O/tasks 与新版运行工作区 O/runs；两者共用同一套 work/state.json 合同。
#   新增运行根不能让旧根失去保护，也不能凭目录名推断类别。
# 函数用途: 返回保存恢复材料的全部规范根目录。
def _recovery_roots(home: object) -> tuple[Path, ...]:
    owner_home = Path(home.owner_home_dir)
    roots = (durable_work_root(owner_home, "task"), owner_home / "tasks", durable_work_root(owner_home, "audit"))
    return tuple(dict.fromkeys(roots))


# LLM: 只枚举本层目录且记录发现代次；仅ENOENT为空，权限/IO/链接/特殊类型必须上抛，不进入scratch正文。
# 函数用途: 严格列出一个发现目录下的普通子目录，避免glob吞错。
def _discovery_directories(root: Path):
    watch_pin_source(root)
    try:
        info = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(22, "unsafe task discovery directory", str(root))
    children = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    member = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # 一个成员消失不代表整个发现目录为空，继续检查其余活跃任务。
                    continue
                if stat.S_ISDIR(member.st_mode):
                    children.append(Path(entry.path))
                elif not stat.S_ISREG(member.st_mode):
                    raise OSError(22, "unsafe task discovery member", entry.path)
    except FileNotFoundError:
        return ()
    return tuple(sorted(children))


# LLM: 精确suffix父链逐段lstat且监视缺失路径；work已存在而首次state发表也会使快照失效。
# 函数用途: 检查任务的规范状态/临时目录，不递归枚举其中内容。
def _discovery_suffix(base: Path, suffix: str):
    path = base
    for part in Path(suffix).parts:
        path = path / part
        watch_pin_source(path)
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) and (path.name != "state.json" or not stat.S_ISREG(info.st_mode)):
            raise OSError(22, "unsafe task discovery suffix", str(path))
    return path


# LLM: 一/二层有界发现共用显式scandir；计划记录错误而pin调用上抛使complete=False，不递归任务内容；不得按合法任务名过滤。
# 函数用途: 枚举扁平和日期分组任务的精确路径并监视work及缺失state。
def _fixed_depth_paths(root: Path, suffix: str, errors=None):
    paths = set()
    try:
        for child in _discovery_directories(root):
            path = _discovery_suffix(child, suffix)
            if path is not None:
                paths.add(path)
            for nested in _discovery_directories(child):
                path = _discovery_suffix(nested, suffix)
                if path is not None:
                    paths.add(path)
    except OSError as exc:
        if errors is None:
            raise
        errors.append(_state_error("MEMORY_RETENTION_TASK_DISCOVERY_UNREADABLE", Path(exc.filename or root)))
        return []
    return sorted(paths)


# LLM: 仅发现owner内两个规范DB位置，读取失败、链接或任何活跃锁均保守关闭，不创建DB或改PRAGMA持久设置。
# 函数用途: 返回可用于只读终态核对的运行库路径。
def _runtime_db_paths(home):
    owner = Path(home.owner_home_dir)
    return tuple(path for path in (owner / "runtime.db", *sorted(
        (Path(home.owner_workspace_dir) / "runtime/workspaces").glob("*/runtime.db"))) if path.exists())


# LLM: query_only+mode=ro，原schema的lease锁先核；不认投影正文、不补猜缺失表的无锁事实。
# 函数用途: 用既有资源锁判断scratch是否可清理；任一库未知则不删。
def _runtime_unlocked(home, now):
    try:
        for path in _runtime_db_paths(home):
            if path.is_symlink() or not path.is_file():
                return False
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
                db.execute("PRAGMA query_only=ON")
                if db.execute("SELECT 1 FROM resource_locks WHERE lease_expires_at > ? LIMIT 1", (now.timestamp(),)).fetchone():
                    return False
        return True
    except (sqlite3.Error, OSError):
        return False


# LLM: 同一只读快照精确查run/task身份，所有匹配运行都须terminal；不把DB更新时钟当来源权威。
# 函数用途: 为仍写RUNNING的恢复档案取得终态及完成时间，任何冲突/未知返回None。
def runtime_terminal(home, task_root, state, now):
    if not _runtime_unlocked(home, now):
        return None
    run_id = str(state.get("run_id") or task_root.name)
    task_id = str(state.get("task_id") or "")
    matches = []
    try:
        for path in _runtime_db_paths(home):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                rows = db.execute("SELECT status, updated_at FROM agent_runs WHERE run_id=? OR agent_run_id=?", (run_id, run_id)).fetchall()
                if task_id:
                    rows += db.execute("SELECT status, MAX(updated_at, closed_at) FROM task_runs WHERE task_id=?", (task_id,)).fetchall()
                if not rows:
                    continue
                if any(str(status).upper() not in {"DONE", "FAILED", "CANCELLED"} or _timestamp(stamp) is None
                       for status, stamp in rows):
                    return None
                matches.extend(rows)
    except (sqlite3.Error, OSError):
        return None
    if not matches:
        return None
    return max(((str(status).upper(), _timestamp(stamp)) for status, stamp in matches), key=lambda item: item[1])


# LLM: 只遍历已授权scratch；链接/特殊文件/新鲜普通文件均关闭整棵，空子目录不凭新建mtime延长旧文件寿命。
# 函数用途: 为闲置scratch计算安全元数据指纹和清理快照，不读取大文件正文。
def scratch_inventory(root: Path, cutoff: float):
    before = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("scratch root is not a regular directory")
    members = []
    for path in _walk_files(root, include_dirs=True):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise OSError("scratch member is unsafe")
        if stat.S_ISREG(info.st_mode) and info.st_mtime >= cutoff:
            raise OSError("scratch is active")
        members.append((str(path.relative_to(root)), _file_identity(info)))
    material = json.dumps([_file_identity(before), sorted(members)], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest(), members


# LLM: stateless任务也可清tmp；存在状态则法律保留/活跃状态/坏状态保护，DB或文件锁未知同样保护。
# 函数用途: 检查task scratch的当前运行保护，不回写state或DB。
def task_scratch_unlocked(home, task_root, held, now):
    if task_root.name in held or not _runtime_unlocked(home, now):
        return False
    path = task_root / "work/state.json"
    if path.exists():
        row = _read_state(path, [], code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if row is None or row.get("task_id") in held:
            return False
        if str(row.get("status") or "").upper() not in TASK_TERMINAL_STATUSES:
            return False
    # 只检查已有锁，不创建lock文件；跨平台无只读锁探针时保守保留。
    for lock in (*task_root.glob("*.lock"), *(task_root / "work").glob("*.lock"), *(task_root / "tmp").glob("*.lock")):
        try:
            import fcntl
            with _fingerprint_source(lock) as source:
                fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(source.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            return False
    return True


# LLM: 只允许规范任务临时目录；复用同次规划pin，所属任务或子证据命中即保护整棵，坏源仍关闭。
# 函数用途: 规划三日闲置任务临时文件和空目录回收。
def _task_tmp_actions(context, excluded, protection):
    days = context.policy.task_tmp_idle_days
    if days <= 0:
        return []
    cutoff = context.now.timestamp() - days * _DAY_SECONDS
    actions = []
    pins, complete = protection
    if not complete and not pins.complete_for(Path(context.home.owner_tasks_dir), context.errors):
        return []
    for root in _fixed_depth_paths(Path(context.home.owner_tasks_dir), "tmp", context.errors):
        if len(root.relative_to(Path(context.home.owner_tasks_dir)).parts) != 3 or pins.protects(root, context.errors):
            continue
        if (any(part.is_symlink() for part in (root, root.parent, root.parent.parent))
                or _inside_any(root, excluded)
                or not task_scratch_unlocked(context.home, root.parent, context.held_task_ids, context.now)):
            continue
        try:
            digest, members = scratch_inventory(root, cutoff)
            state_path = root.parent / "work/state.json"
            state = _read_state(state_path, context.errors, code="MEMORY_RETENTION_TASK_STATE_INVALID") if state_path.exists() else {}
            if state is None or scratch_tree_pinned(pins, root, members, context.errors, task_root=root.parent, state=state):
                continue
        except OSError:
            continue
        if members:
            actions.append(_action(category="task_tmp", operation="scratch_clear", path=root,
                reason=f"idle_scratch_older_than_{days}_days", proof=_ActionProof(
                    source_fingerprint=digest, cutoff_timestamp=cutoff)))
    return actions


# LLM: runs仅终态且共享pin完整才规划；所属运行/任务ID或任何子证据引用都保护整棵scratch。
# 函数用途: 将旧RUNNING档案的tmp/中间工具产物接入原留存计划。
def _run_scratch_actions(context, excluded, protection):
    days = context.policy.tmp_days
    if days <= 0:
        return []
    cutoff = context.now.timestamp() - days * _DAY_SECONDS
    actions = []
    runs = Path(context.home.owner_home_dir) / "runs"
    pins, complete = protection
    if not complete and not pins.complete_for(runs, context.errors):
        return []
    for authority in _fixed_depth_paths(runs, "work/state.json", context.errors):
        root = authority.parent.parent
        if _inside_any(root, excluded):
            continue
        row = _read_state(authority, context.errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if row is None or row.get("task_id") in context.held_task_ids:
            continue
        status = str(row.get("status") or "").upper()
        terminal = ((status, _timestamp(row.get("updated_at"))) if status in TASK_TERMINAL_STATUSES
                    else runtime_terminal(context.home, root, row, context.now) if status == "RUNNING" else None)
        if terminal is None or terminal[1] is None or terminal[1] >= cutoff or not _runtime_unlocked(context.home, context.now):
            continue
        for scratch in (root / "tmp", root / "work/tmp", root / "work/blobs/tool_outputs"):
            if pins.protects(scratch, context.errors):
                continue
            if not scratch.exists() or scratch.is_symlink():
                continue
            try:
                digest, members = scratch_inventory(scratch, cutoff)
                if scratch_tree_pinned(pins, scratch, members, context.errors, task_root=root, state=row):
                    continue
            except OSError:
                continue
            if members:
                actions.append(_action(category="run_scratch", operation="scratch_clear", path=scratch,
                    reason=f"terminal_run_scratch_older_than_{days}_days", proof=_ActionProof(
                        authority_path=authority, authority_fingerprint=file_fingerprint(authority),
                        authority_status=terminal[0], record_id=str(row.get("run_id") or root.name),
                        source_fingerprint=digest, cutoff_timestamp=cutoff)))
    return actions


# LLM: 扫描与执行共用同一判据；只认结构化所属ID及原pins保护，不解析正文猜引用，调用者须先核完整性。
# 函数用途: 所属任务或任一子成员受保护时保留整棵scratch及其未引用兄弟文件。
def scratch_tree_pinned(pins, root, members, errors, *, task_root, state):
    identities = {task_root.name, str(state.get("task_id") or ""), str(state.get("run_id") or "")}
    identities.discard("")
    return bool(identities.intersection(pins.ids) or pins.protects(root, errors)
                or any(pins.protects(root / relative, errors) for relative, _identity in members))


# LLM: 只枚举规范根下state，显式发现错误交调用方记录或上抛，不能把不可读当缺失。
# 函数用途: 逐个产出恢复根与其下的任务状态文件路径。
def _iter_task_states(home: object, errors=None):
    owner_home = Path(home.owner_home_dir)
    for root in _recovery_roots(home):
        for state_path in _fixed_depth_paths(root, "work/state.json", errors):
            task_root = state_path.parent.parent
            # 路径必须正好落在 workspace_paths 认定的规范深度上；深一层或浅一层都不算任务根。
            if canonical_task_root(owner_home, task_root) is None:
                continue
            yield root, state_path


# LLM: 完成任务必须有明确 terminal status 和 updated_at；未知/运行中状态永远保护整个恢复目录。
# 函数用途: 规划超过 completed_task_days 的终态任务移入回收站（runs/tasks/audits 三个规范根一视同仁）。
def _task_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> tuple[list[MemoryRetentionAction], frozenset[Path]]:
    if policy.completed_task_days <= 0:
        return [], frozenset()
    cutoff = now.timestamp() - policy.completed_task_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    selected: set[Path] = set()
    for root, state_path in _iter_task_states(home, errors):
        task_root = state_path.parent.parent
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None or not _safe_child(task_root, root) or task_root.is_symlink():
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        if not task_id or not status:
            errors.append(_state_error("MEMORY_RETENTION_TASK_STATE_INVALID", state_path))
            continue
        if task_id in held_task_ids or status not in TASK_TERMINAL_STATUSES:
            continue
        updated = _timestamp(state.get("updated_at"))
        if updated is None:
            errors.append(_state_error("MEMORY_RETENTION_TASK_TIMESTAMP_INVALID", state_path))
            continue
        if updated >= cutoff:
            continue
        destination = _trash_destination(
            home,
            category="completed_task",
            path=task_root,
            now=now,
        )
        actions.append(
            _action(
                category="completed_task",
                operation="trash_tree",
                path=task_root,
                reason=f"terminal_{status}_older_than_{policy.completed_task_days}_days",
                destination=destination,
                proof=_ActionProof(
                    authority_path=state_path,
                    authority_status=status,
                    authority_fingerprint=file_fingerprint(state_path),
                    source_fingerprint=tree_fingerprint(task_root),
                    record_id=task_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
        selected.add(task_root.resolve(strict=False))
    return actions, frozenset(selected)


# LLM: 工具正文只有在父任务结构化终态且终态时间超过 cutoff 后可清；运行中任务一律保留恢复材料。
# 函数用途: 规划恢复根下 tool_outputs 大正文目录移入回收站（含旧 tasks 与新版 runs 两个根）。
def _tool_output_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.tool_output_days_after_terminal <= 0:
        return []
    cutoff = now.timestamp() - policy.tool_output_days_after_terminal * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    # 本片只扩 completed_task；tool output 侧维持原范围（仅旧 O/tasks），不随恢复根一起扩。
    root = Path(home.owner_tasks_dir)
    owner_home = Path(home.owner_home_dir)
    for state_path in _fixed_depth_paths(root, "work/state.json", errors):
        task_root = state_path.parent.parent
        # 只收窄：任务根必须落在规范深度上。运行中任务的 output 里嵌套一份「像任务」的目录
        # （带终态 state.json）时，它下面的 blobs/tool_outputs 不能被当成任务根移走（dev 2026-09-29）。
        if canonical_task_root(owner_home, task_root) is None:
            continue
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None:
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        updated = _timestamp(state.get("updated_at"))
        if (
            not task_id
            or task_id in held_task_ids
            or status not in TASK_TERMINAL_STATUSES
            or updated is None
            or updated >= cutoff
        ):
            continue
        for output_dir in _task_tool_output_dirs(task_root):
            actions.append(
                _action(
                    category="tool_output",
                    operation="trash_tree",
                    path=output_dir,
                    reason=(
                        f"terminal_{status}_tool_output_older_than_"
                        f"{policy.tool_output_days_after_terminal}_days"
                    ),
                    destination=_trash_destination(
                        home,
                        category="tool_output",
                        path=output_dir,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=state_path,
                        authority_status=status,
                        authority_fingerprint=file_fingerprint(state_path),
                        source_fingerprint=tree_fingerprint(output_dir),
                        record_id=task_id,
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 旧 subagent scratch 清理继续使用同一 v2 plan/apply；只有子代理自身 terminal state 能授权删除。
# 函数用途: 规划终态子代理的 inbox/outbox/compactions/tool_outputs 临时目录。
def _subagent_scratch_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.subagent_scratch_days <= 0:
        return []
    task_root_dir = Path(home.owner_tasks_dir)
    owner_home = Path(home.owner_home_dir)
    cutoff = now.timestamp() - policy.subagent_scratch_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for agents_dir in _fixed_depth_paths(task_root_dir, "work/agents", errors):
        task_root = agents_dir.parent.parent
        # 只收窄：同上，嵌套的「像任务」目录不算任务根，它下面的子代理 inbox 不能被动。
        if canonical_task_root(owner_home, task_root) is None:
            continue
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        task_state = _read_state(
            task_root / "work" / "state.json",
            errors,
            code="MEMORY_RETENTION_TASK_STATE_INVALID",
        )
        if task_state is None or str(task_state.get("task_id") or "") in held_task_ids:
            continue
        for run_root in sorted(path for path in agents_dir.iterdir() if path.is_dir()):
            authority = _subagent_state_path(run_root)
            if authority is None:
                continue
            state = _read_state(
                authority,
                errors,
                code="MEMORY_RETENTION_SUBAGENT_STATE_INVALID",
            )
            if state is None:
                continue
            status = str(state.get("status") or "").strip().upper()
            updated = _timestamp(state.get("updated_at"))
            if status not in TASK_TERMINAL_STATUSES or updated is None or updated >= cutoff:
                continue
            for relative in _SUBAGENT_SCRATCH_PATHS:
                scratch = run_root / relative
                if not scratch.exists() or scratch.is_symlink():
                    continue
                actions.append(
                    _action(
                        category="subagent_scratch",
                        operation="trash_tree",
                        path=scratch,
                        reason=(
                            f"terminal_{status}_scratch_older_than_"
                            f"{policy.subagent_scratch_days}_days"
                        ),
                        destination=_trash_destination(
                            home,
                            category="subagent_scratch",
                            path=scratch,
                            now=now,
                        ),
                        proof=_ActionProof(
                            authority_path=authority,
                            authority_status=status,
                            authority_fingerprint=file_fingerprint(authority),
                            source_fingerprint=tree_fingerprint(scratch),
                            cutoff_timestamp=cutoff,
                        ),
                    )
                )
    return actions


# LLM: ConversationStore 原文按 thread 整体处理；有 active_task_ids、held task 或坏 transcript 时绝不删除。
# 函数用途: 规划超过 conversation_days 的完整会话及其 thread-scoped 附属文件移入回收站。
def _conversation_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    roots: tuple[Path, ...],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.conversation_days <= 0:
        return []
    cutoff = now.timestamp() - policy.conversation_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for root in roots:
        for thread_path in sorted((root / "threads").glob("*.json")):
            report = read_json_object_report(
                thread_path,
                context="memory_retention.conversation_thread",
            )
            if report.load_error is not None:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        str(report.load_error.get("error_type") or "unreadable thread"),
                    )
                )
                continue
            try:
                thread = ConversationThread.from_dict(report.payload)
            except (TypeError, ValueError) as exc:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if thread.thread_id != thread_path.stem or not thread.thread_id:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_ID_MISMATCH",
                        str(thread_path),
                        "thread_id does not match authority filename",
                    )
                )
                continue
            if thread.active_task_ids or held_task_ids.intersection(thread.task_ids):
                continue
            if thread.updated_at <= 0:
                errors.append(
                    _state_error("MEMORY_RETENTION_CONVERSATION_TIMESTAMP_INVALID", thread_path)
                )
                continue
            if thread.updated_at >= cutoff:
                continue
            related, related_errors = _conversation_related_paths(root, thread)
            errors.extend(related_errors)
            if related_errors:
                continue
            actions.append(
                _action(
                    category="conversation",
                    operation="conversation_trash",
                    path=thread_path,
                    reason=f"inactive_conversation_older_than_{policy.conversation_days}_days",
                    destination=_trash_destination(
                        home,
                        category="conversation",
                        path=thread_path,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=thread_path,
                        authority_status=thread.status,
                        authority_fingerprint=file_fingerprint(thread_path),
                        source_fingerprint=paths_fingerprint(related),
                        record_id=thread.thread_id,
                        cutoff_timestamp=cutoff,
                        related_paths=related,
                    ),
                )
            )
    return actions


# LLM: thread-scoped 文件通过结构化 ID 组成；message JSONL 坏行会阻断整个会话删除。
# 函数用途: 收集一个会话的原文、任务链接、目标、观察、guidance、claim、wake 文件，以及唤醒毒丸的尝试账、结案记录和重放留档。
def _conversation_related_paths(
    root: Path,
    thread: ConversationThread,
) -> tuple[tuple[Path, ...], list[MemoryRetentionError]]:
    thread_id = thread.thread_id
    candidates = [
        root / "threads" / f"{thread_id}.json",
        root / "messages" / f"{thread_id}.jsonl",
        root / "observations" / f"{thread_id}.jsonl",
        root / "goals" / f"{thread_id}.json",
        root / "background_claims" / f"{thread_id}.json",
        root / "guidance" / f"thread.{thread_id}.jsonl",
        *(root / "tasks" / f"{task_id}.json" for task_id in thread.task_ids),
        *(root / "wake_queue" / "dedupe").glob(f"{thread_id}.*.json"),
    ]
    errors: list[MemoryRetentionError] = []
    messages = root / "messages" / f"{thread_id}.jsonl"
    if messages.exists():
        report = read_jsonl_objects_report(
            messages,
            context="memory_retention.conversation_messages",
        )
        if report.load_errors or any(
            str(row.get("thread_id") or "") != thread_id for row in report.records
        ):
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CONVERSATION_MESSAGES_INVALID",
                    str(messages),
                    "conversation transcript contains unreadable or cross-thread rows",
                )
            )
    candidates.extend(_thread_wake_files(root / "wake_queue", thread_id, errors))
    paths = tuple(
        sorted(
            {
                path.resolve(strict=False)
                for path in candidates
                if path.exists() and path.is_file() and not path.is_symlink()
            },
            key=str,
        )
    )
    return paths, errors


# LLM: 只按每个 JSON 顶层 thread_id 判定归属；读不出的文件记 MEMORY_RETENTION_CONVERSATION_WAKE_INVALID（写入 errors），
#   不猜归属。目录：待处理的 urgent/normal、毒丸尝试账 attempts、结案 quarantine、quarantine/replayed/<id>/ 留档和
#   quarantine/archive/ 顶层的已归档结案记录（带 thread_id）。archive/ledger、archive/unreadable 与 quarantine/ledger、
#   quarantine/unreadable 一样无法归属会话，不随会话删除，由运维清理。
# 函数用途: 收集一个会话在唤醒队列下的全部文件，供会话删除一起清理；只读文件，不删除。
def _thread_wake_files(wake_root: Path, thread_id: str, errors: list[MemoryRetentionError]) -> list[Path]:
    replayed = wake_root / "quarantine" / "replayed"
    directories = (wake_root / "urgent", wake_root / "normal", wake_root / "attempts", wake_root / "quarantine",
                   wake_root / "quarantine" / "archive",
                   *sorted(path for path in replayed.glob("*") if path.is_dir()))
    paths = [item for directory in directories if directory.exists() for item in directory.glob("*.json")]
    return [path for path in paths if _wake_file_thread(path, errors) == thread_id]


# LLM: 读不出的文件把结构化错误追加进 errors 并返回空串（空串不会等于任何真实 thread_id）。
# 函数用途: 读出一个唤醒相关文件顶层记录的 thread_id。
def _wake_file_thread(path: Path, errors: list[MemoryRetentionError]) -> str:
    report = read_json_object_report(path, context="memory_retention.conversation_wake")
    if report.load_error is None:
        return str(report.payload.get("thread_id") or "")
    errors.append(MemoryRetentionError("MEMORY_RETENTION_CONVERSATION_WAKE_INVALID", str(path), "wake state is unreadable"))
    return ""


# LLM: 单次lstat判类型和mtime；包括链接检查阶段的IO失败均记诊断，不因Python版本把错误吞为空文件。
# 函数用途: 规划过期文件，必要时使用原可恢复回收站。
def _expired_file_actions(
    *,
    category: str,
    root: Path,
    days: int,
    pattern: str,
    now: datetime,
    excluded_roots: frozenset[Path],
    errors: list[MemoryRetentionError],
    protection: tuple[_RetentionPins, object] | None = None,
) -> list[MemoryRetentionAction]:
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for path in sorted(root.rglob(pattern)):
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            errors.append(_state_error("MEMORY_RETENTION_FINGERPRINT_INVALID", path))
            continue
        if stat.S_ISLNK(info.st_mode):
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_SYMLINK_SKIPPED",
                    str(path),
                    "retention never follows symbolic links",
                )
            )
            continue
        if not stat.S_ISREG(info.st_mode) or _inside_any(path, excluded_roots):
            continue
        if info.st_mtime >= cutoff:
            continue
        if protection is not None and protection[0].protects(path, errors):
            continue
        trash_home = protection[1] if protection is not None and category in {"daily", "compact"} else None
        actions.append(
            _action(
                category=category,
                operation="trash_file" if trash_home is not None else "delete_file",
                path=path,
                reason=f"older_than_{days}_days",
                destination=(_trash_destination(trash_home, category=category, path=path, now=now)
                             if trash_home is not None else None),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: purge只接受moved_at并复用规划pin；目标FD复核仍要求全源稳定，坏源或相关引用绝不放行。
# 函数用途: 规划回收站最终删除，保留宽限期内重新被引用的材料。
def _trash_actions(
    *,
    home: object,
    days: int,
    now: datetime,
    errors: list[MemoryRetentionError],
    conversation_roots: tuple[Path, ...] = (),
    protection: tuple[_RetentionPins, bool] | None = None,
) -> list[MemoryRetentionAction]:
    root = Path(home.owner_trash_dir)
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    retention_root = root / "retention"
    for tombstone in sorted(retention_root.glob("*/*/tombstone.json")) if retention_root.exists() else ():
        report = read_json_object_report(tombstone, context="memory_retention.trash_tombstone")
        moved = _timestamp(report.payload.get("moved_at")) if report.load_error is None else None
        if moved is None:
            errors.append(_state_error("MEMORY_RETENTION_TOMBSTONE_INVALID", tombstone))
            continue
        if moved < cutoff:
            container = tombstone.parent
            if protection is None:
                protection = current_retention_pins(home, conversation_roots, errors, now=now)
            pins, complete = protection
            if (not complete and not pins.complete_for(container, errors)) or trash_payload_pinned(pins, container, errors):
                continue
            actions.append(
                _action(
                    category="trash",
                    operation="delete_tree",
                    path=container,
                    reason=f"trashed_older_than_{days}_days",
                    proof=_ActionProof(
                        authority_path=tombstone,
                        authority_fingerprint=file_fingerprint(tombstone),
                        source_fingerprint=tree_fingerprint(container),
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 用scandir传播发现错误；目录与缺失threads也登记pin失效源，新建会话根不能漏掉，链接仍拒绝。
# 函数用途: 严格发现标准ConversationStore根（含直连与runtime布局），保证pin发现完整性。
def _conversation_roots(home: object, explicit: tuple[Path, ...]) -> tuple[Path, ...]:
    discovered = list(explicit)
    owner_home = getattr(home, "owner_home_dir", None)
    if owner_home is not None:
        discovered.append(Path(owner_home) / "conversations")
    runtime = Path(home.owner_workspace_dir) / "runtime" / "workspaces"
    watch_pin_source(runtime)
    try:
        runtime.stat()
    except FileNotFoundError:
        pass
    else:
        if runtime.is_symlink():
            raise OSError("retention discovery root is a symlink")
        with os.scandir(runtime) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise OSError("retention discovery child is a symlink", entry.path)
                if entry.is_dir(follow_symlinks=False):
                    discovered.append(Path(entry.path) / "conversations")
    result: list[Path] = []
    for raw in discovered:
        path = Path(raw).expanduser()
        watch_pin_source(path)
        watch_pin_source(path / "threads")
        try:
            info = (path / "threads").stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if path == Path(path.anchor) or path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise OSError("retention conversation root is unsafe")
        result.append(path.resolve(strict=False))
    return tuple(dict.fromkeys(result))


# LLM: 证明字段把规划动作绑定到精确权威与来源快照，供执行时重新验证，不能凭动作类别猜证据。
# 类用途: 聚合 retention 动作的状态权威、指纹、记录编号、截止时间和关联路径。
@dataclass(frozen=True)
class _ActionProof:
    authority_path: Path | None = None
    authority_status: str = ""
    authority_fingerprint: str = ""
    source_fingerprint: str = ""
    record_id: str = ""
    cutoff_timestamp: float | None = None
    related_paths: tuple[Path, ...] = ()


# LLM: 动作构造器统一生成稳定 ID，防止各扫描器自行定义互相冲突的幂等语义。
# 函数用途: 将基础动作字段与可重验证证据组合成完整 retention 动作。
def _action(
    *,
    category: str,
    operation: str,
    path: Path,
    reason: str,
    destination: Path | None = None,
    proof: _ActionProof | None = None,
) -> MemoryRetentionAction:
    evidence = proof or _ActionProof()
    return MemoryRetentionAction(
        action_id=retention_action_id(
            category=category,
            operation=operation,
            path=path,
            record_id=evidence.record_id,
            cutoff_timestamp=evidence.cutoff_timestamp,
        ),
        category=category,
        operation=operation,
        path=path,
        reason=reason,
        destination=destination,
        authority_path=evidence.authority_path,
        authority_status=evidence.authority_status,
        authority_fingerprint=evidence.authority_fingerprint,
        source_fingerprint=evidence.source_fingerprint,
        record_id=evidence.record_id,
        cutoff_timestamp=evidence.cutoff_timestamp,
        related_paths=evidence.related_paths,
    )


# LLM: 状态读取不容忍坏 JSON 或空对象；调用方据此保护相应目录。
# 函数用途: 加载 task/subagent state 并记录稳定错误。
def _read_state(
    path: Path,
    errors: list[MemoryRetentionError],
    *,
    code: str,
) -> dict[str, Any] | None:
    report = read_json_object_report(path, context="memory_retention.state")
    if report.load_error is not None or not report.payload:
        errors.append(_state_error(code, path))
        return None
    return report.payload


# LLM: 任务输出目录只认规范 work/blobs 和已有 subagent artifacts 结构，不按正文或文件名猜任务状态。
# 函数用途: 找出一个任务下可按终态清理的工具大输出目录。
def _task_tool_output_dirs(task_root: Path) -> tuple[Path, ...]:
    canonical = task_root / "work" / "blobs" / "tool_outputs"
    found = [canonical] if canonical.is_dir() and not canonical.is_symlink() else []
    agents = task_root / "work" / "agents"
    if agents.exists():
        found.extend(
            path
            for path in agents.glob("*/artifacts/tool_outputs")
            if path.is_dir() and not path.is_symlink()
        )
    return tuple(sorted(dict.fromkeys(found), key=str))


# LLM: canonical_state 优先于 state，二者都不存在时不猜子代理终态。
# 函数用途: 定位子代理唯一可用状态权威。
def _subagent_state_path(run_root: Path) -> Path | None:
    for name in ("canonical_state.json", "state.json"):
        path = run_root / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


# LLM: trash 目标绑定时间、类别和源路径 hash；绝不能落到 owner_trash_dir 之外。
# 函数用途: 为可恢复删除生成唯一目标目录。
def _trash_destination(home: object, *, category: str, path: Path, now: datetime) -> Path:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:12]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return Path(home.owner_trash_dir) / "retention" / category / f"{stamp}-{path.name}-{digest}"


# LLM: 只有严格小写64hex可作删除证据；missing/unreadable及任何非字符串都拒绝。
# 函数用途: 校验指纹是否为完整SHA256摘要。
def valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# LLM: 同一FD读前/读后身份须相同且绑定原路径；非阻塞nofollow打开防FIFO替换挂起，不漂白读错。
# 函数用途: 用64KiB块计算稳定普通文件的SHA256，分别返回missing或unreadable。
def file_fingerprint(path: Path) -> str:
    try:
        with _fingerprint_source(path) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                return "unreadable"
            digest = hashlib.sha256()
            while chunk := source.read(65536):
                digest.update(chunk)
            after = os.fstat(source.fileno())
            current = path.stat(follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            return "unreadable"
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


# LLM: 不跟随最终链接并非阻塞打开，再由调用者fstat验证普通文件；fdopen失败也关闭原FD。
# 函数用途: 为流式指纹取得安全二进制句柄，避免FIFO使维护无限阻塞。
def _fingerprint_source(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


# LLM: ctime补强同大小改写检测；与任务要求的dev/ino/size/mtime_ns同时核对，不授予写权限。
# 函数用途: 提取文件快照身份供流式读取验证。
def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


# LLM: 任一来源或权威摘要无效即取消动作并记录路径级诊断，目录组合也不能漂白坏成员。
# 函数用途: 为所有扫描类别统一执行指纹准入门。
def _validated_actions(actions, errors):
    accepted = []
    for action in actions:
        checks = ([(action.path, action.source_fingerprint)] if action.operation != "candidate_delete" else [])
        if action.authority_path is not None:
            checks.append((action.authority_path, action.authority_fingerprint))
        bad = [path for path, digest in checks if not valid_fingerprint(digest)]
        if bad:
            errors.extend(_state_error("MEMORY_RETENTION_FINGERPRINT_INVALID", path) for path in bad)
        else:
            accepted.append(action)
    return accepted


# LLM: 目录枚举/任一成员缺失或不可读都返回无效标记，成员检查集中于_tree_member_material，不漂白错误。
# 函数用途: 计算目录快照，失败关闭并且不跟随链接。
def tree_fingerprint(path: Path) -> str:
    try:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            return file_fingerprint(path)
        if not stat.S_ISDIR(info.st_mode):
            return "unreadable"
        material: list[str] = []
        for item in _walk_files(path, include_dirs=True):
            material.append(_tree_member_material(path, item))
        return hashlib.sha256("\n".join(sorted(material)).encode("utf-8")).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


# LLM: 仅普通目录或有效摘要普通文件可进入组合；链接/特殊文件/坏hash抛错令外层无效。
# 函数用途: 为目录指纹生成一个成员的安全材料。
def _tree_member_material(root: Path, item: Path) -> str:
    relative = item.relative_to(root)
    member = item.stat(follow_symlinks=False)
    if stat.S_ISDIR(member.st_mode):
        return f"D:{relative}"
    if stat.S_ISREG(member.st_mode):
        digest = file_fingerprint(item)
        if valid_fingerprint(digest):
            return f"F:{relative}:{digest}"
    raise OSError("retention tree member is unreadable")


# LLM: 多路径摘要仅组合真实有效文件hash；任何坏成员都使外层无效。
# 函数用途: 安全计算一组精确文件的稳定指纹。
def paths_fingerprint(paths: tuple[Path, ...]) -> str:
    material = []
    for path in sorted(paths, key=str):
        digest = file_fingerprint(path)
        if not valid_fingerprint(digest):
            return "unreadable"
        material.append(f"{path}:{digest}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


# LLM: os.walk错误显式传播；记录发现目录与缺失根，变化使阶段pin视图失效，不跟随链接。
# 函数用途: 严格枚举目录，供指纹和pin来源扫描复用。
def _walk_files(root: Path, *, include_dirs: bool = False):
    watch_pin_source(root)
    try:
        info = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("retention root is not a regular directory")
    # LLM: 遍历器回调不容忍权限等错误，防止静默漏文件。
    # 函数用途: 将目录遍历错误传回扫描调用方。
    def failed(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        watch_pin_source(base)
        for name in sorted(dirs):
            child = base / name
            if child.is_symlink():
                raise OSError("retention never follows symbolic links")
            if include_dirs:
                yield child
        for name in sorted(files):
            child = base / name
            if child.is_symlink():
                raise OSError("retention never follows symbolic links")
            yield child


# LLM: pin为单次扫描临时投影，不是第二份权威；仅解析结构化引用字段，绝不解析正文。
# 类用途: 保存被引用的路径和记录ID，供扫描与执行阶段统一保护文件。
@dataclass
class _RetentionPins:
    home: Path
    paths: set[Path] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)
    complete: bool = True
    target_rebuild: Any = field(default=None, repr=False)
    verified_targets: set[Path] = field(default_factory=set, repr=False)

    # LLM: 只为确切目标重核全体来源；仅开读前追加可采用新FD快照，损坏/不可读/读中变化始终False。
    # 函数用途: 将无关的读取起点竞态与目标保护解耦，绝不从源位置猜没有引用。
    def complete_for(self, path: Path, errors: list[MemoryRetentionError]) -> bool:
        target = path.resolve(strict=False)
        if self.complete or target in self.verified_targets:
            return True
        if self.target_rebuild is None:
            return False
        with target_pin_read():
            recovered, complete = self.target_rebuild(errors)
        if not complete:
            return False
        self.paths.update(recovered.paths)
        self.ids.update(recovered.ids)
        self.verified_targets.add(target)
        return True

    # LLM: 只收引用元数据；相对路径同时按owner和来源目录解释，保守多保护、不扩大删除范围。
    # 函数用途: 递归收集引用容器里的路径和ID，忽略摘要等正文。
    def collect(self, value: object, source: Path, *, reference: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"content", "summary", "quote", "text", "reason", "note"}:
                    continue
                selected = reference or key in {"source", "path", "file_path", "cold_detail_ref"} or any(
                    part in key for part in ("_ref", "_source", "checkpoint", "legal_hold"))
                self.collect(item, source, reference=selected)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.collect(item, source, reference=reference)
        elif reference and isinstance(value, str) and value:
            self.ids.add(value)
            text = value.removeprefix("file://").removeprefix("file:").split("#", 1)[0]
            if "/" in text or text.endswith((".json", ".jsonl")):
                path = Path(text)
                roots = (self.home, source.parent) if not path.is_absolute() else (Path("/"),)
                self.paths.update((root / path).resolve(strict=False) for root in roots)

    # LLM: 路径重叠双向保护；content_path仅供trash读取真实payload，不读取墓碑声明的外部原路径。
    # 函数用途: 判断文件或目录是否被引用；坏记录不按零引用处理。
    def protects(self, path: Path, errors: list[MemoryRetentionError], *, content_path: Path | None = None) -> bool:
        resolved = path.resolve(strict=False)
        if any(resolved == pin or pin in resolved.parents or resolved in pin.parents for pin in self.paths):
            return True
        if path.name in self.ids or path.stem in self.ids:
            return True
        if not self.ids or path.suffix not in {".json", ".jsonl"}:
            return False
        try:
            with closing(_pin_rows(content_path or path, jsonl=path.suffix == ".jsonl")) as rows:
                return any(_row_has_pinned_id(row, self.ids) for row in rows)
        except (OSError, ValueError, TypeError):
            errors.append(_state_error("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", path))
            return True


# LLM: ID只取结构化身份字段；不搜索content/summary字符串，引用不能成为语义授权。
# 函数用途: 检查过期Daily/Compact行身份是否仍被引用。
def _row_has_pinned_id(value: object, ids: set[str]) -> bool:
    if isinstance(value, list):
        return any(_row_has_pinned_id(item, ids) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if (key == "id" or key.endswith("_id")) and isinstance(item, str) and item in ids:
            return True
        if isinstance(item, (dict, list)) and _row_has_pinned_id(item, ids):
            return True
    return False


# LLM: 仅源代次变化可重试，权限/坏JSON不靠重试漂白；filename用于只隔离依赖域。
# 类用途: 区分正常并发追加与真正不可读取的pin来源。
class _PinReadChanged(OSError):
    pass


# LLM: 正常读须完整代次一致；目标复核只容同inode在open前追加，以打开后FD为新起点，首末变化/缩短仍拒绝。
# 函数用途: 流式严格读取pin来源；提前停止者须close，函数不创建锁或写文件。
def _pin_rows(path: Path, *, required: bool = False, jsonl: bool | None = None):
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        watch_pin_source(path)
        if required:
            raise
        return
    if not stat.S_ISREG(before.st_mode):
        raise OSError("pin source is not a regular file")
    with io.TextIOWrapper(_fingerprint_source(path), encoding="utf-8") as source:
        opened = os.fstat(source.fileno())
        if target_pin_read_enabled() and (before.st_dev, before.st_ino, before.st_mode) == (
                opened.st_dev, opened.st_ino, opened.st_mode) and opened.st_size >= before.st_size:
            before = opened
        if _file_identity(before) != _file_identity(opened):
            raise _PinReadChanged(11, "pin source changed before reading", str(path))
        is_jsonl = jsonl if jsonl is not None else path.suffix == ".jsonl"
        rows = (json.loads(line) for line in source if line.strip()) if is_jsonl else (json.load(source),)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("pin source contains non-object rows")
            yield row
        if _file_identity(before) != _file_identity(os.fstat(source.fileno())):
            raise _PinReadChanged(11, "pin source changed while reading", str(path))
    if _file_identity(before) != _file_identity(path.stat(follow_symlinks=False)):
        raise _PinReadChanged(11, "pin source changed while reading", str(path))
    watch_pin_source(path)


# LLM: JSON对象入口完整耗尽读取器以核对首末FD和路径，不允许未验证首行充当权威状态。
# 函数用途: 读取唯一pin状态对象，缺失或多对象拒绝。
def _pin_object(path: Path) -> dict[str, Any]:
    rows = list(_pin_rows(path, required=True))
    if len(rows) != 1:
        raise ValueError("pin source requires exactly one object")
    return rows[0]


# LLM: 墓碑声明只作pin路径映射，正文只读当前容器payload；坏墓碑或坏正文关闭purge，绝不读source外部文件。
# 函数用途: 保护回收站中仍被原路径、实际路径或记录ID引用的材料。
def trash_payload_pinned(pins: _RetentionPins, container: Path, errors: list[MemoryRetentionError]) -> bool:
    try:
        row = _pin_object(container / "tombstone.json")
        original = row.get("source_path")
        if row.get("status") != "trashed" or not isinstance(original, str) or not original:
            return True
        payload = container / "payload"
        return pins.protects(payload, errors) or pins.protects(Path(original), errors, content_path=payload)
    except (OSError, ValueError, TypeError, IndexError):
        errors.append(_state_error("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", container))
        return True


# LLM: 正式记忆复用原物化的版本/墓碑语义；坏schema拒绝，已删除记录不保留旧引用。
# 函数用途: 收集仍有效正式记忆的来源引用。
def _formal_memory_pins(home: object, pins: _RetentionPins, now: datetime) -> None:
    from .jsonl import _materialize_memory_events, _memory_record_from_obj

    path = Path(home.owner_memory_long_term_jsonl)
    events = []
    for row in _pin_rows(path):
        record = _memory_record_from_obj(row)
        if record is None:
            raise ValueError("formal pin source schema is invalid")
        events.append(record)
    for record in _materialize_memory_events(events, include_expired=True):
        if not record.expires_at or record.expires_at > now.timestamp():
            pins.collect({"source": record.source, "attributes": record.attributes}, path)


# LLM: 发现监视work/缺失state；法律保留优先，runs的DB终态只解除非hold运行的隐式根保护，显式证据仍保留。
# 函数用途: 收集规范恢复任务状态引用与受保护恢复根。
def _task_reference_pins(home: object, policy: MemoryRetentionPolicy, pins: _RetentionPins, now: datetime) -> None:
    for _root, path in _iter_task_states(home):
        row = _pin_object(path)
        status = str(row.get("status") or "").upper()
        if (status == "RUNNING" and path.is_relative_to(Path(home.owner_home_dir) / "runs")
                and row.get("task_id") not in policy.legal_hold_task_ids
                and runtime_terminal(home, path.parent.parent, row, now) is not None):
            pins.collect(row, path)
            continue
        if status not in TASK_TERMINAL_STATUSES or row.get("task_id") in policy.legal_hold_task_ids:
            pins.paths.add(path.parent.parent.resolve(strict=False))
            pins.collect(row, path)


# LLM: 线程head存在则整份checkpoint账本及所有来源保护；不凭mtime判断线程是否失活。
# 函数用途: 收集线程、活跃任务以及摘要基础引用，缺失检查点按来源损坏拒绝。
def _conversation_reference_pins(home: object, roots: tuple[Path, ...], pins: _RetentionPins) -> None:
    for root in roots:
        for path in _walk_files(root / "threads"):
            if path.suffix != ".json":
                continue
            row = _pin_object(path)
            thread = ConversationThread.from_dict(row)
            if not thread.thread_id or thread.thread_id != path.stem:
                raise ValueError("pin thread identity mismatches")
            pins.collect(row, path)
            if thread.compact_checkpoint_id:
                checkpoint = Path(home.owner_compact_dir) / "conversations" / f"{thread.thread_id}.jsonl"
                pins.paths.add(checkpoint.resolve(strict=False))
                for item in _pin_rows(checkpoint, required=True):
                    pins.collect(item, checkpoint)


# LLM: 只读原curator审计和结构化warnings；版本标记整片pin保跨日prior_id链，坏源交外层关闭清理。
# 函数用途: 收集策展运行引用，同时保护检查点和增量所在的完整日分片。
def _curator_reference_pins(curator: Path, pins: _RetentionPins) -> None:
    for path in _walk_files(curator):
        if path.suffix not in {".json", ".jsonl"}:
            continue
        for row in _pin_rows(path, required=True):
            pins.collect(row, path)
            warnings = row.get("warnings", ())
            if isinstance(warnings, (list, tuple)) and any(
                isinstance(value, str) and value.startswith((_CURSOR_CHECKPOINT, _CURSOR_DELTA))
                for value in warnings
            ):
                pins.paths.add(path.resolve(strict=False))


# LLM: 正常读取最多三次退避；持续起点竞态保留目标重核入口，只有全源新FD快照完整才可解除特定域阻塞。
# 函数用途: 扫描和执行前重核完整证据引用与游标链，冷归档损坏时关闭相关清理而非当作没有引用。
def current_retention_pins(home: object, roots: tuple[Path, ...], errors: list[MemoryRetentionError],
                           *, now: datetime) -> tuple[_RetentionPins, bool]:
    for attempt in range(4):
        try:
            return _current_retention_pins_once(home, roots, errors, now=now)
        except _PinReadChanged as exc:
            if attempt < 3:
                time.sleep(0.01)
                continue
            errors.append(MemoryRetentionError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", str(exc.filename or ""),
                                              "pin file remained unstable after three retries"))
    # LLM: 仅由已证实代次竞争触发，不为坏JSON或权限错误增设重试；作用域限定本次调用与owner。
    # 函数用途: 在目标需要判定时严格重读全体来源一次。
    def rebuild(target_errors):
        try:
            return _current_retention_pins_once(home, roots, target_errors, now=now)
        except _PinReadChanged as exc:
            target_errors.append(_state_error("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", Path(exc.filename or home.owner_home_dir)))
            return _RetentionPins(Path(home.owner_home_dir), complete=False), False

    return _RetentionPins(Path(home.owner_home_dir), complete=False, target_rebuild=rebuild), False


# LLM: 单次流式读取仍严格校验全部来源；并发代次错误交外层有界重试，其余错误立即关闭相关引用域。
# 函数用途: 构建一份完整的临时引用投影，不物化JSONL大正文。
def _current_retention_pins_once(home: object, roots: tuple[Path, ...], errors: list[MemoryRetentionError],
                                 *, now: datetime) -> tuple[_RetentionPins, bool]:
    pins = _RetentionPins(Path(home.owner_home_dir))
    try:
        policy, error = _load_policy(Path(home.owner_retention_json))
        if error is not None or policy is None or policy.legal_hold:
            raise ValueError("pin protection policy is unavailable or held")
        _formal_memory_pins(home, pins, now)
        candidate_path = Path(home.owner_memory_candidates_jsonl)
        candidate_ids: set[str] = set()
        for row in _pin_rows(candidate_path):
            candidate = MemoryCandidate.from_record(row)
            candidate_id = candidate.candidate_id
            if candidate_id in candidate_ids:
                raise ValueError("duplicate candidate pin source")
            candidate_ids.add(candidate_id)
            if candidate.cold_detail_ref:
                watch_pin_source(candidate_path.parent / candidate.cold_detail_ref.partition("#")[0])
                candidate = _hydrate_candidate(candidate_path, candidate)
                row = candidate.to_record()
            pins.collect(row, candidate_path)
        _task_reference_pins(home, policy, pins, now)
        _conversation_reference_pins(home, _conversation_roots(home, roots), pins)
        curator = Path(home.owner_memory_curator_runs_dir)
        _curator_reference_pins(curator, pins)
        for path in (curator.parent / "state.json",):
            for row in _pin_rows(path):
                pins.collect(row, path)
    except _PinReadChanged:
        raise
    except (CandidateStoreCorruptError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        errors.append(MemoryRetentionError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", str(getattr(exc, "filename", "") or ""),
                                          type(exc).__name__))
        pins.complete = False
        return pins, False
    return pins, True


# LLM: 时间只用于保留期 cutoff，不参与长期事实冲突或真伪判定。
# 函数用途: 将 epoch 或 ISO 时间转换为 UTC epoch。
def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None


# LLM: naive 测试时间按 UTC 解释，生产默认始终 aware UTC。
# 函数用途: 规范当前 retention 运行时间。
def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# LLM: 路径包含检查使用 resolve，不接受简单字符串前缀。
# 函数用途: 判断目标是否位于一个权威根目录内。
def _safe_child(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(relative.parts)


# LLM: 已整体选中的 task 不再生成内部文件动作，避免双删和状态歧义。
# 函数用途: 判断一个文件是否落在任一排除目录内。
def _inside_any(path: Path, roots: frozenset[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: mtime 读取失败返回正无穷，保证扫描不会把不可 stat 文件误判为很旧。
# 函数用途: 读取文件修改时间用于按龄类别。
def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


# LLM: 状态错误保持稳定 code/path，不泄露状态文件正文。
# 函数用途: 构造一条状态损坏诊断。
def _state_error(code: str, path: Path) -> MemoryRetentionError:
    return MemoryRetentionError(code, str(path), "structured retention state is invalid")


__all__ = [
    "build_retention_plan",
    "file_fingerprint",
    "paths_fingerprint",
    "tree_fingerprint",
]
