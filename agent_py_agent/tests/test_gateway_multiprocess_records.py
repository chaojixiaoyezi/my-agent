"""P0 记录合同；不启动 Gateway，不调用模型，输入全部为合成身份。"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_records as records


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / 'explicit-pure-record-home').resolve()
    monkeypatch.setenv('MY_AGENT_HOME', str(target))
    assert target != (Path.home() / '.my-agent').resolve()
    yield
    assert not target.exists()


def identity():
    return {"schema_version": "multiprocess_process_identity.v1", "host_id": "synthetic-host",
            "pid": 123, "start_time": 456}


def executor():
    return {"schema_version": "multiprocess_executor_reservation.v1", "reservation_id": "exec-1",
            "kind": "shell", "c_run_id": "c-1", "owner_sha": "a" * 64, "request_id": "req-1",
            "execution_attempt_id": "attempt-1", "lease_epoch": 1,
            "tool_call_id": "call-1", "state": "reserved", "process_identity": None,
            "pgid": None, "sid": None}


# LLM: 这是固定072已发表v1 wire字面夹具，不再调用新认证盖章；旧golden不允许因新字段而重签。
# 函数用途: 返回独立旧格式记录，专门验证兼容解码和编码不迁移。
def admission():
    return {'schema_version': 'multiprocess_admission_reservation.v1',
     'reservation_id': 'admit-1',
     'owner_sha': '6aa303171d82ddd3b6b552301003711405ca49a578d33566a09784984e114a7e',
     'session_scope': {'channel': 'synthetic', 'thread_id': 't-1'},
     'request_id': 'req-1',
     'route': {'worker': 0,
               'owner': ['synthetic', 'user', 'alice'],
               'owner_sha': '6aa303171d82ddd3b6b552301003711405ca49a578d33566a09784984e114a7e',
               'c_run_id': 'c-1',
               'stamped_at': 1,
               'binding': '622f607134165dffa00f4cc7e4c9eebcee4d151b404367cb067ff1bb46c1b0e8'},
     'state': 'occupied',
     'terminal_fact': None}


def proof():
    return {"schema_version": "multiprocess_takeover_proof.v1", "c_run_id": "c-1", "status": "unknown",
            "platform": "darwin", "method": "sysctl_pgid_sid", "covered_scope": {
                "state_scope": "agent_state", "worker_index": 0, "worker_count": 2, "owner_shas": ["a" * 64],
                "executor_reservation_ids": ["exec-1"]}, "unresolved_instances": ["exec-1"],
            "residual_risks": ["escaped_session"]}


def samples():
    return [{"schema_version": "multiprocess_c_generation.v1", "c_run_id": "c-1", "worker_count": 2},
            identity(), executor(), admission(), proof()]


@pytest.mark.parametrize("payload", samples())
def test_legal_v1_record_roundtrips(payload):
    encoded = records.serialize_record(payload)
    expected = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert encoded == expected
    assert records.deserialize_record(encoded) == payload
    assert records.record_digest(payload) == hashlib.sha256(expected).hexdigest()
    assert records.validate_record(payload) == payload


@pytest.mark.parametrize("payload", samples())
@pytest.mark.parametrize("version", [None, "multiprocess_c_generation.v0", "multiprocess_c_generation.v3", True, []])
def test_missing_old_or_unknown_version_is_structured_rejection(payload, version):
    payload = {**payload, "schema_version": version}
    with pytest.raises(records.MultiprocessRecordError) as raised:
        records.validate_record(payload)
    assert raised.value.to_payload()["error_code"] == "MULTIPROCESS_RECORD_INVALID"
    assert raised.value.field == "schema_version"


@pytest.mark.parametrize("birth", [None, "", " ", True, False, -1, 1.5, float("nan"), [], {}])
def test_process_identity_requires_birth(birth):
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**identity(), "start_time": birth})


def test_process_identity_missing_birth_is_not_pid_only():
    payload = identity()
    del payload["start_time"]
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record(payload)


@pytest.mark.parametrize("pid", [0, -1, True, "123", 1.0, None])
def test_process_identity_requires_strict_positive_pid(pid):
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**identity(), "pid": pid})


@pytest.mark.parametrize("field", ["command", "command_argv", "credentials", "api_key", "unexpected"])
def test_executor_record_cannot_persist_command_credentials_or_unknown_fields(field):
    with pytest.raises(records.MultiprocessRecordError):
        records.serialize_record({**executor(), field: "must-not-persist"})


def test_registered_requires_complete_identity_and_process_scope():
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**executor(), "state": "registered"})


def test_reserved_cannot_release_without_registration():
    with pytest.raises(records.MultiprocessRecordError):
        records.advance_executor(executor(), "released")


def test_executor_registration_then_release_preserves_binding():
    source = executor()
    registration = {"process_identity": identity(), "pgid": 123, "sid": 123}
    registered = records.advance_executor(source, "registered", registration)
    assert registered == {**source, **registration, "state": "registered"}
    released = records.advance_executor(registered, "released")
    assert released == {**registered, "state": "released"}
    assert source == executor()


def test_reserved_abandon_retains_incomplete_identity_and_is_not_stopped_proof():
    assert records.advance_executor(executor(), "abandoned") == {**executor(), "state": "abandoned"}


@pytest.mark.parametrize("state", ["released", "abandoned"])
def test_terminal_executor_cannot_restart(state):
    payload = {**executor(), "state": state, "process_identity": identity(), "pgid": 123, "sid": 123}
    with pytest.raises(records.MultiprocessRecordError):
        records.advance_executor(payload, "registered", {"process_identity": identity(), "pgid": 123, "sid": 123})


@pytest.mark.parametrize("terminal", [{}, {"id": "req-1", "ok": True},
                                     {"schema_version": "gateway_terminal_projection_complete.v1", "id": "req-1"}])
def test_admission_does_not_release_for_projection_directory_or_thread_return(terminal):
    with pytest.raises(records.MultiprocessRecordError):
        records.release_admission(admission(), terminal)


def terminal_envelope():
    return {"schema_version": "gateway_terminal_request.v1", "id": "req-1", "status": "done",
            "turn_phase": "closed", "route": admission()["route"],
            "terminal_response": {"id": "req-1", "ok": True, "status": "done"}}


def test_admission_releases_only_for_matching_canonical_envelope():
    terminal = terminal_envelope()
    released = records.release_admission(admission(), terminal)
    assert released["state"] == "released"
    assert released["terminal_fact"] == {"request_id": "req-1", "canonical_ref": "terminal/req-1.json",
                                         "record_sha256": hashlib.sha256(json.dumps(
                                             terminal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                                         ).encode()).hexdigest()}
    assert released["reservation_id"] == "admit-1"
    assert records.validate_record(released) == released


def test_wrong_request_terminal_cannot_release_admission():
    with pytest.raises(records.MultiprocessRecordError):
        records.release_admission(admission(), {**terminal_envelope(), "id": "req-other"})


def test_released_admission_requires_canonical_fact():
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**admission(), "state": "released"})


def test_admission_owner_and_route_are_same_frozen_owner():
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**admission(), "owner_sha": "b" * 64})


def test_unrepresentable_stamp_is_structured_rejection_not_numeric_overflow():
    payload = admission()
    payload["route"]["stamped_at"] = 10 ** 1000
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record(payload)


def test_live_or_unresolved_tree_is_not_stopped():
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**proof(), "status": "stopped"})


def test_scoped_stopped_proof_keeps_platform_residual_risk_explicit():
    payload = {**proof(), "status": "stopped", "unresolved_instances": []}
    assert records.validate_record(payload) == payload


@pytest.mark.parametrize("content", [b'{', b'[]', b'{"schema_version":1,"schema_version":2}', b'NaN', b'\xff'])
def test_invalid_or_ambiguous_json_never_falls_back(content):
    with pytest.raises(records.MultiprocessRecordError):
        records.deserialize_record(content)


def test_v1_digest_is_independent_of_mapping_order_and_current_object_defaults():
    payload = executor()
    snapshot = copy.deepcopy(payload)
    expected = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert records.record_digest(dict(reversed(list(payload.items())))) == expected
    # 新字段必须进入新版本，不能改写旧 v1 的固定字段或混入运行对象默认值。
    assert records.deserialize_record(records.serialize_record(snapshot)) == snapshot
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record({**payload, "future_field": 0})
    assert records.record_digest(snapshot) == expected


def test_nested_executor_cannot_impersonate_process_identity_or_recurse():
    payload = {**executor(), "state": "registered", "pgid": 123, "sid": 123}
    payload["process_identity"] = payload
    with pytest.raises(records.MultiprocessRecordError):
        records.validate_record(payload)


@pytest.mark.parametrize("status", [True, ["done"], {"done": True}, "processing", "pending"])
def test_nonterminal_status_cannot_release_admission(status):
    terminal = terminal_envelope()
    terminal["status"] = terminal["terminal_response"]["status"] = status
    with pytest.raises(records.MultiprocessRecordError):
        records.release_admission(admission(), terminal)




@pytest.mark.parametrize("payload,expected", list(zip(samples(), [
    "4373dc3e1f49306bc0960cfd5a68c226c18d54a47cccad6025c67d144c226d2b",
    "604ecde7e4f8b541bb859af1b37c58b2fdb04f7c0f2542e9796c44d2c7b94627",
    "5a89c749bbec44d9f12f0d86f2c0acc314ef046d6089ed7744bb183156c46777",
    "0c0d95a6d2308b260603c856ed839a10e90defa30a8bcf44c786ae398c4be215",
    "5d59a602f5b87f5166eec98ec51acedc6057c77d2c4d104116fc50ca5538c7e4",
], strict=True)))
def test_v1_golden_digest_is_frozen_when_another_schema_is_added(payload, expected, monkeypatch):
    monkeypatch.setitem(records.RECORD_V1_FIELDS, "future_record.v2", ("schema_version", "future_field"))
    assert records.record_digest(payload) == expected
    assert records.deserialize_record(records.serialize_record(payload)) == payload


@pytest.mark.parametrize("code", ["MULTIPROCESS_RECORD_INVALID", "OWNER_PARTITION_QUARANTINED",
                                  "EXECUTOR_RESERVATION_FAILED", "ADMISSION_RESERVATION_CONFLICT"])
def test_p0_refusals_are_registered_without_automatic_replay(code):
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS

    contract = ERROR_CONTRACTS[code]
    assert contract.code == code
    assert contract.retryable is False
    assert contract.recommended_action == "report_blocker"


def test_explicit_generation_v2_rejects_legacy_shape_without_closure():
    value = {'schema_version': 'multiprocess_c_generation.v2', 'c_run_id': 'old-c', 'worker_count': 1}
    with pytest.raises(records.MultiprocessRecordError) as raised:
        records.validate_record(value)
    assert raised.value.reason == 'invalid_fields' and raised.value.field == 'record'
