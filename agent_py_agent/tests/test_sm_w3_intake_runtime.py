"""W3 合同与运行时测试：动态服务装配、50 技能合并优先、275+ 技能防截断、通道 B、多用户分页发现、载体入队与缺席模型降级。"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.router import tokenize
from agent_py_agent.agent.capability.skill_learning import (
    CODE_INEFFECTIVE_RECORDED,
    CODE_INEFFECTIVE_RETIRED,
    CODE_INEFFECTIVE_ROLLED_BACK,
    CODE_INEFFECTIVE_UNATTRIBUTED,
    CODE_MERGE_DEFERRED,
    CODE_MODEL_TIMEOUT,
    STATUS_BUSY,
    STATUS_RETRY,
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
    _adopt_committed_lessons_isolated,
    _metadata_similarity_tokens,
    _prune_learning_proposals_isolated,
    _similar_managed_skills,
)
from agent_py_agent.agent.capability.skill_learning_prompt import (
    OUTPUT_SCHEMA_VERSION,
    SkillLearningDecision,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_proposals import SkillProposalService
from agent_py_agent.agent.capability.skill_usage_receipts import record_exact_use
from agent_py_agent.agent.common.request_execution_ports import gateway_short_transaction
from agent_py_agent.agent.conversation.learning_source import (
    TRIGGER_USER_DIALOGUE,
    subagent_projection,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
    _owner_scan_page_limit,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import _execute_learning
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_backend import CuratorModelStillRunningError
from agent_py_agent.agent.owner_wake_discovery import discover_owner_home_page
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.curator_profile import (
    auxiliary_profile_cooldown_remaining,
    clear_auxiliary_profile_cooldown,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.gateway_loops import (
    _curator_maintenance_due,
    _run_memory_curator_maintenance,
    _run_skill_learning,
    _skill_learning_pending,
)
from agent_py_agent.tests.test_skill_learning import BODY, _ctx, _FakeBackend, _output
from agent_py_agent.tests.test_skill_proposals import seed_proposals_from_candidates


def _finalize_ctx(
    run_id: str = "run-1",
    *,
    task_id: str = "",
    root_task_id: str = "",
    used_skill_ids: tuple[str, ...] = (),
) -> SimpleNamespace:
    ctx = _ctx(
        run_id,
        task_id=task_id or f"task-{run_id}",
        root_task_id=root_task_id or f"task-{run_id}",
    )
    ctx.archive_tool_calls = [
        {
            "tool": "skill_search",
            "ok": True,
            "parameters": {"action": "get", "skill_id": sid},
            "output": {"skill_id": sid},
        }
        for sid in used_skill_ids
    ]
    return ctx


def _skill_candidate(
    service,
    run_id: str,
    *,
    body: str,
    applies_when: str = "需要严格遵循既定步骤校对并导出数据时触发。",
):
    return service.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=body,
            subject_key="shared-registry-cas",
            scope=MemoryScope(scope_type="project", scope_key="project:ws-1", applies_when=applies_when),
            origin="subagent_lesson",
            source_task_ids=("task-sub-1",),
            source_run_ids=(run_id,),
            evidence_refs=({"kind": "tool_run", "ref_id": run_id},),
            learning_source=subagent_projection(
                trigger_kind=TRIGGER_USER_DIALOGUE, run_ids=(run_id,), task_ids=("task-sub-1",)
            ),
        )
    )


def test_agent_skill_learning_live_toggle_and_providers(tmp_path: Path) -> None:
    cfg_path = tmp_path / "user_config.yaml"
    cfg_path.write_text(
        f"my_agent_home: {tmp_path / 'home'}\n"
        "enable_self_learning: false\n"
        "self_learning_recommended_explore_percent: 12\n"
        "self_learning_recommended_limit: 9\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    cfg = replace(cfg, prompt_files=[])
    agent = SimpleAgent(cfg, tmp_path / "ws")

    assert agent.skill_learning is None
    assert not (agent.home_paths.owner_home_dir / "data" / "skill_learning").exists()
    assert agent.skills_service.learning_explore_percent_provider() == 12
    assert agent.skills_service.learning_recommended_limit_provider() == 9

    cfg_path.write_text(
        f"my_agent_home: {tmp_path / 'home'}\n"
        "enable_self_learning: true\n"
        "self_learning_recommended_explore_percent: 25\n"
        "self_learning_recommended_limit: 14\n",
        encoding="utf-8",
    )
    svc = agent.skill_learning
    assert isinstance(svc, SkillLearningService)
    assert not (agent.home_paths.owner_home_dir / "data" / "skill_learning").exists()
    assert agent.skills_service.learning_explore_percent_provider() == 25
    assert agent.skills_service.learning_recommended_limit_provider() == 14
    curator_cfg = agent.memory_curator.dependencies.config_provider()
    assert curator_cfg.enable_self_learning is True
    assert curator_cfg.self_learning_recommended_limit == 14

    custom = SimpleNamespace(marker="injected")
    agent.skill_learning = custom
    assert agent.skill_learning is custom
    agent.skill_learning = None

    cfg_path.write_text(
        f"my_agent_home: {tmp_path / 'home'}\n"
        "enable_self_learning: false\n",
        encoding="utf-8",
    )
    assert agent.skill_learning is None


def test_usage_runtime_provider_enforces_execution_mode_isolation(tmp_path: Path) -> None:
    managed = SimpleAgent(
        AgentConfig(
            my_agent_home=str(tmp_path / "managed-home"),
            prompt_files=[],
            execution_mode="managed",
            enable_self_learning=True,
        ),
        tmp_path / "ws-managed",
    )
    unmanaged_home = ensure_my_agent_home(tmp_path / "unmanaged-home")
    # 预先写入遗留 runtime.db 占位文件，验证 LOCAL_UNMANAGED 仍绝对禁止打开该库。
    runtime_db_path(unmanaged_home.owner_home_dir).write_text("leftover", encoding="utf-8")
    unmanaged = SimpleAgent(
        AgentConfig(
            my_agent_home=str(tmp_path / "unmanaged-home"),
            prompt_files=[],
            execution_mode="local_unmanaged",
            enable_self_learning=True,
        ),
        tmp_path / "ws-unmanaged",
    )

    assert managed.skill_learning is not None
    assert callable(managed.skill_learning.store._usage_runtime_provider)
    assert isinstance(managed.skill_learning.store._usage_runtime_provider(), RuntimeRepository)

    assert unmanaged.skill_learning is not None
    assert callable(unmanaged.skill_learning.store._usage_runtime_provider)
    assert unmanaged.skill_learning.store._usage_runtime_provider() is None


def test_live_switch_abort_before_commit_writes_zero_skills(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    cfg = AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[], enable_self_learning=True)
    store = SkillLearningStore.for_home(home)
    backend = _FakeBackend(_output())

    def flip_off_on_model(prompt: str, *, response_schema: dict, messages=None):
        res = backend.generate_structured(prompt, response_schema=response_schema, messages=messages)
        cfg.enable_self_learning = False
        return res

    runtime = SkillLearningRuntime(
        owner_id="local:main",
        deployment_root=tmp_path / "dep",
        backend=SimpleNamespace(generate_structured=flip_off_on_model),
        snapshot_provider=lambda: (),
        guard_config=None,
        config_provider=lambda: cfg,
    )
    service = SkillLearningService(store, SkillLearningSettings.from_config(cfg), runtime)
    assert service.enqueue_from_finalize(_finalize_ctx("run-abort")) == "queued"
    result = service.run_pending()

    assert result.status == "rejected"
    assert result.code == "SKILL_LEARNING_DISABLED"
    assert list(store.load_registry().skills) == []
    assert not (store.learned_root / "csv-merge-by-date").exists()


def test_merge_first_consolidates_or_defers_after_single_retry(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    initial_out = _output()
    create_dup = _output(name="csv-merge-copy")
    merged_update = _output(
        decision="update",
        update_target="csv-merge-by-date",
        name="csv-merge-by-date",
        reason="合并更新既有 CSV 技能，补充异常编码兜底检查。",
        body=BODY + "\n\n## 补充要点\n\n核查 BOM 头后再做日期列排序。",
    )
    backend = _FakeBackend(initial_out, create_dup, merged_update, create_dup, create_dup)
    service = SkillLearningService(
        store,
        SkillLearningSettings(min_tool_rounds=3, max_skills=50, merge_similarity_percent=70, autonomous_verified=True),
        SkillLearningRuntime(
            owner_id="local:main",
            deployment_root=tmp_path / "dep",
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(
                entries=tuple(
                    SimpleNamespace(
                        name=name,
                        source="owner",
                        description="CSV 合并技能",
                        path=store.skill_path(name),
                        stable_id=f"owner:{name}",
                    )
                    for name in store.load_registry().skills
                )
            ),
            guard_config=None,
        ),
    )

    assert service.enqueue_from_finalize(_finalize_ctx("r1")) == "queued"
    assert service.run_pending().status == "published"

    assert service.enqueue_from_finalize(_finalize_ctx("r2")) == "queued"
    res2 = service.run_pending()
    assert res2.status == "updated"
    assert res2.skill_name == "csv-merge-by-date"
    assert len(backend.prompts) == 3
    assert '"merge_hint"' in backend.prompts[2]
    assert list(store.load_registry().skills) == ["csv-merge-by-date"]

    assert service.enqueue_from_finalize(_finalize_ctx("r3")) == "queued"
    res3 = service.run_pending()
    assert res3.status == "rejected"
    assert res3.code == CODE_MERGE_DEFERRED
    assert len(backend.prompts) == 5
    assert list(store.load_registry().skills) == ["csv-merge-by-date"]


def test_275_learned_skills_avoid_200_truncation_and_prioritize_used_skill(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    snapshot: list[object] = []
    for idx in range(275):
        name = f"skill-{idx:03d}"
        publish_learned_skill(
            store,
            PublishRequest(
                SkillLearningDecision(
                    decision="create",
                    reason="沉淀复用步骤",
                    update_target="",
                    name=name,
                    description=f"专用流程 {name}",
                    when_to_use=f"触发条件 {name}",
                    tags=(f"tag-{idx}",),
                    body=f"{BODY}\n\n# skill:{name}",
                ),
                {"request_key": f"seed-{idx}", "run_id": f"seed-{idx}"},
                frozenset(),
                max_skills=300,
                guard_config=None,
            ),
        )
        snapshot.append(
            SimpleNamespace(
                name=name,
                source="owner",
                description=f"专用流程 {name}",
                path=store.skill_path(name),
                stable_id=f"owner:{name}",
            )
        )
    snapshot.append(SimpleNamespace(name="builtin-ref", source="builtin", description="内置技能", path="", stable_id="builtin:builtin-ref"))

    target = "skill-260"
    backend = _FakeBackend(
        _output(
            decision="update",
            update_target=target,
            name=target,
            description="更新后的 skill-260 描述",
            when_to_use="触发条件 skill-260",
            body=f"{BODY}\n\n# skill:{target}\n\n## 新增校对步骤\n\n增加行计数核查。",
        )
    )
    service = SkillLearningService(
        store,
        SkillLearningSettings(min_tool_rounds=3, max_skills=300, autonomous_verified=True),
        SkillLearningRuntime(
            owner_id="local:main",
            deployment_root=tmp_path / "dep",
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(entries=tuple(snapshot)),
            guard_config=None,
        ),
    )
    assert service.enqueue_from_finalize(_finalize_ctx("run-late", used_skill_ids=(target,))) == "queued"
    result = service.run_pending()

    assert result.status == "updated"
    assert result.skill_name == target
    prompt_text = backend.prompts[0]
    assert target in prompt_text
    assert "skill-274" in prompt_text


def test_channel_b_ineffective_requires_receipt_dedups_and_retires_at_three_strikes(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    name = "csv-merge-by-date"
    pub = publish_learned_skill(
        store,
        PublishRequest(
            SkillLearningDecision(
                decision="create",
                reason="沉淀复用步骤",
                update_target="",
                name=name,
                description="CSV 合并校验方法",
                when_to_use="需要合并多个 CSV 文件时",
                tags=("csv",),
                body=f"{BODY}\n\n# skill:{name}",
            ),
            {"request_key": "seed-pub", "run_id": "seed-pub"},
            frozenset(),
            max_skills=50,
            guard_config=None,
        ),
    )
    reg_rec = store.load_registry().skills[name]
    inc_id = str(reg_rec.maintenance["incarnation_id"])
    act_id = str(reg_rec.maintenance["activation_id"])

    backend = _FakeBackend(
        json.dumps(
            {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "decision": "ineffective",
                "reason": "未触发有效兜底步骤",
                "update_target": name,
                "name": name,
                "description": "",
                "when_to_use": "",
                "tags": [],
                "body": "",
            },
            ensure_ascii=False,
        )
    )
    service = SkillLearningService(
        store,
        SkillLearningSettings(min_tool_rounds=3, max_skills=50),
        SkillLearningRuntime(
            owner_id="local:main",
            deployment_root=tmp_path / "dep",
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(
                entries=(
                    SimpleNamespace(
                        name=name,
                        source="owner",
                        description="csv merge",
                        path=store.skill_path(name),
                        stable_id=f"owner:{name}",
                    ),
                )
            ),
            guard_config=None,
            usage_runtime_provider=lambda: repo,
        ),
    )

    # 1) 无送达回执且无 adopted 证据 -> fail-closed 拒绝记负向次；
    assert service.enqueue_from_finalize(_finalize_ctx("r-unattrib", root_task_id="task-missing", used_skill_ids=(name,))) == "queued"
    unattrib = service.run_pending()
    assert unattrib.status == "rejected"
    assert unattrib.code == CODE_INEFFECTIVE_UNATTRIBUTED

    # 2) 为 task-1/2/3 写入真实正文送达回执；同 task-1 两次重复仅记 1 strike，第 3 个独立任务触发回滚/退役；
    codes: list[str] = []
    for run_id, task_key in (("r1a", "task-1"), ("r1b", "task-1"), ("r2", "task-2"), ("r3", "task-3")):
        record_exact_use(
            store,
            {
                "event": "manual_get",
                "kind": "skill",
                "body_chars": 200,
                "incarnation_id": inc_id,
                "activation_id": act_id,
                "content_sha256": pub.sha256,
                "task_id": task_key,
            },
            runtime=repo,
        )
        backend.responses.append(
            json.dumps(
                {
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "decision": "ineffective",
                    "reason": f"{task_key} 使用无效",
                    "update_target": name,
                    "name": name,
                    "description": "",
                    "when_to_use": "",
                    "tags": [],
                    "body": "",
                },
                ensure_ascii=False,
            )
        )
        assert service.enqueue_from_finalize(_finalize_ctx(run_id, root_task_id=task_key, used_skill_ids=(name,))) == "queued"
        codes.append(service.run_pending().code)

    assert codes[0] == CODE_INEFFECTIVE_RECORDED
    assert codes[1] == CODE_INEFFECTIVE_RECORDED
    assert codes[2] == CODE_INEFFECTIVE_RECORDED
    assert codes[3] in {CODE_INEFFECTIVE_ROLLED_BACK, CODE_INEFFECTIVE_RETIRED}


def test_enqueue_lesson_candidates_filters_dedups_and_defers_without_s1_popups(tmp_path: Path) -> None:
    cfg = AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[], enable_self_learning=True)
    agent = SimpleAgent(cfg, tmp_path / "ws")

    proc = _skill_candidate(
        agent.memory_candidates,
        "run-proc-1",
        body=(
            "# 共享表更新规范\n\n## 触发条件\n写共享登记表时触发。\n\n"
            "## 步骤\n1. 先读取当前版本记录。\n2. 用精确版本比较交换落盘。\n"
        ),
    )
    decl = agent.memory_candidates.observe(
        CandidateObservation(
            candidate_type="user_preference",
            content="用户偏好使用 pytest 运行测试。",
            subject_key="pref-pytest",
            scope=MemoryScope(scope_type="personal", scope_key="personal"),
            origin="subagent_lesson",
            source_task_ids=("task-sub-1",),
            source_run_ids=("run-decl",),
            evidence_refs=({"kind": "tool_run", "ref_id": "run-decl"},),
        )
    )

    first = agent.subagents.enqueue_lesson_candidates([proc, decl])
    assert [row["status"] for row in first["queued"]] == ["queued"]
    assert len(first["refused"]) == 1
    assert first["deferred"] == []
    assert not (agent.home_paths.owner_home_dir / "data" / "skill_proposals").exists()
    assert len(agent.skill_learning.store.pending_requests()) == 1

    replay = agent.subagents.enqueue_lesson_candidates([proc])
    assert [row["status"] for row in replay["queued"]] == ["duplicate"]

    agent.config.enable_self_learning = False
    disabled = agent.subagents.enqueue_lesson_candidates([proc])
    assert len(disabled["deferred"]) == 1
    assert disabled["deferred"][0]["code"] == "SKILL_LEARNING_DISABLED"


def test_isolated_proposal_adoption_and_pruning_skip_corrupt_sibling(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    cands = CandidateService(home.owner_memory_candidates_jsonl)
    proposals = SkillProposalService(home)
    valid_cand = _skill_candidate(
        cands,
        "run-legacy-1",
        body=(
            "# 归档清单校验流程\n## 触发条件\n校验归档清单与表头编码时触发。\n"
            "## 步骤\n1. 读取各文件表头与版本。\n2. 核对行数校验和后落盘。"
        ),
        applies_when="需要校验归档清单并核对表头编码时触发。",
    )
    [valid] = seed_proposals_from_candidates(proposals, [valid_cand])
    committed = proposals.confirm(valid.proposal_id, valid.revision)
    assert committed.ok
    (proposals.directory / "broken-corrupt.json").write_text("{corrupt", encoding="utf-8")

    store = SkillLearningStore.for_home(home)
    _adopt_committed_lessons_isolated(store, proposals)
    assert len(store.load_registry().skills) == 1

    # 校验陈旧未确认提案清理在同目录存在坏 JSON 时仍独立清理过期 sibling；
    stale_cand = _skill_candidate(
        cands,
        "run-stale-1",
        body=(
            "# 临时目录核查流程\n## 触发条件\n清理旧临时归档文件时触发。\n"
            "## 步骤\n1. 检查未完成列表并校验权限。\n2. 清理过期项并确认空目录。"
        ),
        applies_when="需要清理旧临时归档文件时触发。",
    )
    [stale] = seed_proposals_from_candidates(proposals, [stale_cand])
    stale_path = proposals.directory / f"{stale.proposal_id}.json"
    assert cands.redact_content(content_hashes={stale_cand.content_hash}) >= 1
    (store.directory / "proposals").mkdir(parents=True, exist_ok=True)
    (store.directory / "proposals" / "broken-s3.json").write_text("{corrupt", encoding="utf-8")
    _prune_learning_proposals_isolated(store, lambda: proposals, batch_count=64)
    assert not stale_path.exists()
    assert (proposals.directory / "broken-corrupt.json").exists()


def test_multiuser_paged_discovery_and_missing_aux_model_run_maintenance_only(tmp_path: Path) -> None:
    home_root = tmp_path / "home"
    for uid in ("u1", "u2", "u3", "u4"):
        (home_root / "owners" / "providers" / "feishu" / "users" / uid / "data" / "skill_learning").mkdir(
            parents=True,
            exist_ok=True,
        )
    main_home = ensure_my_agent_home(home_root)

    cfg_path = tmp_path / "user_config.yaml"
    cfg_path.write_text("self_learning_owner_scan_page_limit: 2\n", encoding="utf-8")
    cfg = AgentConfig(
        my_agent_home=str(home_root),
        config_path=str(cfg_path),
        prompt_files=[],
        enable_self_learning=True,
        self_learning_maintenance_mode="autonomous",
        memory_curator_model_profile="",
    )

    # 先验证按 config/YAML 实时生效的 self_learning_owner_scan_page_limit=2 分页扫描全部远程用户与本地默认用户；
    assert _owner_scan_page_limit(cfg) == 2
    page1 = discover_owner_home_page(home_root / "owners", limit=_owner_scan_page_limit(cfg), after_cursor=None)
    page2 = discover_owner_home_page(home_root / "owners", limit=_owner_scan_page_limit(cfg), after_cursor=page1.next_cursor)

    assert len(page1.targets) == 2
    assert len(page2.targets) == 2
    assert page2.next_cursor is None
    discovered_ids = {f"{t.identity.provider}:{t.identity.owner_kind}:{t.identity.owner_id}" for t in (*page1.targets, *page2.targets)}
    assert {f"feishu:user:{uid}" for uid in ("u1", "u2", "u3", "u4")} <= discovered_ids
    cfg_path.write_text("self_learning_owner_scan_page_limit: 3\n", encoding="utf-8")
    assert _owner_scan_page_limit(cfg) == 3

    # 验证发现模块在某个远程 owner 下能发现 pending FIFO，且对空 owner 跳过；
    remote_store = SkillLearningStore(
        home_root / "owners" / "providers" / "feishu" / "users" / "u2" / "data" / "skill_learning",
        home_root / "owners" / "providers" / "feishu" / "users" / "u2" / "skills",
    )
    remote_svc = SkillLearningService(
        remote_store,
        SkillLearningSettings.from_config(cfg),
        SkillLearningRuntime(
            owner_id="feishu:user:u2",
            deployment_root=tmp_path / "dep",
            backend=None,
            snapshot_provider=lambda: SimpleNamespace(entries=()),
        ),
    )
    assert remote_svc.enqueue_from_finalize(_finalize_ctx("remote-u2")) == "queued"
    discovery = CoordinatorBackgroundRequests.__new__(CoordinatorBackgroundRequests)
    discovery.control = SimpleNamespace(home_root=home_root, config=cfg)
    u2_target = next(t for t in (*page1.targets, *page2.targets) if t.identity.provider == "feishu" and t.identity.owner_id == "u2")
    u2_refs = list(CoordinatorBackgroundRequests._owner_sources(discovery, u2_target))
    assert any(r["kind"] == "learning" and r["source_ref"].startswith("data/skill_learning/requests/") for r in u2_refs)
    local_target = SimpleNamespace(identity=OwnerIdentity.local_main(), home_dir=main_home.owner_home_dir)
    local_refs = list(CoordinatorBackgroundRequests._owner_sources(discovery, local_target))
    assert not any(r["kind"] == "learning" for r in local_refs)

    # 校验无辅助模型时：FIFO 不消费且保持 deferred/pending，仅执行确定性维护；
    agent = SimpleAgent(cfg, tmp_path / "ws")
    store = agent.skill_learning.store
    assert agent.skill_learning.enqueue_from_finalize(_finalize_ctx("run-fifo")) == "queued"
    publish_learned_skill(
        store,
        PublishRequest(
            SkillLearningDecision(
                decision="create",
                reason="沉淀维护技能",
                update_target="",
                name="maint-skill",
                description="CSV 维护样例方法",
                when_to_use="需要校验维护样例时",
                tags=("csv",),
                body=f"{BODY}\n\n# skill:maint-skill",
            ),
            {"request_key": "seed-maint", "run_id": "seed-maint"},
            frozenset(),
            max_skills=50,
            guard_config=None,
        ),
    )
    reg_before = store.load_registry()
    with gateway_short_transaction("test"), store.state_lock():
        store.save_registry(replace(reg_before, last_maintenance_at=1.0))
    os.utime(store.registry_path, (1, 1))

    maint_calls: list[bool] = []
    orig_run_pending = agent.skill_learning.run_pending

    def spy_run_pending(*, expected_request_path=None, maintenance_only=False):
        maint_calls.append(maintenance_only)
        return orig_run_pending(expected_request_path=expected_request_path, maintenance_only=maintenance_only)

    agent.skill_learning.run_pending = spy_run_pending
    assert _skill_learning_pending(agent) is True
    _run_skill_learning(agent, "test")
    assert maint_calls == [True]
    assert len(store.pending_requests()) == 1
    assert store.load_registry().last_maintenance_at > 1.0

    # 校验 curator 在无辅助模型场景下仅走零模型确定性维护；
    assert _curator_maintenance_due(agent) is True
    curator_calls: list[str] = []
    agent.memory_curator.run_maintenance_if_due = lambda: curator_calls.append("maint") or {"status": "skipped"}
    agent.memory_curator.run_if_due = lambda: curator_calls.append("llm")
    _run_memory_curator_maintenance(agent, "test")
    assert curator_calls == ["maint"]

    # execution 端口收到 FIFO ref 在缺席模型下必须推迟消费 FIFO，仅走 maintenance_only=True；
    [fifo_path] = store.pending_requests()
    ref = {
        "kind": "learning",
        "source_id": "learning-fifo",
        "source_ref": fifo_path.relative_to(main_home.owner_home_dir).as_posix(),
    }
    exec_res = _execute_learning(agent, ref)
    assert exec_res["status"] == "deferred" and exec_res["outcome"] == "pending"
    assert len(store.pending_requests()) == 1
    assert store.registry_path.is_file()


def test_similar_managed_skills_metadata_only_integer_boundary_and_empty_family(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)

    def _seed(
        name: str,
        *,
        description: str,
        when_to_use: str,
        tags: tuple[str, ...],
        body: str,
    ) -> None:
        publish_learned_skill(
            store,
            PublishRequest(
                SkillLearningDecision(
                    decision="create",
                    reason="沉淀相似判定样例",
                    update_target="",
                    name=name,
                    description=description,
                    when_to_use=when_to_use,
                    tags=tags,
                    body=body,
                ),
                {"request_key": f"seed-{name}", "run_id": f"seed-{name}"},
                frozenset(),
                max_skills=50,
                guard_config=None,
            ),
        )

    base_body = (
        "# 账务流水对账步骤\n"
        "## 触发条件\n收到日终结算账单时触发。\n"
        "## 步骤\n"
        "1. 拉取当天全部回执并按交易序号排序。\n"
        "2. 核对借贷余额合计数并导出差异报告。"
    )
    _seed(
        "alpha-tool",
        description="核对 CSV 日终流水账本差异的固定步骤",
        when_to_use="需要对账日终 CSV 流水并输出核查清单时",
        tags=("csv", "ledger"),
        body=base_body,
    )

    # 1) 相同元数据 + 截然不同长正文 -> 仍命中（正文不稀释元数据 Jaccard）；
    distinct_long_body = (
        "# 完全不同的文档渲染流程\n"
        "## 步骤\n"
        + "\n".join(f"{i}. 执行独立的排版校验子步骤 {i} 并输出摘要。" for i in range(1, 25))
    )
    same_meta_diff_body = SkillLearningDecision(
        decision="create",
        reason="新建",
        update_target="",
        name="alpha-tool-v2",
        description="核对 CSV 日终流水账本差异的固定步骤",
        when_to_use="需要对账日终 CSV 流水并输出核查清单时",
        tags=("csv", "ledger"),
        body=distinct_long_body,
    )
    hits_same_meta = _similar_managed_skills(store, same_meta_diff_body, threshold_percent=70)
    assert [m["name"] for m in hits_same_meta] == ["alpha-tool"]

    # 2) 不相交元数据 + 部分共享正文（非完全同指纹） -> 不得命中；
    disjoint_shared_body = SkillLearningDecision(
        decision="create",
        reason="新建",
        update_target="",
        name="zenith-pdf-ocr",
        description="extract tabular raster layout from scanned invoices",
        when_to_use="processing multi page image pdf archives",
        tags=("ocr", "pdf"),
        body=base_body + "\n3. 追加一处独立字段校对标记。",
    )
    assert _similar_managed_skills(store, disjoint_shared_body, threshold_percent=70) == []

    # 3) 整数交叉乘法精确边界：139/200 (69.5%) 在 70% 下不命中，140/200 (70%) 命中；
    pool = [f"q{i:02x}" for i in range(210)]
    shared_140 = pool[:140]
    cand_only_60 = pool[140:200]

    def _pack(tokens: list[str]) -> tuple[str, str, tuple[str, ...]]:
        desc = " ".join(tokens[:50])
        when = " ".join(tokens[50:125])
        rest = tokens[125:]
        tags = tuple(" ".join(rest[i:i + 15]) for i in range(0, len(rest), 15))
        return desc, when, tags

    r_desc, r_when, r_tags = _pack(shared_140)
    # 改用纯停用词 "and" / "the" 命名以便精确控制 union == 200；
    _seed(
        "and",
        description=r_desc,
        when_to_use=r_when,
        tags=r_tags,
        body=f"{BODY}\n\nSeed record boundary body variant alpha.",
    )
    # 设 candidate 拥有 shared_140[:139] + cand_only_60，union = 200, inter = 139 -> 13900 < 14000；
    c139 = shared_140[:139] + cand_only_60[:60]
    c_desc, c_when, c_tags = _pack(c139)
    cand_139 = SkillLearningDecision(
        decision="create",
        reason="边界测试",
        update_target="",
        name="the",
        description=c_desc,
        when_to_use=c_when,
        tags=c_tags,
        body=f"{BODY}\n\nCandidate boundary body variant omega.",
    )
    rec_140_set = _metadata_similarity_tokens("and", r_desc, r_when, r_tags, tokenize)
    cand_139_set = _metadata_similarity_tokens("the", c_desc, c_when, c_tags, tokenize)
    assert len(cand_139_set & rec_140_set) == 139
    assert len(cand_139_set | rec_140_set) == 200
    assert _similar_managed_skills(store, cand_139, threshold_percent=70) == []

    # 140/200 命中；
    c140 = shared_140 + cand_only_60[:60]
    c140_desc, c140_when, c140_tags = _pack(c140)
    cand_140 = replace(cand_139, description=c140_desc, when_to_use=c140_when, tags=c140_tags)
    hits_140 = _similar_managed_skills(store, cand_140, threshold_percent=70)
    assert [m["name"] for m in hits_140] == ["and"]
    assert hits_140[0]["similarity_percent"] == 70

    # 4) 空元数据返回空集（union 为空 => 0），且纯标题无词元正文不触发 family 短路；
    assert _metadata_similarity_tokens("", "   ", "---", (), tokenize) == set()
    _seed(
        "for",
        description="---",
        when_to_use="---",
        tags=(),
        body="# Headings Only Section One\n## Headings Only Section Two\n" + "#" * 80,
    )
    headings_only = SkillLearningDecision(
        decision="create",
        reason="空元数据与纯标题检查",
        update_target="",
        name="in",
        description="---",
        when_to_use="---",
        tags=(),
        body="# Headings Only Section One\n## Headings Only Section Two\n" + "#" * 80,
    )
    assert _similar_managed_skills(store, headings_only, threshold_percent=1) == []


def test_retry_merge_first_timeout_and_busy_follow_retry_contract(tmp_path: Path) -> None:
    profile = "w3-merge-retry-test-profile"
    clear_auxiliary_profile_cooldown(profile)
    try:
        home = ensure_my_agent_home(tmp_path / "home")
        store = SkillLearningStore.for_home(home)
        publish_learned_skill(
            store,
            PublishRequest(
                SkillLearningDecision(
                    decision="create",
                    reason="先置既有技能",
                    update_target="",
                    name="csv-merge-by-date",
                    description="合并多个日期 CSV 账单并校验列",
                    when_to_use="需要合并多个 CSV 文件时",
                    tags=("csv",),
                    body=f"{BODY}\n\n# seed-existing",
                ),
                {"request_key": "seed-1", "run_id": "seed-1"},
                frozenset(),
                max_skills=50,
                guard_config=None,
            ),
        )
        cfg = AgentConfig(
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            enable_self_learning=True,
            memory_curator_model_profile=profile,
        )
        first_dup = json.loads(_output(name="csv-merge-copy"))
        calls = {"n": 0}

        class _RetryFailingBackend:
            name = "test-retry-backend"

            def generate_structured(self, prompt: str, *, response_schema: dict, messages=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return SimpleNamespace(text=json.dumps(first_dup, ensure_ascii=False))
                if calls["n"] == 2:
                    raise TimeoutError("merge retry timed out")
                if calls["n"] == 3:
                    return SimpleNamespace(text=json.dumps(first_dup, ensure_ascii=False))
                raise CuratorModelStillRunningError("in_flight")

        service = SkillLearningService(
            store,
            SkillLearningSettings(min_tool_rounds=3, max_skills=50, merge_similarity_percent=70),
            SkillLearningRuntime(
                owner_id="local:main",
                deployment_root=tmp_path / "dep",
                backend=_RetryFailingBackend(),
                snapshot_provider=lambda: SimpleNamespace(
                    entries=(
                        SimpleNamespace(
                            name="csv-merge-by-date",
                            source="owner",
                            description="合并多个日期 CSV 账单并校验列",
                            path=store.skill_path("csv-merge-by-date"),
                            stable_id="owner:csv-merge-by-date",
                        ),
                    )
                ),
                guard_config=None,
                config_provider=lambda: cfg,
            ),
        )
        assert service.enqueue_from_finalize(_finalize_ctx("r-timeout")) == "queued"

        # 合并重试第 2 次调用超时 -> 必须递增 attempts 并进入冷却，不得 attempts=0 卡死 FIFO；
        timeout_res = service.run_pending()
        assert timeout_res.status == STATUS_RETRY
        assert timeout_res.code == CODE_MODEL_TIMEOUT
        [pending_path] = store.pending_requests()
        persisted = store.read_request(pending_path)
        assert persisted is not None and persisted.get("attempts") == 1
        assert auxiliary_profile_cooldown_remaining(profile) > 0

        # 清零冷却后，第 2 次调用返回 CuratorModelStillRunningError -> 返回 STATUS_BUSY 且不丢请求；
        clear_auxiliary_profile_cooldown(profile)
        busy_res = service.run_pending()
        assert busy_res.status == STATUS_BUSY
        assert len(store.pending_requests()) == 1
    finally:
        clear_auxiliary_profile_cooldown(profile)


def test_retry_merge_first_respects_daily_limit_budget(tmp_path: Path) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    publish_learned_skill(
        store,
        PublishRequest(
            SkillLearningDecision(
                decision="create",
                reason="先置既有技能",
                update_target="",
                name="csv-merge-by-date",
                description="合并多个日期 CSV 账单并校验列",
                when_to_use="需要合并多个 CSV 文件时",
                tags=("csv",),
                body=f"{BODY}\n\n# seed-budget",
            ),
            {"request_key": "seed-budget", "run_id": "seed-budget"},
            frozenset(),
            max_skills=50,
            guard_config=None,
        ),
    )
    backend = _FakeBackend(_output(name="csv-merge-copy"), _output(name="should-not-call"))
    service = SkillLearningService(
        store,
        SkillLearningSettings(min_tool_rounds=3, max_skills=50, daily_limit=1, merge_similarity_percent=70),
        SkillLearningRuntime(
            owner_id="local:main",
            deployment_root=tmp_path / "dep",
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(
                entries=(
                    SimpleNamespace(
                        name="csv-merge-by-date",
                        source="owner",
                        description="合并多个日期 CSV 账单并校验列",
                        path=store.skill_path("csv-merge-by-date"),
                        stable_id="owner:csv-merge-by-date",
                    ),
                )
            ),
            guard_config=None,
        ),
    )
    assert service.enqueue_from_finalize(_finalize_ctx("r-budget")) == "queued"
    result = service.run_pending()

    assert result.status == "rejected"
    assert result.code == CODE_MERGE_DEFERRED
    assert len(backend.prompts) == 1
    assert list(store.load_registry().skills) == ["csv-merge-by-date"]
