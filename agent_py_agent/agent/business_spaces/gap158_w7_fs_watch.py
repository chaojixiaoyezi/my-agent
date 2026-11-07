"""工作区拉取投影；订阅不创建线程，搜索使用可取消的有界短任务。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from ..common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath
from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import v13_require_access
from ..path_access_policy import HOST_CONFIG_OWNER_PARTS, PathAccessPolicy
from ..plugin_host.errors import HostAPIError
from ..plugin_host.workspace_v2_handlers import _forbid_internal
from ..user_space.owner_epoch import read_owner_epoch

try:
    from .gap158_limits import (
        FS_SEARCH_MAX_CONCURRENT,
        FS_SEARCH_MAX_FILE_BYTES,
        FS_WATCH_EVENT_RING,
        FS_WATCH_IDLE_EXPIRY_SECONDS,
        FS_WATCH_MAX_ENTRIES_PER_SCAN,
        FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER,
        FS_WATCH_MIN_SCAN_INTERVAL_SECONDS,
    )
except ModuleNotFoundError as error:
    # LLM: 仅目标 W0 模块缺失可回退，其内部依赖故障或缺常量必须原样暴露。
    # 模块用途: 保存冻结兼容默认值，规范模块名仍可被测试独立替换。
    if error.name != __package__ + '.gap158_limits':
        raise
    _fallback_limits = SimpleNamespace(
        fs_search_max_concurrent=2,
        fs_search_max_file_bytes=1048576,
        fs_watch_event_ring=1024,
        fs_watch_idle_expiry_seconds=900,
        fs_watch_max_entries_per_scan=5000,
        fs_watch_max_subscriptions_per_owner=8,
        fs_watch_min_scan_interval_seconds=1.0,
    )
    FS_SEARCH_MAX_CONCURRENT = _fallback_limits.fs_search_max_concurrent
    FS_SEARCH_MAX_FILE_BYTES = _fallback_limits.fs_search_max_file_bytes
    FS_WATCH_EVENT_RING = _fallback_limits.fs_watch_event_ring
    FS_WATCH_IDLE_EXPIRY_SECONDS = _fallback_limits.fs_watch_idle_expiry_seconds
    FS_WATCH_MAX_ENTRIES_PER_SCAN = _fallback_limits.fs_watch_max_entries_per_scan
    FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER = _fallback_limits.fs_watch_max_subscriptions_per_owner
    FS_WATCH_MIN_SCAN_INTERVAL_SECONDS = _fallback_limits.fs_watch_min_scan_interval_seconds

_state_lock = threading.RLock()
_owner_states = OrderedDict()


def confined_parts(value) -> tuple[str, ...]:
    """先做纯词法拒绝；点路径表示可信工作根，不接受跨平台绝对路径。"""
    if (not isinstance(value, str) or not value or '\x00' in value or '\\' in value
            or value.startswith('/') or PureWindowsPath(value).drive):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    parts = value.split('/')
    if any(p == '..' or p.startswith('-') or p.casefold() == '.git' for p in parts):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    return tuple(p for p in parts if p not in ('', '.'))


# LLM: 身份只认原生调用和宿主事实；home 解析仅归一化键，不签发跨 owner 权限。
# 函数用途: 核对宿主归属并生成跨请求稳定的本人状态键。
def _owner_key(call):
    paths = call.context.host.home_paths
    if (paths.owner_id != call.owner_id
            or Path(paths.owner_home_dir).absolute() != Path(call.owner_home).absolute()):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return call.owner_id, str(Path(call.owner_home).resolve())


# LLM: owner 墙由已认证 home 固定，配置工作根和 full 模式都不能替另一个 owner 签发读权。
# 函数用途: 为本次调用构造真实路径策略，不缓存宿主或授权上下文。
def _path_policy(call) -> PathAccessPolicy:
    host = call.context.host
    return PathAccessPolicy.from_config(host.config,
        agent_home_root=getattr(host.home_paths, 'root', None), owner_scope_root=call.owner_home)


# LLM: SCM 共享此签名；工作根同时过 owner 身份、PathAccessPolicy 和原内部目录门。
# 函数用途: 从可信宿主取工作根，拒绝配置指向另一 owner 的路径。
def workspace_root(call) -> Path:
    """根只能来自已认证宿主，参数中的路径永远不能替换这个锚。"""
    host = call.context.host
    paths = host.home_paths
    _owner_key(call)
    configured = getattr(host.config, 'workspace_root', '')
    if isinstance(configured, (list, tuple)):
        configured = next((p for p in configured if p), '')
    root = Path(configured) if configured else Path(paths.owner_workspace_dir)
    if not root.is_absolute():
        root = Path(call.owner_home) / root
    if '..' in root.parts:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    _safe_target(call.owner_home, root, _path_policy(call))
    return root


# LLM: SCM 仍沿同一签名读取原数据库；冷宿主读失败归依赖未就绪，绝不创建代次或伪造常量。
# 函数用途: 核对本人身份后读取原 owner epoch。
def current_epoch(call) -> int:
    """读原 owner 数据库；缺少真实仓库时不能用常量伪造撤销支持。"""
    host = call.context.host
    _owner_key(call)
    repo = getattr(getattr(host, 'subagents', None), 'runtime_db', None)
    if repo is None:
        storage = getattr(getattr(host, 'conversation_store', None), 'storage', None)
        repo = getattr(storage, 'owner_epoch_repository', None)
    try:
        if repo is None:
            # 冷 Gateway 也读取同一规范库，不创建 Agent、激活新代次或补建数据库。
            from ..runtime_db.repository import RuntimeRepository
            from ..runtime_db.schema import runtime_db_path
            repo = RuntimeRepository(runtime_db_path(call.owner_home), read_only=True)
        return read_owner_epoch(repo)
    except (OSError, sqlite3.DatabaseError):
        raise HostAPIError('HOST_NOT_READY', 503) from None


# LLM: 路径策略必须来自当前调用，内部目录门是附加约束，拒绝统一映射冻结路径错误码。
# 函数用途: 检查真实路径权限并拒绝宿主内部内容。
def _safe_target(home, target, policy):
    """复用已有内部目录/inode 门，并映射到本动作合同的路径拒绝码。"""
    home, target = Path(home), Path(target)
    try:
        if not policy.check(target).allowed:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
        _forbid_internal(home, target)
        h, t = tuple(p.casefold() for p in home.parts), tuple(p.casefold() for p in target.parts)
        if t[:len(h)] == h:
            rel = t[len(h):]
            if any(rel[:len(p)] == p for p in HOST_CONFIG_OWNER_PARTS):
                raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    except HostAPIError:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from None


@dataclass
class _Subscription:
    sid: str
    home: Path
    root: Path
    parts: tuple
    options: dict
    owner_epoch: int
    epoch: int
    touched: float
    policy: PathAccessPolicy
    identity: tuple = ()
    seq: int = 0
    last_scan: float | None = None
    entries: dict = field(default_factory=dict)
    signatures: dict = field(default_factory=dict)
    truncated: bool = False
    ring: deque = field(default_factory=lambda: deque(maxlen=FS_WATCH_EVENT_RING))


@dataclass
class _Search:
    search_id: str
    subscription: _Subscription
    epoch: int
    touched: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    state: str = 'running'
    thread: object = None


@dataclass
class _OwnerState:
    owner: tuple
    epoch: int
    subscriptions: dict = field(default_factory=dict)
    searches: OrderedDict = field(default_factory=OrderedDict)
    receipts: dict = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)


def _cancel_subscription(state, sub):
    for job in state.searches.values():
        if job.subscription is sub and not job.done.is_set():
            job.cancel.set()
            job.state = 'cancelled'


# LLM: 状态只存数据，以 owner/home 为稳定键、epoch 原位撤销；每次只惰性清理本人，不扫描所有 owner。
# 函数用途: 跨请求宿主复用有界订阅状态，并在授权与代次变化时拒绝旧结果。
def _state(call):
    v13_require_access(call)
    epoch = current_epoch(call)
    owner = _owner_key(call)
    state = _owner_states.get(owner)
    if state is None:
        # 复用已有环限额限制 owner 桶数；最多只检查最旧一桶，不为新 owner 扫描所有租户。
        if len(_owner_states) >= FS_WATCH_EVENT_RING:
            oldest, stale = next(iter(_owner_states.items()))
            if (time.monotonic() - stale.touched < FS_WATCH_IDLE_EXPIRY_SECONDS
                    or any(not job.done.is_set() for job in stale.searches.values())):
                raise HostAPIError('HOST_NOT_READY', 503)
            del _owner_states[oldest]
        state = _OwnerState(owner, epoch)
        _owner_states[owner] = state
    _owner_states.move_to_end(owner)
    state.touched = time.monotonic()
    now = call.clock()
    if state.epoch != epoch:
        for sub in state.subscriptions.values():
            _cancel_subscription(state, sub)
        state.subscriptions.clear()
        state.receipts.clear()
        state.epoch = epoch
    for sid, sub in tuple(state.subscriptions.items()):
        if now - sub.touched >= FS_WATCH_IDLE_EXPIRY_SECONDS:
            _cancel_subscription(state, sub)
            del state.subscriptions[sid]
    for key, receipt in tuple(state.receipts.items()):
        if (receipt[3] not in state.subscriptions
                and now - receipt[2] >= FS_WATCH_IDLE_EXPIRY_SECONDS):
            del state.receipts[key]
    for jid, job in tuple(state.searches.items()):
        if job.done.is_set() and (job.subscription.sid not in state.subscriptions
                                 or now - job.touched >= FS_WATCH_IDLE_EXPIRY_SECONDS):
            del state.searches[jid]
    return state


# LLM: 订阅读口只暴露资源不存在；路径拒绝不能泄露变化后的配置根，原授权错误仍透传。
# 函数用途: 判断当前工作根和路径策略是否仍是快照的范围。
def _same_scope(call, sub):
    try:
        return workspace_root(call) == sub.root and _path_policy(call) == sub.policy
    except HostAPIError as error:
        if error.code != 'PATH_OUTSIDE_WORKSPACE':
            raise
        return False


# LLM: 当前配置策略必须与快照策略一致；策略改变使旧订阅失效，不能在节流窗口披露旧缓存。
# 函数用途: 按本人订阅标识取状态并更新活动时间。
def _get(state, call):
    sub = state.subscriptions.get(call.params['subscription_id'])
    if sub is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if not _same_scope(call, sub):
        _cancel_subscription(state, sub)
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    sub.touched = call.clock()
    return sub


# LLM: 幂等回执只保留数据，命中也重核本次 live 授权、工作根及订阅路径策略。
# 函数用途: 在有界 owner 状态中执行控制或重放原结果。
def _control(call, mutate):
    """沿 V13 的 subject/action/key 指纹去重；满额拒绝，不丢回执重开搜索。"""
    with _state_lock:
        state = _state(call)
        if not call.idempotency_key:
            raise HostAPIError('INVALID_SCHEMA')
        key = (call.subject_id, call.action, call.idempotency_key)
        digest = hashlib.sha256(json.dumps([call.params, call.expected_revision],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        old = state.receipts.get(key)
        if old is not None:
            if old[0] != digest:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            sub = state.subscriptions.get(old[3])
            if sub is not None:
                _still_current(call, state, sub)
                sub.touched = call.clock()
            return deepcopy(old[1])
        if len(state.receipts) >= FS_WATCH_EVENT_RING:
            code = ('LIMIT_EXCEEDED' if call.action in ('fs_watch.subscribe', 'fs_watch.search')
                    else 'OUTCOME_UNKNOWN')
            raise HostAPIError(code, 400 if code == 'LIMIT_EXCEEDED' else 503)
        result = mutate(state)
        sid = result.get('subscription_id', call.params.get('subscription_id'))
        if 'search_id' in result and result['search_id'] in state.searches:
            sid = state.searches[result['search_id']].subscription.sid
        state.receipts[key] = (digest, deepcopy(result), call.clock(), sid)
        return result


@contextmanager
# LLM: 每次打开目录前重核订阅路径策略，nofollow 逐段打开继续阻止检查后的软链接替换。
# 函数用途: 在固定目录身份下提供只读目录句柄。
def _directory(sub, relative=()):
    """每次从绝对锚逐段 nofollow 打开；不把可换名父路径当作可信根。"""
    try:
        _safe_target(sub.home, sub.root.joinpath(*sub.parts, *relative), sub.policy)
    except HostAPIError:
        raise OSError('订阅目录路径不再可读') from None
    fd = open_directory_beneath(sub.root.anchor,
        (*sub.root.parts[1:], *sub.parts), require_dir_fd=True)
    try:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if sub.identity and sub.identity != identity:
            raise OSError('订阅根身份已变化')
        for part in relative:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, identity
    finally:
        os.close(fd)


def _included(sub, relative):
    path = '/'.join(relative)
    include = sub.options.get('include_globs', ())
    exclude = sub.options.get('exclude_globs', ())
    return ((not include or any(fnmatch.fnmatchcase(path, p) for p in include))
            and not any(fnmatch.fnmatchcase(path, p) for p in exclude))


# LLM: 过滤项仍消耗枚举预算；逐项过真实路径策略，禁止策略拒绝的目录进入递归队列。
# 函数用途: 有界枚举当前订阅根并生成签名投影。
def _scan(sub):
    """预算计算所有实际枚举项，包含过滤项；队列、深度和 FD 同时有界。"""
    pending, entries, signatures = [((), 0)], {}, {}
    count, truncated = 0, False
    while pending and not truncated:
        relative, depth = pending.pop()
        with _directory(sub, relative) as (fd, identity):
            if not sub.identity:
                sub.identity = identity
            with os.scandir(fd) as listing:
                for entry in listing:
                    count += 1
                    parts = (*sub.parts, *relative, entry.name)
                    try:
                        confined_parts('/'.join(parts))
                        _safe_target(sub.home, sub.root.joinpath(*parts), sub.policy)
                        info = entry.stat(follow_symlinks=False)
                    except (OSError, HostAPIError):
                        info = None
                    if info is not None and (stat.S_ISDIR(info.st_mode)
                            or stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                        child = (*relative, entry.name)
                        kind = 'dir' if stat.S_ISDIR(info.st_mode) else 'file'
                        if _included(sub, child):
                            path = '/'.join(parts)
                            entries[path] = {'path': path, 'kind': kind,
                                'size': max(0, info.st_size), 'mtime_ns': max(0, info.st_mtime_ns)}
                            signatures[path] = (info.st_ino, info.st_size, info.st_mtime_ns)
                        if (kind == 'dir' and sub.options.get('recursive', False)
                                and depth < sub.options.get('max_depth', 8)):
                            pending.append((child, depth + 1))
                    if count >= FS_WATCH_MAX_ENTRIES_PER_SCAN:
                        truncated = True
                        break
    return entries, signatures, truncated


# LLM: 只接受冻结事件的显式字段，不开放任意关键字入口，事件序号与环大小保持原约束。
# 函数用途: 把一个规范事件追加到订阅事件环。
def _push(sub, kind, *, reason=None, changes=None, search_id=None, match=None):
    sub.seq += 1
    event = {'seq': sub.seq, 'kind': kind, 'epoch': sub.epoch}
    for name, value in (('reason', reason), ('changes', changes),
                        ('search_id', search_id), ('match', match)):
        if value is not None:
            event[name] = value
    sub.ring.append(event)


def _snapshot(sub):
    return {'subscription_id': sub.sid, 'epoch': sub.epoch, 'seq': sub.seq,
        'snapshot': {'entries': [sub.entries[p] for p in sorted(sub.entries)],
                     'truncated': sub.truncated}}


# LLM: 根路径在扫描前过实时路径策略；扫描后仍重核原访问权和 epoch，成功才登记订阅。
# 函数用途: 建立本人可读工作根的初始快照。
def subscribe(call):
    # LLM: 控制锁内生成订阅，失败不能登记半成品或幂等回执。
    # 函数用途: 校验根并创建有界快照。
    def create(state):
        parts = confined_parts(call.params['root'])
        root = workspace_root(call)
        policy = _path_policy(call)
        _safe_target(call.owner_home, root.joinpath(*parts), policy)
        if len(state.subscriptions) >= FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER:
            raise HostAPIError('LIMIT_EXCEEDED')
        sub = _Subscription(uuid.uuid4().hex, Path(call.owner_home), root, parts,
            deepcopy(call.params), state.epoch, state.epoch, call.clock(), policy)
        try:
            sub.entries, sub.signatures, sub.truncated = _scan(sub)
        except (OSError, ValueError):
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from None
        v13_require_access(call)
        if current_epoch(call) != state.epoch:
            raise HostAPIError('HOST_NOT_READY', 503)
        sub.last_scan = call.clock()
        _push(sub, 'snapshot')
        state.subscriptions[sub.sid] = sub
        return _snapshot(sub)
    return _control(call, create)


# LLM: 异步结果和幂等重放都不是权限；每次用原活跃上下文检查授权、epoch、当前策略和登记身份。
# 函数用途: 拒绝撤权、换代或策略变化之后的旧订阅结果。
def _still_current(call, state, sub):
    """扫描/读文件之后重核原授权及原代次，不把异步结果当作新的访问凭据。"""
    v13_require_access(call)
    if current_epoch(call) != sub.owner_epoch:
        _state(call)
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if state.subscriptions.get(sub.sid) is not sub:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if not _same_scope(call, sub):
        _cancel_subscription(state, sub)
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)


def _refresh(call, state, sub):
    now = call.clock()
    if (sub.last_scan is not None
            and now - sub.last_scan < FS_WATCH_MIN_SCAN_INTERVAL_SECONDS):
        return
    sub.last_scan = now
    try:
        entries, signatures, truncated = _scan(sub)
    except (OSError, ValueError):
        _still_current(call, state, sub)
        _push(sub, 'overflow', reason='root_unavailable')
        return
    _still_current(call, state, sub)
    if truncated or sub.truncated:
        # 部分扫描中的缺项不是删除证据；要求调用方用新快照重建。
        _push(sub, 'overflow', reason='scan_truncated')
    else:
        changes = []
        for path in sorted(sub.entries.keys() | entries.keys()):
            if path not in entries:
                changes.append({'op': 'deleted', 'path': path, 'kind': sub.entries[path]['kind']})
            elif path not in sub.entries:
                changes.append({'op': 'created', **entries[path]})
            elif signatures[path] != sub.signatures[path]:
                changes.append({'op': 'modified', **entries[path]})
        # 500 是 FsEvent.changes 的冻结 DTO 上界，不是新运行时限额。
        for offset in range(0, len(changes), 500):
            _push(sub, 'delta', changes=changes[offset:offset + 500])
    sub.entries, sub.signatures, sub.truncated = entries, signatures, truncated


def events(call):
    with _state_lock:
        state = _state(call)
        sub = _get(state, call)
        _refresh(call, state, sub)
        _still_current(call, state, sub)
        after, limit = call.params['after_seq'], call.params.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HostAPIError('INVALID_SCHEMA')
        overflow = bool(sub.ring and after < sub.ring[0]['seq'] - 1)
        if overflow:
            batch = [{'seq': sub.seq, 'kind': 'overflow', 'epoch': sub.epoch,
                      'reason': 'event_ring_overflow'}]
        else:
            batch = [e for e in sub.ring if e['seq'] > after][:limit]
            overflow = any(e['kind'] == 'overflow' for e in batch)
        return {'subscription_id': sub.sid, 'epoch': sub.epoch, 'events': deepcopy(batch),
                'next_seq': batch[-1]['seq'] if batch else sub.seq, 'overflow': overflow}


def remount(call):
    def reset(state):
        sub = _get(state, call)
        # 新目录身份只在显式 remount 时允许；失败时保留旧快照/代次。
        previous = sub.identity
        sub.identity = ()
        try:
            entries, signatures, truncated = _scan(sub)
            _still_current(call, state, sub)
        except (OSError, ValueError):
            sub.identity = previous
            raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
        _cancel_subscription(state, sub)
        sub.entries, sub.signatures, sub.truncated = entries, signatures, truncated
        sub.last_scan = call.clock()
        sub.epoch += 1
        sub.ring.clear()
        _push(sub, 'snapshot')
        return _snapshot(sub)
    return _control(call, reset)


def unsubscribe(call):
    def remove(state):
        sub = state.subscriptions.pop(call.params['subscription_id'], None)
        if sub is not None:
            _cancel_subscription(state, sub)
        return {'removed': sub is not None}
    return _control(call, remove)


# LLM: 读文件前过真实路径策略，nofollow 与单链接约束不能由枚举快照替代。
# 函数用途: 在文件大小上限内读取当前订阅的 UTF-8 文本。
def _read_search_file(sub, path):
    """逐段 nofollow 且只读普通单链接文件；读前与读后都约束字节上界。"""
    parts = confined_parts(path)
    if parts[:len(sub.parts)] != sub.parts:
        raise OSError('文件不在订阅根内')
    _safe_target(sub.home, sub.root.joinpath(*parts), sub.policy)
    with _directory(sub):
        descriptor = open_readonly_file_beneath(sub.root.anchor, (*sub.root.parts[1:], *parts))
        with os.fdopen(descriptor, 'rb') as stream:
            if os.fstat(stream.fileno()).st_size > FS_SEARCH_MAX_FILE_BYTES:
                return None
            raw = stream.read(FS_SEARCH_MAX_FILE_BYTES + 1)
    if len(raw) > FS_SEARCH_MAX_FILE_BYTES or b'\x00' in raw:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return None


def _search_live(call, state, job):
    sub = job.subscription
    if (job.cancel.is_set() or job.epoch != sub.epoch
            or state.subscriptions.get(sub.sid) is not sub):
        return False
    _still_current(call, state, sub)
    return not job.cancel.is_set()


# LLM: 原调用只在运行线程栈里临时持有，每次读后即使无匹配也重核 live 授权；结束后线程释放参数。
# 函数用途: 按有界文件列表搜索，并仅发布仍在原 epoch 和授权内的匹配。
def _search_worker(call, state, job, paths):
    """每个文件及每条发布前检查 Event/epoch；取消任务退出后才归还并发槽。"""
    sub, found = job.subscription, 0
    try:
        for path in paths:
            with _state_lock:
                if not _search_live(call, state, job):
                    job.cancel.set()
                    break
            try:
                content = _read_search_file(sub, path)
            except (OSError, HostAPIError):
                content = None
            with _state_lock:
                if not _search_live(call, state, job):
                    job.cancel.set()
                    break
            if content is None:
                continue
            for line, text in enumerate(content.splitlines(), 1):
                if job.cancel.is_set():
                    break
                if call.params['query'] not in text:
                    continue
                with _state_lock:
                    if not _search_live(call, state, job):
                        job.cancel.set()
                        break
                    _push(sub, 'search_match', search_id=job.search_id,
                          match={'path': path, 'line': line, 'preview': text[:512]})
                found += 1
                if found >= call.params.get('max_results', 500):
                    return
    except (HostAPIError, BusinessError, OSError, ValueError):
        job.cancel.set()
    finally:
        with _state_lock:
            job.state = 'cancelled' if job.cancel.is_set() else 'completed'
            job.done.set()


def search(call):
    def start(state):
        sub = _get(state, call)
        if sum(not j.done.is_set() for j in state.searches.values()) >= FS_SEARCH_MAX_CONCURRENT:
            raise HostAPIError('LIMIT_EXCEEDED')
        _refresh(call, state, sub)
        _still_current(call, state, sub)
        # 已结束的任务只保留环大小的可查询历史；回执独立保留以免重开。
        while len(state.searches) >= FS_WATCH_EVENT_RING:
            oldest = next((jid for jid, j in state.searches.items() if j.done.is_set()), None)
            if oldest is None:
                raise HostAPIError('LIMIT_EXCEEDED')
            del state.searches[oldest]
        job = _Search(uuid.uuid4().hex, sub, sub.epoch, call.clock())
        pattern = call.params.get('glob', '')
        paths = tuple(path for path, entry in sorted(sub.entries.items())
            if entry['kind'] == 'file' and (not pattern or fnmatch.fnmatchcase(path, pattern)))
        job.thread = threading.Thread(target=_search_worker, args=(call, state, job, paths),
                                      name='fs-search-' + job.search_id, daemon=True)
        state.searches[job.search_id] = job
        try:
            job.thread.start()
        except RuntimeError:
            del state.searches[job.search_id]
            raise HostAPIError('HOST_NOT_READY', 503) from None
        return {'search_id': job.search_id, 'state': 'running'}
    return _control(call, start)


# LLM: 换代任务仍占运行槽直至退出，但旧 search_id 不再是可见资源；取消通过 Event 传递。
# 函数用途: 取消本人当前 epoch 中仍可见的搜索任务。
def search_cancel(call):
    # LLM: 幂等控制锁内修改状态，不能因取消或换代提前释放工作线程槽。
    # 函数用途: 标记可见任务取消并返回其原状态。
    def cancel(state):
        job = state.searches.get(call.params['search_id'])
        if job is None or job.subscription.owner_epoch != state.epoch:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        job.touched = call.clock()
        if not job.done.is_set():
            job.cancel.set()
            job.state = 'cancelled'
        return {'search_id': job.search_id, 'state': job.state}
    return _control(call, cancel)
