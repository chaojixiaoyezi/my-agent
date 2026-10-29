# LLM: 真实生成器必须使用canonical入口；旧CLI别名用例仅作为独立的兼容回归，不代表推荐命令已接通路由。
# 模块用途: 验证压缩产物、恢复提示和历史兼容性，所有写入限于测试临时目录。
from __future__ import annotations

import json
import shlex
from pathlib import Path

import agent_py_agent.agent.memory_archive.compact_apply as compact_apply_module
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_bundle_payload,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.compact_resume.completion import (
    CompactCompletionPromptRequest,
    build_compact_completion_prompt,
)
from agent_py_agent.agent.memory_archive.compact_suggest import (
    MemoryCompactSuggestOptions,
    build_memory_compact_suggestion,
)
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.tests.memory_compact_support import (
    assert_apply_ids_match,
    assert_apply_preserved_sources,
    assert_schema_v2,
    check_names,
    failed_self_check,
    load_apply_artifacts,
    owner_home,
    workspace,
    write_compact_fixture,
    write_config,
    write_real_run_archive_fixture,
)


def test_build_memory_compact_plan_is_read_only_summary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    plan = build_memory_compact_plan(
        root,
        MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact", level=2),
    )

    assert plan["mode"] == "dry-run"
    assert plan["dry_run"] is True
    assert plan["archive"]["record_count"] == 2
    assert plan["archive"]["by_layer"] == {"hook": 1, "raw": 1}
    assert plan["snapshots"]["file_count"] == 1
    assert plan["tokens"]["ledger_count"] == 1
    assert plan["tokens"]["turn_count"] == 1
    assert plan["tokens"]["cumulative_tokens"] == 125
    assert plan["risks"] == []
    assert plan["estimated_compactable_bytes"] == plan["archive"]["total_bytes"] + plan["tokens"]["total_bytes"]


def test_apply_bundle_restore_steps_are_action_first(tmp_path: Path) -> None:
    refs = {
        "context_md": tmp_path / "apply.md",
        "compaction_state_json": tmp_path / "state.json",
        "handoff_summary_md": tmp_path / "handoff.md",
        "metadata_json": tmp_path / "metadata.json",
        "apply_bundle_json": tmp_path / "bundle.json",
        "restore_refs_json": tmp_path / "restore.json",
        "work_state_snapshot_json": tmp_path / "work.json",
        "self_check_json": tmp_path / "self_check.json",
        "failed_self_check_json": tmp_path / "self_check_failed.json",
        "ledger_jsonl": tmp_path / "ledger.jsonl",
    }
    payload = {
        "event_id": "event-action-first",
        "apply_id": "apply-action-first",
        "plan_id": "plan-action-first",
        "compact_status": "applied_non_destructive",
        "workspace_root": str(tmp_path),
        "scope": {},
        "lineage": {},
        "refs": {},
    }
    restore_refs = {"source_refs": {"archive_files": [], "snapshot_files": [], "token_ledgers": []}}
    work_state = {
        "goal": "整理多个项目架构报告",
        "next_step": "继续补齐未看项目并写报告",
        "next_actions": ["继续补齐未看项目并写报告"],
        "missing_fields": [],
        "source_quality": {},
    }

    bundle = apply_bundle_payload(payload, restore_refs, work_state, refs)

    assert bundle["restore_steps"]
    assert "continue" in bundle["restore_steps"][0].lower() or "继续" in bundle["restore_steps"][0]
    assert "read compact_context as the compact entrypoint" not in bundle["restore_steps"]


def test_memory_compact_cli_outputs_json_plan(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    write_compact_fixture(owner_home(config_path))
    parser = build_parser()

    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-compact",
            "--session-id",
            "session-compact",
            "--request-id",
            "request-compact",
            "--json",
        ]
    )
    code = args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["scope"]["session_id"] == "session-compact"
    assert payload["archive"]["record_count"] == 2
    assert payload["snapshots"]["file_count"] == 1


