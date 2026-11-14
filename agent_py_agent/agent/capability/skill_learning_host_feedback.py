# LLM: 仅由原验证命令/钉住能力包检查器宿主出口调用，不暴露模型工具；采用必须同owner、run、hash，副作用只写原registry。
# 模块用途: 把已落账的客观验证结果反馈给当前实际采用的方法，环境错误和普通工具成功不算质量证据。
from __future__ import annotations

import logging
from dataclasses import asdict, replace
from pathlib import Path

from .skill_learning_maintenance import (
    _HOST_DELIVERY_KEYS_CTX,
    LearningObservationSource,
    observe_learning,
)
from .skill_learning_publish import SkillLearningGateError
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_usage_receipts import candidate_delivery_task_keys, has_body_delivery_receipt

_TARGET_KEYS = ('skill_id','skill_name','target_skill_id','asset_id','asset_ref','method_id','target_skill_ids')
_HASH_KEYS = ('sha256','content_hash','content_sha256','skill_content_hash','version_hash')
_ENVIRONMENTAL_FAILURE_MARKERS = (
    'timeout', 'timed_out', 'network', 'connection', 'dns', 'provider',
    'rate_limit', 'quota', 'sandbox_unavailable', 'verifier_timeout',
    'verifier_cancelled', 'verifier_input_unresolved', 'environment',
    'environmental', 'transient', 'service_unavailable', 'bad_gateway',
    'gateway_timeout', 'unavailable',
)


# LLM: 晚到工具回执不能冒当前新run/attempt记分；call身份来自宿主原调用，缺当前scope不猜测。
# 函数用途: 把同一实际执行的已落账证据批次交给质量记录器，跳过过期执行。
def observe_command_verifications(agent,call,evidence):
    current = getattr(agent,'_current_run_params',None)
    if getattr(current,'run_id',None) != call.run_id or getattr(current,'attempt_id',None) != call.attempt_id:
        return
    diagnostics = []
    for fact in evidence:
        diagnostics.extend(observe_command_verification(agent,current,fact) or ())
    return diagnostics


# LLM: 仅原账本成功的真实执行回执可记分；明确dry_run/未执行/静态报告拒绝，缺内容/环境归因的普通失败不降分。
# 函数用途: 登记项目明确声明的验证命令通过事实，不解释命令、输出或整轮状态。
def observe_command_verification(agent, params, evidence):
    if (evidence.get('dry_run') is True or evidence.get('executed') is False
            or evidence.get('verification_kind') == 'deterministic_static'):
        return
    if evidence.get('status') == 'passed' and evidence.get('exit_code') == 0 and type(evidence.get('id')) is int:
        return _observe(agent,params,'effective',f"verification:{evidence['id']}", evidence)


def _is_environmental_pack_failure(result, evidence: dict) -> bool:
    if (
        getattr(result, 'transient', False) is True
        or getattr(result, 'environmental', False) is True
        or evidence.get('transient') is True
        or evidence.get('environmental') is True
        or str(evidence.get('outcome') or '').strip().lower() == 'environment'
    ):
        return True
    indicators: list[str] = []
    for attr in ('reason_code', 'error_code', 'error', 'failure_reason', 'status_reason'):
        val = evidence.get(attr, getattr(result, attr, None))
        if isinstance(val, str) and val.strip():
            indicators.append(val.strip().lower())
    for seq_attr in ('error_codes', 'reasons'):
        seq = evidence.get(seq_attr, getattr(result, seq_attr, None))
        if isinstance(seq, (list, tuple, set)):
            indicators.extend(str(item).strip().lower() for item in seq if item)
    err_counts = evidence.get('error_counts', getattr(result, 'error_counts', None))
    if isinstance(err_counts, dict):
        indicators.extend(str(k).strip().lower() for k in err_counts if k)
    return any(marker in text for text in indicators for marker in _ENVIRONMENTAL_FAILURE_MARKERS)


# LLM: V2-2 通道 C：确定性非环境 pack failed/valid=False 可通过自愿 adopted 或规范正文送达 UsageReceipt 归因；
#   瞬态网络/超时/provider/环境失败一律跳过不击打。
# 函数用途: 登记原沙箱检查器的内容有效性结论，不复制目标、检查错误或秘密。
def observe_pack_verification(agent, params, result, key):
    status = getattr(result, 'status', None)
    valid = getattr(result, 'valid', None)
    evidence = dict(vars(result)) if hasattr(result, '__dict__') else {}
    if status == 'passed' and valid is True:
        return _observe(agent, params, 'effective', f'pack:{key}', evidence)
    if status == 'failed' and valid is False:
        if _is_environmental_pack_failure(result, evidence):
            return []
        return _observe(
            agent,
            params,
            'ineffective',
            f'pack:{key}',
            evidence,
            allow_body_delivery_receipt=True,
        )
    return []


