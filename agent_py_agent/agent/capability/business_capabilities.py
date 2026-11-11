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
from .skill_usage_ledger import _ledger_rows


@dataclass(frozen=True)
class BusinessCapabilityRequest:
    query: str = ''
    limit: int = 100
    candidates: tuple | None = None

    def __post_init__(self):
        if type(self.query) is not str or len(self.query) > 8000 or type(self.limit) is not int or self.limit < 0:
            raise ValueError('无效能力查询')


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


def resolve_business_capabilities(*, context: BusinessExecutionContext, request: object, host: object) -> object:
    """只对原授权候选做mounted_first稳定排序；每次重读空间和可用性，绝不缓存执行许可。"""
    from .router import CapabilitySearchHit, score_card

    space = _space_for_context(host, context)
    if isinstance(request, dict):
        request = BusinessCapabilityRequest(query=request.get('query', ''))
    if not isinstance(request, BusinessCapabilityRequest):
        raise TypeError('需要宿主能力查询')
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
    # 原分数和同分顺序不变；只把同一授权集合里匹配的有效挂载移前，不添加候选。
    authorized.sort(key=lambda hit: (0, preferred[_method_ref(hit.card)]) if _method_ref(hit.card) in preferred else (1, 0))
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


def _space_for_context(host, context):
    if not isinstance(context, BusinessExecutionContext) or context.owner_id != _owner(host) or context.space_id is None:
        raise PermissionError('业务上下文Owner/空间不匹配')
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


class BusinessCapabilityService:
    """三个冻结只读动作的领域入口；W13负责原ActionSpec/grant，actor只能由宿主注入。

    W1提供business_spaces.get；W2提供task_business_context(runtime_task_id)准确反查。
    缺任务映射不猜space；既有使用账只覆盖部分阶段，不能假称完整执行轨迹。
    """
    def __init__(self, host):
        self.host = host

    def execute(self, action: str, request: dict, *, context: BusinessExecutionContext | None = None) -> dict:
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
            return resolve_business_capabilities(context=context, request=BusinessCapabilityRequest(request.get('query', '')),
                                                   host=self.host).to_dict()
        _space_for_context(self.host, context)
        loader = getattr(self.host, 'task_business_context', None)
        if not callable(loader):
            _page([], request, (owner, action, context.space_id, request.get('task_id')))
            return {'items': [], 'next_cursor': None, 'coverage': 'unknown'}
        rows = _ledger_rows(SkillLearningStore.for_home(self.host.home_paths))
        items = []
        phases = {'manual_get': 'read', 'adopted': 'selected', 'host_injected': 'visible', 'manual_search': 'considered', 'executed': 'executed'}
        for row in rows:
            if not row.get('task_id') or type(row.get('sequence')) is not int:
                continue
            business = loader(row['task_id'])
            if not isinstance(business, BusinessExecutionContext) or business.owner_id != owner or business.space_id != context.space_id:
                continue
            if request.get('task_id') is not None and business.task_id != request['task_id']:
                continue
            reason_codes = ['original_skill_usage_ledger']
            if row.get('mounted_first'):
                reason_codes.append('mounted_first')
            items.append({'event_id': f"skill-usage:{row['sequence']}", 'space_id': business.space_id,
                          'task_id': business.task_id, 'run_id': None, 'method_ref': row['skill_id'],
                          'phase': phases.get(row.get('event'), 'read'), 'reason_codes': reason_codes, 'receipt_ref': None})

        # LLM: 从 runtime_db 读取真实工具执行记录并投影为 executed 阶段收据 (V03-06)。
        # 函数用途: 补充执行阶段收据并保留 mounted_first 出处 (V03-04)。
        try:
            from ..runtime_db.repository import RuntimeRepository
            from ..runtime_db.schema import runtime_db_path
            r_path = runtime_db_path(Path(self.host.home_paths.owner_home_dir).resolve())
            if r_path.exists():
                repo = RuntimeRepository(r_path, read_only=True)
                target_task_id = request.get('task_id') or context.task_id
                if target_task_id:
                    task_runs = repo.task_runs_for_task(str(target_task_id))
                    for tr in task_runs:
                        tr_id = tr['task_run_id'] if isinstance(tr, dict) or hasattr(tr, '__getitem__') else getattr(tr, 'task_run_id', None)
                        if not tr_id:
                            continue
                        agent_runs = repo.agent_runs_for_task_run(tr_id)
                        for ar in agent_runs:
                            ar_id = ar['agent_run_id'] if isinstance(ar, dict) or hasattr(ar, '__getitem__') else getattr(ar, 'agent_run_id', None)
                            if not ar_id:
                                continue
                            attempts = repo.attempts_for_run(ar_id)
                            for att in attempts:
                                att_id = att['attempt_id'] if isinstance(att, dict) or hasattr(att, '__getitem__') else getattr(att, 'attempt_id', None)
                                if not att_id:
                                    continue
                                ops = repo.operations_for_attempt(att_id)
                                for op in ops:
                                    op_id = op['operation_id'] if isinstance(op, dict) or hasattr(op, '__getitem__') else getattr(op, 'operation_id', None)
                                    op_type = op['operation_type'] if isinstance(op, dict) or hasattr(op, '__getitem__') else getattr(op, 'operation_type', None)
                                    if not op_id or not op_type:
                                        continue
                                    r_codes = ['runtime_execution_receipt']
                                    try:
                                        space_obj = _space_for_context(self.host, context)
                                        mounts = space_obj.get('mounts', []) if isinstance(space_obj, dict) else []
                                    except Exception:
                                        mounts = []
                                    if any(m.get('package_ref') in op_type for m in mounts if isinstance(m, dict)):
                                        r_codes.append('mounted_first')
                                    items.append({
                                        'event_id': f"runtime-exec:{op_id}",
                                        'space_id': context.space_id,
                                        'task_id': target_task_id,
                                        'run_id': ar_id,
                                        'method_ref': op_type,
                                        'phase': 'executed',
                                        'reason_codes': r_codes,
                                        'receipt_ref': f"receipt:{op_id}"
                                    })
        except Exception:
            pass
        page, cursor = _page(items, request, (owner, action, context.space_id, request.get('task_id')))
        return {'items': page, 'next_cursor': cursor, 'coverage': 'partial' if (rows or items) else 'unknown'}


def _page(rows, request, scope):
    """游标只绑定只读投影版本/过滤/Owner，不是权限令牌；改变来源或范围必须重新分页。"""
    limit, cursor = request.get('limit', 50), request.get('cursor')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('分页大小必须为1至100')
    identity = hashlib.sha256(json.dumps([scope, rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    offset = 0
    if cursor is not None:
        if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
            raise ValueError('无效业务游标')
        payload = json.loads(base64.b64decode(cursor + '='*(-len(cursor) % 4), altchars=b'-_', validate=True))
        if (type(payload) is not dict or set(payload) != {'version', 'identity', 'offset'}
                or payload['version'] != 1 or payload['identity'] != identity
                or type(payload['offset']) is not int or not 0 <= payload['offset'] <= len(rows)):
            raise ValueError('业务游标已过期或范围不匹配')
        offset = payload['offset']
    page = rows[offset:offset+limit]
    end = offset + len(page)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({'version': 1, 'identity': identity, 'offset': end}).encode()).decode().rstrip('=')
                   if end < len(rows) else None)
    return page, next_cursor
