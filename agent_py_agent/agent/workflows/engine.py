# LLM: 公开引擎仅使用宿主注入的模型回调；运行事实和恢复位置来自账本，不从自然语言推断。
# 模块用途: 编排纯脚本、条件跳过、技能分析、异常兜底与 Step 级断点续跑。
from __future__ import annotations

import inspect
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ..assets.workflow_store import WorkflowStore
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import read_text_beneath
from .models import (
    WORKFLOW_ON_ERROR_POLICIES,
    StepExecutionRecord,
    WorkflowRunRecord,
    output_value,
    payload,
    safe_path,
    skill_sequence,
)
from .run_store import WorkflowRunStore
from .script_runner import ScriptRunner, load_script, parse_script_output

SUCCESS = ('succeeded', 'escalated_recovered', 'skipped')


# LLM: 显式支持同形 JSON 与可序列化资产 dataclass，不依赖并行资产模块的导入时序。
# 函数用途: 加载并校验工作流编号、模式和无重复的步骤定义。
def _definition(home, value):
    if isinstance(value, str):
        safe_path(home, value)
        asset = WorkflowStore(home).get_workflow(value)
        value = asset.to_dict() if asset is not None else json.loads(read_text_beneath(home, ('workflows', value, 'workflow.json')) or '{}')
    value = payload(value)
    descriptor = value.get('descriptor', value)
    safe_path(home, descriptor['id'])
    ids = []
    for step in value['steps']:
        if step.get('on_error', 'escalate_to_agent') not in WORKFLOW_ON_ERROR_POLICIES:
            raise ValueError(f"Unsupported on_error policy: {step['on_error']}")
        safe_path(home, step['step_id'] + '.json')
        if step['step_id'] in ids or step['mode'] not in ('deterministic_script', 'llm_skill_analysis', 'hybrid'):
            raise ValueError('步骤编号重复或模式无效')
        ids.append(step['step_id'])
    return value, descriptor


# LLM: 新运行和恢复后失效的后缀都创建新记录，不继承旧后缀输出。
# 函数用途: 依据定义初始化一个待执行步骤。
def _pending(spec):
    return StepExecutionRecord(spec['step_id'], spec.get('name', spec['step_id']), spec['mode'],
                               'pending', 0, 0, 0, False, script_id=spec.get('script_id', ''))


