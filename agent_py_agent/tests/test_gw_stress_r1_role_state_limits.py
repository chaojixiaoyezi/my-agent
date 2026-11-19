# LLM: R2（F02/C2）回归：多进程代次角色读者按确切相对路径分流——gateway_state.json 用 1 MiB 兼容读上限，
#   worker marker 维持原 16 KiB；封口重写的 C 字节超出兼容上限时在写 closure 前按 generation_closure_too_large 拒绝。
# 模块用途: 覆盖 generation_role_files、_check_role_marker/require_role_coverage 与 _generation_final_roles 的预算边界。
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import serialize_private_json_no_newline
from agent_py_agent.agent.common.nofollow_fs import ManagedFileReadLimitExceeded
from agent_py_agent.agent.gateway_parts.gateway_state_bounds import GATEWAY_STATE_MAX_BYTES
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import _generation_final_roles
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    generation_role_files,
)
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import (
    _check_role_marker,
    require_role_coverage,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

_C_RUN_ID = "c-run-r2"
# worker marker 的原读上限；C 状态历史原件（MAX2 22,810 B 一类）超过它但在兼容上限内。
_WORKER_LIMIT_BYTES = 16384
_LEGACY_C_BYTES = 23948


def _encode(payload: dict) -> bytes:
    return serialize_private_json_no_newline(payload, sort_keys=True).encode("utf-8")


def _sized(payload: dict, size: int) -> dict:
    base = len(_encode({**payload, "padding": ""}))
    return {**payload, "padding": "x" * (size - base)}


def _write(path, payload: dict) -> bytes:
    raw = _encode(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return raw


def _tree(tmp_path, *, c_bytes: int, pid: int | None = None):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    c_state = {"status": "running", "c_run_id": _C_RUN_ID, "pid": os.getpid() if pid is None else pid}
    c_raw = _write(paths.state, _sized(c_state, c_bytes))
    w_raw = _write(paths.root / "workers" / "w0.json", {"c_run_id": _C_RUN_ID, "worker_index": 0})
    return paths, c_raw, w_raw


def test_generation_role_files_reads_legacy_c_state_but_keeps_worker_budget(tmp_path):
    paths, c_raw, w_raw = _tree(tmp_path, c_bytes=_LEGACY_C_BYTES)
    assert len(c_raw) == _LEGACY_C_BYTES > _WORKER_LIMIT_BYTES
    assert generation_role_files(paths.root, 1) == {"gateway_state.json": c_raw, "workers/w0.json": w_raw}
    _write(paths.root / "workers" / "w0.json", _sized({"c_run_id": _C_RUN_ID}, _WORKER_LIMIT_BYTES + 1))
    with pytest.raises(ManagedFileReadLimitExceeded):
        generation_role_files(paths.root, 1)


@pytest.mark.parametrize("size,ok", [(GATEWAY_STATE_MAX_BYTES, True), (GATEWAY_STATE_MAX_BYTES + 1, False)])
def test_c_state_compat_limit_is_exact_for_both_role_readers(tmp_path, size, ok):
    paths, c_raw, _w_raw = _tree(tmp_path, c_bytes=size)
    assert len(c_raw) == size
    if ok:
        assert generation_role_files(paths.root, 1)["gateway_state.json"] == c_raw
        _check_role_marker(paths.root, ("gateway_state.json",), _C_RUN_ID)
        return
    with pytest.raises(ManagedFileReadLimitExceeded):
        generation_role_files(paths.root, 1)
    with pytest.raises(ManagedFileReadLimitExceeded):
        _check_role_marker(paths.root, ("gateway_state.json",), _C_RUN_ID)


def test_role_coverage_reads_legacy_c_state_and_keeps_identity_checks(tmp_path):
    paths, _c_raw, _w_raw = _tree(tmp_path, c_bytes=_LEGACY_C_BYTES)
    require_role_coverage(paths, _C_RUN_ID)
    # 关闭覆盖复核用 tuple(name.split('/')) 传入，同一判据生效。
    for name in generation_role_files(paths.root, 1):
        _check_role_marker(paths.root, tuple(name.split("/")), _C_RUN_ID, snapshot_retries=2)
    with pytest.raises(ValueError, match="old_role_lifecycle_unproven"):
        require_role_coverage(paths, "c-run-other")
    other, _c_raw, _w_raw = _tree(tmp_path / "other-pid", c_bytes=_LEGACY_C_BYTES, pid=os.getpid() + 1)
    with pytest.raises(ValueError, match="old_role_lifecycle_unproven"):
        require_role_coverage(other, _C_RUN_ID)
    _write(paths.root / "workers" / "w0.json", _sized({"c_run_id": _C_RUN_ID}, _WORKER_LIMIT_BYTES + 1))
    with pytest.raises(ManagedFileReadLimitExceeded):
        require_role_coverage(paths, _C_RUN_ID)


def test_generation_final_roles_refuses_seal_that_outgrows_compat_limit():
    runtime = SimpleNamespace(scope=SimpleNamespace(c_run_id=_C_RUN_ID))
    stopped = {"status": "stopped", "c_run_id": _C_RUN_ID, "pid": os.getpid()}
    w_raw = _encode({"c_run_id": _C_RUN_ID, "worker_index": 0})
    sealed = _generation_final_roles(runtime, {"gateway_state.json": _encode(_sized(stopped, _LEGACY_C_BYTES)),
                                               "workers/w0.json": w_raw})
    state = json.loads(sealed["gateway_state.json"])
    assert state["drain_complete"] is True and state["generation_closure_ref"].startswith("workers/generations/")
    assert sealed["workers/w0.json"] == w_raw
    with pytest.raises(ValueError, match="generation_closure_too_large"):
        _generation_final_roles(runtime, {"gateway_state.json": _encode(_sized(stopped, GATEWAY_STATE_MAX_BYTES)),
                                          "workers/w0.json": w_raw})
