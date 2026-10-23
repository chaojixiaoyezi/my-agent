# LLM: 公开引擎仅使用宿主注入的模型回调；运行事实和恢复位置来自账本，不从自然语言推断。
# 模块用途: 编排纯脚本、条件跳过、技能分析、异常兜底与 Step 级断点续跑。
from __future__ import annotations

import inspect
import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ..assets.workflow_store import WorkflowStore
from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
from .models import (
    WORKFLOW_ON_ERROR_POLICIES,
    StepExecutionRecord,
    WorkflowRunRecord,
    output_value,
    payload,
    safe_path,
    skill_sequence,
)
from .models import redact_bearer_values as redact_sensitive_value
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

    # LLM: 复用冻结定义及成功前缀；所有补丁先校验再发表，修复输出不能替代未成功前序。
    # 函数用途: 在独占执行锁内注入合法修复或更新数据源，从断点继续运行。
    def resume_workflow_run(self, workflow_run_id, from_step_id=None, *, patched_step_output=None, updated_data_sources=None):
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
            repaired = None
            if patched_step_output is not None:
                if start == len(ids):
                    raise ValueError('没有可注入修复输出的目标步骤')
                repaired = self._patched_step(record.steps[start], definition['steps'][start], patched_step_output)
            if updated_data_sources is not None:
                if not isinstance(updated_data_sources, (dict, list)):
                    raise ValueError('updated_data_sources必须为dict或list')
                definition.setdefault('descriptor', {})['data_sources'] = payload(updated_data_sources)
                definition['_override_data_sources'] = True
                self._save_definition(record, definition)
            if start == len(ids) and record.status == 'succeeded':
                return record
            steps = record.steps[:start] + ((repaired,) if repaired else ()) + tuple(
                _pending(spec) for spec in definition['steps'][start + bool(repaired):])
            record = replace(record, steps=steps, status='running', ended_at=0, failed_step_id='',
                             needs_repair=False, current_step_id='', final_output={}, resumed_from_step_id=ids[start] if start < len(ids) else '')
            if repaired:
                self.store.save_step_result(workflow_run_id, repaired)
            self.store.save_run(record)
            return self._drive(record, definition, start + bool(repaired))

    resume_workflow = resume_workflow_run

    # LLM: 补丁只替代结构化输出，不能证明进程组清理完成；原错误合同和清理硬门禁仍生效。
    # 函数用途: 拒绝清理未确认的补丁，并构造保留诊断和修复来源的合法恢复步骤。
    def _patched_step(self, target, spec, patched):
        if target.escalation_result.get('cleanup_error'):
            raise ValueError('进程组清理未验证，不能用输出补丁恢复')
        if not isinstance(patched, dict):
            raise ValueError('patched_step_output必须为dict')
        output = payload(patched)
        if spec.get('script_id'):
            contract = target.escalation_result.get('io_contract')
            if not contract:
                contract = load_script(self.owner_home, spec['script_id'])[1].get('io_contract', {})
            output, error = parse_script_output(json.dumps(output, allow_nan=False), contract)
            if error:
                raise ValueError(f'修复输出不符合脚本契约: {error}')
        return replace(target, status='escalated_recovered', output_snapshot=redact_sensitive_value(output), needs_repair=False,
                       llm_involved=True, error_code='', error_message='', ended_at=time.time(),
                       escalation_result=redact_sensitive_value({**payload(target.escalation_result),
                           'recovered': True, 'repaired_via': 'patched_step_output'}))

    # LLM: 调用方持运行执行锁；独立快照与Store实际读取的内嵌定义保持同源，拒绝跟随链接。
    # 函数用途: 原子保存更新后的冻结数据源，供本轮及重启后续跑复用。
    def _save_definition(self, record, definition):
        safe = redact_sensitive_value(payload(definition))
        write_text_atomic_beneath(self.owner_home, ('data', 'workflow_runs', record.workflow_run_id, 'workflow_snapshot.json'),
                                  json.dumps(safe, ensure_ascii=False, allow_nan=False) + '\n')
        self.store.save_run(record, definition)

    # LLM: 运行账本先落盘，摘要写入失败不覆盖执行事实；仅内存定义可能没有正式资产。
    # 函数用途: 尽力回写工作流最近状态、耗时和运行时间。
    def _record_summary(self, record):
        try:
            summary = WorkflowStore(self.owner_home).record_run_summary
            error = next((f'{s.error_code}: {s.error_message}' for s in record.steps if s.step_id == record.failed_step_id), '')
            options = {'last_error': error} if 'last_error' in inspect.signature(summary).parameters else {}
            summary(record.workflow_id, record.status, record.duration_ms,
                    datetime.fromtimestamp(record.ended_at, timezone.utc).isoformat(), **options)
        except Exception:
            pass

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
                if condition and output_value(condition, outputs, record.inputs) != condition['equals']:
                    step.status = 'skipped'
                else:
                    self._perform(step, spec, outputs, definition, record)
            except (KeyboardInterrupt, SystemExit):
                step = replace(step, status='paused_for_resume', needs_repair=True, ended_at=time.time(),
                               duration_ms=(time.monotonic() - clock) * 1000)
                record = self.store.save_step_result(record.workflow_run_id, step)
                record = replace(record, status='paused_for_resume', needs_repair=True, failed_step_id=step.step_id,
                                 ended_at=time.time(), duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
                                 final_output=next(reversed(outputs.values()), {}))
                self.store.save_run(record)
                self._record_summary(record)
                raise
            except Exception as exc:
                step.status, step.error_code, step.error_message = 'failed', step.error_code or 'STEP_EXECUTION_ERROR', str(exc)
                step.needs_repair = True
            step.ended_at, step.duration_ms = time.time(), (time.monotonic() - clock) * 1000
            record = self.store.save_step_result(record.workflow_run_id, step)
            if step.status in ('failed', 'paused_for_resume'):
                if not step.needs_repair and step.output_snapshot:
                    outputs[step.step_id] = payload(step.output_snapshot)
                record = replace(record, status=step.status, needs_repair=step.needs_repair,
                                 current_step_id=step.step_id, failed_step_id=step.step_id if step.needs_repair else '')
                break
            if step.status != 'skipped':
                outputs[step.step_id] = payload(step.output_snapshot)
        else:
            record = replace(record, status='succeeded', needs_repair=False, current_step_id='', failed_step_id='')
        record = replace(record, ended_at=time.time(), duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
                         final_output=next(reversed(outputs.values()), {}))
        self.store.save_run(record)
        self._record_summary(record)
        return record

    # LLM: 映射只能引用结构化前序输出或$inputs；传入回调的对象独立复制，不能污染已成功快照。
    # 函数用途: 运行一个脚本或技能步骤，混合步骤先采集再分析。
    def _perform(self, record, spec, outputs, definition, run):
        for key, reference in spec.get('inputs_map', {}).items():
            record.input_snapshot[key] = output_value(reference, outputs, run.inputs) if isinstance(reference, dict) and 'step_id' in reference else payload(reference)
        if record.mode in ('deterministic_script', 'hybrid'):
            self._script_step(record, spec, run, definition)
            if record.status not in ('succeeded', 'escalated_recovered') or record.mode == 'deterministic_script':
                return
            outputs = {**outputs, record.step_id: payload(record.output_snapshot)}
        self._analysis_step(record, spec, outputs, self._sources(definition))

    # LLM: 冻结数据源只覆盖本运行；兜底须满足原合同，清理未验证硬失败不能被回调覆盖，_pause_for_resume支持无错确认暂停。
    # 函数用途: 执行脚本并记录结构化修复指引、主动暂停断点及实际恢复结果。
    def _script_step(self, record, spec, run, definition):
        sources = definition.get('descriptor', definition).get('data_sources')
        options = {'override_data_sources': payload(sources)} if sources or definition.get('_override_data_sources') else {}
        result = self.runner.run_script(record.script_id, record.input_snapshot, timeout_seconds=spec.get('timeout_seconds'), **options)
        record.output_snapshot, record.error_code = payload(result.output_data), result.error_code
        record.error_message = result.stderr_preview if not result.ok else ''
        record.status = 'succeeded' if result.ok else 'failed'
        record.needs_repair = not result.ok
        if result.ok:
            if isinstance(record.output_snapshot, dict) and record.output_snapshot.get('_pause_for_resume') is True:
                record.status, record.needs_repair = 'paused_for_resume', False
            return
        policy = spec.get('on_error', 'escalate_to_agent')
        record.escalation_result = redact_sensitive_value(payload(result.escalation_context or {}))
        cleanup = result.escalation_context.get('cleanup_error')
        if policy == 'skip' and not cleanup:
            record.status, record.needs_repair = 'skipped', False
            return
        if policy != 'escalate_to_agent':
            return
        record.escalation_result['repair_guide'] = redact_sensitive_value({
            'failed_step_id': record.step_id, 'script_id': record.script_id, 'error_code': result.error_code,
            'io_contract': payload(result.escalation_context.get('io_contract', {})),
            'resume_options': ['patched_step_output', 'updated_data_sources', 'rerun_from_step']})
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
        record.output_snapshot = output
        record.status = 'paused_for_resume' if output.get('_pause_for_resume') is True else 'succeeded'
        record.needs_repair = False

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
        if descriptor.get('data_sources') or definition.get('_override_data_sources'):
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
