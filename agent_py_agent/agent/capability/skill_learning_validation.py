# LLM: 只核验候选的确定性结构与脱敏；不执行候选代码，不把静态通过当业务成功，不写人格或工作约定。
# 模块用途: 为默认自动晋升提供可审计的静态机器核验，与真实执行证明分开。
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import PurePosixPath

from ..common.log_redaction import redact_sensitive_text
from .skill_learning_publish import PublishRequest, render_learned_skill
from .skill_snapshot import skill_content_sha256


# LLM: 返回静态检查报告，executed始终False；所有身份/hash由候选重新计算，不采信模型附带receipt或passed。
# 函数用途: 检查名称、目标、脱敏、方法正文或脚本/流程语法，失败只返回结构化码。
def default_machine_promotion_verifier(candidate_or_draft, sha256=None):
    try:
        publish = isinstance(candidate_or_draft, PublishRequest)
        asset = asdict(candidate_or_draft.decision) if publish else dict(candidate_or_draft)
        target = candidate_or_draft.request.get('target', '') if publish else asset.get('target', '')
        _validate_identity(asset, target)
        kind = asset.get('kind', 'skill')
        _validate_procedure(kind, asset)
        text = render_learned_skill(candidate_or_draft.decision) if publish else json.dumps(asset, sort_keys=True, ensure_ascii=False)
        digest = skill_content_sha256(text)
        if sha256 is not None and sha256 != digest:
            raise ValueError('MACHINE_VERIFICATION_HASH_MISMATCH')
        return {'status':'passed', 'verification_kind':'deterministic_static', 'checks_executed':True,
                'executed':False, 'contract_valid':True, 'sha256':digest,
                'receipt_id':'static:'+hashlib.sha256(text.encode()).hexdigest(),
                'checks':['schema','identity','redaction','procedure_syntax']}
    except (TypeError, ValueError, SyntaxError) as error:
        return {'status':'failed', 'verification_kind':'deterministic_static', 'checks_executed':True,
                'executed':False, 'error_code':str(error) if isinstance(error, ValueError) else 'MACHINE_DRAFT_INVALID'}


# LLM: 纯元数据闸门，不打开路径；目标规范化后人格/约定硬拒绝，名称和声明路径不得穿越，脱敏命中整份拒绝。
# 函数用途: 核对候选目标与公开字段，阻止把自动方法发布绕到AGENTS/SOUL。
def _validate_identity(asset, target):
    manual = {'agents','soul','agents.md','soul.md'}
    names = (str(target).strip().casefold(), str(asset.get('name', '')).strip().casefold())
    if any(name in manual or PurePosixPath(name.replace('\\', '/')).name in manual for name in names):
        raise ValueError('MANUAL_ONLY_TARGET')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', str(asset.get('name', ''))):
        raise ValueError('MACHINE_DRAFT_NAME_INVALID')
    description = asset.get('description')
    if not isinstance(description, str) or not description.strip() or '\n' in description or '\r' in description:
        raise ValueError('MACHINE_DRAFT_SCHEMA_INVALID')
    for key in ('path','resource_path','entrypoint','target'):
        value = str(asset.get(key, '') or '').replace('\\', '/')
        if value and (PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts):
            raise ValueError('MACHINE_DRAFT_PATH_INVALID')
    text = json.dumps(asset, sort_keys=True, ensure_ascii=False)
    if redact_sensitive_text(text, code_file=True) != text:
        raise ValueError('MACHINE_DRAFT_SECRET_REJECTED')


# LLM: 仅编译不执行；workflow只接受唯一步骤、原生模式、存在且无环的依赖，未知载体不伪报通过。
# 函数用途: 核对方法有正文、Python可编译、工作流结构能形成DAG。
def _validate_procedure(kind, asset):
    if kind == 'skill':
        if not isinstance(asset.get('body'), str) or not asset['body'].strip():
            raise ValueError('MACHINE_DRAFT_PROCEDURE_EMPTY')
    elif kind == 'script':
        code = asset.get('entry_source', asset.get('code'))
        if not isinstance(code, str) or not code.strip():
            raise ValueError('MACHINE_DRAFT_SCRIPT_EMPTY')
        compile(code, '<machine-candidate>', 'exec', dont_inherit=True)
    elif kind == 'workflow':
        _validate_steps(asset.get('steps'))
    else:
        raise ValueError('MACHINE_DRAFT_CARRIER_UNSUPPORTED')


# LLM: 不调用脚本库或执行步骤；字段不合规、未知依赖、循环都拒绝，不把空流程当通过。
# 函数用途: 静态验证工作流步骤的身份、模式和依赖图。
def _validate_steps(steps):
    if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
        raise ValueError('MACHINE_DRAFT_WORKFLOW_INVALID')
    ids = [step.get('step_id') for step in steps]
    if not all(isinstance(identifier, str) and identifier for identifier in ids) or len(set(ids)) != len(ids):
        raise ValueError('MACHINE_DRAFT_WORKFLOW_IDS_INVALID')
    pending = {}
    for step in steps:
        refs = step.get('depends_on', [])
        if (step.get('mode') not in {'deterministic_script','llm_skill_analysis','hybrid'}
                or not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in ids for ref in refs)):
            raise ValueError('MACHINE_DRAFT_WORKFLOW_STEP_INVALID')
        pending[step['step_id']] = set(refs)
    while pending:
        ready = {key for key, refs in pending.items() if not refs}
        if not ready:
            raise ValueError('MACHINE_DRAFT_WORKFLOW_CYCLE')
        pending = {key:refs-ready for key, refs in pending.items() if key not in ready}


# LLM: 先复用原registry准确的真实执行证明，否则真实运行静态检查；不会把静态报告写成业务执行证据。
# 函数用途: 将默认静态检查与已有宿主证明复用接到同一生产核验出口。
def verify_machine_candidate(publish, sha256, *, store):
    from .skill_learning_host_feedback import builtin_machine_verifier

    return builtin_machine_verifier(publish, sha256, store=store) or default_machine_promotion_verifier(publish, sha256)
