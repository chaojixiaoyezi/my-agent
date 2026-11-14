"""已授权包成员的工作流派生：只消费原字节，拒绝宿主状态，签发不可由JSON提交的来源证明。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..common.strict_json import load_strict_json
from ..tooling.content_transport_policy import FileSourceContent
from . import MAX_ASSET_BYTES, safe_asset_id
from .asset_metadata import HOST_MANAGED_METADATA_KEYS

_ISSUER = object()
_SOURCE_SCHEMA = 'workflow-package-source.v1'
_RESERVED = frozenset(('business_workflow', 'base_workflow_pin',
    'derived_from', 'business_context', 'scope_snapshot', 'host_channel_actor', 'host_parent_execution',
    'tool_runtime_snapshot', 'tool_protocol_snapshot', 'review_receipt', 'approval_ticket',
    'accepted_input_version', 'accepted_by_principal_id', 'permission_grants', 'capability_grants',
    'credential_ref', 'credential_refs', 'review_id', 'approval_id', 'acceptance_id',
    'verification_event_ids'))


class WorkflowTemplateError(ValueError):
    def __init__(self, code, *, field_path='', details=None, next_action='修正模板后从原包重新取得完整source_ref，再用新的Owner工作流ID保存。'):
        self.code = code
        self.details = {'error_code': code, 'field_path': field_path, **(details or {}),
            'effect_outcome': 'not_started', 'next_action': next_action}
        super().__init__(code)


def _digest(definition):
    return hashlib.sha256(json.dumps(definition.to_dict(), sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PackageWorkflowSource:
    owner_home: Path
    target_id: str
    definition_digest: str
    source_fields: tuple[tuple[str, str], ...]
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_HOST_ONLY')

    def provenance(self):
        return {'schema_version': _SOURCE_SCHEMA, 'kind': 'capability_package', 'verified': True,
            'source_ref': dict(self.source_fields)}

    def require(self, owner_home, definition):
        if (self._issuer is not _ISSUER or Path(owner_home).absolute() != self.owner_home
                or definition.descriptor.id != self.target_id or _digest(definition) != self.definition_digest):
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_MISMATCH')


def _check_reserved(value, path='$'):
    if isinstance(value, dict):
        for key, item in value.items():
            if (key in _RESERVED or key.startswith('_')
                    or path.endswith('.metadata') and key in HOST_MANAGED_METADATA_KEYS):
                raise WorkflowTemplateError('WORKFLOW_TEMPLATE_RESERVED_FIELD', field_path=path + '.' + key)
            _check_reserved(item, path + '.' + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_reserved(item, f'{path}[{index}]')


def decode_package_workflow(content, source_ref, workflow_id):
    """解析前后都不执行节点；来源字节已由原资源resolver按Owner、快照与activation核验。"""
    from ..capability.package_resources import _validated_reference

    safe_asset_id(workflow_id)
    reference = _validated_reference(source_ref)
    if type(content) is not FileSourceContent or dict(content.source_ref) != reference:
        raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_MISMATCH')
    if len(content.data) > MAX_ASSET_BYTES:
        raise WorkflowTemplateError('WORKFLOW_TEMPLATE_TOO_LARGE')
    if hashlib.sha256(content.data).hexdigest() != reference['resource_sha256']:
        raise WorkflowTemplateError('WORKFLOW_TEMPLATE_DIGEST_MISMATCH')
    try:
        value = load_strict_json(content.data)
        if type(value) is not dict or type(value.get('descriptor')) is not dict:
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_INVALID', field_path='$.descriptor')
        descriptor = value['descriptor']
        if descriptor.get('id') == workflow_id:
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_TARGET_IS_SOURCE', field_path='$.workflow_id')
        # 这些是宿主身份/运行事实；允许普通导出的空默认值，不接受导入已授予的权限或凭据。
        from .descriptor import AssetDescriptor

        baseline = AssetDescriptor(id='template-defaults', kind='workflow', name='模板').to_dict()
        defaults = {key: baseline[key] for key in ('owner_id', 'scope_layer', 'source_scope',
            'workspace_root', 'revision', 'credential_refs', 'used_by', 'runtime_summary')}
        for key, default in defaults.items():
            if key in descriptor and (type(descriptor[key]) is not type(default) or descriptor[key] != default):
                raise WorkflowTemplateError('WORKFLOW_TEMPLATE_RESERVED_FIELD', field_path='$.descriptor.' + key)
            descriptor.pop(key, None)
        if value.get('schedule_ids'):
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_RESERVED_FIELD', field_path='$.schedule_ids')
        if descriptor.get('kind', 'workflow') != 'workflow':
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_INVALID', field_path='$.descriptor.kind')
        descriptor['id'] = workflow_id
        descriptor['kind'] = 'workflow'
        _check_reserved(value)
        descriptor['credential_refs'] = []
        return value
    except WorkflowTemplateError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        from ..common.log_redaction import redact_sensitive_text

        raise WorkflowTemplateError('WORKFLOW_TEMPLATE_INVALID', field_path='$', details={
            'validation_exception_type': type(error).__name__,
            'validation_reason': redact_sensitive_text(str(error))[:256]}) from error


def issue_package_workflow_source(owner_home, definition, source_ref):
    """仅宿主解析器读完并通过原定义校验后调用；证明同时绑定新目标、消费Owner和完整候选。"""
    return PackageWorkflowSource(Path(owner_home).absolute(), definition.descriptor.id, _digest(definition),
        tuple(sorted(source_ref.items())), _issuer=_ISSUER)
