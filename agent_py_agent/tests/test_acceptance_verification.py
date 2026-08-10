"""Machine verification for DONE acceptance tests (审计 R0 后:仅进程内文件检查).

审计 R0:模型验收 command/cwd/working_dir 执行路径已删除。command 只保留为
inert evidence(永不执行、不参与 VERIFIED 判定);可机验方式仅剩
file_check / content_check / static_site_check / artifact_integrity。
本测试锁定 verify_done_acceptance 的裁决规则:
- 全部可机验条目通过 → VERIFIED 成立;
- 任何失败 → 打真实失败事实,保持 UNVERIFIED(由 ISSUE_UNVERIFIED_DONE
  返工门接管);
- 模型自述 ok/status 以机器验证为准;
- 无 tests / 无产物证据 → NO_CHECKABLE_TESTS 不可绑定;
- 纯 command 项不可绑定机器 → NO_CHECKABLE_TESTS;
- 来源 worker(ledger 权威)跳过;
- owner-scoped 时 file_check 路径边界放开到 owner home,owner home 外拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.model_capabilities import VerificationEvidence
from agent_py_agent.agent.subagents.model_runtime import SubAgentParsedOutput
from agent_py_agent.agent.subagents.services.acceptance_verification import (
    verify_done_acceptance,
)


def _done_task(tmp_path: Path) -> object:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="machine verify", thought="observe", plan=["run"])
    task.status = "DONE"
    return task


def _task_workspace(task) -> Path:
    """与 verify_done_acceptance._workspace_for 同一解析顺序,供测试写真实文件。"""
    for attr in ("agent_run_workspace_dir", "task_dir", "output_dir", "tests_dir"):
        candidate = getattr(task, attr, "") or ""
        if candidate:
            path = Path(candidate)
            path.mkdir(parents=True, exist_ok=True)
            return path
    raise AssertionError("task has no workspace dir")


def _done_structured(tests: list[dict]) -> SubAgentParsedOutput:
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="done",
        tests=tests,
    )


def test_machine_verification_passes_only_when_checkable_items_pass(tmp_path):
    task = _done_task(tmp_path)
    proof = _task_workspace(task) / "proof.txt"
    proof.write_text("ok", encoding="utf-8")
    tests = [
        {"name": "file exists", "validation_method": "file_check", "file_path": "proof.txt"},
        {"name": "content matches", "validation_method": "content_check", "file_path": "proof.txt", "content_equals": "ok", "match_mode": "exact"},
    ]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is True
    assert result.failures == []
    assert result.checked_count == 2
    assert tests[0]["ok"] is True
    assert tests[0]["status"] == "passed"
    assert tests[0]["verified_by"] == "machine_execution"


def test_machine_verification_records_real_failure_facts(tmp_path):
    task = _done_task(tmp_path)
    tests = [{"name": "missing file", "validation_method": "file_check", "file_path": "nope.txt"}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is False
    assert len(result.failures) == 1
    assert result.failures[0]["name"] == "missing file"
    # 机器事实已打到 test dict 上(随 output_payload["tests"] 出站进 decision ledger)
    assert tests[0]["ok"] is False
    assert tests[0]["status"] == "failed"
    assert "文件不存在" in tests[0]["message"]


def test_machine_verdict_overrides_model_claimed_pass(tmp_path):
    """模型自述 ok=true 不算数:文件真不存在 → 机器裁决覆盖。"""
    task = _done_task(tmp_path)
    tests = [{"name": "claimed pass", "validation_method": "file_check", "file_path": "nope.txt", "ok": True}]

    result = verify_done_acceptance(task, tests)

    assert result.passed is False
    assert tests[0]["ok"] is False
    assert tests[0]["status"] == "failed"


def test_command_items_are_inert_evidence_not_checkable(tmp_path):
    """审计 R0:纯 command 项不可绑定机器 → NO_CHECKABLE_TESTS,永不 VERIFIED。"""
    task = _done_task(tmp_path)
    tests = [{"name": "python ok", "command": "python -c 'print(1)'", "ok": True}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is False
    assert result.passed is False
    assert result.reason == "NO_CHECKABLE_TESTS"
    # 模型自述的 ok 不被机器覆盖(没有机器验证),也不被采信为 VERIFIED。
    assert tests[0]["ok"] is True


def test_inert_command_records_do_not_fail_verification(tmp_path):
    """command 项不参与裁决:同批 file_check 通过时,command 项不影响 VERIFIED。"""
    task = _done_task(tmp_path)
    proof = _task_workspace(task) / "proof.txt"
    proof.write_text("ok", encoding="utf-8")
    tests = [
        {"name": "real check", "validation_method": "file_check", "file_path": "proof.txt"},
        {"name": "legacy command", "command": "python -c 'raise SystemExit(1)'"},
    ]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is True
    assert result.checked_count == 1


def test_no_checkable_tests_cannot_bind(tmp_path):
    task = _done_task(tmp_path)

    result = verify_done_acceptance(task, [])

    assert result.checked is False
    assert result.passed is False
    assert result.reason == "NO_CHECKABLE_TESTS"


def test_registered_artifact_evidence_becomes_machine_file_check(tmp_path):
    """C3/G4 据 artifact_registry 合成 DONE 的路径:产物真的在盘上才通过。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="registry done", thought="observe", plan=["run"])
    task.status = "DONE"
    artifact = tmp_path / "subs" / task.id / "proof.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("ok", encoding="utf-8")
    task.evidence.append(
        VerificationEvidence(kind="registered_artifact", summary="ready product", path=str(artifact))
    )

    passed = verify_done_acceptance(task, [])
    assert passed.checked is True
    assert passed.passed is True

    artifact.unlink()
    failed = verify_done_acceptance(task, [])
    assert failed.checked is True
    assert failed.passed is False
    assert len(failed.failures) == 1
    assert "registered artifact exists" in failed.failures[0]["name"]


