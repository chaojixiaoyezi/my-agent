"""从原待审运行投影有界材料；不读取当前资产、不签权限、不执行候选。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ....common.log_redaction import redact_sensitive_value
from ....common.nofollow_fs import read_bytes_beneath
from ....workflows.governance import pending_review_not_started
from ....workflows.models import redact_bearer_values
from ....workflows.native_governance import review_binding


class ReviewMaterialError(ValueError):
    pass


class _BoundedProjection:
    def __init__(self):
        self.remaining = 24576
        self.nodes = 256
        self.truncated = False

    def value(self, value, *, key='', depth=0):
        if self.remaining <= 0 or self.nodes <= 0 or depth > 8:
            self.truncated = True
            return {'omitted': True}
        self.nodes -= 1
        # 在递归前遮蔽敏感字段，避免对秘密做截断后泄漏前缀。
        if redact_sensitive_value('', field_name=key) == '<redacted>':
            return '<redacted>'
        if isinstance(value, dict):
            result = {}
            for i, (name, item) in enumerate(value.items()):
                if i >= 32 or self.remaining <= 0 or self.nodes <= 0:
                    self.truncated = True
                    break
                safe_key = str(name)[:128]
                self.remaining -= len(safe_key)
                result[safe_key] = self.value(item, key=str(name), depth=depth + 1)
            return result
        if isinstance(value, (tuple, list)):
            result = []
            for i, item in enumerate(value):
                if i >= 32 or self.remaining <= 0 or self.nodes <= 0:
                    self.truncated = True
                    break
                result.append(self.value(item, depth=depth + 1))
            return result
        if isinstance(value, str):
            # 先对有界文本脱敏；长秘密字段已在上方整字段遮蔽。
            limit = min(4096, self.remaining)
            text = redact_bearer_values(value[:limit])
            self.truncated |= len(value) > limit
            self.remaining -= len(text)
            return text
        return value if value is None or type(value) in (bool, int, float) else '<unsupported>'


def workflow_review_material(owner_home, run, requirement, snapshot):
    """调用者已通过叶审核范围；再次逐字段核实原冻结门，变化时拒绝返回材料。"""
    matches = [step for step in run.steps if isinstance(step.escalation_result.get('pending_review'), dict)
               and step.escalation_result['pending_review'].get('requirement_id') == requirement.requirement_id]
    if len(matches) != 1:
        raise ReviewMaterialError('REVIEW_PENDING_BINDING_UNPROVEN')
    step = matches[0]
    binding = step.escalation_result['pending_review']
    policy = snapshot.get('descriptor', {}).get('metadata', {}).get('workflow_review_policy', {})
    if (step.status != 'failed' or step.error_code not in {'WORKFLOW_REVIEW_REQUIRED',
            'WORKFLOW_REVIEW_CHANGES_REQUESTED', 'WORKFLOW_REVIEW_STALE'}
            or run.status == 'cancelled' or step.step_id not in policy.get('steps', [])
            or requirement.root != str(owner_home)
            or not pending_review_not_started(run, step) or binding != review_binding(run, step, snapshot)
            or binding['input_version'] != requirement.input_version
            or requirement.owner_ref != run.owner_id or requirement.task_id != run.workflow_run_id):
        raise ReviewMaterialError('REVIEW_PENDING_BINDING_STALE')
    specs = snapshot.get('steps', [])
    spec = next((row for row in specs if row.get('step_id') == step.step_id), None)
    if spec is None:
        raise ReviewMaterialError('REVIEW_STEP_DEFINITION_MISSING')
    # 只返回该步骤的显式依赖/输入映射来源，不能把同run全部旁支结果变成审核索引。
    dependencies = set(spec.get('depends_on') or ())
    for ref in (spec.get('inputs_map') or {}).values():
        if isinstance(ref, dict) and isinstance(ref.get('step_id'), str) and ref['step_id'] != '$inputs':
            dependencies.add(ref['step_id'])
    projection = _BoundedProjection()
    data = {'schema': 'workflow.review-material.v1', 'binding': projection.value(binding),
            'definition_ref': run.execution_definition_ref,
            'step': projection.value({'step_id': step.step_id, 'mode': step.mode, 'script_id': step.script_id,
                                     'input_snapshot': step.input_snapshot, 'definition': spec}),
            'upstream_outputs': []}
    for row in run.steps:
        if row.step_id not in dependencies:
            continue
        if len(data['upstream_outputs']) >= 16:
            projection.truncated = True
            break
        data['upstream_outputs'].append(projection.value({'step_id': row.step_id, 'status': row.status,
                                                         'output_snapshot': row.output_snapshot}))
    pin = snapshot.get('_frozen_scripts', {}).get(step.step_id)
    if isinstance(pin, dict):
        data['script'] = projection.value({key: pin.get(key) for key in
            ('script_id', 'version', 'code_sha256', 'execution_definition_ref', 'source_scope')})
        digest = pin.get('code_sha256')
        try:
            if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
                raise ReviewMaterialError('FROZEN_SCRIPT_HASH_MISSING')
            raw = read_bytes_beneath(Path(owner_home), ('data', 'asset_evolution', 'blobs', digest[:2], digest), max_bytes=16384)
            if raw is None:
                raise ReviewMaterialError('FROZEN_SCRIPT_BLOB_MISSING')
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ReviewMaterialError('FROZEN_SCRIPT_HASH_MISMATCH')
            code = raw.decode('utf-8')
            redacted = redact_bearer_values(code)
            data['script'].update(code=redacted, code_redacted=redacted != code, coverage='complete')
        except (OSError, ValueError, UnicodeError) as error:
            data['script'].update(coverage='unavailable', reason_code=str(error) if isinstance(error, ReviewMaterialError)
                                  else 'FROZEN_SCRIPT_READ_' + type(error).__name__,
                                  recommended_action='request_scoped_frozen_evidence_do_not_approve_unverified_code')
    data['limits'] = {'projection_characters': 24576, 'projection_nodes': 256, 'depth': 8,
                      'items_per_container': 32, 'upstream_steps': 16, 'script_bytes': 16384}
    data.update(coverage='partial' if projection.truncated or data.get('script', {}).get('coverage') == 'unavailable'
                else 'complete', truncated=projection.truncated)
    return data
