"""后台确定性演进服务；分类与路由不调用模型，不以模糊记忆签发发布许可。"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from ..assets import safe_asset_id
from ..assets.pitfalls import add_known_pitfall
from ..assets.revision_history import blob_get
from ..assets.script_store import ScriptStore
from ..common.json_io import locked_json_path
from .asset_evolution_routes import (
    observe_evolution_trial,
    read_evolution_json,
    route_existing_script,
    route_new_script,
    write_evolution_json,
)
from .skill_learning_promotion import _commit


# LLM: 常量值只参与参数化族比较；IO字段名参与指纹，族相同本身不代表兼容或授权。
# 函数用途: 生成替换字符串和数值字面量后的AST族指纹。
def evolution_family_fingerprint(code, io_contract=None):
    import json

    # LLM: 保留AST结构与常量类型，避免字符串、数值、布尔混淆。
    # 类用途: 将可变化常量替换为固定占位。
    class Normalize(ast.NodeTransformer):
        # LLM: bool不是参数数值，其语义不随字面量归一而消失。
        # 函数用途: 归一字符串和数值节点。
        def visit_Constant(self, node):
            if type(node.value) is str:
                return ast.copy_location(ast.Constant('<string>'), node)
            if type(node.value) in (int, float, complex):
                return ast.copy_location(ast.Constant(0), node)
            return node
    tree = Normalize().visit(ast.parse(code))
    contract = io_contract if isinstance(io_contract, dict) else {}
    fields = {key: sorted(contract.get(key, [])) for key in ('inputs', 'outputs')}
    fields['output_types'] = sorted(contract.get('output_types', {}))
    text = json.dumps([ast.dump(tree, include_attributes=False), fields], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


# LLM: bug_fix要求先失败、随后变更、最后同sha成功；specialized只认新增完整常量与回合字面量交集。
# 函数用途: 用有序事实确定分类，返回完整验证投影。
def classify_evolution_target(target, code, *, previous_code=''):
    facts = target.get('facts', [])
    final = target.get('final_sha256')
    mutation_at = max((i for i, row in enumerate(facts) if row.get('schema_version') == 'asset_mutation.v1'), default=-1)
    final_execution = next((row for row in reversed(facts[mutation_at + 1:])
                            if row.get('schema_version') == 'asset_execution.v1' and row.get('sha256') == final), None)
    verified = (target.get('verified') is True and final_execution is not None
                and final_execution.get('ok') is True
                and hashlib.sha256(code.encode()).hexdigest() == final)
    failures = [(i, row) for i, row in enumerate(facts[:mutation_at]) if row.get('schema_version') == 'asset_execution.v1'
                and row.get('ok') is False and row.get('sha256') != final]
    bug_fix = verified and bool(failures) and final_execution is not None
    strings = {node.value for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Constant) and type(node.value) is str}
    previous = {node.value for node in ast.walk(ast.parse(previous_code)) if isinstance(node, ast.Constant) and type(node.value) is str}
    previous_hashes = {hashlib.sha256(value.encode()).hexdigest() for value in previous}
    prior = next((row for row in reversed(facts[:mutation_at]) if 'literal_hashes' in row), {})
    previous_hashes.update(prior.get('literal_hashes', []))
    specialized = verified and any(hashlib.sha256(value.encode()).hexdigest() not in previous_hashes
                                   for value in strings & set(target.get('literals', [])))
    return {'verified': verified, 'bug_fix': bug_fix, 'specialized': specialized,
            'classification': 'bug_fix' if bug_fix else 'specialized' if specialized else 'verified',
            'family_fingerprint': evolution_family_fingerprint(code, target.get('io_contract', {}))}


# LLM: 所有权来自正式store来源；以完整同族指纹匹配，不从名称或自然语言猜旧资产。
# 函数用途: 给新脚本查找同Owner的已登记同族方法。
def find_evolution_family(home, family):
    store = ScriptStore(home)
    for row in store.list_scripts():
        if row.get('source_scope', 'owner') != 'owner':
            continue
        record = store.get_script(row['id'])
        try:
            if record and evolution_family_fingerprint(record['code'], record.get('io_contract', {})) == family:
                return record['id']
        except (SyntaxError, TypeError, ValueError):
            continue
    return None


# LLM: 仅有结构化失败修复成功链时创建一次候选；正式lesson仍经原审核，pitfall只写W1 sidecar。
# 函数用途: 附带精确资产引用登记辅助教训及常见问题。
def route_evolution_lesson(home, request, target, *, asset_id=None, lesson_sink=None, commit=None):
    from ..memory_store.candidate_models import CandidateObservation, MemoryScope
    from ..memory_store.candidates import CandidateService

    if request.get('evidence_tags') or not target.get('bug_fix'):
        return None
    identifier = asset_id or target.get('asset_id')
    if not identifier:
        return None
    safe_asset_id(identifier)
    refs = ['script:owner:' + identifier]
    record = ScriptStore(home).get_script(identifier)
    if record:
        refs.extend('data_source:' + item['system'] for item in record.get('data_sources', []) if item.get('system'))
    failed = next(row for row in reversed(target['facts']) if row.get('schema_version') == 'asset_execution.v1' and row.get('ok') is False)
    success = target['execution']
    text = f"脚本 {identifier} 的 {failed.get('error_code') or 'EXECUTION_FAILED'} 已经变更后同字节执行核对；重用前复核输入合同与依赖回放。"
    evidence = {'call_id': success['call_id'], 'asset_refs': refs[:8], 'evolution_sequence': 'fail_fix_success',
                'failure_sha256': failed['sha256'], 'success_sha256': target['final_sha256'],
                'source_ref': 'data/asset_evolution/requests/' + request['run_id'] + '.json'}
    observation = CandidateObservation(candidate_type='lesson', content=text,
        subject_key='asset-evolution:' + identifier, scope=MemoryScope('personal', 'personal'),
        origin='tool_verified', evidence_refs=(evidence,), source_tool_refs=(evidence,),
        source_run_ids=(request['run_id'],), observed_at=request['created_at'],
        promotion_target='lesson', observation_id='evolution:' + request['run_id'] + ':' + identifier,
        asset_refs=tuple(refs[:8]))
    # LLM: CandidateService仍是唯一候选仓库；同observation_id重试不增发生次数。
    # 函数用途: 在原短写门登记候选和sidecar。
    def write():
        sink = lesson_sink if lesson_sink is not None else CandidateService(Path(home) / 'memory' / 'candidates.jsonl').observe
        candidate = sink(observation)
        add_known_pitfall(home, 'script', identifier, text=text, evidence_ref=success['call_id'])
        return {'status': 'lesson_candidate', 'asset_refs': refs[:8],
                'candidate_id': getattr(candidate, 'candidate_id', '')}
    return _commit(commit, write)


# LLM: 每个run请求锁内幂等消费，所有正式写单独经commit；不持模型锁，也不调用模型。
# 类用途: 连接确定性分类、三条路线和回滚。
class AssetEvolutionService:
    # LLM: 只绑定宿主Owner与现有维护模式，构造不读写文件。
    # 函数用途: 准备后台演进服务。
    def __init__(self, owner_home, *, maintenance_mode='observe', commit=None, lesson_sink=None):
        self.home = Path(owner_home)
        self.maintenance_mode = maintenance_mode
        self.commit = commit
        self.lesson_sink = lesson_sink

    # LLM: 请求只按准确run_id读取；unknown schema、query_failed或已处理请求都不能生成新资产。
    # 函数用途: 消费一个已冻结演进请求并返回volatile通知数据。
    def process(self, run_id):
        safe_asset_id(run_id)
        parts = ('data', 'asset_evolution', 'requests', run_id + '.json')
        request = read_evolution_json(self.home, parts)
        if request is None or request.get('schema_version') != 'asset_evolution_request.v1' or request.get('run_id') != run_id:
            return {'status': 'absent', 'notices': []}
        with locked_json_path(self.home.joinpath(*parts)):
            request = read_evolution_json(self.home, parts)
            if request.get('result'):
                return {**request['result'], 'duplicate': True, 'notices': []}
            if request.get('evidence_tags'):
                result = {'status': 'query_failed', 'notices': [], 'routes': []}
            else:
                result = self._route_request(request)
            _commit(self.commit, lambda: write_evolution_json(self.home, parts, {**request, 'result': result}))
            return result

    # LLM: 每个目标先核blob身份再分类；网络草稿不自动发布，未验证目标不进入任何路线。
    # 函数用途: 路由本轮有界目标与试用执行观察。
    def _route_request(self, request):
        routes, notices = [], []
        for row in request.get('executions', [])[:2048]:
            result = observe_evolution_trial(self.home, row['asset_id'], row['execution'], commit=self.commit)
            if result:
                routes.append(result)
        for target in request.get('targets', [])[:32]:
            if target.get('verified') is not True:
                continue
            raw = blob_get(self.home, target.get('final_sha256'))
            if raw is None:
                continue
            code = raw.decode('utf-8')
            try:
                classification = classify_evolution_target(target, code)
            except (SyntaxError, ValueError, RecursionError):
                continue
            target.update(classification)
            if not classification['verified']:
                continue
            family = classification['family_fingerprint']
            identifier = target.get('asset_id') if target.get('existing') else None
            identifier = identifier or find_evolution_family(self.home, family)
            if identifier:
                result = route_existing_script(self.home, identifier, code,
                    maintenance_mode=self.maintenance_mode, candidate={'io_contract': target.get('io_contract', {})},
                    commit=self.commit, evidence_ref=request['run_id'])
            else:
                result = route_new_script(self.home, request, target, code, family, commit=self.commit)
                if (result.get('status') == 'draft' and result.get('independent_reuse') is True
                        and not result.get('network') and self.maintenance_mode == 'autonomous'):
                    result = self._promote_reused_draft(result)
            routes.append(result)
            lesson = route_evolution_lesson(self.home, request, target, asset_id=identifier or result.get('asset_id'),
                                             lesson_sink=self.lesson_sink, commit=self.commit)
            if lesson:
                routes.append(lesson)
        for row in routes:
            if row.get('status') in ('proposal', 'draft', 'rolled_back', 'promoted', 'history_pending'):
                notices.append({'thread_id': request.get('thread_id', ''), 'status': row['status'],
                                'asset_id': row.get('asset_id', ''), 'run_id': request['run_id']})
        return {'status': 'processed', 'routes': routes, 'notices': notices}

    # LLM: 第二独立来源只触发现有正式发布器，不能跳过沙箱验证/CAS，也不能覆盖同名已有资产。
    # 函数用途: 对重复独立采用且等价通过的草稿尝试自动发布。
    def _promote_reused_draft(self, result):
        from ..assets.turn_extractor import get_extraction_proposal
        from .asset_promotion import _promote_script_asset

        proposal = get_extraction_proposal(self.home, result['proposal_id'])
        spec = proposal['candidate_scripts'][0]
        if ScriptStore(self.home).get_script(spec['script_id']) is not None:
            return result
        binding = _promote_script_asset(self.home, spec['script_id'], spec['descriptor']['summary'],
            {'entry_source': spec['entry_source'], 'io_contract': spec['descriptor']['io_contract'],
             'descriptor': spec['descriptor']}, commit=self.commit)
        return {**result, 'status': 'promoted'} if binding else result


# LLM: 只经现有host notices及IM出口展示，不写prompt稳定目录；缺会话可继续记录IM投影。
# 函数用途: 发布已处理演进结果的短通知。
def emit_evolution_notices(agent, notices):
    from ..conversation.host_notices import host_notice, queue_host_notice
    from ..workflows.datasource_runtime import emit_asset_im_notification

    home = agent.home_paths.owner_home_dir
    for notice in notices[:32]:
        text = f"脚本演进：{notice['asset_id']} · {notice['status']}"
        if notice.get('thread_id'):
            queue_host_notice(agent.conversation_store, notice['thread_id'],
                              host_notice('asset_evolution', notice['status'], text), replace_same_code=True)
        emit_asset_im_notification(home, event_type='asset_evolution', title='脚本演进', message=text, payload=notice)
