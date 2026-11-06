# LLM: 内层工具始终重入原ToolExecutor，沿外层不可变owner/run/attempt/快照/取消/权限；程序不能传身份或获得写权限。
# 模块用途: 为有限只读编排提供宿主派发，复用原连续并行段计划和逐次工具审计事实。
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
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


# LLM: 仅由可信宿主挂在handler上，不进模型schema；回调只接触已脱敏且通过共享预算的正文。
# 类用途: 声明工具程序的可选解码与校验协议，缺省仍沿原JSON行为。
@dataclass(frozen=True)
class ProgramOutputContract:
    decode: Callable[[str], object]
    validate: Callable[[object], None] = lambda _: None


# LLM: 仅携带固定错误码和异常类型，不传播异常正文，也不重执行已完成的工具。
# 类用途: 在程序取值入口明确报告输出协议失败。
class ProgramOutputContractError(ValueError):
    """A host output contract rejected an already-executed tool result."""


# LLM: 标记只在实际DataBudget正文预留失败处产生，序列化与回调异常不能借共享余额冒充预算溢出。
# 类用途: 区分自定义协议的预算拒绝与数据错误，不改变旧JSON路径。
class _ProgramOutputBudgetExceeded(ValueError):
    """The shared budget rejected an already-serialized output chunk."""


# LLM: 流式编码沿DataBudget同一JSON配置逐块预留；编码异常原样交给协议错误分类，不先构造大副本。
# 函数用途: 为自定义解码值预留共享预算，并从实际预留入口标记预算失败。
def _reserve_program_output(value, budget):
    import json

    for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value):
        try:
            budget.reserve(chunk, text=True)
        except UnicodeError:
            raise
        except ValueError as exc:
            raise _ProgramOutputBudgetExceeded from exc


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

    # LLM: 只重入原executor与宿主runner，预算清理只移除解码值，安全协议错误及已执行事实不得被覆盖。
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
            captured.pop('json', None)
            captured['budget_exceeded'] = True
            result = replace(result, content_blocks=(), metadata={})
        observe = (self.request.trusted_run_context or {}).get('nested_tool_result_sink')
        if callable(observe):
            observe(call, result)
        return replace(result, metadata={**result.metadata, 'program_data':captured})

    # LLM: 先脱敏并预留共享预算；自定义解码值只从实际预留入口标记预算失败，拒绝调用不运行自定义协议。
    # 函数用途: 为下一步保留已校验数据或明确协议错误，默认仍解析原脱敏JSON。
    def _archive(self, child, outcome, *, captured, capture_budget):
        import json

        from .executor import _project_output
        from .models import output_policy_for_outcome
        from .output_projection import redact_tool_output_text
        runtime = self.request.runtime_snapshot.runtime(child.tool_name)
        policy = output_policy_for_outcome(runtime.runtime_policy, outcome)
        text = redact_tool_output_text(outcome.output, redaction=policy.redaction)
        contract = getattr(runtime.handler, 'program_output_contract', None)
        try:
            capture_budget.reserve(text, text=True)
            if contract is None:
                captured['json'] = json.loads(text)
            elif outcome.ok:
                try:
                    value = contract.decode(text)
                    _reserve_program_output(value, capture_budget)
                    contract.validate(value)
                    _reserve_program_output(value, capture_budget)
                    captured['json'] = value
                except _ProgramOutputBudgetExceeded:
                    captured['budget_exceeded'] = True
                except Exception as exc:
                    captured['contract_error'] = {'code': 'TOOL_PROGRAM_OUTPUT_INVALID',
                        'exception_type': type(exc).__name__}
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
