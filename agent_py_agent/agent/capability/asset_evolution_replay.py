"""确定性依赖回放闸；原始输入仅在临时stage使用，返回结果不含快照正文。"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from ..assets.descriptor import (
    AssetDescriptor,
    access_target_requires_network,
    output_contract_fields,
)
from ..assets.mutation_ledger import evolution_value_hash, query_analysis_blocked
from ..assets.script_store import ScriptStore
from ..assets.workflow_store import WorkflowStore
from ..workflows import ScriptRunner
from ..workflows.run_store import WorkflowRunStore


# LLM: 只读取正式描述符字段；执行选项与展示投影不进入严格schema。
# 函数用途: 从完整脚本记录恢复无损描述符。
def evolution_descriptor(record):
    keys = {item.name for item in fields(AssetDescriptor)}
    return AssetDescriptor.from_dict({key: value for key, value in record.items() if key in keys})


# LLM: 候选只能覆盖五个约定字段，全部访问/凭据/来源/启用/metadata沿现有记录保留。
# 函数用途: 合并脚本候选并返回正式描述符和执行选项。
def merge_evolution_candidate(name, summary, candidate, existing=None):
    descriptor = evolution_descriptor(existing).to_dict() if existing else {
        'id': name, 'kind': 'script', 'name': name, 'summary': summary,
        'io_contract': candidate.get('io_contract', {})}
    if existing:
        descriptor['summary'] = candidate.get('summary', summary)
        if 'io_contract' in candidate:
            descriptor['io_contract'] = candidate['io_contract']
    options = {key: candidate.get(key, (existing or {}).get(key, default)) for key, default in
               (('entrypoint', 'main.py'), ('interpreter', 'python3'), ('timeout_seconds', 120))}
    return AssetDescriptor.from_dict(descriptor), options


# LLM: 仅扫描有界最近运行窗口，按真实脚本ID和成功状态选最多K份快照；不读其他Owner。
# 函数用途: 得到依赖工作流最近成功的输入输出对。
def dependent_replay_samples(owner_home, script_id, *, limit=10, run_window=200):
    workflows = WorkflowStore(owner_home).list_workflows()
    dependent_ids = {row.descriptor.id for row in workflows if any(step.script_id == script_id for step in row.steps)}
    if not dependent_ids:
        return [], False
    runs = WorkflowRunStore(owner_home).list_runs(limit=run_window)
    samples = [step for run in runs if run.workflow_id in dependent_ids for step in run.steps
               if step.script_id == script_id and step.status == 'succeeded'
               and not query_analysis_blocked(step.escalation_result)]
    samples.sort(key=lambda step: step.ended_at, reverse=True)
    return samples[:limit], True


# LLM: 布尔不是整数，合同值类型按JSON语义校验；未知类型已由描述符硬门拒绝。
# 函数用途: 对网络脚本只比较输出合同，不签等价证明。
def evolution_contract_matches(output, contract):
    required, nullable, types = output_contract_fields(contract)
    if not isinstance(output, dict) or any(key not in output for key in required):
        return False
    predicates = {'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
                  'number': lambda v: type(v) in (int, float), 'boolean': lambda v: type(v) is bool,
                  'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
                  'null': lambda v: v is None}
    return all(key not in output or output[key] is None and key in nullable
               or predicates[kind](output[key]) for key, kind in types.items())


# LLM: 仅在_stage_dir执行，网络依赖永不返回passed；所有返回只含结论、计数及哈希。
# 函数用途: 回放最多十份依赖成功样本并比较完整输出。
def replay_evolution_candidate(owner_home, script_id, code, descriptor, *, options=None,
                               samples=None, has_dependents=None, runner_factory=None, limit=10):
    from .asset_promotion import _stage_dir

    if samples is None:
        samples, has_dependents = dependent_replay_samples(owner_home, script_id, limit=limit)
    selected = list(samples)[:limit]
    network = any(access_target_requires_network(target) for target in descriptor.access_targets)
    if network:
        valid = all(evolution_contract_matches(step.output_snapshot, descriptor.io_contract.to_dict()) for step in selected)
        return {'verdict': 'inconclusive' if valid else 'failed', 'replayed': 0,
                'reason': 'network_contract_only', 'hashes': []}
    if not selected:
        return {'verdict': 'inconclusive' if has_dependents else 'passed', 'replayed': 0,
                'reason': 'no_successful_snapshots' if has_dependents else 'no_dependents', 'hashes': []}
    hashes = []
    factory = ScriptRunner if runner_factory is None else runner_factory
    try:
        with _stage_dir(Path(owner_home)) as staging:
            # disabled沿正式记录保存，stage只验证代码，不改正式启用开关。
            ScriptStore(staging).save_script(replace(descriptor, enabled=True), code, **(options or {}))
            runner = factory(staging, use_sandbox=True, restrict_to_roots=True)
            for step in selected:
                result = runner.run_script(script_id, step.input_snapshot)
                output = getattr(result, 'output_data', None)
                if output is None:
                    output = getattr(result, 'parsed_output', None)
                hashes.append({'input': evolution_value_hash(step.input_snapshot),
                               'expected': evolution_value_hash(step.output_snapshot),
                               'actual': evolution_value_hash(output)})
                if not result.ok:
                    code_value = getattr(result, 'error_code', '')
                    return {'verdict': 'inconclusive' if evolution_environment_error(code_value) else 'failed',
                            'replayed': len(hashes), 'hashes': hashes, 'reason': code_value}
                if (not evolution_contract_matches(output, descriptor.io_contract.to_dict())
                        or hashes[-1]['actual'] != hashes[-1]['expected']):
                    return {'verdict': 'failed', 'replayed': len(hashes), 'hashes': hashes, 'reason': 'output_changed'}
    except (OSError, ValueError, RuntimeError):
        return {'verdict': 'inconclusive', 'replayed': len(hashes), 'hashes': hashes, 'reason': 'staging_unavailable'}
    return {'verdict': 'passed', 'replayed': len(hashes), 'hashes': hashes}


# LLM: 只用结构化错误码判环境故障，不根据错误正文或模型描述猜测。
# 函数用途: 排除不应触发回滚的执行失败。
def evolution_environment_error(code):
    value = str(code or '').upper()
    return (value.startswith(('DATASOURCE_', 'CREDENTIAL_', 'SANDBOX_', 'NETWORK_', 'AUTH_', 'HTTP_'))
            or any(part in value for part in ('TIMEOUT', 'TIMED_OUT', 'SANDBOX_UNAVAILABLE', 'NETWORK', 'CREDENTIAL'))
            or value in {'ECONNRESET', 'ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH', 'ETIMEDOUT', 'DNS_FAILED'})
