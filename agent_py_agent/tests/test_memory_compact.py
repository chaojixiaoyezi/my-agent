from __future__ import annotations

import json
from pathlib import Path

import agent_py_agent.agent.memory_archive.compact_apply as compact_apply_module
from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.memory_archive import (
    RUNTIME_MEMORY_SCHEMA_VERSION,
    CompressionSnapshot,
    RawMemoryEvent,
    TurnTokenUsage,
    append_raw_event,
    append_session_token_usage,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.compact_suggest import (
    MemoryCompactSuggestOptions,
    build_memory_compact_suggestion,
)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _write_compact_fixture(root: Path) -> None:
    append_raw_event(root, _compact_raw_event())
    snapshot = _compact_snapshot()
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    _append_compact_token_usage(root)


# LLM: _write_real_run_archive_fixture simulates run --save archives without authoritative snapshot files.
# 函数用途: 写入真实 run 风格的 raw/hook/token 数据，验证 compact apply 能从 hook recovery 回填状态。
def _write_real_run_archive_fixture(root: Path) -> None:
    append_raw_event(root, _compact_raw_event())
    append_snapshot(root, _compact_snapshot())
    _append_compact_token_usage(root)


def _compact_raw_event() -> RawMemoryEvent:
    return RawMemoryEvent(
        event_id="raw-compact-1",
        session_id="session-compact",
        request_id="request-compact",
        run_id="run-compact",
        task_id="run-compact",
        speaker="user",
        target="assistant",
        action="message",
        status="ok",
        content_preview="需要自动 compact dry-run 计划",
        source="run",
        archive_level=2,
        created_at="2026-05-06T08:00:00+00:00",
    )


def _compact_snapshot() -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snapshot-compact-1",
        session_id="session-compact",
        compression_id="compression-compact",
        turn_range={
            "start": 1,
            "end": 1,
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
        },
        user_intents=["需要自动 compact dry-run 计划"],
        assistant_actions=["准备扫描归档、snapshot 和 token ledger。"],
        dispatch_events=[
            {
                "source": "run",
                "request_id": "request-compact",
                "run_id": "run-compact",
                "task_id": "run-compact",
                "status": "ok",
            }
        ],
        task_refs=["run-compact"],
        next_actions=["先看 dry-run，再决定是否启用 apply。"],
        archive_level=2,
        created_at="2026-05-06T08:01:00+00:00",
    )


def _append_compact_token_usage(root: Path) -> None:
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-compact",
            turn_id="turn-1",
            input_tokens=100,
            output_tokens=20,
            tool_tokens=5,
            created_at="2026-05-06T08:02:00+00:00",
        ),
    )


def test_build_memory_compact_plan_is_read_only_summary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

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


