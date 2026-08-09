"""Machine verification for DONE acceptance tests (问题3:验收证据绑定机器执行).

子代理口头标 DONE/VERIFIED、failing_tests=[] 但真实 build/test 仍失败 = 机器
状态与证据未绑定。本测试锁定 verify_done_acceptance 的裁决规则:
- 全部可机验条目真实通过 → VERIFIED 成立;
- 任何失败/命令被拒 → 打真实失败事实,保持 UNVERIFIED(由 ISSUE_UNVERIFIED_DONE
  返工门接管);
- 模型自述 ok/status 以机器执行为准;
- 无 tests / 无产物证据 → NO_CHECKABLE_TESTS 不可绑定;
- 来源 worker(ledger 权威)跳过;
- owner-scoped 无 bwrap → fail-closed SANDBOX_UNAVAILABLE,绝不裸跑模型命令。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _done_structured(tests: list[dict]) -> SubAgentParsedOutput:
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="done",
        tests=tests,
    )


def test_machine_verification_passes_only_when_all_commands_exit_zero(tmp_path):
    task = _done_task(tmp_path)
    tests = [
        {"name": "python ok", "command": "python -c 'print(1)'"},
        {"name": "file exists", "command": "test -f /etc/hosts"},
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
    tests = [{"name": "must fail", "command": "python -c 'raise SystemExit(7)'"}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is False
    assert len(result.failures) == 1
    assert result.failures[0]["name"] == "must fail"
    assert "7" in result.failures[0]["message"]
    # 机器事实已打到 test dict 上(随 output_payload["tests"] 出站进 decision ledger)
    assert tests[0]["ok"] is False
    assert tests[0]["status"] == "failed"
    assert "exit code 7" in tests[0]["message"]


def test_machine_verdict_overrides_model_claimed_pass(tmp_path):
    """模型自述 ok=true 不算数:命令真失败 → 机器裁决覆盖。"""
    task = _done_task(tmp_path)
    tests = [{"name": "claimed pass", "command": "python -c 'raise SystemExit(3)'", "ok": True}]

    result = verify_done_acceptance(task, tests)

    assert result.passed is False
    assert tests[0]["ok"] is False
    assert tests[0]["status"] == "failed"


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

    result = verify_done_acceptance(task, [{"name": "fake", "command": "python -c 'raise SystemExit(1)'"}])

    assert result.checked is False
    assert result.reason == "LEDGER_AUTHORITATIVE"


def test_non_done_task_is_not_verified(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="pending", thought="observe", plan=["run"])

    result = verify_done_acceptance(task, [{"name": "x", "command": "python -c 'print(1)'"}])

    assert result.checked is False
    assert result.reason == "NOT_DONE"


def test_owner_scoped_without_bwrap_fails_closed(tmp_path, monkeypatch):
    """owner-scoped 验收必须 bwrap 隔离;bwrap 缺失 → 抛 SandboxUnavailable,绝不裸跑。"""
    import agent_py_agent.agent.tooling.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "find_bwrap", lambda: None)
    task = _done_task(tmp_path)
    tests = [{"name": "needs sandbox", "command": "python -c 'print(1)'"}]

    with pytest.raises(sandbox_mod.SandboxUnavailable):
        verify_done_acceptance(task, tests, owner_home=str(tmp_path / "owner"))


def test_owner_scoped_sandbox_prefix_wraps_command_in_bwrap(tmp_path, monkeypatch):
    """bwrap 可用时命令必须包在 bwrap argv 里执行(与 run_command 同一把门)。"""
    import agent_py_agent.agent.tooling.sandbox as sandbox_mod

    # Mac 开发机无 bwrap(生产 testbox 有内置版);此处只验 argv 组装,不需真二进制。
    monkeypatch.setattr(sandbox_mod, "find_bwrap", lambda: "/usr/bin/bwrap")
    from agent_py_agent.agent.subagents.services.acceptance_verification import (
        _sandbox_execution_prefix,
    )

    prefix, env = _sandbox_execution_prefix(str(tmp_path / "owner"), tmp_path / "subs")

    argv = list(prefix)
    assert argv[0].endswith("bwrap")
    assert "--bind" in argv
    assert "--" in argv  # 分隔符前是 bwrap argv,后是用户命令
    assert "bwrap" in argv[0]  # bwrap 以全路径作为首个元素
    assert env["HOME"] == str(tmp_path / "owner")


def test_owner_scoped_runner_binds_bwrap_chdir_to_command_cwd(tmp_path, monkeypatch):
    """模型声明的 working_dir 决定 bwrap --chdir(与 run_command 同一语义)。

    否则沙箱里 --chdir workspace 覆盖 Popen cwd,`go build ./...` 跑错目录假失败。
    """
    import agent_py_agent.agent.tooling.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "find_bwrap", lambda: "/usr/bin/bwrap")
    from agent_py_agent.agent.subagents.services.acceptance_verification import _runner_for

    owner = tmp_path / "owner"
    proj = owner / "proj"
    proj.mkdir(parents=True)
    runner = _runner_for(tmp_path / "workspace", str(owner), command_cwd=proj)

    assert "--chdir" in runner.argv_prefix
    assert str(proj) in runner.argv_prefix[runner.argv_prefix.index("--chdir") + 1]
    # 路径边界 = 沙箱可写宇宙(owner home),不是窄 workspace
    assert runner.boundary_root == owner.resolve()
    assert runner.unrestricted_paths is False


def test_owner_scoped_absolute_working_dir_outside_owner_home_rejected(tmp_path, monkeypatch):
    """owner-scoped 验收:工作目录在 owner home 之外 → 机器失败,绝不越界执行。"""
    import agent_py_agent.agent.tooling.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "find_bwrap", lambda: "/usr/bin/bwrap")
    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    task = _done_task(tmp_path)
    tests = [{"name": "outside cwd", "command": "python -c 'print(1)'", "working_dir": str(outside)}]

    result = verify_done_acceptance(task, tests, owner_home=str(owner))

    assert result.checked is True
    assert result.passed is False
    assert "可执行边界" in result.failures[0]["message"]


def test_unsandboxed_absolute_working_dir_is_trusted(tmp_path):
    """无沙箱可信环境(owner_home 空)与 run_command 普通执行同可信级:绝对路径照实执行。"""
    target = tmp_path / "target"
    target.mkdir()
    task = _done_task(tmp_path)
    tests = [{"name": "absolute cwd", "command": "python -c 'print(1)'", "working_dir": str(target)}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is True


def test_owner_scoped_sandbox_prefix_uses_host_env_like_run_command(tmp_path, monkeypatch):
    """验收命令 env 与 run_command 同一套(host env + 凭据擦洗),沙箱内外解析一致。"""
    import agent_py_agent.agent.tooling.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "find_bwrap", lambda: "/usr/bin/bwrap")
    task = _done_task(tmp_path)
    tests = [{"name": "env preserved", "command": "python -c 'import os; assert os.environ.get(\"PATH\")'"}]

    result = verify_done_acceptance(task, tests, owner_home=str(tmp_path / "owner"))

    # Mac 无 bwrap 二进制 → 子进程启动失败被记为机器失败,而不是提权裸跑;
    # Linux(有 bwrap)则真实通过。两种结局都不会出现"无沙箱直跑"。
    assert result.checked is True
    assert (result.passed is True) == (result.failures == [])


def test_command_outside_allowlist_is_rejected_and_fails_verification(tmp_path):
    task = _done_task(tmp_path)
    tests = [{"name": "rm not allowed", "command": "rm -f /dev/null"}]

    result = verify_done_acceptance(task, tests)

    assert result.checked is True
    assert result.passed is False
    assert "allowlist" in result.failures[0]["message"]


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
                [{"name": "failing build", "command": "python -c 'raise SystemExit(9)'"}]
            ),
        )
    )

    # record_runner_result 内部 reload 任务,权威状态在持久化副本上
    persisted = manager.load(task.id)
    assert persisted.status == "DONE"
    assert persisted.verification_status == "UNVERIFIED"
    assert any("机器验收未通过" in blocker for blocker in persisted.blockers)
    assert "failing build" in " ".join(persisted.blockers)
    # 机器失败事实随 output_payload["tests"] 出站(decision ledger 用同一对象)
    payload = json.loads(Path(persisted.output_json).read_text(encoding="utf-8"))
    assert payload["tests"][0]["ok"] is False
    assert "exit code 9" in payload["tests"][0]["message"]


def test_wiring_keeps_verified_when_machine_passes(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="wire pass", thought="observe", plan=["run"])
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
                [{"name": "python ok", "command": "python -c 'print(1)'"}]
            ),
        )
    )

    persisted = manager.load(task.id)
    assert persisted.status == "DONE"
    assert persisted.verification_status == "VERIFIED"
    assert persisted.blockers == []
