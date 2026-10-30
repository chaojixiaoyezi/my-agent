
# LLM: live枚举沿原rg/有界后备，snapshot按宿主权限值绑定并允许Registry请求级浅拷贝续页；逐页复核只提供best_effort一致性。
# 模块用途: 按glob查找文件并支持绑定来源的冻结分页，保留取消、覆盖披露与资源回收。
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.cancellation import ToolCancelled, raise_if_cancelled, register_cancellation_callback
from ._filesystem_helpers import (
    _FALLBACK_SCAN_MAX_FILES,
    _FALLBACK_SCAN_MAX_SECONDS,
    _bool_param,
    _discovery_file_batch,
    _discovery_identity,
    _discovery_matches_glob,
    _discovery_nofollow_stat,
    _int_param,
    _iter_discovery_files,
    _optional_path,
    _safe_discovery_path,
    _text_param,
    path_resolution_error_outcome,
)
from ._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    FileSystemAccessOptions,
    FileSystemTool,
)
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import (
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_LOGGER = logging.getLogger(__name__)

_FIND_SNAPSHOT_TTL_SECONDS = 60.0
_FIND_SNAPSHOT_MAX_CACHED = 4
_FIND_SNAPSHOT_MAX_CURSORS = 128
_FIND_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024


# LLM: 冻结后只保留不可变路径、筛选及权限值指纹元组，不是文件系统强快照或执行授权。
# 类用途: 保存有界清单的绑定、寿命及真实扫描覆盖。
@dataclass(frozen=True)
class _FindSnapshot:
    snapshot_id: str
    binding: tuple
    filters: tuple[str, bool]
    paths: tuple[Path, ...]
    watches: tuple[tuple[Path, tuple[int, ...]], ...]
    expires_at: float
    scan_exhausted: bool
    limit_reason: str
    scanned_files: int


# LLM: 已观察来源或权限变化必须阻断旧清单续页，不能隐式换代。
# 类用途: 区分快照失效与普通扫描触顶。
class _FindSnapshotStale(RuntimeError):
    pass


# LLM: 触顶只允许交付已披露partial的冻结前缀，不能冒充扫描到尾。
# 类用途: 中止超过清单存储预算的快照构建。
class _FindSnapshotLimit(RuntimeError):
    pass


# LLM: 策略字段已由宿主构造规范化，只取不可变值而非请求级对象地址；危险根按集合定序，逐页仍独立授权。
# 函数用途: 生成跨等值策略实例稳定、任一权限字段变化即不同的值指纹。
def _find_policy_fingerprint(tool) -> tuple:
    policy = tool.path_access_policy
    return (
        'path_access_policy.v1', policy.mode,
        tuple(sorted({str(root) for root in policy.dangerous_roots})),
        str(policy.owner_scope_root) if policy.owner_scope_root is not None else None,
        str(policy.agent_home_root) if policy.agent_home_root is not None else None,
    )


# LLM: 绑定只取工具宿主字段、权限值和回调身份；模型不能自报owner或权限代次，逐页仍独立核验原策略。
# 函数用途: 将root、筛选、忽略版本与当前权限值绑定到同一冻结清单。
def _find_snapshot_binding(tool, target: Path, request) -> tuple:
    check = tool.check_path_access
    resolve = tool.resolve_path
    return (
        'find_snapshot.v1', tool._snapshot_namespace, str(target),
        _discovery_identity(_discovery_nofollow_stat(target))[:2],
        request.pattern, request.include_ignored,
        tuple(sorted(_COMMON_FILE_DISCOVERY_IGNORES)), 'unicode_codepoint.v1',
        _find_policy_fingerprint(tool),
        tuple(map(str, tool.granted_external_roots)),
        str(tool.workspace_root), tuple(map(str, tool.workspace_roots)),
        str(tool.protected_persona_root),
        id(getattr(check, '__func__', check)), id(getattr(resolve, '__func__', resolve)),
        os.geteuid() if hasattr(os, 'geteuid') else None,
        tuple(sorted(os.getgroups())) if hasattr(os, 'getgroups') else (),
    )


# LLM: 沿已有STALE_VERSION错误分类与原reported字段披露快照细类，不篡改归一化后的错误码或共享错误表。
# 函数用途: 返回必须显式新建快照、不能重扫接页的失效结果。
def _find_snapshot_stale(reason: str, snapshot_id: str = '') -> ToolHandlerOutcome:
    return ToolHandlerOutcome('find_files', False,
        '冻结文件清单已失效；旧cursor不能继续。请移除cursor后显式重新请求snapshot。',
        error_code='STALE_VERSION', reported_error_code='FILE_SNAPSHOT_STALE',
        result_envelope={'snapshot': {
            'snapshot_id': snapshot_id, 'next_cursor': '', 'consistency': 'stale',
            'scan_exhausted': False, 'limit_reason': reason, 'partial': True,
        }})


def _find_watch_identity(path: Path) -> tuple[int, ...]:
    info = _discovery_nofollow_stat(path)
    base = _discovery_identity(info)
    if not stat.S_ISDIR(info.st_mode):
        return base
    entries_count = 0
    entries_xor = 0
    entries_sum = 0
    with os.scandir(path) as entries:
        for entry in entries:
            entries_count += 1
            digest = int.from_bytes(
                hashlib.blake2b(os.fsencode(entry.name), digest_size=16).digest(), "little"
            ) ^ int(entry.inode())
            entries_xor ^= digest
            entries_sum = (entries_sum + digest) & 0xFFFFFFFFFFFFFFFF
    return (*base, entries_count, entries_xor, entries_sum)


# LLM: 筛选串线单独披露；每个已观察目录及文件仍重核原权限和五元指纹，不能以绑定相等或根mtime替代，I/O在锁外。
# 函数用途: 在发布或续页前验证冻结清单的来源与权限未观察到变化。
def _validate_find_snapshot(tool, target: Path, request, snapshot: _FindSnapshot) -> None:
    if time.monotonic() >= snapshot.expires_at:
        raise _FindSnapshotStale('expired')
    if (request.pattern, request.include_ignored) != snapshot.filters:
        raise _FindSnapshotStale('filter_mismatch')
    if _find_snapshot_binding(tool, target, request) != snapshot.binding:
        raise _FindSnapshotStale('binding_changed')
    started = time.monotonic()
    for path, expected in snapshot.watches:
        raise_if_cancelled()
        if time.monotonic() - started >= _FALLBACK_SCAN_MAX_SECONDS:
            raise _FindSnapshotStale('validation_budget')
        safe = _safe_discovery_path(tool, path, root=target, include_ignored=request.include_ignored,
                                    ignored_names=_COMMON_FILE_DISCOVERY_IGNORES)
        if safe is None:
            raise _FindSnapshotStale('permission_or_path_changed')
        if _find_watch_identity(safe) != expected:
            raise _FindSnapshotStale('source_changed')
    if _find_snapshot_binding(tool, target, request) != snapshot.binding:
        raise _FindSnapshotStale('binding_changed')


# LLM: 同步声明live offset与显式冻结cursor参数，不允许模型用cursor扩大权限。
# 函数用途: 构建文件查找的模型参数合同。
def _build_find_files_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="find_files",
        description="按 glob 查找文件路径，适合不知道文件具体位置但知道文件名模式时使用。",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "必填；glob 模式，例如 *.py、**/*.md、src/**/*.ts。匹配路径或文件名，不会打开正文。",
                },
                "path": {
                    "type": "string",
                    "description": "可选；从哪个目录开始找，默认工作区根目录。缩小范围会更快。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "分页大小；结果很多时先看一小页，再用 next_offset 继续。",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "上一页返回 next_offset 后，下一次传入这里继续看。",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "是否包含 .git、node_modules 和常见缓存目录；默认 false。",
                },
                "snapshot": {
                    "type": "boolean",
                    "default": False,
                    "description": "显式冻结有界有序清单；可变目录一致性为best_effort，触顶披露partial。",
                },
                "cursor": {
                    "type": "string",
                    "description": "冻结分页的不透明凭证，不能与offset并用；失效后显式新建snapshot。",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=(
                "找所有 Python、Markdown、配置或测试文件",
                "按文件名模式定位候选文件，再用 read_file 阅读",
            ),
            avoid_when=(
                "只是想看某个目录下一层有什么时，用目录查看工具",
                "想搜索文件正文内容时，用正文搜索工具",
            ),
            keywords=("find", "glob", "文件查找", "按模式找文件", "找文件", "文件名"),
            examples=(
                '{"tool": "find_files", "pattern": "**/*.py"}',
                '{"tool": "find_files", "pattern": "*.md", "path": "docs", "limit": 50}',
            ),
        ),
    )


