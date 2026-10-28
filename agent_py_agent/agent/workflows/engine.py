# LLM: 正式运行沿原账本；debug默认无外部副作用，显式许可仍禁正式账本/摘要/pin；通知仅走原Owner出口。
# 模块用途: 编排正式执行及安全单节点调试，保留模式别名、审批等待和真实通知投递状态。
from __future__ import annotations

import hashlib
import json
import math
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
from .datasource_runtime import (
    bind_script_workspace,
    emit_asset_im_notification,
    run_script_with_datasources,
)
from .models import (
    AnalysisContext,
    StepDebugResult,
    StepExecutionRecord,
    WorkflowRunRecord,
    output_value,
    payload,
    safe_path,
    skill_sequence,
)
from .models import redact_bearer_values as redact_sensitive_value
from .run_store import WorkflowRunStore
from .script_runner import ScriptRunner, _EntrypointMissing, load_script, parse_script_output

SUCCESS = ('succeeded', 'escalated_recovered', 'skipped')


# LLM: 定义恢复与refs派生只走资产层权威API，路径安全仍由引擎和Store检查。
# 函数用途: 加载并复制规范执行定义。
def _definition(home, value, workspace_root=None):
    if isinstance(value, str):
        safe_path(home, value)
        asset = WorkflowStore(home, workspace_root=workspace_root).get_workflow(value)
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


