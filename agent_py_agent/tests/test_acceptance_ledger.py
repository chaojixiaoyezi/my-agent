"""R3 验收账本主链接线测试（R6 gate：机器裁决持久化落账）。

ledgerize_done_acceptance 把 verify_done_acceptance 的机器裁决（不采信模型
自报）持久化到 runtime.db：契约冻结（首冻者定契约，CAS 防分叉）+ 每条
机器盖章断言落 validator_operations（带 G2 补尾 assertion_key，VERIFIED/
FAILED 终态）。

铁律：纯落账 fail-silent——所有边界场景不抛、不改变现有裁决/收口行为
（VERIFIED/UNVERIFIED 判定与 blockers 保持原语义）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.acceptance_ledger import (
    ledgerize_done_acceptance,
)
from agent_py_agent.agent.subagents.services.acceptance_verification import (
    verify_done_acceptance,
)

OWNER = "local/main"


@pytest.fixture
def done_task(tmp_path):
    """DONE 状态的假子代理任务（与 test_acceptance_verification 同构造）。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="ledger verify", thought="observe", plan=["run"])
    task.status = "DONE"
    return task


@pytest.fixture
def repo(done_task, tmp_path):
    """真权威库 + 该任务的权威 AgentRun 链（create_run 的库层等价物）。"""
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    repo.record_run_creation(
        owner_id=OWNER, run_id=done_task.id, goal="ledger verify"
    )
    return repo


def _task_workspace(task) -> Path:
    """与 verify_done_acceptance._workspace_for 同一解析顺序，供写真实文件。"""
    for attr in ("agent_run_workspace_dir", "task_dir", "output_dir", "tests_dir"):
        candidate = getattr(task, attr, "") or ""
        if candidate:
            path = Path(candidate)
            path.mkdir(parents=True, exist_ok=True)
            return path
    raise AssertionError("task has no workspace dir")


def _run_ledger(task, tests, repo=None):
    """裁决 + 落账全链路（生产调用顺序）。"""
    result = verify_done_acceptance(task, tests)
    ledgerize_done_acceptance(task, tests, result, repo=repo)
    return result


def _contract_rows(repo):
    with repo._runtime_connection() as conn:
        return conn.execute("SELECT * FROM acceptance_contracts").fetchall()


def _op_rows(repo):
    with repo._runtime_connection() as conn:
        return conn.execute("SELECT * FROM validator_operations").fetchall()


# ------------------------------------------------------------------- 落账
def test_ledger_freezes_contract_and_writes_verified_op(done_task, repo):
    """真 html + file_check → 契约 1 行 + current_contract_id 落定 + VERIFIED op。"""
    proof = _task_workspace(done_task) / "index.html"
    proof.write_text("<html><body>hi</body></html>", encoding="utf-8")
    tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "index.html"},
    ]
    result = _run_ledger(done_task, tests, repo=repo)

    assert result.checked is True and result.passed is True
    contracts = _contract_rows(repo)
    assert len(contracts) == 1
    run_row = repo.get_task_run(str(contracts[0]["task_run_id"]))
    assert run_row["current_contract_id"] == contracts[0]["contract_id"]
    ops = _op_rows(repo)
    assert len(ops) == 1
    op = ops[0]
    assert op["assertion_key"] == "artifact_acceptance::html"
    assert op["validator_ref"] == "artifact_acceptance"
    assert op["validator_kind"] == "pure"
    assert op["status"] == "VERIFIED"
    assert op["exit_code"] == 0
    assert op["code_digest"] != ""


def test_ledger_failed_check_writes_failed_op_with_facts(done_task, repo):
    """file_check 指向不存在文件 → FAILED op 带失败事实；裁决不回归 UNVERIFIED。"""
    _task_workspace(done_task)
    tests = [
        {"name": "missing file", "validation_method": "file_check", "file_path": "nope.html"},
    ]
    result = _run_ledger(done_task, tests, repo=repo)

    assert result.checked is True and result.passed is False
    assert done_task.verification_status == "UNVERIFIED"  # 裁决保持原语义
    ops = _op_rows(repo)
    assert len(ops) == 1
    op = ops[0]
    assert op["status"] == "FAILED"
    assert op["exit_code"] == 1
    assert op["stderr_text"] != ""
    assert op["assertion_key"] == "artifact_acceptance::html"


