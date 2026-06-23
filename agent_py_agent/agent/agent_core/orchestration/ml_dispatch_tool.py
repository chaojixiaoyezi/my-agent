
from __future__ import annotations

"""log_ml_dispatch:ML 评级 → 真实子代理派工(评级落地为行动)。

跑 ML 漏斗(候选→降维→三路评级)→ coordination.plan_dispatch 算派工意图 → _execute_create_subagents
真派工:**按 ML 评分自动决定派几个子代理、每个盯哪个 domain**,替代夜测暴露的"派工靠主代理 LLM 拍脑袋
/默认 single_worker"。每个子代理 goal 含该域簇+证据钩子+研判要求;defer_start(创建任务,启动交给
dispatch 机制,不在派工时同步跑 LLM)。CRITICAL 簇在结果里报告升级(自动开 collaboration case 留 M2 后续)。

放 orchestration 层(单向依赖 ml_engine,不让 ml_engine 反依赖 agent 成环)。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...ml_engine import coordination, engine, feedback
from ...ml_engine.reducer import reduce_by_entity, reduce_candidates
from ...tooling.log_ops.store import LogOpsStore
from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ...core import SimpleAgent

_DEFAULT_SUBDIR = ".log_ops"
_READ_CANDIDATES_CAP = 200000


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def build_log_ml_dispatch_spec() -> ToolSpec:
    return ToolSpec(
        name="log_ml_dispatch",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把 ML 评级转成真实子代理派工:跑 ML 漏斗(候选→降维→三路评级),**按 ML 评分自动决定派几个子代理、"
            "每个盯哪个源 domain**(非 LOW 按 domain 聚合,受 max_runners),替代'派工靠主代理 LLM 拍脑袋/默认 "
            "single_worker'。每个子代理 goal 含该域簇+证据钩子+研判要求;CRITICAL 簇在结果里标记待升级。"
            "defer_start:创建派工任务,启动由 dispatch 机制接管(不在此同步跑)。"
        ),
        use_cases=["候选积压且要按 ML 评级自动分诊派工时,一步派出子代理研判各 critical/high 域"],
        avoid_when=["只想看评级不派工用 log_ml_analyze", "手动指定派谁/派几个用 create_subagents"],
        keywords=["ML派工", "ml_dispatch", "评级派工", "自动编排", "分诊派工", "coordination", "漏斗派工"],
        parameters={
            "max_runners": "最多派几个子代理,默认 8,上限 16",
            "limit": "参与派工的最多评级数,默认 200",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"max_runners": "可选整数,默认 8。", "limit": "可选整数,默认 200。", "monitor_id": "可选。"},
        parameter_schema={
            "max_runners": {"type": "integer", "minimum": 1, "maximum": 16},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "monitor_id": {"type": "string"},
        },
        required_parameters=[],
        examples=['{"tool": "log_ml_dispatch", "max_runners": 8}'],
    )


class LogMlDispatchTool(BaseTool):
    """ML 评级 → 真实子代理派工(持有 agent,调 create_subagents 完整 policy)。"""

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_log_ml_dispatch_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        try:
            return self._run(params if isinstance(params, dict) else {})
        except (LookupError, ValueError, TypeError) as exc:
            return ToolExecutionResult(self.spec.name, False, f"log_ml_dispatch 参数无效:{exc}", error_code="TOOL_INVALID_ARGUMENTS")
        except Exception as exc:  # noqa: BLE001 — 任何运行时异常给精确可重试码,不逃逸成 UNKNOWN_ERROR
            return ToolExecutionResult(self.spec.name, False, f"log_ml_dispatch 执行异常:{exc}", error_code="TOOL_EXECUTION_FAILED")

    def _plan(self, params: dict[str, Any]) -> coordination.DispatchDirective:
        monitor_id = str(params.get("monitor_id") or "default").strip() or "default"
        max_runners = _coerce_int(params.get("max_runners"), 8, 1, 16)
        limit = _coerce_int(params.get("limit"), 200, 1, 1000)
        store = LogOpsStore(Path(self.agent.root) / _DEFAULT_SUBDIR, monitor_id)
        candidates = store.read_candidates(offset=0, limit=_READ_CANDIDATES_CAP)
        clusters = reduce_candidates(candidates) + reduce_by_entity(candidates)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, feedback.read_labels(store.root))
        result = engine.analyze(clusters, weights=weights, limit=limit)
        return coordination.plan_dispatch(list(result.assessments), max_runners=max_runners)

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        directive = self._plan(params)
        if not directive.worker_items:
            return self._envelope(True, {
                "ok": True, "dispatched": 0, "skipped_low": directive.skipped_low,
                "escalations": directive.escalations, "message": "无非 LOW 信号,未派工。",
            })
        from ..orchestration_tools import _execute_create_subagents

        create = _execute_create_subagents(self.agent, {"items": directive.worker_items, "defer_start": True})
        return self._envelope(create.ok, {
            "ok": create.ok,
            "dispatched": len(directive.worker_items),
            "skipped_low": directive.skipped_low,
            "escalations": directive.escalations,
            "create_result": create.result_envelope or {},
            "message": f"ML 评级驱动派出 {len(directive.worker_items)} 个子代理研判;{len(directive.escalations)} 个 CRITICAL 待升级。",
        })

    def _envelope(self, ok: bool, payload: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, ok, json.dumps(payload, ensure_ascii=False), result_envelope=payload)


__all__ = ["LogMlDispatchTool", "build_log_ml_dispatch_spec"]