# LLM: 生命周期持有冻结定义、版本和断点合同，步骤调度由子类_drive提供，不建立另一执行器。
# 类用途: 管理工作流创建、恢复与运行级合同，供公开引擎复用。
class _WorkflowRunLifecycle:
    # LLM: 构造不执行资产；外部debug执行器/分析器只由可信宿主注入，定义JSON不授新权限。
    # 函数用途: 绑定Owner存储、分析及可选通用外部节点执行端口，默认不具备外部节点执行能力。
    def __init__(self, owner_home, *, escalation_handler=None, analysis_handler=None, skill_resolver=None,
                 skill_details_resolver=None, workspace_root=None, debug_node_handler=None):
        self.owner_home = Path(owner_home).absolute()
        self.workspace_root = workspace_root
        self.store, self.runner = WorkflowRunStore(owner_home), ScriptRunner(owner_home, workspace_root=workspace_root)
        self.escalation_handler, self.analysis_handler = escalation_handler, analysis_handler
        self.skill_resolver = skill_resolver
        self.skill_details_resolver = skill_details_resolver
        self.debug_node_handler = debug_node_handler

    # LLM: 首次执行冻结定义及脚本版本/hash/来源；owner_id只校验一致性，不建立授权。
    # 函数用途: 新建运行账本并顺序执行步骤。
    def run_workflow(self, workflow, inputs=None, trigger_kind='manual', owner_id=''):
        definition, descriptor = _definition(self.owner_home, workflow, self.workspace_root)
        self._require_workflow_enabled(definition)
        if owner_id and descriptor.get('owner_id') and owner_id != descriptor['owner_id']:
            raise ValueError('工作流 Owner 不匹配')
        record = WorkflowRunRecord(uuid.uuid4().hex, descriptor['id'], owner_id or descriptor.get('owner_id', ''),
                                   trigger_kind, 'running', time.time(), 0, 0,
                                   steps=tuple(_pending(spec) for spec in definition['steps']),
                                   inputs=payload({} if inputs is None else inputs))
        definition['_frozen_scripts'] = self._script_pins(definition['steps'])
        record = replace(record, workflow_name=descriptor['name'], total_steps=len(definition['steps']))
        self.store.create_run(record, definition)
        with self.store.execution_lock(record.workflow_run_id):
            return self._drive(record, definition, 0)

    # LLM: 参数/补丁先完成无副作用验证，再核后缀版本；合法修复可重钉当前依赖，确认/漂移断点仍强制版本门。
    # 函数用途: 在独占锁内校验修复请求、保存批准的快照变更并续跑，保留成功前缀。
    def resume_workflow_run(self, workflow_run_id, from_step_id=None, *, patched_step_output=None, updated_data_sources=None,
                            resolution_origin: Literal['user', 'agent', 'system'] | None = None, allow_script_drift: bool = False):
        if type(allow_script_drift) is not bool:
            raise ValueError('allow_script_drift必须为布尔值')
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
            if (start == len(ids) and ids and record.status == 'failed'
                    and (record.breakpoint or {}).get('error_code') == 'WORKFLOW_OUTPUT_CONTRACT_VIOLATION'):
                start -= 1
            if any(step.status not in SUCCESS for step in record.steps[:start]):
                raise ValueError('不能跳过未成功的前序步骤')
            repaired = None
            if patched_step_output is not None:
                if start == len(ids):
                    raise ValueError('没有可注入修复输出的目标步骤')
                repaired = self._patched_step(record.steps[start], definition['steps'][start], patched_step_output, resolution_origin)
            if updated_data_sources is not None and not isinstance(updated_data_sources, (dict, list)):
                raise ValueError('updated_data_sources必须为dict或list')
            blocked = self._resume_script_gate(record, definition, start, allow_script_drift)
            if blocked is not None:
                return blocked
            if updated_data_sources is not None:
                definition.setdefault('descriptor', {})['data_sources'] = payload(updated_data_sources)
                definition['_override_data_sources'] = True
                self._save_definition(record, definition)
            if start == len(ids) and record.status == 'succeeded':
                return record
            steps = record.steps[:start] + ((repaired,) if repaired else ()) + tuple(
                _pending(spec) for spec in definition['steps'][start + bool(repaired):])
            record = replace(record, steps=steps, status='running', ended_at=0, failed_step_id='',
                             needs_repair=False, current_step_id='', final_output={}, resumed_from_step_id=ids[start] if start < len(ids) else '')
            record = replace(record, breakpoint=None)
            if repaired:
                self.store.save_step_result(workflow_run_id, repaired)
            self.store.save_run(record)
            return self._drive(record, definition, start + bool(repaired))

    resume_workflow = resume_workflow_run

    # LLM: 项目覆盖走原只读Store，旧meta只在正式Store缺失时读；正文摘要不授执行权。
    # 函数用途: 统一读取引擎当前作用域的脚本代码与元数据。
    def _script_metadata(self, script_id):
        return (ScriptStore(self.owner_home, workspace_root=self.workspace_root).get_script(script_id)
                or load_script(self.owner_home, script_id)[1])

    # LLM: 不可用依赖保留缺失pin，由实际步骤生成断点；代码摘要和资产版本不由输出正文推导。
    # 函数用途: 冻结指定步骤脚本的revision、hash及来源。
    def _script_pins(self, steps):
        pins = {}
        for spec in steps:
            if not spec.get('script_id'):
                continue
            try:
                row = self._script_metadata(spec['script_id'])
                pins[spec['step_id']] = {'script_id': spec['script_id'], 'revision': row.get('revision', 0),
                    'code_sha256': hashlib.sha256(row['code'].encode('utf-8')).hexdigest(),
                    'source_scope': row.get('source_scope', 'owner'),
                    'runtime_run_count': row.get('runtime_summary', {}).get('run_count', 0)}
            except (OSError, ValueError, KeyError):
                pins[spec['step_id']] = {'script_id': spec['script_id'], 'revision': None, 'code_sha256': '', 'source_scope': 'missing'}
        return pins

    # LLM: 仅接受一次原Store摘要推进，代码/来源不变且revision/run_count精确+1；只更新权威run内嵌pin，不创建修复旁件。
    # 函数用途: 推进本次执行自己的摘要版本，避免正常恢复误报漂移。
    def _advance_summary_pin(self, run, definition, spec):
        frozen = definition.get('_frozen_scripts', {})
        previous = frozen.get(spec['step_id'], {})
        current = self._script_pins([spec]).get(spec['step_id'], {})
        revision = previous.get('revision')
        if not isinstance(revision, int):
            return
        identity = ('script_id', 'code_sha256', 'source_scope')
        if (current.get('revision') != revision + 1
                or current.get('runtime_run_count') != previous.get('runtime_run_count', 0) + 1
                or any(current.get(key) != previous.get(key) for key in identity)):
            return
        for key, pin in tuple(frozen.items()):
            if pin == previous:
                frozen[key] = dict(current)
        self.store.save_run(run, definition)

    # LLM: 非漂移故障可修复当前依赖及后缀中完全相同原pin的同版本复用；其它后续/确认断点仍核原pin，前缀不变。
    # 函数用途: 接纳当前故障资产修复，拦截其它后续漂移，仅在核验通过后发表新pin。
    def _resume_script_gate(self, record, definition, start, allow):
        current = self._script_pins(definition['steps'][start:])
        frozen = definition.get('_frozen_scripts', {})
        target = record.steps[start] if start < len(record.steps) else None
        repaired = set()
        if target and target.needs_repair and target.error_code != 'SCRIPT_REVISION_DRIFT' and target.step_id in current:
            previous, replacement = frozen.get(target.step_id), current[target.step_id]
            repaired = {key for key, value in current.items() if key == target.step_id
                        or previous and frozen.get(key) == previous and value == replacement}
        changed = next((key for key, value in current.items() if key not in repaired and frozen.get(key) != value), None)
        if changed is None or allow:
            approved = {key: value for key, value in current.items() if (allow or key in repaired) and frozen.get(key) != value}
            if approved:
                definition['_frozen_scripts'] = {**frozen, **approved}
                self._save_definition(record, definition)
            return None
        index = next(i for i, step in enumerate(record.steps) if step.step_id == changed)
        step = replace(record.steps[index], error_code='SCRIPT_REVISION_DRIFT',
                       error_message='依赖脚本版本已变化，需显式allow_script_drift确认', ended_at=time.time(),
                       status='paused_for_resume', needs_repair=True)
        steps = record.steps[:index] + (step,) + record.steps[index + 1:]
        blocked = replace(record, steps=steps, status='paused_for_resume', needs_repair=True, current_step_id=changed,
                          failed_step_id=changed, ended_at=time.time(), breakpoint=self._breakpoint(step, index))
        self.store.save_run(blocked)
        self._notify_breakpoint(blocked)
        return blocked

    # LLM: 一基位置对应冻结定义，错误消息统一脱敏；主动暂停也有明确恢复选项，不推断执行身份。
    # 函数用途: 构造公开断点对象。
    def _breakpoint(self, step, index):
        return redact_sensitive_value({'step_index': index + 1, 'step_id': step.step_id, 'step_name': step.name,
            'status': 'failed' if step.needs_repair or step.error_code else step.status,
            'error_code': step.error_code or 'WORKFLOW_PAUSED', 'error_message': step.error_message,
            'failed_at': datetime.fromtimestamp(step.ended_at or time.time(), timezone.utc).isoformat(),
            'can_resume': True, 'resume_options': ['rerun_from_step', 'updated_data_sources', 'patched_step_output']})

    # LLM: 通知失败不覆盖已保存运行事实；只经当前Owner通知出口，不自行解析通道或发送认证。
    # 函数用途: 把具体断点交给IM账本及可用outbox。
    def _notify_breakpoint(self, record):
        emit_asset_im_notification(self.owner_home, event_type='workflow_breakpoint', title=record.workflow_name,
            message=f'{record.workflow_name}: {record.current_step_id}',
            payload={'workflow_run_id': record.workflow_run_id, 'workflow_id': record.workflow_id,
                     'total_steps': record.total_steps, 'completed_steps': record.completed_steps, 'breakpoint': record.breakpoint})

    # LLM: 显式类型合同或完全空输出按成功步骤合并值校验，不采纳失败/跳过输出；旧非空无类型合同保持兼容。
    # 函数用途: 更新进度、构造断点并核工作流合同，保留末步真实final_output。
    def _finalize_record(self, record, definition):
        record = replace(record, workflow_name=definition['descriptor']['name'], total_steps=len(record.steps),
                         completed_steps=sum(step.status in SUCCESS for step in record.steps))
        error = ''
        if record.status == 'succeeded':
            merged_outputs = {}
            for step in record.steps:
                if step.status in ('succeeded', 'escalated_recovered') and isinstance(step.output_snapshot, dict):
                    merged_outputs.update(step.output_snapshot)
            merged_outputs.update(record.final_output)
            contract = definition['descriptor'].get('io_contract', {})
            if contract.get('output_types') or not merged_outputs:
                _, error = parse_script_output(json.dumps(merged_outputs), contract)
        if error:
            record = replace(record, status='failed', needs_repair=True,
                current_step_id=record.steps[-1].step_id if record.steps else '',
                failed_step_id=record.steps[-1].step_id if record.steps else '')
        if record.status not in ('failed', 'paused_for_resume'):
            return replace(record, breakpoint=None)
        index = next((i for i, step in enumerate(record.steps) if step.step_id == record.current_step_id), max(len(record.steps) - 1, 0))
        step = record.steps[index] if record.steps else StepExecutionRecord('', '工作流输出', '', 'failed', 0, record.ended_at, 0, False)
        if error:
            step = replace(step, needs_repair=True, error_code='WORKFLOW_OUTPUT_CONTRACT_VIOLATION', error_message=error)
        return replace(record, breakpoint=self._breakpoint(step, index))

