"""通知配置与有界 outbox 投递；外部副作用沿原运行库保持至少一次语义。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..common.request_execution_ports import RequestExecutionOutcomeUncertainError
from ..contracts.gates.network_safety import (
    GatewayEndpointConfig,
    NetworkSafetySettings,
    _canonical_ip,
)
from ..conversation.background_process_owner import canonical_observation_owner_id
from ..conversation.business_access import BusinessError
from ..gateway_parts.multiprocess_background_execution import (
    background_short_operation,
    require_background_model_admission,
)
from ..plugin_host.errors import HostAPIError
from ..runtime_db.operations import RuntimeConflictError
from ..runtime_db.repository import RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from ..tooling.web import _default_network_resolver, _network_safety_error, _normalize_url
from ..tooling.web_fetch_runtime import (
    _connection_timed_out,
    _PinnedHTTPSConnection,
    _start_request_deadline,
    _target_for,
)
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .v13_store import V13Store, _page_rows, v13_store_for

_executor = None
_worker_slots = None
_worker_lock = threading.Lock()
_cursor_key = os.urandom(32)
_dns_slots = None


def _resolve_bounded(host, timeout):
    """系统解析不可强杀；用有界 daemon 槽隔离，超时后不增开线程补偿。"""
    global _dns_slots
    with _worker_lock:
        if _dns_slots is None:
            _dns_slots = threading.BoundedSemaphore(_limit('NOTIFY_WORKERS'))
        slots = _dns_slots
    if timeout <= 0 or not slots.acquire(blocking=False):
        raise TimeoutError('resolver capacity or deadline')
    done, results, errors = threading.Event(), [], []
    resolver = _default_network_resolver

    def resolve():
        try:
            results.append(tuple(resolver(host)))
        except BaseException as error:
            errors.append(error)
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(target=resolve, name='notify-dns', daemon=True).start()
    except BaseException:
        slots.release()
        raise
    if not done.wait(timeout=timeout):
        raise TimeoutError('resolver deadline')
    if errors:
        raise errors[0]
    return results[0]


def _limit(name):
    """W0 未合入时只用明确有界的函数内默认值，不复制常量目录。"""
    defaults = {'NOTIFY_CHANNELS_MAX_PER_OWNER': 20, 'NOTIFY_DRAIN_BATCH': 10, 'NOTIFY_WORKERS': 2}
    try:
        limits = importlib.import_module(__package__ + '.gap158_limits')
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.gap158_limits':
            raise
        return defaults[name]
    value = getattr(limits, name, defaults[name])
    return value if type(value) is int and value > 0 else defaults[name]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _config(call):
    return call.context.host.config


def _timeout(config):
    value = getattr(config, 'tool_http_timeout', 30)
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else 30.0


def notify_ready(call):
    """供 V13 ready= 使用；不构造执行 Agent，也不发送网络请求。"""
    return bool(getattr(_config(call), 'notify_delivery_enabled', False))


def _validate_url(value, config, previous=(), *, deadline=None):
    """配置检查和发送共用有界解析；传入期限时 DNS 消耗计入同次 HTTP 总预算。"""
    deadline = time.monotonic() + _timeout(config) if deadline is None else deadline
    try:
        if type(value) is not str or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError('url')
        url = _normalize_url(value)
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.fragment or '\\' in url:
            raise ValueError('https')
        host = (parts.hostname or '').lower().rstrip('.')
        literal = _canonical_ip(host)
        resolved = (literal,) if literal else _resolve_bounded(host, deadline - time.monotonic())
        settings = NetworkSafetySettings(
            allowed_private_hosts=tuple(getattr(config, 'allowed_private_hosts', ()) or ()),
            allow_private_resolution=False, previous_resolved_ips=tuple(previous),
            gateway_endpoint=GatewayEndpointConfig(getattr(config, 'gateway_port', None)))
        if not resolved or _network_safety_error('notify', url, lambda _host: resolved, settings) is not None:
            raise ValueError('outbound')
        return url, resolved
    except TimeoutError:
        raise BusinessError('HOST_NOT_READY') from None
    except (ValueError, TypeError, OSError):
        # 不把异常文本（可能含 URL token）放进错误详情或回执。
        raise BusinessError('OUTBOUND_DOMAIN_DENIED') from None


def _secret_env(value):
    if value and (type(value) is not str or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value) is None):
        raise BusinessError('INVALID_SCHEMA')
    return value or ''


def _public_channel(row):
    parts = urlsplit(row['url'])
    return {key: row[key] for key in ('channel_id', 'kind', 'name', 'events', 'enabled', 'secret_env', 'created_at')} | {
        'url_redacted': 'https://' + parts.netloc + '/[redacted]'}


def _own_channel(rows, channel_id, owner_id):
    row = rows.get(channel_id)
    if row is None or row.get('owner_id') != owner_id:
        raise BusinessError('RESOURCE_NOT_FOUND')
    return row


def _transaction(call, mutate, *, cas=False):
    store = v13_store_for(call, 'owner')
    return store.transaction('notify_channels', lambda rows: mutate(rows, store),
        idempotency_key=call.idempotency_key,
        params_digest=hashlib.sha256(_json([call.action, call.params])).hexdigest(),
        expected_revision=call.expected_revision if cas else None)


def _channel_write(call):
    params = dict(call.params)
    if call.action != 'notify.channels.create' and (type(call.expected_revision) is not int or call.expected_revision < 0):
        raise BusinessError('INVALID_SCHEMA')
    if 'secret_env' in params:
        params['secret_env'] = _secret_env(params['secret_env'])
    checked = _validate_url(params['url'], _config(call)) if 'url' in params else None

    def mutate(rows, store):
        # 幂等回执由原 Store 点查；重放不会重做配置提交或发送网络副作用。
        if call.action != 'notify.channels.create':
            _own_channel(rows, params['channel_id'], call.owner_id)
        if call.action == 'notify.channels.create':
            if len(rows) >= _limit('NOTIFY_CHANNELS_MAX_PER_OWNER'):
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': _limit('NOTIFY_CHANNELS_MAX_PER_OWNER')})
            channel_id = 'notify-' + uuid.uuid4().hex
            row = {'owner_id': call.owner_id, 'channel_id': channel_id, 'kind': 'webhook',
                'name': params['name'], 'events': list(params['events']),
                'enabled': params.get('enabled', True), 'secret_env': params.get('secret_env', ''),
                'url': checked[0], 'resolved_ips': list(checked[1]), 'created_at': call.clock()}
            rows[channel_id] = row
        else:
            row = _own_channel(rows, params['channel_id'], call.owner_id)
            if call.action == 'notify.channels.delete':
                del rows[row['channel_id']]
                return {'deleted': True, 'revision': store.revision('notify_channels') + 1}
            for key in ('name', 'events', 'enabled', 'secret_env'):
                if key in params:
                    row[key] = params[key]
            if checked:
                row.update(url=checked[0], resolved_ips=list(checked[1]))
        return {'channel': _public_channel(row), 'revision': store.revision('notify_channels') + 1}

    return _transaction(call, mutate, cas=call.action != 'notify.channels.create')


def _channels_list(call):
    store = v13_store_for(call, 'owner')
    rows, revision = store._record_view('notify_channels', None, all_rows=True)
    page = _page_rows([_public_channel(row) for row in rows if row.get('owner_id') == call.owner_id],
        [str(call.owner_home), call.owner_id, call.subject_id, 'notify_channels', revision],
        cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    if page['next_cursor'] is None:
        del page['next_cursor']
    return {'revision': revision, **page}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHandler(HTTPSHandler):
    """urllib 连接复用原安全传输的 IP pin 与原域名 TLS 校验，禁用环境代理。"""
    def __init__(self, url, pin_ip):
        super().__init__()
        self.target = _target_for(url, pin_ip)

    def https_open(self, request):
        connections = []

        def connection(_host, timeout=None):
            import ssl
            conn = _PinnedHTTPSConnection(self.target, timeout, ssl.create_default_context())
            connections.append(conn)
            # 与原传输共用总期限中断，不能被持续滴流的响应头拖成长连接。
            _start_request_deadline(conn, timeout)
            return conn

        try:
            return self.do_open(connection, request)
        except Exception:
            if any(_connection_timed_out(conn) for conn in connections):
                raise TimeoutError() from None
            raise
        finally:
            for conn in connections:
                conn._request_deadline_timer.cancel()
                conn.close()


def _send_webhook(url, *, body, headers, timeout, pin_ip):
    """唯一真实传输边界；测试在此替换，既不读响应正文也不跟随重定向。"""
    opener = build_opener(ProxyHandler({}), _NoRedirect(), _PinnedHandler(url, pin_ip))
    request = Request(url, body, headers, method='POST')
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status)
    except HTTPError as error:
        try:
            return int(error.code)
        finally:
            error.close()


def _runtime(home, *, read_only=False):
    repo = RuntimeRepository(runtime_db_path(home), read_only=read_only)
    if not read_only:
        # W0 尚未合入时在写消费者的初始化提交兼容索引，只读查询不执行迁移。
        repo.prepare_outbox_pages()
    return repo


def _get_delivery(repo, owner_id, outbox_id):
    # 点查也在 SQL 中核 scope/owner，不先读异主正文再做 Python 筛选。
    with repo._runtime_connection() as connection:
        row = connection.execute('SELECT * FROM outbox_entries WHERE scope=? AND owner_id=? AND outbox_id=?',
            ('notify', owner_id, outbox_id)).fetchone()
    if row is None:
        raise BusinessError('RESOURCE_NOT_FOUND')
    return repo._outbox_row_to_dict(row)


def _attempt(repo, row, channels, config, clock):
    """一次领取只做一次发送；总预算从解析前开始，结算必须仍持有准确 claim。"""
    started = time.monotonic()
    deadline = started + _timeout(config)
    evidence = {'ok': False, 'http_status': 0, 'error_code': ''}
    try:
        if not getattr(config, 'notify_delivery_enabled', False):
            raise BusinessError('HOST_NOT_READY')
        channel = _own_channel(channels, row['payload']['channel_id'], row['owner_id'])
        if not channel['enabled'] and not row['payload'].get('test'):
            raise BusinessError('HOST_NOT_READY')
        url, resolved = _validate_url(channel['url'], config, channel.get('resolved_ips', ()), deadline=deadline)
        body = _json({key: row['payload'][key] for key in ('event', 'event_id', 'payload')})
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': row['effect_key']}
        if channel['secret_env']:
            secret = os.environ.get(channel['secret_env'])
            if not secret:
                raise BusinessError('HOST_NOT_READY')
            headers['X-MyAgent-Signature'] = 'sha256=' + hmac.digest(secret.encode(), body, 'sha256').hex()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('delivery deadline')
        require_background_model_admission()
        status = _send_webhook(url, body=body, headers=headers, timeout=remaining, pin_ip=resolved[0])
        evidence.update(ok=200 <= status < 300, http_status=status)
        if not evidence['ok']:
            evidence['error_code'] = 'NETWORK_REQUEST_FAILED'
    except RequestExecutionOutcomeUncertainError:
        raise
    except (BusinessError, HostAPIError) as error:
        evidence['error_code'] = error.code
    except TimeoutError:
        evidence['error_code'] = 'TOOL_TIMEOUT'
    except Exception:
        # 外部传输可抛任意库异常；只记结构化类别，不保存 URL、密钥或远端回显。
        evidence['error_code'] = 'NETWORK_REQUEST_FAILED'
    evidence['latency_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return background_short_operation('notify_settlement', lambda: repo.settle_outbox_for_claim(
        claim=row, status='ACKED' if evidence['ok'] else 'FAILED', provider_evidence=evidence,
        next_retry_at=0 if evidence['ok'] else clock() + max(1.0, _timeout(config))))


def _submit(function, args=()):
    """执行器的队列也有界：满载不排内存任务，耐久 pending 留给下一次触发。"""
    global _executor, _worker_slots
    with _worker_lock:
        if _executor is None:
            workers = _limit('NOTIFY_WORKERS')
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='notify')
            _worker_slots = threading.BoundedSemaphore(workers)
        slots = _worker_slots
        if not slots.acquire(blocking=False):
            return None
        try:
            context = copy_context()
            future = Context().run(_executor.submit, context.run, function, *args)
        except BaseException:
            slots.release()
            raise
    future.add_done_callback(lambda _future: slots.release())
    return future


def _read_channels(home, owner_id):
    store = V13Store(home, owner_id=owner_id)
    store._readonly = True
    return {row['channel_id']: row for row in store.list('notify_channels')}


def _recover_expired(repo, owner_id, config, clock):
    """每次消费只回收一片过期领取；副作用不明仍保持至少一次语义。"""
    return background_short_operation('notify_recovery', lambda: repo.recover_outbox_for_scope(
        scope='notify', owner_id=owner_id, now=clock(), lease_seconds=max(1.0, 2 * _timeout(config)),
        limit=min(100, _limit('NOTIFY_DRAIN_BATCH'))))


def notification_deadline(home, config=None):
    """只读原 owner 的自动通知责任；分别走状态/期限索引，不读 ACKED 历史。"""
    from ..owner_wake_discovery import _quiescent_probe_connection

    owner_id = canonical_observation_owner_id(Path(home))
    if (not owner_id or not runtime_db_path(home).is_file()
            or config is not None and not getattr(config, 'notify_delivery_enabled', False)):
        return None
    deadlines = []
    with _quiescent_probe_connection(Path(home)) as conn:
        if conn is None:
            return 0.0
        for status in ('PENDING', 'FAILED', 'IN_FLIGHT'):
            column = 'claimed_at' if status == 'IN_FLIGHT' else 'next_retry_at'
            ordering = 'claimed_at,outbox_id' if status == 'IN_FLIGHT' else 'next_retry_at,created_at,outbox_id'
            row = conn.execute(f'SELECT {column} FROM outbox_entries WHERE scope=? AND owner_id=? '
                'AND status=? AND json_extract(payload_json,\'$.event\')=\'scheduler_delivery\' '
                f'ORDER BY {ordering} LIMIT 1', ('notify', owner_id, status)).fetchone()
            if row is not None:
                deadlines.append(row[0] + (max(1.0, 2 * _timeout(config)) if status == 'IN_FLIGHT' else 0))
    return min(deadlines) if deadlines else None


def resume_notifications(agent, *, now=None):
    """由既有调度 tick 重试一片责任；满载保留 outbox，不另建 owner 队列或定时器。"""
    config, paths = agent.config, agent.home_paths
    if not getattr(config, 'notify_delivery_enabled', False):
        return None
    home, owner_id = paths.owner_home_dir, paths.owner_id
    if canonical_observation_owner_id(Path(home)) != owner_id:
        raise BusinessError('RESOURCE_NOT_FOUND')
    clock = getattr(agent, 'business_v13_clock', time.time)
    deadline = notification_deadline(home, config)
    if deadline is None or deadline > (clock() if now is None else now):
        return None
    repo = background_short_operation('notify_prepare', lambda: _runtime(home))
    return _kick(repo, home, owner_id, config, clock, True)


def _drain(repo, home, owner_id, config, clock, automatic_only=False):
    if not getattr(config, 'notify_delivery_enabled', False):
        return []
    _recover_expired(repo, owner_id, config, clock)
    # 集合有自然上限，一批只读一次；逐条领取避免结算失败时把未开始的整批锁死。
    channels = _read_channels(home, owner_id)
    results = []

    def claim():
        outbox_id = ''
        if automatic_only:
            # 原入队触发保持通用事件语义；周期续跑只消费已完成调度的自动通知。
            with repo._runtime_connection() as conn:
                row = conn.execute("SELECT outbox_id FROM outbox_entries WHERE scope=? AND owner_id=? "
                    "AND status IN ('PENDING','FAILED') AND next_retry_at<=? "
                    "AND json_extract(payload_json,'$.event')='scheduler_delivery' "
                    "ORDER BY next_retry_at,created_at,outbox_id LIMIT 1", ('notify', owner_id, clock())).fetchone()
            if row is None:
                return []
            outbox_id = row[0]
        return repo.claim_outbox_for_scope(scope='notify', owner_id=owner_id,
            claimed_by='notify-' + uuid.uuid4().hex, batch_size=1, now=clock(), outbox_id=outbox_id)

    for _ in range(min(100, _limit('NOTIFY_DRAIN_BATCH'))):
        if not getattr(config, 'notify_delivery_enabled', False):
            break
        rows = background_short_operation('notify_claim', claim)
        if not rows:
            break
        results.append(_attempt(repo, rows[0], channels, config, clock))
    return results


def _kick(repo, home, owner_id, config, clock, automatic_only=False):
    return _submit(_drain, (repo, home, owner_id, config, clock, automatic_only))


def _target_once(repo, owner_id, outbox_id, channels, config, clock):
    current = _get_delivery(repo, owner_id, outbox_id)
    if current['status'] not in ('PENDING', 'FAILED'):
        return current
    # 显式重试可以越过该目标的退避，但不能提前领取其它行或消费其它 scope。
    claimed = repo.claim_outbox_for_scope(scope='notify', owner_id=owner_id,
        claimed_by='notify-' + uuid.uuid4().hex, batch_size=1,
        now=max(clock(), current['next_retry_at']), outbox_id=outbox_id)
    return (_attempt(repo, claimed[0], channels, config, clock) if claimed
        else _get_delivery(repo, owner_id, outbox_id))


def _synchronous_attempt(repo, call, outbox_id, channels):
    future = _submit(_target_once, (repo, call.owner_id, outbox_id, channels, _config(call), call.clock))
    if future is None:
        # outbox 已耐久入队，不能把尚待尝试伪报成没有副作用的 HOST_NOT_READY。
        raise BusinessError('OUTCOME_UNKNOWN')
    return future.result()


def _evidence(row):
    return row['provider_evidence'].get('attempt_' + str(row['attempts']), {})


def _control(call):
    if not notify_ready(call):
        raise BusinessError('HOST_NOT_READY')
    repo = _runtime(call.owner_home)
    _recover_expired(repo, call.owner_id, _config(call), call.clock)

    def mutate(channels, _store):
        if call.action == 'notify.channels.test':
            channel = _own_channel(channels, call.params['channel_id'], call.owner_id)
            _validate_url(channel['url'], _config(call), channel.get('resolved_ips', ()))
            event_id = 'test|' + call.idempotency_key
            key = hashlib.sha256(f'{call.owner_id}|{channel["channel_id"]}|{event_id}'.encode()).hexdigest()
            receipt = repo.enqueue_outbox(scope='notify', owner_id=call.owner_id, effect_key=key,
                payload={'channel_id': channel['channel_id'], 'event': 'test', 'event_id': event_id,
                         'payload': {}, 'test': True})
            row = _synchronous_attempt(repo, call, receipt['outbox_id'], channels)
            evidence = _evidence(row)
            return {'ok': row['status'] == 'ACKED', 'outbox_id': row['outbox_id'],
                'latency_ms': evidence.get('latency_ms', 0), 'http_status': evidence.get('http_status', 0),
                'error_code': evidence.get('error_code', '')}
        row = _get_delivery(repo, call.owner_id, call.params['outbox_id'])
        if row['status'] not in ('PENDING', 'FAILED', 'DEAD_LETTER'):
            raise BusinessError('STATE_CONFLICT', details={'state': row['status']})
        if row['status'] == 'DEAD_LETTER':
            try:
                repo.replay_dead_letter(outbox_id=row['outbox_id'])
            except RuntimeConflictError:
                raise BusinessError('STATE_CONFLICT') from None
        row = _synchronous_attempt(repo, call, row['outbox_id'], channels)
        return {key: row[key] for key in ('outbox_id', 'status', 'attempts')}

    # 使用原 Store 的耐久幂等回执，不另造 retry 账本；同键重放不重做网络副作用。
    # 网络只在有界 worker 中执行；回执仅含脱敏 DTO，外部发送不宣称跨库原子。
    result = _transaction(call, mutate)
    _kick(repo, call.owner_home, call.owner_id, _config(call), call.clock)
    return result


def _cursor_binding(call):
    return hashlib.sha256(_json([str(call.owner_home), call.owner_id, call.subject_id, call.action,
        call.params.get('channel_id', ''), call.params.get('status', '')])).hexdigest()


def _after(call):
    cursor = call.params.get('cursor')
    if cursor is None:
        return None
    try:
        token = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
        data = token['data']
        if (type(cursor) is not str or len(cursor) > 2048 or set(token) != {'data', 'signature'}
                or set(data) != {'binding', 'after'} or data['binding'] != _cursor_binding(call)
                or not hmac.compare_digest(hmac.digest(_cursor_key, _json(data), 'sha256').hex(), token['signature'])):
            raise ValueError('cursor')
        position = data['after']
        if (type(position) is not list or len(position) != 2 or type(position[0]) not in (int, float)
                or not math.isfinite(position[0]) or position[0] < 0 or type(position[1]) is not str):
            raise ValueError('position')
        return tuple(position)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise BusinessError('CURSOR_EXPIRED') from None


def _next_cursor(call, after):
    data = {'binding': _cursor_binding(call), 'after': after}
    return base64.urlsafe_b64encode(_json({'data': data,
        'signature': hmac.digest(_cursor_key, _json(data), 'sha256').hex()})).decode('ascii')


def _public_delivery(row):
    return {key: row[key] for key in ('outbox_id', 'status', 'attempts', 'created_at', 'next_retry_at', 'settled_at')} | {
        'channel_id': row['payload'].get('channel_id', ''), 'event': row['payload'].get('event', ''),
        'error_code': _evidence(row).get('error_code', '')}


def _deliveries_list(call):
    limit = call.params.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA')
    after = _after(call)
    if not runtime_db_path(call.owner_home).exists():
        return {'items': [], 'coverage': 'complete'}
    rows, next_after = _runtime(call.owner_home, read_only=True).list_outbox_page(scope='notify',
        owner_id=call.owner_id, status=call.params.get('status', ''), after=after, limit=limit)
    # 可选通道过滤只作用于本次 SQL 有界窗口，不循环补满页，更不全量读历史。
    selected = call.params.get('channel_id', '')
    result = {'items': [_public_delivery(row) for row in rows
        if not selected or row['payload'].get('channel_id') == selected],
        'coverage': 'partial' if next_after else 'complete'}
    if next_after:
        result['next_cursor'] = _next_cursor(call, next_after)
    return result


def enqueue_event(agent, *, owner_id, event_kind, event_id, payload):
    """只消费调度器已认证的结构化事件；不需要或伪造 V13/通道绑定调用上下文。"""
    config = agent.config
    if not getattr(config, 'notify_delivery_enabled', False):
        return []
    paths = agent.home_paths
    identity = OwnerIdentity(paths.owner_provider, paths.owner_kind, Path(paths.owner_home_dir).name)
    owner = resolve_owner_home(paths.root, identity)
    if (not owner_id or owner_id != paths.owner_id or owner_id != owner.owner_id
            or owner.home_dir.resolve() != Path(paths.owner_home_dir).resolve()):
        raise BusinessError('RESOURCE_NOT_FOUND')
    if identity.owner_kind == 'user':
        from ..plugin_host.owner_context import owner_state
        from ..user_space.local_user_store import LocalUserStore
        owner_state(LocalUserStore(paths.root), identity)
    if (type(event_kind) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', event_kind) is None
            or type(event_id) is not str or not event_id or type(payload) is not dict):
        raise BusinessError('INVALID_SCHEMA')
    _json(payload)
    home = owner.home_dir
    channels = _read_channels(home, owner_id)
    repo = _runtime(home)
    receipts = []
    for channel in channels.values():
        if channel.get('owner_id') != owner_id or not channel['enabled'] or event_kind not in channel['events']:
            continue
        key = hashlib.sha256(f'{owner_id}|{channel["channel_id"]}|{event_id}'.encode()).hexdigest()
        receipts.append(repo.enqueue_outbox(scope='notify', owner_id=owner_id, effect_key=key,
            payload={'channel_id': channel['channel_id'], 'event': event_kind,
                     'event_id': event_id, 'payload': payload}))
    _kick(repo, home, owner_id, config, getattr(agent, 'business_v13_clock', time.time))
    return receipts


def handle_notify(call):
    """所有 notify.* 入口共用真实 V13 owner 授权与唯一配置/运行库。"""
    from ..gateway_parts.business_api_v13 import v13_require_access
    v13_require_access(call)
    if call.action == 'notify.meta':
        config = _config(call)
        return {'enabled': notify_ready(call), 'channel_kinds': ['webhook'],
            'max_channels': _limit('NOTIFY_CHANNELS_MAX_PER_OWNER'),
            'outbound_allowlist_configured': bool(getattr(config, 'allowed_private_hosts', ())),
            'delivery_timeout_seconds': _timeout(config)}
    if call.action == 'notify.channels.list':
        return _channels_list(call)
    if call.action in ('notify.channels.create', 'notify.channels.update', 'notify.channels.delete'):
        return _channel_write(call)
    if call.action in ('notify.channels.test', 'notify.deliveries.retry'):
        return _control(call)
    if call.action == 'notify.deliveries.list':
        return _deliveries_list(call)
    raise BusinessError('INVALID_SCHEMA')
