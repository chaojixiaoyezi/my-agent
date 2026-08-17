"""模块用途: clock.sleep 工具——模型主动定时等待(扫描治理第二步, 对齐 会话运行时
handlers/sleep.rs)。

模型干完一段说"我睡 X 秒", 系统写 wake_queue 字条(kind=sleep)后模型回合
正常结束——不是进程内打盹(10 万用户挂进程不成立), 而是跨进程睡觉: 调度器
热层到期字条唤醒; 期间有事件(用户消息/子代理完成/wake signal)由事件通道
提前唤醒(字条被取消, 闹钟作废)。EXEC-39 语义不受影响: sleep 是模型主动
短期待机, 不是系统自动续跑。改动契约: wake_queue 仓储方法签名见
runtime_db/repository.py; 事件取消桥在 conversation/runtime.py。
"""
from __future__ import annotations

import json
import time
from typing import Any

from ...tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_TOOL_NAME = "sleep"
_MIN_SECONDS = 1
# 对齐 会话运行时 MAX_SLEEP_DURATION_MS = 12h
_MAX_SECONDS = 12 * 60 * 60


def build_sleep_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_TOOL_NAME,
        description=(
            "定时等待：结束本轮并安排系统在 N 秒后重新唤醒你继续当前任务。"
            "适合等待外部条件(定时任务/部署生效/数据延迟)时使用。"
            "等待期间有新输入(用户消息/子代理完成)会提前唤醒。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "minimum": _MIN_SECONDS,
                    "maximum": _MAX_SECONDS,
                    "description": f"睡多久后唤醒，秒。范围 {_MIN_SECONDS}..{_MAX_SECONDS}。",
                },
                "reason": {
                    "type": "string",
                    "description": "简短说明等待什么(留给醒来后的自己看)。",
                },
            },
            "required": ["duration_seconds"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="clock",
            use_cases=(
                "等待定时任务/部署/数据延迟生效后继续",
                "任务暂时没有可推进的工作, 定个时间再回来",
            ),
            avoid_when=(
                "可以立刻推进或已有后台执行者时会自己醒来",
                "等待某个子代理完成——用 wait 工具, 不用 sleep",
            ),
            keywords=("sleep", "等待", "定时唤醒", "稍后继续"),
        ),
    )


class SleepTool(BaseTool):
    """模型主动定时等待; 落 wake_queue 字条, 事件可提前唤醒。"""

    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=(),
        ),
        input_policy=ToolInputPolicy(internal_parameters=("__run_scope",)),
    )

    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.model_spec = build_sleep_model_spec()

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        run_scope = params.get("__run_scope") if isinstance(params, dict) else None
        task_id = str((run_scope or {}).get("task_id") or "").strip()
        run_id = str((run_scope or {}).get("run_id") or "").strip()
        if not task_id and not run_id:
            return ToolHandlerOutcome(
                _TOOL_NAME, False, "无法定位当前任务身份, 无法安排唤醒",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        duration = _seconds(params.get("duration_seconds"))
        if duration is None:
            return ToolHandlerOutcome(
                _TOOL_NAME, False,
                f"duration_seconds 必须是 {_MIN_SECONDS}..{_MAX_SECONDS} 的整数",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        repo = getattr(getattr(self.agent, "subagents", None), "runtime_db", None)
        if repo is None or not callable(getattr(repo, "upsert_wake", None)):
            return ToolHandlerOutcome(
                _TOOL_NAME, False, "当前运行环境没有权威账本, 无法安排唤醒",
                error_code="SUBAGENT_CAPACITY_UNAVAILABLE",
            )
        try:
            wake = repo.upsert_wake(
                root_task_id=task_id or run_id,
                root_run_id=run_id,
                next_due_at=time.time() + duration,
                kind="sleep",
            )
        except Exception as exc:  # noqa: BLE001 字条写失败如实返回
            return ToolHandlerOutcome(
                _TOOL_NAME, False, f"唤醒字条写入失败: {type(exc).__name__}",
                error_code="CONVERSATION_PERSISTENCE_UNAVAILABLE",
            )
        reason = str(params.get("reason") or "").strip()
        message = (
            f"已安排 {duration} 秒后唤醒你继续当前任务(wake_id={wake['wake_id']})。"
            "本轮结束; 等待期间有新输入(用户消息/子代理完成)会提前唤醒。"
            + (f" 等待原因: {reason}" if reason else "")
        )
        return ToolHandlerOutcome(
            _TOOL_NAME, True, json.dumps({"ok": True, "message": message}, ensure_ascii=False)
        )


def _seconds(value: object) -> int | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if not (_MIN_SECONDS <= seconds <= _MAX_SECONDS):
        return None
    return seconds