# LLM: 复用原执行链；debug显式副作用许可与内存标志由调用方传入，不能从config/mock授予权限。
# 类用途: 执行正式工作流，或默认只预览一个节点而不改变正式运行、资产修订及外部业务。
class WorkflowEngine(_WorkflowRunLifecycle):
    # LLM: 默认False在映射/条件后阻止业务副作用；显式True不写RunStore/摘要/pin或升级，返回原mode与新旧DTO键。
    # 函数用途: 只调试目标节点，报告渲染输入、分支、预览和未持久化正式运行；脚本仍走原沙箱。
    def debug_step(self, workflow_id: str, step_id: str, *, mock_inputs: dict | None = None,
                   context_overrides: dict | None = None, timeout_seconds: float = 30.0,
                   allow_side_effects: bool = False):
        if type(allow_side_effects) is not bool:
            raise ValueError('allow_side_effects必须为布尔值')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 3600):
            raise ValueError('调试超时必须为(0,3600]秒')
        if any(value is not None and not isinstance(value, dict) for value in (mock_inputs, context_overrides)):
            raise ValueError('调试输入和上下文必须是对象')
        definition, descriptor = _definition(self.owner_home, workflow_id, self.workspace_root)
        self._require_workflow_enabled(definition)
        selected = next((spec for spec in definition['steps'] if spec['step_id'] == step_id), None)
        if selected is None:
            raise ValueError('调试步骤不存在')
        spec = payload(selected)
        inputs, outputs = payload(mock_inputs or {}), payload(context_overrides or {})
        spec['inputs_map'] = {key: value for key, value in spec.get('inputs_map', {}).items() if key not in inputs}
        spec['timeout_seconds'] = min(float(spec.get('timeout_seconds') or timeout_seconds), timeout_seconds)
        # 失败调试不能通过escalation回调间接持久化修复；debug保留原失败，不触发兜底。
        spec['on_error'] = 'fail'
        run = WorkflowRunRecord(f'debug-{uuid.uuid4().hex[:12]}', descriptor['id'],
            descriptor.get('owner_id', ''), 'debug', 'running', time.time(), 0, 0,
            inputs=inputs, ephemeral_debug=True)
        record = replace(_pending(spec), status='running', input_snapshot=payload(inputs), started_at=time.time())
        timer = time.monotonic()
        try:
            self._perform(record, spec, outputs, definition, run, record_summary=False,
                          allow_side_effects=allow_side_effects)
        except Exception as exc:
            record.status, record.error_code = 'failed', getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR')
            record.error_message = '调试步骤执行失败'
        status = ('skipped' if record.status == 'skipped' else 'succeeded'
                  if record.status in ('succeeded', 'escalated_recovered', 'paused_for_resume') else 'failed')
        return StepDebugResult(workflow_id=descriptor['id'], step_id=step_id, step_type=record.mode, status=status,
            duration_ms=(time.monotonic() - timer) * 1000, output_data=record.output_snapshot,
            rendered_input=record.input_snapshot, condition_branch=record.escalation_result.get('condition_branch'),
            logs=record.escalation_result.get('debug_logs', []),
            dry_run=record.escalation_result.get('dry_run', False),
            side_effects_blocked=record.escalation_result.get('side_effects_blocked', False),
            error_detail={'code': record.error_code, 'message': record.error_message} if status == 'failed' else None).to_dict()

    # LLM: 补丁仅替代输出；脚本合同来自当前授权作用域，故障默认agent/确认默认user，清理门保持。
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
                contract = self._script_metadata(spec['script_id']).get('io_contract', {})
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

    # LLM: 项目/全局命中不写同名Owner摘要；原Store协议和失败隔离保持，不扩大只读资产写权限。
    # 函数用途: 尽力回写最近状态、耗时及错误。
    def _record_summary(self, record):
        try:
            if self.workspace_root is not None:
                current = WorkflowStore(self.owner_home, workspace_root=self.workspace_root).get_workflow(record.workflow_id)
                if current is not None and current.descriptor.scope_layer != 'owner':
                    return
            summary = WorkflowStore(self.owner_home).record_run_summary
            error = next((f'{s.error_code}: {s.error_message}' for s in record.steps if s.step_id == record.failed_step_id), '')
            summary(record.workflow_id, record.status, record.duration_ms,
                    datetime.fromtimestamp(record.ended_at, timezone.utc).isoformat(), last_error=error)
        except Exception:
            pass

    # LLM: 失败/暂停在真实步骤停住，结算前核最终合同并保存断点；中断仍先落盘再抛，通知不修改运行事实。
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
                record = self._finalize_record(record, definition)
                self.store.save_run(record)
                self._notify_breakpoint(record)
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
        record = self._finalize_record(record, definition)
        self.store.save_run(record)
        if record.breakpoint:
            self._notify_breakpoint(record)
        self._record_summary(record)
        return record

    # LLM: 正式链仍只原三模式；条件别名纯求值，脚本别名及审批/通知都先过debug副作用门，内置mock例外不变。
    # 函数用途: 复用旧脚本执行链并安全分派单节点，不能从mode别名/config或分析器属性取得新权限。
    def _perform(self, record, spec, outputs, definition, run, *, record_summary=True, allow_side_effects=True):
        from .debug_nodes import analysis_preview
        self._require_step_enabled(spec)
        if not run.ephemeral_debug and record.mode not in ('deterministic_script', 'llm_skill_analysis', 'hybrid'):
            record.status, record.error_code = 'failed', 'NODE_EXECUTOR_UNAVAILABLE'
            record.error_message = '通用节点当前仅支持单节点调试，不支持正式DAG执行'
            record.needs_repair = True
            return
        for key, reference in spec.get('inputs_map', {}).items():
            record.input_snapshot[key] = output_value(reference, outputs, run.inputs) if isinstance(reference, dict) and 'step_id' in reference else payload(reference)
        if run.ephemeral_debug and spec.get('condition'):
            condition = spec['condition']
            if not isinstance(condition, dict) or 'equals' not in condition:
                raise ValueError('条件必须是结构化比较')
            matched = output_value(condition, outputs, run.inputs) == condition['equals']
            record.escalation_result['condition_branch'] = matched
            if not matched:
                record.status = 'skipped'
                return
        pure = record.mode in ('condition', 'condition_branch') or record.mode == 'script_transform' and not spec.get('script_id')
        local_analysis = (record.mode in ('llm_skill_analysis', 'llm_prompt')
                          and self.analysis_handler is analysis_preview)
        if run.ephemeral_debug and not allow_side_effects and not pure:
            record.escalation_result.update(dry_run=True, side_effects_blocked=True)
            if not local_analysis:
                record.status = 'succeeded'
                record.escalation_result['debug_logs'] = ['仅预览：未执行脚本、外部分析器、网络、工具或子工作流']
                return
        if record.mode in ('condition', 'condition_branch', 'script_transform', 'http_request', 'tool_call',
                           'subworkflow', 'approval_wait', 'notification'):
            self._generic_debug_step(record, spec, outputs, definition, run)
            return
        if record.mode in ('deterministic_script', 'script', 'hybrid'):
            self._script_step(record, spec, run, definition, record_summary=record_summary)
            if record.status not in ('succeeded', 'escalated_recovered') or record.mode in ('deterministic_script', 'script'):
                return
            outputs = {**outputs, record.step_id: payload(record.output_snapshot)}
        context = AnalysisContext(outputs, run.inputs, record.input_snapshot, data_sources=self._sources(definition))
        self._analysis_step(record, spec, context)

    # LLM: 仅debug通过副作用门后进入；审批不自批，通知只走原Owner通知账/outbox并保留真实状态，不写正式run。
    # 函数用途: 计算条件/转换、审批等待或显式通知；其它外部节点仍需可信执行器，脚本源合同不变。
    def _generic_debug_step(self, record, spec, outputs, definition, run):
        from .debug_nodes import approval_wait_result, evaluate_expression
        config = spec.get('config', {})
        if record.mode in ('condition', 'condition_branch'):
            if 'expression' in config:
                matched = bool(evaluate_expression(config['expression'], record.input_snapshot))
            else:
                matched = record.escalation_result.get('condition_branch')
                if matched is None:
                    raise ValueError('条件节点缺少表达式或结构化比较')
            record.escalation_result['condition_branch'] = matched
            record.output_snapshot = {'matched': matched}
        elif record.mode == 'script_transform' and not spec.get('script_id'):
            if 'expression' in config:
                output = evaluate_expression(config['expression'], record.input_snapshot)
            else:
                output = {key: output_value(value, outputs, record.input_snapshot)
                          if isinstance(value, dict) and 'step_id' in value else payload(value)
                          for key, value in config.get('mapping', record.input_snapshot).items()}
            if not isinstance(output, dict):
                raise ValueError('转换输出必须是JSON对象')
            record.output_snapshot = output
        elif record.mode == 'script_transform':
            self._script_step(record, spec, run, definition, record_summary=False)
            return
        elif record.mode == 'approval_wait':
            record.output_snapshot = approval_wait_result(record.input_snapshot)
        elif record.mode == 'notification':
            title, message = config.get('title', record.name), config.get('message', record.input_snapshot.get('message', ''))
            if not isinstance(title, str) or not isinstance(message, str):
                raise ValueError('通知title/message必须是字符串')
            notice = redact_sensitive_value({'title': title, 'message': message, 'payload': record.input_snapshot})
            dispatched = emit_asset_im_notification(self.owner_home, event_type='workflow_debug_notification', **notice)
            record.output_snapshot = {'dispatch_result': dispatched}
        elif not run.ephemeral_debug or not callable(self.debug_node_handler):
            record.status, record.error_code = 'failed', 'NODE_EXECUTOR_UNAVAILABLE'
            record.error_message = '未配置可信单节点执行器'
            return
        else:
            context = AnalysisContext(outputs, run.inputs, record.input_snapshot)
            output = payload(self.debug_node_handler(payload(spec), context, timeout_seconds=spec['timeout_seconds']))
            if not isinstance(output, dict):
                raise ValueError('节点执行器必须返回JSON对象')
            record.output_snapshot = output
        record.status = 'succeeded'

    # LLM: 当前授权workspace/Owner定义决定开关，临时内联仍受同ID禁用约束，不改冻结步骤。
    # 函数用途: 在新建、恢复前拒绝已关闭工作流。
    def _require_workflow_enabled(self, definition):
        descriptor = definition['descriptor']
        current = WorkflowStore(self.owner_home, workspace_root=self.workspace_root).get_workflow(descriptor['id'])
        if (definition.get('enabled', True) is False or descriptor.get('enabled', True) is False
                or current is not None and not current.enabled):
            raise AssetDisabledError('workflow', descriptor['id'])

    # LLM: 同workspace/Owner只读核验，明确False硬拒绝；确切入口缺失交后续版本门归类，其它坏元数据不吞。
    # 函数用途: 在运行任一步前核对脚本和显式技能是否停用。
    def _require_step_enabled(self, spec):
        if spec.get('script_id'):
            with bind_script_workspace(self.owner_home, self.workspace_root):
                try:
                    script = load_script(self.owner_home, spec['script_id'])[1]
                except _EntrypointMissing:
                    script = None
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

    # LLM: 源/漂移门不变；debug禁摘要/pin与失败升级，替换脚本错误上下文时仍保留已评估分支及预览事实。
    # 函数用途: 沿原脚本链执行并保留成功/失败日志及调试合同，内存调试不写资产或运行账。
    def _script_step(self, record, spec, run, definition, *, record_summary=True):
        sources = definition.get('descriptor', definition).get('data_sources')
        options = {'override_data_sources': payload(sources)} if sources or definition.get('_override_data_sources') else {}
        if not record_summary:
            options['options'] = {'record_summary': False}
        result = run_script_with_datasources(self.owner_home, record.script_id, record.input_snapshot,
            runner=self.runner, workspace_root=self.workspace_root, timeout_seconds=spec.get('timeout_seconds'),
            expected_script_pin=definition.get('_frozen_scripts', {}).get(spec['step_id']), **options)
        if record_summary:
            self._advance_summary_pin(run, definition, spec)
        else:
            record.escalation_result['debug_logs'] = [text for text in
                (result.stdout_preview, result.stderr_preview) if text]
        record.output_snapshot, record.error_code = payload(result.output_data), result.error_code
        record.error_message = result.stderr_preview if not result.ok else ''
        record.status = 'succeeded' if result.ok else 'failed'
        record.needs_repair = not result.ok
        if result.ok:
            if isinstance(record.output_snapshot, dict) and record.output_snapshot.get('_pause_for_resume') is True:
                record.status, record.needs_repair = 'paused_for_resume', False
            return
        policy = spec.get('on_error', 'escalate_to_agent')
        debug_logs = record.escalation_result.get('debug_logs', [])
        debug_metadata = {key: record.escalation_result[key] for key in
                          ('condition_branch', 'dry_run', 'side_effects_blocked') if key in record.escalation_result}
        record.escalation_result = redact_sensitive_value(payload(result.escalation_context or {}))
        if not record_summary:
            record.escalation_result.update(debug_logs=debug_logs, **debug_metadata)
        cleanup = result.escalation_context.get('cleanup_error')
        record.escalation_result['repair_guide'] = redact_sensitive_value({
            'failed_step_id': record.step_id, 'script_id': record.script_id, 'error_code': result.error_code,
            'io_contract': payload(result.escalation_context.get('io_contract', {})),
            'resume_options': ['patched_step_output', 'updated_data_sources', 'rerun_from_step']})
        hard_stop = result.error_code in {'SCRIPT_REVISION_DRIFT', 'DATASOURCE_MISSING', 'DATASOURCE_EMPTY'}
        if hard_stop:
            record.status = 'paused_for_resume' if policy == 'escalate_to_agent' else 'failed'
            return
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
        record.escalation_result.update({key: value for key, value in redact_sensitive_value(recovered).items()
                                         if key not in ('repair_guide', 'cleanup_error')})
        if cleanup:
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
            metadata = self._script_metadata(script_id)
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
