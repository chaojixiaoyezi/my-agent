"""脚本演进的发布、草稿与旁路教训；机器决定只依赖结构化证据。"""
from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..assets import _verify_directory_beneath, safe_asset_id
from ..assets.mutation_ledger import evolution_value_hash, query_analysis_blocked
from ..assets.revision_history import (
    asset_fingerprint,
    blob_get,
    blob_put,
    find_version,
    get_version,
    materialize_script,
    record_compat_edge,
)
from ..assets.script_store import ScriptStore
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import read_bytes_beneath, write_text_atomic_beneath
from .asset_evolution_replay import (
    evolution_descriptor,
    evolution_environment_error,
    merge_evolution_candidate,
    replay_evolution_candidate,
)
from .skill_learning_promotion import _commit


# LLM: 所有路径段来自宿主固定集合或safe_asset_id；原JSON writer保留nofollow与原子替换。
# 函数用途: 读取有界演进记录，不创建目录。
def read_evolution_json(home, parts):
    raw = read_bytes_beneath(Path(home), tuple(parts), max_bytes=4194304)
    value = json.loads(raw) if raw is not None else None
    if value is not None and not isinstance(value, dict):
        raise ValueError('evolution record must be an object')
    return value


# LLM: 调用者拥有领域锁；不保存回放输入或输出，只写JSON值。
# 函数用途: 原子保存一份演进记录。
def write_evolution_json(home, parts, value):
    write_text_atomic_beneath(Path(home), tuple(parts), json.dumps(value, ensure_ascii=False,
                                                                sort_keys=True, allow_nan=False) + '\n')


# LLM: 候选以精确代码hash和base编号去重，正文留blob；通知属于volatile出口，不写Tier1。
# 函数用途: 保存可人工处理的补丁提案。
def save_evolution_proposal(home, script_id, code, *, base_version=None, replay=None,
                            reason='', candidate=None, commit=None):
    safe_asset_id(script_id)
    digest = hashlib.sha256(code.encode()).hexdigest()
    identity = hashlib.sha256(json.dumps([script_id, digest, base_version], sort_keys=True).encode()).hexdigest()[:24]
    parts = ('data', 'asset_evolution', 'candidates', 'script', script_id, identity + '.json')
    # LLM: 本次同步回调才有写权；目录与记录沿原来源短门提交。
    # 函数用途: 去重创建提案，不把第二次后台重试变成新候选。
    def save():
        parent = _verify_directory_beneath(Path(home), parts[:-1], create=True)
        with locked_json_path(parent / parts[-1]):
            old = read_evolution_json(home, parts)
            if old is not None:
                return old
            sha = blob_put(home, code.encode())
            value = {'schema_version': 'asset_evolution_candidate.v1', 'candidate_id': identity,
                     'kind': 'script', 'asset_id': script_id, 'code_sha256': sha,
                     'base_version': base_version, 'status': 'proposal', 'reason': reason,
                     'replay': replay or {}, 'created_at': datetime.now(timezone.utc).isoformat(),
                     'overrides': {key: candidate[key] for key in ('summary', 'io_contract', 'entrypoint', 'interpreter', 'timeout_seconds')
                                   if candidate and key in candidate}}
            write_evolution_json(home, parts, value)
            return value
    return _commit(commit, save)