def test_ledger_groups_same_key_tests_into_one_assertion(done_task, repo):
    """同 key 多 test（一真一假）→ 契约 1 断言 + op 1 行 + 组内任一失败 → FAILED。"""
    workspace = _task_workspace(done_task)
    (workspace / "a.html").write_text("<html>ok</html>", encoding="utf-8")
    tests = [
        {"name": "file", "validation_method": "file_check", "file_path": "a.html"},
        {"name": "content", "validation_method": "content_check", "file_path": "a.html", "content_equals": "NOPE", "match_mode": "exact"},
    ]
    result = _run_ledger(done_task, tests, repo=repo)

    assert result.passed is False
    assert len(_contract_rows(repo)) == 1
    ops = _op_rows(repo)
    assert len(ops) == 1  # 同 key 合并一条断言
    assert ops[0]["assertion_key"] == "artifact_acceptance::html"
    assert ops[0]["status"] == "FAILED"


def test_ledger_content_check_infers_kind_from_extension(done_task, repo):
    """content_check + proof.md → assertion_key == artifact_acceptance::md。"""
    workspace = _task_workspace(done_task)
    (workspace / "proof.md").write_text("# ok", encoding="utf-8")
    tests = [
        {"name": "md content", "validation_method": "content_check", "file_path": "proof.md", "content_equals": "# ok", "match_mode": "exact"},
    ]
    _run_ledger(done_task, tests, repo=repo)
    ops = _op_rows(repo)
    assert len(ops) == 1
    assert ops[0]["assertion_key"] == "artifact_acceptance::md"
    assert ops[0]["status"] == "VERIFIED"


def test_ledger_uninferable_extension_falls_back_to_wildcard(done_task, repo):
    """无法推断扩展名 → assertion_key == artifact_acceptance::*。"""
    workspace = _task_workspace(done_task)
    (workspace / "proof.bin").write_bytes(b"\x00\x01")
    tests = [
        {"name": "bin exists", "validation_method": "file_check", "file_path": "proof.bin"},
    ]
    _run_ledger(done_task, tests, repo=repo)
    ops = _op_rows(repo)
    assert len(ops) == 1
    assert ops[0]["assertion_key"] == "artifact_acceptance::*"


def test_ledger_static_site_check_maps_ref(done_task, repo):
    """static_site_check → ref=static_site_check、key=static_site_check::*（站点级）。"""
    _task_workspace(done_task)  # 空目录 + required_files 缺位 → 站点必失败
    tests = [
        {"name": "site", "validation_method": "static_site_check", "required_files": ["index.html"]},
    ]
    result = _run_ledger(done_task, tests, repo=repo)

    assert result.checked is True and result.passed is False
    ops = _op_rows(repo)
    assert len(ops) == 1
    assert ops[0]["validator_ref"] == "static_site_check"
    assert ops[0]["assertion_key"] == "static_site_check::*"
    assert ops[0]["status"] == "FAILED"


# ------------------------------------------------------------------- 幂等
def test_ledger_replay_same_attempt_is_idempotent(done_task, repo):
    """同任务同 attempt 重复落账 → 契约仍 1 行、op 仍 1 行（无重复 settle）。"""
    proof = _task_workspace(done_task) / "index.html"
    proof.write_text("<html>ok</html>", encoding="utf-8")
    tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "index.html"},
    ]
    _run_ledger(done_task, tests, repo=repo)
    _run_ledger(done_task, tests, repo=repo)  # 重放（交付重试/重启）

    assert len(_contract_rows(repo)) == 1
    ops = _op_rows(repo)
    assert len(ops) == 1
    assert ops[0]["status"] == "VERIFIED"


def test_ledger_second_child_reuses_existing_contract(done_task, repo):
    """第二子代理（新 attempt、同 task_run）→ 契约复用不重编、差异 key 不落账。"""
    proof = _task_workspace(done_task) / "index.html"
    proof.write_text("<html>ok</html>", encoding="utf-8")
    html_tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "index.html"},
    ]
    _run_ledger(done_task, html_tests, repo=repo)
    contract_id = _contract_rows(repo)[0]["contract_id"]

    # 第二子代理：新 attempt（同 task_run）收口，断言面是 md。
    chain = repo.record_run_creation(
        owner_id=OWNER, run_id=f"{done_task.id}-child2", goal="second child",
        parent_run_id=done_task.id,
    )
    # 复用同一 workspace（同一 task_run 域），写 md 断言文件。
    (Path(done_task.agent_run_workspace_dir) / "proof.md").write_text("# ok", encoding="utf-8")
    md_tests = [
        {"name": "md exists", "validation_method": "file_check", "file_path": "proof.md"},
    ]
    result = verify_done_acceptance(done_task, md_tests)
    ledgerize_done_acceptance(done_task, md_tests, result, repo=repo)

    contracts = _contract_rows(repo)
    assert len(contracts) == 1  # 契约不重编
    assert contracts[0]["contract_id"] == contract_id
    ops = _op_rows(repo)
    assert len(ops) == 1  # md key 不在契约 → 差异断言不落账；html 终态幂等跳过
    assert ops[0]["assertion_key"] == "artifact_acceptance::html"


