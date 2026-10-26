# LLM: 公开引擎仅使用宿主注入的模型回调；运行事实和恢复位置来自账本，不从自然语言推断。
# 模块用途: 编排纯脚本、条件跳过、技能分析、异常兜底与 Step 级断点续跑。
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from ..assets import canonical_skill_name
from ..assets.delivery_contracts import AssetDisabledError
from ..assets.script_store import ScriptStore
from ..assets.skill_adapter import get_owner_skill_descriptor
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore, with_workflow_step_refs
from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
from .models import (
    AnalysisContext,
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


# LLM: 定义恢复与refs派生只走资产层权威API，路径安全仍由引擎和Store检查。
# 函数用途: 加载并复制规范执行定义。
def _definition(home, value):
    if isinstance(value, str):
        safe_path(home, value)
        asset = WorkflowStore(home).get_workflow(value)
        value = asset.to_dict() if asset is not None else json.loads(read_text_beneath(home, ('workflows', value, 'workflow.json')) or '{}')
    value = with_workflow_step_refs(WorkflowDefinition.from_any(payload(value))).to_dict()
    descriptor = value['descriptor']
    safe_path(home, descriptor['id'])
    return value, descriptor


# LLM: 新运行和恢复后失效的后缀都创建新记录，不继承旧后缀输出。
# 函数用途: 依据定义初始化一个待执行步骤。
def _pending(spec):
    return StepExecutionRecord(spec['step_id'], spec.get('name', spec['step_id']), spec['mode'],
                               'pending', 0, 0, 0, False, script_id=spec.get('script_id', ''))


# LLM: 执行前须由宿主核验 Owner 与权限；回调只提供判断数据，不得自动应用脚本修复。
# 类用途: 运行工作流并在失败或中断时保留已成功步骤。
class WorkflowEngine:
    # LLM: 构造不执行资产，宿主提供已授权正文读取器，所有回调协议固定。
    # 函数用途: 绑定Owner存储及分析、技能解析与详情依赖。
    def __init__(self, owner_home, *, escalation_handler=None, analysis_handler=None, skill_resolver=None, skill_details_resolver=None):
        self.owner_home = Path(owner_home).absolute()
        self.store, self.runner = WorkflowRunStore(owner_home), ScriptRunner(owner_home)
        self.escalation_handler, self.analysis_handler = escalation_handler, analysis_handler
        self.skill_resolver = skill_resolver
        self.skill_details_resolver = skill_details_resolver

    # LLM: 首次执行冻结定义，先核传入及当前canonical运行闸；owner_id只校验一致性，不能建立授权。
    # 函数用途: 新建运行账本并顺序执行步骤。
    def run_workflow(self, workflow, inputs=None, trigger_kind='manual', owner_id=''):
        definition, descriptor = _definition(self.owner_home, workflow)
        self._require_workflow_enabled(definition)
        if owner_id and descriptor.get('owner_id') and owner_id != descriptor['owner_id']:
            raise ValueError('工作流 Owner 不匹配')
        record = WorkflowRunRecord(uuid.uuid4().hex, descriptor['id'], owner_id or descriptor.get('owner_id', ''),
                                   trigger_kind, 'running', time.time(), 0, 0,
                                   steps=tuple(_pending(spec) for spec in definition['steps']),
                                   inputs=payload({} if inputs is None else inputs))
        self.store.create_run(record, definition)
        with self.store.execution_lock(record.workflow_run_id):
            return self._drive(record, definition, 0)

    # LLM: 恢复前重核当前Owner开关，旧启用快照不能复活停用资产；冻结输入/成功前缀及补丁来源合同不变。
    # 函数用途: 在独占锁内注入带来源合法输出或数据源变更并续跑。
    def resume_workflow_run(self, workflow_run_id, from_step_id=None, *, patched_step_output=None, updated_data_sources=None,
                            resolution_origin: Literal['user', 'agent', 'system'] | None = None):
        if resolution_origin is not None and resolution_origin not in ('user', 'agent', 'system'):
            raise ValueError('resolution_origin必须为user、agent或system')
        with self.store.execution_lock(workflow_run_id):
            record = self.store.load_run(workflow_run_id)
            definition = self.store.load_workflow_snapshot(workflow_run_id)
            self._require_workflow_enabled(definition)
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
                repaired = self._patched_step(record.steps[start], definition['steps'][start], patched_step_output, resolution_origin)
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

    # LLM: 补丁仅替代输出；故障修复默认agent、正常确认默认user，保留既有模型参与事实和清理门。
    # 函数用途: 校验恢复输出并合成来源明确且输入不变的步骤。
    def _patched_step(self, target, spec, patched, resolution_origin: Literal['user', 'agent', 'system'] | None = None):
        origin = resolution_origin if resolution_origin is not None else ('agent' if target.needs_repair else 'user')
        if origin not in ('user', 'agent', 'system'):
            raise ValueError('resolution_origin必须为user、agent或system')
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
                       llm_involved=bool(target.llm_involved or origin == 'agent'), error_code='', error_message='', ended_at=time.time(),
                       escalation_result=redact_sensitive_value({**payload(target.escalation_result),
                           'recovered': True, 'repaired_via': 'patched_step_output', 'resolution_origin': origin}))

    # LLM: 调用方持运行执行锁；独立快照与Store实际读取的内嵌定义保持同源，拒绝跟随链接。
    # 函数用途: 原子保存更新后的冻结数据源，供本轮及重启后续跑复用。
    def _save_definition(self, record, definition):
        safe = redact_sensitive_value(payload(definition))
        write_text_atomic_beneath(self.owner_home, ('data', 'workflow_runs', record.workflow_run_id, 'workflow_snapshot.json'),
                                  json.dumps(safe, ensure_ascii=False, allow_nan=False) + '\n')
        self.store.save_run(record, definition)

    # LLM: 摘要采用固定Store协议，派生视图失败不能覆盖运行事实或中断。
    # 函数用途: 尽力回写最近状态、耗时及错误。
    def _record_summary(self, record):
        try:
            summary = WorkflowStore(self.owner_home).record_run_summary
            error = next((f'{s.error_code}: {s.error_message}' for s in record.steps if s.step_id == record.failed_step_id), '')
            summary(record.workflow_id, record.status, record.duration_ms,
                    datetime.fromtimestamp(record.ended_at, timezone.utc).isoformat(), last_error=error)
        except Exception:
            pass

    # LLM: 禁用异常保留机器码且硬失败，不走on_error兜底；原条件、暂停、账本与中断收口保持。
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
                step.status, step.error_code, step.error_message = 'failed', step.error_code or getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR'), str(exc)
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

    # LLM: 步骤引用先核运行闸，禁用依赖不能执行hybrid前半步或on_error=skip；输入映射与独立上下文不变。
    # 函数用途: 执行脚本或技能步骤，混合步先采集再完整分析。
    def _perform(self, record, spec, outputs, definition, run):
        self._require_step_enabled(spec)
        for key, reference in spec.get('inputs_map', {}).items():
            record.input_snapshot[key] = output_value(reference, outputs, run.inputs) if isinstance(reference, dict) and 'step_id' in reference else payload(reference)
        if record.mode in ('deterministic_script', 'hybrid'):
            self._script_step(record, spec, run, definition)
            if record.status not in ('succeeded', 'escalated_recovered') or record.mode == 'deterministic_script':
                return
            outputs = {**outputs, record.step_id: payload(record.output_snapshot)}
        context = AnalysisContext(outputs, run.inputs, record.input_snapshot, data_sources=self._sources(definition))
        self._analysis_step(record, spec, context)

    # LLM: 当前canonical定义是Owner开关权威；临时内联定义仍受同ID当前禁用约束，不改冻结步骤。
    # 函数用途: 在新建、恢复前拒绝已关闭工作流。
    def _require_workflow_enabled(self, definition):
        descriptor = definition['descriptor']
        current = WorkflowStore(self.owner_home).get_workflow(descriptor['id'])
        if (definition.get('enabled', True) is False or descriptor.get('enabled', True) is False
                or current is not None and not current.enabled):
            raise AssetDisabledError('workflow', descriptor['id'])

    # LLM: 缺失资产继续由原执行/解析器分类，不能把未发现冒充授权；发现的明确False始终硬拒绝。
    # 函数用途: 在运行任一步前核对脚本和显式技能是否停用。
    def _require_step_enabled(self, spec):
        if spec.get('script_id'):
            script = ScriptStore(self.owner_home).get_script(spec['script_id'])
            if script is not None and script.get('enabled', True) is False:
                raise AssetDisabledError('script', spec['script_id'])
        self._require_skills_enabled(spec.get('skill_ids', ()))

    # LLM: 精确来源缺失交原宿主resolver处理，已发现Owner技能的关闭不能被外部分析回调绕过。
    # 函数用途: 核对声明或动态解析出来的Skill运行闸。
    def _require_skills_enabled(self, references):
        for reference in references:
            try:
                descriptor = get_owner_skill_descriptor(self.owner_home, reference)
            except FileNotFoundError:
                continue
            if descriptor.get('enabled', True) is False:
                raise AssetDisabledError('skill', reference)

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

    # LLM: 升级只走三参协议，内部TypeError不能触发换签名重试。
    # 函数用途: 向兜底传入独立步骤、脚本结果和运行快照。
    def _escalate(self, spec, result, run):
        return self.escalation_handler(SimpleNamespace(**payload(spec)), replace(result, **payload(result)),
                                       WorkflowRunRecord.from_dict(run.to_dict())) or {}

    # LLM: 动态解析和宿主详情中的禁用同样硬拒绝，保留原机器码；缺技能/分析器仍不走脚本兜底。
    # 函数用途: 核对引用并读取正文，用完整上下文调用真实分析器。
    def _analysis_step(self, record, spec, context):
        try:
            resolved = self._resolve_skills(spec, context)
            self._require_skills_enabled(resolved)
            record.resolved_skill_ids = resolved
            details = [] if self.skill_details_resolver is None else self.skill_details_resolver(list(resolved))
            if not isinstance(details, list) or not all(isinstance(item, dict) for item in details):
                raise ValueError('技能正文读取器必须返回对象列表')
            for item in details:
                if item.get('enabled', True) is False:
                    raise AssetDisabledError('skill', str(item.get('id', '')))
            context = AnalysisContext(context.prior_outputs, context.run_inputs, context.step_inputs, details, context.data_sources)
        except (LookupError, FileNotFoundError, ValueError) as exc:
            record.status, record.needs_repair = 'failed', True
            record.error_code, record.error_message = getattr(exc, 'error_code', 'SKILL_NOT_FOUND'), str(exc)
            return
        if self.analysis_handler is None:
            record.status, record.needs_repair = 'failed', True
            record.error_code, record.error_message = 'ANALYZER_UNAVAILABLE', '未提供真实技能分析回调'
            return
        record.llm_involved = record.llm_involved or getattr(self.analysis_handler, 'llm_involved', True) is not False
        output = payload(self.analysis_handler(payload(spec), context, resolved))
        if not isinstance(output, dict):
            raise ValueError('分析回调必须返回 JSON 对象')
        record.output_snapshot = output
        record.status = 'paused_for_resume' if output.get('_pause_for_resume') is True else 'succeeded'
        record.needs_repair = False

    # LLM: 注入解析器是宿主权威；无解析器只认真实Owner技能，准确来源不能被替换。
    # 函数用途: 按固定step/context协议解析引用，拒绝遗漏必需Skill。
    def _resolve_skills(self, spec, context):
        requested = skill_sequence(spec.get('skill_ids', ()))
        skill_sequence(spec.get('skill_tags', ()))
        if self.skill_resolver is None:
            for skill_id in requested:
                get_owner_skill_descriptor(self.owner_home, skill_id)
            resolved = requested
        else:
            isolated = AnalysisContext(context.prior_outputs, context.run_inputs, context.step_inputs,
                                       context.skill_details, context.data_sources)
            resolved = skill_sequence(self.skill_resolver(SimpleNamespace(**payload(spec)), isolated))
        missing = [item for item in requested if not any(item == actual or ':' not in item
                   and canonical_skill_name(actual) == item for actual in resolved)]
        if missing:
            raise FileNotFoundError('缺少必需Skill: ' + ', '.join(missing))
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
