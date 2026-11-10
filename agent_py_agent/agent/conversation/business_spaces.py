"""Owner业务元数据的唯一权威：空间、界面绑定、资产引用与同笔幂等回执。

只复用原quota→目录锁→no-follow私有原子写。任务、Workflow政策、媒体正文和安装状态
仍归原服务；本目录不创建Agent、任务或额外授权。读取不建目录、不取得写锁。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unicodedata
import uuid
from pathlib import Path

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..plugin_host.business_action_specs import (
    BUSINESS_LIST_DEFAULT_ROWS,
    BUSINESS_LIST_MAX_ROWS,
    business_action_spec,
    standalone_schema,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import validate_object
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .business_access import (
    BusinessError,
    governance_call,
    require_access,
    require_actor,
    require_revision,
    validate_params,
    validate_result,
)
from .business_receipt_rows import ReceiptRows


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _name(value):
    text = unicodedata.normalize('NFKC', value).strip()
    if not text:
        raise BusinessError('INVALID_SCHEMA')
    return text


def _identifier(value):
    if type(value) is not str or not value or len(value.encode('utf-8')) > 128:
        raise BusinessError('INVALID_SCHEMA')
    return value


class BusinessSpaceService:
    """宿主按准确Owner根实例化；外部引用和活跃工作查询必须注入原服务的实时端口。

    enabled来自原AgentConfig.business_spaces_enabled，不接受模型参数开启。
    reference_resolver(kind, ref, actor)只返回已核Owner/范围的原对象元数据，不返回正文。
    """
    def __init__(self, owner_root, *, owner_id, reference_resolver=None, active_references=None,
                 governance=None, enabled=False):
        if type(owner_id) is not str or not owner_id or type(enabled) is not bool:
            raise BusinessError('ACTOR_REQUIRED')
        self.owner_root = Path(os.path.abspath(owner_root))
        self.owner_id, self.enabled = owner_id, enabled
        self.reference_resolver, self.active_references = reference_resolver, active_references
        self.governance = governance
        self.state_parts = ('data', 'business', 'catalog.json')
        self.state_path = self.owner_root.joinpath(*self.state_parts)
        # 只保护只读分页，不是授权/持久状态；服务重建后旧游标明确过期。
        self._cursor_key = os.urandom(32)

    # LLM: 同一 journal 前后采样，目录、revision 与回执提交保持单笔逻辑视图；不执行恢复写。
    # 函数用途: 读取有界目录快照或已提交日志的覆盖状态。
    def _read(self):
        rows = ReceiptRows(self)
        for _attempt in range(3):
            before = rows.pending()
            state = self._read_snapshot(allow_missing=True)
            after = rows.pending()
            if before == after:
                if after is not None:
                    return after['state']
                if state.pop('_missing_snapshot', False) and rows.has_history():
                    raise BusinessError('STORE_UNAVAILABLE')
                return state
        raise BusinessError('HOST_NOT_READY')

    # LLM: 缺失快照但有永久行时必须拒绝；采样读者仅由同笔有效journal覆盖这种缺失。
    # 函数用途: 读取有界目录，区分首次初始化与canonical损坏。
    def _read_snapshot(self, *, allow_missing=False):
        raw = read_bytes_beneath(self.owner_root, self.state_parts)
        if raw is None:
            if not allow_missing and ReceiptRows(self).has_history():
                raise BusinessError('STORE_UNAVAILABLE')
            return {'schema_version': 'business-catalog.v1', 'owner_id': self.owner_id, 'revision': 0,
                    'spaces': {}, 'bindings': {}, 'assets': {}, 'receipts': {},
                    **({'_missing_snapshot': True} if allow_missing else {})}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error
        return self._validate_catalog(value)

    # LLM: 原实体约束保持不变；同一校验也用于 journal 覆盖，坏源不能变成空成功。
    # 函数用途: 校验目录 owner、修订及所有有界业务实体。
    def _validate_catalog(self, value):
        if (type(value) is not dict or value.get('schema_version') != 'business-catalog.v1'
                or value.get('owner_id') != self.owner_id or type(value.get('revision')) is not int
                or value['revision'] < 0 or any(type(value.get(k)) is not dict for k in ('spaces', 'bindings', 'assets', 'receipts'))):
            raise BusinessError('STORE_UNAVAILABLE')
        for collection, identity in (('spaces', 'space_id'), ('bindings', 'binding_id'), ('assets', 'asset_id')):
            if any(type(row) is not dict or row.get('owner_id') != self.owner_id or row.get(identity) != key
                   or type(row.get('revision')) is not int or row['revision'] < 0
                   for key, row in value[collection].items()):
                raise BusinessError('STORE_UNAVAILABLE')
        self._validate_records(value)
        return value

    def _validate_records(self, value):
        """坏业务记录不能变成KeyError、空成功或静默清库；只复用冻结DTO做纯形状校验。"""
        routes = (
            ('spaces', 'spaces.list', 'items', ('owner_id',)),
            ('bindings', 'interfaces.get', 'binding', ('owner_id', 'state')),
            ('assets', 'assets.get', 'asset', ('owner_id', 'state', 'versions')),
        )
        try:
            for collection, action, field_name, private in routes:
                if not value[collection]:
                    continue
                shape = business_action_spec(action).result_schema['properties'][field_name]
                schema = standalone_schema(shape['items'] if field_name == 'items' else shape)
                for row in value[collection].values():
                    validate_object({k: v for k, v in row.items() if k not in private}, schema)
                    if collection == 'bindings':
                        if row['state'] not in ('bound', 'unbound') or any(sid not in value['spaces'] for sid in row['space_ids']):
                            raise ValueError('binding structure')
                    if collection == 'assets':
                        if (row['state'] not in ('linked', 'unlinked') or row['space_id'] not in value['spaces']
                                or type(row['versions']) is not list or not row['versions']):
                            raise ValueError('asset structure')
                        version_shape = business_action_spec('assets.versions.list').result_schema['properties']['items']['items']
                        version_schema = standalone_schema(version_shape)
                        for version in row['versions']:
                            validate_object(version, version_schema)
                            if version['artifact_ref']['version_ref'] != version['version_ref']:
                                raise ValueError('version structure')
                        refs = [version['version_ref'] for version in row['versions']]
                        accepted = [version['version_ref'] for version in row['versions'] if version['state'] == 'accepted']
                        if len(refs) != len(set(refs)) or accepted != ([] if row['accepted_version'] is None else [row['accepted_version']]):
                            raise ValueError('acceptance structure')
            for key, receipt in value['receipts'].items():
                if (type(key) is not str or len(key) != 64 or type(receipt) is not dict
                        or set(receipt) != {'fingerprint', 'result'} or type(receipt['result']) is not dict
                        or type(receipt['fingerprint']) is not str or len(receipt['fingerprint']) != 64):
                    raise ValueError('receipt structure')
        except (KeyError, TypeError, ValueError, HostAPIError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    def _space(self, state, sid, *, active=False):
        row = state['spaces'].get(sid)
        if row is None:
            raise BusinessError('SPACE_NOT_FOUND')
        if active and row['state'] != 'active':
            raise BusinessError('SPACE_ARCHIVED')
        return row

    def authorize_call(self, action, params, *, actor, state=None):
        require_actor(actor, self)
        if not self.enabled:
            raise BusinessError('BUSINESS_NOT_ENABLED')
        scope = {key: params[key] for key in ('space_id', 'project_ref', 'binding_id', 'asset_id', 'approval_id') if key in params}
        require_access(actor, action, service=self, **scope)
        state = self._read() if state is None else state
        sid = params.get('space_id')
        if sid is not None:
            row = self._space(state, sid)
            require_access(actor, action, service=self, record=row, **scope)
        for kind, key, code in (('bindings', 'binding_id', 'BINDING_NOT_FOUND'), ('assets', 'asset_id', 'ASSET_NOT_FOUND')):
            if key not in params:
                continue
            row = state[kind].get(params[key])
            if row is None or row.get('state') == 'unbound' and action != 'interfaces.unbind':
                raise BusinessError(code)
            if kind == 'assets' and row.get('state') == 'unlinked' and action != 'assets.unlink':
                raise BusinessError(code)
            require_access(actor, action, service=self, record=row, **scope)
            if kind == 'bindings':
                for selected in row['space_ids']:
                    self._space(state, selected)
                    require_access(actor, action, service=self, space_id=selected, binding_id=row['binding_id'])
        return state

    def reference(self, kind, ref, *, actor, space_id=None, project_ref=None):
        require_actor(actor, self)
        _identifier(ref)
        if not callable(self.reference_resolver):
            raise BusinessError('HOST_NOT_READY')
        row = self.reference_resolver(kind, ref, actor)
        if row is None:
            raise BusinessError('REFERENCE_NOT_FOUND')
        if type(row) is not dict or row.get('owner_id') != self.owner_id or row.get('ref') != ref:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        if (space_id is not None and row.get('space_id') not in (None, space_id)
                or project_ref is not None and row.get('project_ref') not in (None, project_ref)):
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        if row.get('state') not in ('available', 'disabled', 'not_connected'):
            raise BusinessError('REFERENCE_NOT_FOUND')
        # 仅取定位字段，绝不复制path、Base64或插件任意metadata到业务账。
        return {key: row[key] for key in ('owner_id', 'space_id', 'project_ref', 'state', 'ref',
            'version_ref', 'artifact_id', 'content_sha256', 'package_ref') if key in row}

    # LLM: 原末核、CAS 和指纹不变；小快照与逐键回执沿同一日志提交，重放不解析旧回执历史。
    # 函数用途: 在原 quota→目录锁内执行目录变更或返回准确的永久幂等结果。
    def _transaction(self, action, params, *, actor, expected_revision=None, idempotency_key=None, mutate, commit_guard=None):
        spec = business_action_spec(action)
        params = validate_params(action, params)
        if 'name' in params:
            params['name'] = _name(params['name'])
        _identifier(idempotency_key)
        if spec.expected_revision_required and (type(expected_revision) is not int or expected_revision < 0):
            raise BusinessError('INVALID_SCHEMA')
        require_actor(actor, self)
        self.authorize_call(action, params, actor=actor)
        fingerprint = hashlib.sha256(json.dumps([action, params, expected_revision], ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        key = hashlib.sha256(json.dumps([actor.subject_id, idempotency_key], ensure_ascii=False).encode()).hexdigest()
        gate = owner_quota_enforcer_from_policy(self.owner_root)
        rows = ReceiptRows(self)
        with gate.admission() as admission:
            with locked_private_directory(self.owner_root, relative_parts=('data', 'business'), lock_name='.catalog.lock'):
                rows.recover(admission)
                state = self.authorize_call(action, params, actor=actor)
                state = rows.migrate(state, admission)
                self.authorize_call(action, params, actor=actor, state=state)
                receipt = rows.get(key)
                if receipt is not None:
                    if receipt.get('fingerprint') != fingerprint:
                        raise BusinessError('IDEMPOTENCY_CONFLICT')
                    result = validate_result(action, _copy(receipt['result']))
                    self.authorize_call(action, params, actor=actor, state=state)
                    return result
                result, changed = mutate(state, params)
                result = validate_result(action, result)
                if not changed:
                    return result
                self.authorize_call(action, params, actor=actor, state=state)
                state['revision'] += 1
                receipt = {'fingerprint': fingerprint, 'result': _copy(result)}
                state.update(receipts={}, receipt_rows=True)
                pending = {'schema': 'business-catalog-transaction.v1', 'owner_id': self.owner_id,
                           'state': state, 'key': key, 'receipt': receipt}
                raw = (json.dumps(pending, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
                payloads = rows.materialized(pending)
                admission.check([OwnerQuotaChange(self.owner_root.joinpath(*path), final_size=len(data))
                                 for path, data in [*payloads, (rows.journal, raw)]])
                self.authorize_call(action, params, actor=actor, state=state)
                if commit_guard is not None:
                    commit_guard()
                write_bytes_atomic_beneath(self.owner_root, rows.journal, raw)
                rows.write(payloads, admission)
                write_bytes_atomic_beneath(self.owner_root, rows.journal, b'{}\n')
                # fsync/配额结算失败可发生在替换后；不伪称未提交，同键重放只读同笔回执。
                return _copy(result)

    def _mounts(self, values, *, actor, space_id=None):
        if len({value['package_ref'] for value in values}) != len(values):
            raise BusinessError('INVALID_SCHEMA')
        result = []
        for position, value in enumerate(values):
            ref = value['package_ref']
            row = self.reference('package', ref, actor=actor, space_id=space_id)
            result.append({'package_ref': ref, 'enabled': value.get('enabled', True), 'position': position,
                'version_ref': row.get('version_ref'), 'availability': {'ref': ref, 'state': row['state'],
                    'reason_code': '', 'required_capabilities': []}})
        return result

    def space_view(self, row, *, actor):
        result = {key: _copy(row[key]) for key in ('space_id', 'name', 'purpose', 'revision', 'state', 'mounts', 'default_project_ref')}
        # 可用性是当前原服务投影，不把当初挂载偏好当成安装/连接/授权事实。
        for mount in result['mounts']:
            try:
                current = self.reference('package', mount['package_ref'], actor=actor, space_id=row['space_id'])
                mount['availability']['state'] = current['state']
                mount['version_ref'] = current.get('version_ref')
            except BusinessError as error:
                mount['availability'].update(state='denied' if error.code == 'SPACE_SCOPE_MISMATCH' else 'missing', reason_code=error.code)
        return result

    def _unique_name(self, state, name, *, excluding=None):
        if any(row['space_id'] != excluding and row['state'] == 'active' and _name(row['name']).casefold() == name.casefold()
               for row in state['spaces'].values()):
            raise BusinessError('SPACE_NAME_CONFLICT')

    def create(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
        def mutate(state, params):
            self._unique_name(state, params['name'])
            project = params.get('default_project_ref')
            if project is not None:
                self.reference('project', project, actor=actor)
            row = {'owner_id': self.owner_id, 'space_id': 'space-' + uuid.uuid4().hex, 'name': params['name'],
                'purpose': params.get('purpose', ''), 'revision': 0, 'state': 'active',
                'mounts': self._mounts(params.get('mounts', []), actor=actor), 'default_project_ref': project}
            state['spaces'][row['space_id']] = row
            return {key: row[key] for key in ('space_id', 'name', 'purpose', 'revision', 'state', 'mounts')}, True
        return self._transaction('spaces.create', request, actor=actor, idempotency_key=idempotency_key, mutate=mutate)

    def update(self, space_id: str, changes: dict, *, actor: object, expected_revision: int, idempotency_key: str) -> dict:
        if type(changes) is not dict or 'space_id' in changes:
            raise BusinessError('INVALID_SCHEMA')
        def mutate(state, params):
            row = self._space(state, space_id, active=True)
            require_revision(expected_revision, row['revision'])
            if 'name' in params:
                self._unique_name(state, params['name'], excluding=space_id)
            if params.get('default_project_ref') is not None:
                self.reference('project', params['default_project_ref'], actor=actor, space_id=space_id)
            row.update({key: value for key, value in params.items() if key != 'space_id'})
            row['revision'] += 1
            return {'space': self.space_view(row, actor=actor), 'affected_runs': []}, True
        return self._transaction('spaces.update', {'space_id': space_id, **changes}, actor=actor,
            expected_revision=expected_revision, idempotency_key=idempotency_key, mutate=mutate)

    def get(self, space_id: str, *, actor: object) -> dict:
        params = validate_params('spaces.get', {'space_id': space_id})
        state = self.authorize_call('spaces.get', params, actor=actor)
        row = self._space(state, space_id)
        refs = [bid for bid, binding in state['bindings'].items() if binding['state'] == 'bound' and space_id in binding['space_ids']]
        if len(refs) > BUSINESS_LIST_MAX_ROWS:
            # 详情合同无分页字段，不能返回截断列表冒充完整反向关系；调用方改走interfaces.list。
            raise BusinessError('RESULT_LIMIT_EXCEEDED')
        return validate_result('spaces.get', {'space': self.space_view(row, actor=actor),
            'mounts': self.space_view(row, actor=actor)['mounts'], 'interface_refs': refs,
            'policy_refs': [], 'allowed_actions': self.allowed_actions(actor, space_id=space_id)})

    # LLM: W1动作是否已接上原端口的唯一判据；能力发现(system.capabilities)与对象allowed_actions共用，避免公布不可调用的动作。
    # 函数用途: 判断一个空间域动作当前能否真正执行（端口已注入且功能开启）。
    def action_ready(self, action):
        if action.startswith('policies.'):
            return self.governance is not None
        if action == 'spaces.archive':
            return callable(self.active_references)
        if action.startswith(('interfaces.', 'assets.')) or action == 'spaces.packages.mount':
            return callable(self.reference_resolver)
        return bool(self.enabled)

    def allowed_actions(self, actor, **scope):
        from ..plugin_host.business_action_specs import business_action_specs
        allowed = []
        for spec in business_action_specs().values():
            if spec.metadata.get('implementer') != 'W1':
                continue
            if spec.action.startswith(('policies.', 'approvals.')) and self.governance is None:
                continue
            # BIZ-004：未接端口的动作不在对象详情里公布；调用仍走原授权与就绪检查。
            if not spec.action.startswith('approvals.') and not self.action_ready(spec.action):
                continue
            try:
                require_access(actor, spec.action, service=self, **scope)
            except BusinessError as error:
                if error.code != 'BUSINESS_ACCESS_DENIED':
                    raise
            else:
                allowed.append(spec.action)
        return allowed

    def page(self, action, params, rows, state, *, actor):
        # 游标绑定主体、筛选、可见集合与canonical修订；只观察，不推进任何任务/队列。
        limit = params.get('limit', BUSINESS_LIST_DEFAULT_ROWS)
        context = [self.owner_id, actor.subject_id, action, {k: v for k, v in params.items() if k != 'cursor'},
                   state['revision'], rows]
        binding = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        start = 0
        if 'cursor' in params:
            try:
                token = json.loads(base64.urlsafe_b64decode(params['cursor'].encode()))
                if (type(token) is not dict or set(token) != {'binding', 'position', 'signature'} or token['binding'] != binding
                        or type(token['signature']) is not str
                        or type(token['position']) is not int or not 0 <= token['position'] <= len(rows)):
                    raise ValueError('cursor')
                if not hmac.compare_digest(token['signature'], self._cursor_signature(binding, token['position'])):
                    raise ValueError('cursor')
                start = token['position']
            except (ValueError, UnicodeError, TypeError, KeyError) as error:
                raise BusinessError('CURSOR_EXPIRED') from error
        end = min(start + limit, len(rows))
        cursor = base64.urlsafe_b64encode(json.dumps({'binding': binding, 'position': end,
            'signature': self._cursor_signature(binding, end)}).encode()).decode() if end < len(rows) else None
        return {'items': rows[start:end], 'next_cursor': cursor, 'coverage': 'partial' if cursor else 'complete'}

    def _cursor_signature(self, binding, position):
        return hmac.digest(self._cursor_key, json.dumps([binding, position]).encode(), 'sha256').hex()

    def list(self, request=None, *, actor):
        params = validate_params('spaces.list', request or {})
        state = self.authorize_call('spaces.list', params, actor=actor)
        selected = params.get('state', 'active')
        query = unicodedata.normalize('NFKC', params.get('query', '')).casefold()
        rows = []
        for row in sorted(state['spaces'].values(), key=lambda r: r['space_id']):
            if selected != 'all' and selected != row['state'] or query not in row['name'].casefold():
                continue
            try:
                require_access(actor, 'spaces.list', service=self, record=row)
            except BusinessError as error:
                if error.code != 'BUSINESS_ACCESS_DENIED':
                    raise
                continue
            rows.append(self.space_view(row, actor=actor))
        return validate_result('spaces.list', self.page('spaces.list', params, rows, state, actor=actor))

    def blockers(self, space_id, *, actor):
        if not callable(self.active_references):
            raise BusinessError('HOST_NOT_READY')
        rows = self.active_references(space_id, actor)
        if type(rows) is not list or len(rows) > 100 or any(type(row) is not dict or set(row) != {'ref', 'kind', 'reason_code'}
                or any(type(v) is not str or not v for v in row.values()) for row in rows):
            raise BusinessError('DOMAIN_RESULT_INVALID')
        return _copy(rows)

    def change_state(self, action, params, *, actor, expected_revision, idempotency_key):
        def mutate(state, values):
            row = self._space(state, values['space_id'])
            require_revision(expected_revision, row['revision'])
            target = 'archived' if action == 'spaces.archive' else 'active'
            blockers = self.blockers(row['space_id'], actor=actor) if target == 'archived' else []
            if blockers:
                return {'space': self.space_view(row, actor=actor), 'operation_id': None, 'blockers': blockers}, False
            if target == 'active':
                self._unique_name(state, _name(row['name']), excluding=row['space_id'])
            row.update(state=target, revision=row['revision'] + 1)
            return {'space': self.space_view(row, actor=actor), 'operation_id': None, 'blockers': []}, True
        return self._transaction(action, params, actor=actor, expected_revision=expected_revision,
                                 idempotency_key=idempotency_key, mutate=mutate)

    def packages(self, action, params, *, actor, expected_revision=None, idempotency_key=None):
        if action in ('spaces.packages.list', 'spaces.packages.preview'):
            state = self.authorize_call(action, params, actor=actor)
            row = self._space(state, params['space_id'])
            if action == 'spaces.packages.list':
                return validate_result(action, self.page(action, params, self.space_view(row, actor=actor)['mounts'], state, actor=actor))
            desired, unavailable = [], []
            if len({m['package_ref'] for m in params['mounts']}) != len(params['mounts']):
                raise BusinessError('INVALID_SCHEMA')
            for mount in params['mounts']:
                try:
                    self.reference('package', mount['package_ref'], actor=actor, space_id=row['space_id'])
                except BusinessError as error:
                    unavailable.append({'ref': mount['package_ref'], 'state': 'denied' if error.code == 'SPACE_SCOPE_MISMATCH' else 'missing',
                                        'reason_code': error.code, 'required_capabilities': []})
                desired.append({'package_ref': mount['package_ref'], 'enabled': mount.get('enabled', True)})
            prior = [{'package_ref': m['package_ref'], 'enabled': m['enabled']} for m in row['mounts']]
            diff = [] if prior == desired else [{'target_ref': row['space_id'], 'field': 'mounts', 'before': prior, 'after': desired}]
            return validate_result(action, {'diff': diff, 'affected_runs': [], 'unavailable': unavailable, 'warnings': []})
        def mutate(state, values):
            row = self._space(state, values['space_id'], active=True)
            require_revision(expected_revision, row['revision'])
            old = [{'package_ref': m['package_ref'], 'enabled': m['enabled']} for m in row['mounts']]
            by_ref = {m['package_ref']: m for m in old}
            refs = values.get('package_refs', [])
            if action.endswith('.set'):
                desired = values['mounts']
            elif action.endswith('.attach'):
                desired = old + [{'package_ref': ref} for ref in refs if ref not in by_ref]
            elif action.endswith('.detach'):
                if not set(refs) <= by_ref.keys():
                    raise BusinessError('REFERENCE_NOT_FOUND')
                desired = [m for m in old if m['package_ref'] not in refs]
            else:
                if len(refs) != len(old) or set(refs) != by_ref.keys():
                    raise BusinessError('INVALID_SCHEMA')
                desired = [by_ref[ref] for ref in refs]
            row['mounts'] = self._mounts(desired, actor=actor, space_id=row['space_id'])
            row['revision'] += 1
            result = {'space_id': row['space_id'], 'revision': row['revision'], 'mounts': row['mounts']}
            if action.endswith('.set'):
                result['affected_runs'] = []
            return result, True
        return self._transaction(action, params, actor=actor, expected_revision=expected_revision,
                                 idempotency_key=idempotency_key, mutate=mutate)

    def call(self, action, params=None, *, actor, expected_revision=None, idempotency_key=None):
        """W13/native共用领域入口；只接native冻结动作，不处理HTTP、不伪装其它分片handler。"""
        spec = business_action_spec(action)
        if spec.metadata.get('implementer') != 'W1':
            raise BusinessError('UNSUPPORTED_ACTION')
        params = validate_params(action, {} if params is None else params)
        if action == 'spaces.create':
            return self.create(params, actor=actor, idempotency_key=idempotency_key)
        if action == 'spaces.get':
            return self.get(params['space_id'], actor=actor)
        if action == 'spaces.list':
            return self.list(params, actor=actor)
        if action == 'spaces.update':
            return self.update(params['space_id'], {k: v for k, v in params.items() if k != 'space_id'},
                actor=actor, expected_revision=expected_revision, idempotency_key=idempotency_key)
        if action in ('spaces.archive', 'spaces.restore'):
            return self.change_state(action, params, actor=actor, expected_revision=expected_revision, idempotency_key=idempotency_key)
        if action.startswith('spaces.packages.'):
            return self.packages(action, params, actor=actor, expected_revision=expected_revision, idempotency_key=idempotency_key)
        if action.startswith('interfaces.'):
            from .business_bindings import InterfaceBindingService
            return InterfaceBindingService(self).call(action, params, actor=actor,
                expected_revision=expected_revision, idempotency_key=idempotency_key)
        if action.startswith('assets.'):
            from .business_assets import BusinessAssetService
            return BusinessAssetService(self).call(action, params, actor=actor,
                expected_revision=expected_revision, idempotency_key=idempotency_key)
        return governance_call(self, action, params, actor=actor,
                               expected_revision=expected_revision, idempotency_key=idempotency_key)
