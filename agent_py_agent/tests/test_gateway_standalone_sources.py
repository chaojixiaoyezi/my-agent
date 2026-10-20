# LLM: 只读临时规范源并用原codec验证逆迁移拒绝，不启动Agent或服务、不发布旧代关闭证据。
# 模块用途: 防止Count0绕过MP来源检查，同时保留真实旧单进程和合法空目录。
import json

import pytest

from agent_py_agent.agent.common.nofollow_fs import write_bytes_atomic_beneath
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    generation_metadata_parts,
)
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_sources import (
    ReverseMigrationUnsupported,
    StandaloneSourceUnproven,
    require_standalone_sources,
)
from agent_py_agent.tests.test_gateway_multiprocess_record_semantics import heartbeat, registry


def _check(root):
    with standalone_lifecycle_guard(root) as held:
        require_standalone_sources(gateway_paths_from_root(root), held)


def _write(root, parts, value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    write_bytes_atomic_beneath(root, parts, raw)


def test_new_home_and_accurate_empty_facilities_are_allowed(tmp_path):
    _check(tmp_path)
    for name in ("executors", "generations", "takeover_evidence"):
        (tmp_path / "workers" / name).mkdir(parents=True)
    (tmp_path / "admission" / "reservations").mkdir(parents=True)
    _check(tmp_path)


@pytest.mark.parametrize("status", ["starting", "running", "stopped", "failed", "interrupted", "http_server_error", "killed"])
def test_original_count0_state_without_birth_can_run_but_is_not_migration_proof(tmp_path, status):
    _write(tmp_path, ("gateway_state.json",), {"pid": 1234, "status": status})
    _check(tmp_path)
    assert "process_identity" not in json.loads((tmp_path / "gateway_state.json").read_text())
    assert "standalone_shutdown_observation" not in json.loads((tmp_path / "gateway_state.json").read_text())


@pytest.mark.parametrize("value", [
    [], {}, {"pid": True, "status": "stopped"}, {"pid": 1234, "status": "unknown"},
    {"pid": 1234, "status": "stopped", "worker_count": True},
    {"pid": 1234, "status": "stopped", "worker_count": 2},
    {"pid": 1234, "status": "stopped", "c_run_id": None},
    b'{"pid":1234,"status":"stopped","worker_count":0,"worker_count":2}',
])
def test_unknown_or_mixed_state_refuses_without_repair(tmp_path, value):
    _write(tmp_path, ("gateway_state.json",), value)
    raw = (tmp_path / "gateway_state.json").read_bytes()
    with pytest.raises(StandaloneSourceUnproven):
        _check(tmp_path)
    assert (tmp_path / "gateway_state.json").read_bytes() == raw


@pytest.mark.parametrize("kind", ["state", "registry", "heartbeat", "generation"])
def test_original_mp_facts_refuse_even_if_state_says_stopped(tmp_path, kind):
    if kind == "state":
        parts = ("gateway_state.json",)
        raw = json.dumps({"pid": 1234, "status": "stopped", "worker_count": 2, "c_run_id": "old-c"}).encode()
    elif kind == "registry":
        parts, raw = ("workers", "registry.json"), serialize_record(registry())
    elif kind == "heartbeat":
        parts, raw = ("workers", "w0.json"), serialize_record(heartbeat())
    else:
        parts = generation_metadata_parts("old-c")
        raw = serialize_record({"schema_version": "multiprocess_c_generation.v1", "c_run_id": "old-c", "worker_count": 2})
    _write(tmp_path, parts, raw)
    with pytest.raises(ReverseMigrationUnsupported):
        _check(tmp_path)
    assert tmp_path.joinpath(*parts).read_bytes() == raw


@pytest.mark.parametrize("parts", [
    ("workers", "registry.json"), ("workers", "w0.json"),
    ("workers", "generations", "unknown.json"), ("workers", "unexpected.json"),
    ("admission", "reservations", "unknown.json"),
])
def test_bad_canonical_or_unknown_record_is_not_empty(tmp_path, parts):
    _write(tmp_path, parts, b"{}")
    with pytest.raises(StandaloneSourceUnproven):
        _check(tmp_path)


def test_symlink_worker_namespace_is_hard_refusal(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "workers").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StandaloneSourceUnproven):
        _check(tmp_path)


def test_generation_filename_binding_is_not_ignored(tmp_path):
    raw = serialize_record({"schema_version": "multiprocess_c_generation.v1", "c_run_id": "old-c", "worker_count": 2})
    _write(tmp_path, ("workers", "generations", "a" * 64 + ".json"), raw)
    with pytest.raises(StandaloneSourceUnproven) as failure:
        _check(tmp_path)
    assert not isinstance(failure.value, ReverseMigrationUnsupported)