# LLM: 显式目标须准确 hash；无目标核对当前及跨更新历史的唯一资产版本对，只有当前版实际采用或同任务存在正文送达回执才归因。
# 函数用途: 将原宿主回执归给准确采用或使用过的方法，兼容无目标字段的单技能运行。
def _observe(agent, params, outcome, receipt, evidence=None, *, allow_body_delivery_receipt: bool = False):
    if not getattr(getattr(agent, 'config', None), 'enable_self_learning', False):
        return
    run_id = str(getattr(params, 'run_id', '') or '')
    current = getattr(agent, '_current_run_params', None)
    if not run_id or getattr(current, 'run_id', None) != run_id or getattr(current, 'attempt_id', None) != getattr(params, 'attempt_id', None):
        return
    repo = getattr(agent, 'runtime_db', None) or getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
    provider = (lambda r=repo: r) if repo is not None else None
    try:
        store = SkillLearningStore.for_home(agent.home_paths, usage_runtime_provider=provider)
        registry = store.load_registry()
    except (SkillLearningStoreError, OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("failed to load registry in _observe: %s", exc, exc_info=True)
        return []
    evidence = evidence or {}
    task_id = str(getattr(params, 'task_id', '') or '')
    request_id = str(getattr(params, 'request_id', '') or '')
    attrs = getattr(params, 'task_attributes', None) or {}
    root_task_id = str(attrs.get('root_task_id') or '') if isinstance(attrs, dict) else ''
    canonical_task = root_task_id or task_id or (f'request:{request_id}' if request_id else run_id)

    candidates: list[tuple[object, list[dict], bool]] = []
    body_receipt_pairs: set[tuple[str, str]] = set()
    for row in registry.skills.values():
        if row.layout == 'owner':
            continue
        adoptions = [
            fact
            for fact in row.maintenance.get('observations', [])
            if fact.get('authority') == 'model'
            and fact.get('outcome') == 'adopted'
            and fact.get('run_id') == run_id
            and fact.get('sha256') == row.sha256
        ]
        if adoptions:
            candidates.append((row, adoptions, False))
        elif allow_body_delivery_receipt and repo is not None and row.sha256:
            inc = str(row.maintenance.get('incarnation_id') or '')
            act = str(row.maintenance.get('activation_id') or '')
            if inc and act and has_body_delivery_receipt(
                repo,
                incarnation_id=inc,
                activation_id=act,
                content_sha256=row.sha256,
                root_task_id=root_task_id,
                task_id=task_id,
                request_id=request_id,
            ):
                body_receipt_pairs.add((row.name, row.sha256))
                candidates.append((row, [{'task_id': canonical_task, 'job_id': ''}], True))

    pairs = _adopted_skill_pairs(registry.skills.values(), run_id) | body_receipt_pairs
    explicit = any(key in evidence for key in _TARGET_KEYS)
    diagnostics = []
    for row, items, via_body_receipt in candidates:
        if explicit and not _receipt_targets(evidence, row):
            continue
        if not explicit and (len(pairs) != 1 or len(candidates) != 1 or not row.sha256 or not _receipt_hash_matches(evidence, row)):
            continue
        inc = str(row.maintenance.get('incarnation_id') or '')
        act = str(row.maintenance.get('activation_id') or '')
        epoch = row.maintenance.get('strike_epoch')
        epoch_val = epoch if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0 else None
        delivery_keys = (
            tuple(
                candidate_delivery_task_keys(
                    root_task_id=root_task_id,
                    task_id=task_id,
                    request_id=request_id,
                )
            )
            if via_body_receipt
            else ()
        )
        for item in items:
            eff_task_id = canonical_task if (via_body_receipt or root_task_id) else (item.get('task_id') or canonical_task)
            token = _HOST_DELIVERY_KEYS_CTX.set(delivery_keys) if delivery_keys else None
            try:
                observe_learning(
                    store,
                    row.name,
                    sha256=row.sha256,
                    task_id=eff_task_id,
                    outcome=outcome,
                    source=LearningObservationSource(
                        authority='host',
                        run_id=run_id,
                        receipt_id=receipt,
                        job_id=item.get('job_id', ''),
                        incarnation_id=inc,
                        activation_id=act,
                        strike_epoch=epoch_val,
                    ),
                )
            except SkillLearningGateError as error:
                # 只跳过这一项的过期观察，CAS不变，旧质量绝不归给新版；其余错误原样交宿主。
                if error.code != 'SKILL_LEARNING_REVISION_CONFLICT':
                    raise
                diagnostics.append({
                    'status': 'stale',
                    'reason': error.code,
                    'skill_id': 'owner:' + row.name,
                    'receipt_ref': receipt,
                })
            finally:
                if token is not None:
                    _HOST_DELIVERY_KEYS_CTX.reset(token)
    return diagnostics


def execute_skill_learning_host_action(agent: object, action: object) -> object:
    """Execute skill learning host action guarding against store errors."""
    try:
        if callable(action):
            return action(agent)
        return None
    except (InterruptedError, KeyboardInterrupt):
        raise
    except (SkillLearningStoreError, OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("execute_skill_learning_host_action error: %s", exc, exc_info=True)
        return None


def record_user_skill_edit_after_tool(agent: object, path: str | Path | None) -> None:
    """Record user edit on learned skill file after tool execution, guarding against crashes."""
    if not path or not getattr(getattr(agent, "config", None), "enable_self_learning", False):
        return
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return
    try:
        store = SkillLearningStore.for_home(home_paths)
        fn = getattr(store, "record_user_edit_if_tracked", None)
        if callable(fn):
            fn(path)
    except (InterruptedError, KeyboardInterrupt):
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("failed to record user skill edit: %s", exc, exc_info=True)


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
