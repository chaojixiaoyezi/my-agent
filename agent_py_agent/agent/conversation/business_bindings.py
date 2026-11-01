"""单一InterfaceBinding记录；反向空间关联现查，不另写空间侧镜像列表。"""
from __future__ import annotations

import uuid

from .business_access import BusinessError, require_access, require_revision, validate_result
from .business_spaces import _copy


class InterfaceBindingService:
    def __init__(self, spaces):
        self.spaces = spaces

    @staticmethod
    def view(row):
        return {key: _copy(row[key]) for key in ('binding_id', 'surface_ref', 'name', 'space_ids',
                                                'default_space_id', 'controller', 'revision')}

    def _checked(self, row, state, *, actor, action):
        self.spaces.reference('surface', row['surface_ref'], actor=actor)
        if row['default_space_id'] not in row['space_ids']:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        for sid in row['space_ids']:
            self.spaces._space(state, sid, active=True)
            require_access(actor, action, service=self.spaces, space_id=sid, binding_id=row['binding_id'])
        if row['controller']['mode'] == 'agent':
            agent = self.spaces.reference('agent', row['controller']['agent_id'], actor=actor)
            if agent.get('space_id') not in row['space_ids']:
                raise BusinessError('SPACE_SCOPE_MISMATCH')

    def call(self, action, params, *, actor, expected_revision=None, idempotency_key=None):
        service = self.spaces
        state = service.authorize_call(action, params, actor=actor)
        if action == 'interfaces.list':
            if 'surface_ref' in params:
                service.reference('surface', params['surface_ref'], actor=actor)
            rows = []
            for row in sorted(state['bindings'].values(), key=lambda r: r['binding_id']):
                if (row['state'] != 'bound' or 'space_id' in params and params['space_id'] not in row['space_ids']
                        or 'surface_ref' in params and params['surface_ref'] != row['surface_ref']):
                    continue
                try:
                    service.authorize_call(action, {'binding_id': row['binding_id']}, actor=actor, state=state)
                    self._checked(row, state, actor=actor, action=action)
                except BusinessError as error:
                    if error.code not in ('BUSINESS_ACCESS_DENIED', 'REFERENCE_NOT_FOUND', 'SPACE_ARCHIVED'):
                        raise
                    continue
                rows.append(self.view(row))
            return validate_result(action, service.page(action, params, rows, state, actor=actor))
        if action in ('interfaces.get', 'interfaces.resolve'):
            row = state['bindings'][params['binding_id']]
            self._checked(row, state, actor=actor, action=action)
            result = {'binding': self.view(row), 'allowed_actions': service.allowed_actions(actor, binding_id=row['binding_id'])}
            if action == 'interfaces.get':
                result['revision'] = row['revision']
            else:
                chosen = params.get('selected_space_id', row['default_space_id'])
                if chosen not in row['space_ids']:
                    raise BusinessError('SPACE_SCOPE_MISMATCH')
                result.update(effective_spaces=list(row['space_ids']), controller_ref=row['controller'].get('agent_id'))
            return validate_result(action, result)
        def mutate(current, values):
            if action == 'interfaces.bind':
                if any(row['state'] == 'bound' and row['surface_ref'] == values['surface_ref'] for row in current['bindings'].values()):
                    raise BusinessError('BINDING_CONFLICT')
                row = {'owner_id': service.owner_id, 'binding_id': 'binding-' + uuid.uuid4().hex,
                       **_copy(values), 'revision': 0, 'state': 'bound'}
                self._checked(row, current, actor=actor, action=action)
                current['bindings'][row['binding_id']] = row
                return {'binding_id': row['binding_id'], 'revision': 0, 'binding': self.view(row),
                        'allowed_actions': service.allowed_actions(actor, binding_id=row['binding_id'])}, True
            row = current['bindings'][values['binding_id']]
            require_revision(expected_revision, row['revision'])
            if action == 'interfaces.unbind':
                row.update(state='unbound', revision=row['revision'] + 1)
                return {'binding_id': row['binding_id'], 'state': 'unbound', 'unaffected_refs': list(row['space_ids'])}, True
            prospective = {**row, **{k: _copy(v) for k, v in values.items() if k != 'binding_id'}}
            self._checked(prospective, current, actor=actor, action=action)
            prospective['revision'] += 1
            current['bindings'][row['binding_id']] = prospective
            return {'binding': self.view(prospective), 'revision': prospective['revision'], 'affected_runs': []}, True
        return service._transaction(action, params, actor=actor, expected_revision=expected_revision,
                                    idempotency_key=idempotency_key, mutate=mutate)
