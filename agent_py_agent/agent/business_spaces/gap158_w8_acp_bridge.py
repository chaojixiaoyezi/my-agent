"""ACP 会话与 owner 边界；磁盘只存身份/生命周期，不存 prompt、事件或审批正文。

进程终止始终复用 MCPTransport 的原出生身份核验；未确认的清理禁止重启，
不能用新进程或旧的成功回执覆盖未知资源。部署配置才可给 argv/env。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..capability.runtime_config_reload import capability_config_for_paths
from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..conversation.business_access import BusinessError
from ..plugin_host.workspace_v2_handlers import _forbid_internal
from ..tooling.mcp_protocol import MCPError
from .gap158_w8_acp_protocol import AcpTransport, normalize_update
from .v13_store import v13_store_for

try:
    from .gap158_limits import (
        ACP_EVENT_RING as _event_ring,
    )
    from .gap158_limits import (
        ACP_MAX_SESSIONS_PER_OWNER as _max_sessions,
    )
    from .gap158_limits import (
        ACP_PROBE_TIMEOUT_SECONDS as _probe_timeout,
    )
    from .gap158_limits import (
        ACP_TERMINATE_GRACE_SECONDS as _terminate_grace,
    )
except ModuleNotFoundError as exc:
    if exc.name != __package__ + '.gap158_limits':
        raise
    # W8 r2 明确允许依赖片尚未合入时使用同合同值；不吞掉依赖内部损坏。
    _max_sessions, _event_ring, _probe_timeout, _terminate_grace = 4, 2048, 15, 3.0


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _parts(value, *, absolute=False):
    if (type(value) is not str or not value or '\x00' in value or '\\' in value
            or value.startswith('-') or '..' in value.split('/')
            or PurePosixPath(value).is_absolute() != absolute):
        raise BusinessError('PATH_OUTSIDE_WORKSPACE')
    return PurePosixPath(value).parts


def _cleanup(transport):
    started = time.monotonic()
    receipt = transport.terminate(grace_seconds=_terminate_grace)
    result = {'terminated': receipt.confirmed, 'process_group_released': receipt.confirmed,
              'duration_ms': max(0, int((time.monotonic() - started) * 1000))}
    if receipt.return_code is not None and receipt.return_code >= 0:
        result['exit_code'] = receipt.return_code
    if receipt.method in ('SIGTERM', 'SIGTERM->SIGKILL'):
        result['signal'] = 'SIGKILL' if receipt.method.endswith('SIGKILL') else 'SIGTERM'
    return result


class _ReceiptMissing(Exception):
    """仅在 Store 原锁内探测回执缺失；不提交数据，也不代替业务错误。"""


@dataclass
class AcpSession:
    record: dict
    store: object
    authority: object
    clock: object
    root: Path
    lock: object = field(default_factory=threading.RLock)
    control_lock: object = field(default_factory=threading.RLock)
    started: object = field(default_factory=threading.Event)
    stopped: object = field(default_factory=threading.Event)
    events: object = field(default_factory=lambda: deque(maxlen=_event_ring))
    permissions: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    configs: dict = field(default_factory=dict)
    file_versions: dict = field(default_factory=dict)
    transport: object = None
    cleanup: dict = field(default_factory=dict)
    seq: int = 0
    closing: bool = False
    exited_generation: str = ''
    failed_turn_generation: str = ''
    turn_state: str = 'completed'
    worker: object = None

    def public(self):
        keys = ('session_id', 'agent_id', 'project_root', 'runtime_id', 'acp_session_id',
                'state', 'turn_id', 'created_at')
        return {key: self.record[key] for key in keys if key in self.record} | {
            'process_alive': self.transport is not None and self.transport.is_running()}

    def emit(self, kind, data=None):
        with self.lock:
            self.seq += 1
            self.events.append({'seq': self.seq, 'kind': kind, 'at': self.clock(),
                'turn_id': self.record.get('turn_id', ''), 'data': deepcopy(data or {})})


class AcpBridge:
    """宿主每个 owner 一个实例；活动集、事件、审批和文件版本均有明确上限。"""

    def __init__(self, call):
        self.owner_id, self.owner_home = call.owner_id, Path(call.owner_home).resolve()
        self.host = call.context.host
        self.root = Path(getattr(self.host, 'effective_workspace_root', self.owner_home)).resolve()
        self.roots = tuple(Path(root).resolve() for root in getattr(self.host, 'effective_workspace_roots', [self.root]))
        self.sessions, self.probes = {}, {}
        self.operations = {}
        self.lock, self.closed = threading.RLock(), threading.Event()
        self.probe_cleanup, self.probe_transports = {}, {}
        atexit.register(self.shutdown)

    def _require_root(self, target):
        current_root = Path(getattr(self.host, 'effective_workspace_root', self.owner_home)).resolve()
        roots = tuple(Path(root).resolve() for root in getattr(self.host, 'effective_workspace_roots', [current_root]))
        if self.root != current_root or not any(target.is_relative_to(root) for root in roots):
            raise BusinessError('PATH_OUTSIDE_WORKSPACE')

    def _agent(self, agent_id):
        agents = getattr(self.host.config, 'acp_bridge_agents', {})
        if type(agents) is not dict or agent_id not in agents:
            raise BusinessError('RESOURCE_NOT_FOUND')
        definition = agents[agent_id]
        if (type(definition) is not dict or type(definition.get('command')) is not list
                or not definition['command'] or any(type(arg) is not str or not arg or '\x00' in arg
                                                   for arg in definition['command'])
                or type(definition.get('env_allowlist', [])) is not list
                or any(type(key) is not str or not key for key in definition.get('env_allowlist', []))):
            raise BusinessError('INVALID_SCHEMA')
        return definition

    # LLM: argv/env仍只从部署定义取得，显式回调不能成为任意Popen参数或新的权限来源。
    # 函数用途: 核对当前工作根后启动一条固定部署代理连接。
    def _launch(self, agent_id, root, *, on_update=None, on_request=None, on_exit=None):
        self._require_root(root)
        definition = self._agent(agent_id)
        env = {key: os.environ[key] for key in definition.get('env_allowlist', []) if key in os.environ}
        return AcpTransport.launch(list(definition['command']), cwd=root, environment=env,
            timeout=_probe_timeout, max_line_chars=getattr(self.host.config, 'mcp_max_line_chars', 1048576),
            on_update=on_update, on_request=on_request, on_exit=on_exit)

    def _project(self, relative):
        parts = _parts(relative)
        target = self.root.joinpath(*parts).resolve()
        self._require_root(target)
        if not any(target.is_relative_to(root) for root in self.roots):
            raise BusinessError('PATH_OUTSIDE_WORKSPACE')
        _forbid_internal(self.owner_home, target)
        try:
            fd = open_directory_beneath(target.anchor, target.parts[1:], require_dir_fd=True)
            os.close(fd)
        except (OSError, ValueError) as exc:
            raise BusinessError('PATH_OUTSIDE_WORKSPACE') from exc
        return target

    def _owned(self, call, records=None):
        from ..gateway_parts.business_api_v13 import v13_require_access

        identifier = call.params['session_id']
        record = ((records or {}).get(identifier) if records is not None
                  else v13_store_for(call, 'owner').get('acp_sessions', identifier))
        if record is None or record.get('owner_id') != call.owner_id:
            raise BusinessError('RESOURCE_NOT_FOUND')
        v13_require_access(call, record)
        session = self.sessions.get(identifier)
        if session is None:
            session = AcpSession(deepcopy(record), v13_store_for(call, 'owner'),
                lambda: v13_require_access(call), call.clock, self._project(record['project_root']))
            session.cleanup = deepcopy(record.get('cleanup', {}))
            if record.get('canonical_project_root') != str(session.root):
                raise BusinessError('STATE_CONFLICT')
            session.record['state'] = 'closed' if session.cleanup.get('process_group_released') else 'failed'
            self.sessions[identifier] = session
        return session

    def _save(self, session, *, generation=None):
        def mutate(records):
            # 进入 Store 事务后才取当前代快照，旧回合不能在排队后覆盖新连接或清理证明。
            with session.lock:
                if generation is not None and getattr(session, 'generation', None) != generation:
                    return {}
                snapshot = deepcopy(session.record)
                if session.cleanup:
                    snapshot['cleanup'] = deepcopy(session.cleanup)
                current = records.get(snapshot['session_id'])
                if current is not None and generation is not None and current.get('connection_generation') != generation:
                    return {}
                if current is not None or len(records) < _max_sessions:
                    records[snapshot['session_id']] = snapshot
            return {}
        session.store.transaction('acp_sessions', mutate, idempotency_key=uuid.uuid4().hex,
                                  params_digest=_digest([session.record['session_id'], generation]))

    def _replay(self, call, store, digest):
        def missing(records):
            raise _ReceiptMissing
        try:
            return store.transaction('acp_sessions', missing, idempotency_key=call.idempotency_key,
                                     params_digest=digest)
        except _ReceiptMissing:
            return None

    def invoke(self, call):
        from ..gateway_parts.business_api_v13 import v13_require_access

        v13_require_access(call)
        if call.owner_id != self.owner_id or Path(call.owner_home).resolve() != self.owner_home:
            raise BusinessError('RESOURCE_NOT_FOUND')
        action = call.action.removeprefix('acp_bridge.')
        if action == 'catalog':
            agents = getattr(self.host.config, 'acp_bridge_agents', {})
            if type(agents) is not dict or len(agents) > 32:
                raise BusinessError('INVALID_SCHEMA')
            return {'enabled': bool(getattr(self.host.config, 'acp_bridge_enabled', False)),
                'agents': [{'agent_id': identifier, 'display_name': str(value.get('display_name', ''))[:120],
                    'probe_state': self.probes.get(identifier, {}).get('probe_state', 'unknown'),
                    **{key: self.probes[identifier][key] for key in ('protocol_version', 'last_probe_at')
                       if key in self.probes.get(identifier, {})}}
                    for identifier, value in sorted(agents.items())]}
        if not getattr(self.host.config, 'acp_bridge_enabled', False) or self.closed.is_set():
            raise BusinessError('HOST_NOT_READY')
        store = v13_store_for(call, 'owner')
        if action == 'list':
            page = store.page('acp_sessions', cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
            items = []
            for record in page['items']:
                if record.get('owner_id') != call.owner_id:
                    continue
                v13_require_access(call, record)
                with self.lock:
                    session = self.sessions.get(record['session_id'])
                items.append(session.public() if session is not None else {
                    key: value for key, value in record.items() if key in (
                        'session_id', 'agent_id', 'project_root', 'runtime_id', 'acp_session_id',
                        'state', 'turn_id', 'created_at')} | {'process_alive': False})
            return {'items': items, 'next_cursor': page['next_cursor'] or '', 'coverage': page['coverage']}
        if action == 'events':
            with self.lock:
                session = self._owned(call)
            self._expire(session)
            return self._events(session, call.params)
        digest = _digest({'action': call.action, 'params': call.params})
        operation_key = _digest([call.subject_id, call.action, call.idempotency_key])
        with self.lock:
            if self.closed.is_set():
                raise BusinessError('HOST_NOT_READY')
            replay = self._replay(call, store, digest)
            if replay is not None:
                return replay
            active = self.operations.get(operation_key)
            creator = active is None
            if creator:
                if len(self.operations) >= _event_ring:
                    raise BusinessError('HOST_NOT_READY')
                fresh = []
                def reserve(records):
                    fresh.append(True)
                    return {'operation_id': uuid.uuid4().hex}
                # 预留与最终回执都归同一 Store。重启后只有预留无终态时保持未知，绝不盲重发。
                store.transaction('acp_sessions', reserve,
                    idempotency_key=_digest(['reservation', call.idempotency_key]), params_digest=digest)
                if not fresh:
                    raise BusinessError('OUTCOME_UNKNOWN')
                active = {'done': threading.Event(), 'error': None}
                self.operations[operation_key] = active
        if not creator:
            if not active['done'].wait(getattr(self.host.config, 'request_timeout', 120)):
                raise BusinessError('OUTCOME_UNKNOWN')
            replay = self._replay(call, store, digest)
            if replay is not None:
                return replay
            raise BusinessError('OUTCOME_UNKNOWN')
        try:
            result = self._execute(call, action, store)
            return store.transaction('acp_sessions', lambda records: result,
                idempotency_key=call.idempotency_key, params_digest=digest)
        finally:
            with self.lock:
                self.operations.pop(operation_key, None)
                active['done'].set()

    def _execute(self, call, action, store):
        """只在短事务中登记责任；协议等待、取消和进程清理均在 Store 锁外。"""
        if action == 'probe':
            return self._probe(call)
        if action == 'create':
            with self.lock:
                if self.closed.is_set():
                    raise BusinessError('HOST_NOT_READY')
                prepared = store.transaction('acp_sessions', lambda records: self._create(call, records, store),
                    idempotency_key=_digest(['prepare', call.idempotency_key]), params_digest=_digest(call.params))
                session = self.sessions[prepared['session_id']]
            if prepared['created']:
                try:
                    with session.control_lock:
                        self._start(session)
                finally:
                    session.started.set()
                    self._save(session)
            elif session.record['state'] == 'starting':
                if not session.started.wait(_probe_timeout) or session.record['state'] != 'ready':
                    raise BusinessError('EXTERNAL_PROCESS_FAILED')
            return {'session': session.public(), 'created': prepared['created']}
        with self.lock:
            session = self._owned(call)
        with session.control_lock:
            try:
                return self._control(call, action, session)
            finally:
                self._save(session)

    def _probe(self, call):
        identifier, started = call.params['agent_id'], time.monotonic()
        transport = None
        try:
            with self.lock:
                if self.closed.is_set():
                    raise BusinessError('HOST_NOT_READY')
                if identifier in self.probe_transports:
                    raise BusinessError('EXTERNAL_PROCESS_FAILED')
                if len(self.probe_transports) + len(self.sessions) >= _max_sessions:
                    raise BusinessError('LIMIT_EXCEEDED')
                transport = self._launch(identifier, self.root)
                self.probe_transports[identifier] = transport
            initialized = transport.initialize()
            result = {'agent_id': identifier, 'ok': True, 'protocol_version': str(initialized['protocolVersion']),
                      'capabilities': initialized.get('agentCapabilities', {})}
        except (OSError, MCPError) as exc:
            self.probes[identifier] = {'probe_state': 'failed', 'last_probe_at': call.clock()}
            raise BusinessError('EXTERNAL_PROCESS_FAILED') from exc
        finally:
            if transport is not None:
                cleanup = _cleanup(transport)
                with self.lock:
                    self.probe_cleanup[identifier] = cleanup
                    if cleanup['process_group_released'] and self.probe_transports.get(identifier) is transport:
                        self.probe_transports.pop(identifier, None)
        if not cleanup.get('process_group_released'):
            raise BusinessError('EXTERNAL_PROCESS_FAILED')
        result['duration_ms'] = max(0, int((time.monotonic() - started) * 1000))
        self.probes[identifier] = {'probe_state': 'ok', 'protocol_version': result['protocol_version'],
                                   'last_probe_at': call.clock()}
        return result

    def _create(self, call, records, store):
        from ..gateway_parts.business_api_v13 import v13_require_access

        root = self._project(call.params['project_root'])
        self._agent(call.params['agent_id'])
        ensure = _digest([call.owner_id, str(root), call.params['agent_id'], call.params.get('client_session_key', '')])
        for record in tuple(records.values()):
            if record.get('owner_id') == call.owner_id and record.get('ensure_key') == ensure:
                session = self._owned_session(record['session_id'])
                if session is not None and (session.record['state'] == 'starting'
                        or session.transport is not None and session.transport.is_running()):
                    return {'session_id': record['session_id'], 'created': False}
                cleanup = session.cleanup if session is not None else record.get('cleanup', {})
                if not cleanup.get('process_group_released'):
                    raise BusinessError('EXTERNAL_PROCESS_FAILED')
                records.pop(record['session_id'])
                self.sessions.pop(record['session_id'], None)
        if len(set(records) | set(self.sessions)) + len(self.probe_transports) >= _max_sessions:
            removable = next((key for key, row in records.items() if row.get('state') in ('closed', 'failed')
                and row.get('cleanup', {}).get('process_group_released')), None)
            if removable is None:
                raise BusinessError('LIMIT_EXCEEDED')
            records.pop(removable)
            self.sessions.pop(removable, None)
        identifier = 'acp-' + uuid.uuid4().hex
        record = {'session_id': identifier, 'agent_id': call.params['agent_id'], 'project_root': call.params['project_root'],
            'owner_id': call.owner_id, 'canonical_project_root': str(root),
            'runtime_id': call.params['agent_id'] + '@1', 'acp_session_id': '',
            'state': 'starting', 'created_at': call.clock(), 'ensure_key': ensure}
        session = AcpSession(record, store, lambda: v13_require_access(call), call.clock, root)
        self.sessions[identifier] = session
        records[identifier] = deepcopy(session.record)
        return {'session_id': identifier, 'created': True}

    def _owned_session(self, identifier):
        return self.sessions.get(identifier)

    def _start(self, session, *, load=False):
        generation = uuid.uuid4().hex
        with session.lock:
            session.closing = False
            session.stopped.clear()
            session.generation = generation
            session.record['connection_generation'] = generation
        transport = None
        try:
            with self.lock:
                if self.closed.is_set():
                    raise BusinessError('HOST_NOT_READY')
                session.authority()
                transport = self._launch(session.record['agent_id'], session.root,
                    on_update=lambda params: self._update(session, generation, params),
                    on_request=lambda method, params, req_id: self._request(session, generation, method, params, req_id),
                    on_exit=lambda: self._exited(session, generation))
                session.transport = transport
            transport.initialize()
            if load and transport.agent_capabilities.get('loadSession') is not True:
                raise BusinessError('STATE_CONFLICT')
            params = {'cwd': str(session.root), 'mcpServers': []}
            if load:
                params['sessionId'] = session.record['acp_session_id']
            response = transport.request('session/load' if load else 'session/new', params,
                timeout=_probe_timeout, authority_check=session.authority)
            if type(response) is not dict:
                raise MCPError('ACP 会话响应无效', code='MCP_PROTOCOL_ERROR')
            identifier = session.record['acp_session_id'] if load else response.get('sessionId')
            if type(identifier) is not str or not 1 <= len(identifier) <= 256:
                raise MCPError('ACP 会话编号无效', code='MCP_PROTOCOL_ERROR')
            with session.lock:
                if self.closed.is_set() or session.closing or session.generation != generation:
                    raise BusinessError('HOST_NOT_READY')
                session.record.update(acp_session_id=identifier, state='ready')
                session.cleanup = {}
            threading.Thread(target=self._watch, args=(session, generation), daemon=True,
                             name='acp-authority').start()
        except (OSError, MCPError, BusinessError) as exc:
            if transport is not None:
                self._stop(session, state='failed', generation=generation)
            else:
                session.record['state'] = 'failed'
                if (isinstance(exc, (OSError, BusinessError))
                        or isinstance(exc, MCPError) and exc.effect_outcome == 'not_started'):
                    session.cleanup = {'terminated': True, 'process_group_released': True, 'duration_ms': 0}
            if isinstance(exc, BusinessError):
                raise
            raise BusinessError('EXTERNAL_PROCESS_FAILED') from exc

    def _stop(self, session, *, state='closed', generation=None):
        with session.lock:
            if generation is not None and getattr(session, 'generation', None) != generation:
                return deepcopy(session.cleanup)
            session.closing = True
            transport = session.transport
            original_generation = getattr(session, 'generation', None)
            # 确认过的同一实例返回原回执；未确认的重试只作用于原 transport。
            if transport is None:
                receipt = session.cleanup or {'terminated': False, 'process_group_released': False, 'duration_ms': 0}
                session.record['state'] = state if receipt['process_group_released'] else 'failed'
                session.stopped.set()
                return deepcopy(receipt)
        self._expire(session, cancel=True, generation=original_generation)
        receipt = _cleanup(transport)
        with session.lock:
            if generation is not None and getattr(session, 'generation', None) != generation:
                return receipt
            if session.transport is not transport:
                return receipt
            session.cleanup = receipt
            session.record['state'] = state if receipt['process_group_released'] else 'failed'
            if session.turn_state in ('running', 'cancelling'):
                session.turn_state = 'cancelled'
            session.stopped.set()
        return deepcopy(receipt)

    def shutdown(self):
        """正常解释器退出及显式宿主关闭共用此口，返回每个原会话的真实清理回执。"""
        with self.lock:
            self.closed.set()
            sessions, probes = tuple(self.sessions.items()), tuple(self.probe_transports.items())
        results = {}
        for identifier, session in sessions:
            if session.record['state'] == 'starting' and not session.started.is_set():
                # 已交给启动线程的责任先中断已登记管道，再等启动方交回句柄，不能漏过晚发表。
                if session.transport is not None:
                    self._stop(session)
                session.started.wait(_probe_timeout)
            results[identifier] = self._stop(session)
            try:
                self._save(session)
            except Exception:
                pass
        for identifier, transport in probes:
            results['probe:' + identifier] = _cleanup(transport)
            with self.lock:
                if results['probe:' + identifier]['process_group_released']:
                    self.probe_transports.pop(identifier, None)
        return results

    def revoke_owner(self):
        """撤权只收回当前 owner 原资源，不接受参数中的 owner 或跨 owner 的会话编号。"""
        return self.shutdown()

    def _watch(self, session, generation):
        while not session.stopped.wait(0.05):
            if session.generation != generation:
                return
            if self.closed.is_set():
                self._stop(session, generation=generation)
                return
            try:
                session.authority()
                if not getattr(self.host.config, 'acp_bridge_enabled', False):
                    raise BusinessError('HOST_NOT_READY')
                self._expire(session)
            except Exception:
                self._stop(session, generation=generation)
                return

    # LLM: 原连接退出事实按代只发布一次；回合失败先取得清理责任时，迟到回调不得漏报或再次清理。
    # 函数用途: 记录同代退出及活动回合失败，保留主动关闭和重启后的资源边界。
    def _exited(self, session, generation):
        with session.lock:
            if (getattr(session, 'generation', None) != generation
                    or session.exited_generation == generation):
                return
            if session.closing:
                # 响应箱关闭先唤醒回合线程；它已记失败并接管清理时，这里只补原退出事实。
                if session.failed_turn_generation == generation:
                    session.exited_generation = generation
                    session.emit('process_exited')
                return
            session.exited_generation = generation
            was_running = session.turn_state in ('running', 'cancelling')
            session.record['state'] = 'failed'
            session.turn_state = 'failed'
            session.emit('process_exited')
            if was_running:
                session.emit('turn_failed', {'reason': 'process_exited'})
        self._stop(session, state='failed', generation=generation)
        try:
            self._save(session)
        except Exception:
            # 撤权后仍须回收进程；不能为了落盘重获旧写权限。
            pass

    def _events(self, session, params):
        after, limit = params['after_seq'], params.get('limit', 50)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise BusinessError('INVALID_SCHEMA')
        with session.lock:
            oldest = session.events[0]['seq'] if session.events else session.seq + 1
            overflow = after < oldest - 1
            events = []
            if overflow:
                events.append({'seq': oldest - 1, 'kind': 'overflow', 'at': session.clock(),
                    'data': {'first_available_seq': oldest}})
            for event in session.events:
                if len(events) >= limit:
                    break
                if event['seq'] > after:
                    events.append(deepcopy(event))
            return {'events': events, 'next_seq': events[-1]['seq'] if events else after, 'overflow': overflow}

    def _control(self, call, action, session):
        params = call.params
        self._expire(session)
        if action == 'close':
            receipt = self._stop(session)
            return {'closed': receipt['process_group_released'], 'cleanup': receipt}
        if action in ('restart', 'resume'):
            if session.transport is not None and session.transport.is_running() and action == 'resume':
                return {'session': session.public()}
            receipt = self._stop(session)
            if not receipt['process_group_released']:
                raise BusinessError('EXTERNAL_PROCESS_FAILED')
            self._start(session, load=action == 'resume')
            return {'session': session.public(), **({'cleanup': receipt} if action == 'restart' else {})}
        if action == 'config.set':
            key, value = params['key'], params['value']
            if key != 'mode' or type(value) is not str or not value or len(value) > 128:
                raise BusinessError('INVALID_SCHEMA')
            with session.lock:
                session.configs[key] = {'value': value, 'state': 'pending_next_turn'}
                session.emit('config_state', {'key': key, 'value': value, 'state': 'pending_next_turn'})
            return {'key': key, 'state': 'pending_next_turn'}
        if action == 'prompt':
            with session.lock:
                if (session.record['state'] != 'ready' or session.transport is None
                        or not session.transport.is_running()):
                    raise BusinessError('STATE_CONFLICT')
                identifier = 'turn-' + uuid.uuid4().hex
                session.record.update(turn_id=identifier, state='running')
                session.turn_state = 'running'
                binding = (session.transport, session.generation, identifier, session.record['acp_session_id'])
                session.worker = threading.Thread(target=self._turn, args=(session, deepcopy(params['content']), binding),
                                                   name='acp-turn', daemon=True)
                session.worker.start()
            return {'turn_id': identifier, 'state': 'running'}
        if action == 'cancel_turn':
            with session.lock:
                if params['turn_id'] != session.record.get('turn_id'):
                    raise BusinessError('RESOURCE_NOT_FOUND')
                state = session.turn_state
                if state in ('completed', 'cancelled', 'failed'):
                    return {'turn_id': params['turn_id'], 'state': 'cancelled' if state == 'cancelled' else 'completed'}
                session.turn_state = 'cancelling'
            self._expire(session, cancel=True)
            try:
                session.transport.notify('session/cancel', {'sessionId': session.record['acp_session_id']})
            except MCPError:
                pass
            return {'turn_id': params['turn_id'], 'state': 'cancelling'}
        if action == 'permission.decide':
            return self._decide(session, params)
        if action == 'answer':
            return self._answer(session, params)
        raise BusinessError('INVALID_SCHEMA')

    def _apply_config(self, session, transport, generation, authority):
        with session.lock:
            pending = [(key, value) for key, value in session.configs.items() if value['state'] == 'pending_next_turn']
        for key, item in pending:
            transport.request('session/set_mode', {'sessionId': session.record['acp_session_id'],
                'modeId': item['value']}, timeout=_probe_timeout, authority_check=authority)
            with session.lock:
                if session.generation != generation or session.configs.get(key) is not item:
                    continue
                item['state'] = 'command_ack'
                session.emit('config_state', {'key': key, 'value': item['value'], 'state': 'command_ack'})
                if item.get('observed_value') == item['value']:
                    item['state'] = 'observed'
                    session.emit('config_state', {'key': key, 'value': item['value'], 'state': 'observed'})

    # LLM: 回合失败及其清理资格固定原连接代；重启保留的展示状态不能给新连接的迟到回调授予补报资格。
    # 函数用途: 执行当前ACP回合并发表完成或失败，保存同代失败清理责任。
    def _turn(self, session, content, binding=None):
        with session.lock:
            transport, generation, turn_id, acp_id = binding or (
                session.transport, session.generation, session.record['turn_id'], session.record['acp_session_id'])
        def authority():
            session.authority()
            self._require_root(session.root)
            with session.lock:
                if (session.generation != generation or session.transport is not transport or session.closing
                        or session.record.get('turn_id') != turn_id):
                    raise MCPError('原 ACP 回合已失效', code='MCP_CONNECTION_CLOSED')
        try:
            self._apply_config(session, transport, generation, authority)
            # resource_link 是业务 DTO 的名字，ACP v1 的 wire 类型为 resource_link，同字段直接传递。
            response = transport.request('session/prompt', {'sessionId': acp_id,
                'prompt': content}, timeout=getattr(self.host.config, 'request_timeout', 120),
                authority_check=authority)
            if type(response) is not dict or type(response.get('stopReason')) is not str:
                raise MCPError('ACP 回合响应无效', code='MCP_PROTOCOL_ERROR')
            with session.lock:
                if session.generation != generation or session.closing or session.record['state'] == 'failed':
                    return
                session.turn_state = 'cancelled' if response['stopReason'] == 'cancelled' else 'completed'
                session.record['state'] = 'ready'
                session.emit('turn_completed', {'stop_reason': response['stopReason']})
        except Exception:
            with session.lock:
                if session.generation != generation or session.closing or session.record['state'] == 'failed':
                    return
                session.turn_state = 'failed'
                session.failed_turn_generation = generation
                session.record['state'] = 'failed'
                session.emit('turn_failed', {'reason': 'protocol_or_process_failed'})
            self._stop(session, state='failed', generation=generation)
        finally:
            try:
                self._save(session, generation=generation)
            except Exception:
                pass

    def _update(self, session, generation, params):
        if (getattr(session, 'generation', None) != generation
                or params.get('sessionId') != session.record.get('acp_session_id')):
            return
        value = normalize_update(params.get('update'))
        if value is None:
            return
        kind, data = value
        with session.lock:
            if session.generation != generation or session.closing:
                return
            session.emit(kind, data)
            if kind == 'config_state' and data.get('sessionUpdate') == 'current_mode_update':
                item = session.configs.get('mode')
                if item is not None:
                    item['observed_value'] = data.get('currentModeId')
                    if item['state'] == 'command_ack' and item['value'] == item['observed_value']:
                        item['state'] = 'observed'
                        session.emit('config_state', {'key': 'mode', 'value': item['value'], 'state': 'observed'})

    # LLM: 审批寿命只读可信HomePaths绑定的部署配置，坏源失败关闭，不能用请求级host或默认值盖住读取失败。
    # 函数用途: 将本连接的文件、审批和表单请求交给对应owner边界及独立状态机。
    def _request(self, session, generation, method, params, req_id):
        known = ('session/request_permission', 'elicitation/create', 'fs/read_text_file', 'fs/write_text_file')
        if method not in known:
            raise LookupError(method)
        if (getattr(session, 'generation', None) != generation or session.closing
                or params.get('sessionId') != session.record['acp_session_id']):
            raise BusinessError('RESOURCE_NOT_FOUND')
        session.authority()
        if method.startswith('fs/'):
            return self._file(session, method, params, generation=generation)
        with session.lock:
            if session.generation != generation or session.closing:
                raise BusinessError('RESOURCE_NOT_FOUND')
            # 终态保留有界回查；只淘汰已回答的项，不擦除尚未答复的请求责任。
            collection = session.permissions if method == 'session/request_permission' else session.questions
            for identifier in tuple(collection):
                if len(collection) < _event_ring:
                    break
                if collection[identifier]['state'] not in ('requested', 'asked'):
                    collection.pop(identifier)
            if len(collection) >= _event_ring:
                raise BusinessError('LIMIT_EXCEEDED')
            if any(item['rpc_id'] == req_id and item['state'] in ('requested', 'asked')
                   for item in (*session.permissions.values(), *session.questions.values())):
                raise BusinessError('STATE_CONFLICT')
            try:
                config = capability_config_for_paths(root=getattr(self.host.home_paths, 'root', None))
            except (OSError, ValueError, TypeError) as exc:
                raise BusinessError('HOST_NOT_READY') from exc
            if config is None:
                raise BusinessError('HOST_NOT_READY')
            timeout = config.tool_approval_timeout_seconds
            item = {'rpc_id': req_id, 'params': deepcopy(params), 'expires_at': session.clock() + timeout,
                    'generation': generation, 'transport': session.transport}
            if method == 'session/request_permission':
                options = params.get('options')
                if (type(options) is not list or not options or len(options) > 64
                        or any(type(option) is not dict or type(option.get('optionId')) is not str
                               or option.get('kind') not in ('allow_once', 'allow_always', 'reject_once', 'reject_always')
                               for option in options)
                        or len({option['optionId'] for option in options}) != len(options)
                        or type(params.get('toolCall')) is not dict):
                    raise BusinessError('INVALID_SCHEMA')
                identifier = 'permission-' + uuid.uuid4().hex
                item['state'] = 'requested'
                session.permissions[identifier] = item
                session.emit('permission_requested', {'permission_request_id': identifier,
                    'tool_call': params['toolCall'], 'options': options, 'expires_at': item['expires_at']})
            else:
                schema = params.get('requestedSchema')
                if (params.get('mode') != 'form' or type(schema) is not dict or schema.get('type') != 'object'
                        or type(schema.get('properties')) is not dict or len(schema['properties']) > 64):
                    raise BusinessError('INVALID_SCHEMA')
                identifier = 'question-' + uuid.uuid4().hex
                item['state'] = 'asked'
                session.questions[identifier] = item
                session.emit('question_asked', {'question_id': identifier, 'message': params.get('message', ''),
                    'requested_schema': schema, 'expires_at': item['expires_at']})
        return None

    def _expire(self, session, *, cancel=False, generation=None):
        replies = []
        with session.lock:
            if generation is not None and getattr(session, 'generation', None) != generation:
                return
            for collection, active, kind, id_key in (
                    (session.permissions, 'requested', 'permission_resolved', 'permission_request_id'),
                    (session.questions, 'asked', 'question_answered', 'question_id')):
                for identifier, item in collection.items():
                    if item['state'] == active and (cancel or session.clock() >= item['expires_at']):
                        item['state'] = 'expired'
                        session.emit(kind, {id_key: identifier, 'state': 'expired'})
                        replies.append((item['transport'], item['rpc_id'], {'outcome': {'outcome': 'cancelled'}}
                                        if active == 'requested' else {'action': 'cancel'}))
        for transport, req_id, response in replies:
            try:
                transport.reply(req_id, result=response)
            except MCPError:
                pass

    def _decide(self, session, params):
        identifier = params['permission_request_id']
        with session.lock:
            item = session.permissions.get(identifier)
            if item is None:
                raise BusinessError('RESOURCE_NOT_FOUND')
            if item['state'] == 'expired':
                return {'permission_request_id': identifier, 'state': 'expired'}
            if item['state'] != 'requested':
                raise BusinessError('STATE_CONFLICT')
            decision = params['decision']
            candidates = [option for option in item['params']['options']
                          if option['kind'].startswith('allow_' if decision == 'approved' else 'reject_')]
            selected = next((option for option in candidates if option['optionId'] == params.get('option_id')), None)
            if selected is None and 'option_id' not in params and len(candidates) == 1:
                selected = candidates[0]
            if selected is None:
                raise BusinessError('STATE_CONFLICT')
            # 先发布单调终态；回包丢失不能让后续决定重发不同授权。
            item['state'] = decision
            session.emit('permission_resolved', {'permission_request_id': identifier, 'state': decision})
        try:
            item['transport'].reply(item['rpc_id'], result={'outcome': {
                'outcome': 'selected', 'optionId': selected['optionId']}})
        except MCPError as exc:
            raise BusinessError('OUTCOME_UNKNOWN') from exc
        return {'permission_request_id': identifier, 'state': decision}

    def _answer(self, session, params):
        from ..plugin_host.schemas import validate_object
        identifier = params['question_id']
        with session.lock:
            item = session.questions.get(identifier)
            if item is None:
                raise BusinessError('RESOURCE_NOT_FOUND')
            if item['state'] == 'expired':
                return {'question_id': identifier, 'state': 'expired'}
            if item['state'] != 'asked':
                raise BusinessError('STATE_CONFLICT')
            schema = item['params']['requestedSchema']
            properties = schema['properties']
            if len(properties) == 1 and next(iter(properties.values())).get('type') == 'string':
                content = {next(iter(properties)): params['answer']}
            else:
                try:
                    content = json.loads(params['answer'])
                except ValueError as exc:
                    raise BusinessError('STATE_CONFLICT') from exc
            try:
                content = validate_object(content, schema)
            except Exception as exc:
                raise BusinessError('STATE_CONFLICT') from exc
            item['state'] = 'answered'
            session.emit('question_answered', {'question_id': identifier, 'state': 'answered'})
        try:
            item['transport'].reply(item['rpc_id'], result={'action': 'accept', 'content': content})
        except MCPError as exc:
            raise BusinessError('OUTCOME_UNKNOWN') from exc
        return {'question_id': identifier, 'state': 'answered'}

    # LLM: 受信锚点只能是文件系统根；完整项目父链必须仍经no-follow验证，不能把可替换的项目路径重新授信。
    # 函数用途: 在owner文件锁内校验范围和SHA版本，再沿完整目录链读写原目标。
    def _file(self, session, method, params, *, generation=None):
        path = params.get('path')
        _parts(path, absolute=True)
        target = Path(path)
        self._require_root(session.root)
        try:
            parts = target.relative_to(session.root).parts
            if not parts or '.git' in tuple(part.casefold() for part in parts):
                raise ValueError('工作区目标无效')
            _forbid_internal(self.owner_home, target)
        except Exception as exc:
            raise BusinessError('PATH_OUTSIDE_WORKSPACE') from exc
        anchor, file_parts = target.anchor, target.parts[1:]
        limit = getattr(self.host.config, 'tool_read_max_chars', 100000)
        with locked_private_directory(self.owner_home, relative_parts=('data', 'business_v13'), lock_name='.acp-files.lock'), session.lock:
            if generation is not None and (getattr(session, 'generation', None) != generation or session.closing):
                raise BusinessError('RESOURCE_NOT_FOUND')
            session.authority()
            raw = read_bytes_beneath(anchor, file_parts, max_bytes=limit, require_dir_fd=True)
            version = 'absent' if raw is None else hashlib.sha256(raw).hexdigest()
            if method == 'fs/read_text_file':
                if raw is None:
                    raise BusinessError('RESOURCE_NOT_FOUND')
                text = raw.decode('utf-8')
                start, count = params.get('line', 1), params.get('limit', limit)
                if type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= limit:
                    raise BusinessError('INVALID_SCHEMA')
                session.file_versions[str(target)] = version
                # 缓存仅用于相同会话的隐式CAS；越过上限淘汰后必须重新读取或显式传版本。
                while len(session.file_versions) > _event_ring:
                    session.file_versions.pop(next(iter(session.file_versions)))
                return {'content': ''.join(text.splitlines(keepends=True)[start - 1:start - 1 + count]),
                        '_meta': {'sha256': version}}
            metadata = params.get('_meta', {})
            if type(metadata) is not dict:
                raise BusinessError('INVALID_SCHEMA')
            expected = metadata.get('expected_sha256', session.file_versions.get(str(target), 'absent'))
            if (type(expected) is not str or expected != 'absent' and re.fullmatch('[0-9a-f]{64}', expected) is None
                    or expected != version):
                raise BusinessError('STATE_CONFLICT')
            text = params.get('content')
            if type(text) is not str or len(text.encode('utf-8')) > limit:
                raise BusinessError('INVALID_SCHEMA')
            session.authority()
            write_bytes_atomic_beneath(anchor, file_parts, text.encode('utf-8'))
            session.file_versions[str(target)] = hashlib.sha256(text.encode('utf-8')).hexdigest()
            while len(session.file_versions) > _event_ring:
                session.file_versions.pop(next(iter(session.file_versions)))
            return {}


def bridge_for(call):
    """只从当前已发行宿主取 owner 实例，参数不能注入桥接器或选取别人的缓存。"""
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    host = call.context.host
    scope = getattr(host, 'business_v13_gateway_paths', host)
    key = (id(scope), str(Path(call.owner_home).resolve()), call.owner_id)
    # 原桥保留可信宿主及 scope 引用；HTTP 临时宿主和 services 淘汰不改变 transport 所有权。
    with _bridge_lock:
        bridge = _bridges.get(key)
        if bridge is None:
            bridge = AcpBridge(call)
            _bridges[key] = bridge
        if bridge.owner_id != call.owner_id or bridge.owner_home != Path(call.owner_home).resolve():
            raise BusinessError('RESOURCE_NOT_FOUND')
        host._gap158_acp_bridge = bridge
        bridge.host.config = host.config
        return bridge


_bridge_lock = threading.Lock()
_bridges = {}