def test_memory_compact_cli_outputs_json_plan(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    _write_compact_fixture(_workspace(config_path))
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


def test_memory_compact_plan_reports_invalid_manifests(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    (root / "memory_archive" / "snapshots" / "bad.json").write_text("{bad", encoding="utf-8")
    (root / "memory_archive" / "tokens" / "bad.json").write_text("{bad", encoding="utf-8")

    plan = build_memory_compact_plan(root, MemoryCompactPlanOptions())

    assert plan["snapshots"]["invalid_count"] == 1
    assert plan["tokens"]["invalid_count"] == 1
    assert "compression snapshot directory contains invalid JSON files" in plan["risks"]
    assert "token ledger directory contains invalid JSON files" in plan["risks"]


def test_apply_memory_compact_writes_non_destructive_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    artifacts = _load_apply_artifacts(result)
    assert result["mode"] == "apply"
    _assert_apply_artifact_schemas(result, artifacts)
    _assert_successful_apply_payload(result, artifacts)
    _assert_apply_preserved_sources(root)


def test_memory_compact_apply_reads_hook_recovery_state_without_snapshot_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_real_run_archive_fixture(root)

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
        "my-agent memory-compact --apply --session-id session-compact --request-id request-compact"
    )
    assert "验收条件" in resume["completion_prompt"]["prompt_template"]
    assert "Completion Prompt" in resume["context_block"]
    assert auto_resume["action_guard"]["status"] == "blocked_missing_work_state_fields"
    assert auto_resume["action_guard"]["allowed_to_continue"] is False


# LLM: _load_apply_artifacts keeps compact apply tests focused on behavior instead of path-reading boilerplate.
# 函数用途: 按 result refs 读取 apply bundle、restore refs、work state、self-check 和最后一条 ledger。
def _load_apply_artifacts(result: dict[str, object]) -> dict[str, object]:
    refs = result["refs"]
    assert isinstance(refs, dict)
    ledger_lines = Path(str(refs["apply_ledger"])).read_text(encoding="utf-8").splitlines()
    return {
        "apply_bundle": json.loads(Path(str(refs["apply_bundle"])).read_text(encoding="utf-8")),
        "restore_refs": json.loads(Path(str(refs["restore_refs"])).read_text(encoding="utf-8")),
        "work_state": json.loads(Path(str(refs["work_state_snapshot"])).read_text(encoding="utf-8")),
        "self_check": json.loads(Path(str(refs["post_compact_self_check"])).read_text(encoding="utf-8")),
        "ledger_record": json.loads(ledger_lines[-1]),
    }


# LLM: _assert_apply_artifact_schemas verifies all manual compact apply files share schema v2 and IDs.
# 函数用途: 校验 apply 相关 JSON 产物的 schema、apply_id 和 plan_id 一致。
def _assert_apply_artifact_schemas(result: dict[str, object], artifacts: dict[str, object]) -> None:
    apply_bundle = artifacts["apply_bundle"]
    restore_refs = artifacts["restore_refs"]
    work_state = artifacts["work_state"]
    self_check = artifacts["self_check"]
    _assert_schema_v2(result, "compact_apply")
    _assert_schema_v2(apply_bundle, "compact_apply_bundle")
    _assert_schema_v2(restore_refs, "compact_apply_restore_refs")
    _assert_schema_v2(work_state, "compact_work_state_snapshot")
    _assert_schema_v2(self_check, "compact_apply_self_check")
    _assert_apply_ids_match(result, apply_bundle, restore_refs, work_state, self_check)


# LLM: _assert_successful_apply_payload captures the Step 1 manual apply contract in one readable place.
# 函数用途: 校验成功 apply 的状态、恢复引用、work state、自检和 ledger 关键字段。
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
    assert "work_state_snapshot_written" in _check_names(self_check)
    assert "restore_refs_exist" in _check_names(self_check)
    assert "apply_ids_consistent" in _check_names(self_check)
    assert "artifact_refs_valid" in _check_names(self_check)
    assert "latest_tests_recorded" in _check_names(self_check)
    assert self_check["ok"] is True
    _assert_schema_v2(ledger_record, "compact_apply_ledger")
    assert ledger_record["event_id"] == result["event_id"]
    assert ledger_record["apply_id"] == result["apply_id"]
    assert ledger_record["plan_id"] == result["plan_id"]


# LLM: _assert_apply_preserved_sources proves manual compact apply did not delete or rewrite source families.
# 函数用途: 校验 raw memory 和 snapshot 源文件仍然存在，保证 apply 仍是非破坏性第一片。
def _assert_apply_preserved_sources(root: Path) -> None:
    assert (root / "memory" / "raw" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()


def test_apply_memory_compact_uses_stable_plan_id_and_unique_apply_id(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
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
    _write_compact_fixture(root)
    monkeypatch.setattr(compact_apply_module, "_self_check_payload", _failed_self_check)

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
    _assert_schema_v2(failure, "compact_apply_self_check_failure")
    assert failure["apply_id"] == result["apply_id"]
    assert failure["plan_id"] == result["plan_id"]
    assert failure["failed_checks"][0]["name"] == "forced_failure"
    assert ledger_record["compact_status"] == "blocked_self_check_failed"
    assert ledger_record["restore_ready"] is False
    assert (root / "memory" / "raw" / "2026-05-06.jsonl").exists()
    assert (root / "memory_archive" / "snapshots" / "2026-05-06--snapshot-compact-1.json").exists()


def test_memory_compact_cli_apply_outputs_json_result(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    _write_compact_fixture(_workspace(config_path))
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
    _write_compact_fixture(root)
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
            owner_type="subagent_session",
            owner_id="run-compact",
        ),
    )

    assert resume["ok"] is True
    assert resume["mode"] == "resume_from_compact"
    _assert_schema_v2(resume, "compact_resume")
    _assert_schema_v2(resume["consistency_report"], "compact_resume_consistency_report")
    assert resume["apply_id"] == apply_result["apply_id"]
    assert resume["work_state"]["goal"] == "需要自动 compact dry-run 计划"
    assert resume["consistency_report"]["status"] == "ok"
    _assert_schema_v2(resume["action_guard"], "compact_action_guard")
    assert resume["action_guard"]["status"] == "requires_user_confirmation"
    assert resume["action_guard"]["allowed_to_continue"] is False
    assert resume["owner"] == {"owner_type": "subagent_session", "owner_id": "run-compact"}
    assert resume["subagent_session_compact"]["status"] == "owner_refs_not_found"
    assert resume["subagent_session_compact"]["writes_main_memory"] is False
    assert "Compact Resume Context" in resume["context_block"]
    assert Path(apply_result["refs"]["metadata"]).exists()


def test_memory_resume_from_compact_auto_guard_blocks_missing_work_state(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
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
    assert resume["action_guard"]["ok"] is False
    assert resume["action_guard"]["status"] == "blocked_missing_work_state_fields"
    assert resume["action_guard"]["allowed_next_action"] == "stop_and_request_review"
    assert resume["action_guard"]["missing_fields"] == ["acceptance", "constraints", "latest_tests"]


def test_memory_resume_from_compact_cli_outputs_context_only(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    _write_compact_fixture(root)
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

    assert code == 2
    assert "# Compact Resume Context" in captured.out
    assert apply_result["apply_id"] in captured.out


# LLM: test_memory_fact_write_closes_compact_missing_fields verifies the semi-auto manual fact loop.
# 函数用途: 先让 compact resume 因缺字段阻断，再写入用户确认事实并重新 apply，确认 auto guard 放行。
def test_memory_compact_suggestion_prompts_without_applying(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    suggestion = build_memory_compact_suggestion(
        root,
        MemoryCompactSuggestOptions(
            current_tokens=8000,
            max_context_tokens=10000,
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
            owner_type="subagent_session",
            owner_id="run-compact",
        ),
    )

    assert suggestion["status"] == "suggest_compact"
    assert suggestion["should_prompt"] is True
    assert suggestion["requires_confirmation"] is True
    assert suggestion["automatic_action"] == "none"
    assert suggestion["owner"] == {"owner_type": "subagent_session", "owner_id": "run-compact"}
    assert suggestion["token_budget"]["ratio"] == 0.8
    assert suggestion["candidate_counts"]["archive_records"] == 2
    assert suggestion["recommended_commands"][0].startswith("my-agent memory-compact --session-id session-compact")


# LLM: _assert_schema_v2 keeps compact apply metadata, ledger, and self-check on the shared v2 contract.
# 函数用途: 校验 compact apply 相关记录的 schema 名称、版本和 reserved 扩展槽。
def _assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"]["name"] == name
    assert record["reserved"]["schema_name"] == name
    assert record["reserved"]["schema_version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert set(record["reserved"]) >= {"extensions", "compat", "future"}


# LLM: _assert_apply_ids_match keeps compact apply artifacts tied to one concrete apply attempt.
# 函数用途: 校验 metadata、apply bundle、restore refs、work state 和 self-check 的 apply_id/plan_id 一致。
def _assert_apply_ids_match(result: dict[str, object], *records: dict[str, object]) -> None:
    for record in records:
        assert record["apply_id"] == result["apply_id"]
        assert record["plan_id"] == result["plan_id"]


# LLM: _check_names extracts self-check names for focused assertions.
# 函数用途: 从 self-check payload 中提取检查名集合。
def _check_names(self_check: dict[str, object]) -> set[str]:
    return {str(item["name"]) for item in self_check["checks"]}


# LLM: _failed_self_check simulates a hard post-apply validation failure without touching production files.
# 函数用途: 为 self-check failure 测试返回一个 v2 自检失败 payload，验证 apply 会阻断而不是改写事实源。
def _failed_self_check(
    plan: dict[str, object], paths: dict[str, Path], now: str, work_state: dict[str, object]
) -> dict[str, object]:
    schema = compact_apply_module.COMPACT_SELF_CHECK_SCHEMA
    return {
        "version": RUNTIME_MEMORY_SCHEMA_VERSION,
        "schema": compact_apply_module.runtime_memory_schema_payload(schema),
        "ok": False,
        "apply_id": work_state["apply_id"],
        "plan_id": work_state["plan_id"],
        "event_type": "post_compact_self_check",
        "checks": [{"name": "forced_failure", "ok": False, "severity": "hard"}],
        "created_at": now,
        "reserved": compact_apply_module.runtime_memory_reserved_fields(schema),
    }
