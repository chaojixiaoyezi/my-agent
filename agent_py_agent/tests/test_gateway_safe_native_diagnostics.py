# LLM: 只用原safe producer写合成行与原diagnostic reader，未知载荷不得回显；无网络、服务、真实日志或权限替换。
# 模块用途: 验证原native错误经当前生命周期日志进入安全诊断，保持有界及无正文边界。
import io
import json
from contextlib import redirect_stderr

import pytest

from agent_py_agent.agent.gateway_parts import logging as log
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
    WorkerSharedResourceError,
)
from agent_py_agent.agent.gateway_parts.multiprocess_kernel_message import KernelMessageError
from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import WorkerCommitRejected
from agent_py_agent.agent.gateway_parts.status_rendering import gateway_log_diagnostics
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    _owner_directories,
    _owner_home_result,
)

SECRET = "SYNTHETIC_PRIVATE_DIAGNOSTIC_BODY_587"
PREFIX = "[gateway-native-diagnostic] "


# LLM: 使用真实producer固定字段，不手造来源授权；异常正文仅合成且必须被生产投影丢弃。
# 函数用途: 在内存捕获原诊断stderr，不运行任何native进程。
def native_line(error=None, *, stage="dispatch", op="materialize", seq=1):
    out = io.StringIO()
    with redirect_stderr(out):
        log.report_native_transport_diagnostic(
            stage,
            error or RuntimeError(SECRET),
            op=op,
            seq=seq,
            request_id=SECRET,
            execution_attempt_id=SECRET,
        )
    value = out.getvalue()
    assert SECRET not in value
    return value


# LLM: 原reader只读单个临时文件；不读取实际Gateway目录。
# 函数用途: 对合成当前生命周期日志取得原安全回执。
def diagnose(tmp_path, text, **kwargs):
    path = tmp_path / "gateway.log"
    path.write_text(text)
    return gateway_log_diagnostics(path, **kwargs)


@pytest.mark.parametrize("field", ["owner_home", "owner_directory_0", "owner_directory_25"])
def test_missing_canonical_owner_resource_is_visible_without_body(tmp_path, field):
    text = native_line(WorkerSharedResourceError("resource_missing", field))
    produced = json.loads(text.removeprefix(PREFIX))
    assert produced["reason"] == "resource_missing" and produced["field"] == field
    result = diagnose(tmp_path, text)
    assert result["status"] == "noise_observed"
    assert result["native_diagnostics"] == [
        {
            "stage": "dispatch",
            "op": "materialize",
            "error_type": "WorkerSharedResourceError",
            "error_code": "WORKER_SHARED_RESOURCE_INVALID",
            "reason": "resource_missing",
            "field": field,
        }
    ]
    assert result["raw_log_included"] is False and SECRET not in json.dumps(result)
    assert all(
        key not in result["native_diagnostics"][0]
        for key in ["request", "attempt", "seq", "owner", "message", "traceback"]
    )


@pytest.mark.parametrize(
    "field",
    ["owner_directory_26", "owner_directory_00", "owner_directory_-1", "owner_seed_0", SECRET],
)
def test_unknown_resource_field_does_not_enter_safe_producer(field):
    row = json.loads(
        native_line(WorkerSharedResourceError("resource_missing", field)).removeprefix(PREFIX)
    )
    assert row["reason"] is None and row["field"] is None


def test_owner_directory_enum_is_exactly_the_original_generator(tmp_path):
    owner = _owner_home_result(tmp_path, OwnerIdentity.local_main(), tmp_path / "owners/local/main")
    directories = _owner_directories(owner)
    assert len(directories) == 26
    for index in range(len(directories)):
        row = json.loads(
            native_line(
                WorkerSharedResourceError("resource_missing", f"owner_directory_{index}")
            ).removeprefix(PREFIX)
        )
        assert row["field"] == f"owner_directory_{index}" and row["reason"] == "resource_missing"