# LLM: 风险与有原归档两条真实分支都必须给出canonical提示，不从源码字符串猜测行为。
# 函数用途: 验证坏manifest披露和压缩计划的安全操作建议。
def test_memory_compact_plan_reports_invalid_manifests(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    (root / "memory_archive" / "snapshots" / "bad.json").write_text("{bad", encoding="utf-8")
    (root / "memory_archive" / "tokens" / "bad.json").write_text("{bad", encoding="utf-8")

    plan = build_memory_compact_plan(root, MemoryCompactPlanOptions())

    assert plan["snapshots"]["invalid_count"] == 1
    assert plan["tokens"]["invalid_count"] == 1
    assert "compression snapshot directory contains invalid JSON files" in plan["risks"]
    assert "token ledger directory contains invalid JSON files" in plan["risks"]
    assert "Keep original raw/hook files until my-agent memory archive resume is verified after compact." in plan["recommended_actions"]
    assert "Run my-agent memory doctor and local-doctor before applying compact." in plan["recommended_actions"]


# LLM: 新默认actor须进入真实落盘metadata；不改写显式actor或历史产物。
# 函数用途: 验证非破坏性apply与canonical默认actor。
def test_apply_memory_compact_writes_non_destructive_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    artifacts = load_apply_artifacts(result)
    assert json.loads(Path(result["refs"]["metadata"]).read_text())["actor"] == "my-agent memory compact"
    assert result["mode"] == "apply"
    _assert_apply_artifact_schemas(result, artifacts)
    _assert_successful_apply_payload(result, artifacts)
    assert_apply_preserved_sources(root)


# LLM: 保留原hook事实、guard和自动恢复断言，仅迁移新生成的补全命令。
# 函数用途: 验证无独立快照时的恢复事实与canonical补救提示。
def test_memory_compact_apply_reads_hook_recovery_state_without_snapshot_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_real_run_archive_fixture(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))
    auto_resume = build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(apply_ref=result["apply_id"], resume_mode="auto"),
    )

    work_state = result["work_state_snapshot"]
    assert result["source_plan"]["snapshot_file_count"] == 0
    assert work_state["goal"] == "需要自动 compact dry-run 计划"
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"
    assert work_state["missing_fields"] == ["acceptance", "constraints", "latest_tests"]
    assert resume["action_guard"]["status"] == "requires_user_confirmation"
    assert resume["handoff"]["goal"] == "需要自动 compact dry-run 计划"
    assert resume["continue_packet"]["ready_to_continue"] is False
    assert resume["continue_packet"]["continue_mode"] == "manual_handoff"
    assert resume["continue_packet"]["automatic_tool_execution"] == "none"
    assert resume["completion_prompt"]["status"] == "needs_user_input"
    assert resume["completion_prompt"]["suggested_commands"]
    assert resume["completion_prompt"]["suggested_commands"][1] == (
        "my-agent memory compact --apply --session-id session-compact --request-id request-compact"
    )
    assert "验收条件" in resume["completion_prompt"]["prompt_template"]
    assert "Completion Prompt" in resume["context_block"]
    assert auto_resume["action_guard"]["status"] == "allow_automated_continue"
    assert auto_resume["action_guard"]["allowed_to_continue"] is True
    assert auto_resume["action_guard"]["missing_fields"] == ["acceptance", "constraints", "latest_tests"]


def _assert_apply_artifact_schemas(result: dict[str, object], artifacts: dict[str, object]) -> None:
    apply_bundle = artifacts["apply_bundle"]
    restore_refs = artifacts["restore_refs"]
    work_state = artifacts["work_state"]
    self_check = artifacts["self_check"]
    assert_schema_v2(result, "compact_apply")
    assert_schema_v2(apply_bundle, "compact_apply_bundle")
    assert_schema_v2(restore_refs, "compact_apply_restore_refs")
    assert_schema_v2(work_state, "compact_work_state_snapshot")
    assert_schema_v2(self_check, "compact_apply_self_check")
    assert_apply_ids_match(result, apply_bundle, restore_refs, work_state, self_check)


def _assert_successful_apply_payload(result: dict[str, object], artifacts: dict[str, object]) -> None:
    refs = result["refs"]
    apply_bundle = artifacts["apply_bundle"]
    restore_refs = artifacts["restore_refs"]
    work_state = artifacts["work_state"]
    self_check = artifacts["self_check"]
    ledger_record = artifacts["ledger_record"]
    assert result["dry_run"] is False
    assert result["compact_status"] == "applied_non_destructive"
    assert result["content_preserved"] is True
    assert result["source_plan"]["archive_record_count"] == 2
    assert Path(refs["compact_context"]).exists()
    assert Path(refs["metadata"]).exists()
    assert apply_bundle["restore_refs_summary"] == {
        "archive_files": 2,
        "snapshot_files": 1,
        "token_ledgers": 1,
    }
    assert restore_refs["source_refs"]["archive_files"]
    assert restore_refs["source_refs"]["snapshot_files"]
    assert restore_refs["source_refs"]["token_ledgers"]
    assert work_state["goal"] == "需要自动 compact dry-run 计划"
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"
    assert work_state["missing_fields"] == ["acceptance", "constraints", "latest_tests"]
    assert work_state["restore_refs"]["all_source_paths_exist"] is True
    assert "work_state_snapshot_written" in check_names(self_check)
    assert "restore_refs_exist" in check_names(self_check)
    assert "apply_ids_consistent" in check_names(self_check)
    assert "artifact_refs_valid" in check_names(self_check)
    assert "latest_tests_recorded" in check_names(self_check)
    assert self_check["ok"] is True
    assert_schema_v2(ledger_record, "compact_apply_ledger")
    assert ledger_record["event_id"] == result["event_id"]
    assert ledger_record["apply_id"] == result["apply_id"]
    assert ledger_record["plan_id"] == result["plan_id"]


