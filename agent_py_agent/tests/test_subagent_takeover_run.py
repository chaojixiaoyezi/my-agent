from __future__ import annotations

import json
import time
from pathlib import Path

from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.services.takeover_run import TakeoverRunRequest


# LLM: test_takeover_run_records_source_and_reuses_task_refs covers dead-run takeover.
# 函数用途: 原 run 挂死后，新 takeover run 要带着同一任务目录和 artifacts refs 接着做，并记录旧 run 被接管。
def test_takeover_run_records_source_and_reuses_task_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(
        goal="实现 checkout 支付流程",
        thought="worker 中途挂了。",
        plan=["写页面", "跑测试"],
        role="worker",
        allowed_tools=["read_file", "write_file"],
    )
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    source.artifact_refs = [source.agent_run_artifacts_dir]
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="runner_timeout"))

    takeover = manager.load(result.takeover_run_id)
    reloaded_source = manager.load(source.id)
    source_refs = takeover.attributes["takeover_source_refs"]
    assert result.created is True
    assert reloaded_source.status == "TAKEN_OVER"
    assert reloaded_source.takeover_by == takeover.id
    assert takeover.parent_id == source.parent_id
    assert takeover.root_id == source.root_id
    assert source.task_dir in takeover.allowed_write_roots
    assert source_refs["task_dir"] == source.task_dir
    assert source_refs["agent_run_artifacts_dir"] == source.agent_run_artifacts_dir
    assert source_refs["latest_continue_packet"].endswith("latest_continue_packet.json")
    assert takeover.attributes["takeover_chain_depth"] == 1
    assert takeover.attributes["takeover_lineage_root_run_id"] == source.id


# LLM: takeover must inherit the parent-approved product write root, not force another capability request.
# 函数用途: 原 worker 已经被授权写业务产物目录时，新 takeover run 需要直接继承这个目录。
def test_takeover_run_inherits_source_product_write_roots(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    product_root = tmp_path / "deliverables" / "build"
    source = manager.create_run(
        goal="写 takeover demo",
        thought="worker 会被故意 timeout。",
        plan=["写产物"],
        role="worker",
        allowed_tools=["write_file"],
        extra_write_roots=[str(product_root)],
    )
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="runner_timeout"))
    takeover = manager.load(result.takeover_run_id)

    assert not product_root.exists()
    assert str(product_root) in takeover.allowed_write_roots


# LLM: test_takeover_run_is_idempotent_when_source_already_taken_over prevents runaway expansion.
# 函数用途: 同一个挂死 run 已经有 takeover 时，再次恢复不能继续创建无限多个接管 run。
def test_takeover_run_is_idempotent_when_source_already_taken_over(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)

    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))
    second = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))

    run_ids = [task.id for task in manager.list_runs()]
    assert second.created is False
    assert second.takeover_run_id == first.takeover_run_id
    assert run_ids.count(first.takeover_run_id) == 1


# LLM: takeover source-ref lookup catches stale source snapshots that lost takeover_by.
# 函数用途: 即便旧 run 的 takeover_by 被旧快照污染为空，也能通过现有 takeover run 的 attributes 复用接管者。
def test_takeover_run_reuses_existing_source_ref_when_source_snapshot_is_stale(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    existing = manager.create_run(goal="接管旧 run", thought="继续", plan=["继续"], role="worker")
    existing.attributes["takeover_source_run_id"] = source.id
    manager.save(existing)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))

    assert result.created is False
    assert result.takeover_run_id == existing.id
    assert [task.id for task in manager.list_runs()].count(existing.id) == 1


# LLM: persistence merge protects recorded takeover from later stale full-object saves.
# 函数用途: 超时旧 runner 或旧父级快照保存 TIMEOUT 时，不能覆盖已经落盘的 TAKEN_OVER/takeover_by。
def test_takeover_state_survives_stale_task_save(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))

    stale = manager.load(source.id)
    stale.status = "TIMEOUT"
    stale.takeover_by = ""
    stale.takeover_records = []
    manager.save(stale)
    reloaded = manager.load(source.id)

    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by == first.takeover_run_id
    assert len(reloaded.takeover_records) == 1


