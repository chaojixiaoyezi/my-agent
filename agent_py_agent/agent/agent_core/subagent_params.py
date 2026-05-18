# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from dataclasses import dataclass


# LLM: SubagentRunParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存子代理run参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubagentRunParams:
    run_id: str
    instruction: str = ""
    dry_run: bool = True
    max_cards: int = 0
    probe: bool = True
    retry_reason: str = ""
    attempt_id: str = ""


# LLM: SpawnSubagentsParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存spawn子代理参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SpawnSubagentsParams:
    goal: str
    count: int | None = None
    role: str = "worker"
    agent_name: str = ""
    extra_write_roots: list[str] | None = None


# LLM: SubagentProbeParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存子代理probe参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubagentProbeParams:
    run_id: str
    active_attempt_id: str
    max_cards: int
    instruction: str
    probe: bool


# LLM: SubagentRunFailureParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存子代理run失败参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubagentRunFailureParams:
    run_id: str
    active_attempt_id: str
    exc: Exception
    context: object
    prompt: str


# LLM: SubagentFinalizeParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存子代理finalize参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubagentFinalizeParams:
    run_id: str
    active_attempt_id: str
    result: object
    context: object
    prompt: str


# LLM: subagent_run_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理子代理run参数相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def subagent_run_params(
    params: SubagentRunParams | None,
    *,
    run_id: str | None,
    instruction: str = "",
    dry_run: bool = True,
    max_cards: int = 0,
    probe: bool = True,
    retry_reason: str = "",
    attempt_id: str = "",
) -> SubagentRunParams:
    if params is not None:
        if not isinstance(params, SubagentRunParams):
            raise TypeError("run_subagent() requires params: SubagentRunParams")
        return params
    # LLM: run_subagent 保留显式旧字段，核心流程只消费统一运行参数包。
    return SubagentRunParams(
        run_id=str(run_id or ""),
        instruction=str(instruction),
        dry_run=bool(dry_run),
        max_cards=int(max_cards),
        probe=bool(probe),
        retry_reason=str(retry_reason),
        attempt_id=str(attempt_id),
    )


# LLM: spawn_subagents_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理spawn子代理参数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def spawn_subagents_params(
    params: SpawnSubagentsParams | None,
    *,
    goal: str | None,
    count: int | None,
    role: str = "worker",
    agent_name: str = "",
    extra_write_roots: list[str] | None = None,
) -> SpawnSubagentsParams:
    if params is not None:
        if not isinstance(params, SpawnSubagentsParams):
            raise TypeError("spawn_subagents() requires params: SpawnSubagentsParams")
        return params
    # LLM: spawn_subagents keeps the old goal/count shape but immediately normalizes it.
    return SpawnSubagentsParams(
        goal=str(goal or ""),
        count=count,
        role=str(role or "worker"),
        agent_name=str(agent_name or ""),
        extra_write_roots=list(extra_write_roots or []),
    )