def test_apply_memory_compact_uses_stable_plan_id_and_unique_apply_id(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    monkeypatch.setattr(compact_apply_module, "_utc_now", lambda: "2026-05-07T08:00:00+00:00")

    first = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    second = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    ledger = [
        json.loads(line)
        for line in Path(first["refs"]["apply_ledger"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert first["plan_id"] == second["plan_id"]
    assert first["apply_id"] != second["apply_id"]
    assert Path(first["refs"]["metadata"]).exists()
    assert Path(second["refs"]["metadata"]).exists()
    assert ledger[-2]["apply_id"] == first["apply_id"]
    assert ledger[-1]["apply_id"] == second["apply_id"]


def test_apply_memory_compact_records_self_check_failure_without_rewriting_sources(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    monkeypatch.setattr(compact_apply_module, "_self_check_payload", failed_self_check)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    refs = result["refs"]
    failure = json.loads(Path(refs["self_check_failure"]).read_text(encoding="utf-8"))
    ledger_record = json.loads(Path(refs["apply_ledger"]).read_text(encoding="utf-8").splitlines()[-1])

    assert result["ok"] is False
    assert result["compact_status"] == "blocked_self_check_failed"
    assert_schema_v2(failure, "compact_apply_self_check_failure")
    assert failure["apply_id"] == result["apply_id"]
    assert failure["plan_id"] == result["plan_id"]
    assert failure["failed_checks"][0]["name"] == "forced_failure"
    assert ledger_record["compact_status"] == "blocked_self_check_failed"
    assert ledger_record["restore_ready"] is False
    assert (root / "audit" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()


def test_memory_compact_cli_apply_outputs_json_result(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    write_compact_fixture(owner_home(config_path))
    parser = build_parser()

    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-compact",
            "--apply",
            "--session-id",
            "session-compact",
            "--json",
        ]
    )
    code = args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["mode"] == "apply"
    assert payload["scope"]["session_id"] == "session-compact"
    assert Path(payload["refs"]["compact_context"]).exists()
    assert Path(payload["refs"]["work_state_snapshot"]).exists()
    assert Path(payload["refs"]["apply_bundle"]).exists()
    assert Path(payload["refs"]["restore_refs"]).exists()


def test_memory_resume_from_compact_builds_manual_context(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    resume = build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(
            apply_ref=apply_result["apply_id"],
            owner_type="subagent_run",
            owner_id="run-compact",
        ),
    )

    assert resume["ok"] is True
    assert resume["mode"] == "resume_from_compact"
    assert_schema_v2(resume, "compact_resume")
    assert_schema_v2(resume["consistency_report"], "compact_resume_consistency_report")
    assert resume["apply_id"] == apply_result["apply_id"]
    assert resume["work_state"]["goal"] == "需要自动 compact dry-run 计划"
    assert resume["consistency_report"]["status"] == "ok"
    assert_schema_v2(resume["action_guard"], "compact_action_guard")
    assert resume["action_guard"]["status"] == "requires_user_confirmation"
    assert resume["action_guard"]["allowed_to_continue"] is False
    assert resume["owner"] == {"owner_type": "subagent_run", "owner_id": "run-compact"}
    assert "Compact Resume Context" in resume["context_block"]
    assert Path(apply_result["refs"]["metadata"]).exists()


def test_memory_resume_from_compact_auto_guard_allows_optional_notes_missing(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    resume = build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"], resume_mode="auto"),
    )

    assert resume["ok"] is True
    assert resume["action_guard"]["ok"] is True
    assert resume["action_guard"]["status"] == "allow_automated_continue"
    assert resume["action_guard"]["allowed_next_action"] == "continue_after_guard"
    assert resume["action_guard"]["missing_fields"] == ["acceptance", "constraints", "latest_tests"]


def test_memory_resume_from_compact_cli_outputs_context_only(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    root = owner_home(config_path)
    write_compact_fixture(root)
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    parser = build_parser()

    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-resume",
            "--from-compact",
            apply_result["apply_id"],
            "--compact-resume-mode",
            "auto",
            "--context-only",
        ]
    )
    code = args.func(args)
    captured = capsys.readouterr()

    assert code == 0
    assert "# Compact Resume Context" in captured.out
    assert apply_result["apply_id"] in captured.out


# LLM: 三条建议按实际输出逐项断言；提示不能触发apply或自动动作。
# 函数用途: 验证canonical建议保留原scope和手动确认边界。
def test_memory_compact_suggestion_prompts_without_applying(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    suggestion = build_memory_compact_suggestion(
        root,
        MemoryCompactSuggestOptions(
            current_tokens=8000,
            max_context_tokens=10000,
            trigger_percent=70,
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
            owner_type="subagent_run",
            owner_id="run-compact",
        ),
    )

    assert suggestion["status"] == "ready_to_compact"
    assert suggestion["should_prompt"] is True
    assert suggestion["requires_confirmation"] is True
    assert suggestion["automatic_action"] == "none"
    assert suggestion["owner"] == {"owner_type": "subagent_run", "owner_id": "run-compact"}
    assert suggestion["token_budget"]["ratio"] == 0.8
    assert suggestion["candidate_counts"]["archive_records"] == 2
    assert suggestion["recommended_commands"] == [
        "my-agent memory compact --session-id session-compact --request-id request-compact --dry-run",
        "my-agent memory compact --session-id session-compact --request-id request-compact --apply",
        "my-agent memory archive resume --from-compact <apply_id> --context-only",
    ]


# LLM: 缺失metadata仍须阻塞，不允许靠改提示放宽guard或隐式创建归档。
# 函数用途: 验证失败恢复入口的canonical补救建议及只读行为。
def test_canonical_missing_resume_preserves_blocked_guard(tmp_path):
    root = tmp_path / "missing-root"
    result = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref="missing"))
    assert result["ok"] is False
    assert result["action_guard"]["allowed_to_continue"] is False
    assert result["next_actions"] == [
        "Find a valid compact apply id or rerun my-agent memory compact --apply."
    ]
    assert not (root / "memory_archive").exists()


# LLM: 两种guard分支均经公开packet构造器，canonical文案不得变成自动执行权限。
# 函数用途: 验证延续包允许分支和阻塞分支的恢复说明。
def test_canonical_continue_packet_preserves_guard_branches():
    for allowed in (False, True):
        result = build_compact_continue_packet(CompactContinuePacketRequest(
            metadata={"apply_id": "a", "plan_id": "p"}, work_state={},
            consistency={"status": "ok" if allowed else "blocked"},
            action_guard={"allowed_to_continue": allowed}, handoff={},
            recommended_read_paths=[], next_actions=[], main_context_bundle={},
        ))
        assert result["ready_to_continue"] is allowed
        assert result["automatic_tool_execution"] == "none"
        if allowed:
            assert result["resume_instructions"][0] == "Continue from resume_focus.next_action first."
        else:
            assert result["resume_instructions"][-1] == (
                "Rerun my-agent memory compact --apply and my-agent memory archive resume after facts are complete."
            )


# LLM: scope含空格和引号时须保持原shell转义合同；事实齐全时仍返回空建议。
# 函数用途: 验证补全提示的canonical路径、scope值和空列表语义。
def test_canonical_completion_commands_preserve_quoted_scope():
    scope = {"session_id": "session space", "request_id": "request'quoted", "run_id": "run-one", "task_id": "task two"}
    result = build_compact_completion_prompt(CompactCompletionPromptRequest(
        apply_id="a", plan_id="p", work_state={"missing_fields": ["acceptance"], "scope": scope},
    ))
    argv = shlex.split(result["suggested_commands"][1])
    assert argv[:4] == ["my-agent", "memory", "compact", "--apply"]
    assert dict(zip(argv[4::2], argv[5::2], strict=True)) == {
        "--session-id": scope["session_id"], "--request-id": scope["request_id"],
        "--run-id": scope["run_id"], "--task-id": scope["task_id"],
    }
    assert result["suggested_commands"][2] == (
        "my-agent memory archive resume --from-compact <new_apply_id> --compact-resume-mode auto"
    )
    complete = build_compact_completion_prompt(CompactCompletionPromptRequest(
        apply_id="a", plan_id="p", work_state={"missing_fields": [], "scope": scope},
    ))
    assert complete["suggested_commands"] == []


# LLM: 显式历史actor与v1产物仅为兼容fixture；恢复不得把它们静默改成新默认值。
# 函数用途: 验证历史v1metadata仍可读且读恢复不重写原字节。
def test_canonical_generation_does_not_rewrite_historical_v1(tmp_path):
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    applied = apply_memory_compact(root, MemoryCompactApplyOptions(
        plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        actor="memory-compact",
    ))
    path = Path(applied["refs"]["metadata"])
    metadata = json.loads(path.read_text())
    metadata["version"] = 1
    metadata.pop("schema", None)
    metadata.pop("compaction_gate", None)
    for field in ("compaction_state", "handoff_summary"):
        metadata["refs"].pop(field, None)
    path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    resumed = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=applied["apply_id"]))
    assert resumed["ok"] is True
    assert path.read_bytes() == before
    assert json.loads(before)["actor"] == "memory-compact"