# LLM: 执行前须由宿主核验 Owner 与权限；回调只提供判断数据，不得自动应用脚本修复。
# 类用途: 运行工作流并在失败或中断时保留已成功步骤。
class WorkflowEngine:
    # LLM: 构造不执行资产、不调用模型；每个实例绑定宿主选定的Owner。
    # 函数用途: 注入可选兜底、分析、技能匹配回调与本地存储。
    def __init__(self, owner_home, *, escalation_handler=None, analysis_handler=None, skill_resolver=None):
        self.owner_home = Path(owner_home).absolute()
        self.store, self.runner = WorkflowRunStore(owner_home), ScriptRunner(owner_home)
        self.escalation_handler, self.analysis_handler = escalation_handler, analysis_handler
        self.skill_resolver = skill_resolver

    # LLM: 首次执行冻结工作流定义；owner_id 只用于一致性校验，不能建立授权。
    # 函数用途: 新建运行账本并顺序执行步骤。
    def run_workflow(self, workflow, inputs=None, trigger_kind='manual', owner_id=''):
        definition, descriptor = _definition(self.owner_home, workflow)
        if owner_id and descriptor.get('owner_id') and owner_id != descriptor['owner_id']:
            raise ValueError('工作流 Owner 不匹配')
        record = WorkflowRunRecord(uuid.uuid4().hex, descriptor['id'], owner_id or descriptor.get('owner_id', ''),
                                   trigger_kind, 'running', time.time(), 0, 0,
                                   steps=tuple(_pending(spec) for spec in definition['steps']),
                                   inputs=payload({} if inputs is None else inputs))
        self.store.create_run(record, definition)
        with self.store.execution_lock(record.workflow_run_id):
            return self._drive(record, definition, 0)

    # LLM: 仅复用冻结定义及成功前缀；显式回退会清空整个后缀，绝不混用旧输出。
    # 函数用途: 在独占执行锁内从失败步、未完成步或指定步骤续跑。
    def resume_workflow_run(self, workflow_run_id, from_step_id=None):
        with self.store.execution_lock(workflow_run_id):
            record = self.store.load_run(workflow_run_id)
            definition = self.store.load_workflow_snapshot(workflow_run_id)
            ids = [s.step_id for s in record.steps]
            if from_step_id is not None and from_step_id not in ids:
                raise ValueError('恢复步骤不存在')
            start = ids.index(from_step_id) if from_step_id is not None else next(
                (i for i, step in enumerate(record.steps) if step.status not in SUCCESS), len(ids))
            if any(step.status not in SUCCESS for step in record.steps[:start]):
                raise ValueError('不能跳过未成功的前序步骤')
            if start == len(ids) and record.status == 'succeeded':
                return record
            steps = record.steps[:start] + tuple(_pending(spec) for spec in definition['steps'][start:])
            record = replace(record, steps=steps, status='running', ended_at=0, failed_step_id='',
                             needs_repair=False, current_step_id='', final_output={}, resumed_from_step_id=ids[start] if start < len(ids) else '')
            self.store.save_run(record)
            return self._drive(record, definition, start)

    # LLM: 每步条件独立判断；暂停和失败停止后缀，跳过不影响无关步骤。
    # 函数用途: 调度后缀步骤，更新当前进度并结算整次运行。
    def _drive(self, record, definition, start):
        timer = time.monotonic()
        outputs = {s.step_id: payload(s.output_snapshot) for s in record.steps[:start] if s.status in SUCCESS and s.status != 'skipped'}
        for spec in definition['steps'][start:]:
            step = replace(_pending(spec), status='running', started_at=time.time(), input_snapshot=payload(record.inputs))
            record = self.store.save_step_result(record.workflow_run_id, step)
            clock = time.monotonic()
            try:
                condition = spec.get('condition')
                if condition and output_value(condition, outputs) != condition['equals']:
                    step.status = 'skipped'
                else:
                    self._perform(step, spec, outputs, definition, record)
            except (KeyboardInterrupt, SystemExit):
                step = replace(step, status='paused_for_resume', needs_repair=True, ended_at=time.time(),
                               duration_ms=(time.monotonic() - clock) * 1000)
                record = self.store.save_step_result(record.workflow_run_id, step)
                self.store.save_run(replace(record, status='paused_for_resume', needs_repair=True, failed_step_id=step.step_id,
                                            ended_at=time.time(), duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
                                            final_output=next(reversed(outputs.values()), {})))
                raise
            except Exception as exc:
                step.status, step.error_code, step.error_message = 'failed', step.error_code or 'STEP_EXECUTION_ERROR', str(exc)
                step.needs_repair = True
            step.ended_at, step.duration_ms = time.time(), (time.monotonic() - clock) * 1000
            record = self.store.save_step_result(record.workflow_run_id, step)
            if step.status in ('failed', 'paused_for_resume'):
                record = replace(record, status=step.status, needs_repair=True, failed_step_id=step.step_id)
                break
            if step.status != 'skipped':
                outputs[step.step_id] = payload(step.output_snapshot)
        else:
            record = replace(record, status='succeeded', needs_repair=False, failed_step_id='')
        record = replace(record, ended_at=time.time(), duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
                         final_output=next(reversed(outputs.values()), {}))
        self.store.save_run(record)
        return record

    # LLM: 映射只能引用结构化前序输出；传入回调的对象独立复制，不能污染已成功快照。
    # 函数用途: 运行一个脚本或技能步骤，混合步骤先采集再分析。
    def _perform(self, record, spec, outputs, definition, run):
        for key, reference in spec.get('inputs_map', {}).items():
            record.input_snapshot[key] = output_value(reference, outputs) if isinstance(reference, dict) and 'step_id' in reference else payload(reference)
        if record.mode in ('deterministic_script', 'hybrid'):
            self._script_step(record, spec, run)
            if record.status not in ('succeeded', 'escalated_recovered') or record.mode == 'deterministic_script':
                return
            outputs = {**outputs, record.step_id: payload(record.output_snapshot)}
        self._analysis_step(record, spec, outputs, self._sources(definition))

    # LLM: 兜底须满足原输出合同；进程组清理未验证的硬失败不能被回调的 recovered 覆盖。
    # 函数用途: 执行脚本并按实际兜底结果记录恢复或失败。
    def _script_step(self, record, spec, run):
        result = self.runner.run_script(record.script_id, record.input_snapshot, timeout_seconds=spec.get('timeout_seconds'))
        record.output_snapshot, record.error_code = payload(result.output_data), result.error_code
        record.error_message = result.stderr_preview if not result.ok else ''
        record.status = 'succeeded' if result.ok else 'failed'
        record.needs_repair = not result.ok
        if result.ok:
            return
        policy = spec.get('on_error', 'escalate_to_agent')
        record.escalation_result = redact_sensitive_value(payload(result.escalation_context or {}))
        cleanup = result.escalation_context.get('cleanup_error')
        if policy == 'skip' and not cleanup:
            record.status, record.needs_repair = 'skipped', False
            return
        if policy != 'escalate_to_agent':
            return
        record.status = 'paused_for_resume'
        if self.escalation_handler is None:
            return
        record.llm_involved = True
        recovered = payload(self._escalate(spec, result, run))
        if not isinstance(recovered, dict):
            raise ValueError('兜底回调必须返回 JSON 对象')
        record.escalation_result.update(redact_sensitive_value(recovered))
        if cleanup:
            record.escalation_result['cleanup_error'] = payload(cleanup)
            return
        if recovered.get('recovered') is True:
            patched = recovered.get('patched_output', recovered.get('output_data'))
            output, error = parse_script_output(json.dumps(patched), result.escalation_context.get('io_contract', {}))
            if error:
                record.error_code = error
                return
            record.status, record.output_snapshot, record.needs_repair = 'succeeded', output, False

    # LLM: 先检查签名再选择旧单参或新三参协议，绝不因回调内部TypeError重复执行。
    # 函数用途: 兼容既有上下文回调，同时提供结构化步骤、结果和运行快照。
    def _escalate(self, spec, result, run):
        try:
            inspect.signature(self.escalation_handler).bind(SimpleNamespace(**spec), result, run)
        except TypeError:
            return self.escalation_handler({**payload(result.escalation_context), 'step_id': spec['step_id']}) or {}
        return self.escalation_handler(SimpleNamespace(**payload(spec)), replace(result, **payload(result)),
                                       WorkflowRunRecord.from_dict(run.to_dict())) or {}

    # LLM: 静态技能与动态结果去重合并；未提供分析器明确失败，不伪造 LLM 成功。
    # 函数用途: 解析所需技能并调用宿主分析回调。
    def _analysis_step(self, record, spec, outputs, sources):
        ids, tags = skill_sequence(spec.get('skill_ids', ())), skill_sequence(spec.get('skill_tags', ()))
        resolved = self._resolve_skills(spec, outputs, (ids, tags, sources))
        if not all(isinstance(item, str) for item in resolved):
            raise ValueError('技能解析器必须返回技能编号序列')
        record.resolved_skill_ids = resolved
        if self.analysis_handler is None:
            raise ValueError('未提供技能分析回调')
        record.llm_involved = record.llm_involved or getattr(self.analysis_handler, 'llm_involved', True) is not False
        output = payload(self.analysis_handler(payload(spec), payload(outputs), resolved))
        if not isinstance(output, dict):
            raise ValueError('分析回调必须返回 JSON 对象')
        record.status, record.output_snapshot = 'succeeded', output

    # LLM: 新解析器返回权威ID列表；旧三参解析器保留静态与动态合并行为，签名错误不重试执行。
    # 函数用途: 适配步骤/上下文解析协议与既有编号/标签/来源协议。
    def _resolve_skills(self, spec, outputs, legacy):
        ids, tags, sources = legacy
        if self.skill_resolver is None:
            return ids
        try:
            inspect.signature(self.skill_resolver).bind(SimpleNamespace(**spec), outputs)
        except TypeError:
            resolved = (*ids, *skill_sequence(self.skill_resolver(ids, tags, payload(sources))))
        else:
            resolved = skill_sequence(self.skill_resolver(SimpleNamespace(**payload(spec)), payload(outputs)))
        return tuple(dict.fromkeys(resolved))

    # LLM: 数据源来自冻结工作流显式配置或其脚本当前权威配置；禁止将不同形状悄悄覆盖。
    # 函数用途: 给动态技能匹配提供完整的已配置数据源。
    def _sources(self, definition):
        descriptor = definition.get('descriptor', definition)
        if descriptor.get('data_sources'):
            return payload(descriptor['data_sources'])
        merged = None
        for script_id in dict.fromkeys(s.get('script_id') for s in definition['steps'] if s.get('script_id')):
            _, metadata = load_script(self.owner_home, script_id)
            sources = metadata.get('data_sources', {})
            if merged is None:
                merged = payload(sources)
            elif isinstance(merged, dict) and isinstance(sources, dict):
                merged.update(payload(sources))
            elif isinstance(merged, list) and isinstance(sources, list):
                merged.extend(payload(sources))
            else:
                raise ValueError('数据源配置形状不一致')
        return merged if merged is not None else {}