# LLM: stale finalized runner results must not rewrite a source run after takeover.
# 函数用途: 旧 attempt 超时后继续返回结果时，只能得到 ignored quick result，不能把 TAKEN_OVER 写回 TIMEOUT。
def test_runner_result_is_ignored_after_takeover(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))

    result = manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=source.id,
            dry_run=False,
            ok=False,
            message="late timeout",
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type="runner_timeout",
        )
    )
    reloaded = manager.load(source.id)

    assert "already taken-over" in result.message
    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by == first.takeover_run_id


# LLM: takeover chain fuse prevents repeated timeouts from growing an infinite replacement tree.
# 函数用途: 验证接管 run 自己也超时后，超过链深度上限时会阻塞上报，不再继续创建新 run。
def test_takeover_run_chain_exhaustion_blocks_without_new_child(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    manager.takeover_chain_max_depth = 1
    source = manager.create_run(goal="短超时任务", thought="原 worker 超时", plan=["写文件"], role="worker")
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first_timeout"))
    takeover = manager.load(first.takeover_run_id)
    takeover.status = "TIMEOUT"
    takeover.failure_type = "runner_timeout"
    manager.save(takeover)

    second = manager.create_takeover_run(TakeoverRunRequest(source_run_id=takeover.id, reason="second_timeout"))
    reloaded_takeover = manager.load(takeover.id)

    assert second.created is False
    assert second.takeover_run_id == ""
    assert "chain exhausted" in second.message
    assert reloaded_takeover.status == "BLOCKED"
    assert reloaded_takeover.failure_type == "takeover_chain_exhausted"
    assert len(manager.list_runs()) == 2


# LLM: zero takeover chain depth means the default path is unrestricted, not disabled.
# 函数用途: 默认/显式 0 不应把第一个接管 run 直接判成链路耗尽。
def test_takeover_chain_zero_allows_replacement_creation(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, takeover_chain_max_depth=0)
    source = manager.create_run(goal="真实长任务", thought="中途失败", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="timeout"))

    assert result.created is True
    assert result.takeover_run_id


# LLM: stale RUNNING recovery must clear the abandoned active attempt before a takeover can safely continue.
# 函数用途: 复现 gateway/runner 进程被杀后遗留 RUNNING active attempt；action apply 应先落 TIMEOUT 再创建接管 run。
def test_action_takeover_abandons_stale_active_attempt(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="生成购物站", thought="真实 runner 被杀", plan=["写页面"], role="worker")
    source.status = "RUNNING"
    source.runner_active_attempt_id = "attempt-stale"
    source.runner_last_attempt_at = time.time() - 600
    source.heartbeat_at = time.time() - 600
    source.updated_at = time.time() - 600
    manager.save(source)

    report = manager.apply_actions(
        CapabilityConfig(subagent_heartbeat_timeout=1, subagent_run_timeout=0),
        apply=True,
    )
    reloaded = manager.load(source.id)

    assert report.summary["takeover_or_reassign"] == 1
    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.runner_active_attempt_id == ""
    assert "attempt-stale" in reloaded.runner_abandoned_attempt_ids
    assert reloaded.takeover_by


# LLM: provider timeout after partial artifact creation should enter takeover recovery instead of manual blocker classification.
# 函数用途: 真实模型超时会把任务写成 BLOCKED/provider_timeout；下一轮 action apply 应创建接管 run 继续。
def test_provider_timeout_blocked_task_creates_takeover_run(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="生成购物站", thought="模型超时", plan=["写页面"], role="worker")
    source.status = "BLOCKED"
    source.failure_type = "provider_timeout"
    source.runner_last_error = "模型接口请求超时"
    manager.save(source)

    plan = manager.plan_actions(CapabilityConfig())
    assert plan.actions[0].action == "takeover_or_reassign"

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    reloaded = manager.load(source.id)

    assert report.summary["takeover_or_reassign"] == 1
    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by


