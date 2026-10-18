"""F3-a 只声明记录结构；字段不授予准入、ready、进程死亡或排空证明。"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args, get_type_hints, is_typeddict

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_record_schema as schema
from agent_py_agent.agent.user_space.home_layout import home_paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert home_paths(str(target)).root == target
    assert target != (Path.home() / ".my-agent").resolve()
    yield
    assert not target.exists()


@pytest.mark.parametrize("name", ["CoordinatorGeneration", "ProcessIdentity", "ExecutorReservation",
    "ExecutorReservationV2", "AdmissionReservation", "AdmissionReservationV2", "TakeoverProof", "TakeoverProofV2",
    "WorkerRegistry", "WorkerHeartbeat", "CancellationRecord"])
def test_declared_version_has_exact_frozen_field_set(name):
    model = getattr(schema, name)
    assert is_typeddict(model)
    annotations = get_type_hints(model)
    version, = get_args(annotations["schema_version"])
    fields = {**schema.RECORD_V1_FIELDS, **schema.RECORD_V2_FIELDS}
    assert version in fields
    assert set(annotations) == set(fields[version]) == set(model.__required_keys__)


def test_v1_and_explicit_v2_are_separate_without_silent_migration():
    assert schema.RECORD_V1_FIELDS["multiprocess_process_identity.v1"] == (
        "schema_version", "host_id", "pid", "start_time")
    assert schema.RECORD_V1_FIELDS["multiprocess_executor_reservation.v1"] == (
        "schema_version", "reservation_id", "kind", "c_run_id", "owner_sha", "request_id",
        "execution_attempt_id", "lease_epoch", "tool_call_id", "state", "process_identity", "pgid", "sid")
    assert schema.RECORD_V2_FIELDS["multiprocess_executor_reservation.v2"] == (
        *schema.RECORD_V1_FIELDS["multiprocess_executor_reservation.v1"], "execution_domain")
    assert not any(key.endswith(".v2") for key in schema.RECORD_V1_FIELDS)
    assert not any(key.endswith(".v1") for key in schema.RECORD_V2_FIELDS)
    assert len(schema.RECORD_V1_FIELDS) == 8
    assert len(schema.RECORD_V2_FIELDS) == 4
    assert schema.RECORD_V2_FIELDS["multiprocess_c_generation.v2"] == (
        *schema.RECORD_V1_FIELDS["multiprocess_c_generation.v1"], "closure")


def test_schema_import_has_no_runtime_bootstrap_or_record_validator_dependency():
    tree = ast.parse(Path(schema.__file__).read_text(encoding="utf-8"))
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert imports == ["__future__", "typing"]
    assert not any(isinstance(node, (ast.Call, ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))


def test_schema_import_exports_no_record_validators():
    # 无源码包仍须检查实际模块导出，不能随AST检查一起只在源码树运行。
    assert not hasattr(schema, "validate_record")
    assert not hasattr(schema, "serialize_record")