# LLM: 回放在锁外，最终store CAS在原短写门；冲突最多重新读取及回放一次，不静默覆盖新base。
# 函数用途: 处理已有脚本的补丁晋升或保留提案。
def route_existing_script(home, script_id, code, *, maintenance_mode='observe', candidate=None,
                           replay=None, commit=None, evidence_ref=''):
    store = ScriptStore(home)
    candidate = dict(candidate or {})
    gate = replay_evolution_candidate if replay is None else replay
    for attempt in range(2):
        old = store.get_script(script_id)
        if old is None or old.get('source_scope', 'owner') != 'owner':
            return {'status': 'skipped', 'reason': 'owner_script_missing'}
        descriptor, options = merge_evolution_candidate(script_id, candidate.get('summary', old['summary']), candidate, old)
        base = find_version(home, 'script', script_id, code_sha256=hashlib.sha256(old['code'].encode()).hexdigest(),
                            execution_definition_ref=old['execution_definition_ref'])
        verdict = gate(home, script_id, code, descriptor, options=options)
        if verdict['verdict'] != 'passed' or maintenance_mode != 'autonomous' or base is None:
            reason = ('history_unavailable' if base is None else
                      'observe_mode' if maintenance_mode != 'autonomous' else verdict['verdict'])
            proposal = save_evolution_proposal(home, script_id, code, base_version=base['version'] if base else None,
                                               replay=verdict, reason=reason, candidate=candidate, commit=commit)
            return {'status': 'proposal', 'asset_id': script_id, 'candidate_id': proposal['candidate_id'], 'replay': verdict}
        new_payload = descriptor.to_dict() | options
        old_payload = evolution_descriptor(old).to_dict() | {key: old[key] for key in options}
        if asset_fingerprint(payload=new_payload, code=code) == asset_fingerprint(payload=old_payload, code=old['code']):
            return {'status': 'unchanged', 'asset_id': script_id, 'replay': verdict}
        try:
            submitted = _commit(commit, lambda: store.save_script(descriptor, code, expected_revision=old['revision'], **options))
        except ValueError:
            current = store.get_script(script_id)
            if attempt == 0 and current is not None and current['revision'] != old['revision']:
                continue
            proposal = save_evolution_proposal(home, script_id, code, base_version=base['version'],
                                               replay=verdict, reason='cas_conflict', candidate=candidate, commit=commit)
            return {'status': 'proposal', 'asset_id': script_id, 'candidate_id': proposal['candidate_id'], 'reason': 'cas_conflict'}
        # W4的唯一store hook负责版本；W7不导入它、不另建平行版本写者。
        new = find_version(home, 'script', script_id, code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                           execution_definition_ref=submitted['execution_definition_ref'])
        if new is None or new['storage_revision'] != submitted['revision']:
            return {'status': 'history_pending', 'asset_id': script_id, 'revision': submitted['revision']}
        _commit(commit, lambda: record_compat_edge(home, 'script', script_id, from_version=base['version'],
                    to_version=new['version'], verdict='replay_passed', replayed=verdict['replayed'], evidence_ref=evidence_ref))
        trial = {'schema_version': 'asset_evolution_trial.v1', 'asset_id': script_id,
                 'base_version': base['version'], 'promoted_version': new['version'],
                 'promoted_definition_ref': new['execution_definition_ref'],
                 'promoted_revision': submitted['revision'],
                 'promoted_sha256': new['code_sha256'], 'active': True, 'failures': 0, 'seen_calls': []}
        _commit(commit, lambda: write_evolution_json(home, ('data', 'asset_evolution', 'trials', script_id + '.json'), trial))
        return {'status': 'promoted', 'asset_id': script_id, 'version': new['version'], 'revision': submitted['revision'], 'replay': verdict}
    raise AssertionError('bounded CAS loop exhausted')


