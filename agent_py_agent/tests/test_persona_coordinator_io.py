"""C人格真实文件/原版本算法，fake仅外层同步提交门；不跑网络、模型或native探针。"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import agents_source_publish as publish
from agent_py_agent.agent.capability import persona_body_transaction as body
from agent_py_agent.agent.capability import persona_repository as repository_module
from agent_py_agent.agent.capability import persona_snapshot_transaction as snapshots
from agent_py_agent.agent.capability import persona_storage_io as storage
from agent_py_agent.agent.capability import persona_version_ledger as ledger
from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaConflictError,
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.tests.test_coordinator_jsonl_write_ports import actual_errors


# LLM: 仅fake外层Root门，不替换文件/锁/版本或权限算法；拒绝后禁止再尝试提交，记录每个真实叶持时。
# 类用途: 穷举原事务各出生/末写边界的当前来源失效。
class Door:
    def __init__(self, error=None):
        self.calls = 0
        self.reject_at = None
        self.inside = False
        self.error = error or LocalControlCoordinatorUnavailable("current_source_revoked")
        self.durations = []

    def __call__(self, operation):
        self.calls += 1
        if self.reject_at is not None and self.calls >= self.reject_at:
            raise self.error
        assert not self.inside
        self.inside = True
        started = time.perf_counter()
        try:
            return operation()
        finally:
            self.durations.append(time.perf_counter() - started)
            self.inside = False


# LLM: 实际C Persona仅复用原C quota引用；准备旧正文是测试输入，不补业务提交/账本。
# 函数用途: 创建可选择既有/缺失正文的真实隔离资源。
def persona(root, target="user", *, existed=True, quota=0):
    root.mkdir(parents=True, exist_ok=True)
    if existed:
        (root / (target.upper() + ".md")).write_text("# 原始人格\n", encoding="utf-8")
    enforcer = OwnerQuotaEnforcer(root, max_bytes=quota, coordinator_io=True)
    return PersonaRepository(
        owner_home=root,
        soul_path=root / "SOUL.md",
        user_path=root / "USER.md",
        agents_path=root / "AGENTS.md",
        quota_enforcer=enforcer,
        coordinator_io=True,
    )


# LLM: 原USER/SOUL typed请求继续携confirmed/source_quote，测试不代替原promotion授权。
# 函数用途: 通过原repository提交一条确认的人格事实。
def add(repo, target="user", content="偏好：先核事实", **kwargs):
    return repo.mutate(
        PersonaMutationRequest(
            target=target,
            action="add",
            content=content,
            confirmed=True,
            source_quote="请保存此偏好",
            **kwargs,
        )
    )


@pytest.mark.parametrize("target", ["user", "soul"])
@pytest.mark.parametrize("existed", [False, True])
def test_each_real_birth_and_commit_native_stops_all_later_writes(
    tmp_path, target, existed, record_property
):
    baseline = persona(tmp_path / "baseline", target, existed=existed, quota=1024 * 1024)
    measured = Door()
    with global_write_commit_scope(measured):
        assert add(baseline, target)["version"] == 1
    assert measured.calls > 15
    for offset in range(1, measured.calls + 1):
        repo = persona(tmp_path / f"revoked-{offset}", target, existed=existed, quota=1024 * 1024)
        door = Door()
        door.reject_at = offset
        with global_write_commit_scope(door), pytest.raises(type(door.error)) as caught:
            add(repo, target)
        assert caught.value is door.error
        assert door.calls == offset, "native之后仍尝试清理/恢复提交"
        assert not door.inside
    record_property("real_commit_leaves", measured.calls)
    record_property("largest_gate_seconds", max(measured.durations))
    record_property("body_bytes", len(baseline.path_for(target).read_bytes()))


@pytest.mark.parametrize(
    "error_type",
    [
        GlobalWriteCommitUnavailable,
        GlobalWriteInWorkerError,
        LocalControlCoordinatorUnavailable,
        OwnerManagementIngressUnavailable,
    ],
)
def test_real_canonical_error_unchanged_at_formal_body_and_no_restore(
    tmp_path, monkeypatch, error_type
):
    repo = persona(tmp_path / "owner")
    error = error_type("actual current source rejected")
    original = body.replace_descriptor_text
    attempts = []

    def failed(*args):
        attempts.append(True)
        raise error

    monkeypatch.setattr(body, "replace_descriptor_text", failed)
    door = Door()
    with global_write_commit_scope(door), pytest.raises(error_type) as caught:
        add(repo)
    assert caught.value is error and attempts == [True]
    assert repo.path_for("user").read_text() == "# 原始人格\n"
    monkeypatch.setattr(body, "replace_descriptor_text", original)


@pytest.mark.parametrize("target", ["user", "soul"])
def test_original_single_batch_cas_baseline_external_preimage_rollback(tmp_path, target):
    repo = persona(tmp_path / "owner", target)
    door = Door()
    with global_write_commit_scope(door):
        first = add(repo, target)
        batch = repo.mutate_batch(
            PersonaBatchMutationRequest(
                target,
                (
                    PersonaMutationRequest(
                        target,
                        "replace",
                        content="偏好：新值",
                        entry_id=first["entry_id"],
                        confirmed=True,
                    ),
                    PersonaMutationRequest(target, "add", content="语言：中文", confirmed=True),
                ),
                expected_sha256=first["sha256"],
            )
        )
        assert batch["version"] == 2
        before = repo.path_for(target).read_bytes()
        with pytest.raises(PersonaConflictError):
            add(repo, target, expected_sha256=first["sha256"])
        assert repo.path_for(target).read_bytes() == before
        with pytest.raises(PersonaConflictError):
            repo.mutate_batch(
                PersonaBatchMutationRequest(
                    target,
                    (PersonaMutationRequest(target, "add", content="stale", confirmed=True),),
                    expected_versions_tail="stale",
                )
            )
        rolled = repo.mutate(
            PersonaMutationRequest(target, "rollback", rollback_version=1, confirmed=True)
        )
        assert rolled["version"] == 3
        assert "先核事实" in repo.path_for(target).read_text()
        repo.path_for(target).write_bytes(b"# manual CRLF\r\n")
        add(repo, target, content="手工后事实")
        assert any(
            p.read_bytes() == b"# manual CRLF\r\n"
            for p in (repo.backups_dir / target / "preimages").glob("*.md")
        )
        assert len(repo.history(target)) == 5
    assert not any(callable(x) or x is door for x in vars(repo).values())


def test_missing_scope_expired_and_cross_thread_never_create_files(tmp_path):
    repo = persona(tmp_path / "owner", existed=False)
    before = sorted(repo.owner_home.rglob("*"))
    with pytest.raises(GlobalWriteCommitUnavailable):
        add(repo)
    door = Door()
    with global_write_commit_scope(door):
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(GlobalWriteCommitUnavailable):
                executor.submit(copied.run, lambda: add(repo)).result()
        expired = copy_context()
    with pytest.raises(GlobalWriteCommitUnavailable):
        expired.run(lambda: add(repo))
    assert sorted(repo.owner_home.rglob("*")) == before == []


def test_c_missing_original_quota_rejected_before_any_io(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(ValueError, match="original coordinator quota"):
        PersonaRepository(
            owner_home=root,
            soul_path=root / "SOUL.md",
            user_path=root / "USER.md",
            agents_path=root / "AGENTS.md",
            coordinator_io=True,
        )
    assert not root.exists()


def test_serialization_scan_and_flock_remain_outside_root(tmp_path, monkeypatch):
    import fcntl

    repo = persona(tmp_path / "owner", quota=1024 * 1024)
    door = Door()
    encoded = ledger.encode_version_records
    flock = fcntl.flock

    def encode(records):
        assert not door.inside
        return encoded(records)

    def lock(*args):
        assert not door.inside
        return flock(*args)

    monkeypatch.setattr(ledger, "encode_version_records", encode)
    monkeypatch.setattr(fcntl, "flock", lock)
    with global_write_commit_scope(door):
        add(repo)


@pytest.mark.parametrize("existed", [False, True])
def test_ordinary_stage_failure_original_compensation_restores_body(tmp_path, monkeypatch, existed):
    repo = persona(tmp_path / "owner", existed=existed)
    original = repo.path_for("user").read_bytes() if existed else None

    def failed(*args):
        raise OSError("ordinary stage disk failed")

    monkeypatch.setattr(ledger, "append_encoded_version_records", failed)
    with global_write_commit_scope(Door()), pytest.raises(OSError):
        add(repo)
    assert (
        repo.path_for("user").read_bytes() if repo.path_for("user").exists() else None
    ) == original
    assert not repo.versions_path.exists()
    assert not list((repo.backups_dir / "user").glob("00000001-*.md"))


def test_published_ordinary_failure_keeps_original_body_and_ledger(tmp_path, monkeypatch):
    repo = persona(tmp_path / "owner")
    remove = ledger.remove_private

    def failed(path, **kwargs):
        if path.name == "versions.jsonl":
            raise OSError("ordinary cleanup failure after link")
        return remove(path, **kwargs)

    monkeypatch.setattr(ledger, "remove_private", failed)
    with global_write_commit_scope(Door()), pytest.raises(ledger.PersonaLedgerPublishedError):
        add(repo)
    assert "先核事实" in repo.path_for("user").read_text()
    assert repo.versions_path.exists()


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("error", actual_errors(), ids=lambda e: type(e).__name__)
def test_physical_unknown_same_object_cause_no_compensation(
    tmp_path, monkeypatch, published, error
):
    repo = persona(tmp_path / "owner")
    cause = OSError("original physical exit")

    def fail(*args, **kwargs):
        raise error from cause

    if published:
        monkeypatch.setattr(ledger, "remove_private", fail)
    else:
        monkeypatch.setattr(ledger, "append_encoded_version_records", fail)
    with global_write_commit_scope(Door()), pytest.raises(type(error)) as caught:
        add(repo)
    assert caught.value is error and error.__cause__ is cause
    assert "先核事实" in repo.path_for("user").read_text()
    assert repo.versions_path.exists() is published


def test_snapshot_conflict_keeps_external_identity(tmp_path):
    path = tmp_path / "snapshot.md"
    path.write_bytes(b"external")
    with global_write_commit_scope(Door()), pytest.raises(OSError, match="CONFLICT"):
        snapshots.write_version_snapshot(path, "different", coordinator_io=True)
    assert path.read_bytes() == b"external"


@pytest.mark.parametrize("kind", ["temporary", "prepared"])
def test_native_primary_closes_descriptors_and_leaves_private_material(tmp_path, kind):
    from agent_py_agent.agent.common.nofollow_fs import open_directory_beneath

    root = tmp_path / "owner"
    root.mkdir()
    (root / "persona").mkdir()
    error = LocalControlCoordinatorUnavailable("real source closed")
    door = Door(error)
    opened = []
    with global_write_commit_scope(door), pytest.raises(type(error)) as caught:
        if kind == "temporary":
            with storage.temporary_directory(
                prefix=".private-", dir=root, coordinator_io=True
            ) as directory:
                (Path(directory) / "kept").write_bytes(b"recovery")
                raise error
        else:
            parent = open_directory_beneath(root.anchor, root.parts[1:], require_dir_fd=True)
            try:
                with publish.prepared_default_source(
                    parent, b"body", coordinator_io=True
                ) as prepared:
                    opened.extend([prepared.descriptor, prepared.control])
                    raise error
            finally:
                os.close(parent)
    assert caught.value is error
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert list(root.rglob(".agents-create-*")) or list(root.glob(".private-*"))


def test_normal_prepared_exit_revoke_closes_both_fds(tmp_path):
    from agent_py_agent.agent.common.nofollow_fs import open_directory_beneath

    root = tmp_path / "owner"
    root.mkdir()
    (root / "persona").mkdir()
    parent = open_directory_beneath(root.anchor, root.parts[1:], require_dir_fd=True)
    door = Door()
    fds = []
    try:
        with global_write_commit_scope(door), pytest.raises(type(door.error)) as caught:
            with publish.prepared_default_source(parent, b"body", coordinator_io=True) as prepared:
                fds.extend([prepared.descriptor, prepared.control])
                door.reject_at = door.calls + 1
        assert caught.value is door.error
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(parent)


@pytest.mark.parametrize("phase", ["body", "ledger"])
def test_actual_http_source_revoke_after_body_keeps_first_native_and_closes_fd(
    tmp_path, monkeypatch, phase
):
    from agent_py_agent.agent.gateway_parts.coordinator_client_memory import (
        _assemble_owner_resources,
    )
    from agent_py_agent.tests.test_coordinator_client_memory_source import body as request_body
    from agent_py_agent.tests.test_coordinator_client_memory_source import install
    from agent_py_agent.tests.test_coordinator_model_menu import post_model
    from agent_py_agent.tests.test_coordinator_private_credential_transactions import original_case

    observed_domains = []
    fds = []
    errors = []

    def exercise(source):
        facts = source.http_origin
        with global_write_commit_scope(source.perform):
            domain = _assemble_owner_resources(facts.control, facts.owner)
            observed_domains.append(domain)
            assert domain.authorities.persona.quota_enforcer is domain.long_term.quota_enforcer
            add(domain.authorities.persona)
            replace = body.replace_descriptor_text

            def close_after_write(*args):
                replace(*args)
                fds.append(args[0])
                if phase == "body":
                    facts.runtime._closing = True

            monkeypatch.setattr(body, "replace_descriptor_text", close_after_write)
            if phase == "ledger":
                commit = ledger._publish_ledger_current

                def close_after_ledger(*args, **kwargs):
                    result = commit(*args, **kwargs)
                    facts.runtime._closing = True
                    return result

                monkeypatch.setattr(ledger, "_publish_ledger_current", close_after_ledger)
            try:
                add(domain.authorities.persona, content="partial before actual source revoke")
            except BaseException as error:
                errors.append(error)
                raise

    observed = install(monkeypatch, exercise)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        status, result = post_model(
            server, request_body(), route="/client/memory", actor=row["actor_id"]
        )
        assert status == 503 and result["error_type"] == "LocalControlCoordinatorUnavailable", (
            result
        )
        source = observed[0]
        assert observed[-1] is errors[0] is source._native_source_failure
        assert not source.lifetime.active and not source.lease._commit_pins
        assert (
            "partial before actual source revoke"
            in observed_domains[0].authorities.persona.path_for("user").read_text()
        )
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)


def test_runtime_health_does_not_wash_native_history_birth(tmp_path):
    repo = persona(tmp_path / "owner")
    door = Door()
    door.reject_at = 1
    with global_write_commit_scope(door), pytest.raises(type(door.error)) as caught:
        repo.runtime_snapshot()
    assert caught.value is door.error and door.calls == 1


def test_body_identity_conflict_never_overwrites_external_file(tmp_path, monkeypatch):
    repo = persona(tmp_path / "owner")
    verify = repository_module._verify_persona_commit

    def replace_before_validate(writer, created):
        external = repo.owner_home / "external"
        external.write_bytes(b"external exact owner data")
        os.replace(external, repo.path_for("user"))
        return verify(writer, created)

    monkeypatch.setattr(repository_module, "_verify_persona_commit", replace_before_validate)
    with global_write_commit_scope(Door()), pytest.raises(OSError):
        add(repo)
    assert repo.path_for("user").read_bytes() == b"external exact owner data"
    assert not repo.versions_path.exists()


def test_exclusive_ledger_link_never_adopts_external_ledger(tmp_path, monkeypatch):
    repo = persona(tmp_path / "owner")
    transfer = ledger.transfer

    def occupy_before_link(source, destination, **kwargs):
        if source.name == "versions.jsonl" and destination == repo.versions_path:
            destination.write_bytes(b"external ledger must survive")
        return transfer(source, destination, **kwargs)

    monkeypatch.setattr(ledger, "transfer", occupy_before_link)
    with global_write_commit_scope(Door()), pytest.raises(OSError):
        add(repo)
    assert repo.versions_path.read_bytes() == b"external ledger must survive"
    assert repo.path_for("user").read_text() == "# 原始人格\n"


def test_short_body_write_uses_original_conditional_restore_inside_leaf(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_files as files

    repo = persona(tmp_path / "owner")
    before = repo.path_for("user").read_bytes()
    once = files._write_once
    calls = []
    door = Door()
    target = []
    replace = body.replace_descriptor_text

    def track(descriptor, *args):
        target.append(descriptor)
        return replace(descriptor, *args)

    monkeypatch.setattr(body, "replace_descriptor_text", track)

    def partial(descriptor, raw):
        if not target or descriptor != target[-1]:
            return once(descriptor, raw)
        assert door.inside
        calls.append(True)
        if len(calls) == 1:
            return once(descriptor, raw[: len(before) + 3])
        if len(calls) == 2:
            raise OSError("actual partial OS write")
        return once(descriptor, raw)

    monkeypatch.setattr(files, "_write_once", partial)
    with global_write_commit_scope(door), pytest.raises(OSError):
        add(repo)
    assert len(calls) >= 3 and repo.path_for("user").read_bytes() == before
    assert not repo.versions_path.exists()


def test_prepared_native_primary_survives_close_error_and_still_closes_control(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.common.nofollow_fs import open_directory_beneath

    root = tmp_path / "owner"
    root.mkdir()
    (root / "persona").mkdir()
    parent = open_directory_beneath(root.anchor, root.parts[1:], require_dir_fd=True)
    error = LocalControlCoordinatorUnavailable("original primary")
    close = os.close
    target = []
    closed = []

    def close_with_secondary(fd):
        close(fd)
        closed.append(fd)
        if target and fd == target[0]:
            raise OSError("secondary close error")

    monkeypatch.setattr(os, "close", close_with_secondary)
    try:
        with global_write_commit_scope(Door()), pytest.raises(type(error)) as caught:
            with publish.prepared_default_source(parent, b"body", coordinator_io=True) as prepared:
                target.extend([prepared.descriptor, prepared.control])
                raise error
        assert caught.value is error and all(fd in closed for fd in target)
    finally:
        close(parent)


def test_ledger_fixed_transaction_measurement_is_explicitly_linear(tmp_path, record_property):
    path = tmp_path / "versions.jsonl"
    rows = [{"version": n, "target": "user", "data": "原记录" * 64} for n in range(1000)]
    before = storage.encode_version_records(rows)
    path.write_bytes(before)
    door = Door()

    def forbidden(*_args):
        raise AssertionError("C不得在Root内调用任意旧append callback")

    def validate():
        assert not door.inside

    with global_write_commit_scope(door):
        ledger.append_persona_versions(
            path,
            [{"version": 1001, "target": "user"}],
            forbidden,
            validate=validate,
            coordinator_io=True,
        )
    assert path.read_bytes().startswith(before)
    record_property("records_preimage", len(rows))
    record_property("bytes_preimage", len(before))
    record_property("largest_gate_seconds", max(door.durations))


@pytest.mark.parametrize(
    "operation",
    [
        "load",
        "list_entries",
        "current_sha256",
        "history",
        "snapshot",
        "runtime_snapshot",
        "tail",
        "retained",
    ],
)
def test_read_entry_lock_birth_missing_scope_is_not_downgraded(tmp_path, operation):
    repo = persona(tmp_path / "owner")
    before = sorted(p.relative_to(repo.owner_home).as_posix() for p in repo.owner_home.rglob("*"))
    with pytest.raises(GlobalWriteCommitUnavailable):
        if operation == "tail":
            repository_module.versions_ledger_tail(repo.versions_path, coordinator_io=True)
        elif operation == "retained":
            ledger.retained_ledger_bytes(repo.versions_path, coordinator_io=True)
        elif operation in {"snapshot", "runtime_snapshot"}:
            getattr(repo, operation)()
        else:
            getattr(repo, operation)("user")
    assert (
        sorted(p.relative_to(repo.owner_home).as_posix() for p in repo.owner_home.rglob("*"))
        == before
    )


def test_default_false_never_consumes_global_scope_guard(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import global_write_commit
    from agent_py_agent.tests.test_persona_repository import _repository

    def forbidden(*_args):
        pytest.fail("旧默认人格路径不得新增C guard")

    monkeypatch.setattr(global_write_commit, "require_global_writer", forbidden)
    repo = _repository(tmp_path / "default-owner")
    assert add(repo)["version"] == 1
    assert repo.history("user")[-1]["version"] == 1


@pytest.mark.parametrize("phase", ["body_restore", "snapshot_rollback"])
def test_first_native_during_ordinary_compensation_stops_later_writes(tmp_path, monkeypatch, phase):
    repo = persona(tmp_path / "owner")
    door = Door()
    original_restore = body._BodyWriter.restore
    original_withdraw = snapshots._withdraw_snapshot
    attempted = []

    def ordinary_failure(*_args):
        raise OSError("ordinary stage fault before compensation")

    def restore(writer):
        if phase == "body_restore":
            door.reject_at = door.calls + 1
        result = original_restore(writer)
        if phase == "snapshot_rollback":
            raise OSError("ordinary restore failure still requires snapshot cleanup")
        return result

    def withdraw(*args, **kwargs):
        attempted.append(True)
        door.reject_at = door.calls + 1
        return original_withdraw(*args, **kwargs)

    monkeypatch.setattr(ledger, "append_encoded_version_records", ordinary_failure)
    monkeypatch.setattr(body._BodyWriter, "restore", restore)
    monkeypatch.setattr(snapshots, "_withdraw_snapshot", withdraw)
    with global_write_commit_scope(door), pytest.raises(type(door.error)) as caught:
        add(repo)
    assert caught.value is door.error and door.calls == door.reject_at
    assert len(attempted) == (0 if phase == "body_restore" else 1)


def test_native_snapshot_exit_closes_all_original_fds_despite_secondary_close(
    tmp_path, monkeypatch
):
    error = LocalControlCoordinatorUnavailable("snapshot primary must survive")
    close = os.close
    fds = []
    closed = []

    def secondary(fd):
        close(fd)
        closed.append(fd)
        if fds and fd == fds[0]:
            raise OSError("secondary snapshot FD close")

    with global_write_commit_scope(Door()), pytest.raises(type(error)) as caught:
        with snapshots.snapshot_transaction(coordinator_io=True) as created:
            snapshots.write_version_snapshot(tmp_path / "first.md", "first", coordinator_io=True)
            snapshots.write_version_snapshot(tmp_path / "second.md", "second", coordinator_io=True)
            fds.extend(row.descriptor for row in created)
            monkeypatch.setattr(os, "close", secondary)
            raise error
    assert caught.value is error and all(fd in closed for fd in fds)
    assert snapshots._ACTIVE_SNAPSHOTS.get() is None
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
