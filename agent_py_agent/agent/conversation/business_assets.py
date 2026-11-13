"""业务资产只关联原artifact/version；候选与正式采用分开，永不保存媒体副本。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .business_access import (
    BusinessError,
    content_authorization_facts,
    require_access,
    require_content_authorization,
    require_revision,
    validate_result,
)
from .business_spaces import _copy, _name


class BusinessAssetService:
    def __init__(self, spaces):
        self.spaces = spaces

    @staticmethod
    def view(row):
        return {key: _copy(row[key]) for key in ('asset_id', 'space_id', 'project_ref', 'name', 'kind', 'revision', 'accepted_version')}

    def _artifact(self, ref, *, actor, space_id, project_ref):
        original = self.spaces.reference('artifact_version', ref['version_ref'], actor=actor,
                                         space_id=space_id, project_ref=project_ref)
        if (original.get('artifact_id') != ref['artifact_id'] or original.get('version_ref') != ref['version_ref']
                or original['state'] != 'available' or 'content_sha256' in ref and original.get('content_sha256') != ref['content_sha256']):
            raise BusinessError('REFERENCE_NOT_FOUND')
        return _copy(ref)

    def _version(self, ref, *, actor, space_id, project_ref):
        checked = self._artifact(ref, actor=actor, space_id=space_id, project_ref=project_ref)
        return {'version_ref': checked['version_ref'], 'artifact_ref': checked, 'state': 'candidate',
                'created_at': datetime.now(timezone.utc).isoformat()}

    def selected_versions(self, state, *, actor, space_id, project_ref, requested=()):
        rows = [row for row in state['assets'].values() if row['state'] == 'linked' and row['space_id'] == space_id
                and row['project_ref'] == project_ref]
        if not requested:
            requested = tuple(row['accepted_version'] for row in rows if row['accepted_version'] is not None)
        result = []
        for ref in requested:
            matches = [(row, version) for row in rows for version in row['versions'] if version['version_ref'] == ref]
            if not matches or any(v['artifact_ref'] != matches[0][1]['artifact_ref'] for _, v in matches):
                raise BusinessError('REFERENCE_NOT_FOUND')
            for row, version in matches:
                require_access(actor, 'assets.get', service=self.spaces, record=row, space_id=space_id, project_ref=project_ref)
                self._artifact(version['artifact_ref'], actor=actor, space_id=space_id, project_ref=project_ref)
            if ref not in result:
                result.append(ref)
        return tuple(result)

    def call(self, action, params, *, actor, expected_revision=None, idempotency_key=None):
        service = self.spaces
        state = service.authorize_call(action, params, actor=actor)
        if action == 'assets.list':
            rows = []
            for row in sorted(state['assets'].values(), key=lambda r: r['asset_id']):
                if (row['state'] != 'linked' or row['space_id'] != params['space_id']
                        or 'project_ref' in params and row['project_ref'] != params['project_ref']):
                    continue
                try:
                    require_access(actor, action, service=service, record=row, space_id=params['space_id'])
                except BusinessError as error:
                    if error.code != 'BUSINESS_ACCESS_DENIED':
                        raise
                    continue
                rows.append(self.view(row))
            return validate_result(action, service.page(action, params, rows, state, actor=actor))
        if action in ('assets.get', 'assets.versions.list'):
            row = state['assets'][params['asset_id']]
            if action == 'assets.get':
                if row['accepted_version'] is not None:
                    self.selected_versions(state, actor=actor, space_id=row['space_id'], project_ref=row['project_ref'],
                                           requested=(row['accepted_version'],))
                return validate_result(action, {'asset': self.view(row), 'accepted_version': row['accepted_version'],
                                                'versions_ref': row['asset_id']})
            for version in row['versions']:
                self._artifact(version['artifact_ref'], actor=actor, space_id=row['space_id'], project_ref=row['project_ref'])
            return validate_result(action, service.page(action, params, _copy(row['versions']), state, actor=actor))
        def content_guard():
            require_content_authorization(actor, content_authorization_facts(
                service.owner_id, action, params, expected_revision))

        def mutate(current, values):
            sid = values['space_id']
            service._space(current, sid, active=True)
            if action == 'assets.register':
                project = values.get('project_ref')
                if project is not None:
                    service.reference('project', project, actor=actor, space_id=sid)
                version = self._version(values['artifact_ref'], actor=actor, space_id=sid, project_ref=project)
                row = {'owner_id': service.owner_id, 'asset_id': 'asset-' + uuid.uuid4().hex, 'space_id': sid,
                    'project_ref': project, 'name': _name(values['name']), 'kind': values['kind'], 'revision': 0,
                    'accepted_version': None, 'state': 'linked', 'versions': [version]}
                current['assets'][row['asset_id']] = row
                return {'asset_id': row['asset_id'], 'revision': 0, 'version_ref': version['version_ref']}, True
            row = current['assets'][values['asset_id']]
            require_revision(expected_revision, row['revision'])
            if action == 'assets.unlink':
                blocked = service.blockers(sid, actor=actor)
                if blocked:
                    return {'state': 'blocked', 'blocked_refs': blocked}, False
                row.update(state='unlinked', revision=row['revision'] + 1)
                return {'state': 'unlinked', 'blocked_refs': []}, True
            if action == 'assets.versions.add':
                version = self._version(values['artifact_ref'], actor=actor, space_id=sid, project_ref=row['project_ref'])
                if any(v['version_ref'] == version['version_ref'] for v in row['versions']):
                    raise BusinessError('ASSET_VERSION_CONFLICT')
                row['versions'].append(version)
                row['revision'] += 1
                return {'asset_id': row['asset_id'], 'version_ref': version['version_ref'], 'state': 'candidate'}, True
            versions = [version for version in row['versions'] if version['version_ref'] == values['version_ref']]
            if len(versions) != 1:
                raise BusinessError('REFERENCE_NOT_FOUND')
            self._artifact(versions[0]['artifact_ref'], actor=actor, space_id=sid, project_ref=row['project_ref'])
            content_guard()
            for version in row['versions']:
                if version['state'] == 'accepted':
                    version['state'] = 'superseded'
            versions[0]['state'] = 'accepted'
            row.update(accepted_version=values['version_ref'], revision=row['revision'] + 1)
            return {'accepted_version': row['accepted_version'], 'revision': row['revision']}, True
        return service._transaction(action, params, actor=actor, expected_revision=expected_revision,
            idempotency_key=idempotency_key, mutate=mutate,
            commit_guard=content_guard if action == 'assets.versions.accept' else None)
