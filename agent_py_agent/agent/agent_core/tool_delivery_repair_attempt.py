# LLM: Recovery-attempt budget logic is isolated from delivery repair classification.
# 模块用途: 读取结构化 recovery_attempt marker，决定自动恢复阶段是否还允许少量检查工具。

from __future__ import annotations

import json
from pathlib import Path


# LLM: strict_write_required upgrades the guard once unchanged failures have reached the no-progress threshold.
# 函数用途: 根据 unchanged_failure_count 和 no_progress_block_threshold 判断是否必须切到严格写入模式。
def strict_write_required(progress: dict[str, object], *, agent_root: Path) -> bool:
    if _within_recovery_attempt_inspection_budget(progress, agent_root=agent_root):
        return False
    unchanged = _safe_int(progress.get("unchanged_failure_count"))
    threshold = _safe_int(progress.get("no_progress_block_threshold"))
    return threshold > 0 and unchanged >= threshold


# LLM: A recovery attempt marker gives one fresh attempt an inspection window before strict repair resumes.
# 函数用途: 根据恢复 marker 的基线和预算判断是否还允许少量读取现状，而不继承旧失败债务。
def _within_recovery_attempt_inspection_budget(progress: dict[str, object], *, agent_root: Path) -> bool:
    marker_payload = _recovery_attempt_marker(agent_root)
    if marker_payload:
        return _progress_delta_within_budget(progress, marker_payload)
    return _fresh_recovery_attempt_by_mtime(agent_root)


# LLM: _recovery_attempt_marker keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _recovery_attempt_marker(agent_root: Path) -> dict[str, object]:
    marker = agent_root / ".agent_delivery" / "recovery_attempt.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != "delivery-recovery-attempt.v1":
        return {}
    return value


# LLM: _progress_delta_within_budget keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _progress_delta_within_budget(progress: dict[str, object], marker: dict[str, object]) -> bool:
    unchanged = _safe_int(progress.get("unchanged_failure_count"))
    baseline = _safe_int(marker.get("baseline_unchanged_failure_count"))
    budget = _safe_int(marker.get("inspection_round_budget"))
    return budget > 0 and unchanged <= baseline + budget


# LLM: _fresh_recovery_attempt_by_mtime keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _fresh_recovery_attempt_by_mtime(agent_root: Path) -> bool:
    closeout = agent_root / ".agent_delivery" / "closeout.json"
    marker = agent_root / ".agent_delivery" / "recovery_attempt.json"
    try:
        return marker.stat().st_mtime > closeout.stat().st_mtime
    except OSError:
        return False


# LLM: _safe_int keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
