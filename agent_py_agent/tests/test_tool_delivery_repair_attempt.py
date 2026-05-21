from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


# LLM: a fresh recovery attempt should not inherit the previous run's strict no-progress debt.
# 函数用途: 验证自动续跑刚开始时可以先读取已有产物定位补丁；这由结构化 attempt marker 决定。
def test_delivery_repair_guard_allows_read_when_recovery_attempt_is_newer_than_closeout(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_strict_artifact_closeout(tmp_path)
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

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is True


# LLM: recovery inspection budget should survive new closeout writes inside the same attempt.
# 函数用途: 验证恢复窗口按基线计数，而不是被第一次 closeout 更新时间戳立即关掉。
def test_delivery_repair_guard_keeps_read_window_within_recovery_attempt_budget(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_strict_artifact_closeout(tmp_path, unchanged_failure_count=6)
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

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is True


# LLM: once a resumed attempt spends its inspection budget, strict mode must apply again if failures are unchanged.
# 函数用途: 验证恢复窗口是有限预算，不会永久放宽阶段修复 guard。
def test_delivery_repair_guard_reapplies_strict_after_recovery_attempt_budget(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_strict_artifact_closeout(tmp_path, unchanged_failure_count=8)
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

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False


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


# LLM: _write_closeout keeps the delivery-repair fixture tiny and grounded in the same machine report the runtime uses.
# 函数用途: 向测试工作区写入 .agent_delivery/closeout.json，供 repair guard 直接读取。
def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
