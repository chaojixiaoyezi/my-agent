"""同一业务typed迭代器的native NDJSON编码端；不改变原Plugin SSE配额。"""
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone

from ..conversation.business_access import BusinessError, require_actor
from ..conversation.business_observability import ObservationRecord, _copy, _digest
from ..plugin_host.business_action_specs import (
    business_defs,
    business_schema_bundle,
    standalone_schema,
)
from ..plugin_host.schemas import validate_object

BUSINESS_STREAM_MAX_PENDING_EVENTS = 128


def _service(services, name, default=None):
    return services.get(name, default) if isinstance(services, Mapping) else getattr(services, name, default)


class BusinessEventStream:
    """拉式观察器：积压留在原来源，客户端内存有界；关闭只结束观察，不取消工作。"""
    content_type = 'application/x-ndjson'

    def __init__(self, request, authenticated_context, services):
        self.request = _copy(request)
        self.service = _service(services, 'observation')
        if self.service is None:
            raise BusinessError('HOST_NOT_READY')
        actor_factory = _service(services, 'observation_actor')
        self.actor = actor_factory(authenticated_context) if callable(actor_factory) else authenticated_context
        require_actor(self.actor, self.service)
        self.wait = _service(services, 'wait', time.sleep)
        self.clock = _service(services, 'clock', time.monotonic)
        self.event_signal = _service(services, 'event_signal')
        self.heartbeat_seconds = business_schema_bundle()['stream_transport_contract']['plugin']['heartbeat_seconds']
        # Plugin仍走原8事件泵/16全局/2安装；host指定transport，模型请求不能放大它。
        self.max_pending = 8 if _service(services, 'stream_transport') == 'plugin' else BUSINESS_STREAM_MAX_PENDING_EVENTS
        self._pending = deque()
        self._used = False
        self._cursor = request.get('cursor')
        self._last_sent = self._cursor
        self._authorize()

    @property
    def pending_count(self):
        return len(self._pending)

    def _authorize(self, event=None):
        params = {'filter': self.request['filter']}
        self.service.authorize_call('events.list', params, actor=self.actor)
        if self.request.get('binding_id') is not None and self.actor.authorize('events.list', {
                'owner_id': self.service.owner_id, 'binding_id': self.request['binding_id'], **self.request['filter']}) is not True:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        if event is not None and event['stream_item'] == 'event':
            row = ObservationRecord(event, self.service.owner_id, event.get('space_id'), event.get('project_ref'),
                event.get('task_id'), event['payload'].get('thread_id'), run_id=event.get('run_id'), attempt_id=event.get('attempt_id'))
            if not self.service._visible(row, 'events.list', params, self.actor):
                raise BusinessError('BUSINESS_ACCESS_DENIED')

    def _frame(self, kind, payload, *, coverage='complete', cursor=None):
        cursor = cursor or self._cursor or self.service.initial_event_cursor(self.request['filter'], actor=self.actor)
        value = {'stream_item': kind, 'event_id': _digest([kind, self.request['request_id'], cursor, payload]),
            'cursor': cursor, 'event_type': 'business.' + kind, 'occurred_at': datetime.now(timezone.utc).isoformat(),
            'node': None, 'space_id': self.request['filter'].get('space_id'),
            'project_ref': self.request['filter'].get('project_ref'), 'task_id': self.request['filter'].get('task_id'),
            'run_id': self.request['filter'].get('run_id'), 'attempt_id': self.request['filter'].get('attempt_id'),
            'operation_id': None, 'payload': payload, 'coverage': coverage}
        return validate_object(value, standalone_schema(business_defs()['EventView']))

    def _gap(self, kind, reason):
        return self._frame(kind, {'reason_code': reason, 'from_cursor': self._last_sent,
            'to_cursor': self._cursor, 'reset_required': True}, coverage='expired' if kind == 'cursor_expired' else 'partial')

    def _fill(self):
        coverage = 'unknown'
        while len(self._pending) < self.max_pending:
            self._authorize()
            # 积压留在来源，只读取空余容量；来源忽略limit时也只推进到实际入队的事件。
            limit = min(100, self.max_pending - len(self._pending))
            page = self.service.query('events.list', {'filter': self.request['filter'], 'limit': limit,
                **({'cursor': self._cursor} if self._cursor else {})}, actor=self.actor)
            previous = self._cursor
            coverage = page['coverage']
            admitted = page['events'][:limit]
            for event in admitted:
                self._pending.append(event)
            self._cursor = admitted[-1]['cursor'] if len(admitted) < len(page['events']) else page['next_cursor']
            if len(page['events']) < limit or previous == self._cursor:
                return None, coverage
        return None, coverage

    # LLM: 快照仍从同一来源读取，恢复时不绕过游标签名、代际与实时权限。
    # 函数用途: 在首次交付前复核续点并构建只读基线。
    def _snapshot(self):
        self._authorize()
        if self._cursor:
            self.service.cursor_position(self._cursor, self.service.scope_digest('events.list', {'filter': self.request['filter']}, self.actor))
            self.service.query('events.list', {'filter': self.request['filter'], 'cursor': self._cursor, 'limit': 1}, actor=self.actor)
        else:
            self._cursor = self.service.initial_event_cursor(self.request['filter'], actor=self.actor)
        snapshot = self.service.snapshot(self.request['filter'], actor=self.actor)
        self._authorize()
        self._last_sent = self._cursor
        return self._frame('snapshot', snapshot, coverage=snapshot['coverage'])

    def events(self):
        """W13 Plugin消费者使用此同一typed流，再交原SSE编码器，不复制事件库。"""
        if self._used:
            raise BusinessError('STREAM_ALREADY_CONSUMED')
        self._used = True
        initialized, expired_once = False, False
        idle_since = self.clock()
        delay = self.heartbeat_seconds / 60
        try:
            while True:
                try:
                    self._authorize()
                    if not initialized:
                        snapshot = self._snapshot()
                        initialized = True
                        yield snapshot
                    gap, coverage = self._fill()
                    if gap is not None:
                        self._authorize()
                        yield gap
                        continue
                    while self._pending:
                        event = self._pending.popleft()
                        self._authorize(event)
                        self._last_sent = event['cursor']
                        expired_once = False
                        idle_since, delay = self.clock(), self.heartbeat_seconds / 60
                        yield validate_object(event, standalone_schema(business_defs()['EventView']))
                    now = self.clock()
                    if now - idle_since >= self.heartbeat_seconds:
                        self._authorize()
                        expired_once = False
                        idle_since, delay = now, self.heartbeat_seconds / 60
                        yield self._frame('heartbeat', {'server_time': datetime.now(timezone.utc).isoformat()}, coverage=coverage)
                    timeout = min(delay, max(0, self.heartbeat_seconds - (self.clock() - idle_since)))
                    signal_wait = getattr(self.event_signal, 'wait', None)
                    (signal_wait if callable(signal_wait) else self.wait)(timeout)
                    delay = min(delay * 2, self.heartbeat_seconds)
                except BusinessError as error:
                    if error.code != 'CURSOR_EXPIRED':
                        raise
                    self._cursor = self.service.initial_event_cursor(self.request['filter'], actor=self.actor)
                    self._authorize()
                    yield self._gap('cursor_expired', 'CURSOR_EXPIRED')
                    # snapshot不算恢复进展；只有实际事件或完整空闲心跳才能解除连续过期保护。
                    if expired_once:
                        return
                    expired_once = True
        except BusinessError as error:
            if error.code not in {'BUSINESS_ACCESS_DENIED', 'SPACE_SCOPE_MISMATCH', 'ACTOR_REQUIRED'}:
                raise
        finally:
            self._pending.clear()

    def __iter__(self):
        for event in self.events():
            yield (json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')


def stream_business_events(request: dict, *, authenticated_context: object, services: object) -> object:
    request = validate_object(request, standalone_schema(business_schema_bundle()['stream_request_schema']))
    return BusinessEventStream(request, authenticated_context, services)
