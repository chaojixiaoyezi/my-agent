"""record_lesson 结构化经验通道：工具、账本、结果合并、候选与 S1 提案的离线合同（只用假件，不调真实 provider）。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from agent_py_agent.agent.agent_core.runner.prompts import (
    prepare_subagent_runner_prompt,
    render_subagent_runner_prompt,
)
from agent_py_agent.agent.agent_core.runtime.record_lesson_tool import (
    RecordLessonTool,
    build_record_lesson_model_spec,
)
from agent_py_agent.agent.capability.skill_proposals import SkillProposalService
from agent_py_agent.agent.conversation.background_tool_policy import background_allowed_tools
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents import SubAgentExecutionContext, lesson_ledger
from agent_py_agent.agent.subagents.lesson_ledger import (
    LEDGER_OK,
    LEDGER_OVERSIZED,
    LEDGER_UNREADABLE,
    LESSON_FIELD_LIMITS,
    MAX_LESSON_LEDGER_BYTES,
    MAX_LESSONS_PER_RUN,
    LessonFields,
    LessonIdentity,
    append_lesson_record,
    lesson_id_for,
    lesson_ledger_record,
    read_lesson_ledger,
    render_lesson_text,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
from agent_py_agent.agent.subagents.role_templates import RECORD_LESSON_TOOL, ROLE_BASE_TOOLS
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

GOAL = "整理批量处理脚本并总结经验"
LESSON_A = {
    "title": "zsh 下批量处理文件先用 find -print0",
    "when_to_use": "在 macOS zsh 里批量处理可能不存在的文件模式时",
    "procedure": "用 find ... -print0 | xargs -0 生成列表，不直接展开可能无匹配的 glob。",
    "applies_to": "macOS/zsh 的 shell 脚本任务",
}
LESSON_B = {
    "title": "改共享状态前先读版本",
    "when_to_use": "多个工作者并发写同一份状态文件时",
    "procedure": "先读取当前版本，再用精确版本做比较交换写入，冲突时重读重试。",
    "applies_to": "带版本号的 JSON 状态文件",
}


def _fields(values: dict[str, str]) -> LessonFields:
    return LessonFields(**{name: values[name] for name, _limit in LESSON_FIELD_LIMITS})


def _lesson(index: int) -> dict[str, str]:
    return {
        "title": f"第 {index} 条经验",
        "when_to_use": f"遇到第 {index} 类情况时",
        "procedure": f"按第 {index} 种做法处理。",
        "applies_to": "测试任务",
    }


def _runtime(tmp_path: Path, *, proposals: bool = False) -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    service = SkillProposalService(home)
    manager.skill_proposals = service if proposals else None
    created = manager.create_run(goal=GOAL, thought="记录经验", plan=["整理"])
    task = manager.load(created.id)
    agent = SimpleNamespace(subagents=manager)
    return SimpleNamespace(
        home=home, candidates=candidates, manager=manager, service=service,
        task=task, agent=agent, tool=RecordLessonTool(agent),
    )


@contextmanager
def _runner(agent: object, run_id: str, attempt_id: str = "attempt-1"):
    previous = set_current_subagent_context(agent, run_id=run_id, attempt_id=attempt_id)
    try:
        yield
    finally:
        restore_current_subagent_context(agent, previous)


def _record(ctx: SimpleNamespace, values: dict[str, object], attempt_id: str = "attempt-1"):
    with _runner(ctx.agent, ctx.task.id, attempt_id):
        return ctx.tool.execute(dict(values))


def _payload(outcome) -> dict:
    return json.loads(outcome.output)


def _rows(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _natural_result(ctx: SimpleNamespace, structured: SubAgentParsedOutput | None = None):
    return ctx.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=ctx.task.id,
            dry_run=False,
            ok=True,
            message="已完成",
            status="DONE",
            turn_end_reason="completed",
            structured_output=structured,
        )
    )


def _output(ctx: SimpleNamespace) -> dict:
    return json.loads(Path(ctx.manager.load(ctx.task.id).output_json).read_text(encoding="utf-8"))


def _work_log(ctx: SimpleNamespace) -> str:
    return Path(ctx.manager.load(ctx.task.id).work_log_file).read_text(encoding="utf-8")


# ---- 工具：身份、Schema、校验、幂等与上限 ----


def test_record_lesson_writes_one_entry_with_identity_from_runner_context(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    forged = {**LESSON_A, "run_id": "forged-run", "task_id": "forged-task", "attempt_id": "forged-attempt"}

    outcome = _record(ctx, forged, attempt_id="attempt-7")
    payload = _payload(outcome)
    ledger = ctx.task.agent_run_lessons_jsonl
    [row] = _rows(ledger)

    assert outcome.ok and payload["status"] == "recorded" and payload["recorded"] is True
    assert payload["ledger_ref"] == ledger
    assert Path(ledger).name == "lessons.jsonl"
    assert Path(ledger).parent == Path(ctx.task.agent_run_findings_jsonl).parent
    assert (row["run_id"], row["attempt_id"], row["task_id"]) == (
        ctx.task.id,
        "attempt-7",
        ctx.task.root_id or ctx.task.id,
    )
    assert row["id"] == payload["lesson_id"] == lesson_id_for(_fields(LESSON_A))
    assert {name: row[name] for name, _limit in LESSON_FIELD_LIMITS} == LESSON_A
    assert "forged" not in Path(ledger).read_text(encoding="utf-8")


def test_record_lesson_is_unavailable_without_a_child_run(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    main_thread = ctx.tool.execute(dict(LESSON_A))
    with _runner(ctx.agent, "run-that-does-not-exist"):
        unknown_run = ctx.tool.execute(dict(LESSON_A))
    bare_agent = SimpleNamespace()
    with _runner(bare_agent, ctx.task.id):
        no_manager = RecordLessonTool(bare_agent).execute(dict(LESSON_A))

    for outcome in (main_thread, unknown_run, no_manager):
        assert not outcome.ok
        assert (outcome.error_code, outcome.effect_outcome) == ("TOOL_UNAVAILABLE", "not_started")
        assert _payload(outcome)["reason"] == "no_subagent_run_ledger"
    assert not Path(ctx.task.agent_run_lessons_jsonl).exists()


def test_schema_accepts_only_the_four_bounded_lesson_fields() -> None:
    spec = build_record_lesson_model_spec()
    schema = spec.input_schema
    names = [name for name, _limit in LESSON_FIELD_LIMITS]

    assert spec.name == RECORD_LESSON_TOOL
    assert sorted(schema["properties"]) == sorted(names)
    assert sorted(schema["required"]) == sorted(names)
    assert schema["additionalProperties"] is False
    assert {name: schema["properties"][name]["maxLength"] for name in names} == dict(LESSON_FIELD_LIMITS)
    assert not {"run_id", "task_id", "attempt_id", "agent_id"} & set(schema["properties"])


@pytest.mark.parametrize(
    "case",
    [
        ({"title": None}, "TOOL_PARAMETER_REQUIRED", "title", "missing"),
        ({"when_to_use": " \n\t "}, "TOOL_PARAMETER_REQUIRED", "when_to_use", "missing"),
        ({"procedure": 123}, "TOOL_INVALID_ARGUMENTS", "procedure", "not_string"),
        ({"applies_to": "界" * 201}, "TOOL_INVALID_ARGUMENTS", "applies_to", "too_long"),
        ({"title": "t" * 121}, "TOOL_INVALID_ARGUMENTS", "title", "too_long"),
    ],
)
def test_invalid_fields_are_refused_before_any_write(tmp_path: Path, case) -> None:
    change, code, field, reason = case
    ctx = _runtime(tmp_path)
    values = {**LESSON_A, **change}
    values = {key: value for key, value in values.items() if value is not None}

    outcome = _record(ctx, values)
    payload = _payload(outcome)

    assert not outcome.ok
    assert (outcome.error_code, outcome.effect_outcome) == (code, "not_started")
    assert (payload["field"], payload["reason"]) == (field, reason)
    assert not Path(ctx.task.agent_run_lessons_jsonl).exists()


def test_field_bounds_are_inclusive(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    values = {name: "界" * limit for name, limit in LESSON_FIELD_LIMITS}

    outcome = _record(ctx, values)

    assert outcome.ok, outcome.output
    assert len(render_lesson_text(_fields(values))) <= 2000


def test_whitespace_is_normalized_into_single_line_fields(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    values = {**LESSON_A, "procedure": "1. 先 find\n2. 再 xargs\t\t处理\r\n"}

    outcome = _record(ctx, values)
    [row] = _rows(ctx.task.agent_run_lessons_jsonl)
    text = render_lesson_text(_fields(row))

    assert outcome.ok
    assert row["procedure"] == "1. 先 find 2. 再 xargs 处理"
    assert len(text.splitlines()) == 4


def test_identical_call_in_the_same_run_is_recorded_once(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)

    first = _payload(_record(ctx, LESSON_A))
    again = _record(ctx, LESSON_A)
    spaced = _record(ctx, {**LESSON_A, "title": f"  {LESSON_A['title']}  "})

    assert again.ok and spaced.ok
    for payload in (_payload(again), _payload(spaced)):
        assert (payload["status"], payload["recorded"], payload["lesson_id"]) == (
            "already_recorded",
            False,
            first["lesson_id"],
        )
        assert payload["lesson_count"] == 1
    assert len(_rows(ctx.task.agent_run_lessons_jsonl)) == 1


def test_per_run_count_cap_refuses_structurally_and_keeps_retries_idempotent(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    accepted = [_record(ctx, _lesson(index)) for index in range(MAX_LESSONS_PER_RUN)]

    refused = _record(ctx, _lesson(99))
    retry = _record(ctx, _lesson(0))
    payload = _payload(refused)

    assert all(item.ok for item in accepted)
    assert not refused.ok
    assert (refused.error_code, refused.reported_error_code, refused.effect_outcome) == (
        "TOOL_GUARDRAIL_DENIED",
        "LESSON_LIMIT_REACHED",
        "not_started",
    )
    assert (payload["status"], payload["lesson_count"], payload["lesson_limit"]) == (
        "limit_reached",
        MAX_LESSONS_PER_RUN,
        MAX_LESSONS_PER_RUN,
    )
    assert retry.ok and _payload(retry)["status"] == "already_recorded"
    assert len(_rows(ctx.task.agent_run_lessons_jsonl)) == MAX_LESSONS_PER_RUN


def test_per_run_byte_cap_refuses_without_writing(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    big = [{name: f"{index}" + "界" * (limit - 1) for name, limit in LESSON_FIELD_LIMITS} for index in range(3)]
    first, second = (_record(ctx, item) for item in big[:2])
    size_before = Path(ctx.task.agent_run_lessons_jsonl).stat().st_size

    refused = _record(ctx, big[2])
    payload = _payload(refused)

    assert first.ok and second.ok
    assert not refused.ok
    assert (refused.error_code, refused.reported_error_code, refused.effect_outcome) == (
        "TOOL_INVALID_ARGUMENTS",
        "LESSON_LEDGER_BYTES_EXCEEDED",
        "not_started",
    )
    assert (payload["status"], payload["ledger_byte_limit"]) == ("bytes_exceeded", MAX_LESSON_LEDGER_BYTES)
    assert Path(ctx.task.agent_run_lessons_jsonl).stat().st_size == size_before <= MAX_LESSON_LEDGER_BYTES
    assert len(_rows(ctx.task.agent_run_lessons_jsonl)) == 2


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    ledger = Path(ctx.task.agent_run_lessons_jsonl)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("not json\n", encoding="utf-8")

    outcome = _record(ctx, LESSON_A)

    assert not outcome.ok
    assert (outcome.error_code, outcome.effect_outcome) == ("TOOL_PERSISTENCE_FAILED", "not_started")
    assert ledger.read_text(encoding="utf-8") == "not json\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
def test_symlinked_ledger_is_never_followed(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    ledger = Path(ctx.task.agent_run_lessons_jsonl)
    # 目标是空文件（本身合法），拒绝只能来自符号链接判定，而不是坏行判定。
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.symlink_to(outside)

    outcome = _record(ctx, LESSON_A)
    report = read_lesson_ledger(str(ledger), run_id=ctx.task.id)

    assert not outcome.ok
    assert (outcome.error_code, outcome.effect_outcome) == ("TOOL_PERSISTENCE_FAILED", "not_started")
    assert _payload(outcome)["status"] == "ledger_unusable"
    assert outside.read_text(encoding="utf-8") == ""
    assert (report.status, report.entries) == (LEDGER_UNREADABLE, ())


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
def test_low_level_append_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    link = tmp_path / "lessons.jsonl"
    link.symlink_to(outside)

    with pytest.raises(OSError):
        lesson_ledger._append_line_no_follow(link, b"{}\n")

    assert outside.read_text(encoding="utf-8") == ""


# ---- 账本读回：复核、上限、去重 ----


def test_ledger_read_back_rejects_tampered_foreign_and_duplicate_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "run-1" / "lessons.jsonl"
    identity = LessonIdentity(run_id="run-1", attempt_id="a-1", task_id="root-1")
    valid = lesson_ledger_record(_fields(LESSON_A), identity, created_at=10.0)
    assert append_lesson_record(ledger, valid).status == "recorded"
    # 另一 run 的合法条目：id 与 valid 不同，只能靠 run 归属复核拒绝。
    foreign = lesson_ledger_record(_fields(LESSON_B), LessonIdentity("run-2", "a-9", "root-1"), created_at=11.0)
    bad_rows = [
        {**valid, "procedure": "被改过的做法"},
        foreign,
        {**valid, "title": f" {LESSON_A['title']}"},
        {**valid, "version": 2},
        {**valid, "version": True},
        dict(valid),
    ]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in bad_rows)
        handle.write("{broken\n")

    report = read_lesson_ledger(str(ledger), run_id="run-1")
    [entry] = report.entries

    assert report.status == LEDGER_OK
    assert report.rejected == len(bad_rows) + 1
    assert (entry.lesson_id, entry.run_id, entry.attempt_id, entry.task_id) == (
        valid["id"], "run-1", "a-1", "root-1",
    )
    assert entry.text == render_lesson_text(_fields(LESSON_A))
    assert read_lesson_ledger(str(ledger), run_id="").entries == ()


def test_ledger_read_back_caps_entries_and_skips_oversized_files(tmp_path: Path) -> None:
    ledger = tmp_path / "run-1" / "lessons.jsonl"
    identity = LessonIdentity(run_id="run-1", attempt_id="", task_id="root-1")
    ledger.parent.mkdir(parents=True)
    rows = [lesson_ledger_record(_fields(_lesson(index)), identity, created_at=1.0) for index in range(7)]
    ledger.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    capped = read_lesson_ledger(str(ledger), run_id="run-1")
    ledger.write_text("x" * (MAX_LESSON_LEDGER_BYTES + 1), encoding="utf-8")
    oversized = read_lesson_ledger(str(ledger), run_id="run-1")

    assert [entry.lesson_id for entry in capped.entries] == [row["id"] for row in rows[:MAX_LESSONS_PER_RUN]]
    assert capped.rejected == 7 - MAX_LESSONS_PER_RUN
    assert (oversized.status, oversized.entries) == (LEDGER_OVERSIZED, ())


# ---- 结果收口：合并进 output.json / runner result ----


def test_natural_result_merges_ledger_lessons_into_output_and_runner_result(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    _record(ctx, LESSON_A)
    _record(ctx, LESSON_B)

    result = _natural_result(ctx)
    output = _output(ctx)
    runner_json = json.loads(Path(ctx.manager.load(ctx.task.id).runner_result_json).read_text(encoding="utf-8"))
    expected = [render_lesson_text(_fields(LESSON_A)), render_lesson_text(_fields(LESSON_B))]

    assert result.ok and result.structured_output_found is False
    assert output["lessons"] == expected
    assert output["structured_output"]["lesson_count"] == 2
    assert result.lesson_count == runner_json["lesson_count"] == 2
    assert "lesson_ledger=ok lesson_ledger_entries=2 lesson_ledger_rejected=0" in _work_log(ctx)


def test_structured_lessons_merge_first_in_order_without_duplicates(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    _record(ctx, LESSON_A)
    _record(ctx, LESSON_B)
    text_a, text_b = (render_lesson_text(_fields(item)) for item in (LESSON_A, LESSON_B))
    structured = SubAgentParsedOutput(
        found=True, ok=True, status="DONE", summary="完成", lessons=["结构化输出里的经验", f"{text_b} "],
    )

    _natural_result(ctx, structured)
    lessons = [item for item in ctx.candidates.list() if item.candidate_type == "lesson"]

    assert _output(ctx)["lessons"] == ["结构化输出里的经验", text_b, text_a]
    assert sorted(item.content for item in lessons) == sorted(["结构化输出里的经验", text_a, text_b])
    plain = next(item for item in lessons if item.content == "结构化输出里的经验")
    assert plain.scope["applies_when"] == GOAL and not plain.evidence_refs


def test_unreadable_ledger_is_reported_without_blocking_result_delivery(tmp_path: Path, monkeypatch) -> None:
    ctx = _runtime(tmp_path)
    _record(ctx, LESSON_A)

    def refuse(_path):
        raise PermissionError("lock unavailable")

    monkeypatch.setattr(lesson_ledger, "locked_json_path", refuse)
    report = read_lesson_ledger(ctx.task.agent_run_lessons_jsonl, run_id=ctx.task.id)
    result = _natural_result(ctx)

    assert (report.status, report.entries) == (LEDGER_UNREADABLE, ())
    assert result.ok and _output(ctx)["lessons"] == []
    assert ctx.candidates.list() == []
    assert "lesson_ledger=unreadable lesson_ledger_entries=0" in _work_log(ctx)


def test_run_without_ledger_keeps_the_previous_output_shape(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)

    _natural_result(ctx)

    assert _output(ctx)["lessons"] == []
    assert ctx.candidates.list() == []
    assert "lesson_ledger" not in _work_log(ctx)


# ---- 候选与 S1 提案 ----


def test_ledger_lessons_become_subagent_lesson_candidates_with_ledger_refs(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    lesson_id = _payload(_record(ctx, LESSON_A, attempt_id="attempt-3"))["lesson_id"]

    _natural_result(ctx)
    [candidate] = ctx.candidates.list()
    task_id = ctx.task.root_id or ctx.task.id
    ledger = ctx.task.agent_run_lessons_jsonl

    assert (candidate.candidate_type, candidate.origin, candidate.status) == (
        "lesson",
        "subagent_lesson",
        "pending_review",
    )
    assert candidate.content == render_lesson_text(_fields(LESSON_A))
    assert (candidate.source_task_ids, candidate.source_run_ids) == ([task_id], [ctx.task.id])
    assert candidate.scope["applies_when"] == LESSON_A["when_to_use"]
    assert candidate.evidence_refs == [
        {
            "source_ref": f"{ledger}#{lesson_id}",
            "evidence_type": "subagent_lesson_ledger",
            "ledger_ref": ledger,
            "lesson_id": lesson_id,
            "task_id": task_id,
            "run_id": ctx.task.id,
            "attempt_id": "attempt-3",
        }
    ]
    assert candidate.source_artifact_refs[0]["artifact_ref"] == ctx.manager.load(ctx.task.id).output_json


def test_self_learning_on_proposes_once_per_lesson_and_replay_adds_nothing(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path, proposals=True)
    _record(ctx, LESSON_A)
    _record(ctx, LESSON_B)

    first = _natural_result(ctx)
    proposals = ctx.service.list()
    files = sorted(ctx.home.owner_skill_proposals_dir.glob("*.json"))
    replay = _natural_result(ctx)
    candidates = ctx.candidates.list()

    assert first.ok and replay.ok
    assert len(proposals) == 2 and {item.status for item in proposals} == {"pending_confirmation"}
    assert {item.source.run_ids for item in proposals} == {(ctx.task.id,)}
    assert any(LESSON_A["when_to_use"] in item.draft.when_to_use for item in proposals)
    assert sorted(ctx.home.owner_skill_proposals_dir.glob("*.json")) == files
    assert len(candidates) == 2 and {item.occurrence_count for item in candidates} == {1}
    log = _work_log(ctx)
    assert "skill_proposals=2" in log and "skill_proposals=0" in log


def test_self_learning_off_records_candidates_without_proposals(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path, proposals=False)
    _record(ctx, LESSON_A)

    _natural_result(ctx)

    assert [item.origin for item in ctx.candidates.list()] == ["subagent_lesson"]
    assert not ctx.home.owner_skill_proposals_dir.exists()
    assert "skill_proposals" not in _work_log(ctx)


def test_candidate_failure_never_blocks_result_delivery(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    _record(ctx, LESSON_A)

    # 替身签名与 record_result_candidates 的显式关键字参数一致。
    def explode(task, *, lessons, findings, lesson_entries=()):
        raise RuntimeError("candidate store unavailable")

    ctx.manager.memory_candidates.record_result_candidates = explode
    result = _natural_result(ctx)

    assert result.ok and ctx.manager.load(ctx.task.id).status == "DONE"
    assert _output(ctx)["lessons"] == [render_lesson_text(_fields(LESSON_A))]
    assert "memory_candidates=0" in _work_log(ctx)
    assert "memory_candidates_error=RuntimeError" in _work_log(ctx)


def test_dry_run_records_no_candidates(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    _record(ctx, LESSON_A)

    ctx.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(run_id=ctx.task.id, dry_run=True, ok=True, message="预演", status="DONE")
    )

    assert ctx.candidates.list() == []


# ---- 暴露面：子代理默认可见，主线程不提供 ----


def test_child_grant_surfaces_include_record_lesson() -> None:
    parent = [*CODING_SUBAGENT_TOOLS]
    grandchild = scheduled_child_tools(
        ToolPolicyRequest(
            parent_tools=parent,
            spec=SimpleNamespace(allowed_tools=["read_file"], role="worker"),
            extra_write_roots=[],
        )
    )
    capped = scheduled_child_tools(
        ToolPolicyRequest(
            parent_tools=[tool for tool in parent if tool != RECORD_LESSON_TOOL],
            spec=SimpleNamespace(allowed_tools=["read_file"], role="worker"),
            extra_write_roots=[],
        )
    )

    assert RECORD_LESSON_TOOL in CODING_SUBAGENT_TOOLS
    assert RECORD_LESSON_TOOL in READ_ONLY_SUBAGENT_TOOLS
    assert RECORD_LESSON_TOOL in ROLE_BASE_TOOLS
    assert RECORD_LESSON_TOOL in subagent_allowed_tools({})
    assert RECORD_LESSON_TOOL in subagent_allowed_tools({"tool_preset": "read_only"})
    assert RECORD_LESSON_TOOL in grandchild
    assert RECORD_LESSON_TOOL not in capped
    assert RECORD_LESSON_TOOL not in background_allowed_tools()


def test_main_thread_registry_hides_record_lesson_while_child_snapshot_exposes_it(tmp_path: Path) -> None:
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path / "workspace")
    registry = agent.tools

    main = registry.runtime_snapshot()
    child = registry.runtime_snapshot(allowed_tools=subagent_allowed_tools({}), run_id="child-1")
    listed = registry.tools["list_tools"].execute({})

    assert RECORD_LESSON_TOOL in registry.tools
    assert RECORD_LESSON_TOOL not in main.available_tool_names
    assert RECORD_LESSON_TOOL not in {spec.name for spec in registry.search_deferred_specs("record_lesson 经验 做法")}
    assert RECORD_LESSON_TOOL not in listed.output
    assert RECORD_LESSON_TOOL in child.available_tool_names


def test_record_lesson_is_not_registered_when_subagents_are_disabled(tmp_path: Path) -> None:
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[], enable_subagents=False)

    agent = SimpleAgent(config, tmp_path / "workspace")

    assert RECORD_LESSON_TOOL not in agent.tools.tools


# ---- runner 提示：一条可选软引导，不恢复状态 JSON ----


def _prompt(tmp_path: Path, allowed_tools: list[str]) -> str:
    context = SubAgentExecutionContext(
        run_id="leaf-1",
        generated_at=1.0,
        goal=GOAL,
        thought="",
        plan=["执行"],
        role="worker",
        task_dir=str(tmp_path / "subagents" / "leaf-1"),
        allowed_tools=allowed_tools,
    )
    return render_subagent_runner_prompt(prepare_subagent_runner_prompt(context))


def test_runner_prompt_offers_one_optional_line_only_when_the_tool_is_granted(tmp_path: Path) -> None:
    granted = _prompt(tmp_path, ["read_file", RECORD_LESSON_TOOL])
    withheld = _prompt(tmp_path, ["read_file"])
    # 上下文 JSON 会列出授权工具名；这里只数 Runner Contract 里的引导条款。
    lines = [line for line in granted.splitlines() if RECORD_LESSON_TOOL in line and line.startswith("- ")]

    assert len(lines) == 1
    assert "可选" in lines[0] and "可复用" in lines[0]
    assert RECORD_LESSON_TOOL not in withheld
    for prompt in (granted, withheld):
        assert "不要输出 SUBAGENT_RESULT、" in prompt and "状态 JSON" in prompt
        assert '"lessons"' not in prompt
