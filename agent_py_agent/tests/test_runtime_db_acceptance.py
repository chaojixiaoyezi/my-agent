"""R3 Acceptance 测试（3.txt §5 测试 15/16 + I 节不变量矩阵）。

覆盖：
- 测试 15：契约 schema 无 command/cwd/working_dir；legacy 字段只作
  inert evidence，全程 subprocess spy = 0（I.3/I.4）。
- 测试 16：平台沙箱不可用时 fail closed → UNAVAILABLE（I.9）；
  artifact 缺失 → BLOCKED。
- I.2/I.5/I.6/I.7：compile → 冻结 → CAS 防分叉。
- I.11：无 required assertion 拒绝编译（WORK_DONE ≠ VERIFIED）。
- H.9：validator 只验证内容寻址 immutable snapshot，篡改即失效。
- A.8/§6：ValidatorOperation 追到 attempt_id，code digest/argv/
  stdout/stderr/artifact digest 全留痕。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_py_agent.agent.acceptance import (
    CompiledContract,
    ContractCompileError,
    SnapshotMaterializeError,
    ValidatorEntry,
    compile_acceptance_contract,
    load_artifact_snapshot,
    resolve_validator,
    run_contract_validation,
    run_process_validator,
)
from agent_py_agent.agent.runtime_db.operations import (
    CONTRACT_DIVERGED,
    RuntimeConflictError,
    sha256_of,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.sandbox import SandboxReadiness

OWNER = "local/main"
GOOD_HTML = (
    "<!DOCTYPE html><html><head><title>R3</title></head>"
    "<body><h1>ok</h1></body></html>"
)


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


@pytest.fixture
def chain(repo):
    return repo.record_run_creation(owner_id=OWNER, run_id="run-main", goal="g")


def _compile(repo, chain, proposed):
    return compile_acceptance_contract(
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        proposed=proposed,
    )


def _freeze(repo, chain, contract: CompiledContract) -> None:
    repo.freeze_contract(
        contract_id=contract.contract_id,
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        compiled=contract.compiled,
        digest=contract.digest,
        inert_legacy=contract.inert_legacy,
    )


def _publish_artifact(repo, chain, tmp_path, rel, content):
    """staging → 共享区发布一个 artifact（R2 publish 链），返回 rel。"""
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    src = staging / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content, encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": rel, "kind": "created", "postimage_digest": sha256_of(src)}],
    )
    repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    return shared


def _validated_snapshot(repo, chain, shared):
    records = repo.artifact_records_for_attempt(chain["attempt_id"])
    snap = load_artifact_snapshot(records=records, shared_root=shared)
    assert snap is not None
    return snap


# ------------------------------------------------------------------- 测试 15
def test_contract_schema_no_exec_keys(repo, chain):
    """测试 15：契约 compiled/表 schema 无 command/cwd/working_dir 执行面。"""
    contract = _compile(
        repo,
        chain,
        {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]},
    )
    for key in ("command", "cwd", "working_dir", "executable", "script", "args", "shell"):
        assert key not in contract.compiled, f"compiled 不得含执行字段: {key}"
    # inert_legacy 承载剥离物：证明它们没被丢弃，只是不再可执行（I.3/I.4）。
    legacy = _compile(
        repo,
        chain,
        {
            "command": "echo hi",
            "working_dir": "/tmp",
            "assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}],
        },
    )
    assert legacy.inert_legacy["command"] == "echo hi"
    assert legacy.inert_legacy["working_dir"] == "/tmp"
    assert "command" not in legacy.compiled


def test_legacy_fields_never_touch_subprocess(repo, chain, monkeypatch):
    """测试 15：legacy command 只作 inert evidence，全程 subprocess 调用 = 0。"""
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("不应发生 subprocess 调用")

    monkeypatch.setattr(subprocess, "run", _spy)
    monkeypatch.setattr(subprocess, "Popen", _spy)
    monkeypatch.setattr(subprocess, "call", _spy)
    monkeypatch.setattr(subprocess, "check_call", _spy)
    monkeypatch.setattr(subprocess, "check_output", _spy)
    contract = _compile(
        repo,
        chain,
        {
            "command": "echo hi",
            "cwd": "/tmp",
            "shell": True,
            "assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}],
        },
    )
    _freeze(repo, chain, contract)
    assert calls == []


# ------------------------------------------------------------------ 编译矩阵
def test_compile_empty_contract_rejected(repo, chain):
    """I.11：无 required assertion → 拒绝编译（WORK_DONE ≠ VERIFIED）。"""
    with pytest.raises(ContractCompileError, match="required"):
        _compile(repo, chain, {"assertions": []})
    with pytest.raises(ContractCompileError, match="required"):
        _compile(repo, chain, {})


def test_compile_unknown_ref_rejected(repo, chain):
    """I.7：unknown validator ref → fail closed 拒绝。"""
    with pytest.raises(ContractCompileError, match="unknown validator"):
        _compile(repo, chain, {"assertions": [{"validator": "not_a_real_validator"}]})


def test_compile_dual_input_rejected(repo, chain):
    """顶层 validator 与 assertions 并存 → 协议歧义拒绝。"""
    with pytest.raises(ContractCompileError, match="不得同时"):
        _compile(
            repo,
            chain,
            {
                "validator": "artifact_acceptance",
                "assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}],
            },
        )


def test_compile_required_upgrade(repo, chain):
    """I.5：requiredness 只能升不能降 —— required_by_default 条目标 advisory 也升回。"""
    contract = _compile(
        repo,
        chain,
        {
            "assertions": [
                {"validator": "artifact_acceptance", "artifact_kind": "html", "required": False}
            ]
        },
    )
    assert contract.compiled["assertions"][0]["required"] is True
    assert contract.compiled["advisory_assertions"] == []
    assert contract.compiled["required_artifact_kinds"] == ["html"]


def test_compile_canonical_digest(repo, chain):
    """契约冻结产物带 canonical digest：同 propose 幂等，不同 propose 不同。"""
    a = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    b = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    c = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "md"}]}
    )
    assert a.digest == b.digest
    assert a.digest != c.digest
    assert a.compiled["assertions"][0]["code_digest"]  # §6：validator code digest 入契约
    assert a.compiled["assertions"][0]["validator_kind"] == "pure"


# ------------------------------------------------------------------- I.6 CAS
def test_freeze_contract_cas(repo, chain):
    """I.6：current_contract_id CAS 防分叉 —— 第二份契约冻结 → CONTRACT_DIVERGED。"""
    first = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    _freeze(repo, chain, first)
    assert repo.current_contract(chain["task_run_id"])["contract_id"] == first.contract_id
    second = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "md"}]}
    )
    with pytest.raises(RuntimeConflictError, match=CONTRACT_DIVERGED):
        _freeze(repo, chain, second)
    # 冻结失败后权威仍是第一份。
    assert repo.current_contract(chain["task_run_id"])["contract_id"] == first.contract_id


def test_freeze_idempotent_same_contract(repo, chain):
    """同 contract_id 幂等冻结（重试安全）。"""
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    _freeze(repo, chain, contract)
    _freeze(repo, chain, contract)  # 不抛
    assert repo.current_contract(chain["task_run_id"])["contract_id"] == contract.contract_id


def test_freeze_unknown_task_run(repo, chain):
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    with pytest.raises(RuntimeConflictError, match="task_run 不存在"):
        repo.freeze_contract(
            contract_id=contract.contract_id,
            task_run_id="task_run-zzz",
            attempt_id=chain["attempt_id"],
            compiled=contract.compiled,
            digest=contract.digest,
            inert_legacy=contract.inert_legacy,
        )


# ------------------------------------------------------------------- H.9
def test_snapshot_tamper_invalidated(repo, chain, tmp_path):
    """H.9+G2补：validator 只读内容寻址 store —— store 被篡改 → snapshot 失效；
    live workspace 被篡改 → 不影响（发布时冻结的对象不受 live 再写影响）。"""
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    records = repo.artifact_records_for_attempt(chain["attempt_id"])
    assert load_artifact_snapshot(records=records, shared_root=shared) is not None
    # G2：live 篡改不再使 snapshot 失效（验证源已是 store）。
    (shared / "report.html").write_text("tampered", encoding="utf-8")
    assert load_artifact_snapshot(records=records, shared_root=shared) is not None
    # store 对象被篡改（攻击者直接改 store）→ digest 失配 → snapshot 无效。
    store_file = Path(records[0]["content_path"])
    store_file.write_text("tampered", encoding="utf-8")
    assert load_artifact_snapshot(records=records, shared_root=shared) is None


def test_snapshot_rejects_bad_relpath(repo, chain, tmp_path):
    """防御：rel_path 不合法（绝对路径/.. 段）→ snapshot 无效。"""
    shared = _publish_artifact(repo, chain, tmp_path, "a.txt", "v1")
    records = repo.artifact_records_for_attempt(chain["attempt_id"])
    records[0]["rel_path"] = "../escape.txt"
    assert load_artifact_snapshot(records=records, shared_root=shared) is None


def test_snapshot_materialize_isolates_from_live(repo, chain, tmp_path):
    """H.9+G2补：物化副本与 live workspace 解耦 —— live 篡改既不影响物化
    副本，也不影响 snapshot 本身（源是发布时冻结的 store）。"""
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    snap = _validated_snapshot(repo, chain, shared)
    materialized = snap.materialize(tmp_path / "materialized")
    # live 被篡改 → snapshot 仍有效（G2：读 store），物化副本仍是最初内容。
    (shared / "report.html").write_text("tampered", encoding="utf-8")
    assert load_artifact_snapshot(
        records=repo.artifact_records_for_attempt(chain["attempt_id"]),
        shared_root=shared,
    ) is not None
    assert (materialized.path_for("report.html")).read_text(encoding="utf-8") == GOOD_HTML
    assert (tmp_path / "materialized" / "report.html").read_text(encoding="utf-8") == GOOD_HTML


def test_snapshot_materialize_recomputes_copy_digest(repo, chain, tmp_path):
    """G2（探针 snapshot_metadata_digest_matches_copy 复现）：验证后、物化前
    store 对象被篡改 → materialize 复制的是篡改内容，重算副本 digest 必须检出。

    修复前：load（T1）只验 live 时点 digest，materialize（T2）复制时不重算
    副本 digest —— 篡改发生在 T1 与 T2 之间时，副本与记录不符却静默通过。
    G2 补后验证源是 store；纵深仍在：物化前 store 被改（T1 与 T2 之间）
    → 副本 digest 与记录不符，物化必须抛错（绝不交付未确认副本）。
    """
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    snap = _validated_snapshot(repo, chain, shared)  # T1：验证通过
    # T1 与物化之间 store 对象被篡改（攻击者直接改 store/磁盘损坏）
    records = repo.artifact_records_for_attempt(chain["attempt_id"])
    Path(records[0]["content_path"]).write_text(
        "<div>evil half-written</div>", encoding="utf-8"
    )
    with pytest.raises(SnapshotMaterializeError) as excinfo:
        snap.materialize(tmp_path / "materialized")
    assert "digest" in str(excinfo.value)
    assert excinfo.value.rel_path == "report.html"


def test_runner_blocks_when_materialize_detects_tamper(repo, chain, tmp_path):
    """G2：验证后、物化前 store 对象被篡改 → 全部断言 BLOCKED，绝不 VERIFIED。

    A.8 账本完整性：物化失败也逐条落 operation（BLOCKED），聚合方
    「required 全 VERIFIED 才算过」不受绕过。
    """
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    snap = _validated_snapshot(repo, chain, shared)
    records = repo.artifact_records_for_attempt(chain["attempt_id"])
    Path(records[0]["content_path"]).write_text("<div>evil</div>", encoding="utf-8")
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    _freeze(repo, chain, contract)
    entries = {e.name: e for e in [resolve_validator("artifact_acceptance")]}
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=snap,
        owner_home=tmp_path,
        entries=entries,
        contract_id=contract.contract_id,
    )
    assert [r["status"] for r in results] == ["BLOCKED"]
    assert not any(r["status"] == "VERIFIED" for r in results)


def test_runner_verifies_materialized_copy_when_live_tampered(repo, chain, tmp_path):
    """H.9：runner 只读物化副本 —— 验证后 live 被改也不影响验收结果。

    TOCTOU 修复前：load_artifact_snapshot 校验通过后 runner 直接读 live
    shared root，工具在验证与执行之间改写 live 会让验收读到半成品。
    """
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    snap = _validated_snapshot(repo, chain, shared)
    materialized = snap.materialize(tmp_path / "materialized")
    # 篡改 live（原 snapshot 已失效）：runner 若读 live 就会 FAILED/BLOCKED。
    (shared / "report.html").write_text("<div>half-written</div>", encoding="utf-8")
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    _freeze(repo, chain, contract)
    entries = {e.name: e for e in [resolve_validator("artifact_acceptance")]}
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=materialized,
        owner_home=tmp_path,
        entries=entries,
        contract_id=contract.contract_id,
    )
    assert results[0]["status"] == "VERIFIED"


# ------------------------------------------------------------------- runner
def test_runner_verified_ledger(repo, chain, tmp_path):
    """A.8/§6：required 断言全过 → VERIFIED；operation 账全字段留痕。"""
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    _freeze(repo, chain, contract)
    snap = _validated_snapshot(repo, chain, shared)
    entries = {e.name: e for e in [resolve_validator("artifact_acceptance")]}
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=snap,
        owner_home=tmp_path,
        entries=entries,
        contract_id=contract.contract_id,
    )
    assert len(results) == 1
    assert results[0]["status"] == "VERIFIED"
    op = repo.validator_operation(results[0]["operation_id"])
    assert op["attempt_id"] == chain["attempt_id"]
    assert op["agent_run_id"] == chain["agent_run_id"]
    assert op["contract_id"] == contract.contract_id
    assert op["validator_ref"] == "artifact_acceptance"
    assert op["code_digest"]  # §6：code digest
    assert op["artifact_digests"]  # §6：artifact digest
    assert op["status"] == "VERIFIED"
    assert op["exit_code"] == 0
    # 双重 settle 拒绝（终态不可改写）。
    with pytest.raises(RuntimeConflictError, match="已终态"):
        repo.settle_validator_operation(results[0]["operation_id"], status="FAILED")


def test_runner_failed_rejects_bad_artifact(repo, chain, tmp_path):
    """I.11：内容不合格 → FAILED，stderr 带判读详情。"""
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", "<div>no doc</div>")
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]}
    )
    snap = _validated_snapshot(repo, chain, shared)
    entries = {e.name: e for e in [resolve_validator("artifact_acceptance")]}
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=snap,
        owner_home=tmp_path,
        entries=entries,
        contract_id=contract.contract_id,
    )
    assert results[0]["status"] == "FAILED"
    op = repo.validator_operation(results[0]["operation_id"])
    assert op["stderr_text"] and "rejected" in op["stderr_text"]


def test_runner_no_matching_file_failed(repo, chain, tmp_path):
    """snapshot 无匹配 artifact_kind → FAILED（H.9：不拿 live workspace 兜底）。"""
    shared = _publish_artifact(repo, chain, tmp_path, "report.html", GOOD_HTML)
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "artifact_acceptance", "artifact_kind": "pdf"}]}
    )
    snap = _validated_snapshot(repo, chain, shared)
    entries = {e.name: e for e in [resolve_validator("artifact_acceptance")]}
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=snap,
        owner_home=tmp_path,
        entries=entries,
    )
    assert results[0]["status"] == "FAILED"
    assert "无匹配" in repo.validator_operation(results[0]["operation_id"])["stderr_text"]


# ------------------------------------------------------------------- 测试 16
def test_process_validator_fail_closed_unavailable(repo, chain, tmp_path, monkeypatch):
    """测试 16：平台沙箱不可用 → UNAVAILABLE，绝不退回宿主直跑（fail closed）。"""
    shared = _publish_artifact(repo, chain, tmp_path, "a.txt", "payload")
    entry = ValidatorEntry(
        name="echo_check",
        kind="process",
        version="1",
        code_digest="d" * 64,
        argv_template=("/bin/echo",),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.acceptance.process_validator.validator_platform_ready",
        lambda **kwargs: SandboxReadiness(False, "TEST_NO_SANDBOX", "测试注入不可用"),
    )
    outcome = run_process_validator(
        entry=entry,
        artifact_snapshot=shared / "a.txt",
        owner_home=tmp_path,
    )
    assert outcome.status == "UNAVAILABLE"
    assert "SANDBOX_UNAVAILABLE" in outcome.stderr
    assert outcome.validator_kind == "process"
    assert outcome.code_digest == "d" * 64  # §6 字段齐备


def test_process_validator_blocked_missing_snapshot(repo, chain, tmp_path, monkeypatch):
    """artifact snapshot 不存在 → BLOCKED（执行前阻断，fail closed）。"""
    entry = ValidatorEntry(
        name="echo_check",
        kind="process",
        version="1",
        code_digest="d" * 64,
        argv_template=("/bin/echo",),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.acceptance.process_validator.validator_platform_ready",
        lambda **kwargs: SandboxReadiness(True, "READY", "ready"),
    )
    outcome = run_process_validator(
        entry=entry,
        artifact_snapshot=tmp_path / "missing.txt",
        owner_home=tmp_path,
    )
    assert outcome.status == "BLOCKED"
    assert "不存在" in outcome.stderr


def test_process_validator_kind_mismatch(repo, chain, tmp_path):
    """pure 条目走 process 网关 → BLOCKED（执行面不匹配拒绝）。"""
    entry = resolve_validator("artifact_acceptance")
    outcome = run_process_validator(
        entry=entry,
        artifact_snapshot=tmp_path / "x.txt",
        owner_home=tmp_path,
    )
    assert outcome.status == "BLOCKED"
    assert "不是 process 类型" in outcome.stderr


def test_runner_process_unavailable_settled(repo, chain, tmp_path, monkeypatch):
    """runner 层把 UNAVAILABLE 落账（I.9：required 断言不可用 = 不可交付）。"""
    shared = _publish_artifact(repo, chain, tmp_path, "a.txt", "payload")
    process_entry = ValidatorEntry(
        name="sandboxed_cat",
        kind="process",
        version="1",
        code_digest="e" * 64,
        argv_template=("/bin/cat",),
    )
    from agent_py_agent.agent.acceptance import registry as _registry

    monkeypatch.setitem(_registry.VALIDATOR_REGISTRY, "sandboxed_cat", process_entry)
    contract = _compile(
        repo, chain, {"assertions": [{"validator": "sandboxed_cat", "artifact_kind": "txt"}]}
    )
    snap = _validated_snapshot(repo, chain, shared)
    monkeypatch.setattr(
        "agent_py_agent.agent.acceptance.process_validator.validator_platform_ready",
        lambda **kwargs: SandboxReadiness(False, "TEST_NO_SANDBOX", "测试注入不可用"),
    )
    results = run_contract_validation(
        repo=repo,
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract=contract.compiled,
        snapshot=snap,
        owner_home=tmp_path,
        entries={"sandboxed_cat": process_entry},
        contract_id=contract.contract_id,
    )
    assert results[0]["status"] == "UNAVAILABLE"
    op = repo.validator_operation(results[0]["operation_id"])
    assert op["status"] == "UNAVAILABLE"
    assert op["contract_id"] == contract.contract_id
    assert "SANDBOX_UNAVAILABLE" in op["stderr_text"]
