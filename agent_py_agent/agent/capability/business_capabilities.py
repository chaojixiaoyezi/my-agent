"""F6只读业务能力选择；挂载仅排序，不扩权、不展开包成员、不预读正文。"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..conversation.business_contracts import BusinessExecutionContext
from .package_provider import list_installed_capability_packages
from .skill_learning_store import SkillLearningStore
from .skill_usage_ledger import MAX_USAGE_LEDGER_EVENTS, _usage_read_index

# 能力查询接受的最大字符数，已授权候选沿同一查询截取边界排序。
BUSINESS_CAPABILITY_QUERY_MAX_CHARS = 8000


@dataclass(frozen=True)
class BusinessCapabilityRequest:
    query: str = ''
    limit: int = 100
    candidates: tuple | None = None

    def __post_init__(self):
        if type(self.query) is not str:
            raise ValueError('CAPABILITY_QUERY_TYPE_INVALID: query 必须是字符串；查询尚未执行，请修正参数')
        if len(self.query) > BUSINESS_CAPABILITY_QUERY_MAX_CHARS:
            raise ValueError(f'CAPABILITY_QUERY_TOO_LONG: query 共 {len(self.query)} 字符，超过 '
                             f'{BUSINESS_CAPABILITY_QUERY_MAX_CHARS} 字符上限；查询尚未执行，请缩小查询')
        if type(self.limit) is not int:
            raise ValueError('CAPABILITY_LIMIT_TYPE_INVALID: limit 必须是整数；查询尚未执行，请修正参数')
        if self.limit < 0:
            raise ValueError('CAPABILITY_LIMIT_NEGATIVE: limit 不得为负数（0 表示不限制）；查询尚未执行，请修正参数')


@dataclass(frozen=True)
class BusinessCapabilityResolution:
    hits: tuple = ()
    candidates: tuple = ()
    origins: tuple = ()
    unavailable: tuple = ()
    policy_revision: int = 0

    def to_dict(self):
        return {'candidates': list(self.candidates), 'origins': list(self.origins),
                'unavailable': list(self.unavailable), 'policy_revision': self.policy_revision}


def resolve_business_capabilities(*, context: BusinessExecutionContext, request: object, host: object, spaces=None, actor=None) -> object:
    """只对原授权候选做mounted_first稳定排序；每次重读空间和可用性，绝不缓存执行许可。"""
    from .router import CapabilitySearchHit, score_card

    space = _space_for_context(host, context, spaces=spaces, actor=actor)
    if isinstance(request, dict):
        request = BusinessCapabilityRequest(query=request.get('query', ''))
    if not isinstance(request, BusinessCapabilityRequest):
        raise TypeError('需要宿主能力查询')
    if (not callable(getattr(host, 'current_skill_snapshot', None))
            or getattr(host, 'skills_service', None) is None
            or getattr(host, 'tools', None) is None
            or (request.candidates is None and getattr(host, 'capability_router', None) is None)):
        from ..plugin_host.errors import HostAPIError
        raise HostAPIError('HOST_NOT_READY', 503, message='当前入口未挂载能力快照解析端口')
    frozen = host.current_skill_snapshot()
    live = host.skills_service.snapshot_for(frozen.workspace_root)
    if frozen.owner_id != context.owner_id or live.owner_id != context.owner_id:
        raise PermissionError('Skill快照Owner不匹配')
    params = getattr(host, '_current_run_params', None)
    tools = getattr(params, 'tool_runtime_snapshot', None)
    if tools is None:
        tools = host.tools.runtime_snapshot(allowed_tools=getattr(params, 'allowed_tools', None),
                                            run_id=getattr(params, 'run_id', ''))
    live_tools = host.tools.runtime_snapshot(allowed_tools=list(tools.available_tool_names), run_id=tools.run_id)
    visible_now = {row.model_spec.name for row in live_tools.runtimes if row.exposure.model_visible}
    tool_names = {row.model_spec.name for row in tools.runtimes if row.exposure.model_visible
                  and (tools.allowed_tools is None or row.model_spec.name in tools.allowed_tools)} & visible_now
    skill_ids = {entry.stable_id for entry in frozen.enabled_entries()} & {entry.stable_id for entry in live.enabled_entries()}
    live_packages = {package.stable_id: package for package in live.packages}
    packages = {package.stable_id: package for package in frozen.packages
                if package.stable_id in live_packages
                and (package.package_sha256, package.activation_id) ==
                    (live_packages[package.stable_id].package_sha256, live_packages[package.stable_id].activation_id)}
    preferred, unavailable = {}, []
    for mount in space['mounts']:
        ref = mount['package_ref']
        package = packages.get(ref)
        availability = mount['availability']
        state, reason = 'available', ''
        if mount['enabled'] is not True:
            state, reason = 'disabled', 'mount_disabled'
        elif availability['state'] != 'available':
            state, reason = availability['state'], availability['reason_code']
        elif package is None:
            state, reason = 'missing', 'package_unavailable'
        elif mount['version_ref'] is not None and mount['version_ref'] != package.package_sha256:
            state, reason = 'missing', 'version_unavailable'
        if state == 'available':
            preferred[ref] = mount['position']
        else:
            unavailable.append(_availability(ref, state, reason))
    if request.candidates is None:
        hits = []
        for card in host.capability_router.cards():
            score, reasons = score_card(request.query, card)
            if score > 0:
                hits.append(CapabilitySearchHit(card, score, reasons[:4]))
        hits.sort(key=lambda hit: (-hit.score, hit.card.kind, hit.card.name))
    else:
        hits = list(request.candidates)
    authorized = []
    for hit in hits:
        card, ref = hit.card, _method_ref(hit.card)
        if ((card.kind == 'tool' and card.name in tool_names)
                or (card.kind == 'skill' and ref in skill_ids)
                or (card.kind == 'capability_package' and ref in packages)
                or (card.kind in {'script', 'workflow'} and card.source in {'owner', 'workspace'}
                    and all(name in tool_names for name in card.metadata.get('tools_required', ())))):
            authorized.append(hit)
    # 有效挂载先按相关性、同分按挂载位置；未挂载保留原顺序，不添加候选或改变授权。
    authorized.sort(key=lambda hit: (0, -hit.score, preferred[_method_ref(hit.card)])
                    if _method_ref(hit.card) in preferred else (1, 0, 0))
    if request.limit:
        authorized = authorized[:request.limit]
    candidates = []
    for hit in authorized[:100]:
        ref = _method_ref(hit.card)
        origin = {'kind': hit.card.source or 'snapshot',
                  'source_ref': context.space_id if ref in preferred else None,
                  'revision': space['revision'] if ref in preferred else None}
        candidates.append({'method_ref': ref, 'kind': {'capability_package': 'package', 'script': 'tool'}.get(hit.card.kind, hit.card.kind),
                           'score': hit.score, 'reason_codes': ['query_match', 'mounted_first' if ref in preferred else 'automatic_route'],
                           'origin': origin, 'availability': _availability(ref)})
    return BusinessCapabilityResolution(tuple(authorized), tuple(candidates), tuple(row['origin'] for row in candidates),
                                         tuple(unavailable[:100]), space['revision'])


def business_context_for_host(host):
    """只取宿主运行属性中的冻结DTO；文字不参与选择，开关关闭保持原路由字节。"""
    if host is None or getattr(getattr(host, 'config', None), 'business_spaces_enabled', False) is not True:
        return None
    current = getattr(host, '_current_run_params', None)
    attrs = getattr(current, 'task_attributes', None) or {}
    context = attrs.get('business_context')
    if context is None:
        context = getattr(current, 'business_context', None)
    if context is None:
        return None
    context = context if isinstance(context, BusinessExecutionContext) else BusinessExecutionContext.from_dict(context)
    # 未归属空间的业务任务没有挂载能力，沿原普通路由；Owner/空间不一致仍由 _space_for_context 拒绝。
    return context if context.space_id is not None else None


def _owner(host):
    if getattr(getattr(host, 'config', None), 'business_spaces_enabled', False) is not True:
        raise PermissionError('业务空间未启用')
    owner = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    if not owner:
        raise PermissionError('缺少宿主Owner')
    return owner


def _space_for_context(host, context, *, spaces=None, actor=None):
    if not isinstance(context, BusinessExecutionContext) or context.owner_id != _owner(host) or context.space_id is None:
        raise PermissionError('业务上下文Owner/空间不匹配')
    if spaces is None or actor is None:
        if hasattr(host, 'business_spaces') and hasattr(host, 'business_actor'):
            spaces, actor = host.business_spaces, host.business_actor
        else:
            from ..gateway_parts.business_api import local_business_context

            call_context, services = local_business_context(host, model=True)
            spaces, actor = services.spaces, services.actor(call_context)
    view = spaces.get(context.space_id, actor=actor)
    space = view['space']
    if space['space_id'] != context.space_id or space['state'] != 'active':
        raise PermissionError('业务空间不可用')
    if type(space['revision']) is not int or space['revision'] < 0 or view['mounts'] != space['mounts']:
        raise ValueError('空间投影身份不一致')
    return space


def _method_ref(card):
    return card.metadata.get('stable_id') or card.id


def _availability(ref, state='available', reason=''):
    return {'ref': ref, 'state': state, 'reason_code': reason, 'required_capabilities': []}


# 函数用途: 按工具操作的真实状态判定 usage 投影阶段，只有真的开始执行才算 executed。
def usage_phase_for_operation(status: object, handler_started_at: object) -> tuple[str, list[str]]:
    """CLAIMED 未启动、CANCELLED、UNKNOWN 都只是"选中过"，不能当作执行证据（V03-06）。"""
    op_status = str(status or '')
    try:
        started = float(handler_started_at or 0)
    except (TypeError, ValueError):
        started = 0.0
    if op_status in {'EXECUTING', 'SUCCEEDED', 'FAILED'} or (op_status == 'CLAIMED' and started > 0):
        return 'executed', ['runtime_execution_receipt']
    return 'selected', ['execution_unknown' if op_status == 'UNKNOWN' else 'not_executed']


class BusinessCapabilityService:
    """三个冻结只读动作的领域入口；W13负责原ActionSpec/grant，actor只能由宿主注入。

    W1提供business_spaces.get；W2提供task_business_context(runtime_task_id)准确反查。
    缺任务映射不猜space；既有使用账只覆盖部分阶段，不能假称完整执行轨迹。
    """
    def __init__(self, host):
        self.host = host

    def execute(self, action: str, request: dict, *, context: BusinessExecutionContext | None = None,
                spaces=None, actor=None) -> dict:
        owner = _owner(self.host)
        fields = {'spaces.packages.catalog': {'cursor', 'limit', 'query'},
                  'spaces.capabilities.resolve': {'space_id', 'project_ref', 'task_id', 'query'},
                  'spaces.usage.list': {'space_id', 'cursor', 'limit', 'task_id'}}
        if action not in fields or type(request) is not dict or set(request) - fields[action]:
            raise ValueError('未知业务动作或参数')
        if action == 'spaces.packages.catalog':
            query = request.get('query', '')
            if type(query) is not str or len(query) > 256:
                raise ValueError('无效包查询')
            items = []
            for record in list_installed_capability_packages(self.host.home_paths.owner_home_dir):
                if query.casefold() not in (record['name'] + ' ' + record.get('summary', '')).casefold():
                    continue
                ref = 'capability:' + record['package_id']
                available = _availability(ref, 'available' if record['enabled'] else 'disabled',
                                          '' if record['enabled'] else 'package_disabled')
                items.append({'package_ref': ref, 'name': record['name'][:120],
                              'description': record.get('summary', '')[:4000],
                              'version_ref': record.get('digest') or None, 'availability': available})
            items.sort(key=lambda item: item['package_ref'])
            page, cursor = _page(items, request, (owner, action, query))
            return {'items': page, 'next_cursor': cursor, 'availability': [item['availability'] for item in page]}
        if (not isinstance(context, BusinessExecutionContext) or request.get('space_id') != context.space_id
                or ('task_id' in request and request['task_id'] != context.task_id)
                or ('project_ref' in request and request['project_ref'] != context.project_ref)):
            raise PermissionError('业务对象与宿主上下文不一致')
        if action == 'spaces.capabilities.resolve':
            result = resolve_business_capabilities(context=context,
                request=BusinessCapabilityRequest(request.get('query', '')), host=self.host,
                spaces=spaces, actor=actor)
            from .method_usage import record_candidates
            matches = [{'kind': 'capability_package' if row['kind'] == 'package' else row['kind'],
                        'stable_id': row['method_ref']} for row in result.candidates]
            record_candidates(self.host, self.host.current_skill_snapshot(), matches,
                              source='spaces.capabilities.resolve')
            return result.to_dict()
        return _usage_page(self.host, owner, action, request, context, spaces=spaces, actor=actor)


def _usage_page(host, owner, action, request, context, *, spaces=None, actor=None):
    """原来源行有界推进；过滤空页也保留游标，不为一页结果全扫历史。"""
    import sqlite3

    from ..conversation.business_access import BusinessError
    from ..local_storage.owner_read_index import require_complete
    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path

    space = _space_for_context(host, context, spaces=spaces, actor=actor)
    loader = getattr(host, 'task_business_context', None)
    if not callable(loader):
        _page([], request, (owner, action, context.space_id, request.get('task_id')))
        return {'items': [], 'next_cursor': None, 'coverage': 'unknown'}
    limit = request.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA', details={'reason': 'USAGE_LIMIT_OUT_OF_RANGE'})
    try:
        skill_store = SkillLearningStore.for_home(host.home_paths)
        index = _usage_read_index(skill_store)
        parts = skill_store.directory.relative_to(skill_store.skills_root.parent).parts
        view = index.advance('usage', (*parts, 'usage_ledger.jsonl'), max_rows=MAX_USAGE_LEDGER_EVENTS)
        header = require_complete(index.query(view, limit=0))
        skill_count = header['total_count']
        if skill_count > MAX_USAGE_LEDGER_EVENTS:
            raise ValueError('skill usage source exceeds writer retention bound')
    except (OSError, ValueError, TypeError) as error:
        raise BusinessError('STORE_UNAVAILABLE', details={
            'reason': 'SKILL_USAGE_SOURCE_UNREADABLE', 'exception_type': type(error).__name__,
            'source_coverage': getattr(error, 'coverage', {}),
            'next_action': '检查原技能使用账本后从首页重新读取。'}) from error
    path = runtime_db_path(Path(host.home_paths.owner_home_dir).resolve())

    def expired(reason='USAGE_CURSOR_VERSION_OR_SOURCE_CHANGED'):
        return BusinessError('CURSOR_EXPIRED', details={'reason': reason,
            'next_action': '移除旧 cursor，从首页重新读取当前保留范围的使用记录。'})

    def skill_boundary(count):
        if not count:
            return {'count': 0, 'first': '', 'last': ''}
        result = []
        for offset in (0, count - 1):
            rows = require_complete(index.query(view, limit=1, offset=offset))['rows']
            if len(rows) != 1:
                raise expired('SKILL_USAGE_FROZEN_RANGE_MISSING')
            result.append(hashlib.sha256(json.dumps(rows[0], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        return {'count': count, 'first': result[0], 'last': result[1]}

    def runtime_boundary(expected=None):
        if not path.exists():
            if expected is not None:
                raise expired('RUNTIME_USAGE_FROZEN_RANGE_MISSING')
            return None
        info = path.stat()
        physical = [info.st_dev, info.st_ino]
        repo = RuntimeRepository(path, read_only=True)
        with repo._runtime_connection() as conn:
            if expected is None:
                first = conn.execute('SELECT rowid,operation_id FROM tool_operations ORDER BY rowid LIMIT 1').fetchone()
                last = conn.execute('SELECT rowid,operation_id FROM tool_operations ORDER BY rowid DESC LIMIT 1').fetchone()
                return {'physical': physical, 'first': list(first) if first else None,
                        'last': list(last) if last else None}
            if physical != expected['physical']:
                raise expired('RUNTIME_USAGE_SOURCE_REPLACED')
            for anchor in (expected['first'], expected['last']):
                if anchor is not None:
                    row = conn.execute('SELECT operation_id FROM tool_operations WHERE rowid=?', (anchor[0],)).fetchone()
                    if row is None or row[0] != anchor[1]:
                        raise expired('RUNTIME_USAGE_FROZEN_RANGE_MISSING')
        return expected

    # 游标冻结来源截止而非整个WAL版本；查询自己的写账和后来追加不会扩入旧读窗。
    # 每页重新鉴权并读取行的当前状态，不把该有界读窗宣称为所有历史状态的不可变快照。
    scope = [owner, action, context.space_id, context.project_ref, context.binding_id,
             request.get('task_id'), context.task_id]
    position = {'skill_offset': 0, 'runtime_after': 0, 'runtime_after_id': ''}
    cursor = request.get('cursor')
    try:
        if cursor is None:
            cutoff = {'skill': skill_boundary(skill_count), 'runtime': runtime_boundary()}
        else:
            if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
                raise ValueError('invalid cursor')
            decoded = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
            if (type(decoded) is not dict or set(decoded) != {'version', 'identity', 'cutoff', 'position'}
                    or decoded['version'] != 3):
                raise ValueError('expired cursor')
            cutoff, position = decoded['cutoff'], decoded['position']
            if type(cutoff) is not dict or set(cutoff) != {'skill', 'runtime'}:
                raise ValueError('invalid cutoff')
            skill = cutoff['skill']
            if (type(skill) is not dict or set(skill) != {'count', 'first', 'last'}
                    or type(skill['count']) is not int or not 0 <= skill['count'] <= MAX_USAGE_LEDGER_EVENTS
                    or any(type(skill[k]) is not str or len(skill[k]) > 64 for k in ('first', 'last'))
                    or skill['count'] > skill_count or skill_boundary(skill['count']) != skill):
                raise expired('SKILL_USAGE_FROZEN_RANGE_MISSING')
            runtime = cutoff['runtime']
            if runtime is not None:
                if (type(runtime) is not dict or set(runtime) != {'physical', 'first', 'last'}
                        or type(runtime['physical']) is not list or len(runtime['physical']) != 2
                        or any(type(v) is not int or v < 0 for v in runtime['physical'])):
                    raise ValueError('invalid runtime cutoff')
                for anchor in (runtime['first'], runtime['last']):
                    if anchor is not None and (type(anchor) is not list or len(anchor) != 2
                            or type(anchor[0]) is not int or not 0 < anchor[0] <= 2**63 - 1
                            or type(anchor[1]) is not str or not 1 <= len(anchor[1]) <= 128):
                        raise ValueError('invalid runtime anchor')
                if (runtime['first'] is None) != (runtime['last'] is None):
                    raise ValueError('invalid runtime range')
                if runtime['first'] and runtime['first'][0] > runtime['last'][0]:
                    raise ValueError('invalid runtime range')
                runtime_boundary(runtime)
            identity = hashlib.sha256(json.dumps([scope, cutoff], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if decoded['identity'] != identity:
                raise ValueError('scope changed')
            if (type(position) is not dict or set(position) != {'skill_offset', 'runtime_after', 'runtime_after_id'}
                    or any(type(position[k]) is not int or position[k] < 0 for k in ('skill_offset', 'runtime_after'))
                    or type(position['runtime_after_id']) is not str or len(position['runtime_after_id']) > 128
                    or bool(position['runtime_after']) != bool(position['runtime_after_id'])
                    or position['skill_offset'] > skill['count']
                    or position['runtime_after'] > (runtime['last'][0] if runtime and runtime['last'] else 0)):
                raise ValueError('invalid position')
    except BusinessError:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise expired() from error
    except (OSError, RuntimeError, sqlite3.Error) as error:
        raise BusinessError('STORE_UNAVAILABLE', details={'reason': 'RUNTIME_USAGE_SOURCE_UNREADABLE',
            'exception_type': type(error).__name__, 'next_action': '检查原runtime使用来源后从首页重读。'}) from error
    identity = hashlib.sha256(json.dumps([scope, cutoff], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    skill_count = cutoff['skill']['count']
    runtime = cutoff['runtime']
    items, contexts = [], {}
    def business_for(task_id):
        if task_id not in contexts:
            try:
                contexts[task_id] = loader(task_id)
            except (OSError, ValueError, RuntimeError) as error:
                raise BusinessError('STORE_UNAVAILABLE', details={
                    'reason': 'USAGE_TASK_CONTEXT_UNREADABLE', 'exception_type': type(error).__name__,
                    'next_action': '检查原任务归属记录后重新读取。'}) from error
        business = contexts[task_id]
        if (not isinstance(business, BusinessExecutionContext) or business.owner_id != owner
                or business.space_id != context.space_id
                or request.get('task_id') is not None and business.task_id != request['task_id']):
            return None
        return business
    offset = position['skill_offset']
    try:
        selected = (require_complete(index.query(view, limit=min(limit, skill_count - offset), offset=offset))['rows']
                    if offset < skill_count else [])
    except (OSError, ValueError) as error:
        raise BusinessError('STORE_UNAVAILABLE', details={'reason': 'SKILL_USAGE_PAGE_UNREADABLE',
            'source_coverage': getattr(error, 'coverage', {}),
            'next_action': '检查原技能使用来源后从首页重新读取。'}) from error
    phases = {'manual_get': 'read', 'adopted': 'selected', 'host_injected': 'visible', 'manual_search': 'considered',
              'visible': 'visible', 'selected': 'selected', 'executed': 'executed'}
    for row in selected:
        if not row.get('task_id') or type(row.get('sequence')) is not int:
            continue
        business = business_for(row['task_id'])
        if business is None:
            continue
        reasons = ['original_skill_usage_ledger'] + (['mounted_first'] if row.get('mounted_first') else [])
        if row.get('source_key') and row.get('producer'):
            reasons.append(row['producer'])
        items.append({'event_id': f"skill-usage:{row['sequence']}", 'space_id': business.space_id,
            'task_id': business.task_id, 'run_id': None, 'method_ref': row['skill_id'],
            'phase': phases.get(row.get('event'), 'read'), 'reason_codes': reasons, 'receipt_ref': row.get('receipt_ref') or None})
    position['skill_offset'] += len(selected)
    more = position['skill_offset'] < skill_count
    if not selected and not more and runtime is not None and runtime['last'] is not None:
        # 与原 tool_calls reader 同一 rowid/revision 来源页；先取位置再做有界授权 join。
        after = position['runtime_after']
        try:
            repo = RuntimeRepository(path, read_only=True)
            with repo._runtime_connection() as conn:
                conn.execute('BEGIN')
                if after:
                    anchor = conn.execute('SELECT operation_id FROM tool_operations WHERE rowid=?', (after,)).fetchone()
                    if anchor is None or anchor[0] != position['runtime_after_id']:
                        raise expired('RUNTIME_USAGE_FROZEN_RANGE_MISSING')
                positions = conn.execute('SELECT rowid,operation_id FROM tool_operations WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',
                    (after, runtime['last'][0], limit + 1)).fetchall()
                ids = [row[0] for row in positions[:limit]]
                operations = conn.execute(
                    'SELECT x.operation_id,x.operation_type,x.status,x.handler_started_at,ar.run_id,t.task_id, '
                    't.conversation_task_id,tr.metadata_json '
                    'FROM tool_operations x JOIN agent_runs ar ON ar.agent_run_id=x.agent_run_id '
                    'JOIN agent_attempts aa ON aa.attempt_id=x.attempt_id AND aa.agent_run_id=ar.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'WHERE t.owner_id=? AND x.rowid IN (' + ','.join('?' for _ in ids) + ') ORDER BY x.rowid',
                    [owner, *ids]).fetchall() if ids else []
            for op in operations:
                # 原usage只列当前准确任务的runtime操作；空间页不会扩大为同owner所有运行。
                metadata = json.loads(op['metadata_json'])
                raw_context = metadata.get('business_context')
                if raw_context:
                    frozen = BusinessExecutionContext.from_dict(raw_context)
                    if frozen.owner_id != owner or frozen.task_id != metadata.get('session_task_ref'):
                        raise ValueError('runtime usage context mismatch')
                    if frozen.space_id != context.space_id:
                        continue
                    source_task = frozen.task_id
                else:
                    source_task = op['conversation_task_id'] or op['task_id']
                if source_task != (request.get('task_id') or context.task_id):
                    continue
                business = business_for(source_task)
                if business is None:
                    continue
                phase, reasons = usage_phase_for_operation(op['status'], op['handler_started_at'])
                if phase == 'executed' and any(m.get('package_ref') in op['operation_type']
                        for m in space['mounts'] if isinstance(m, dict) and m.get('package_ref')):
                    reasons.append('mounted_first')
                items.append({'event_id': ('runtime-exec:' if phase == 'executed' else 'runtime-op:') + op['operation_id'],
                    'space_id': business.space_id, 'task_id': business.task_id, 'run_id': op['run_id'] or None,
                    'method_ref': op['operation_type'], 'phase': phase, 'reason_codes': reasons,
                    'receipt_ref': 'receipt:' + op['operation_id'] if phase == 'executed' else None})
            runtime_boundary(runtime)
            if ids:
                position['runtime_after'] = ids[-1]
                position['runtime_after_id'] = positions[len(ids) - 1][1]
            more = len(positions) > limit
        except BusinessError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
            raise BusinessError('STORE_UNAVAILABLE', details={
                'reason': 'RUNTIME_USAGE_SOURCE_UNREADABLE', 'exception_type': type(error).__name__,
                'next_action': '检查原 runtime 工具账本；修复后从首页重新读取。'}) from error
    elif selected and not more:
        more = runtime is not None and runtime['last'] is not None
    next_cursor = (base64.urlsafe_b64encode(json.dumps({'version': 3, 'identity': identity,
        'cutoff': cutoff, 'position': position}).encode()).decode().rstrip('=') if more else None)
    return {'items': items, 'next_cursor': next_cursor, 'coverage': 'partial' if skill_count or items or more else 'unknown'}


def _page(rows, request, scope):
    """游标只绑定只读投影版本/过滤/Owner，不是权限令牌；改变来源或范围必须重新分页。"""
    from ..conversation.business_access import BusinessError

    limit, cursor = request.get('limit', 50), request.get('cursor')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA', details={'field': 'limit', 'reason': 'PAGE_LIMIT_OUT_OF_RANGE',
            'next_action': 'limit须为1至100的整数；保留上一页查询和过滤条件后重新读取。'})
    identity = hashlib.sha256(json.dumps([scope, rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    offset = 0
    if cursor is not None:
        def expired(reason, field='cursor'):
            return BusinessError('CURSOR_EXPIRED', details={'field': field, 'reason': reason,
                'pagination_recovery': '下一页须保留上一页的query及过滤参数，limit须为1至100；'
                    '可去掉cursor并按原查询从首页重新读取。游标不是授权，不能跨Owner或来源复用。'})
        if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
            raise expired('CURSOR_FORMAT_INVALID')
        try:
            payload = json.loads(base64.b64decode(cursor + '='*(-len(cursor) % 4), altchars=b'-_', validate=True))
        except (ValueError, RecursionError):
            raise expired('CURSOR_FORMAT_INVALID') from None
        if (type(payload) is not dict or set(payload) != {'version', 'identity', 'offset'}
                or type(payload['version']) is not int or payload['version'] != 1):
            raise expired('CURSOR_PAYLOAD_INVALID')
        if payload['identity'] != identity:
            raise expired('CURSOR_SCOPE_OR_SOURCE_CHANGED')
        if type(payload['offset']) is not int or not 0 <= payload['offset'] <= len(rows):
            raise expired('CURSOR_OFFSET_INVALID', 'cursor.offset')
        offset = payload['offset']
    page = rows[offset:offset+limit]
    end = offset + len(page)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({'version': 1, 'identity': identity, 'offset': end}).encode()).decode().rstrip('=')
                   if end < len(rows) else None)
    return page, next_cursor