# LLM: 简短回执按SHA及可选版本匹配；冻结回执仍须完整来源/定义证明，当前发布修订与最终Store CAS不放宽。
# 函数用途: 校验两种回执后累计非环境失败，通过store恢复版本并记失败兼容边。
def observe_evolution_trial(home, script_id, execution, *, commit=None, failure_threshold=2):
    safe_asset_id(script_id)
    parts = ('data', 'asset_evolution', 'trials', script_id + '.json')
    parent = Path(home).joinpath(*parts[:-1])
    if not parent.exists():
        return None
    # LLM: 同资产trial状态在原短门内锁定；回滚只读取历史blob，不等待模型。
    # 函数用途: 幂等更新试用计数及必要的恢复提交。
    def update():
        with locked_json_path(parent / parts[-1]):
            trial = read_evolution_json(home, parts)
            call_id = execution.get('call_id')
            if (not trial or not trial.get('active') or not call_id or call_id in trial.get('seen_calls', [])
                    or query_analysis_blocked(execution)
                    or execution.get('sha256') != trial.get('promoted_sha256')
                    or not trial.get('promoted_definition_ref')
                    or any(execution.get(key) not in (None, trial['promoted_version'])
                           for key in ('version', 'requested_version', 'execution_version'))):
                return None
            # 冻结证明只要出现就必须完整有效，不能把部分或明确未验证的证据降为简短SHA回执。
            capture_fields = ('schema_version', 'verified', 'source_scope', 'execution_definition_ref',
                              'published_definition_ref', 'requested_version', 'execution_version')
            if (any(key in execution for key in capture_fields)
                    and (execution.get('verified') is not True or execution.get('source_scope') != 'owner'
                         or execution.get('execution_definition_ref') != trial['promoted_definition_ref']
                         or execution.get('published_definition_ref') != trial['promoted_definition_ref'])):
                return None
            store = ScriptStore(home)
            current = store.get_script(script_id)
            if (current is None or current.get('source_scope', 'owner') != 'owner'
                    or current['revision'] != trial.get('promoted_revision')
                    or current['execution_definition_ref'] != trial['promoted_definition_ref']
                    or hashlib.sha256(current['code'].encode()).hexdigest() != trial['promoted_sha256']):
                trial['active'] = False
                write_evolution_json(home, parts, trial)
                return {'status': 'superseded'}
            trial['seen_calls'] = [*trial.get('seen_calls', []), call_id][-64:]
            failed = execution.get('ok') is False and not evolution_environment_error(execution.get('error_code'))
            trial['failures'] = trial.get('failures', 0) + 1 if failed else 0
            if trial['failures'] < failure_threshold:
                write_evolution_json(home, parts, trial)
                return {'status': 'trial', 'failures': trial['failures']}
            base = get_version(home, 'script', script_id, trial['base_version'])
            if base is None:
                return {'status': 'inconclusive', 'reason': 'restore_version_missing'}
            from .asset_promotion import _stage_dir
            with _stage_dir(Path(home)) as stage:
                folder = materialize_script(home, base, stage)
                payload = json.loads(blob_get(home, base['payload_blob']))
                descriptor = evolution_descriptor(payload)
                options = {key: payload.get(key, current[key]) for key in ('entrypoint', 'interpreter', 'timeout_seconds')}
                raw = read_bytes_beneath(stage, (*folder.relative_to(stage).parts, options['entrypoint']), max_bytes=262144)
                saved = store.save_script(descriptor, raw.decode(), expected_revision=current['revision'], **options)
            restored = find_version(home, 'script', script_id, code_sha256=hashlib.sha256(saved['code'].encode()).hexdigest())
            if restored and restored['version'] > trial['promoted_version']:
                record_compat_edge(home, 'script', script_id, from_version=trial['promoted_version'],
                                   to_version=restored['version'], verdict='replay_failed', evidence_ref=call_id)
            trial.update(active=False, restored_from=trial['base_version'], restore_version=restored['version'] if restored else None)
            write_evolution_json(home, parts, trial)
            return {'status': 'rolled_back', 'asset_id': script_id, 'restored_from': trial['base_version']}
    return _commit(commit, update)


