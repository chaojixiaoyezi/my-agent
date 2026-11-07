"""把一次性实时票据交换成有界终端授权；进程仍由原 PTY 注册表持有。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import threading
import time
from contextlib import ExitStack
from copy import copy
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ..gateway_parts.business_api_v13 import v13_require_access
from ..plugin_host.errors import HostAPIError
from ..tooling.pty_sessions import (
    _MAX_SESSION_COUNT,
    PtyHostPolicy,
    PtySessionRegistry,
    PtyWriteError,
    _pty_access_scope,
)
from ..tooling.shell import ShellTool
from ..tooling.write_boundary import SandboxWriteProtection
from . import v13_w12_oncall_push as tickets


def _limit(name, default):
    # W0 尚未合入时只保留本片的安全默认，不把共享配置写进第二个权威文件。
    module_name = __package__ + '.gap158_limits'
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        return default
    return getattr(module, name)


def _owner(call):
    return call.owner_id, str(Path(call.owner_home).resolve())


def _failure(code='VERIFICATION_FAILED', status=403):
    return HostAPIError(code, status, 'unknown' if code == 'OUTCOME_UNKNOWN' else 'not_started')


def _launch_inputs(call, workspace):
    """只借可信宿主已有 Shell 的上界；缺失时拒绝，不另造宽松策略。"""
    tools = getattr(getattr(call.services.host, 'tools', None), 'tools', {})
    shell = tools.get('run_command') if isinstance(tools, dict) else None
    owner = Path(call.owner_home).resolve()
    if (not isinstance(shell, ShellTool) or not shell.path_access_policy.owner_scope_root
            or Path(shell.path_access_policy.owner_scope_root).resolve() != owner):
        raise _failure('HOST_NOT_READY', 503)
    target = shell._execution_target({'working_dir': str(workspace)}, '/bin/sh')
    if not isinstance(target, Path) or target.resolve() != workspace:
        raise _failure()
    denied = tuple(getattr(shell, '_sandbox_forbidden_write_roots', ()))
    protected = tuple(dict.fromkeys([*(owner / name for name in ('AGENTS.md', 'SOUL.md', 'USER.md')),
                                    *shell.host_exclusive_paths]))
    # PTY 后端重建策略不会保留 Shell 的 hidden_paths 扩展；转为更严格的拒读根。
    private = tuple(dict.fromkeys((*shell.host_private_roots, *(str(p) for p in shell.host_exclusive_paths))))
    policy = PtyHostPolicy(private_roots=private,
        network_access=getattr(shell, '_sandbox_network_access', False),
        host_credential_env_names=shell.host_credential_env_names,
        protected_persona_root=shell.protected_persona_root or owner,
        full_access_other_owner_homes=shell.full_access_other_owner_homes,
        idle_timeout_seconds=shell.pty_session_idle_timeout_seconds)
    return owner, (workspace,), (workspace,), SandboxWriteProtection(protected, denied), policy


def _execute_with_host(registry, method, call):
    """原生 HTTP 的只读宿主视图只在需要工作根时租用同 owner 完整宿主。"""
    v13_require_access(call)
    if method not in {'grant', 'open'} or hasattr(call.services.host, 'workspace'):
        return registry._execute(method, call)
    with ExitStack() as stack:
        provider = call.context.host_provider
        if not callable(provider):
            raise _failure('HOST_NOT_READY', 503)
        host = provider(stack)
        paths = getattr(host, 'home_paths', None)
        if (getattr(paths, 'owner_id', None) != call.owner_id
                or Path(paths.owner_home_dir).resolve() != Path(call.owner_home).resolve()):
            raise _failure('HOST_NOT_READY', 503)
        # 只复制本次 service 视图，不改签发 context、共享服务或缓存宿主租约。
        services = copy(call.services)
        services.host = host
        selected = replace(call, services=services)
        v13_require_access(selected)
        return registry._execute(method, selected)


def _workspace(call):
    root = getattr(call.services.host, 'workspace', None)
    if root is None:
        raise _failure('HOST_NOT_READY', 503)
    root = Path(root).resolve(strict=True)
    value = call.params['workspace_root']
    path = PurePosixPath(value)
    if (not value or '\x00' in value or '\\' in value or value.startswith('-')
            or path.is_absolute() or '..' in path.parts):
        raise _failure('PATH_OUTSIDE_WORKSPACE')
    try:
        target = root.joinpath(*path.parts).resolve(strict=True)
        relative = target.relative_to(root)
        if not target.is_dir() or any(part in {'.git', '.my-agent', '.agent'} for part in relative.parts):
            raise ValueError('private path')
    except (OSError, ValueError, RuntimeError) as error:
        raise _failure('PATH_OUTSIDE_WORKSPACE') from error
    return target, relative.as_posix()


@dataclass
class TerminalGrant:
    grant_id: str
    owner: tuple[str, str]
    subject_id: str
    workspace: Path
    workspace_label: str
    capabilities: tuple[str, ...]
    digest: str
    expires_at: float
    channel: str = 'web_terminal'
    bound: bool = False
    timer: object = None
    sessions: dict = field(default_factory=dict)
    cleanup_delay: float = 0.1


class WebTerminalRegistry:
    """授权、启动、撤权同锁线性化；未知清理结果保留原进程责任，不按容量删除。"""

    def __init__(self, backend=None, *, schedule=True):
        self.backend = backend if backend is not None else PtySessionRegistry()
        self.lock = threading.RLock()
        self.grants = {}
        self.schedule = schedule
        self.receipts = {}

    def execute(self, method, call):
        return _execute_with_host(self, method, call)

    def _execute(self, method, call):
        # 只在期限内保留有界回执；重放前仍重核授权，不以旧成功绕过撤权或到期。
        with self.lock:
            v13_require_access(call)
            now = call.clock()
            self.receipts = {key: value for key, value in self.receipts.items() if value[0] > now}
            capability = {'write': 'write', 'resize': 'resize'}.get(method)
            if method in {'open', 'write', 'resize'}:
                self._verified(call, capability)
            key = (_owner(call), call.subject_id, call.action, call.idempotency_key)
            fingerprint = hashlib.sha256(json.dumps(call.params, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            saved = self.receipts.get(key)
            if saved is not None:
                if saved[1] != fingerprint:
                    raise _failure('IDEMPOTENCY_CONFLICT', 409)
                if isinstance(saved[2], HostAPIError):
                    # 清理重试只针对原句柄；open/write 的未知提交绝不再次执行。
                    if method not in {'close', 'revoke'}:
                        error = saved[2]
                        raise HostAPIError(error.code, error.status, error.outcome, error.message)
                else:
                    return json.loads(json.dumps(saved[2]))
            if len(self.receipts) >= _MAX_SESSION_COUNT * 256:
                raise _failure('HOST_NOT_READY', 503)
            try:
                result = getattr(self, method)(call)
            except HostAPIError as error:
                if error.outcome == 'unknown':
                    # 保存独立错误，避免保留 traceback 和调用参数中的原票据。
                    self.receipts[key] = (now + _limit('WEB_TERMINAL_GRANT_TTL_SECONDS', 120),
                        fingerprint, HostAPIError(error.code, error.status, error.outcome, error.message))
                raise
            safe = {**result, 'ticket': '<redacted>'} if method == 'grant' else result
            self.receipts[key] = (now + _limit('WEB_TERMINAL_GRANT_TTL_SECONDS', 120),
                                  fingerprint, json.loads(json.dumps(safe)))
            return result

    def _close(self, grant):
        for session in grant.sessions.values():
            self._close_session(session)

    def _close_session(self, session):
        try:
            self.backend.close(session.session_id, session.access_scope)
        except (OSError, RuntimeError):
            # 失败仍保留原资源，由到期重试或显式关闭继续清理。
            pass

    def _retired(self, grant):
        return all(session.closed and session.termination is not None and session.termination.confirmed
                   for session in grant.sessions.values())

    def _schedule(self, grant, delay):
        if not self.schedule:
            return
        if grant.timer is not None:
            grant.timer.cancel()
        grant.timer = threading.Timer(delay, self._expire, (grant.grant_id,))
        grant.timer.daemon = True
        grant.timer.start()

    def _expire(self, grant_id):
        with self.lock:
            grant = self.grants.get(grant_id)
            if grant is not None:
                self._close(grant)
                # 到期即拒绝新操作；未确认退出的资源仍有准确记录，后续清理可重试。
                grant.expires_at = 0
                if self._retired(grant):
                    self.grants.pop(grant_id, None)
                else:
                    self._schedule(grant, grant.cleanup_delay)
                    grant.cleanup_delay = min(30.0, grant.cleanup_delay * 2)

    def sweep(self, now):
        with self.lock:
            for grant in tuple(self.grants.values()):
                if grant.expires_at <= now:
                    if grant.timer is not None:
                        grant.timer.cancel()
                    self._expire(grant.grant_id)

    def clear(self):
        with self.lock:
            for identifier in tuple(self.grants):
                grant = self.grants[identifier]
                if grant.timer is not None:
                    grant.timer.cancel()
                self._expire(identifier)

    def grant(self, call):
        v13_require_access(call)
        target, label = _workspace(call)
        with self.lock:
            self.sweep(call.clock())
            own = [g for g in self.grants.values() if g.owner == _owner(call)]
            if len(own) >= _limit('WEB_TERMINAL_MAX_SESSIONS_PER_OWNER', 4) * 4 or len(self.grants) >= _MAX_SESSION_COUNT * 4:
                raise _failure('LIMIT_EXCEEDED', 400)
            minted = tickets.ticket_mint(replace(call, action='realtime.ticket.mint', params={
                'channel': 'web_terminal', 'ttl_seconds': _limit('WEB_TERMINAL_GRANT_TTL_SECONDS', 120)},
                expected_revision=None))
            ticket = minted['ticket']
            if ticket == '<redacted>':
                # secret-once 的幂等重放不可能重新授予原始秘密。
                raise _failure('IDEMPOTENCY_CONFLICT', 409)
            grant = TerminalGrant('grant-' + uuid4().hex, _owner(call), call.subject_id,
                target, label, tuple(dict.fromkeys(call.params['capabilities'])),
                hashlib.sha256(ticket.encode()).hexdigest(), tickets._epoch(minted['expires_at']))
            self.grants[grant.grant_id] = grant
            self._schedule(grant, max(0, grant.expires_at - call.clock()))
            return {'grant_id': grant.grant_id, 'ticket': ticket, 'expires_at': grant.expires_at,
                    'capabilities': list(grant.capabilities)}

    def _verified(self, call, capability=None):
        v13_require_access(call)
        self.sweep(call.clock())
        digest = hashlib.sha256(call.params['ticket'].encode()).hexdigest()
        grant = next((g for g in self.grants.values() if g.owner == _owner(call)
                      and hmac.compare_digest(g.digest, digest)), None)
        if (grant is None or grant.subject_id != call.subject_id or grant.channel != 'web_terminal'
                or grant.expires_at <= call.clock()
                or capability is not None and capability not in grant.capabilities):
            raise _failure()
        return grant

    def _session(self, call, *, grant=None):
        candidates = [grant] if grant is not None else self.grants.values()
        for selected in candidates:
            if selected.owner == _owner(call) and selected.subject_id == call.subject_id:
                session = selected.sessions.get(call.params['session_id'])
                if session is not None:
                    return session
        raise _failure('RESOURCE_NOT_FOUND', 404)

    def open(self, call):
        with self.lock:
            grant = self._verified(call)
            sessions = [s for g in self.grants.values() if g.owner == grant.owner for s in g.sessions.values()]
            maximum = _limit('WEB_TERMINAL_MAX_SESSIONS_PER_OWNER', 4)
            if sum(s.termination is None or not s.termination.confirmed for s in sessions) >= maximum:
                raise _failure('LIMIT_EXCEEDED', 400)
            if len(sessions) >= maximum * 4:
                # 只裁剪已确认退出的历史，不擦掉仍有清理责任的句柄。
                for g in self.grants.values():
                    if g.owner == grant.owner:
                        g.sessions = {k: s for k, s in g.sessions.items()
                                      if s.termination is None or not s.termination.confirmed}
            owner_home, write_roots, read_roots, protection, policy = _launch_inputs(call, grant.workspace)
            if not grant.bound:
                tickets.ticket_verify(replace(call, action='realtime.ticket.verify', params={
                    'ticket': call.params['ticket'], 'channel': 'web_terminal'}, expected_revision=None))
                grant.bound = True
            # 根和 cwd 都重新核验；授权之后替换的符号链接不能扩大原访问范围。
            if grant.workspace.resolve(strict=True) != grant.workspace:
                raise _failure()
            session = None
            run_scope = {'owner_home': str(owner_home), 'thread_id': grant.grant_id, 'run_id': 'web-' + uuid4().hex}
            scope = _pty_access_scope(owner_home, write_roots, read_roots, protection.absolute_paths, run_scope,
                write_scope_denied_roots=protection.denied_roots, network_access=policy.network_access,
                protected_persona_root=policy.protected_persona_root)
            known = set(grant.sessions)
            try:
                session = self.backend.start('/bin/sh', grant.workspace, owner_home,
                    write_roots, read_roots, protection,
                    run_scope,
                    host_policy=policy)
                grant.sessions[session.session_id] = session
                resized = self.backend.resize(session.session_id, call.params['cols'], call.params['rows'], session.access_scope)
                if resized is None or resized.resize_readback is None:
                    raise OSError('PTY 没有返回尺寸读回值')
                cols, rows = resized.resize_readback
            except (OSError, ValueError, RuntimeError) as error:
                if session is None:
                    # start 未返回不是未启动证明；原注册表中可能已有本次出生的会话。
                    for late in self.backend.list(scope):
                        if late.session_id not in known:
                            grant.sessions[late.session_id] = late
                            self._close_session(late)
                    raise _failure('OUTCOME_UNKNOWN', 503) from error
                else:
                    self._close_session(session)
                    if not (session.closed and session.termination is not None and session.termination.confirmed):
                        raise _failure('OUTCOME_UNKNOWN', 503) from error
                raise _failure('EXTERNAL_PROCESS_FAILED', 502) from error
            return {'session_id': session.session_id, 'cols': cols, 'rows': rows}

    def write(self, call):
        with self.lock:
            grant = self._verified(call, 'write')
            session = self._session(call, grant=grant)
            try:
                data = base64.b64decode(call.params['data_b64'], validate=True)
            except (ValueError, TypeError) as error:
                raise _failure('INVALID_SCHEMA', 400) from error
            if not data or len(data) > _limit('WEB_TERMINAL_READ_MAX_BYTES', 65536):
                raise _failure('INVALID_SCHEMA', 400)
            if session.closed or session.process.poll() is not None:
                raise _failure('STATE_CONFLICT', 409)
            try:
                written = self.backend.write(session.session_id, data, session.access_scope)
            except PtyWriteError as error:
                if error.written:
                    raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown',
                        f'已接受 {error.written} 字节；剩余输入未确认，不可整段重放。') from error
                raise _failure('STATE_CONFLICT', 409) from error
            if written is None:
                raise _failure('RESOURCE_NOT_FOUND', 404)
            return {'accepted_bytes': len(data)}

    def read(self, call):
        with self.lock:
            grant = self._verified(call, 'read')
            session = self._session(call, grant=grant)
            maximum = call.params.get('max_bytes', min(32000, _limit('WEB_TERMINAL_READ_MAX_BYTES', 65536)))
            if type(maximum) is not int or not 1 <= maximum <= _limit('WEB_TERMINAL_READ_MAX_BYTES', 65536):
                raise _failure('INVALID_SCHEMA', 400)
            # get 保留原 PTY 来源版本核验；已结束对象仍由本授权保留，以便读回尾部和原回执。
            if self.backend.get(session.session_id, session.access_scope) is None and not session.closed:
                raise _failure('RESOURCE_NOT_FOUND', 404)
            data, cursor, overflow = session.read(call.params['cursor'], maximum)
            receipt = session.termination
            result = {'data_b64': base64.b64encode(data).decode(), 'next_cursor': cursor,
                      'overflow': overflow, 'exited': bool(receipt is not None and receipt.confirmed)}
            if receipt is not None and receipt.return_code is not None and receipt.return_code >= 0:
                result['exit_code'] = receipt.return_code
            return result

    def resize(self, call):
        with self.lock:
            grant = self._verified(call, 'resize')
            session = self._session(call, grant=grant)
            try:
                resized = self.backend.resize(session.session_id, call.params['cols'], call.params['rows'], session.access_scope)
            except OSError as error:
                raise _failure('RESOURCE_NOT_FOUND', 404) from error
            if resized is None or resized.closed or resized.resize_readback is None:
                raise _failure('RESOURCE_NOT_FOUND', 404)
            cols, rows = resized.resize_readback
            return {'cols': cols, 'rows': rows}

    def close(self, call):
        with self.lock:
            v13_require_access(call)
            # 关闭无需票据，仍必须匹配认证的 owner/subject，且重复关闭沿同一原回执。
            session = self._session(call)
            started = time.monotonic()
            self.backend.close(session.session_id, session.access_scope)
            receipt = session.termination
            confirmed = bool(receipt is not None and receipt.confirmed)
            if not confirmed:
                raise _failure('OUTCOME_UNKNOWN', 503)
            proof = {'terminated': confirmed, 'process_group_released': confirmed,
                     'duration_ms': max(0, int((time.monotonic() - started) * 1000))}
            result = {'closed': confirmed, 'receipt': proof}
            if receipt is not None and receipt.return_code is not None and receipt.return_code >= 0:
                result['exit_code'] = proof['exit_code'] = receipt.return_code
            return result

    def revoke(self, call):
        with self.lock:
            v13_require_access(call)
            grant = self.grants.get(call.params['grant_id'])
            if grant is None or grant.owner != _owner(call) or grant.subject_id != call.subject_id:
                return {'revoked': False, 'closed_sessions': []}
            grant.expires_at = 0
            if grant.timer is not None:
                grant.timer.cancel()
            self._close(grant)
            closed = [s.session_id for s in grant.sessions.values()
                      if s.termination is not None and s.termination.confirmed]
            if not self._retired(grant):
                self._schedule(grant, grant.cleanup_delay)
                raise _failure('OUTCOME_UNKNOWN', 503)
            self.grants.pop(grant.grant_id, None)
            return {'revoked': True, 'closed_sessions': closed}

    def list_sessions(self, call):
        with self.lock:
            v13_require_access(call)
            self.sweep(call.clock())
            rows = []
            for grant in self.grants.values():
                if grant.owner != _owner(call) or grant.subject_id != call.subject_id:
                    continue
                for session in grant.sessions.values():
                    size = session.resize_readback or (0, 0)
                    receipt = session.termination
                    row = {'session_id': session.session_id, 'grant_id': grant.grant_id,
                        'workspace_root': grant.workspace_label, 'cols': size[0], 'rows': size[1],
                        'created_at': session.started_at,
                        'exited': bool(receipt is not None and receipt.confirmed)}
                    if receipt is not None and receipt.return_code is not None and receipt.return_code >= 0:
                        row['exit_code'] = receipt.return_code
                    rows.append(row)
            return {'items': sorted(rows, key=lambda row: (row['created_at'], row['session_id']))}


_registry = WebTerminalRegistry()


def terminal_ready(call):
    # readiness 只观察开关与 PTY 平台，不触发启动或修改授权。
    # 能力目录的探针没有签发 context；宿主只能从原 services 取得。
    host = call.services.host
    wired = (hasattr(host, 'workspace') and getattr(host, 'tools', None) is not None
             or getattr(host, 'business_complete_host_wired', False) is True)
    return os.name != 'nt' and wired and bool(getattr(host.config, 'web_terminal_enabled', False))


def grant(call):
    return _registry.execute('grant', call)


def open_session(call):
    return _registry.execute('open', call)


def write(call):
    return _registry.execute('write', call)


def read(call):
    return _registry.read(call)


def resize(call):
    return _registry.execute('resize', call)


def close(call):
    return _registry.execute('close', call)


def revoke(call):
    return _registry.execute('revoke', call)


def list_sessions(call):
    return _registry.list_sessions(call)