# LLM: artifact integrity blockers should become scoped repair children, not generic manual classify work.
# 函数用途: 真实购物站失败会写 BLOCKED/artifact_integrity_failed；action apply 应自动派修复小傻妞。
def test_artifact_integrity_blocked_task_creates_repair_child(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text('<html><body><a href="#">占位</a></body></html>', encoding="utf-8")
    source = manager.create_run(
        goal="生成购物站",
        thought="产物检查失败",
        plan=["修复 HTML"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    blocker = f"artifact_integrity_failed:{output}:placeholder_hash_link"
    source.status = "BLOCKED"
    source.failure_type = "artifact_integrity_failed"
    source.blockers = [blocker]
    source.artifact_refs = [str(output)]
    manager.save(source)
    from pathlib import Path

    Path(source.output_json).write_text(
        "{"
        f'"run_id":"{source.id}",'
        '"status":"BLOCKED",'
        '"failure_type":"artifact_integrity_failed",'
        f'"blockers":["{blocker}"],'
        f'"artifacts":["{output}"]'
        "}",
        encoding="utf-8",
    )

    plan = manager.plan_actions(CapabilityConfig())
    assert plan.actions[0].action == "create_repair_child_from_artifact_integrity_refs"

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "artifact_integrity"
    ]

    assert report.summary["create_repair_child_from_artifact_integrity_refs"] == 1
    assert len(repair_runs) == 1
    assert str(output.parent) in repair_runs[0].allowed_write_roots
    assert str(output) in repair_runs[0].attributes["target_artifact_refs"]


# LLM: artifact repair creation must be idempotent when due-check sees the same failed source again.
# 函数用途: 防止同一个产物验收失败反复创建多批相同修复小傻妞。
def test_artifact_integrity_repair_child_creation_is_idempotent(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text('<html><body><a href="#">占位</a></body></html>', encoding="utf-8")
    source = manager.create_run(
        goal="生成购物站",
        thought="产物检查失败",
        plan=["修复 HTML"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    blocker = f"artifact_integrity_failed:{output}:placeholder_hash_link"
    source.status = "BLOCKED"
    source.failure_type = "artifact_integrity_failed"
    source.blockers = [blocker]
    source.artifact_refs = [str(output)]
    manager.save(source)
    from pathlib import Path

    Path(source.output_json).write_text(
        "{"
        f'"run_id":"{source.id}",'
        '"status":"BLOCKED",'
        '"failure_type":"artifact_integrity_failed",'
        f'"blockers":["{blocker}"],'
        f'"artifacts":["{output}"]'
        "}",
        encoding="utf-8",
    )

    manager.apply_actions(CapabilityConfig(), apply=True)
    manager.apply_actions(CapabilityConfig(), apply=True)
    repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "artifact_integrity"
    ]

    assert len(repair_runs) == 1

    repair_runs[0].status = "TAKEN_OVER"
    manager.save(repair_runs[0])
    manager.apply_actions(CapabilityConfig(), apply=True)
    repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "artifact_integrity"
    ]

    assert len(repair_runs) == 1
    assert repair_runs[0].status == "TAKEN_OVER"


# LLM: once a repair child is VERIFIED the source blocker must be closed, not planned again forever.
# 函数用途: 复现真实购物站修复子任务通过后，父任务仍 BLOCKED 且 due-check 重复派 repair 的脏状态。
def test_verified_artifact_integrity_repair_child_closes_blocked_parent(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button>购买</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="生成购物站",
        thought="产物检查失败",
        plan=["修复 HTML"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    blocker = f"artifact_integrity_failed:{output}:placeholder_hash_link"
    source.status = "BLOCKED"
    source.failure_type = "artifact_integrity_failed"
    source.blockers = [blocker]
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.output_json).write_text(
        json.dumps(
            {
                "run_id": source.id,
                "status": "BLOCKED",
                "failure_type": "artifact_integrity_failed",
                "blockers": [blocker],
                "artifacts": [str(output)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repair = manager.create_run(
        goal="修复购物站产物",
        thought="确定性修复",
        plan=["修复占位链接"],
        role="worker",
        parent_id=source.id,
        root_id=source.id,
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "artifact_integrity",
            "repair_source_run_id": source.id,
            "target_artifact_refs": [str(output)],
        },
    )
    repair.status = "DONE"
    repair.verification_status = "VERIFIED"
    repair.artifact_refs = [str(output)]
    from agent_py_agent.agent.subagents.model_capabilities import VerificationEvidence

    repair.evidence.append(
        VerificationEvidence(kind="artifact_integrity_repair", summary="修复已通过", path=str(output), ok=True)
    )
    manager.save(repair)

    plan = manager.plan_actions(CapabilityConfig())
    assert plan.actions[0].action == "close_parent_from_verified_repair_child"

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    reloaded = manager.load(source.id)

    assert report.summary["close_parent_from_verified_repair_child"] == 1
    assert reloaded.status == "DONE"
    assert reloaded.verification_status == "VERIFIED"
    assert reloaded.failure_type == ""
    assert reloaded.blockers == []
    assert reloaded.attributes["resolved_by_repair_run_id"] == repair.id
    assert manager.plan_actions(CapabilityConfig()).actions == []


# LLM: failed parent acceptance tests should create a scoped repair child without waiting for another model turn.
# 函数用途: 真实购物站 static_site_check 失败后，due-check/action-apply 应自动派验收修复小傻妞。
def test_parent_acceptance_rejected_task_creates_repair_child(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs", workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><form><input></form></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="生成购物站",
        thought="父级验收失败",
        plan=["修复静态站点"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    source.status = "AWAITING_ACCEPTANCE"
    source.verification_status = "NEEDS_ACCEPTANCE"
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.reports_dir, "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT"}, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(source.reports_dir, "test_execution.json").write_text(
        json.dumps(
            {
                "total_tests": 2,
                "failed": 1,
                "records": [
                    {
                        "test_name": "inferred static site check",
                        "validation_method": "static_site_check",
                        "error": "missing_dom_id_hits=3",
                        "validation_result": {
                            "checked_root": str(output.parent),
                            "checked_files": ["index.html"],
                            "missing_dom_id_hits": [
                                "getElementById:checkoutName",
                                "getElementById:checkoutAddress",
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    Path(source.reports_dir, "parent_acceptance_auto_followup.json").write_text(
        json.dumps(
            {
                "followup": {
                    "status": "needs_manual_rescue",
                    "action": "plan_rescue",
                    "failed_tests": [
                        {
                            "name": "inferred static site check",
                            "error": "missing_dom_id_hits=3",
                            "validation_method": "static_site_check",
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan = manager.plan_actions(CapabilityConfig())
    assert plan.actions[0].action == "create_repair_child_from_parent_acceptance_refs"

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "parent_acceptance"
    ]

    assert report.summary["create_repair_child_from_parent_acceptance_refs"] == 1
    assert len(repair_runs) == 1
    assert str(output.parent) in repair_runs[0].allowed_write_roots
    assert str(output) in repair_runs[0].attributes["target_artifact_refs"]


# LLM: repair chain idempotency must include children that already entered recovery.
# 函数用途: 旧验收修复 run 已被接管后，父任务再次 due-check 不能创建 sibling repair。
def test_parent_acceptance_repair_reuses_taken_over_child_chain(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs", workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button>购买</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="生成购物站",
        thought="父级验收失败",
        plan=["修复静态站点"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    source.status = "AWAITING_ACCEPTANCE"
    source.verification_status = "NEEDS_ACCEPTANCE"
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.reports_dir, "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT"}, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(source.reports_dir, "test_execution.json").write_text(
        json.dumps(
            {
                "total_tests": 1,
                "failed": 1,
                "records": [
                    {
                        "test_name": "inferred static site check",
                        "validation_method": "static_site_check",
                        "error": "missing_dom_id_hits=1",
                        "validation_result": {
                            "checked_root": str(output.parent),
                            "checked_files": ["index.html"],
                            "missing_dom_id_hits": ["getElementById:toast"],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old_repair = manager.create_run(
        goal="旧验收修复",
        thought="已经失败并被接管",
        plan=["修复"],
        role="worker",
        parent_id=source.id,
        root_id=source.root_id or source.id,
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "parent_acceptance",
            "repair_source_run_id": source.id,
            "target_artifact_refs": [str(output)],
        },
    )
    old_repair.status = "TAKEN_OVER"
    manager.save(old_repair)

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "parent_acceptance"
    ]

    assert report.summary["create_repair_child_from_parent_acceptance_refs"] == 1
    assert len(repair_runs) == 1
    assert repair_runs[0].id == old_repair.id
    assert repair_runs[0].status == "TAKEN_OVER"


# LLM: verified parent-acceptance repair children close their rejected parent instead of nesting forever.
# 函数用途: 验收修复子任务已过时，父任务要收口，不能继续保持 REJECT/NEEDS_ACCEPTANCE。
def test_verified_parent_acceptance_repair_child_closes_rejected_parent(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs", workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button>购买</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="修复购物站产物",
        thought="父级验收失败",
        plan=["修复静态站点"],
        role="worker",
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "artifact_integrity",
            "repair_source_run_id": "root-run",
            "target_artifact_refs": [str(output)],
        },
    )
    source.status = "AWAITING_ACCEPTANCE"
    source.verification_status = "NEEDS_ACCEPTANCE"
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.reports_dir, "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT"}, ensure_ascii=False),
        encoding="utf-8",
    )
    repair = manager.create_run(
        goal="修复父级验收失败",
        thought="修复 DOM 缺口",
        plan=["修复"],
        role="worker",
        parent_id=source.id,
        root_id=source.root_id or source.id,
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "parent_acceptance",
            "repair_source_run_id": source.id,
            "target_artifact_refs": [str(output)],
        },
    )
    repair.status = "DONE"
    repair.verification_status = "VERIFIED"
    repair.artifact_refs = [str(output)]
    from agent_py_agent.agent.subagents.model_capabilities import VerificationEvidence

    repair.evidence.append(
        VerificationEvidence(kind="parent_acceptance_repair", summary="验收修复已通过", path=str(output), ok=True)
    )
    manager.save(repair)

    plan = manager.plan_actions(CapabilityConfig())
    assert plan.actions[0].action == "close_parent_from_verified_repair_child"

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    reloaded = manager.load(source.id)

    assert report.summary["close_parent_from_verified_repair_child"] == 1
    assert reloaded.status == "DONE"
    assert reloaded.verification_status == "VERIFIED"
    assert reloaded.attributes["resolved_by_repair_run_id"] == repair.id


# LLM: failed parent-acceptance repair workers must not recursively spawn more parent-acceptance repairs.
# 函数用途: 验收修复卡自己被拒绝时，应转入接管链路，而不是继续创建同类验收修复卡。
def test_parent_acceptance_repair_task_plans_takeover_not_nested_repair(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs", workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button>购买</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="修复父级验收失败",
        thought="验收修复失败",
        plan=["修复 DOM"],
        role="worker",
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "parent_acceptance",
            "repair_source_run_id": "parent-run",
            "target_artifact_refs": [str(output)],
        },
    )
    source.status = "AWAITING_ACCEPTANCE"
    source.verification_status = "NEEDS_ACCEPTANCE"
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.reports_dir, "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT"}, ensure_ascii=False),
        encoding="utf-8",
    )

    plan = manager.plan_actions(CapabilityConfig())

    assert plan.actions[0].action == "takeover_or_reassign"
    assert "parent_acceptance_repair_failed" in plan.actions[0].source_issue_kinds


# LLM: auto apply of failed parent-acceptance repair must create a takeover run without manual --take-over-by.
# 函数用途: 自动恢复链路没有用户手填参数时，也能接管验收修复失败的任务继续推进。
def test_parent_acceptance_repair_task_apply_creates_takeover_run(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs", workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button>购买</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="修复父级验收失败",
        thought="验收修复失败",
        plan=["修复 DOM"],
        role="worker",
        extra_write_roots=[str(output.parent)],
        attributes={
            "repair_kind": "parent_acceptance",
            "repair_source_run_id": "parent-run",
            "target_artifact_refs": [str(output)],
        },
    )
    source.status = "AWAITING_ACCEPTANCE"
    source.verification_status = "NEEDS_ACCEPTANCE"
    source.artifact_refs = [str(output)]
    manager.save(source)
    Path(source.reports_dir, "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT"}, ensure_ascii=False),
        encoding="utf-8",
    )

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    reloaded = manager.load(source.id)
    takeover_runs = [task for task in manager.list_runs() if task.attributes.get("takeover_source_run_id") == source.id]

    assert report.summary["takeover_or_reassign"] == 1
    assert reloaded.status == "TAKEN_OVER"
    assert len(takeover_runs) == 1


# LLM: failed repair workers must not recursively spawn more repair workers.
# 函数用途: artifact repair 本身失败时，应转入接管链路，避免 repair child 无限套娃。
def test_artifact_integrity_repair_task_plans_takeover_not_nested_repair(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text('<html><body><a href="#">占位</a></body></html>', encoding="utf-8")
    source = manager.create_run(
        goal="修复购物站 HTML",
        thought="repair worker 修复失败",
        plan=["修复 HTML"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    blocker = f"artifact_integrity_failed:{output}:placeholder_hash_link"
    source.status = "BLOCKED"
    source.failure_type = "artifact_integrity_failed"
    source.blockers = [blocker]
    source.artifact_refs = [str(output)]
    source.attributes["repair_kind"] = "artifact_integrity"
    manager.save(source)
    from pathlib import Path

    Path(source.output_json).write_text(
        "{"
        f'"run_id":"{source.id}",'
        '"status":"BLOCKED",'
        '"failure_type":"artifact_integrity_failed",'
        f'"blockers":["{blocker}"],'
        f'"artifacts":["{output}"]'
        "}",
        encoding="utf-8",
    )

    plan = manager.plan_actions(CapabilityConfig())

    assert plan.actions[0].action == "takeover_or_reassign"
    assert "artifact_repair_failed" in plan.actions[0].source_issue_kinds


# LLM: action apply must preserve the no-nested-repair contract, not only dry-run planning.
# 函数用途: apply 阶段真的执行时，也只能创建 takeover run，不能给 repair run 再生 repair child。
def test_artifact_integrity_repair_task_apply_creates_takeover_not_nested_repair(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    output = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text("<html><body><button onclick=\"checkout()\">去结算</button></body></html>", encoding="utf-8")
    source = manager.create_run(
        goal="修复购物站 HTML",
        thought="repair worker 修复失败",
        plan=["修复 HTML"],
        role="worker",
        extra_write_roots=[str(output.parent)],
    )
    blocker = f"artifact_integrity_failed:{output}:missing_onclick_handler"
    source.status = "BLOCKED"
    source.failure_type = "artifact_integrity_failed"
    source.blockers = [blocker]
    source.artifact_refs = [str(output)]
    source.attributes["repair_kind"] = "artifact_integrity"
    manager.save(source)
    from pathlib import Path

    Path(source.output_json).write_text(
        "{"
        f'"run_id":"{source.id}",'
        '"status":"BLOCKED",'
        '"failure_type":"artifact_integrity_failed",'
        f'"blockers":["{blocker}"],'
        f'"artifacts":["{output}"]'
        "}",
        encoding="utf-8",
    )

    report = manager.apply_actions(CapabilityConfig(), apply=True)
    reloaded = manager.load(source.id)
    nested_repair_runs = [
        task for task in manager.list_runs()
        if task.parent_id == source.id and task.attributes.get("repair_kind") == "artifact_integrity"
    ]

    assert report.summary["takeover_or_reassign"] == 1
    assert nested_repair_runs == []
    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by
