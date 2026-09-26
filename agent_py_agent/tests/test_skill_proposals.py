from __future__ import annotations

"""自学习 S1：子代理 lesson → 待确认 Skill 提案 → 用户 CLI 确认后经 guard 安装。"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_proposals
from agent_py_agent.agent.capability.skill_proposals import (
    SkillProposalError,
    SkillProposalService,
    render_skill_markdown,
)
from agent_py_agent.agent.capability.skill_service import SkillsService
from agent_py_agent.agent.capability.skills import parse_skill_file
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import LessonRepository
from agent_py_agent.agent.memory_store.migration import MemoryMigrationService
from agent_py_agent.agent.memory_store.operations import memory_content_hash
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.cli.parser import main as cli_main

LESSON = "修改共享状态前先读取当前版本，再用精确版本做比较交换写入。"
GOAL = "修复并发写入导致的状态覆盖"


def _task(run_id: str = "run-1") -> SubAgentTask:
    return SubAgentTask(
        id=run_id,
        root_id="project-root",
        goal=GOAL,
        thought="",
        plan=[],
        output_json=f"/artifacts/{run_id}/output.json",
        updated_at=100.0,
    )


def _runtime(tmp_path: Path) -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    return SimpleNamespace(home=home, candidates=candidates, manager=manager, service=SkillProposalService(home))


def _record_lesson(ctx: SimpleNamespace, lesson: str = LESSON, run_id: str = "run-1") -> list:
    return ctx.manager.memory_candidates.record_result_candidates(_task(run_id), lessons=[lesson], findings=[])


def _proposal_ctx(tmp_path: Path, lesson: str = LESSON) -> SimpleNamespace:
    ctx = _runtime(tmp_path)
    [ctx.proposal] = ctx.service.propose_from_candidates(_record_lesson(ctx, lesson))
    return ctx


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _proposal_path(ctx: SimpleNamespace) -> Path:
    return ctx.home.owner_skill_proposals_dir / f"{ctx.proposal.proposal_id}.json"


def _target(ctx: SimpleNamespace) -> Path:
    return ctx.home.owner_home_dir / "skills" / ctx.proposal.target.skill_name


def _staging_left(ctx: SimpleNamespace) -> list[Path]:
    return list(ctx.home.owner_skill_proposals_dir.glob(".staging-*"))


def test_self_learning_switch_defaults_off_in_yaml_dataclass_and_normalizer() -> None:
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    normalized, warnings = normalize_agent_config({"enable_self_learning": "false"})

    assert AgentConfig().enable_self_learning is False
    assert shipped.enable_self_learning is False
    assert normalized["enable_self_learning"] is False
    assert warnings == []


def test_default_off_agent_has_no_service_and_creates_no_directory(tmp_path: Path) -> None:
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path / "workspace")

    assert agent.subagents.skill_proposals is None
    assert agent.home_paths.owner_skill_proposals_dir == agent.home_paths.owner_data_dir / "skill_proposals"
    assert not agent.home_paths.owner_skill_proposals_dir.exists()


def test_enabled_agent_wires_service_to_scoped_owner_paths(tmp_path: Path) -> None:
    config = AgentConfig(
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        enable_self_learning=True,
        my_agent_owner_provider="feishu",
        my_agent_owner_kind="user",
        my_agent_owner_id="ou_1",
    )
    agent = SimpleAgent(config, tmp_path / "workspace")
    service = agent.subagents.skill_proposals
    owner_home = agent.home_paths.owner_home_dir

    assert isinstance(service, SkillProposalService)
    assert owner_home.parts[-4:] == ("providers", "feishu", "users", "ou_1")
    assert service.directory == owner_home / "data" / "skill_proposals"
    assert service.skills_root == owner_home / "skills"
    assert service.candidates_path == agent.home_paths.owner_memory_candidates_jsonl
    assert not service.directory.exists()


def test_one_lesson_yields_exactly_one_idempotent_proposal(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    recorded = _record_lesson(ctx)
    created = ctx.service.propose_from_candidates(recorded)
    replays = [ctx.service.propose_from_candidates(_record_lesson(ctx, run_id=run)) for run in ("run-1", "run-2")]
    files = sorted(ctx.home.owner_skill_proposals_dir.glob("*.json"))
    record = json.loads(files[0].read_text(encoding="utf-8"))
    candidate = recorded[0]
    expected_id = hashlib.sha256((candidate.candidate_id + candidate.content_hash).encode()).hexdigest()[:24]

    assert len(created) == 1 and replays == [[], []]
    assert [path.stem for path in files] == [created[0].proposal_id] == [expected_id]
    assert (record["schema_version"], record["status"], record["revision"]) == (
        "my-agent.skill-proposal.v1",
        "pending_confirmation",
        1,
    )
    assert record["trigger"] == "subagent_lesson_candidate"
    assert record["source"] == {
        "candidate_id": candidate.candidate_id,
        "content_hash": candidate.content_hash,
        "task_ids": ["project-root"],
        "run_ids": ["run-1"],
    }
    assert record["target"] == {"skill_name": f"lesson-{candidate.content_hash[:12]}", "before": "absent"}
    assert LESSON in record["draft"]["body"] and record["draft"]["description"].startswith("子代理经验：")
    assert GOAL in record["draft"]["when_to_use"]
    assert not (ctx.home.owner_home_dir / "skills" / record["target"]["skill_name"]).exists()


def test_non_lesson_model_inferred_and_provenance_less_candidates_are_ignored(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)
    findings = ctx.manager.memory_candidates.record_result_candidates(
        _task(), lessons=[], findings=[{"id": "finding-1", "claim": "长期事实候选不是 Skill。", "confidence": 0.9}]
    )
    inferred = ctx.manager.memory_candidates.record_introspection_lesson(
        _task(), suggested_params={"timeout": 60}, failure_type="timeout", attempts=1, confidence=0.9
    )
    orphan = ctx.candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="没有任务来源的经验不能生成提案。",
            subject_key="subagent.lesson.orphan",
            scope=MemoryScope("project", "project:orphan"),
            origin="subagent_lesson",
            evidence_refs=({"source_ref": "manual-note"},),
            promotion_target="lesson",
        )
    )
    mislabeled = ctx.candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="类型不是 lesson 的候选即使标成子代理经验也不能生成提案。",
            subject_key="subagent.lesson.mislabeled",
            scope=MemoryScope("project", "project:project-root"),
            origin="subagent_lesson",
            source_task_ids=("project-root",),
            source_run_ids=("run-1",),
        )
    )
    ignored = [*findings, inferred, orphan, mislabeled, SimpleNamespace(candidate_type="lesson")]

    assert (inferred.candidate_type, inferred.origin, orphan.status) == ("lesson", "model_inferred", "pending_review")
    assert (mislabeled.origin, mislabeled.status) == ("subagent_lesson", "pending_review")
    assert ctx.service.propose_from_candidates(ignored) == []
    assert not ctx.home.owner_skill_proposals_dir.exists()


def test_curator_migration_keeps_skill_proposal_directory(tmp_path: Path) -> None:
    ctx = _proposal_ctx(tmp_path)
    home = ctx.home
    legacy = home.owner_data_dir / "learning_drafts"
    legacy.mkdir(parents=True)
    (legacy / "draft.json").write_text(json.dumps({"content": "旧学习草稿。"}, ensure_ascii=False), encoding="utf-8")
    before = _tree(home.owner_skill_proposals_dir)
    migration = MemoryMigrationService(
        home_paths=home,
        candidates=ctx.candidates,
        long_term=JsonlMemory(
            home.owner_memory_long_term_jsonl,
            ops_path=home.owner_memory_ops_jsonl,
            candidate_service=ctx.candidates,
        ),
        daily=DailyMemoryStore(home.owner_memory_daily_dir),
        lessons=LessonRepository(home.owner_memory_lessons_dir, home.owner_memory_routing_index_md),
    )

    report = migration.apply()

    assert report.ok and report.applied
    assert "learning_drafts" in {finding.category for finding in report.findings}
    assert not legacy.exists()
    assert before and _tree(home.owner_skill_proposals_dir) == before
    assert [item.proposal_id for item in ctx.service.list()] == [ctx.proposal.proposal_id]


def _stale_revision(ctx: SimpleNamespace) -> int:
    return 2


def _occupied_target(ctx: SimpleNamespace) -> int:
    _target(ctx).mkdir(parents=True)
    (_target(ctx) / "SKILL.md").write_text("---\nname: mine\ndescription: 用户自己的 Skill\n---\n", encoding="utf-8")
    return 1


def _changed_candidate(ctx: SimpleNamespace) -> int:
    path = ctx.home.owner_memory_candidates_jsonl
    row = json.loads(path.read_text(encoding="utf-8"))
    row["content"] = "候选正文已被改写。"
    row["content_hash"] = memory_content_hash(row["content"])
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return 1


def _redacted_candidate(ctx: SimpleNamespace) -> int:
    ctx.candidates.redact_content(content_hashes={ctx.proposal.source.content_hash})
    return 1


def _rejected_candidate(ctx: SimpleNamespace) -> int:
    ctx.candidates.transition(ctx.proposal.source.candidate_id, "rejected", reviewer="admin")
    return 1


def _deleted_candidate(ctx: SimpleNamespace) -> int:
    _rejected_candidate(ctx)
    ctx.candidates.delete_terminal([ctx.proposal.source.candidate_id])
    return 1


def _tampered_draft(ctx: SimpleNamespace) -> int:
    record = json.loads(_proposal_path(ctx).read_text(encoding="utf-8"))
    record["draft"]["body"] += "\n未经用户查看的新增内容。"
    _proposal_path(ctx).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (_stale_revision, "SKILL_PROPOSAL_REVISION_MISMATCH"),
        (_occupied_target, "SKILL_PROPOSAL_TARGET_EXISTS"),
        (_changed_candidate, "SKILL_PROPOSAL_SOURCE_CHANGED"),
        (_redacted_candidate, "SKILL_PROPOSAL_SOURCE_REDACTED"),
        (_rejected_candidate, "SKILL_PROPOSAL_SOURCE_INACTIVE"),
        (_deleted_candidate, "SKILL_PROPOSAL_SOURCE_MISSING"),
        (_tampered_draft, "SKILL_PROPOSAL_DRAFT_MISMATCH"),
    ],
)
def test_confirm_refusals_write_nothing_and_keep_proposal_pending(tmp_path: Path, mutate, code: str) -> None:
    ctx = _proposal_ctx(tmp_path)
    revision = mutate(ctx)
    proposal_before = _proposal_path(ctx).read_bytes()
    target_before = _tree(_target(ctx))

    outcome = ctx.service.confirm(ctx.proposal.proposal_id, revision)

    assert (outcome.ok, outcome.code, outcome.proposal) == (False, code, None)
    assert _proposal_path(ctx).read_bytes() == proposal_before
    assert _tree(_target(ctx)) == target_before
    assert ctx.service.show(ctx.proposal.proposal_id).status == "pending_confirmation"
    assert _staging_left(ctx) == []


def test_confirm_refuses_guard_caution_verdict(tmp_path: Path) -> None:
    ctx = _proposal_ctx(tmp_path, lesson="部署失败时不要用 chmod 777 放开整个目录，只给目标文件最小权限。")
    before = _proposal_path(ctx).read_bytes()

    outcome = ctx.service.confirm(ctx.proposal.proposal_id, 1)

    assert (outcome.ok, outcome.code) == (False, "SKILL_PROPOSAL_GUARD_BLOCKED")
    assert (outcome.detail["verdict"], outcome.detail["trust_level"]) == ("caution", "agent_generated")
    assert "insecure_perms" in outcome.detail["findings"]
    assert not _target(ctx).exists()
    assert _proposal_path(ctx).read_bytes() == before
    assert _staging_left(ctx) == []


def _raise_os_error(*_args, **_kwargs):
    raise OSError("simulated storage failure")


_FAILURE_PATCHES = {
    "install": (os, "replace", "SKILL_PROPOSAL_INSTALL_FAILED"),
    "commit": (skill_proposals, "write_json_file_atomic_unlocked", "SKILL_PROPOSAL_RECORD_WRITE_FAILED"),
}


@pytest.mark.parametrize("failure", sorted(_FAILURE_PATCHES))
def test_install_or_commit_failure_leaves_no_skill(tmp_path: Path, monkeypatch, failure: str) -> None:
    module, attribute, code = _FAILURE_PATCHES[failure]
    ctx = _proposal_ctx(tmp_path)
    before = _proposal_path(ctx).read_bytes()
    monkeypatch.setattr(module, attribute, _raise_os_error)

    outcome = ctx.service.confirm(ctx.proposal.proposal_id, 1)
    monkeypatch.undo()

    assert (outcome.ok, outcome.code) == (False, code)
    assert not _target(ctx).exists()
    assert _proposal_path(ctx).read_bytes() == before
    assert _staging_left(ctx) == []


def test_confirm_installs_skill_for_next_snapshot_only(tmp_path: Path) -> None:
    ctx = _proposal_ctx(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = SkillsService(
        home_paths=ctx.home,
        workspace_root=workspace,
        policy_provider=lambda: resolve_effective_owner_policy(ctx.home),
    )
    name = ctx.proposal.target.skill_name
    earlier = skills.snapshot_for(workspace)

    outcome = ctx.service.confirm(ctx.proposal.proposal_id, 1)
    later = skills.snapshot_for(workspace)
    entry = later.resolve(name)

    assert (outcome.ok, outcome.code) == (True, "SKILL_PROPOSAL_COMMITTED")
    assert (outcome.proposal.status, outcome.proposal.revision) == ("committed", 2)
    assert outcome.proposal.receipt["guard_verdict"] == "safe"
    assert earlier.resolve(name) is None and earlier.fingerprint != later.fingerprint
    assert (entry.stable_id, entry.content_sha256) == (f"owner:{name}", ctx.proposal.draft.sha256)
    assert LESSON in later.read_body(name)
    assert ctx.service.show(ctx.proposal.proposal_id).revision == 2
    assert ctx.service.confirm(ctx.proposal.proposal_id, 2).code == "SKILL_PROPOSAL_NOT_PENDING"
    assert _staging_left(ctx) == []


def test_hostile_lesson_text_round_trips_through_skill_parser(tmp_path: Path) -> None:
    ctx = _proposal_ctx(tmp_path, lesson='C# 项目里 "配置" 先校验 # 再写入\r\n第二行说明 "引号结尾"')
    name = ctx.proposal.target.skill_name
    draft = ctx.proposal.draft
    path = tmp_path / "parsed" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(render_skill_markdown(name, draft), encoding="utf-8")

    card = parse_skill_file(path, require_frontmatter=True)

    assert (card.name, card.description, card.when_to_use) == (name, draft.description, draft.when_to_use)
    assert "\r" not in draft.body and "C# 项目里" in draft.body


def test_invalid_missing_and_corrupt_proposals_return_structured_codes(tmp_path: Path) -> None:
    ctx = _proposal_ctx(tmp_path)
    _proposal_path(ctx).write_text("{not json", encoding="utf-8")

    assert ctx.service.confirm("../../escape", 1).code == "SKILL_PROPOSAL_INVALID_ID"
    assert ctx.service.reject("0" * 24, 1).code == "SKILL_PROPOSAL_NOT_FOUND"
    assert ctx.service.confirm(ctx.proposal.proposal_id, 1).code == "SKILL_PROPOSAL_CORRUPT"
    with pytest.raises(SkillProposalError) as listing:
        ctx.service.list()
    assert listing.value.code == "SKILL_PROPOSAL_CORRUPT"
    assert not _target(ctx).exists()


class _ExplodingProposals:
    def propose_from_candidates(self, candidates):
        raise RuntimeError("proposal store unavailable")


def _run_with(ctx: SimpleNamespace, proposals: object) -> tuple:
    ctx.manager.skill_proposals = proposals
    task = ctx.manager.create_run(goal=GOAL, thought="记录经验", plan=["整理"])
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="已完成", lessons=[LESSON])
    result = ctx.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="已完成",
            status="DONE",
            turn_end_reason="completed",
            structured_output=structured,
        )
    )
    saved = ctx.manager.load(task.id)
    return result, saved, Path(saved.work_log_file).read_text(encoding="utf-8")


def test_proposal_failure_never_affects_runner_result(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)

    result, saved, work_log = _run_with(ctx, _ExplodingProposals())

    assert result.ok is True and saved.status == "DONE"
    assert [item.candidate_type for item in ctx.candidates.list()] == ["lesson"]
    assert "skill_proposals_error=RuntimeError" in work_log
    assert not ctx.home.owner_skill_proposals_dir.exists()


def test_runner_result_auto_confirms_proposal_when_service_attached(tmp_path: Path) -> None:
    # 用户 2026-09-26 决定自学习不逐条审批：服务接上（自学习开启）时新提案立即以 actor=auto 走原确认链。
    ctx = _runtime(tmp_path)

    result, saved, work_log = _run_with(ctx, ctx.service)
    [proposal] = ctx.service.list()

    assert result.ok is True and "skill_proposals=1 skill_proposals_committed=1" in work_log
    assert (proposal.status, proposal.source.run_ids) == ("committed", (saved.id,))
    assert proposal.receipt["confirmed_by"] == "auto" and proposal.receipt["guard_verdict"] == "safe"
    assert (ctx.home.owner_home_dir / "skills" / proposal.target.skill_name / "SKILL.md").is_file()


def test_runner_result_without_service_records_candidate_only(tmp_path: Path) -> None:
    ctx = _runtime(tmp_path)

    result, _saved, work_log = _run_with(ctx, None)

    assert result.ok is True
    assert [item.candidate_type for item in ctx.candidates.list()] == ["lesson"]
    assert "skill_proposals" not in work_log
    assert not ctx.home.owner_skill_proposals_dir.exists()


def _config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(f'my_agent_home: "{tmp_path / "home"}"\n', encoding="utf-8")
    return config_path


def _cli(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    code = cli_main(["--config", str(config_path), "skills", "proposals", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_list_show_confirm_reject_round_trip(tmp_path: Path, capsys) -> None:
    ctx = _runtime(tmp_path)
    first = ctx.service.propose_from_candidates(_record_lesson(ctx))[0]
    second = ctx.service.propose_from_candidates(_record_lesson(ctx, "写入用户文件前先备份原文件。", "run-2"))[0]
    config_path = _config(tmp_path)

    listed = _cli(capsys, config_path, "list")
    shown = _cli(capsys, config_path, "show", first.proposal_id)
    confirmed = _cli(capsys, config_path, "confirm", first.proposal_id, "--expected-revision", "1")
    rejected = _cli(capsys, config_path, "reject", second.proposal_id, "--expected-revision", "1")
    pending = _cli(capsys, config_path, "list", "--status", "pending_confirmation")
    again = _cli(capsys, config_path, "confirm", first.proposal_id, "--expected-revision", "2")

    assert listed[0] == 0 and listed[1]["self_learning_enabled"] is False
    assert {item["proposal_id"] for item in listed[1]["proposals"]} == {first.proposal_id, second.proposal_id}
    assert shown[0] == 0 and shown[1]["proposal"]["draft"]["sha256"] == first.draft.sha256
    assert confirmed[0] == 0 and confirmed[1]["code"] == "SKILL_PROPOSAL_COMMITTED"
    assert confirmed[1]["proposal"]["status"] == "committed"
    assert (ctx.home.owner_home_dir / "skills" / first.target.skill_name / "SKILL.md").is_file()
    assert rejected[0] == 0 and rejected[1]["proposal"]["status"] == "rejected"
    assert not (ctx.home.owner_home_dir / "skills" / second.target.skill_name).exists()
    assert pending[0] == 0 and pending[1]["proposals"] == []
    assert again[0] == 1 and again[1]["error_code"] == "SKILL_PROPOSAL_NOT_PENDING"


def test_cli_human_output_shows_review_facts(tmp_path: Path, capsys) -> None:
    ctx = _proposal_ctx(tmp_path)

    code = cli_main(["--config", str(_config(tmp_path)), "skills", "proposals", "show", ctx.proposal.proposal_id])
    output = capsys.readouterr().out

    assert code == 0
    for text in (ctx.proposal.proposal_id, "subagent_lesson_candidate", "project-root", LESSON, GOAL):
        assert text in output
    assert f"--expected-revision {ctx.proposal.revision}" in output
