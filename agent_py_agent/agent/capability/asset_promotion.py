# LLM: 四类自动提炼资产共用原registry；不授工具、Owner或人格修改权限。
# 模块用途: 为学习入口统一语义族、来源与机器验证晋升。
from __future__ import annotations

import ast
import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import read_text_beneath, unlink_file_beneath, write_text_atomic_beneath
from .skill_learning_store import LearnedSkill, SkillLearningStore, utc_now
from .skill_snapshot import skill_content_sha256


# LLM: 纯结构分类，不解析自然语言授执行权。
# 函数用途: 选择适合复用的资产载体。
def select_asset_carrier(features):
    if features.get('branches'):
        return 'skill'
    if len(features.get('steps') or ()) > 1:
        return 'workflow'
    return 'script' if features.get('commands') else 'skill'


# LLM: 只计算语义族，不作为授权或验证通过证明。
# 函数用途: 计算资产方法的稳定去重键。
def task_family_fingerprint(kind, asset):
    if kind == 'script':
        code = asset.get('entry_source', asset.get('code', ''))
        signature = [ast.dump(ast.parse(code), include_attributes=False), asset.get('input_schema'), asset.get('output_schema')]
    elif kind == 'workflow':
        steps = asset.get('steps', [])
        ids = {step.get('step_id', step.get('id')): str(i) for i, step in enumerate(steps)}
        signature = [{key:value for key,value in step.items() if key not in {'name','step_id','id'}} for step in steps]
        signature = _normalize_step_refs(signature, ids)
    else:
        body = asset.get('body', '')
        body = re.sub(r'\A---\n.*?\n---\n', '', body, flags=re.S)
        body = re.sub(r'^#{1,6}[^\n]*', '', body, flags=re.M)
        signature = re.findall(r'[\w]+', body.casefold())
    return hashlib.sha256(json.dumps([kind, signature], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# LLM: 只规范明确的DAG引用字段，业务字面值与未知字段原样；映射一次不级联重写。
# 函数用途: 对步骤重命名消歧，不把不同业务输入或不同依赖合为同族。
def _normalize_step_refs(value, ids):
    if isinstance(value, list):
        return [_normalize_step_refs(item, ids) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: [ids.get(item, item) for item in val] if key == 'depends_on' and isinstance(val, list)
            else ids.get(val, val) if key in {'step_id', 'rule_id'} and isinstance(val, str)
            else _normalize_step_refs(val, ids) for key, val in value.items()}


# LLM: 不把退出成功等同业务执行通过。
# 函数用途: 检查资产真实执行与业务验证证据。
def business_validated(receipt):
    output = receipt.get('output', {})
    return (receipt.get('status') in {'passed', 'succeeded'} and receipt.get('executed') is True
            and receipt.get('contract_valid') is True and isinstance(output, dict)
            and output.get('dry_run') is not True and output.get('executed') is not False)


# LLM: 调用方持原state/配额锁；只保存摘要/精确引用不存代码秘密，语义族不替代正式hash和CAS。
# 函数用途: 在唯一registry内合并四类资产来源，不因改名重复创建同族资产。
def register_asset_locked(store, registry, *, kind, asset_id, asset, source, sha256='', state='candidate'):
    family = task_family_fingerprint(kind, asset)
    previous = registry.assets.get(family, {})
    refs = list(previous.get('provenance', []))
    if source not in refs:
        refs.append(source)
    same = previous.get('asset_id', asset_id).removeprefix('owner:') == asset_id.removeprefix('owner:')
    update = same and bool(sha256)
    row = {**previous, 'kind':kind, 'asset_id':previous.get('asset_id', asset_id),
           'task_family_fingerprint':family, 'sha256':sha256 if update else previous.get('sha256', ''),
           'state':state if update or not previous else previous['state'], 'provenance':refs[-10:]}
    return replace(registry, assets={**registry.assets, family:row}), row


# LLM: 仅宿主调用；原Owner锁包住唯一registry登记，正文仍留原资产库，不接管用户文件。
# 函数用途: 为S1与显式提炼登记同族来源，返回可复用的既有资产身份。
def register_asset(store, **kwargs):
    with store.state_lock():
        registry, row = register_asset_locked(store, store.load_registry(), **kwargs)
        store.save_registry(registry)
    return row


# LLM: 提炼事务持原资产锁；yield跨正文/提案写入，失败恢复准确版本及registry，不接管workspace正文。
# 函数用途: 把显式发布的资产来源和Owner技能纳入同一维护权威。
@contextmanager
def register_extracted_assets(home, chosen, proposal, scope):
    from pathlib import Path

    store = SkillLearningStore(Path(home)/'data'/'skill_learning', Path(home)/'skills')
    with store.state_lock(), ExitStack() as rollback:
        registry = store.load_registry()
        _restore_on_failure(store, ('registry.json',), rollback)
        for kind, asset in chosen:
            identifier = asset['descriptor']['id'] if kind == 'workflow' else asset.get('script_id', asset.get('name'))
            text = asset.get('body', asset.get('entry_source', json.dumps(asset, sort_keys=True)))
            sha = skill_content_sha256(text)
            registry, _ = register_asset_locked(store, registry, kind=kind, asset_id=identifier, asset=asset,
                source={'origin':'turn_extractor','proposal_id':proposal['proposal_id'],'scope':scope}, sha256=sha, state='trial')
            if kind == 'skill' and scope == 'owner':
                if identifier in registry.skills or identifier in registry.blocked_names:
                    raise ValueError('EXTRACTION_REGISTERED_NAME_EXISTS')
                now = utc_now()
                _restore_on_failure(store, ('versions', identifier, 'v1.md'), rollback)
                store.save_version(identifier, 1, text)
                record = LearnedSkill(identifier, 1, sha, now, now, maintenance={'state':'trial','quality':'trial'})
                registry = replace(registry, skills={**registry.skills, identifier:record})
        yield
        store.save_registry(registry)
        rollback.pop_all()


# LLM: 恢复只作用于原store目录下的准确叶，不遍历、不删除既有目录；异常向上传递不能伪报成功。
# 函数用途: 为注册事务保存写入前的字节状态。
def _restore_on_failure(store, parts, rollback):
    previous = read_text_beneath(store.directory, parts)
    rollback.callback(unlink_file_beneath, store.directory, parts) if previous is None else rollback.callback(write_text_atomic_beneath, store.directory, parts, previous)


# LLM: 宿主重新验证前撤销同条件旧证明；准确执行/合同过闸后登记原回执，默认复用不重跑，publisher仍核CAS。
# 函数用途: 让通过机器核验的Skill免人工确认进入试用，否则保持候选，绝不伪造user_confirmed。
def promote_verified_skill(store, publish, verifier):
    from . import skill_learning_host_feedback as feedback
    from .skill_learning_publish import (
        _redacted_publish,
        _stage_and_check,
        publish_learned_skill,
        render_learned_skill,
    )

    if verifier is None:
        return None
    safe, redacted = _redacted_publish(publish)
    if redacted:
        return None
    with store.state_lock():
        _stage_and_check(store, safe, render_learned_skill(safe.decision))
    sha = skill_content_sha256(render_learned_skill(safe.decision))
    if getattr(verifier, 'func', verifier) is not feedback.builtin_machine_verifier:
        feedback.remember_verified_candidate(store, safe, None, sha256=sha)
    receipt = verifier(safe, sha)
    feedback.remember_verified_candidate(store, safe, receipt, sha256=sha)
    if (not isinstance(receipt, dict) or not business_validated(receipt) or receipt.get('sha256') != sha
            or not receipt.get('receipt_id')):
        return None
    return publish_learned_skill(store, replace(safe, promotion_authority='autonomous_verified',
        verification_receipt={'sha256':sha,'receipt_id':receipt['receipt_id']}))


# LLM: 先按正式语义族和准确候选复用，再尝试宿主机器核验；候选默认仍经原结构/脱敏/guard入口，不伪造用户确认。
# 函数用途: 为S3统一处理复用、机器晋升与保留候选三种路径。
def propose_or_promote_skill(store, publish, *, verifier=None):
    with locked_json_path(store.directory / '.families'):
        return _propose_or_promote_locked(store, publish, verifier)


# LLM: 只由family锁内调用，模型无verifier；正式复用仍验证磁盘所有权，原发布闸门二次核CAS。
# 函数用途: 串行化同族候选检查及创建，允许已有pending获得真实验证后晋升。
def _propose_or_promote_locked(store, publish, verifier):
    from dataclasses import asdict

    from .skill_learning_proposals import list_learning_proposals, propose_learning
    from .skill_learning_publish import (
        CODE_LIMIT_REACHED,
        SkillLearningGateError,
        _family_record_locked,
    )
    from .skill_learning_store import SkillLearningEvent

    family = task_family_fingerprint('skill', asdict(publish.decision))
    with store.state_lock():
        registry = store.load_registry()
        if (publish.decision.decision == 'create' and publish.max_skills > 0
                and len(registry.skills) >= publish.max_skills and publish.decision.name not in registry.skills):
            raise SkillLearningGateError(CODE_LIMIT_REACHED)
        reusable = _family_record_locked(store, registry, publish) if publish.decision.decision == 'create' else None
    if reusable is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=reusable.name)
    pending = next((row for row in list_learning_proposals(store) if row['status']=='pending'
                    and task_family_fingerprint('skill', row['decision']) == family), None)
    event = promote_verified_skill(store, publish, verifier)
    if event is not None:
        return event
    if pending is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=pending['decision']['name'])
    event = propose_learning(store, publish)
    register_asset(store, kind='skill', asset_id=publish.decision.name, asset=asdict(publish.decision),
                   source={'origin':'S3','proposal_id':event.reason}, state='candidate')
    return event