# LLM: 快照凭证仅由注册handler及其请求级浅拷贝共享，不能跨owner或独立注册实例使用；原路径策略仍逐页生效。
# 类用途: 提供live查找和显式冻结文件路径分页。
class FindFilesTool(FileSystemTool):

    model_spec = _build_find_files_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        promotes_task=True,
    )

    # LLM: 仅创建有帽内存缓存，不落盘快照或借用其他工具实例的owner身份。
    # 函数用途: 初始化查找上限与实例私有分页状态。
    def __init__(
        self,
        workspace_root: Path,
        max_matches: int,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_matches = max_matches
        self._snapshot_namespace = secrets.token_urlsafe(24)
        self._snapshot_lock = threading.RLock()
        self._snapshots: OrderedDict[str, _FindSnapshot] = OrderedDict()
        self._snapshot_cursors: OrderedDict[str, tuple[str, int, float]] = OrderedDict()

    # LLM: 参数失败不静默回退；旧cursor路径或权限失效只能返回stale，不重新扫描接页。
    # 函数用途: 校验请求并分派live与冻结分页。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = _find_files_request_from_params(params, self.max_matches)
        except ValueError as exc:
            return path_resolution_error_outcome('find_files', exc)
        try:
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            if request.cursor:
                return _find_snapshot_stale('path_or_permission_changed')
            # 参数/路径解析失败→TOOL_INVALID_ARGUMENTS(改参可修)；路径策略拒绝→原权限码+authorization。
            return path_resolution_error_outcome("find_files", exc)
        if request.cursor:
            return self._find_snapshot_page(target, request)
        if not target.exists():
            # 目标路径不存在是状态问题，应带 PATH_NOT_FOUND + candidate_paths(与 list/search 一致)，
            # 而非无码兜底 UNKNOWN_ERROR——后者让模型放弃，前者引导改用候选路径或先 list_files 定位。
            return missing_path_result(MissingPathRequest(
                tool_name="find_files",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="find_files",
            decide_access=self.check_path_access,
            ))
        if request.snapshot:
            return self._find_snapshot_page(target, request)
        if target.is_file():
            return self._find_in_single_file(target, request)
        return self._find_in_directory(target, request)

    # LLM: 单文件遵守offset，不能在offset大于零时重复交出唯一结果。
    # 函数用途: 保留单文件live查找及原输出形状。
    def _find_in_single_file(self, target: Path, request: _FindFilesRequest) -> ToolHandlerOutcome:
        if request.offset == 0 and _matches_find_pattern(self.display_path(target), target.name, request.pattern):
            return ToolHandlerOutcome(
                "find_files",
                True,
                self.display_path(target),
                result_envelope={
                    "page_window": _offset_page_window(
                        _OffsetPageWindowRequest(
                            source_path=self.display_path(target),
                            offset=request.offset,
                            limit=request.limit,
                            returned=1,
                            has_more=False,
                        )
                    )
                },
            )
        return ToolHandlerOutcome(
            "find_files",
            True,
            "没有找到匹配文件",
            result_envelope={
                "page_window": _offset_page_window(
                    _OffsetPageWindowRequest(
                        source_path=self.display_path(target),
                        offset=request.offset,
                        limit=request.limit,
                        returned=0,
                        has_more=False,
                    )
                )
            },
        )

    # LLM: 省略include_ignored等同false，零结果不隐式扩大第二次扫描。
    # 函数用途: 仅执行原目录请求的live分页。
    def _find_in_directory(self, target: Path, request: _FindFilesRequest) -> ToolHandlerOutcome:
        batch = _find_directory_matches(self, target, request)
        return _find_directory_result(
            _FindDirectoryResultRequest(
                self,
                target,
                request,
                batch,
            )
        )


    # LLM: 调用方持实例缓存锁；只操作有帽元数据，不做stat、遍历或正文复制。
    # 函数用途: 清理到期快照及无效服务器凭证。
    def _prune_snapshots(self) -> None:
        now = time.monotonic()
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.expires_at <= now:
                del self._snapshots[key]
        for token, (key, _, expiry) in list(self._snapshot_cursors.items()):
            if expiry <= now or key not in self._snapshots:
                del self._snapshot_cursors[token]
        while len(self._snapshots) > _FIND_SNAPSHOT_MAX_CACHED:
            self._snapshots.popitem(last=False)
        while len(self._snapshot_cursors) > _FIND_SNAPSHOT_MAX_CURSORS:
            self._snapshot_cursors.popitem(last=False)

    # LLM: 构建固定筛选和权限值绑定，受文件数、目录项、时间及保留字节预算约束，不用首屏哨兵冒充完整快照。
    # 函数用途: 在缓存锁外构建按根相对Unicode路径排序的冻结清单。
    def _build_snapshot(self, target: Path, request: _FindFilesRequest) -> _FindSnapshot:
        binding = _find_snapshot_binding(self, target, request)
        state = _FindScanState(started_at=time.monotonic())
        watches: dict[Path, tuple[int, ...]] = {}
        paths: list[Path] = []
        retained = 0

        # LLM: 入队、下降及扫描后重复观察同一目录，不把末尾一次根stat当作强快照。
        # 函数用途: 收集来源身份并拒绝已观察到的构建期间漂移。
        def observe(path: Path) -> None:
            nonlocal retained
            identity = _find_watch_identity(path)
            previous = watches.get(path)
            if previous is not None:
                if previous != identity:
                    raise _FindSnapshotStale('changed_during_scan')
                return
            cost = len(str(path).encode('utf-8', 'surrogatepass')) + 256
            if retained + cost > _FIND_SNAPSHOT_MAX_BYTES:
                raise _FindSnapshotLimit()
            retained += cost
            watches[path] = identity

        try:
            for item in _iter_discovery_files(self, target,
                    include_ignored=request.include_ignored, ignored_names=_COMMON_FILE_DISCOVERY_IGNORES,
                    scan_state=state, admit=_admit_find_candidate, observe=observe):
                raise_if_cancelled()
                if not _matches_find_pattern(self.display_path(item), item.name, request.pattern):
                    continue
                cost = len(str(item).encode('utf-8', 'surrogatepass')) + 64
                if retained + cost > _FIND_SNAPSHOT_MAX_BYTES:
                    raise _FindSnapshotLimit()
                retained += cost
                paths.append(item)
        except _FindSnapshotLimit:
            state.limited = True
            state.limit_reason = 'manifest_byte_limit'
        snapshot = _FindSnapshot(
            snapshot_id=secrets.token_urlsafe(24), binding=binding,
            filters=(request.pattern, request.include_ignored),
            paths=tuple(sorted(paths, key=lambda path: path.relative_to(target).as_posix())),
            watches=tuple(watches.items()), expires_at=time.monotonic() + _FIND_SNAPSHOT_TTL_SECONDS,
            scan_exhausted=not state.limited, limit_reason=state.limit_reason, scanned_files=state.candidates)
        _validate_find_snapshot(self, target, request, snapshot)
        return snapshot

    # LLM: 调用方持缓存锁；凭证的位置与期限只存本实例，不允许模型编辑offset签名。
    # 函数用途: 给同一冻结清单后续位置签发不透明续页凭证。
    def _snapshot_cursor(self, snapshot: _FindSnapshot, position: int) -> str:
        for token, value in self._snapshot_cursors.items():
            if value == (snapshot.snapshot_id, position, snapshot.expires_at):
                return token
        token = secrets.token_urlsafe(32)
        self._snapshot_cursors[token] = (snapshot.snapshot_id, position, snapshot.expires_at)
        self._prune_snapshots()
        return token

    # LLM: 锁内只取引用/交换有帽缓存；构建、来源验证及权限检查全部锁外，缓存被淘汰时明确stale而不重扫。
    # 函数用途: 处理首个冻结请求或同一清单的cursor续页。
    def _find_snapshot_page(self, target: Path, request: _FindFilesRequest) -> ToolHandlerOutcome:
        snapshot = None
        try:
            if request.cursor:
                with self._snapshot_lock:
                    self._prune_snapshots()
                    credential = self._snapshot_cursors.get(request.cursor)
                    if credential is None:
                        return _find_snapshot_stale('cursor_invalid_or_expired')
                    key, position, expiry = credential
                    snapshot = self._snapshots.get(key)
                if snapshot is None or time.monotonic() >= expiry:
                    return _find_snapshot_stale('snapshot_missing_or_expired', key)
            else:
                position = request.offset
                snapshot = self._build_snapshot(target, request)
                with self._snapshot_lock:
                    self._prune_snapshots()
                    self._snapshots[snapshot.snapshot_id] = snapshot
                    self._prune_snapshots()
            _validate_find_snapshot(self, target, request, snapshot)
            page = snapshot.paths[position:position + request.limit]
            rows: list[str] = []
            for item in page:
                raise_if_cancelled()
                safe = _safe_discovery_path(self, item, root=target,
                    include_ignored=request.include_ignored, ignored_names=_COMMON_FILE_DISCOVERY_IGNORES)
                if safe is None:
                    raise _FindSnapshotStale('page_path_denied')
                rows.append(self.display_path(safe))
            _validate_find_snapshot(self, target, request, snapshot)
            end = position + len(rows)
            has_more = end < len(snapshot.paths)
            with self._snapshot_lock:
                self._prune_snapshots()
                if self._snapshots.get(snapshot.snapshot_id) is not snapshot:
                    return _find_snapshot_stale('snapshot_evicted', snapshot.snapshot_id)
                next_cursor = self._snapshot_cursor(snapshot, end) if has_more else ''
            page_request = replace(request, offset=position)
            batch = _FindMatchBatch(rows, end, has_more, snapshot.scan_exhausted,
                'python_snapshot', snapshot.scanned_files, True, snapshot.limit_reason)
            envelope = _find_result_envelope(_FindDirectoryResultRequest(self, target, page_request, batch))
            envelope['snapshot'] = {
                'snapshot_id': snapshot.snapshot_id, 'next_cursor': next_cursor,
                'consistency': 'best_effort', 'scan_exhausted': snapshot.scan_exhausted,
                'limit_reason': snapshot.limit_reason, 'partial': not snapshot.scan_exhausted,
                'ordering': 'root_relative_unicode_codepoint',
            }
            envelope['page_window'].update({'kind': 'snapshot_page', 'next_offset': 0, 'next_cursor': next_cursor})
            output = '\n'.join(rows) or '没有找到匹配文件'
            if has_more:
                output += f'\n... 冻结分页，next_cursor={next_cursor}'
            if not snapshot.scan_exhausted:
                output += (f'\n文件查找未完整覆盖：{snapshot.limit_reason}；cursor仅覆盖冻结前缀，'
                           '不能据此认定工作区完全无其他匹配。')
            return ToolHandlerOutcome('find_files', True, output, result_envelope=envelope)
        except (OSError, ValueError, _FindSnapshotStale) as exc:
            key = snapshot.snapshot_id if snapshot is not None else ''
            if key:
                with self._snapshot_lock:
                    if self._snapshots.get(key) is snapshot:
                        self._snapshots.pop(key, None)
            reason = str(exc) if isinstance(exc, _FindSnapshotStale) else 'source_unavailable'
            return _find_snapshot_stale(reason, key)


# LLM: 结果只对应一次明确忽略策略的查询，不再携带无调用方的隐式扩大分支。
# 类用途: 打包实际文件查询与覆盖事实。
@dataclass(frozen=True)
class _FindDirectoryResultRequest:
    tool: FindFilesTool
    target: Path
    request: _FindFilesRequest
    batch: _FindMatchBatch


@dataclass(frozen=True)
class _OffsetPageWindowRequest:
    source_path: str
    offset: int
    limit: int
    returned: int
    has_more: bool
    scan_complete: bool = True


# LLM: This is one bounded file-discovery execution result. scan_complete is
# execution coverage, not a claim about repository or task completeness.
# 类用途: 保存文件查找的一页结果、后端和扫描边界，避免把受限空结果误报成“没有文件”。
@dataclass(frozen=True)
class _FindMatchBatch:
    results: list[str]
    seen: int
    has_more: bool
    scan_complete: bool
    backend: str
    scanned_files: int
    scanned_files_known: bool
    limit_reason: str = ""


# LLM: The Python fallback is deliberately bounded; hitting the boundary must
# be projected as incomplete and must never be converted into a no-match fact.
# 类用途: 记录没有 rg 时已经扫描多少文件，并在目录过大时及时停下而不是卡住 Gateway。
@dataclass
class _FindScanState:
    started_at: float
    candidates: int = 0
    limited: bool = False
    limit_reason: str = ""


def _find_directory_matches(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch:
    rg_batch = _find_directory_matches_with_rg(tool, target, request)
    if rg_batch is not None:
        return rg_batch
    return _find_directory_matches_with_python(tool, target, request)


# LLM: 后备遍历按稳定目录序流式交付候选，见当前页哨兵即停止，资源触顶必须明确披露而非宣称无匹配。
# 函数用途: 在系统没有 rg 时逐个遍历文件；够一页就返回，避免先把整个用户目录装进内存再排序。
def _find_directory_matches_with_python(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch:
    results: list[str] = []
    seen = 0
    has_more = False
    scan_state = _FindScanState(started_at=time.monotonic())
    for item in _iter_find_candidates(
        target,
        tool=tool,
        include_ignored=request.include_ignored,
        scan_state=scan_state,
    ):
        # H2：后备遍历不交出宿主托管存储里的文件，与路径策略同一判定（rg 路径逐条经 resolve_path 已拒绝）。
        if tool.path_access_policy.host_managed_store(item) is not None:
            continue
        rel = tool.display_path(item)
        if not _matches_find_pattern(rel, item.name, request.pattern):
            continue
        if seen < request.offset:
            seen += 1
            continue
        if len(results) >= request.limit:
            has_more = True
            break
        results.append(rel)
        seen += 1
    return _FindMatchBatch(
        results=results,
        seen=seen,
        has_more=has_more,
        scan_complete=not has_more and not scan_state.limited,
        backend="python",
        scanned_files=scan_state.candidates,
        scanned_files_known=True,
        limit_reason=scan_state.limit_reason,
    )


# LLM: rg只接收共同发现策略准入的有帽文件批次，不自行进入任何目录；枚举预算原样披露。
# 函数用途: 在宿主完成权限剪枝后使用原生程序输出当前分页。
def _find_directory_matches_with_rg(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch | None:
    if not shutil.which('rg'):
        return None
    state = _FindScanState(started_at=time.monotonic())
    candidates = _discovery_file_batch(tool, target, include_ignored=request.include_ignored,
        ignored_names=_COMMON_FILE_DISCOVERY_IGNORES, scan_state=state,
        admit=_admit_find_candidate)
    if not candidates:
        return _FindMatchBatch([], 0, False, not state.limited, 'rg', state.candidates, True,
                               state.limit_reason)
    process = _start_rg_find_process(candidates)
    if process is None:
        return None
    with register_cancellation_callback(lambda: _stop_find_process(process)):
        batch = _read_rg_find_process(tool, target, request, process)
    if batch is None:
        return None
    return replace(batch, scan_complete=batch.scan_complete and not state.limited,
                   scanned_files=state.candidates, scanned_files_known=True,
                   limit_reason=state.limit_reason)


# LLM: 子进程只接收已经准入的类型化路径，不执行shell命令；其生命周期由当前工具取消令牌负责。
# 函数用途: 启动不经过 shell 的 rg 文件枚举进程；启动失败时让调用方走 Python 后备。
def _start_rg_find_process(
    candidates: list[Path],
) -> subprocess.Popen[str] | None:
    rg_path = shutil.which("rg")
    if not rg_path:
        return None
    args = [
        rg_path,
        "--files",
        "--hidden",
        "--no-ignore",
        "--no-config",
        "--no-messages",
        "--no-follow",
        "--sort",
        "path",
    ]
    args.extend(["--", *(str(path) for path in candidates)])
    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        _LOGGER.debug("rg file discovery failed to start; using Python fallback", exc_info=True)
        return None


# LLM: 输出只消费至offset+limit+1哨兵；每条路径在交给模型前都必须经过原规范访问策略解析。
#   读线程finally负责有界回收与关闭stdout，包括BaseException；异步取消不得关闭正在迭代的流。
#   取消异常原样传递，普通读取异常走Python后备，不重复调用发生异常的检查函数。
# 函数用途: 读取 rg 的一页文件结果；看到下一页哨兵后立刻结束进程，并保持路径权限校验。
def _read_rg_find_process(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
    process: subprocess.Popen[str],
) -> _FindMatchBatch | None:
    stdout = process.stdout
    if stdout is None:
        _stop_find_process(process)
        return None
    results: list[str] = []
    seen = 0
    has_more = False
    stopped_early = False
    try:
        for raw_line in stdout:
            raise_if_cancelled()
            raw_path = Path(raw_line.rstrip("\r\n"))
            if not raw_path.name:
                continue
            candidate = raw_path if raw_path.is_absolute() else target / raw_path
            try:
                safe_candidate = _safe_discovery_path(tool, candidate, root=target,
                    include_ignored=request.include_ignored, ignored_names=_COMMON_FILE_DISCOVERY_IGNORES)
            except OSError:
                continue
            if safe_candidate is None:
                continue
            if not _matches_find_pattern(tool.display_path(safe_candidate), safe_candidate.name, request.pattern):
                continue
            if seen < request.offset:
                seen += 1
                continue
            if len(results) >= request.limit:
                has_more = True
                stopped_early = True
                _stop_find_process(process)
                break
            results.append(tool.display_path(safe_candidate))
            seen += 1
        return_code = process.wait(timeout=2)
    except ToolCancelled:
        raise
    except Exception:
        _LOGGER.debug("rg file discovery failed while reading; using Python fallback", exc_info=True)
        return None
    finally:
        try:
            _stop_find_process(process)
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
    if not stopped_early and return_code not in {0, 1}:
        return None
    return _FindMatchBatch(
        results=results,
        seen=seen,
        has_more=has_more,
        scan_complete=not has_more,
        backend="rg",
        scanned_files=0,
        scanned_files_known=False,
    )


# LLM: TERM/KILL后wait均有界；kill后再次wait回收僵尸，不关闭主读线程正在迭代的stdout。
# 函数用途: 结束当前文件查找子进程并回收，普通完成时仅poll，不让取消回调争抢读管道。
def _stop_find_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(signal, "SIGKILL"):
            process.kill()
    except OSError:
        return
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=1)


# LLM: 只渲染真实分页与覆盖，不生成自动扩大忽略范围的旧提示。
# 函数用途: 生成非空文件查找结果。
def _find_directory_result(result: _FindDirectoryResultRequest) -> ToolHandlerOutcome:
    if not result.batch.results:
        return _find_directory_empty_result(
            result.tool,
            result.target,
            result.request,
            result.batch,
        )
    output_rows = list(result.batch.results)
    if result.batch.has_more:
        output_rows.append(
            f"... 已截断，next_offset={result.batch.seen} limit={result.request.limit}；继续查找请再次调用 find_files 并传入 offset={result.batch.seen}"
        )
    output = "\n".join(output_rows)
    envelope = _find_result_envelope(result)
    return ToolHandlerOutcome(
        "find_files",
        True,
        output,
        result_envelope=envelope,
    )


# LLM: 空结果仍区分完整与截断，不能借空结果启动第二次扩大范围查询。
# 函数用途: 生成可辨识的零匹配或不完整查找结果。
def _find_directory_empty_result(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
    batch: _FindMatchBatch | None = None,
) -> ToolHandlerOutcome:
    batch = batch or _FindMatchBatch([], 0, False, True, "single", 1, True)
    result = _FindDirectoryResultRequest(tool, target, request, batch)
    output = "没有找到匹配文件"
    if not batch.scan_complete:
        output = (
            "文件查找未完整覆盖；请缩小 path 或使用更具体的 pattern 后重试，"
            "不能据此认定工作区没有匹配文件"
        )
    return ToolHandlerOutcome(
        "find_files",
        True,
        output,
        result_envelope=_find_result_envelope(result),
    )


# LLM: 结果元数据区分命中、完整无匹配与扫描不完整，资源触顶不能升级为整个仓库不存在文件的结论。
# 函数用途: 给文件查找结果附上分页、后端和扫描完整性，供模型决定翻页还是缩小范围。
def _find_result_envelope(result: _FindDirectoryResultRequest) -> dict[str, object]:
    batch = result.batch
    envelope = {'page_window': _offset_page_window(
            _OffsetPageWindowRequest(
                source_path=result.tool.display_path(result.target),
                offset=result.request.offset,
                limit=result.request.limit,
                returned=len(batch.results),
                has_more=batch.has_more,
                scan_complete=batch.scan_complete,
            )
        )}
    status = "matches" if batch.results else ("no_matches" if batch.scan_complete else "incomplete")
    envelope["file_search_result"] = {
        "schema": "local_file_search.v1",
        "scope": "local_workspace",
        "source_path": result.tool.display_path(result.target),
        "backend": batch.backend,
        "status": status,
        "scan_complete": batch.scan_complete,
        "scanned_files": batch.scanned_files,
        "scanned_files_known": batch.scanned_files_known,
        "scan_limit_reason": batch.limit_reason,
        "suggested_actions": ["narrow_path", "use_specific_pattern"] if status == "incomplete" else [],
    }
    return envelope


# LLM: 规范化请求只携原筛选与显式快照参数，不接受模型自报owner/权限版本。
# 类用途: 固定live或冻结文件查找请求。
@dataclass(frozen=True)
class _FindFilesRequest:
    pattern: str
    raw_path: str
    limit: int
    offset: int
    include_ignored: bool
    snapshot: bool = False
    cursor: str = ''


# LLM: 新参数明确类型校验，cursor与显式offset冲突即拒绝，不猜续页位置。
# 函数用途: 将模型输入解析为规范查找请求。
def _find_files_request_from_params(params: dict[str, Any], max_matches: int) -> _FindFilesRequest:
    snapshot = params.get('snapshot', False)
    cursor = params.get('cursor', '')
    if not isinstance(snapshot, bool):
        raise ValueError('snapshot必须是boolean')
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ValueError('cursor必须是最多512字符的string')
    if cursor and 'offset' in params:
        raise ValueError('cursor不能与offset并用')
    return _FindFilesRequest(
        pattern=_text_param(
            params.get("pattern"),
            name="pattern",
            max_chars=300,
            strip=True,
        ),
        raw_path=_optional_path(params.get("path", "."), default="."),
        limit=min(
            _int_param(params.get("limit"), name="limit", default=max_matches, min_value=1),
            max_matches,
        ),
        offset=_int_param(params.get("offset"), name="offset", default=0, min_value=0),
        include_ignored=_bool_param(params.get("include_ignored", False), default=False),
        snapshot=snapshot,
        cursor=cursor,
    )


# LLM: 后备与search复用同一安全候选流，目录下降前即应用权限和nofollow剪枝。
# 函数用途: 在没有 rg 时按目录和文件名顺序逐个产出候选文件，不预先堆积整个目录树。
def _iter_find_candidates(
    target: Path,
    *,
    tool: FindFilesTool,
    include_ignored: bool,
    scan_state: _FindScanState,
) -> Iterable[Path]:
    yield from _iter_discovery_files(tool, target, include_ignored=include_ignored,
        ignored_names=_COMMON_FILE_DISCOVERY_IGNORES, scan_state=scan_state, admit=_admit_find_candidate)


# LLM: This liveness fence only bounds the Python fallback and is reported as
# incomplete; it never authorizes a no-match conclusion.
# 函数用途: 在后备文件遍历超过 2 万文件或 10 秒前停止，保护单 Gateway 不被大目录长期占住。
def _admit_find_candidate(state: _FindScanState) -> bool:
    if state.candidates >= _FALLBACK_SCAN_MAX_FILES:
        state.limited = True
        state.limit_reason = "file_limit"
        return False
    if time.monotonic() - state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
        state.limited = True
        state.limit_reason = "time_limit"
        return False
    state.candidates += 1
    return True


# LLM: 后端共用大小写明确的glob，前导双星目录也允许匹配根级文件。
# 函数用途: 统一路径及basename的模式匹配语义。
def _matches_find_pattern(display_path: str, basename: str, pattern: str) -> bool:
    return _discovery_matches_glob(display_path, basename, pattern)


def _offset_page_window(request: _OffsetPageWindowRequest) -> dict[str, int | bool | str]:
    return {
        "kind": "offset_page",
        "tool": "find_files",
        "source_path": request.source_path,
        "offset": request.offset,
        "limit": request.limit,
        "returned": request.returned,
        "next_offset": request.offset + request.returned if request.has_more else 0,
        "complete": not request.has_more and request.scan_complete,
    }
