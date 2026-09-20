from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_models import CuratorRunResult
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli import memory_admin_commands
from agent_py_agent.cli.parser import build_parser


class _CuratorProbe:
    def __init__(self, daily_store: DailyMemoryStore) -> None:
        self.daily_store = daily_store
        self.reasons: list[str] = []
        self.force_values: list[bool] = []

    def request(self, reason: str) -> dict[str, object]:
        self.reasons.append(reason)
        return {"requested": True, "reason": reason, "pending_reasons": [reason]}

    def run(self, *, reason: str, force: bool = False) -> CuratorRunResult:
        self.reasons.append(reason)
        self.force_values.append(force)
        return CuratorRunResult(
            run_id="memory-curator-run-cli",
            status="succeeded",
            reason=reason,
            provider="probe-provider",
            model="probe-model",
        )

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "provider": "probe-provider",
            "model": "probe-model",
            "state": {"pending_reasons": list(self.reasons)},
        }


def _agent(tmp_path: Path) -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    long_term = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
    )
    lessons = LessonRepository(
        home.owner_memory_lessons_dir,
        home.owner_memory_routing_index_md,
    )
    persona = PersonaRepository.from_home_paths(home)
    promotion = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=candidates,
            long_term=long_term,
            persona=persona,
            lessons=lessons,
            hot=HotRuleRepository(home.owner_memory_hot_md),
        ),
    )
    conversation_root = home.owner_sessions_dir / "conversation"
    conversation_root.mkdir(parents=True)
    config = SimpleNamespace(
        memory_archive_level=3,
        memory_hook_enabled=True,
        memory_hook_archive_level=3,
        memory_hook_retention_days=7,
        memory_rule_routing_enabled=True,
        memory_rule_routing_mode="soft",
        memory_rule_auto_read_limit=3,
        memory_rule_receipt_enabled=False,
        memory_doctor_recent_archive_file_limit=5,
    )
    return SimpleNamespace(
        root=workspace,
        effective_workspace_roots=(workspace,),
        workspace_roots=(workspace,),
        home_paths=home,
        config=config,
        memory_candidates=candidates,
        memory=long_term,
        memory_lessons=lessons,
        memory_promotion=promotion,
        memory_curator=_CuratorProbe(DailyMemoryStore(home.owner_memory_daily_dir)),
        conversation_store=SimpleNamespace(storage=SimpleNamespace(root=conversation_root)),
    )


def _run_json(monkeypatch, capsys, agent: SimpleNamespace, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(memory_admin_commands, "make_agent", lambda _args: agent)
    args = build_parser().parse_args(["memory", *argv, "--json"])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


def _candidate(agent: SimpleNamespace):
    return agent.memory_candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="用户个人开发电脑使用 macOS。",
            subject_key="user.device.personal.os",
            scope=MemoryScope("personal", "personal"),
            origin="reviewed",
            promotion_target="long_term",
            observation_id="cli-review-source-1",
        )
    )


def test_memory_admin_parser_exposes_one_nested_chinese_command_tree(capsys) -> None:
    parser = build_parser()
    args = parser.parse_args(["memory", "candidates", "list"])
    assert args.func is memory_admin_commands.cmd_memory_candidates_list
    try:
        parser.parse_args(["memory", "candidates", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    help_text = capsys.readouterr().out
    assert "唯一候选事实源" in help_text
    assert "blocked_missing_evidence=缺少证据" in help_text.replace("\n", "")


def test_candidate_list_review_and_promote_share_canonical_services(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    agent = _agent(tmp_path)
    candidate = _candidate(agent)
    code, listed = _run_json(monkeypatch, capsys, agent, "candidates", "list")
    assert code == 0
    assert listed["candidate_source"] == str(agent.memory_candidates.path)
    assert listed["candidates"][0]["status"] == "pending_review"

    code, reviewed = _run_json(
        monkeypatch,
        capsys,
        agent,
        "candidates",
        "review",
        candidate.candidate_id,
        "--decision",
        "approve",
        "--note",
        "证据与范围已人工核对。",
    )
    assert code == 0
    assert reviewed["candidate"]["status"] == "approved"

    code, promoted = _run_json(
        monkeypatch,
        capsys,
        agent,
        "candidates",
        "promote",
        candidate.candidate_id,
    )
    assert code == 0
    assert promoted["promotion"]["promoted"] is True
    assert agent.memory_candidates.get(candidate.candidate_id).status == "promoted"
    assert [record.content for record in agent.memory.all()] == ["用户个人开发电脑使用 macOS。"]


def test_curator_admin_run_persists_fixed_reason_before_same_service_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    agent = _agent(tmp_path)
    code, payload = _run_json(monkeypatch, capsys, agent, "curator", "run", "--force")
    assert code == 0
    assert payload["request"]["reason"] == "admin"
    assert payload["run"]["reason"] == "admin"
    assert agent.memory_curator.reasons == ["admin", "admin"]
    assert agent.memory_curator.force_values == [True]


def test_retention_migration_and_doctor_use_v2_services(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    agent = _agent(tmp_path)
    code, retention = _run_json(monkeypatch, capsys, agent, "retention", "plan")
    assert code == 0
    assert retention["retention"]["schema_version"] == "my-agent.memory-retention-plan.v2"
    assert retention["retention"]["applied"] is False

    marker = agent.home_paths.owner_memory_dir / "migration.json"
    code, migration = _run_json(monkeypatch, capsys, agent, "migrate")
    assert code == 0
    assert migration["mode"] == "dry-run"
    assert not marker.exists()

    code, doctor = _run_json(monkeypatch, capsys, agent, "doctor")
    assert code == 0
    assert doctor["candidate"]["schema_version"] == "my-agent.memory-candidate.v2"
    assert doctor["curator"]["provider"] == "probe-provider"
    assert doctor["migration"]["applied"] is False
    assert doctor["retention"]["applied"] is False


def test_migration_apply_and_retention_apply_are_explicit_subcommands(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    agent = _agent(tmp_path)
    code, migration = _run_json(monkeypatch, capsys, agent, "migrate", "--apply")
    assert code == 0
    assert migration["mode"] == "apply"
    assert (agent.home_paths.owner_memory_dir / "migration.json").exists()

    code, retention = _run_json(monkeypatch, capsys, agent, "retention", "apply")
    assert code == 0
    assert retention["retention"]["applied"] is True
