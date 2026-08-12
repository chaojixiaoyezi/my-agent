
from __future__ import annotations

"""后台进程管理工具 —— list_processes / process_status / kill_process。

给模型一套精确 schema 的工具,管住 run_command(run_in_background=true) 起的后台
进程(进程注册表见 process_registry.py)。对标 长期助手 的 process(action=...) 单工具
多动作,这里拆成三个独立工具,因为 my-agent 走 native tool_use + 精确 input_schema,
独立工具比"一个工具靠 action 字段分流"对模型更直白(每个工具的必填参数/语义单一,
不会出现 list 误填 session_id 之类的歧义)。
"""

import json
from typing import Any

from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .process_registry import process_registry

_PROCESS_NOT_FOUND_HINT = (
    "没有这个 session_id 的后台进程。先用 list_processes 看当前有哪些后台进程及其 session_id;"
    "session_id 来自 run_command(run_in_background=true) 的返回。"
)


def _coerce_session_id(params: dict[str, Any]) -> str:
    # 有的模型会把 session_id 发成数字或带空白,统一成裸字符串。
    value = params.get("session_id")
    return str(value).strip() if value is not None else ""


class ListProcessesTool(BaseTool):
    """列出所有登记的后台进程及其状态。"""

    model_spec = ToolModelSpec(
        name="list_processes",
        description="列出所有用 run_command(run_in_background=true) 启动并登记的后台进程及其状态。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "启动了一个或多个后台任务后,想总览它们现在是运行中还是已结束",
                "忘了某个后台进程的 session_id,先列出来找到它",
                "收尾前确认是否还有后台进程在跑,需要 kill_process 终止",
            ),
            avoid_when=(
                "只关心某一个进程的详细状态和输出时,直接用 process_status",
                "前台一次性命令用 run_command 即可,不涉及后台进程",
            ),
            keywords=("进程", "后台", "process", "list", "jobs", "background", "ps", "任务列表"),
            examples=('{"tool": "list_processes"}',),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("process_registry",)),
    )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        processes = process_registry.list()
        payload = {"count": len(processes), "processes": processes}
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(payload, ensure_ascii=False))


class ProcessStatusTool(BaseTool):
    """查单个后台进程的状态 + 最近输出(日志尾部)。"""

    model_spec = ToolModelSpec(
        name="process_status",
        description="查单个后台进程的状态(运行中/已退出/已终止)、退出码和最近输出(日志尾部)。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "必填。run_command(run_in_background=true) 返回或 list_processes 列出的后台进程 session_id。",
                }
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "查某个后台任务是否还在跑、是否已经结束、退出码是多少",
                "看后台任务最近的输出/日志尾部判断进度,而不用 read_file 整文件读",
                "wait 一段时间后回来确认某个后台进程的进展",
            ),
            avoid_when=("想看所有后台进程总览时用 list_processes", "要终止进程时用 kill_process"),
            keywords=("进程", "状态", "process", "status", "poll", "后台", "输出", "进度", "日志"),
            examples=('{"tool": "process_status", "session_id": "bg-1-1718500000"}',),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id",),
            parameter_kinds={"session_id": "logical"},
        ),
    )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = _coerce_session_id(params)
        if not session_id:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                "TOOL_INVALID_ARGUMENTS: process_status 需要 session_id 参数。",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        status = process_registry.status(session_id)
        if status is None:
            return _process_not_found(self.model_spec.name, session_id)
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(status, ensure_ascii=False))


class KillProcessTool(BaseTool):
    """终止一个后台进程(SIGTERM→超时 SIGKILL,杀整个进程组)。"""

    model_spec = ToolModelSpec(
        name="kill_process",
        description="终止一个后台进程:先 SIGTERM,宽限后仍未退出再 SIGKILL,作用于整个进程组(连带子进程一起杀)。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "必填。run_command(run_in_background=true) 返回或 list_processes 列出的后台进程 session_id。",
                }
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "后台任务跑偏/卡死/不再需要,按 session_id 把它和它起的子进程一起终止",
                "收尾阶段清理还在运行的后台进程",
            ),
            avoid_when=(
                "只想查看状态而不终止时用 process_status",
                "进程已经结束就不必再 kill(kill_process 会直接返回 already_exited)",
            ),
            keywords=("进程", "终止", "杀", "kill", "stop", "terminate", "后台", "收尾", "进程组"),
            examples=('{"tool": "kill_process", "session_id": "bg-1-1718500000"}',),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id",),
            parameter_kinds={"session_id": "logical"},
        ),
    )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        session_id = _coerce_session_id(params)
        if not session_id:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                "TOOL_INVALID_ARGUMENTS: kill_process 需要 session_id 参数。",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        result = process_registry.kill(session_id)
        if result is None:
            return _process_not_found(self.model_spec.name, session_id)
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(result, ensure_ascii=False))


def _process_not_found(tool_name: str, session_id: str) -> ToolHandlerOutcome:
    payload = {
        "ok": False,
        "error": "process_not_found",
        "session_id": session_id,
        "message": _PROCESS_NOT_FOUND_HINT,
    }
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="PROCESS_NOT_FOUND",
    )


__all__ = ["ListProcessesTool", "ProcessStatusTool", "KillProcessTool"]
