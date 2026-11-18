"""PP1118 Round-2 Slice 2: 3.11/3.12 scratch rmtree、并发共享父目录 ENOENT 重试、执行后清理回执与回合脱敏额度拒绝测试。"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import shlex
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import task_credential_material as cred_mat_mod
from agent_py_agent.agent.user_space.task_credential_material import (
    _force_rmtree,
    credential_file_for_child,
    credential_scratch_dir,
    credential_scratch_parent,
    credential_staging_root,
)
from agent_py_agent.agent.user_space.task_credential_storage import CredentialInputError


def _open_fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        count = 0
        for fd in range(256):
            try:
                os.fstat(fd)
                count += 1
            except OSError:
                pass
        return count


def test_force_rmtree_selects_onerror_on_311_and_onexc_on_312(tmp_path, monkeypatch):
    recorded_calls: list[tuple[tuple[int, ...], dict[str, object]]] = []
    real_rmtree = shutil.rmtree

    for fake_ver, expected_key, forbidden_key in (
        ((3, 11, 15), "onerror", "onexc"),
        ((3, 12, 0), "onexc", "onerror"),
    ):
        target_dir = tmp_path / f"dir_{fake_ver[1]}"
        target_dir.mkdir(mode=0o700)
        (target_dir / "leaf.txt").write_text("item", encoding="utf-8")

        def _fake_rmtree(path, **kwargs):
            recorded_calls.append((fake_ver, dict(kwargs)))
            real_rmtree(path)

        monkeypatch.setattr(cred_mat_mod, "sys", SimpleNamespace(version_info=fake_ver))
        monkeypatch.setattr(cred_mat_mod, "shutil", SimpleNamespace(rmtree=_fake_rmtree))

        assert _force_rmtree(target_dir) is True
        assert not target_dir.exists()
        ver_seen, kw_seen = recorded_calls[-1]
        assert ver_seen == fake_ver
        assert expected_key in kw_seen and callable(kw_seen[expected_key])
        assert forbidden_key not in kw_seen


def test_credential_scratch_dir_force_rmtree_removes_readonly_0500_subdir_and_prunes_parent(tmp_path, monkeypatch):
    isolated_tmp = tmp_path / "sys_tmp"
    isolated_tmp.mkdir(mode=0o700)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(mode=0o700)
    outside_readonly = outside_dir / "keep_0400.txt"
    outside_readonly.write_text("immutable", encoding="utf-8")
    outside_readonly.chmod(0o400)

    monkeypatch.setattr(tempfile, "tempdir", str(isolated_tmp))
    tmpdir_before = set(os.listdir(isolated_tmp))
    fd_before = _open_fd_count()

    try:
        with credential_scratch_dir() as (scratch, state):
            assert scratch.is_dir()
            assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
            assert state["scratch_cleaned"] is False

            (scratch / "plain.txt").write_text("ok", encoding="utf-8")

            ro_500 = scratch / "ro_500_subdir"
            ro_500.mkdir(mode=0o700)
            ro_leaf = ro_500 / "nested_0400.txt"
            ro_leaf.write_text("nested", encoding="utf-8")
            ro_leaf.chmod(0o400)
            (ro_500 / "outside_link").symlink_to(outside_readonly)
            ro_500.chmod(0o500)

            ro_000 = scratch / "ro_000_subdir"
            ro_000.mkdir(mode=0o700)
            (ro_000 / "opaque.txt").write_text("opaque", encoding="utf-8")
            ro_000.chmod(0o000)

        assert state["scratch_cleaned"] is True
        assert not scratch.exists()
        assert not credential_scratch_parent().exists()
        assert set(os.listdir(isolated_tmp)) == tmpdir_before
        assert _open_fd_count() - fd_before == 0
        assert stat.S_IMODE(outside_readonly.stat().st_mode) == 0o400
        assert outside_readonly.read_text(encoding="utf-8") == "immutable"
    finally:
        if outside_readonly.exists():
            outside_readonly.chmod(0o600)


def test_concurrent_scratch_and_fd_staging_survive_empty_parent_rmdir_race(tmp_path, monkeypatch):
    # 1. Deterministic Event interleaving for credential_scratch_dir:
    #    Thread 1 holds sole child under shared parent -> Thread 2 verifies shared parent ->
    #    Thread 1 exits and prunes now-empty shared parent via rmdir -> Thread 2 hits ENOENT on mkdtemp and retries cleanly.
    scratch_tmp = tmp_path / "scratch_race_tmp"
    scratch_tmp.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))

    orig_mkdtemp = cred_mat_mod.tempfile.mkdtemp
    t1_entered = threading.Event()
    t1_allow_exit = threading.Event()
    t1_exited = threading.Event()
    t2_at_mkdtemp = threading.Event()
    scratch_enoent_count = 0
    scratch_intercepted = {"done": False}
    errors: list[BaseException] = []
    states: dict[int, dict[str, bool]] = {}

    def _racing_scratch_mkdtemp(*args, **kwargs):
        nonlocal scratch_enoent_count
        if threading.current_thread().name == "scratch-t2" and not scratch_intercepted["done"]:
            scratch_intercepted["done"] = True
            t2_at_mkdtemp.set()
            assert t1_exited.wait(timeout=10)
        try:
            return orig_mkdtemp(*args, **kwargs)
        except FileNotFoundError:
            scratch_enoent_count += 1
            raise

    monkeypatch.setattr(cred_mat_mod.tempfile, "mkdtemp", _racing_scratch_mkdtemp)
    fd_before_scratch = _open_fd_count()

    def _scratch_worker_1() -> None:
        try:
            with credential_scratch_dir() as (s1, st1):
                (s1 / "w1.txt").write_text("one", encoding="utf-8")
                t1_entered.set()
                assert t1_allow_exit.wait(timeout=10)
            states[1] = dict(st1)
        except BaseException as exc:  # pragma: no cover - collected for assertion
            errors.append(exc)
        finally:
            t1_exited.set()

    def _scratch_worker_2() -> None:
        try:
            assert t1_entered.wait(timeout=10)
            with credential_scratch_dir() as (s2, st2):
                assert stat.S_IMODE(s2.stat().st_mode) == 0o700
                (s2 / "w2.txt").write_text("two", encoding="utf-8")
                assert (s2 / "w2.txt").read_text(encoding="utf-8") == "two"
            states[2] = dict(st2)
        except BaseException as exc:  # pragma: no cover - collected for assertion
            errors.append(exc)

    th1 = threading.Thread(target=_scratch_worker_1, name="scratch-t1")
    th2 = threading.Thread(target=_scratch_worker_2, name="scratch-t2")
    th1.start()
    th2.start()
    assert t2_at_mkdtemp.wait(timeout=10)
    t1_allow_exit.set()
    th1.join(timeout=10)
    th2.join(timeout=10)

    assert not errors, errors
    assert scratch_enoent_count == 1
    assert states == {1: {"scratch_cleaned": True}, 2: {"scratch_cleaned": True}}
    assert set(os.listdir(scratch_tmp)) == set()
    assert _open_fd_count() - fd_before_scratch == 0

    # 2. Deterministic Event interleaving for credential_file_for_child shared staging root:
    #    credential_file_for_child prunes stage_dir & stage_root right after unlinking the named file before yield!
    stage_tmp = tmp_path / "stage_race_tmp"
    stage_tmp.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(stage_tmp))

    f1_ready_to_prune = threading.Event()
    f1_allow_prune = threading.Event()
    f1_pruned = threading.Event()
    f2_at_mkdtemp = threading.Event()
    stage_enoent_count = 0
    stage_intercepted = {"t1": False, "t2": False}
    orig_prune = cred_mat_mod._try_prune_empty_dir
    stage_errors: list[BaseException] = []
    stage_payloads: dict[int, dict[str, object]] = {}

    def _racing_stage_prune(path: Path) -> None:
        if (
            threading.current_thread().name == "stage-t1"
            and path == credential_staging_root()
            and not stage_intercepted["t1"]
        ):
            stage_intercepted["t1"] = True
            f1_ready_to_prune.set()
            assert f1_allow_prune.wait(timeout=10)
            orig_prune(path)
            f1_pruned.set()
            return
        orig_prune(path)

    def _racing_stage_mkdtemp(*args, **kwargs):
        nonlocal stage_enoent_count
        if threading.current_thread().name == "stage-t2" and not stage_intercepted["t2"]:
            stage_intercepted["t2"] = True
            f2_at_mkdtemp.set()
            assert f1_pruned.wait(timeout=10)
        try:
            return orig_mkdtemp(*args, **kwargs)
        except FileNotFoundError:
            stage_enoent_count += 1
            raise

    monkeypatch.setattr(cred_mat_mod, "_try_prune_empty_dir", _racing_stage_prune)
    monkeypatch.setattr(cred_mat_mod.tempfile, "mkdtemp", _racing_stage_mkdtemp)

    mat_1 = SimpleNamespace(
        credential_ref="cred_" + "1" * 32,
        origin="https://one.example.test",
        target_url="https://one.example.test/v1",
        values={"secret": "SYNTH_STAGE_ONE"},
    )
    mat_2 = SimpleNamespace(
        credential_ref="cred_" + "2" * 32,
        origin="https://two.example.test",
        target_url="https://two.example.test/v1",
        values={"secret": "SYNTH_STAGE_TWO"},
    )
    fd_before_stage = _open_fd_count()

    def _stage_worker_1() -> None:
        try:
            with credential_file_for_child(mat_1, sandbox_backend="linux") as cf1:
                assert os.fstat(cf1.read_fd).st_nlink == 0
                stage_payloads[1] = json.loads(os.pread(cf1.read_fd, 4096, 0).decode("utf-8"))
            assert cf1.fd_closed is True
        except BaseException as exc:  # pragma: no cover - collected for assertion
            stage_errors.append(exc)

    def _stage_worker_2() -> None:
        try:
            assert f1_ready_to_prune.wait(timeout=10)
            with credential_file_for_child(mat_2, sandbox_backend="linux") as cf2:
                assert os.fstat(cf2.read_fd).st_nlink == 0
                stage_payloads[2] = json.loads(os.pread(cf2.read_fd, 4096, 0).decode("utf-8"))
            assert cf2.fd_closed is True
        except BaseException as exc:  # pragma: no cover - collected for assertion
            stage_errors.append(exc)

    st1 = threading.Thread(target=_stage_worker_1, name="stage-t1")
    st2 = threading.Thread(target=_stage_worker_2, name="stage-t2")
    st1.start()
    st2.start()
    assert f2_at_mkdtemp.wait(timeout=10)
    f1_allow_prune.set()
    st1.join(timeout=10)
    st2.join(timeout=10)

    assert not stage_errors, stage_errors
    # FD 交接与 ExitStack 退出都会裁剪空的共享父目录，并发重试次数至少 1 次（3.11 下可能更多）。
    assert stage_enoent_count >= 1
    assert stage_payloads[1]["values"] == {"secret": "SYNTH_STAGE_ONE"}
    assert stage_payloads[2]["values"] == {"secret": "SYNTH_STAGE_TWO"}
    assert set(os.listdir(stage_tmp)) == set()
    assert _open_fd_count() - fd_before_stage == 0

    # 3. Symlink and ownership/permission errors fail closed immediately without retry.
    monkeypatch.setattr(cred_mat_mod, "_try_prune_empty_dir", orig_prune)
    monkeypatch.setattr(cred_mat_mod.tempfile, "mkdtemp", orig_mkdtemp)
    symlink_tmp = tmp_path / "symlink_tmp"
    symlink_tmp.mkdir(mode=0o700)
    real_elsewhere = tmp_path / "elsewhere"
    real_elsewhere.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(symlink_tmp))
    credential_scratch_parent().symlink_to(real_elsewhere)
    with pytest.raises(OSError):
        with credential_scratch_dir():
            pass
    credential_scratch_parent().unlink()

    uid_calls = 0
    orig_ensure = cred_mat_mod._ensure_private_host_dir

    def _perm_fail(path: Path) -> Path:
        nonlocal uid_calls
        uid_calls += 1
        raise PermissionError("credential private root uid mismatch")

    monkeypatch.setattr(cred_mat_mod, "_ensure_private_host_dir", _perm_fail)
    with pytest.raises(PermissionError, match="uid mismatch"):
        with credential_scratch_dir():
            pass
    assert uid_calls == 1
    monkeypatch.setattr(cred_mat_mod, "_ensure_private_host_dir", orig_ensure)


def test_post_child_cleanup_oserror_preserves_outcome_and_missing_outcome_reports_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import shell as shell_mod
    from agent_py_agent.agent.tooling.host_output_redaction import turn_credential_redaction
    from agent_py_agent.tests.golden.replayer import _shell_python
    from agent_py_agent.tests.test_fc2_credential_shell_boundary import (
        _active,
        _agent,
        _assert_zero_secret_hits,
        _extract_json_report,
        _run_formal_tool,
        _submit_custom_card,
    )

    agent = _agent(tmp_path, "user", full=False)
    _active(agent)
    sec_value = "SYNTHETIC_R2_POST_EXEC_SECRET_9402"
    ref = _submit_custom_card(
        agent,
        "https://cleanup.example.test/v1/action",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": sec_value},
        authentication="bearer",
        collection_mode="password",
    )

    ws_dir = agent.tools.workspace_root
    ws_dir.mkdir(parents=True, exist_ok=True)
    child_script = ws_dir / "child_effect.py"
    child_script.write_text(
        "import hashlib, json, os, sys\n"
        "from pathlib import Path\n"
        "loaded = json.load(open(os.environ['MY_AGENT_CREDENTIAL_FILE'], encoding='utf-8'))\n"
        "sec = loaded['values']['secret']\n"
        "marker = Path(os.environ['TMPDIR']) / 'child_completed.marker'\n"
        "marker.write_text('done', encoding='utf-8')\n"
        "sys.stderr.write(f'echo_check={sec}\\n')\n"
        "print(json.dumps({'effect_marker': marker.is_file(), 'digest': hashlib.sha256(sec.encode()).hexdigest()}))\n",
        encoding="utf-8",
    )

    # 1. Real child runs to completion; single simulated OSError during scratch cleanup exit preserves real outcome
    #    and marks scratch_cleaned=False / cleanup_confirmed=False (never not_started).
    leftover_dirs: list[Path] = []
    real_scratch_cm = cred_mat_mod.credential_scratch_dir

    @contextlib.contextmanager
    def _failing_exit_scratch_dir():
        parent = cred_mat_mod._ensure_private_host_dir(credential_scratch_parent())
        scratch = Path(tempfile.mkdtemp(prefix="run-", dir=os.fspath(parent))).resolve(strict=False)
        os.chmod(scratch, 0o700)
        leftover_dirs.append(scratch)
        state = {"scratch_cleaned": False}
        yield scratch, state
        raise OSError(errno.EIO, "simulated I/O error on scratch cleanup")

    monkeypatch.setattr(cred_mat_mod, "credential_scratch_dir", _failing_exit_scratch_dir)
    try:
        out_cleanup_err = _run_formal_tool(
            agent,
            "run_command",
            {
                "command": f"{shlex.quote(_shell_python())} {shlex.quote(str(child_script))}",
                "credential_ref": ref,
            },
        )
        assert len(leftover_dirs) == 1
        assert (leftover_dirs[0] / "child_completed.marker").is_file()
    finally:
        for leftover in leftover_dirs:
            shutil.rmtree(leftover, ignore_errors=True)
        cred_mat_mod._try_prune_empty_dir(credential_scratch_parent())
        monkeypatch.setattr(cred_mat_mod, "credential_scratch_dir", real_scratch_cm)

    assert out_cleanup_err.ok is True, out_cleanup_err.output
    assert out_cleanup_err.effect_outcome != "not_started"
    report = _extract_json_report(out_cleanup_err.output)
    assert report["effect_marker"] is True
    env_cleanup = out_cleanup_err.metadata.get("handler_details") or out_cleanup_err.metadata
    assert env_cleanup["process"]["status"] == "exited"
    assert env_cleanup["process"]["return_code"] == 0
    cred_exec_1 = env_cleanup["credential_execution"]
    assert cred_exec_1["effect_outcome"] == "succeeded"
    assert cred_exec_1["fd_closed"] is True
    assert cred_exec_1["scratch_cleaned"] is False
    assert cred_exec_1["cleanup_confirmed"] is False
    _assert_zero_secret_hits(out_cleanup_err.output, (sec_value,))
    _assert_zero_secret_hits(out_cleanup_err.metadata, (sec_value,))

    # 2. Post-dispatch outcome is None (both returning None and raising mid-execution):
    #    must report TOOL_OPERATION_OUTCOME_UNKNOWN / effect_outcome='unknown', never 'not_started'.
    monkeypatch.setattr(shell_mod, "_execute_with_artifact_protection", lambda _req: None)
    out_none = _run_formal_tool(
        agent,
        "run_command",
        {"command": "echo noop-1", "credential_ref": ref},
    )
    assert out_none.ok is False
    assert out_none.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert out_none.effect_outcome == "unknown"
    env_none = out_none.metadata.get("handler_details") or out_none.metadata
    assert env_none["process"]["status"] == "unknown"
    assert env_none["credential_execution"]["effect_outcome"] == "unknown"
    assert env_none["credential_execution"]["fd_closed"] is True
    assert env_none["credential_execution"]["scratch_cleaned"] is True
    assert env_none["credential_execution"]["process_tree_reaped"] is False
    assert env_none["credential_execution"]["cleanup_confirmed"] is False

    def _raise_mid_execution(_req):
        raise RuntimeError(f"simulated mid-flight error {sec_value}")

    monkeypatch.setattr(shell_mod, "_execute_with_artifact_protection", _raise_mid_execution)
    out_raised = _run_formal_tool(
        agent,
        "run_command",
        {"command": "echo noop-2", "credential_ref": ref},
    )
    assert out_raised.ok is False
    assert out_raised.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert out_raised.effect_outcome == "unknown"
    env_raised = out_raised.metadata.get("handler_details") or out_raised.metadata
    assert env_raised["process"]["status"] == "unknown"
    assert env_raised["credential_execution"]["effect_outcome"] == "unknown"
    assert env_raised["credential_execution"]["fd_closed"] is True
    assert env_raised["credential_execution"]["scratch_cleaned"] is True
    assert env_raised["credential_execution"]["cleanup_confirmed"] is False
    _assert_zero_secret_hits(out_raised.output, (sec_value,))
    _assert_zero_secret_hits(out_raised.metadata, (sec_value,))

    # Also verify mid-execution raise + failed scratch cleanup reports scratch_cleaned=False.
    leftover_unknown: list[Path] = []

    @contextlib.contextmanager
    def _failing_exit_scratch_unknown():
        parent = cred_mat_mod._ensure_private_host_dir(credential_scratch_parent())
        scratch = Path(tempfile.mkdtemp(prefix="run-", dir=os.fspath(parent))).resolve(strict=False)
        os.chmod(scratch, 0o700)
        leftover_unknown.append(scratch)
        yield scratch, {"scratch_cleaned": False}
        raise OSError(errno.EIO, "simulated scratch cleanup error")

    monkeypatch.setattr(cred_mat_mod, "credential_scratch_dir", _failing_exit_scratch_unknown)
    try:
        out_both_failed = _run_formal_tool(
            agent,
            "run_command",
            {"command": "echo noop-3", "credential_ref": ref},
        )
    finally:
        for leftover in leftover_unknown:
            shutil.rmtree(leftover, ignore_errors=True)
        cred_mat_mod._try_prune_empty_dir(credential_scratch_parent())
        monkeypatch.setattr(cred_mat_mod, "credential_scratch_dir", real_scratch_cm)

    assert out_both_failed.ok is False
    assert out_both_failed.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert out_both_failed.effect_outcome == "unknown"
    env_both = out_both_failed.metadata.get("handler_details") or out_both_failed.metadata
    assert env_both["credential_execution"]["effect_outcome"] == "unknown"
    assert env_both["credential_execution"]["fd_closed"] is True
    assert env_both["credential_execution"]["scratch_cleaned"] is False
    assert env_both["credential_execution"]["cleanup_confirmed"] is False

    turn_credential_redaction(agent, agent._current_run_params).close()


def test_turn_redaction_budget_exhaustion_refuses_before_yield_and_clears_raw_values(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.tooling.host_output_redaction import (
        _MAX_TURN_RAW_BYTES,
        _MAX_TURN_RAW_VALUES_COUNT,
        HostOutputRedaction,
        TurnCredentialRedaction,
        turn_credential_redaction,
    )
    from agent_py_agent.agent.user_space import task_credentials as task_cred_mod
    from agent_py_agent.tests.test_fc2_credential_shell_boundary import (
        _active,
        _agent,
        _assert_zero_secret_hits,
        _run_formal_tool,
        _submit_custom_card,
    )

    agent = _agent(tmp_path, "user", full=False)
    _active(agent)
    overflow_secret = "SYNTHETIC_OVERFLOW_CARD_SECRET_257"
    ref = _submit_custom_card(
        agent,
        "https://budget.example.test/v1",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": overflow_secret},
        authentication="bearer",
        collection_mode="password",
    )

    c1_entered = 0
    c1_cleaned = 0
    orig_c1 = task_cred_mod.authorized_credential_material

    @contextlib.contextmanager
    def _spy_c1(*args, **kwargs):
        nonlocal c1_entered, c1_cleaned
        c1_entered += 1
        try:
            with orig_c1(*args, **kwargs) as mat:
                yield mat
        finally:
            c1_cleaned += 1

    monkeypatch.setattr(task_cred_mod, "authorized_credential_material", _spy_c1)

    # 1. Exhaust the 256-value turn redaction budget, then verify 0 material is yielded and run_command never starts.
    turn_red = turn_credential_redaction(agent, agent._current_run_params)
    turn_red.register_values([f"SYNTHETIC_CAP_FILL_{idx:04d}" for idx in range(_MAX_TURN_RAW_VALUES_COUNT)])

    boundary = write_boundary_with_runtime_ledger(agent, agent._current_run_params)
    cred_use = boundary["_host_output_redaction"]._credential_use.begin_invocation(HostOutputRedaction())
    yielded_count = 0
    try:
        with pytest.raises(CredentialInputError) as exc_info:
            with cred_use(ref, consumer="shell") as _mat:
                yielded_count += 1
        assert yielded_count == 0
        assert exc_info.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert exc_info.value.stage == "runtime_gate"
        assert exc_info.value.effect_outcome == "not_started"
        _assert_zero_secret_hits(str(exc_info.value), (overflow_secret,))
    finally:
        cred_use.disarm()

    marker_file = tmp_path / "should_never_run.txt"
    shell_denied = _run_formal_tool(
        agent,
        "run_command",
        {
            "command": f"echo ran > {shlex.quote(str(marker_file))}",
            "credential_ref": ref,
        },
    )
    assert shell_denied.ok is False
    assert shell_denied.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert shell_denied.failure_stage == "runtime_gate"
    assert shell_denied.effect_outcome == "not_started"
    assert not marker_file.exists()
    _assert_zero_secret_hits(shell_denied.output, (overflow_secret,))
    turn_red.close()

    # 2. Byte-budget exhaustion (> 512 KiB) also refuses before yield with 0 yields.
    byte_turn = TurnCredentialRedaction(owner_key="byte-owner", thread_id="thr-byte", turn_key="turn-byte")
    chunk = "B" * 65_536
    byte_turn.register_values([f"{chunk}_{i}" for i in range((_MAX_TURN_RAW_BYTES // 65_536) + 1)])
    byte_use = cred_mat_mod.HostCredentialUse(
        agent,
        agent._current_run_params,
        turn_redaction=byte_turn,
        redaction=HostOutputRedaction(),
        armed=True,
    )
    try:
        with pytest.raises(CredentialInputError) as byte_exc:
            with byte_use(ref, consumer="shell"):
                yielded_count += 1
        assert yielded_count == 0
        assert byte_exc.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert byte_exc.value.effect_outcome == "not_started"
    finally:
        byte_use.disarm()
        byte_turn.close()

    # 3. Atomic try_register(material) -> None contract (withheld stays False on incumbent scope):
    #    still refuses before yield with CREDENTIAL_INPUT_UNAVAILABLE / not_started and keeps register_values intact.
    try_calls = 0

    class _AtomicRefusalTurnRedaction:
        withheld = False

        def try_register(self, _material: object):
            nonlocal try_calls
            try_calls += 1
            return None

        def register_values(self, values):
            return HostOutputRedaction().with_credential_values(values)

    atomic_use = cred_mat_mod.HostCredentialUse(
        agent,
        agent._current_run_params,
        turn_redaction=_AtomicRefusalTurnRedaction(),
        redaction=HostOutputRedaction(),
        armed=True,
    )
    try:
        with pytest.raises(CredentialInputError) as atomic_exc:
            with atomic_use(ref, consumer="shell"):
                yielded_count += 1
        assert try_calls == 1
        assert yielded_count == 0
        assert c1_entered >= 3 and c1_cleaned == c1_entered
        assert atomic_exc.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
        assert atomic_exc.value.stage == "runtime_gate"
        assert atomic_exc.value.effect_outcome == "not_started"
        # Post-use HostCredentialUse.register_values remains unchanged.
        post_red = atomic_use.register_values(("POST_HTTP_SESSION_TOK",))
        assert "POST_HTTP_SESSION_TOK" in post_red.snapshot.secret_values.credential_values
    finally:
        atomic_use.disarm()