def test_ledger_authoritative_source_worker_skips_machine_verification(tmp_path):
    """来源 worker 的 DONE 由持久账本证明,模型 tests 不适用机器裁决。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="source worker", thought="observe", plan=["run"])
    task.status = "DONE"
    task.attributes = {
        "audit_source_worker": True,
        "conversation_request_id": "conv-1",
        "audit_source_id": "src-1",
        "audit_source_watch_id": "w-1",
        # worker_key 的 audit_id 取自 conversation_request_id(见 audit_activation),
        # 不是 audit_source_id;生产打标同规则(debug 实证 audit_source_worker_key("conv-1","w-1"))。
        "audit_source_worker_key": "audit-source:conv-1:w-1",
        "audit_source_owner_home": str(tmp_path),
    }

    result = verify_done_acceptance(task, [{"name": "fake", "validation_method": "file_check", "file_path": "x.txt"}])

    assert result.checked is False
    assert result.reason == "LEDGER_AUTHORITATIVE"


def test_non_done_task_is_not_verified(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="pending", thought="observe", plan=["run"])

    result = verify_done_acceptance(task, [{"name": "x", "validation_method": "file_check", "file_path": "x.txt"}])

    assert result.checked is False
    assert result.reason == "NOT_DONE"


def test_owner_scoped_file_check_can_reach_artifact_outside_workspace(tmp_path):
    """owner-scoped 时 file_check 路径边界放开到 owner home:注册表背书的绝对
    产物路径可能在任务工作区外、owner home 内,必须可验。"""
    owner = tmp_path / "owner"
    artifact = owner / "shared" / "proof.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    task = _done_task(tmp_path)
    tests = [{"name": "outside workspace artifact", "validation_method": "file_check", "file_path": str(artifact)}]

    result = verify_done_acceptance(task, tests, owner_home=str(owner))

    assert result.checked is True
    assert result.passed is True


def test_owner_scoped_file_check_outside_owner_home_rejected(tmp_path):
    """owner-scoped 验收:file_path 在 owner home 之外 → 机器失败,绝不越界检查。"""
    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    task = _done_task(tmp_path)
    tests = [{"name": "outside owner", "validation_method": "file_check", "file_path": str(outside / "secret.txt")}]

    result = verify_done_acceptance(task, tests, owner_home=str(owner))

    assert result.checked is True
    assert result.passed is False
    assert "超出可执行边界" in result.failures[0]["message"]


def test_unsandboxed_absolute_file_check_is_trusted(tmp_path):
    """无 owner scope(owner_home 空)与 run_command 普通执行同可信级:绝对路径照实检查。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "proof.txt").write_text("ok", encoding="utf-8")
    task = _done_task(tmp_path)
    tests = [{"name": "absolute path", "validation_method": "file_check", "file_path": str(target / "proof.txt")}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is True


def test_wiring_downgrades_done_to_unverified_and_stamps_blockers(tmp_path):
    """runner 收口接线:机器验收失败 → task 降回 UNVERIFIED + blockers + payload facts。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="wire verify", thought="observe", plan=["run"])
    manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="done",
            status="DONE",
            verification_status="VERIFIED",
            backend="fake",
            tool_rounds=1,
            structured_output=_done_structured(
                [{"name": "missing file", "validation_method": "file_check", "file_path": "nope.txt"}]
            ),
        )
    )

    # record_runner_result 内部 reload 任务,权威状态在持久化副本上
    persisted = manager.load(task.id)
    assert persisted.status == "DONE"
    assert persisted.verification_status == "UNVERIFIED"
    assert any("机器验收未通过" in blocker for blocker in persisted.blockers)
    assert "missing file" in " ".join(persisted.blockers)
    # 机器失败事实随 output_payload["tests"] 出站(decision ledger 用同一对象)
    payload = json.loads(Path(persisted.output_json).read_text(encoding="utf-8"))
    assert payload["tests"][0]["ok"] is False
    assert "文件不存在" in payload["tests"][0]["message"]


def test_wiring_keeps_verified_when_machine_passes(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="wire pass", thought="observe", plan=["run"])
    proof = _task_workspace(task) / "proof.txt"
    proof.write_text("ok", encoding="utf-8")
    manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="done",
            status="DONE",
            verification_status="VERIFIED",
            backend="fake",
            tool_rounds=1,
            structured_output=_done_structured(
                [{"name": "file ok", "validation_method": "file_check", "file_path": "proof.txt"}]
            ),
        )
    )

    persisted = manager.load(task.id)
    assert persisted.status == "DONE"
    assert persisted.verification_status == "VERIFIED"
    assert persisted.blockers == []
