from __future__ import annotations

"""自学习 S3 接线：组合根装配、收口入队、Gateway 策展车道、`skills learned` CLI、S1 自动确认与配置。"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _finalization_service as finalization
from agent_py_agent.agent.agent_core._finalization_service import _request_skill_learning
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.runtime import goal_accounting
from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_prompt import OUTPUT_SCHEMA_VERSION
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_proposals import SkillProposalService
from agent_py_agent.agent.capability.task_learning_bridge import (
    TaskLearningEvidence,
    enqueue_completed_task_learning,
)
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessExecutionContext,
    TrustedTaskCompletion,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.parser import main as cli_main

NAME = "release-notes-draft"
BODY = (
    "# 起草发布说明\n\n## 步骤\n\n1. 用 git log 列出上次发布之后的提交，只看合入主干的那些。\n"
    "2. 按功能、修复、文档三类归并，每类最多五条，写用户能看懂的结果而不是实现细节。\n"
    "3. 发布前让负责人核对版本号和日期。\n\n## 验证方法\n\n对照提交列表确认没有漏掉用户可见的改动。"
)


def test_agent_wires_skill_learning_only_when_self_learning_is_on(tmp_path: Path) -> None:
    off = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "off"), prompt_files=[]), tmp_path / "ws-off")
    on = SimpleAgent(
        AgentConfig(my_agent_home=str(tmp_path / "on"), prompt_files=[], enable_self_learning=True,
                     self_learning_min_tool_rounds=4, self_learning_daily_limit=7, my_agent_owner_provider="feishu",
                     my_agent_owner_kind="user", my_agent_owner_id="ou_1"),
        tmp_path / "ws-on",
    )
    service = on.skill_learning

    assert off.skill_learning is None
    assert isinstance(service, SkillLearningService)
    assert service.runtime.owner_id == str(on.home_paths.owner_id) and "ou_1" in service.runtime.owner_id
    assert service.store.directory == on.home_paths.owner_home_dir / "data" / "skill_learning"
    assert service.store.learned_root == on.home_paths.owner_home_dir / "skills" / "learned"
    assert service.runtime.backend is on.memory_curator.backend
    assert (service.settings.min_tool_rounds, service.settings.daily_limit) == (4, 7)
    assert not service.store.directory.exists()


def test_finalize_helper_enqueues_and_never_raises() -> None:
    seen: list[object] = []
    ctx = SimpleNamespace(run_id="run-1")

    _request_skill_learning(SimpleNamespace(skill_learning=SimpleNamespace(enqueue_from_finalize=seen.append)), ctx)
    _request_skill_learning(SimpleNamespace(skill_learning=None), ctx)
    _request_skill_learning(
        SimpleNamespace(skill_learning=SimpleNamespace(enqueue_from_finalize=lambda _ctx: 1 / 0)), ctx
    )

    assert seen == [ctx]


def _finalize_ctx(completed: bool) -> FinalizeContext:
    return FinalizeContext(
        user_prompt="合并表格", final_prompt="", final_response=SimpleNamespace(text="已完成"), memories=[],
        executed_tools=[], archive_tool_calls=[], routed_context=None, resume_context_result=None, runtime_injections=[],
        compression_snapshot_id="", compression_snapshot_path="", compression_applied=False, request_id="req-1",
        run_id="run-1", task_id="task-1", source="tui", do_save=True,
        task_attributes={"conversation_task_completed": completed}, recovery_task_refs=None,
        recovery_content_paths=None, recovery_next_actions=None, tool_rounds=9,
    )


def test_finalize_submits_learning_only_after_the_task_completes(monkeypatch) -> None:
    seen: list[object] = []
    agent = SimpleNamespace(skill_learning=SimpleNamespace(enqueue_from_finalize=seen.append), memory_curator=None)
    service = finalization.FinalizationService(agent)
    for name in ("_archive_run_if_needed", "_write_runtime_fact_source_if_needed", "settle_model_usage",
                 "_update_main_context_bundle_artifacts", "_estimate_token_usage", "_build_agent_run_result"):
        monkeypatch.setattr(service, name, lambda *_args, **_kwargs: {})
    for name in ("_current_model_call_summary", "_estimate_token_params", "_schedule_typed_unfinished_continuation"):
        monkeypatch.setattr(finalization, name, lambda *_args, **_kwargs: {})
    monkeypatch.setattr(finalization, "_conversation_turn_is_terminal", lambda _ctx: False)
    monkeypatch.setattr(goal_accounting, "finish_goal_turn_accounting", lambda *_args: None)
    done, open_task = _finalize_ctx(True), _finalize_ctx(False)

    service.finalize(open_task)
    service.finalize(done)

    assert seen == [done]


def _lane_agent(*, memory_enabled: bool, pending: bool, calls: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        owner_id="owner-a",
        owner_policy=SimpleNamespace(memory_enabled=memory_enabled),
        memory_curator=SimpleNamespace(run_if_due=lambda: calls.append("curator")),
        skill_learning=SimpleNamespace(has_pending=lambda: pending, run_pending=lambda: calls.append("learning")),
    )


def _lane_supervisor(config: SimpleNamespace) -> object:
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=config)
    supervisor._curator_inflight = {}
    supervisor._curator_quota_count = 0
    supervisor._curator_quota_date = ""
    supervisor._curator_quota_path = None
    return supervisor


def test_lane_admits_pending_learning_even_when_memory_is_disabled() -> None:
    supervisor = _lane_supervisor(SimpleNamespace(memory_curator_enabled=True))
    calls: list[str] = []
    learning_only = _lane_agent(memory_enabled=False, pending=True, calls=calls)
    idle = _lane_agent(memory_enabled=False, pending=False, calls=calls)

    assert gateway_loops._curator_candidate_is_admitted(supervisor, learning_only, is_soft=False, pool=None)
    assert not gateway_loops._curator_candidate_is_admitted(supervisor, idle, is_soft=False, pool=None)
    supervisor._safe_run_curator(learning_only, "owner-a")
    assert calls == ["learning"]


def test_lane_runs_curator_then_learning_and_isolates_learning_failures() -> None:
    supervisor = _lane_supervisor(SimpleNamespace(memory_curator_enabled=True))
    calls: list[str] = []
    agent = _lane_agent(memory_enabled=True, pending=True, calls=calls)
    supervisor._safe_run_curator(agent, "owner-a")
    agent.skill_learning.run_pending = lambda: 1 / 0
    supervisor._safe_run_curator(agent, "owner-a")
    agent.config = SimpleNamespace(memory_curator_enabled=False)
    supervisor._safe_run_curator(agent, "owner-a")

    assert calls == ["curator", "learning", "curator"]
    assert gateway_loops._background_curation_enabled(
        SimpleNamespace(config=SimpleNamespace(memory_curator_enabled=False, enable_self_learning=True))
    )
    assert not gateway_loops._background_curation_enabled(
        SimpleNamespace(config=SimpleNamespace(memory_curator_enabled=False, enable_self_learning=False))
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "agent_config.yaml"
    path.write_text(f'my_agent_home: "{tmp_path / "home"}"\nenable_self_learning: true\n', encoding="utf-8")
    return path


def _published_store(tmp_path: Path) -> SkillLearningStore:
    home = ensure_my_agent_home(tmp_path / "home")
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION, "decision": "create", "reason": "发布流程可复用",
        "update_target": "", "name": NAME, "description": "按提交记录起草发布说明",
        "when_to_use": "准备对外发布版本时", "tags": ["release"], "body": BODY,
    }
    backend = SimpleNamespace(generate_structured=lambda prompt, response_schema, messages=None: SimpleNamespace(
        text=json.dumps(output, ensure_ascii=False)))
    service = SkillLearningService(
        SkillLearningStore.for_home(home), SkillLearningSettings(min_tool_rounds=1),
        SkillLearningRuntime(backend=backend, snapshot_provider=lambda: SimpleNamespace(entries=())),
    )
    ctx = SimpleNamespace(do_save=True, context_scope="default", source="tui", tool_rounds=3, run_id="run-1",
                          request_id="req-1", task_id="task-1", task_attributes={}, user_prompt="写发布说明",
                          final_response=SimpleNamespace(text="已完成"), archive_tool_calls=[])
    service.enqueue_from_finalize(ctx)
    assert service.run_pending().status == "proposed"
    from agent_py_agent.agent.capability.skill_learning_proposals import (
        list_learning_proposals,
        resolve_learning_proposal,
    )
    proposal = list_learning_proposals(service.store)[0]
    assert resolve_learning_proposal(service.store,proposal['proposal_id'],1,decision='confirm')['status'] == 'committed'
    return service.store


def _cli(capsys, config: Path, *argv: str) -> tuple[int, dict]:
    code = cli_main(["--config", str(config), "skills", "learned", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_list_show_revert_remove_round_trip(tmp_path: Path, capsys) -> None:
    store = _published_store(tmp_path)
    config = _config_file(tmp_path)

    listed = _cli(capsys, config, "list")
    shown = _cli(capsys, config, "show", NAME)
    missing = _cli(capsys, config, "show", "no-such-skill")
    human_code = cli_main(["--config", str(config), "skills", "learned", "list"])
    human = capsys.readouterr().out
    skill_md = store.learned_root / NAME / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\n我自己补的一句。\n", encoding="utf-8")
    edited = _cli(capsys, config, "list")
    refused = _cli(capsys, config, "revert", NAME)
    removed = _cli(capsys, config, "remove", NAME)
    again = _cli(capsys, config, "remove", NAME)

    assert listed[0] == 0 and listed[1]["self_learning_enabled"] is True and listed[1]["calls_today"] == 1
    assert [(item["name"], item["state"], item["version"]) for item in listed[1]["skills"]] == [(NAME, "active", 1)]
    assert shown[0] == 0 and [event["event"] for event in shown[1]["events"]] == ["proposed", "published"]
    assert missing[0] == 1 and missing[1]["error_code"] == "SKILL_LEARNING_NOT_LEARNED"
    assert human_code == 0 and NAME in human and "今日总结调用 1/20 次" in human
    assert [item["state"] for item in edited[1]["skills"]] == ["user_modified"]
    assert refused[0] == 1 and refused[1]["error_code"] == "SKILL_LEARNING_USER_MODIFIED"
    assert removed[0] == 0 and removed[1]["code"] == "removed"
    assert again[0] == 1 and again[1]["error_code"] == "SKILL_LEARNING_NOT_LEARNED"
    assert [event["event"] for event in store.events(NAME, 0)] == ["proposed", "published", "removed"]
    assert NAME in store.load_registry().blocked_names and not (store.learned_root / NAME).exists()


def _subagent_task(run_id: str) -> SubAgentTask:
    return SubAgentTask(id=run_id, root_id="project-root", goal="修复并发写入导致的状态覆盖", thought="", plan=[],
                        output_json=f"/artifacts/{run_id}/output.json", updated_at=100.0)


def test_subagent_lesson_proposals_wait_for_user_confirmation(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=CandidateService(home.owner_memory_candidates_jsonl))
    manager.skill_proposals = SkillProposalService(home)
    task = _subagent_task("run-1")
    manager.save(task)
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="已完成",
                                      lessons=["修改共享状态前先读取当前版本，再用精确版本做比较交换写入。"])

    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=task.id, dry_run=False, ok=True, message="已完成", status="DONE",
        turn_end_reason="completed", structured_output=structured,
    ))
    [proposal] = manager.skill_proposals.list()
    work_log = Path(manager.load(task.id).work_log_file).read_text(encoding="utf-8")

    assert result.ok is True and proposal.status == "pending_confirmation" and not proposal.receipt
    assert not (home.owner_home_dir / "skills" / proposal.target.skill_name / "SKILL.md").exists()
    assert manager.skill_proposals.confirm(proposal.proposal_id,proposal.revision).ok
    assert (home.owner_home_dir / "skills" / proposal.target.skill_name / "SKILL.md").is_file()
    assert "skill_proposals=1 skill_proposals_committed=0" in work_log


def test_self_learning_budget_keys_ship_in_yaml_dataclass_and_normalizer() -> None:
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    normalized, warnings = normalize_agent_config({
        "self_learning_min_tool_rounds": "0",
        "self_learning_daily_limit": "5",
        "self_learning_max_skills": -1,
        "self_learning_timeout_seconds": 3,
    })

    assert shipped.enable_self_learning is False
    assert (shipped.self_learning_min_tool_rounds, shipped.self_learning_daily_limit) == (6, 20)
    assert (shipped.self_learning_max_skills, shipped.self_learning_timeout_seconds) == (0, 180)
    assert normalized["self_learning_min_tool_rounds"] == 6 and normalized["self_learning_daily_limit"] == 5
    assert normalized["self_learning_max_skills"] == 0 and normalized["self_learning_timeout_seconds"] == 180
    assert len(warnings) == 3


def _completed_learning_host(tmp_path):
    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )
    from agent_py_agent.tests.test_skill_learning import _ctx, _output, _runtime
    ctx = _runtime(tmp_path, _output('skip', name='', description='', when_to_use='', tags=[], body=''),
                   settings=SkillLearningSettings(min_tool_rounds=6, include_background=True))
    ctx.home = home_paths_with_owner(ctx.home, ensure_owner_home(ctx.home.root, OwnerIdentity.local_main()))
    ctx.skills.home_paths = ctx.home
    ctx.skills.policy_provider = lambda: resolve_effective_owner_policy(ctx.home)
    ctx.service.runtime = replace(ctx.service.runtime, owner_id=ctx.home.owner_id)
    material = _ctx('run', do_save=False, source='background_main_agent', tool_rounds=6,
                    attempt_id='attempt', trajectory_digest='trajectory-digest', task_id='runtime-task',
                    task_attributes={'conversation_thread_id': 'thread', 'session_task_ref': 'stask-1'})
    business = BusinessExecutionContext(str(ctx.home.owner_id), 'space', 'project', 'stask-1', 2, None,
                                        ('owner:method',), ('asset-version',), 'business-context.v1')
    completion = TrustedTaskCompletion(str(ctx.home.owner_id), 'thread', 'stask-1', 'run', 'attempt',
                                       'completed', 'claim', 'completion', 'space', 'project',
                                       'background_main_agent', 6, ('artifact',), 'trajectory-digest')

    class Host:
        def __init__(self):
            self.home_paths, self.skill_learning = ctx.home, ctx.service
            self.config = SimpleNamespace(enable_self_learning=True)
            self.evidence = TaskLearningEvidence(completion, material, business)
            self.canonical_completed = True

        def load_task_learning_evidence(self, candidate):
            return self.evidence if self.canonical_completed and candidate == self.evidence.completion else None

    return ctx, Host(), completion


def test_trusted_background_completion_save_false_uses_original_fifo_and_replays_once(tmp_path):
    ctx, host, completion = _completed_learning_host(tmp_path)
    result = enqueue_completed_task_learning(completion=completion, host=host)
    assert result['status'] == 'enqueued'
    assert ctx.backend.prompts == [] and host.evidence.context.do_save is False
    [path] = ctx.store.pending_requests()
    request = ctx.store.read_request(path)
    assert request['source'] == 'background_main_agent' and request['task_id'] == 'stask-1'
    assert request['runtime_task_id'] == 'runtime-task'
    assert request['attempt_id'] == 'attempt' and request['completion_ref'] == 'completion'
    assert request['business_context']['asset_version_refs'] == ['asset-version']
    assert request['result_refs'] == ['artifact'] and request['learning_scope']['space_id'] == 'space'
    host.skill_learning = SkillLearningService(SkillLearningStore.for_home(ctx.home), ctx.service.settings, ctx.service.runtime)
    assert enqueue_completed_task_learning(completion=completion, host=host)['status'] == 'duplicate'
    assert ctx.service.run_pending().status == 'skipped'
    assert enqueue_completed_task_learning(completion=completion, host=host)['status'] == 'duplicate'
    assert ctx.store.pending_requests() == []


@pytest.mark.parametrize('case', ['disabled', 'excluded', 'short', 'unknown', 'claim', 'owner', 'attempt', 'space', 'task_local', 'json'])
def test_completed_learning_rejects_unverified_or_ineligible_sources_without_writes(tmp_path, case):
    ctx, host, completion = _completed_learning_host(tmp_path)
    if case == 'disabled':
        host.config.enable_self_learning = False
    elif case == 'excluded':
        ctx.service.settings = replace(ctx.service.settings, include_background=False)
    elif case == 'claim':
        host.canonical_completed = False
    elif case == 'task_local':
        host.evidence.context.context_scope = 'task_local'
    elif case == 'short':
        completion = replace(completion, tool_rounds=5)
        host.evidence.context.tool_rounds = 5
    elif case == 'unknown':
        completion = replace(completion, source_kind='unknown_source')
        host.evidence.context.source = 'unknown_source'
    elif case in {'owner', 'attempt', 'space'}:
        field = {'owner': 'owner_id', 'attempt': 'attempt_id', 'space': 'space_id'}[case]
        completion = replace(completion, **{field: 'other'})
    elif case == 'json':
        completion = {**completion.to_dict(), 'trusted': True}
    if isinstance(completion, TrustedTaskCompletion) and case not in {'claim', 'json'}:
        host.evidence = replace(host.evidence, completion=completion)
    assert enqueue_completed_task_learning(completion=completion, host=host)['status'] in {'ineligible', 'conflict'}
    assert ctx.store.pending_requests() == [] and not ctx.store.registry_path.exists()
    assert ctx.backend.prompts == [] and host.evidence.context.do_save is False


def test_original_finalize_consumes_only_host_bound_completion_evidence(tmp_path):
    ctx, host, completion = _completed_learning_host(tmp_path)
    material = host.evidence.context
    assert ctx.service.enqueue_from_finalize(material) == 'ineligible'
    material.trusted_task_completion = completion
    ctx.service.runtime = replace(ctx.service.runtime, task_completion_host=host)
    assert ctx.service.enqueue_from_finalize(material) == 'queued'
    assert material.do_save is False and material.source == 'background_main_agent'
    assert len(ctx.store.pending_requests()) == 1


def test_completion_queue_storage_error_is_not_an_enqueued_receipt(tmp_path, monkeypatch):
    ctx, host, completion = _completed_learning_host(tmp_path)
    def fail(_request):
        raise OSError('original FIFO unavailable')
    monkeypatch.setattr(ctx.store, 'enqueue', fail)
    with pytest.raises(OSError):
        enqueue_completed_task_learning(completion=completion, host=host)


def test_confirmed_background_method_keeps_business_refs_in_original_asset_registry(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_proposals import (
        list_learning_proposals,
        resolve_learning_proposal,
    )
    from agent_py_agent.tests.test_skill_learning import _output

    ctx, host, completion = _completed_learning_host(tmp_path)
    ctx.backend.responses[:] = [_output()]
    # _runtime的旧测试子类会自动确认提案；本用例使用原服务验证实际确认链。
    ctx.service = SkillLearningService(ctx.store, replace(ctx.service.settings, autonomous_verified=False), ctx.service.runtime)
    host.skill_learning = ctx.service
    assert enqueue_completed_task_learning(completion=completion, host=host)['status'] == 'enqueued'
    assert ctx.service.run_pending().status == 'proposed'
    [proposal] = list_learning_proposals(ctx.store)
    assert resolve_learning_proposal(ctx.store, proposal['proposal_id'], 1, decision='confirm')['status'] == 'committed'
    [asset] = [row for row in ctx.store.load_registry().assets.values() if row['kind'] == 'skill']
    source = asset['provenance'][-1]
    assert source['origin'] == 'S3'
    assert source['task_id'] == completion.task_id and source['runtime_task_id'] == 'runtime-task'
    assert source['business_context'] == host.evidence.business_context.to_dict()
    assert source['learning_scope'] == {'kind': 'business_method', 'space_id': 'space', 'project_ref': 'project'}
    assert source['result_refs'] == ['artifact'] and source['asset_version_refs'] == ['asset-version']
    assert source['claim_ref'] == 'claim' and source['completion_ref'] == 'completion'
    assert host.evidence.context.do_save is False