@pytest.mark.parametrize(
    "stage",
    [
        "original_source",
        "receive",
        "authenticate",
        "decode",
        "identity",
        "dispatch",
        "send",
        "worker_future_result",
    ],
)
def test_all_original_native_stages_reach_safe_reader(tmp_path, stage):
    result = diagnose(tmp_path, native_line(stage=stage))
    assert result["status"] == "noise_observed"
    row = result["native_diagnostics"][0]
    assert row["stage"] == stage and row["error_type"] == "RuntimeError"
    assert row["op"] == (
        "materialize" if stage in {"dispatch", "send", "worker_future_result"} else None
    )
    assert row["reason"] is None and row["field"] is None
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "extra",
        "stage",
        "op",
        "seq_bool",
        "seq_negative",
        "error_type",
        "error_code",
        "reason",
        "field",
        "fingerprint_extra",
        "fingerprint_sha",
        "fingerprint_bool",
        "fingerprint_limit",
        "unlinked_identity",
        "code_type",
        "duplicate",
        "oversize",
        "nan",
        "truncated",
    ],
)
def test_native_reader_rejects_unknown_or_unsafe_shapes(tmp_path, change):
    row = json.loads(native_line().removeprefix(PREFIX))
    if change == "schema":
        row["schema_version"] = "unknown"
    elif change == "extra":
        row["body"] = SECRET
    elif change == "stage":
        row["stage"] = SECRET
    elif change == "op":
        row["op"] = SECRET
    elif change == "seq_bool":
        row["seq"] = True
    elif change == "seq_negative":
        row["seq"] = -1
    elif change == "error_type":
        row["error_type"] = SECRET
    elif change == "error_code":
        row["error_code"] = SECRET
    elif change == "reason":
        row["reason"] = SECRET
    elif change == "field":
        row["field"] = SECRET
    elif change == "fingerprint_extra":
        row["request"]["body"] = SECRET
    elif change == "fingerprint_sha":
        row["request"]["sha256"] = SECRET
    elif change == "fingerprint_bool":
        row["request"]["chars"] = True
    elif change == "fingerprint_limit":
        row["request"]["chars"] = 513
    elif change == "unlinked_identity":
        row["stage"] = "decode"
    elif change == "code_type":
        row["error_code"] = []
    text = json.dumps(row)
    if change == "duplicate":
        text = text[:-1] + ',"error_type":"RuntimeError"}'
    elif change == "oversize":
        text += " " * 4096
    elif change == "nan":
        text = text.replace('"seq": 1', '"seq": NaN')
    elif change == "truncated":
        text = text[:-3]
    result = diagnose(tmp_path, PREFIX + text + "\n")
    assert result["native_diagnostics"] == []
    assert SECRET not in json.dumps(result) and result["raw_log_included"] is False


def test_current_lifecycle_tail_and_eight_record_bound_remain(tmp_path):
    old = native_line(stage="decode")
    stages = ["dispatch", "send", "worker_future_result"] * 4
    current = "".join(native_line(stage=stage) for stage in stages)
    result = diagnose(tmp_path, old + current, start_offset_bytes=len(old.encode()))
    assert [row["stage"] for row in result["native_diagnostics"]] == stages[-8:]
    assert result["native_diagnostic_count"] == 12
    assert result["native_diagnostics_omitted"] == 4
    assert result["lifecycle_start_offset_bytes"] == len(old.encode())
    bounded = diagnose(tmp_path, "x" * (70 * 1024) + "\n" + current)
    assert bounded["sampled_bytes"] <= 64 * 1024 and bounded["sample_truncated"] is True
    assert [row["stage"] for row in bounded["native_diagnostics"]] == stages[-8:]


@pytest.mark.parametrize('kind,code,reason,field', [
    ('commit', 'WORKER_COMMIT_REJECTED', 'original_native_admission_required', None),
    ('kernel', 'KERNEL_MESSAGE_TIMEOUT', None, None),
    ('unsafe', 'WORKER_SHARED_RESOURCE_INVALID', 'resource_unsafe_path', 'owner_home'),
])
def test_original_typed_error_categories_still_reach_safe_reader(tmp_path, kind, code, reason, field):
    errors = {'commit': WorkerCommitRejected('original_native_admission_required'),
              'kernel': KernelMessageError('KERNEL_MESSAGE_TIMEOUT', 'dispatch', SECRET),
              'unsafe': WorkerSharedResourceError('resource_unsafe_path', 'owner_home')}
    result = diagnose(tmp_path, native_line(errors[kind]))
    assert result['status'] == 'noise_observed'
    row = result['native_diagnostics'][0]
    assert row['error_code'] == code and row['reason'] == reason and row['field'] == field
    assert SECRET not in json.dumps(result)


def test_original_receive_poll_timeout_stays_silent(tmp_path):
    text = native_line(KernelMessageError('KERNEL_MESSAGE_TIMEOUT', 'receive', SECRET), stage='receive')
    assert text == ''
    result = diagnose(tmp_path, text)
    assert result['status'] == 'no_new_log_bytes'
    assert result['native_diagnostics'] == [] and result['native_diagnostic_count'] == 0
