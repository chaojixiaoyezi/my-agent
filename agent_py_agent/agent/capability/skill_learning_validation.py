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


# LLM: 纯元数据闸门，不打开路径；人格/约定硬拒绝，Skill缺描述单列；路径穿越和脱敏命中整份拒绝。
# 函数用途: 核对候选目标与公开字段，阻止把自动方法发布绕到AGENTS/SOUL。
def _validate_identity(asset, target):
    manual = {'agents','soul','agents.md','soul.md'}
    names = (str(target).strip().casefold(), str(asset.get('name', '')).strip().casefold())
    payload_text = json.dumps([asset.get('entry_source', ''), asset.get('code', ''), asset.get('steps', ''), asset.get('sample_inputs', '')], ensure_ascii=False).casefold()
    if any(name in manual or PurePosixPath(name.replace('\\', '/')).name in manual for name in names) or 'agents.md' in payload_text or 'soul.md' in payload_text:
        raise ValueError('MANUAL_ONLY_TARGET')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', str(asset.get('name', ''))):
        raise ValueError('MACHINE_DRAFT_NAME_INVALID')
    description = asset.get('description')
    if asset.get('kind', 'skill') == 'skill' and (not isinstance(description, str) or not description.strip()):
        raise ValueError('description_missing')
    if not isinstance(description, str) or not description.strip() or '\n' in description or '\r' in description:
        raise ValueError('MACHINE_DRAFT_SCHEMA_INVALID')
    for key in ('path','resource_path','entrypoint','target'):
        value = str(asset.get(key, '') or '').replace('\\', '/')
        if value and (PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts):
            raise ValueError('MACHINE_DRAFT_PATH_INVALID')
    text = json.dumps(asset, sort_keys=True, ensure_ascii=False)
    if redact_sensitive_text(text, code_file=True) != text:
        raise ValueError('MACHINE_DRAFT_SECRET_REJECTED')


# LLM: Skill检查触发信息与结构；脚本仅编译不执行，workflow要求唯一身份和DAG，未知载体不伪报通过。
# 函数用途: 按载体核对方法结构，不把检查通过当作业务执行。
def _validate_procedure(kind, asset):
    if kind == 'skill':
        return _validate_skill_procedure(asset)
    if kind == 'script':
        code = asset.get('entry_source', asset.get('code'))
        if not isinstance(code, str) or not code.strip():
            raise ValueError('MACHINE_DRAFT_SCRIPT_EMPTY')
        return compile(code, '<machine-candidate>', 'exec', dont_inherit=True)
    if kind == 'workflow':
        return _validate_steps(asset.get('steps'))
    if kind != 'capability_package' or not isinstance(asset.get('entry_document', 'CAPABILITY.md'), str) or not (asset.get('body') or asset.get('files')):
        raise ValueError('MACHINE_DRAFT_PACKAGE_INVALID' if kind == 'capability_package' else 'MACHINE_DRAFT_CARRIER_UNSUPPORTED')


# LLM: 至少两行且有结构；旧格式兼容须标题加真实步骤或闭合非空代码块，空触发节不能借下一标题充数。
# 函数用途: 拒绝空洞单句或没有适用信号的方法，保留旧的完整分节Skill。
def _validate_skill_procedure(asset):
    body = asset.get('body')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('MACHINE_DRAFT_PROCEDURE_EMPTY')
    heading = re.search(r'(?m)^\s*#{1,6}\s+\S', body)
    code = re.search(r'(?m)^[ \t]*(`{3,}|~{3,})[^\n]*\n((?:[^\n]*\n)+?)[ \t]*\1[ \t]*$', body)
    steps = re.search(r'(?m)^[ \t]*(?:\d+[.)]\s+\S|[-*]\s+\S|\$\s+\S)', body) or (code and code.group(2).strip())
    if len([line for line in body.splitlines() if line.strip()]) < 2 or not (heading or steps):
        raise ValueError('skill_procedure_unstructured')
    when = asset.get('when_to_use')
    tags = asset.get('tags')
    metadata = (isinstance(when, str) and bool(when.strip())) or (
        isinstance(tags, (list, tuple)) and any(isinstance(tag, str) and tag.strip() for tag in tags))
    trigger = _has_skill_trigger_section(body)
    if not (metadata or trigger or (asset.get('description') and heading and steps)):
        raise ValueError('skill_trigger_metadata_missing')


# LLM: Markdown标题是节边界；仅本节非空内容构成信号，不跨过空节读下一标题，不做自然语言业务定性。
# 函数用途: 识别有内容的中英文适用/触发节。
def _has_skill_trigger_section(body):
    titles = r'when(?:\s+to)?\s+use|适用(?:场景|范围|时机)?|何时使用|使用时机|触发(?:条件|场景)'
    for section in re.split(r'(?m)^[ \t]*#{1,6}[ \t]+', body)[1:]:
        title, _, content = section.partition('\n')
        if re.fullmatch(titles, title.strip().rstrip('#').strip(), re.I) and content.strip():
            return True
    return False


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
