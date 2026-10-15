# LLM: 内层工具始终重入原ToolExecutor，沿外层不可变owner/run/attempt/快照/取消/权限；程序不能传身份或获得写权限。
# 模块用途: 为有限只读编排提供宿主派发，复用原连续并行段计划和逐次工具审计事实。
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace

from .concurrency import describe_tool_concurrency
from .input_schema import normalize_tool_input
from .models import tool_effect_for_runtime_policy
from .program_data import DataBudget
from .runtime_contracts import ToolCall


# LLM: 仅由执行器构造，不能放入模型参数或共享handler；每批重新校验动态效果，原权限门仍逐调用执行。
# 类用途: 保存原调用边界并执行最多32个只读调用，派发结果作为外层原工具账的结构化子记录。
class NestedToolDispatcher:
    # LLM: 只保存本次请求，不复制配置或授权；max_parallel沿本次宿主固定值，0沿原不限制语义。
    # 函数用途: 初始化原调用派生器，无网络、文件或工具执行。
    def __init__(self, request, parent_call):
        self.request, self.parent = request, parent_call
        self.count = 0
        self.groups = 0

    # LLM: 工具名/参数是数据，schema和执行身份只读本轮快照；不接受嵌套程序、隐藏工具或有副作用的动作。
    # 函数用途: 派生一条准确内部ToolCall，在执行前验证它具备只读效果。
    def _call(self, name, arguments, key):
        runtime = self.request.runtime_snapshot.runtime(name)
        if name == 'tool_program' or runtime is None or name not in self.request.runtime_snapshot.available_tool_names:
            raise ValueError('程序包含不可用工具')
        normalized = normalize_tool_input(arguments, runtime.model_spec.input_schema).value
        if tool_effect_for_runtime_policy(runtime.runtime_policy, normalized) != 'read_only':
            raise ValueError('程序只允许只读工具动作')
        return ToolCall(call_id=f'{self.parent.call_id}/{key}', tool_name=name, arguments=arguments,
            source_protocol=self.parent.source_protocol, schema_hash=runtime.model_spec.schema_hash,
            run_id=self.parent.run_id, turn_id=self.parent.turn_id, attempt_id=self.parent.attempt_id)

    # LLM: 段选择仍用原parallel_segment_end；前置与handler取消、网络和文件权限全部由原执行器判断，不调用handler捷径。
    # 函数用途: 执行共享累计数据预算的批次，沿原线程身份协议并按顺序保留全部已尝试调用。
    def batch(self, entries, *, budget=None):
        from ..agent_core.tool_loop.segment_planning import parallel_segment_end
        from .executor import ToolExecutor
        if self.count + len(entries) > 32:
            raise ValueError('程序工具调用超过32项')
        capture_budget = budget or DataBudget()
        calls = [self._call(name, args, key) for name, args, key in entries]
        self.count += len(calls)
        # LLM: 只读原快照和本次范围，调度描述不产生授权。
        # 函数用途: 按原调度协议描述一个候选调用。
        def describe(call):
            return describe_tool_concurrency(self.request.runtime_snapshot, call,
                workspace_root=self.request.workspace_root, write_boundary=self.request.write_boundary)
        limit = max(0, int((self.request.trusted_run_context or {}).get('max_parallel_tool_calls', 8)))
        results, position = [], 0
        while position < len(calls):
            end = parallel_segment_end(calls, position, batch_limit=limit,
                defer_for_compact=lambda _name: False, describe_call=describe)
            end = max(position + 1, end)
            segment = calls[position:end]
            self.groups += 1
            # LLM: 原request保留权限/插件/输出归档，取消上下文逐线程copy，子调用不再获得编排派发器。
            # 函数用途: 用唯一工具执行器执行段中的一条调用，返回原生结构化结果。
            def execute(call):
                captured = {}
                # LLM: 数据投影读取原handler完整值并沿原脱敏规则；归档仍调用原archiver，不能从模型预览猜JSON。
                # 函数用途: 捕获本步安全JSON供下一步机械引用，同时保留原完整证据归档。
                def archive(child, outcome):
                    import json

                    from .executor import _project_output
                    from .models import output_policy_for_outcome
                    from .output_projection import redact_tool_output_text
                    runtime = self.request.runtime_snapshot.runtime(child.tool_name)
                    policy = output_policy_for_outcome(runtime.runtime_policy, outcome)
                    text = redact_tool_output_text(outcome.output, redaction=policy.redaction)
                    try:
                        capture_budget.reserve(text, text=True)
                        captured['json'] = json.loads(text)
                    except ValueError:
                        captured['budget_exceeded'] = capture_budget.remaining < 0
                    except TypeError:
                        pass
                    return _project_output(self.request, child, runtime, outcome)
                request = replace(self.request, call=call, allow_nested_dispatch=False, output_archiver=archive)
                runner = (self.request.trusted_run_context or {}).get('nested_tool_thread_runner')
                # LLM: 可信宿主runner负责线程身份；实际工具仍只进入原执行器。
                # 函数用途: 执行当前子调用并返回原生结果。
                def invoke():
                    return ToolExecutor().execute(request).result
                result = runner(invoke) if callable(runner) else invoke()
                try:
                    capture_budget.reserve({'output':result.output, 'metadata':result.metadata})
                except (ValueError, TypeError):
                    captured.clear()
                    captured['budget_exceeded'] = True
                    result = replace(result, content_blocks=(), metadata={})
                observe = (self.request.trusted_run_context or {}).get('nested_tool_result_sink')
                if callable(observe):
                    observe(call, result)
                return replace(result, metadata={**result.metadata, 'program_data':captured})
            if len(segment) > 1:
                with ThreadPoolExecutor(max_workers=len(segment)) as pool:
                    futures = [pool.submit(copy_context().run, execute, call) for call in segment]
                    results.extend(future.result() for future in futures)
            else:
                results.append(execute(segment[0]))
            position = end
        return results
