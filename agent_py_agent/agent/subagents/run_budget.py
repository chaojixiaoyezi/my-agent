# LLM: Refs-only subagent runner budget summaries for large multi-agent E2E runs.
# 模块用途: 汇总一棵 subagent 任务树的模型调用、工具轮数和粗略 token，不读取大产物正文。

from __future__ import annotations

"""Refs-only budget summaries for subagent runner activity."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# LLM: SubagentRunBudgetRequest bundles report scope, limits, and future extension fields.
# 类用途: 定义预算查询入口；manager 提供任务读取，root_id 和阈值决定报告范围与红线。
@dataclass(frozen=True)
class SubagentRunBudgetRequest:
    """Request bundle for a subagent run budget report."""

    manager: Any
    root_id: str = ""
    max_model_calls: int = 0
    max_tool_rounds: int = 0
    max_prompt_response_tokens: int = 0
    include_dry_runs: bool = False
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: SubagentRunBudgetRecord is the per-run cost line with refs instead of prompt bodies.
# 类用途: 记录单个 runner 的预算摘要；只保存文件路径、计数和估算值，不展开 prompt/response 内容。
@dataclass(frozen=True)
class SubagentRunBudgetRecord:
    """Per-run budget line item."""

    run_id: str
    status: str
    dry_run: bool
    backend: str
    tool_rounds: int
    prompt_token_estimate: int
    response_token_estimate: int
    prompt_file: str
    response_file: str
    result_json: str


# LLM: SubagentRunBudgetReport keeps totals machine-readable for dashboards and real E2E logs.
# 类用途: 汇总预算查询结果；exceeded 明确列出超过的阈值，方便父代理决定是否降速或拆分。
@dataclass(frozen=True)
class SubagentRunBudgetReport:
    """Budget summary for a subagent run tree."""

    generated_at: float
    root_id: str
    totals: dict[str, int]
    limits: dict[str, int]
    exceeded: list[str]
    records: list[SubagentRunBudgetRecord] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: build_subagent_run_budget_report scans stored runner refs and never opens artifact payloads.
# 函数用途: 生成模型调用和 token 预算摘要；用于大型真实 E2E 前后确认调用没有失控。
def build_subagent_run_budget_report(request: SubagentRunBudgetRequest) -> SubagentRunBudgetReport:
    """Build a refs-only budget report from persisted subagent runner results."""

    records = [_budget_record(task) for task in _scoped_tasks(request)]
    records = [record for record in records if record is not None]
    if not request.include_dry_runs:
        records = [record for record in records if not record.dry_run]
    totals = _totals(records)
    limits = _limits(request)
    return SubagentRunBudgetReport(
        generated_at=time.time(),
        root_id=str(request.root_id or ""),
        totals=totals,
        limits=limits,
        exceeded=_exceeded(totals, limits),
        records=records,
        reserved=dict(request.reserved or {}),
    )


# LLM: _scoped_tasks keeps shared subagent workspaces from mixing unrelated task trees.
# 函数用途: 根据 root_id 过滤任务；root_id 为空时返回全部任务，便于本地全局预算查询。
def _scoped_tasks(request: SubagentRunBudgetRequest) -> list[Any]:
    root_id = str(request.root_id or "").strip()
    tasks = list(request.manager.list_runs())
    if not root_id:
        return tasks
    return [task for task in tasks if (str(getattr(task, "root_id", "") or getattr(task, "id", ""))) == root_id]


# LLM: _budget_record reads only runner result JSON and prompt/response file sizes.
# 函数用途: 将单个任务的 runner 文件转换成预算行；坏 JSON 或缺失文件会被静默跳过。
def _budget_record(task: Any) -> SubagentRunBudgetRecord | None:
    result_path = Path(str(getattr(task, "runner_result_json", "") or ""))
    if not result_path.is_file():
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    prompt_file = str(payload.get("prompt_file") or "")
    response_file = str(payload.get("response_file") or "")
    return SubagentRunBudgetRecord(
        run_id=str(payload.get("run_id") or getattr(task, "id", "")),
        status=str(payload.get("status") or getattr(task, "status", "")),
        dry_run=bool(payload.get("dry_run", False)),
        backend=str(payload.get("backend") or ""),
        tool_rounds=int(payload.get("tool_rounds") or 0),
        prompt_token_estimate=_file_token_estimate(prompt_file),
        response_token_estimate=_file_token_estimate(response_file),
        prompt_file=prompt_file,
        response_file=response_file,
        result_json=str(result_path),
    )


# LLM: _file_token_estimate avoids tokenizer dependencies and large reads.
# 函数用途: 用文件字节大小粗略估算 token；不存在文件按 0 处理，避免预算查询阻塞主流程。
def _file_token_estimate(path: str) -> int:
    if not path:
        return 0
    try:
        size = Path(path).stat().st_size
    except OSError:
        return 0
    return max(1, int(size / 4)) if size else 0


# LLM: _totals keeps every aggregate as an int for stable JSON and CLI display.
# 函数用途: 汇总预算行，模型调用数按非 dry-run runner 记录数量估算。
def _totals(records: list[SubagentRunBudgetRecord]) -> dict[str, int]:
    return {
        "runs": len(records),
        "model_calls": sum(1 for record in records if not record.dry_run),
        "tool_rounds": sum(record.tool_rounds for record in records),
        "prompt_token_estimate": sum(record.prompt_token_estimate for record in records),
        "response_token_estimate": sum(record.response_token_estimate for record in records),
        "prompt_response_token_estimate": sum(
            record.prompt_token_estimate + record.response_token_estimate for record in records
        ),
    }


# LLM: _limits normalizes zero/negative values as disabled thresholds.
# 函数用途: 生成报告里的阈值字典，便于后续 CLI 和自动护栏共用。
def _limits(request: SubagentRunBudgetRequest) -> dict[str, int]:
    return {
        "model_calls": max(0, int(request.max_model_calls or 0)),
        "tool_rounds": max(0, int(request.max_tool_rounds or 0)),
        "prompt_response_token_estimate": max(0, int(request.max_prompt_response_tokens or 0)),
    }


# LLM: _exceeded returns stable field names so callers can route budget warnings without parsing text.
# 函数用途: 对比 totals 和 limits；阈值为 0 时视为未启用。
def _exceeded(totals: dict[str, int], limits: dict[str, int]) -> list[str]:
    return [key for key, limit in limits.items() if limit > 0 and totals.get(key, 0) > limit]
