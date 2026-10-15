# LLM: 有限数据计划只调用原只读工具，不执行Python/JS/shell表达式；宿主逐调用权限/取消/审计，失败和覆盖不可被投影隐藏。
# 模块用途: 用一个模型工具回合完成确定取数、有限遍历与整理，保留所有内部调用的结构化证据。
from __future__ import annotations

import hashlib
import json
import time

from .models import (
    BaseTool,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .program_data import DataBudget, project, resolve


# LLM: 只对本次ToolInvocationContext的派发器开放；开关由宿主能力配置决定，不从参数、文件或旧快照恢复。
# 类用途: 给所有已授权owner提供渐进加载的有限只读编排工具。
class ToolProgramTool(BaseTool):
    model_spec = ToolModelSpec(name='tool_program', description=(
        '有限只读批量取数和整理。steps最多16项，每项id唯一：tool+arguments调用已加载只读工具；'
        '可foreach引用此前数组（最多32次总调用），when为{value:显式引用,equals:值}；'
        '或data+project做flatten/where{path,equals}/unique_by/fields/count_by/offset/limit。'
        '显式引用仅{"$ref":"step_id#/JSON/Pointer"}，遍历项为item#；不执行代码。'
        '独立调用可在同项calls数组中批量执行。每个结果含原ToolResult及output，完整JSON输出另有json字段。'
        '失败不会因筛选消失。先tool_search加载底层工具；写入/删除/发消息继续原工具。'
        '长命令用run_command(run_in_background=true)取得句柄，勿在此循环等待。'),
        input_schema={'type':'object', 'properties':{
            'steps':{'type':'array','minItems':1,'maxItems':16,'items':{'type':'object'}},
            'return_step':{'type':'string'}},'required':['steps'],'additionalProperties':False},
        hints=ToolModelHints(category='orchestration',default_deferred=True,
            deferred_summary='确定的只读取数、有限遍历、筛选去重统计；保持原工具权限。',
            keywords=('batch','pipeline','批量','筛选','去重','统计')))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('read_only'),
        resource_scopes=ResourceScopePolicy(mode='none'), output_policy=OutputPolicy(trust='external_data'))

    # LLM: getter由原Agent装配，不缓存允许事实，损坏/关闭立即不可用；不读或展示配置秘密。
    # 函数用途: 保存能力开关的宿主读取器，不执行程序。
    def __init__(self, enabled):
        self.enabled = enabled

    # LLM: 探测与执行均读同一开关；未知不回落开启。
    # 函数用途: 给原工具发现链说明这项能力是否启用。
    def availability(self):
        enabled = bool(self.enabled())
        return ToolAvailability.ready() if enabled else ToolAvailability.unavailable('受限只读编排开关未启用')

    # LLM: 没有宿主上下文不能构造身份或自行直调handler。
    # 函数用途: 拒绝无原执行器的直接调用。
    def execute(self, params):
        return ToolHandlerOutcome('tool_program', False, '缺少原工具执行上下文。',error_code='TOOL_PROGRAM_INVALID',effect_outcome='not_started')

    # LLM: 计划解释限16步/32调用/2MB中间数据/16KB返回；所有调用逐原工具执行，异常只给固定类型，已执行轨迹始终保留。
    # 函数用途: 执行固定只读步骤，返回最终投影、内部失败、覆盖、原证据引用和实际批次指标。
    def execute_scoped(self, params, context):
        started = time.monotonic()
        dispatch = context.tool_dispatch
        values, trace, skipped = {}, [], []
        error = ''
        try:
            if not self.enabled() or dispatch is None:
                raise ValueError('受限编排不可用')
            if len(json.dumps(params, ensure_ascii=False).encode()) > 65536:
                raise ValueError('程序输入超过预算')
            steps = params['steps']
            if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
                raise ValueError('步骤数量无效')
            for index, step in enumerate(steps):
                self._step(step, index, values, trace, skipped, dispatch)
                if len(json.dumps(values, ensure_ascii=False).encode()) > 2_000_000:
                    raise ValueError('中间数据超过预算')
            result = values[params.get('return_step', steps[-1]['id'])]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            result, error = None, type(exc).__name__
        encoded = json.dumps(result, ensure_ascii=False)
        truncated = len(encoded.encode()) > 16000
        payload = {'result': result, 'result_truncated':truncated,
            'result_chars':len(encoded),'calls':trace,'skipped_steps':skipped,
            'stats':{'tool_calls':dispatch.count if dispatch else 0,'execution_groups':dispatch.groups if dispatch else 0,
                'elapsed_ms':int((time.monotonic()-started)*1000)},'program_error':error}
        live = self._live_payload(payload)
        failed = bool(error) or any(row['status'] != 'succeeded' for row in trace)
        return ToolHandlerOutcome('tool_program', not failed, json.dumps(payload,ensure_ascii=False),
            error_code='TOOL_PROGRAM_INCOMPLETE' if failed else '',effect_outcome='not_started' if failed else '',
            result_envelope={'program':live,'tool_output_policy':{'preserve_prompt_output':True,
                'live_prompt_output':json.dumps(live, ensure_ascii=False)}})

    # LLM: step仅认准确结构，引用只指向已有数据；when跳过不能消掉之前失败，foreach溢出不静默裁掉条目。
    # 函数用途: 执行一个调用/批次或纯转换，并将原调用事实记入输出。
    def _step(self, step, index, values, trace, skipped, dispatch):
        if not isinstance(step, dict) or set(step) - {'id','tool','arguments','calls','foreach','when','data','project'}:
            raise ValueError('步骤字段无效')
        budget = DataBudget()
        budget.reserve(values)
        name = step.get('id')
        if not isinstance(name, str) or not name or name == 'item' or name in values or len(name)>64:
            raise ValueError('步骤编号无效')
        if 'when' in step:
            condition = step['when']
            if set(condition) != {'value','equals'}:
                raise ValueError('条件无效')
            if resolve(condition['value'], values, budget=budget) != condition['equals']:
                values[name] = {'skipped':True}
                skipped.append(name)
                return
        if 'data' in step:
            if set(step) - {'id','when','data','project'}:
                raise ValueError('数据步骤不能调用工具')
            data = resolve(step['data'], values, budget=budget)
            values[name] = project(data, step['project']) if 'project' in step else data
            return
        if 'calls' in step:
            if set(step) - {'id','when','calls'} or not isinstance(step['calls'],list):
                raise ValueError('批次字段无效')
            if dispatch.count + len(step['calls']) > 32:
                raise ValueError('程序工具调用超过32项')
            calls = [(call['tool'], resolve(call.get('arguments',{}),values,budget=budget),f'{index}:{item}')
                for item,call in enumerate(step['calls']) if isinstance(call,dict) and set(call) <= {'tool','arguments'}]
            if len(calls) != len(step['calls']):
                raise ValueError('批次调用字段无效')
        else:
            items = resolve(step['foreach'],values,budget=budget) if 'foreach' in step else [None]
            if not isinstance(items,list) or len(items)>32:
                raise ValueError('遍历范围无效')
            if dispatch.count + len(items) > 32:
                raise ValueError('程序工具调用超过32项')
            calls = [(step['tool'],resolve(step.get('arguments',{}),values,item,budget=budget),f'{index}:{item_index}')
                for item_index,item in enumerate(items)]
        results = dispatch.batch(calls, budget=budget)
        for result in results:
            trace.append({**{key:getattr(result,key) for key in ('call_id','tool_name','status','error_code','duration_ms')},
                'refs':[ref.to_dict() for ref in result.refs],
                'reported_error_code':result.reported_error_code, 'parent_call_id':dispatch.parent.call_id})
        rows = [self._value(result) for result in results]
        values[name] = rows if 'foreach' in step or 'calls' in step else rows[0]

    # LLM: 原ToolResult保留不改，完整JSON解析只增加数据投影，解析失败仍保留原output；不扫描文本片段当结构化事实。
    # 函数用途: 提供供下一步引用的工具结果及可选完整JSON值。
    @staticmethod
    def _value(result):
        from ..common.log_redaction import redact_sensitive_value
        if result.metadata.get('program_data', {}).get('budget_exceeded'):
            raise ValueError('中间数据超过预算')
        value = {'ok':result.ok,'status':result.status,'output':result.output,
            'error_code':result.error_code,'refs':[ref.to_dict() for ref in result.refs],
            'metadata':{'handler_details':redact_sensitive_value(result.metadata.get('handler_details',{}))}}
        if 'json' in result.metadata.get('program_data', {}):
            value['json'] = result.metadata['program_data']['json']
        return value


    # LLM: 完整result与refs留在原output归档；模型视图按UTF-8字节限制，所有子调用状态/错误始终保留，不能把失败藏在截断尾部。
    # 函数用途: 生成最多16KB的模型正文，超大引用交给外层原归档锚点读取。
    @staticmethod
    def _live_payload(payload):
        live = dict(payload)
        if len(json.dumps(live, ensure_ascii=False).encode()) > 16000:
            live.update(result=None, result_truncated=True)
        if len(json.dumps(live, ensure_ascii=False).encode()) > 16000:
            live['calls'] = [{**row, 'refs':[], 'refs_omitted':len(row['refs'])} for row in payload['calls']]
            live['details_in_output_archive'] = True
        if len(json.dumps(live, ensure_ascii=False).encode()) > 16000:
            live['calls'] = [{'call_id_sha256':hashlib.sha256(row['call_id'].encode()).hexdigest(),
                'status':row['status'], 'error_code':row['error_code'],
                'refs_omitted':len(row['refs'])} for row in payload['calls']]
            live['call_identity_in_output_archive'] = True
        return live
