# LLM: 内层工具始终重入原ToolExecutor，沿外层不可变owner/run/attempt/快照/取消/权限；程序不能传身份或获得写权限。
# 模块用途: 为有限只读编排提供宿主派发，复用原连续并行段计划和逐次工具审计事实。
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

from .concurrency import describe_tool_concurrency
from .input_schema import normalize_tool_input
from .models import tool_effect_for_runtime_policy
from .owner_tool_scheduler import (
    ToolWorkItem,
    current_owner_scheduler,
    current_worker_sqlite_reuse,
    execute_work_items,
)
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

    # LLM: 段选择沿原规划；同owner真实worker内联防自等，异owner不可借许可；每次仍重入原executor。
    # 函数用途: 执行共享累计数据预算的批次，沿原线程身份协议并按顺序保留全部已尝试调用。
    def batch(self, entries, *, budget=None):
        from .segment_planning import parallel_segment_end
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
            results.extend(self._segment(segment, capture_budget))
            position = end
        return results

    # LLM: 每子调用独立Context/SQLite；只继承布尔配置，同owner许可内联，legacy仍原段池/屏障。
    # 函数用途: 执行一段只读子调用，按原配对收集全部在途结果。
    def _segment(self, segment, capture_budget):
        scheduler = _nested_scheduler(self.request)
        items = [ToolWorkItem(call.call_id, partial(self._execute, call, capture_budget),
                              sqlite_reuse=current_worker_sqlite_reuse()) for call in segment]
        if scheduler is not None:
            return execute_work_items(items, scheduler.submit)
        if len(items) == 1:
            return [items[0].captured_context.run(items[0].run)]
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            return execute_work_items(items, lambda item: pool.submit(item.captured_context.run, item.run))

    # LLM: 只重入原executor与宿主runner，权限/取消/完整输出归档不旁路，子项不可再编排。
    # 函数用途: 执行一条子调用并保留原生结果与结构化程序数据。
    def _execute(self, call, capture_budget):
        from .executor import ToolExecutor
        captured = {}
        request = replace(self.request, call=call, allow_nested_dispatch=False,
                          output_archiver=partial(self._archive, captured=captured, capture_budget=capture_budget))
        runner = (self.request.trusted_run_context or {}).get('nested_tool_thread_runner')
        # LLM: 原宿主runner安装线程身份，不由模型指定；返回原执行器结果。
        # 函数用途: 执行当前子工具。
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

    # LLM: 捕获真实handler脱敏值，预算超限不保留部分JSON；完整证据仍由原archiver处理。
    # 函数用途: 为下一步数据引用保留安全JSON并投影原始工具输出。
    def _archive(self, child, outcome, *, captured, capture_budget):
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


# LLM: 根来自原executor宿主字段，不看模型参数；异owner显式拒绝，避免跨owner相互等待形成环。
# 函数用途: 验证内层请求可否沿用当前worker许可。
def _nested_scheduler(request):
    current = current_owner_scheduler()
    if current is None:
        return None
    root = (request.write_boundary or {}).get("canonical_owner_home_root") or request.owner_scope_root
    if not root or current_owner_scheduler(root) is not current:
        raise ValueError("嵌套工具owner与worker许可不一致")
    return current
