from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


# LLM: a fresh recovery attempt should not inherit no-progress debt when the target is still missing.
# 函数用途: 验证自动续跑刚开始时可以先查找缺失产物位置；这由结构化 attempt marker 决定。
def test_delivery_repair_guard_allows_read_when_recovery_attempt_is_newer_than_closeout(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_missing_artifact_closeout(tmp_path)
    marker = tmp_path / ".agent_delivery" / "recovery_attempt.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "delivery-recovery-attempt.v1",
                "packet_ref": "recovery_packet.json",
                "baseline_unchanged_failure_count": 4,
                "inspection_round_budget": 4,
            }
        ),
        encoding="utf-8",
    )
    closeout = tmp_path / ".agent_delivery" / "closeout.json"
    os.utime(closeout, (100, 100))
    os.utime(marker, (200, 200))
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/site"}]) is True


# LLM: recovery inspection budget should survive new closeout writes inside the same attempt.
# 函数用途: 验证恢复窗口按基线计数，而不是被第一次 closeout 更新时间戳立即关掉。
def test_delivery_repair_guard_keeps_read_window_within_recovery_attempt_budget(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_missing_artifact_closeout(tmp_path, unchanged_failure_count=6)
    marker = tmp_path / ".agent_delivery" / "recovery_attempt.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "delivery-recovery-attempt.v1",
                "packet_ref": "recovery_packet.json",
                "baseline_unchanged_failure_count": 4,
                "inspection_round_budget": 3,
            }
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/site"}]) is True


# LLM: once a resumed attempt spends its inspection budget, strict mode must apply again if failures are unchanged.
# 函数用途: 验证恢复窗口是有限预算，不会永久放宽阶段修复 guard。
def test_delivery_repair_guard_reapplies_strict_after_recovery_attempt_budget(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_missing_artifact_closeout(tmp_path, unchanged_failure_count=8)
    marker = tmp_path / ".agent_delivery" / "recovery_attempt.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "delivery-recovery-attempt.v1",
                "packet_ref": "recovery_packet.json",
                "baseline_unchanged_failure_count": 4,
                "inspection_round_budget": 3,
            }
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/site"}]) is False


# LLM: direct repair actions should not get a fresh read window from the attempt marker.
# 函数用途: 验证 recovery attempt 的检查预算由结构化恢复动作决定，不靠提示词劝模型少读。
def test_recovery_attempt_budget_is_zero_for_direct_artifact_repair(tmp_path: Path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        recovery_attempt_inspection_budget,
    )

    artifact = tmp_path / "outputs/site/index.html"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("<html></html>", encoding="utf-8")
    _write_strict_artifact_closeout(tmp_path)

    assert recovery_attempt_inspection_budget(tmp_path) == 0


def test_recovery_attempt_budget_allows_small_lookup_for_missing_artifact(tmp_path: Path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        recovery_attempt_inspection_budget,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_path": "outputs/report.xlsx",
                        "finding_codes": ["ARTIFACT_MISSING"],
                    }
                ],
            },
        },
    )

    assert recovery_attempt_inspection_budget(tmp_path) == 2


# LLM: a zero inspection budget should immediately force mutation even before failure count reaches threshold.
# 函数用途: 验证直接修复类 recovery attempt 不会因为 unchanged_failure_count 还低就放行检查工具。
def test_zero_inspection_budget_immediately_enforces_write(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_strict_artifact_closeout(tmp_path, unchanged_failure_count=1)
    marker = tmp_path / ".agent_delivery" / "recovery_attempt.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "delivery-recovery-attempt.v1",
                "packet_ref": "recovery_packet.json",
                "baseline_unchanged_failure_count": 1,
                "inspection_round_budget": 0,
            }
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/site"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/site/index.html", "content": "<!doctype html>"}],
        )
        is True
    )


def _write_strict_artifact_closeout(root: Path, *, unchanged_failure_count: int = 4) -> None:
    _write_closeout(
        root,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": unchanged_failure_count,
                "no_progress_block_threshold": 4,
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_values": ["index.html:href=styles.css"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ],
            },
        },
    )


def _write_missing_artifact_closeout(root: Path, *, unchanged_failure_count: int = 4) -> None:
    _write_closeout(
        root,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": unchanged_failure_count,
                "no_progress_block_threshold": 4,
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_path": "outputs/site/index.html",
                        "finding_codes": ["ARTIFACT_MISSING"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ],
            },
        },
    )


# LLM: _write_closeout keeps the delivery-repair fixture tiny and grounded in the same machine report the runtime uses.
# 函数用途: 向测试工作区写入 .agent_delivery/closeout.json，供 repair guard 直接读取。
def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