# ------------------------------------------------------------------- 边界
def test_ledger_no_repo_is_noop(done_task):
    """repo=None（LOCAL_UNMANAGED）→ 不抛、无副作用。"""
    proof = _task_workspace(done_task) / "index.html"
    proof.write_text("<html>ok</html>", encoding="utf-8")
    tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "index.html"},
    ]
    result = verify_done_acceptance(done_task, tests)
    ledgerize_done_acceptance(done_task, tests, result, repo=None)  # 不抛


def test_ledger_missing_agent_run_is_noop(tmp_path):
    """repo 存在但无该 run 的权威记录 → 不抛、两表 0 行。"""
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="no authority", thought="observe", plan=[])
    task.status = "DONE"
    workspace = _task_workspace(task)
    (workspace / "a.html").write_text("<html>ok</html>", encoding="utf-8")
    tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "a.html"},
    ]
    result = verify_done_acceptance(task, tests)
    ledgerize_done_acceptance(task, tests, result, repo=repo)

    assert len(_contract_rows(repo)) == 0
    assert len(_op_rows(repo)) == 0


def test_ledger_no_checkable_tests_is_noop(done_task, repo):
    """tests 仅 command 项（模型自报）→ 无可机验条目，不落账。"""
    _task_workspace(done_task)
    tests = [
        {"name": "claimed", "validation_method": "command", "command": "echo hi", "ok": True},
    ]
    result = verify_done_acceptance(done_task, tests)
    assert result.checked is False
    ledgerize_done_acceptance(done_task, tests, result, repo=repo)

    assert len(_contract_rows(repo)) == 0
    assert len(_op_rows(repo)) == 0


def test_ledger_checked_false_is_noop(done_task, repo):
    """裁决 checked=False（NOT_DONE）→ 不落账。"""
    done_task.status = "RUNNING"  # 非 DONE：裁决与落账都跳过
    tests = [
        {"name": "html exists", "validation_method": "file_check", "file_path": "a.html"},
    ]
    result = verify_done_acceptance(done_task, tests)
    assert result.checked is False
    ledgerize_done_acceptance(done_task, tests, result, repo=repo)

    assert len(_contract_rows(repo)) == 0
    assert len(_op_rows(repo)) == 0


# ------------------------------------------------------------------- 接线
def test_apply_status_and_build_payload_passes_repo(tmp_path):
    """apply_status_and_build_payload 的 repo kwarg 透传不破坏既有路径。"""
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
        apply_status_and_build_payload,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="payload", thought="observe", plan=[])
    task.status = "DONE"
    workspace = _task_workspace(task)
    (workspace / "a.html").write_text("<html>ok</html>", encoding="utf-8")

    extracted = _extracted_for_task(task, [{"name": "html exists", "validation_method": "file_check", "file_path": "a.html"}])
    params = RecordRunnerResultParams(
        run_id=task.id, dry_run=False, ok=True, message="done",
        status="DONE", verification_status="VERIFIED",
    )
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    repo.record_run_creation(owner_id=OWNER, run_id=task.id, goal="payload")
    payload, _ctx = apply_status_and_build_payload(
        params, extracted, 1000.0, owner_home="", repo=repo
    )
    assert payload is not None
    # 裁决 + 落账都发生：op 行 VERIFIED。
    ops = _op_rows(repo)
    assert len(ops) == 1
    assert ops[0]["status"] == "VERIFIED"


def _extracted_for_task(task, tests):
    """构造 _ApplyStatusParams（apply_status_and_build_payload 输入）。"""
    from agent_py_agent.agent.subagents.manager_runner_result_payload import _ApplyStatusParams
    from agent_py_agent.agent.subagents.model_runtime import SubAgentParsedOutput

    parsed = SubAgentParsedOutput(
        found=True, ok=True, status="DONE", summary="done", tests=tests,
    )
    return _ApplyStatusParams(
        task=task,
        parsed=parsed,
        structured_evidence_count=0,
        structured_request_count=0,
        created_request_ids=[],
        structured_repair_attempted=False,
        structured_repair_ok=False,
        structured_repair_error="",
        actual_tools=[],
        ignored_tools=[],
        ignored_skills=[],
        artifacts=[],
        evidence_packets=[],
        findings=[],
        tests=tests,
        patches=[],
        lessons=[],
        next_actions=[],
    )
