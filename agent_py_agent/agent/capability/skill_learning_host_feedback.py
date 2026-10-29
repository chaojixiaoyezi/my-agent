# LLM: 仅由原验证命令/钉住能力包检查器宿主出口调用，不暴露模型工具；采用必须同owner、run、hash，副作用只写原registry。
# 模块用途: 把已落账的客观验证结果反馈给当前实际采用的方法，环境错误和普通工具成功不算质量证据。
from __future__ import annotations

from dataclasses import asdict, replace

from .skill_learning_maintenance import LearningObservationSource, observe_learning
from .skill_learning_store import SkillLearningStore

_TARGET_KEYS = ('skill_id','skill_name','target_skill_id','asset_id','asset_ref','method_id','target_skill_ids')
_HASH_KEYS = ('sha256','content_hash','content_sha256','skill_content_hash','version_hash')


# LLM: 晚到工具回执不能冒当前新run/attempt记分；call身份来自宿主原调用，缺当前scope不猜测。
# 函数用途: 把同一实际执行的已落账证据批次交给质量记录器，跳过过期执行。
def observe_command_verifications(agent,call,evidence):
    current = getattr(agent,'_current_run_params',None)
    if getattr(current,'run_id',None) != call.run_id or getattr(current,'attempt_id',None) != call.attempt_id:
        return
    for fact in evidence:
        observe_command_verification(agent,current,fact)


# LLM: 仅原账本成功的真实执行回执可记分；明确dry_run/未执行/静态报告拒绝，缺内容/环境归因的普通失败不降分。
# 函数用途: 登记项目明确声明的验证命令通过事实，不解释命令、输出或整轮状态。
def observe_command_verification(agent, params, evidence):
    if (evidence.get('dry_run') is True or evidence.get('executed') is False
            or evidence.get('verification_kind') == 'deterministic_static'):
        return
    if evidence.get('status') == 'passed' and evidence.get('exit_code') == 0 and type(evidence.get('id')) is int:
        _observe(agent,params,'effective',f"verification:{evidence['id']}", evidence)


# LLM: 包检查器failed仅来自valid=false的声明式内容检查；not_run/error/cancelled均不记负样本，复用键绑定目标内容hash。
# 函数用途: 登记原沙箱检查器的内容有效性结论，不复制目标、检查错误或秘密。
def observe_pack_verification(agent, params, result, key):
    if result.status == 'passed' and result.valid is True:
        _observe(agent,params,'effective',f'pack:{key}', vars(result))
    elif result.status == 'failed' and result.valid is False:
        _observe(agent,params,'ineffective',f'pack:{key}', vars(result))


# LLM: 显式目标须准确hash；无目标核对当前及跨更新历史的唯一资产版本对，只有当前版实际采用才能记分。
# 函数用途: 将原宿主回执归给准确采用的方法，兼容真实验证器没有目标字段的单技能运行。
def _observe(agent,params,outcome,receipt,evidence=None):
    if not getattr(getattr(agent,'config',None),'enable_self_learning',False):
        return
    run_id = str(getattr(params,'run_id','') or '')
    current = getattr(agent,'_current_run_params',None)
    if not run_id or getattr(current,'run_id',None) != run_id or getattr(current,'attempt_id',None) != getattr(params,'attempt_id',None):
        return
    try:
        store = SkillLearningStore.for_home(agent.home_paths)
        registry = store.load_registry()
        evidence = evidence or {}
        adopted = [(row, [fact for fact in row.maintenance.get('observations', [])
                         if fact.get('authority') == 'model' and fact.get('outcome') == 'adopted'
                         and fact.get('run_id') == run_id and fact.get('sha256') == row.sha256])
                   for row in registry.skills.values()]
        adopted = [(row, facts) for row, facts in adopted if facts]
        pairs = _adopted_skill_pairs(registry.skills.values(), run_id)
        explicit = any(key in evidence for key in _TARGET_KEYS)
        for row, adoptions in adopted:
            if explicit and not _receipt_targets(evidence, row):
                continue
            if not explicit and (len(pairs) != 1 or len(adopted) != 1 or not row.sha256 or not _receipt_hash_matches(evidence, row)):
                continue
            for adoption in adoptions:
                observe_learning(store,row.name,sha256=row.sha256,task_id=adoption['task_id'],outcome=outcome,source=LearningObservationSource(authority='host',run_id=run_id,receipt_id=receipt,job_id=adoption.get('job_id','')))
    except (AttributeError,OSError,RuntimeError,ValueError):
        return


# LLM: 历史采用只用于歧义判断；返回名称+内容hash，不把历史采用变为当前版本的adoption事实。
# 函数用途: 汇总同一run真正采用过的所有技能版本，包括更新前的版本。
def _adopted_skill_pairs(entries, run_id):
    return {(row.name, fact.get('sha256')) for row in entries
            for fact in (*row.maintenance.get('observations', []), *row.maintenance.get('historical_adoptions', []))
            if isinstance(fact, dict) and fact.get('authority') == 'model'
            and fact.get('outcome') == 'adopted' and fact.get('run_id') == run_id}