# LLM: 仅替换main(inputs)作用域中完整字符串常量；模块常量、docstring及局部遮蔽inputs的函数不动。
# 函数用途: 将已验证的会话字面量确定性提升为带原默认值的输入。
def lift_evolution_literals(code, literals):
    import re

    tree = ast.parse(code)
    main = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main'
                 and node.args.args and node.args.args[0].arg == 'inputs'), None)
    if main is None:
        return code, {}
    values = sorted({value for value in literals if isinstance(value, str)})
    names = {}
    for value in values:
        kind = ('time' if re.fullmatch(r'\d\d:\d\d', value) else
                'monitor_alias' if re.fullmatch(r'job[\w-]+', value) else
                'date' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else
                'ip' if re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', value) else 'file_name')
        names[value] = kind + '_' + hashlib.sha256(value.encode()).hexdigest()[:6]
    lifted = {}

    # LLM: AST位置由解析器确定，拒绝修改嵌套函数或类的另一个词法作用域。
    # 类用途: 替换主函数内的完整字面量而非子串。
    class Lift(ast.NodeTransformer):
        # LLM: 原函数签名不变；嵌套定义整个保留。
        # 函数用途: 仅访问当前main函数的语句。
        def visit_FunctionDef(self, node):
            return self.generic_visit(node) if node is main else node

        # LLM: 类体和lambda的inputs绑定未知，不能隐式捕获调用参数。
        # 函数用途: 保留嵌套类。
        def visit_ClassDef(self, node):
            return node

        # LLM: lambda可能遮蔽inputs，保守不提升其中的字面量。
        # 函数用途: 保留lambda局部作用域。
        def visit_Lambda(self, node):
            return node

        # LLM: default仍是原常量，除调用方明确提供参数外行为不变。
        # 函数用途: 用inputs.get替换完整常量。
        def visit_Constant(self, node):
            if type(node.value) is str and node.value in names:
                key = names[node.value]
                lifted[key] = node.value
                return ast.copy_location(ast.Call(ast.Attribute(ast.Name('inputs', ast.Load()), 'get', ast.Load()),
                                                   [ast.Constant(key), ast.Constant(node.value)], []), node)
            return node
    Lift().visit(main)
    ast.fix_missing_locations(tree)
    return (ast.unparse(tree) + '\n', lifted) if lifted else (code, {})


# LLM: 不执行网络草稿；无准确原输入/输出证明则不生成草稿，等价失败也不能晋升。
# 函数用途: 验证字面量提升后的行为并保存原提取库DRAFT。
def route_new_script(home, request, target, code, family, *, runner_factory=None, commit=None):
    from ..assets.descriptor import access_target_requires_network
    from ..assets.turn_extractor import _script_spec, save_evolution_draft
    from ..workflows import ScriptRunner
    from .asset_promotion import _stage_dir

    execution = target.get('execution', {})
    lifted_code, defaults = lift_evolution_literals(code, target.get('literals', []))
    network = any(access_target_requires_network(item) for item in target.get('access_targets', []))
    original_inputs = original_evolution_inputs(home, request, execution)
    if original_inputs is None or not execution.get('output_hash'):
        return {'status': 'skipped', 'reason': 'original_snapshot_unavailable'}
    stem = Path(target.get('path') or target.get('asset_id') or 'script').stem
    import re
    name = re.sub(r'[^A-Za-z0-9_-]', '-', stem).strip('-')[:64] or 'script'
    name = name + '-' + family[:8]
    doc = ast.get_docstring(ast.parse(code)) or stem
    spec = _script_spec(name, name, lifted_code,
                        {'type': 'object', 'properties': {key: {'type': 'string', 'default': value} for key, value in defaults.items()}},
                        {'type': 'object', 'properties': {key: {} for key in target.get('io_contract', {}).get('outputs', [])}})
    spec['descriptor']['summary'] = doc.splitlines()[0][:160]
    spec['descriptor']['metadata'] = {'member_of': request.get('member_of', [])}
    spec['descriptor']['access_targets'] = target.get('access_targets', [])
    if not network:
        factory = ScriptRunner if runner_factory is None else runner_factory
        with _stage_dir(Path(home)) as stage:
            ScriptStore(stage).save_script(evolution_descriptor(spec['descriptor']), spec['entry_source'])
            result = factory(stage, use_sandbox=True, restrict_to_roots=True).run_script(name, original_inputs)
            if not result.ok or evolution_value_hash(result.output_data) != execution['output_hash']:
                return {'status': 'skipped', 'reason': 'equivalence_mismatch'}
    proposal = _commit(commit, lambda: save_evolution_draft(home, family, spec,
                        run_id=request['run_id'], thread_id=request.get('thread_id', ''),
                        created_at=request['created_at'], member_of=request.get('member_of', [])))
    return {'status': 'draft', 'asset_id': name, 'proposal_id': proposal['proposal_id'],
            'independent_reuse': proposal.get('independent_reuse', False), 'network': network}


# LLM: 原输入不复制到演进请求；只按同Owner/thread准确call_id从原会话取回，并复核输入hash。
# 函数用途: 在有界消息窗口中取得原执行输入，缺失时拒绝等价检查。
def original_evolution_inputs(home, request, execution, *, message_limit=32):
    if execution.get('input_hash') == evolution_value_hash({}):
        return {}
    if not request.get('thread_id'):
        return None
    from ..assets.turn_extractor import _tool_invocations
    from ..conversation.store import ConversationStore

    store = ConversationStore(Path(home) / 'conversations')
    rows, errors = store.messages.recent_report(request['thread_id'], limit=message_limit)
    if errors:
        return None
    for call in _tool_invocations(rows, home):
        inputs = call['arguments'].get('inputs')
        if (call['call_id'] == execution.get('call_id') and isinstance(inputs, dict)
                and evolution_value_hash(inputs) == execution.get('input_hash')):
            return inputs
    return None
