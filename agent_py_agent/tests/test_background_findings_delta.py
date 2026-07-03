"""逐条结论送达(§2 不挨条报):唤醒轮工具集含 record_finding;结论账增量机制层附进出站消息。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.conversation.runtime import (
    SCHEDULED_BACKGROUND_ALLOWED_TOOLS,
    SUBAGENT_INTEGRATION_ALLOWED_TOOLS,
    _content_with_findings_delta,
    _scheduled_continuation_prompt,
)


def _agent_with_ledger(tmp_path: Path, records: list[dict], *, relpath=("work", "shared", "findings.jsonl")) -> SimpleNamespace:
    ledger = tmp_path.joinpath("taskws", *relpath)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return SimpleNamespace(_current_run_task_workspace=str(tmp_path / "taskws"))


def _write_ledger(agent: SimpleNamespace, records: list[dict], *, relpath) -> None:
    ledger = Path(agent._current_run_task_workspace, *relpath)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def _record(finding_id: str, claim: str, created_at: float, refs: list[str] | None = None) -> dict:
    return {"id": finding_id, "claim": claim, "created_at": created_at, "evidence_refs": refs or []}


def test_record_finding_present_in_background_tool_profiles() -> None:
    assert "record_finding" in SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    assert "record_finding" in SUBAGENT_INTEGRATION_ALLOWED_TOOLS


def test_scheduled_prompt_demands_per_item_findings() -> None:
    prompt = _scheduled_continuation_prompt("progress_policy_due")
    assert "record_finding" in prompt
    assert "逐条" in prompt


def test_findings_delta_appended_to_outbound_content(tmp_path) -> None:
    started = time.time()
    agent = _agent_with_ledger(
        tmp_path,
        [
            _record("rf-old", "上轮已报", started - 300.0),
            _record("rf-a", "条目 EVT-1 命中:结果端取值异常", started + 0.2, ["EVT-1"]),
            _record("rf-b", "条目 EVT-2 命中:响应端确认", started + 0.4),
        ],
    )

    stored, send = _content_with_findings_delta(agent, "本轮盯守小结。", started)

    assert stored == send
    assert stored.startswith("本轮盯守小结。")
    assert "rf-a" in send and "EVT-1" in send
    assert "rf-b" in send
    assert "rf-old" not in send
    assert "本轮新增 2 条" in send


def test_internal_marker_response_sends_findings_alone(tmp_path) -> None:
    started = time.time()
    agent = _agent_with_ledger(tmp_path, [_record("rf-x", "条目 EVT-9 命中", started + 0.1)])
    marker_response = '[RUN_NONBLOCKING_YIELD]\n{"mode":"non_blocking_yield"}'

    stored, send = _content_with_findings_delta(agent, marker_response, started)

    assert stored.startswith("[RUN_NONBLOCKING_YIELD]")
    assert "rf-x" in stored
    assert send.startswith("【逐条结论")
    assert "[RUN_NONBLOCKING_YIELD]" not in send


def test_no_new_findings_leaves_content_unchanged(tmp_path) -> None:
    started = time.time()
    agent = _agent_with_ledger(tmp_path, [_record("rf-old", "旧结论", started - 60.0)])

    stored, send = _content_with_findings_delta(agent, "无新情况。", started)

    assert stored == "无新情况。" and send == "无新情况。"


def test_missing_workspace_or_ledger_is_graceful(tmp_path) -> None:
    started = time.time()
    no_ws = SimpleNamespace(_current_run_task_workspace="")
    assert _content_with_findings_delta(no_ws, "原文", started) == ("原文", "原文")

    empty_ws = SimpleNamespace(_current_run_task_workspace=str(tmp_path / "nowhere"))
    assert _content_with_findings_delta(empty_ws, "原文", started) == ("原文", "原文")


def test_model_written_output_ledger_with_ts_and_synthesized_claim(tmp_path) -> None:
    # 真机实锤:record_finding 属 deferred,模型转而 write_file 逐条落 output/findings.jsonl,
    # 自定 schema(event_id + ts 字符串时戳 + 无 claim)。送达层须容忍并从结构字段合成一行。
    started = time.time()
    agent = _agent_with_ledger(
        tmp_path,
        [
            {"event_id": "EVT-000150", "outcome": "diverted ref=abc t=150",
             "result_field": "outcome.log", "hit": True,
             "confidence": "minority_field_value: token=s1:diverted", "ts": str(started + 0.3)},
        ],
        relpath=("output", "findings.jsonl"),
    )

    _stored, send = _content_with_findings_delta(agent, "本轮小结:计数在涨。", started)

    assert "EVT-000150" in send
    assert "diverted" in send  # 从 outcome/confidence 合成的依据
    assert "本轮新增 1 条" in send


def test_dedups_same_finding_across_both_ledgers(tmp_path) -> None:
    started = time.time()
    agent = _agent_with_ledger(
        tmp_path, [_record("EVT-9", "命中 EVT-9", started + 0.1)],
        relpath=("work", "shared", "findings.jsonl"),
    )
    _write_ledger(
        agent, [{"event_id": "EVT-9", "outcome": "diverted", "ts": str(started + 0.2)}],
        relpath=("output", "findings.jsonl"),
    )

    _stored, send = _content_with_findings_delta(agent, "小结。", started)

    assert send.count("\n- ") == 1, "两账本同一标识只送达一条"
    assert "本轮新增 1 条" in send


def test_delta_line_cap_points_to_ledger(tmp_path) -> None:
    started = time.time()
    records = [_record(f"rf-{i}", f"条目 EVT-{i} 命中", started + 0.1) for i in range(60)]
    agent = _agent_with_ledger(tmp_path, records)

    _stored, send = _content_with_findings_delta(agent, "", started)

    assert "本轮新增 60 条" in send
    assert send.count("\n- ") == 50
    assert "另有 10 条" in send