# LLM: 显式各组目标须提供顶层或同一结构引用内的准确hash；全部身份/hash别名须一致，不接受其它scope。
# 函数用途: 核对一份宿主验证究竟覆盖哪项当前版本的方法。
def _receipt_targets(evidence, row):
    groups = [[evidence[key]] for key in _TARGET_KEYS[:-1] if key in evidence]
    multiple = evidence.get('target_skill_ids')
    if multiple is not None:
        if not isinstance(multiple, (list, tuple)):
            return False
        groups.append(multiple)
    if not groups:
        return False
    if not _receipt_hash_matches(evidence, row):
        return False
    hashed = any(key in evidence for key in _HASH_KEYS)
    return all(any(_target_matches(target, row) and (hashed or isinstance(target, dict)
                   and any(key in target for key in _HASH_KEYS)) for target in group) for group in groups)


# LLM: 所有版本hash别名须匹配，不能用正确sha256掩盖冲突skill_content_hash/content_hash；不解析正文。
# 函数用途: 让显式目标和单技能归因共用准确版本核对。
def _receipt_hash_matches(evidence, row):
    return all(evidence[key] == row.sha256 for key in _HASH_KEYS if key in evidence)


# LLM: 结构化引用的所有身份/hash别名均须一致；列表中不同引用仍为OR，不用一个正确字段掩盖冲突字段。
# 函数用途: 核对一个完整方法目标，不把别的范围或版本算成命中。
def _target_matches(target, row):
    identities = [target[key] for key in ('stable_id','asset_id') if key in target] if isinstance(target, dict) else [target]
    return bool(identities) and all(isinstance(value, str) and value in {row.name, 'owner:'+row.name} for value in identities) and (
        not isinstance(target, dict) or _receipt_hash_matches(target, row))


# LLM: 仅复用原registry已登记的准确宿主验证；不消费request中的passed/receipt，不执行模型命令或把guard当业务验证。
# 函数用途: 默认机器核验入口，为相同候选和冻结输入的发布重试复用已有执行回执；缺证据保持候选。
def builtin_machine_verifier(publish, sha256, *, store=None):
    from .asset_promotion import asset_identity

    if store is None:
        return None
    binding = _candidate_binding(store, publish, sha256)
    if binding is None:
        return None
    identity = asset_identity('skill', asdict(publish.decision), store.skills_root.parent)
    proof = store.load_registry().assets.get(identity, {}).get('candidate_verification', {})
    if proof.get('binding') != binding or not isinstance(proof.get('receipt_id'), str) or not proof['receipt_id']:
        return None
    return {'status':'passed', 'executed':True, 'contract_valid':True, 'output':{},
            'sha256':sha256, 'receipt_id':proof['receipt_id']}


# LLM: 仅原宿主verifier出口调用，原registry内保存最小证明或使旧证明失效；不保存输出，不把失败安装当正式Skill。
# 函数用途: 耐久保留准确核验，发布中断可重用；同条件重新核验失败则撤销旧通过证明。
def remember_verified_candidate(store, publish, receipt, *, sha256):
    from .asset_promotion import asset_identity, business_validated

    binding = _candidate_binding(store, publish, sha256)
    if binding is None:
        return
    valid = (isinstance(receipt, dict) and business_validated(receipt) and receipt.get('sha256') == sha256
             and isinstance(receipt.get('receipt_id'), str) and bool(receipt['receipt_id']))
    proof = {'binding':binding, 'receipt_id':receipt['receipt_id']} if valid else {}
    identity = asset_identity('skill', asdict(publish.decision), store.skills_root.parent)
    with store.state_lock():
        registry = store.load_registry()
        row = registry.assets.get(identity, {})
        if not valid and row.get('candidate_verification', {}).get('binding') != binding:
            return
        if row.get('candidate_verification') == proof:
            return
        row = {**row, 'candidate_verification':proof}
        if not registry.assets.get(identity):
            row.update(kind='skill', asset_id=publish.decision.name, identity_sha256=identity,
                       sha256='', state='candidate', provenance=[{'origin':'host_verification', 'run_id':binding['run_id']}])
        store.save_registry(replace(registry, assets={**registry.assets, identity:row}))


# LLM: 发布材料、原owner和原请求三重绑定；无准确轨迹不缓存，更新CAS与决策种类变更也使证明失效。
# 函数用途: 固定可复用证据的观察条件，不用同task或同名替代准确候选hash。
def _candidate_binding(store, publish, sha256):
    identity = {key:publish.request.get(key) for key in ('request_key','run_id','trajectory_fingerprint')}
    if not all(isinstance(value, str) and value for value in identity.values()):
        return None
    return {**identity, 'owner_home':str(store.skills_root.parent.resolve()), 'sha256':sha256,
            'asset_id':'owner:'+publish.decision.name, 'decision':publish.decision.decision,
            'expected_revision':publish.expected_revision, 'expected_sha256':publish.expected_sha256}
