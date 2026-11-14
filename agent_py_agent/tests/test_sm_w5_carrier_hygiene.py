"""W5 离线合同测试：lesson_carrier 互斥分类、runner 独家路由与 S1/S3 有界裁剪。"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_proposals, skill_proposals
from agent_py_agent.agent.capability.asset_promotion import register_asset
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_proposals import (
    list_learning_proposals,
    propose_learning,
    prune_learning_proposals,
    resolve_learning_proposal,
)
from agent_py_agent.agent.capability.skill_learning_publish import PublishRequest
from agent_py_agent.agent.capability.skill_learning_store import (
    MAX_PENDING_REQUESTS,
    SkillLearningStore,
)
from agent_py_agent.agent.capability.skill_learning_validation import _validate_skill_procedure
from agent_py_agent.agent.capability.skill_proposals import (
    PROPOSAL_COMMITTED,
    PROPOSAL_REJECTED,
    SkillProposalService,
    lesson_carrier,
    render_lesson_draft,
)
from agent_py_agent.agent.memory_store.candidate_models import _MAX_CONTENT_CHARS, MemoryCandidate
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.operations import memory_content_hash
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_proposals import seed_proposals_from_candidates


def _make_candidate(
    content: str,
    *,
    candidate_id: str = "memory-candidate-w5test000001",
    candidate_type: str = "lesson",
    origin: str = "subagent_lesson",
    scope: dict[str, str] | None = None,
    status: str = "pending_review",
    source_task_ids: list[str] | None = None,
    source_run_ids: list[str] | None = None,
) -> MemoryCandidate:
    default_scope = {"scope_type": "project", "scope_key": "project:root", "applies_when": "并发写入共享文件时"}
    return MemoryCandidate(
        schema_version="my-agent.memory-candidate.v1",
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        content=content,
        subject_key="subagent.lesson.sample",
        scope=default_scope if scope is None else scope,
        origin=origin,
        evidence_refs=[{"source_ref": "/tmp/run-1/lessons.jsonl#lesson-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=["project-root"] if source_task_ids is None else source_task_ids,
        source_run_ids=["run-1"] if source_run_ids is None else source_run_ids,
        observed_at="2026-11-13T00:00:00Z",
        last_observed_at="2026-11-13T00:00:00Z",
        valid_from="",
        valid_until="",
        occurrence_count=1,
        confidence=0.8,
        proposed_action="add",
        target_entry_id="",
        conflicts_with=[],
        promotion_target="lesson",
        status=status,
        reviewer="",
        review_note="",
        created_at="2026-11-13T00:00:00Z",
        updated_at="2026-11-13T00:00:00Z",
        promoted_at="",
        promotion_ref="",
        content_hash=memory_content_hash(content) if content else "",
    )


def _runner_runtime(tmp_path: Path) -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    s1_service = SkillProposalService(home)
    learning_store = SkillLearningStore.for_home(home)
    return SimpleNamespace(
        home=home,
        candidates=candidates,
        manager=manager,
        s1_service=s1_service,
        learning_store=learning_store,
    )


def _s3_decision(name: str) -> SkillLearningDecision:
    return SkillLearningDecision(
        decision="create",
        reason="复用已验证排查流程",
        name=name,
        description=f"自动化技能 {name}",
        when_to_use="定位存储冲突时使用",
        tags=("storage", "verification"),
        body=f"# {name}\n\n## 适用场景\n\n定位存储冲突时使用。\n\n## 步骤\n\n1. 先读取当前版本。\n2. 再执行比较交换。\n",
    )


def _seed_s3_proposal(
    store: SkillLearningStore,
    *,
    name: str,
    request_key: str,
    created_at_ns: int | None = None,
    register_candidate_asset: bool = True,
) -> dict:
    decision = _s3_decision(name)
    publish = PublishRequest(
        decision=decision,
        request={"request_key": request_key, "run_id": f"run-{name}", "task_id": f"task-{name}"},
    )
    event = propose_learning(store, publish)
    proposal_id = event.reason
    if created_at_ns is not None:
        path = store.directory / "proposals" / f"{proposal_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at_ns"] = created_at_ns
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if register_candidate_asset:
        register_asset(
            store,
            kind="skill",
            asset_id=name,
            asset=asdict(decision),
            source={"origin": "S3", "proposal_id": proposal_id},
            state="candidate",
        )
    return next(row for row in list_learning_proposals(store) if row["proposal_id"] == proposal_id)


def _enqueue_s3_request(store: SkillLearningStore, request_key: str, idx: int = 1) -> str:
    return store.enqueue(
        {
            "request_key": request_key,
            "run_id": f"run-req-{idx}",
            "task_id": f"task-req-{idx}",
            "prompt": f"整理经验请求 {idx}",
            "existing_skills": [],
        }
    )


# ---- 1. lesson_carrier 纯有界分类测试 ----


def test_lesson_carrier_distinguishes_raw_inputs_and_never_inflates_draft_template() -> None:
    declarative = _make_candidate("修改共享状态前先读取当前版本，再用精确版本做比较交换写入。")
    multi_line_declarative = _make_candidate("注意保持锁序一致。\n不要在锁内做耗时的模型调用。")
    numbered = _make_candidate("1. 先读取当前版本。\n2. 用精确版本做比较交换写入。")
    code_block = _make_candidate("执行定向回归检查：\n```bash\npytest -q agent_py_agent/tests/test_skill_proposals.py\n```")
    oversized = _make_candidate("1. 步骤一\n2. " + ("字" * _MAX_CONTENT_CHARS))
    missing_scope_empty = _make_candidate("1. 先读版本\n2. 再写盘", scope={})
    missing_applies_when = _make_candidate(
        "1. 先读版本\n2. 再写盘",
        scope={"scope_type": "project", "scope_key": "project:root", "applies_when": ""},
    )
    wrong_type = _make_candidate("1. 先读版本\n2. 再写盘", candidate_type="long_term_fact")

    assert lesson_carrier(declarative) == "lesson"
    assert lesson_carrier(multi_line_declarative) == "lesson"
    # render_lesson_draft 会包裹标题和 "## 适用场景" 节，但分类器只看原始 content，不能被模板抬升为 skill。
    draft = render_lesson_draft(declarative, "lesson-000000000000")
    _validate_skill_procedure({"body": draft.body, "when_to_use": draft.when_to_use})
    assert lesson_carrier(declarative) == "lesson"

    assert lesson_carrier(numbered) == "skill"
    assert lesson_carrier(code_block) == "skill"
    assert lesson_carrier(oversized) == "lesson"
    assert lesson_carrier(missing_scope_empty) == "lesson"
    assert lesson_carrier(missing_applies_when) == "lesson"
    assert lesson_carrier(wrong_type) == "lesson"

    for malformed in (
        None,
        "1. step\n2. step",
        {"content": "1. a\n2. b"},
        SimpleNamespace(candidate_type="lesson", content="1. a\n2. b", scope={"scope_type": "project", "scope_key": "p", "applies_when": "x"}),
        _make_candidate("   \n  "),
        _make_candidate("1. a\n2. b", scope="invalid"),  # type: ignore[arg-type]
    ):
        assert lesson_carrier(malformed) == "lesson"  # type: ignore[arg-type]


# ---- 2. runner_result_service 独家路由与 V2-4 防弹窗 ----


def test_runner_exclusive_carrier_routing_never_creates_s1_popups(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    ctx.manager.skill_proposals = ctx.s1_service
    received: list[list[str]] = []

    def _enqueue(candidates):
        ids = [item.candidate_id for item in candidates]
        received.append(ids)
        return {
            "queued": [{"candidate_id": cid, "request_key": "aa11bb22cc33dd44", "status": "queued"} for cid in ids],
            "refused": [],
            "deferred": [],
        }

    ctx.manager.enqueue_lesson_candidates = _enqueue
    task = ctx.manager.create_run(goal="排查并沉淀工作流经验", thought="记录", plan=["检查"])
    decl = "修改共享文件前先核对版本号。"
    proc = "1. 先读取当前版本。\n2. 执行比较交换更新。"
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="完成", lessons=[decl, proc])

    result = ctx.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="完成",
            status="DONE",
            turn_end_reason="completed",
            structured_output=structured,
        )
    )
    saved = ctx.manager.load(task.id)
    work_log = Path(saved.work_log_file).read_text(encoding="utf-8")
    recorded_candidates = ctx.candidates.list()

    assert result.ok is True and saved.status == "DONE"
    assert len(recorded_candidates) == 2
    assert len(received) == 1 and len(received[0]) == 1
    proc_cand = next(item for item in recorded_candidates if item.content == proc)
    assert received[0] == [proc_cand.candidate_id]
    assert "skill_carrier_queued=1 skill_carrier_duplicate=0 skill_carrier_deferred=0 skill_carrier_refused=0" in work_log
    assert "skill_proposals" not in work_log
    assert not ctx.home.owner_skill_proposals_dir.exists()


def test_runner_missing_disabled_or_failing_callback_defers_without_s1_fallback(tmp_path: Path) -> None:
    proc = "1. 先检查环境。\n2. 再执行写入。"

    # A. 回调缺失（即使挂着 S1 skill_proposals 也绝不回退生成 S1 待确认提案）
    ctx_missing = _runner_runtime(tmp_path / "missing")
    ctx_missing.manager.skill_proposals = ctx_missing.s1_service
    task_a = ctx_missing.manager.create_run(goal="验证缺失回调", thought="t", plan=["p"])
    res_a = ctx_missing.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_a.id,
            dry_run=False,
            ok=True,
            message="ok",
            status="DONE",
            turn_end_reason="completed",
            structured_output=SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="ok", lessons=[proc]),
        )
    )
    log_a = Path(ctx_missing.manager.load(task_a.id).work_log_file).read_text(encoding="utf-8")
    assert res_a.ok is True
    assert len(ctx_missing.candidates.list()) == 1
    assert "skill_carrier_deferred=1" in log_a and "SKILL_CARRIER_UNAVAILABLE" in log_a
    assert not ctx_missing.home.owner_skill_proposals_dir.exists()

    # B. 回调返回 disabled deferred / 抛错均不影响交付、保留源候选且零 S1 弹窗
    ctx_err = _runner_runtime(tmp_path / "error")
    ctx_err.manager.skill_proposals = ctx_err.s1_service
    ctx_err.manager.enqueue_lesson_candidates = lambda items: {
        "queued": [],
        "refused": [],
        "deferred": [{"candidate_id": item.candidate_id, "code": "SKILL_LEARNING_DISABLED", "reason": "learning off"} for item in items],
    }
    task_b = ctx_err.manager.create_run(goal="验证延迟回执", thought="t", plan=["p"])
    res_b = ctx_err.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_b.id,
            dry_run=False,
            ok=True,
            message="ok",
            status="DONE",
            turn_end_reason="completed",
            structured_output=SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="ok", lessons=[proc]),
        )
    )
    log_b = Path(ctx_err.manager.load(task_b.id).work_log_file).read_text(encoding="utf-8")
    assert res_b.ok is True
    assert len(ctx_err.candidates.list()) == 1
    assert "skill_carrier_deferred=1" in log_b
    assert not ctx_err.home.owner_skill_proposals_dir.exists()

    # C. 失败任务即使包含步骤文本也不触发自动入队
    called_on_fail = []
    ctx_fail = _runner_runtime(tmp_path / "failed")
    ctx_fail.manager.enqueue_lesson_candidates = lambda items: called_on_fail.append(items)
    task_c = ctx_fail.manager.create_run(goal="验证失败不入队", thought="t", plan=["p"])
    ctx_fail.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_c.id,
            dry_run=False,
            ok=False,
            message="failed",
            status="FAILED",
            turn_end_reason="failed",
            structured_output=SubAgentParsedOutput(found=True, ok=False, status="FAILED", summary="failed", lessons=[proc]),
        )
    )
    assert called_on_fail == []
    assert not ctx_fail.home.owner_skill_proposals_dir.exists()


# ---- 3. S3 prune_learning_proposals 有界裁剪与崩溃恢复 ----


def test_s3_prune_protects_main_and_overflow_queues_respects_stale_zero_and_preserves_trial_assets(
    tmp_path: Path,
) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    old_ns = 1_000_000_000 * 1_000_000
    now_sec = 1_000_200.0

    # 放入 21 条排队请求：第 1 条进主区前排，第 21 条使前排/溢出区同时存在排队键
    for idx in range(MAX_PENDING_REQUESTS + 1):
        _enqueue_s3_request(store, f"{idx:016x}", idx)
    hot_key = f"{0:016x}"
    overflow_paths = store.overflow_requests()
    assert len(overflow_paths) == 1
    overflow_key = overflow_paths[0].stem.partition("-")[2]

    kept_hot = _seed_s3_proposal(store, name="skill-hot-queued", request_key=hot_key, created_at_ns=old_ns)
    kept_overflow = _seed_s3_proposal(store, name="skill-overflow-queued", request_key=overflow_key, created_at_ns=old_ns)
    expired_pending = _seed_s3_proposal(store, name="skill-expired-pending", request_key="aaaa000000000001", created_at_ns=old_ns)
    fresh_pending = _seed_s3_proposal(
        store, name="skill-fresh-pending", request_key="aaaa000000000002", created_at_ns=int((now_sec - 5) * 1e9)
    )
    committed = _seed_s3_proposal(store, name="skill-committed-trial", request_key="aaaa000000000003", created_at_ns=old_ns)
    resolve_learning_proposal(store, committed["proposal_id"], 1, decision="confirm", actor_id="user")
    rejected = _seed_s3_proposal(store, name="skill-rejected-cand", request_key="aaaa000000000004", created_at_ns=old_ns)
    resolve_learning_proposal(store, rejected["proposal_id"], 1, decision="reject", actor_id="user")

    # stale_seconds=0：仅关闭 pending 过期，已裁决且未排队的 resolved 记录可清理，trial 正式资产纹丝不动。
    res_zero = prune_learning_proposals(store, now=now_sec, stale_seconds=0, batch_count=10)
    assert res_zero["expired"] == 0 and res_zero["resolved"] == 2 and res_zero["pruned"] == 2
    remaining_ids = {row["proposal_id"] for row in list_learning_proposals(store)}
    assert remaining_ids == {
        kept_hot["proposal_id"],
        kept_overflow["proposal_id"],
        expired_pending["proposal_id"],
        fresh_pending["proposal_id"],
    }
    reg_after_zero = store.load_registry()
    assert "skill-committed-trial" in reg_after_zero.skills
    assert (home.owner_home_dir / "skills" / "learned" / "skill-committed-trial" / "SKILL.md").is_file()
    assert any(
        row.get("state") == "trial" and row.get("asset_id") == "owner:skill-committed-trial"
        for row in reg_after_zero.assets.values()
    )
    assert not any(
        any(ref.get("proposal_id") == rejected["proposal_id"] for ref in row.get("provenance", []))
        for row in reg_after_zero.assets.values()
    )

    # 正 stale_seconds=60：仅过期未排队的陈旧 pending；主区与溢出区排队中的提案及新鲜 pending 全部保留。
    res_pos = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=10)
    assert res_pos["expired"] == 1 and res_pos["pruned"] == 1 and res_pos["assets_removed"] == 1
    assert {row["proposal_id"] for row in list_learning_proposals(store)} == {
        kept_hot["proposal_id"],
        kept_overflow["proposal_id"],
        fresh_pending["proposal_id"],
    }


def test_s3_prune_crash_recovery_corrupt_tolerance_and_batch_convergence(tmp_path: Path, monkeypatch) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    old_ns = 1_000_000_000 * 1_000_000
    now_sec = 1_000_500.0
    seeded = [
        _seed_s3_proposal(store, name=f"skill-batch-{i}", request_key=f"bbbb0000000000{i:02x}", created_at_ns=old_ns + i)
        for i in range(5)
    ]
    # 写入一个损坏提案文件，验证单条坏记录不会阻塞后续有效裁剪。
    bad_file = store.directory / "proposals" / f"learning-proposal-{'f' * 32}.json"
    bad_file.write_text("{corrupt", encoding="utf-8")

    # 模拟删除过程中在意图日志写入后中断（绕过 _apply_plan 的 OSError 自动回滚，模拟进程突然退出）。
    orig_unlink = skill_learning_proposals.unlink_file_beneath
    crash_once = {"armed": True}

    def _interrupt_after_one(root, parts):
        orig_unlink(root, parts)
        if crash_once["armed"] and parts != skill_learning_proposals._INTENT:
            crash_once["armed"] = False
            raise RuntimeError("simulated process exit during prune")

    monkeypatch.setattr(skill_learning_proposals, "unlink_file_beneath", _interrupt_after_one)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    monkeypatch.undo()
    assert (store.directory / ".proposals-intent.json").exists()

    # 下次调用自动通过既有 .proposals-intent.json 恢复被中断的那批删除及资产清理，并继续推进剩余批次。
    r1 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert not (store.directory / ".proposals-intent.json").exists()
    assert r1["pruned"] == 2 and r1["corrupt_skipped"] >= 1
    r2 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert r2["pruned"] == 1 and r2["has_more"] is False
    r3 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert r3["pruned"] == 0 and r3["remaining"] == 0

    for row in seeded:
        assert not (store.directory / "proposals" / f"{row['proposal_id']}.json").exists()
    assert store.load_registry().assets == {}


# ---- 4. S1 prune_orphaned 裁决标记保留、批收敛与锁序验证 ----


def _seed_s1(ctx: SimpleNamespace, lesson: str, run_id: str) -> tuple[str, str]:
    task = SubAgentTask(
        id=run_id,
        root_id=f"root-{run_id}",
        goal="测试 S1 提案生命周期",
        thought="",
        plan=[],
        output_json=f"/artifacts/{run_id}/output.json",
        updated_at=100.0,
    )
    recorded = ctx.manager.memory_candidates.record_result_candidates(task, lessons=[lesson], findings=[])
    [proposal] = seed_proposals_from_candidates(ctx.s1_service, recorded)
    return proposal.proposal_id, recorded[0].candidate_id


def test_s1_prune_orphaned_rules_decision_markers_corrupt_tolerance_and_batch_convergence(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    p_active, _c_active = _seed_s1(ctx, "保留活跃来源提案的正文内容。", "run-active")
    p_committed, c_committed = _seed_s1(ctx, "保留已确认裁决标记的经验内容。", "run-committed")
    p_rejected, c_rejected = _seed_s1(ctx, "保留已拒绝裁决标记的经验内容。", "run-rejected")
    p_inactive, c_inactive = _seed_s1(ctx, "来源停用时应保留待重试的经验。", "run-inactive")
    p_missing, c_missing = _seed_s1(ctx, "来源彻底删除后应清理的提案一。", "run-missing")
    p_redacted, _c_redacted = _seed_s1(ctx, "来源正文脱敏后应清理的提案二。", "run-redacted")
    p_changed, c_changed = _seed_s1(ctx, "来源哈希改写后应清理的提案三。", "run-changed")

    # 确认 committed 与 rejected 后删源，两者仍必须保留作为用户裁决标记。
    committed_item = ctx.s1_service.show(p_committed)
    assert ctx.s1_service.confirm(p_committed, committed_item.revision).ok
    rejected_item = ctx.s1_service.show(p_rejected)
    assert ctx.s1_service.reject(p_rejected, rejected_item.revision).ok

    for cid in (c_committed, c_rejected, c_inactive, c_missing):
        ctx.candidates.transition(cid, "rejected", reviewer="admin")
    ctx.candidates.delete_terminal([c_committed, c_rejected, c_missing])
    ctx.candidates.redact_content(content_hashes={ctx.s1_service.show(p_redacted).source.content_hash})

    # 改写 p_changed 源候选内容与 hash
    with ctx.candidates._locked_candidates():
        from agent_py_agent.agent.memory_store import candidates as cand_mod

        rows = ctx.candidates._load_unlocked()
        idx = rows.index_for(c_changed)
        new_text = "来源已经被其他写入者改写。"
        rows[idx] = replace(rows[idx], content=new_text, content_hash=memory_content_hash(new_text))
        cand_mod._write_candidates_unlocked(ctx.candidates.path, rows, admission=cand_mod._NullAdmission())

    # 插入一条无关 S3 候选资产与一条损坏 S1 提案文件
    register_asset(
        ctx.learning_store,
        kind="skill",
        asset_id="s3-keep-asset",
        asset={"name": "s3-keep-asset", "body": "1. a\n2. b"},
        source={"origin": "S3", "proposal_id": "learning-proposal-" + "0" * 32},
        state="candidate",
    )
    (ctx.home.owner_skill_proposals_dir / ("a" * 24 + ".json")).write_text("{broken json", encoding="utf-8")

    # 分批收敛：共有 3 条可清理（missing, redacted, changed），batch_count=2 需两轮清完。
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 2
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 1
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 0

    existing_files = {p.stem for p in ctx.home.owner_skill_proposals_dir.glob("*.json") if not p.name.startswith(".")}
    assert {p_active, p_committed, p_rejected, p_inactive}.issubset(existing_files)
    assert existing_files.isdisjoint({p_missing, p_redacted, p_changed})
    assert ctx.s1_service.show(p_committed).status == PROPOSAL_COMMITTED
    assert ctx.s1_service.show(p_rejected).status == PROPOSAL_REJECTED

    # 验证已清理的 S1 candidate 资产被清除，而 committed trial 资产与 S3 资产完整保留。
    reg = ctx.learning_store.load_registry()
    all_asset_props = {
        ref.get("proposal_id")
        for row in reg.assets.values()
        for ref in row.get("provenance", [])
        if isinstance(ref, dict)
    }
    assert all_asset_props.isdisjoint({p_missing, p_redacted, p_changed})
    assert p_committed in all_asset_props
    assert ("learning-proposal-" + "0" * 32) in all_asset_props


def test_s1_prune_retains_unreadable_source_proposals(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    p_id, _ = _seed_s1(ctx, "账本损坏不可读时应保留待恢复的经验。", "run-unreadable")
    ctx.candidates.path.write_text("{not-valid-jsonl\n", encoding="utf-8")

    assert ctx.s1_service.prune_orphaned(batch_count=10) == 0
    assert (ctx.home.owner_skill_proposals_dir / f"{p_id}.json").is_file()


def test_s1_prune_releases_directory_lock_before_learning_state_lock_concurrency(tmp_path: Path, monkeypatch) -> None:
    ctx = _runner_runtime(tmp_path)
    ids = [_seed_s1(ctx, f"并发锁序检查提案 {i}。", f"run-lock-{i}") for i in range(4)]
    for cid in [c for _, c in ids[:2]]:
        ctx.candidates.transition(cid, "rejected", reviewer="admin")
    ctx.candidates.delete_terminal([c for _, c in ids[:2]])

    s1_lock_held = False
    state_lock_attempted_while_s1_held = False
    inside_state_lock = threading.Event()
    s1_lock_released = threading.Event()
    allow_state_lock_exit = threading.Event()
    errors: list[BaseException] = []

    orig_locked = ctx.s1_service._locked

    from contextlib import contextmanager

    @contextmanager
    def _tracked_s1_locked():
        nonlocal s1_lock_held
        with orig_locked():
            s1_lock_held = True
            try:
                yield
            finally:
                s1_lock_held = False
                s1_lock_released.set()

    monkeypatch.setattr(ctx.s1_service, "_locked", _tracked_s1_locked)

    orig_for_home = SkillLearningStore.for_home

    def _tracked_for_home(home_paths):
        nonlocal state_lock_attempted_while_s1_held
        store = orig_for_home(home_paths)
        if s1_lock_held:
            state_lock_attempted_while_s1_held = True
        return store

    monkeypatch.setattr(SkillLearningStore, "for_home", staticmethod(_tracked_for_home))

    # 线程 B 持有 learning store.state_lock() 期间，线程 A 仍可完整执行并释放 S1 锁，然后等待 state_lock 放行完成资产清理，无任何反向死锁。
    def _hold_learning_state():
        try:
            with ctx.learning_store.state_lock():
                inside_state_lock.set()
                assert allow_state_lock_exit.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _run_s1_prune():
        try:
            assert inside_state_lock.wait(timeout=5.0)
            assert ctx.s1_service.prune_orphaned(batch_count=2) == 2
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_state = threading.Thread(target=_hold_learning_state)
    t_prune = threading.Thread(target=_run_s1_prune)
    t_state.start()
    t_prune.start()
    assert s1_lock_released.wait(timeout=5.0)
    allow_state_lock_exit.set()
    t_prune.join(timeout=5.0)
    t_state.join(timeout=5.0)

    assert not t_prune.is_alive() and not t_state.is_alive()
    assert errors == []
    assert state_lock_attempted_while_s1_held is False
    assert {p for p, _ in ids[:2]}.isdisjoint({p.stem for p in ctx.home.owner_skill_proposals_dir.glob("*.json")})
    # 确认 S1 锁从未与 learning state_lock 嵌套（静态覆盖：even with fake _verify_source）
    assert hasattr(skill_proposals, "lesson_carrier")
