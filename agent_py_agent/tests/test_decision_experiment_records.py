"""E2 实验对照记录：只观察实验调用后的请求记录条目（身份、基线/候选、原账结算）与回合结束的实际工具用量；
只连本地 HTTP 服务、原设置/原账/原 Gateway 请求文件与精确回合转换锁，不调用真实供应商。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.gateway_parts import request_experiment_records as records
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import CAPABILITY_OBSERVATION_KEY
from agent_py_agent.tests.test_decision_capability_consumer import provider
from agent_py_agent.tests.test_decision_capability_consumer import surface as surface  # noqa: F401
from agent_py_agent.tests.test_decision_capability_http import (
    capability_http as capability_http,  # noqa: F401
)
from agent_py_agent.tests.test_decision_experiment_send_gate import _grant, _recommend, _snapshot
from agent_py_agent.tests.test_decision_experiment_send_gate import lab as lab  # noqa: F401
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)

KEY = records.EXPERIMENT_RECORDS_KEY
_SETTLED = ("status", "reserved_http_requests", "max_http_requests", "charged_input_tokens", "provider_input_tokens",
            "max_input_tokens", "unknown_usage_calls", "input_bound_kind", "input_bound_ratio", "input_bound_warning")


# LLM: 真实原 Gateway 请求文件与 processing 目录，回合转换锁按文件事实裁决；agent 仅供需要 owner 的读路径使用。
# 函数用途: 建立一条 processing 中的请求记录及其宿主上下文。
@pytest.fixture
def gateway_request(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    request = {"id": "req-1", "kind": "ask", "status": "processing", "turn_phase": "open", "execution_attempt_id": "exec-1",
               "prompt": "核对来源"}
    path = paths.processing / "req-1.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return SimpleNamespace(request_path=path, request_id="req-1", request=request, agent=None, paths=paths)


# LLM: 与生产构造器同形的最小条目，只用于写入器/收尾测试；产品评估另有纯函数测试覆盖完整字段。
# 函数用途: 生成一条状态为 observed 的合成实验记录。
def sample(record_id="call-1", **changes):
    entry = {"schema": "decision_experiment_record.v1", "record_id": record_id, "status": "observed", "point": "skill_tool",
             "refs": {"owner_ref": "owner", "thread_id": "thread", "request_id": "req-1"}, "realized": None}
    return {**entry, **changes}


def _stored(fixture):
    stored = json.loads(fixture.request_path.read_text(encoding="utf-8"))
    assert stored.get(KEY) == fixture.request.get(KEY), "文件与内存中的请求记录必须一致"
    return stored


def _finished(**changes):
    return SimpleNamespace(**{"runtime_status": "ok", "turn_end_reason": "completed", "archive_tool_calls": [], **changes})


def test_experiment_call_yields_one_record_whose_settlement_equals_the_ledger(lab):
    authorization = _grant(lab)
    result = _recommend(lab)
    record = result.observation["experiment_record"]
    [call] = model_call_ledger(lab.surface.host).records()
    snapshot = _snapshot(lab, authorization)
    assert record["record_id"] == call.call_id == record["refs"]["call_id"]
    assert {key: record["settlement"][key] for key in _SETTLED} == {key: snapshot[key] for key in _SETTLED}
    assert record["settlement"]["outcome"] == "charged" and record["settlement"]["estimated_input_tokens"] == call.input_tokens
    assert record["settlement"]["input_bound_tokens"] == call.metadata["input_bound"]["tokens"]
    assert (record["status"], record["variant"], record["realized"]) == ("observed", "observe", None)
    assert record["authorization_id"] == authorization["authorization_id"]
    refs, params = record["refs"], lab.surface.params
    assert (refs["request_id"], refs["run_id"], refs["attempt_id"]) == (params.request_id, params.run_id, "capability-attempt")
    assert record["config"]["settings_revision"] == authorization["settings_revision"]
    assert record["config"]["input_bound_policy"] == "empirical:jev_wire_bytes.v1"


def test_record_holds_baseline_and_projected_candidate_names_only(lab):
    _grant(lab)
    record = _recommend(lab).observation["experiment_record"]
    baseline, candidate = record["baseline"], record["candidate"]
    visible = sorted(spec.name for spec in lab.surface.host.tools.model_visible_specs(runtime_snapshot=lab.surface.snapshot))
    assert baseline["presented_names"] == visible and baseline["presented_count"] == len(visible)
    assert baseline["names_truncated"] is False and len(baseline["presented_digest"]) == 64
    assert candidate["status"] == "projected" and candidate["deferred_names"] == ["presentation_optional_b"]
    assert "presentation_optional_a" in candidate["shortlist_names"] and candidate["deferred_count"] == 1
    assert candidate["names_truncated"] is False and candidate["selected_skill_count"] == 1
    text = json.dumps(record, ensure_ascii=False)
    for secret in ("核对来源", "按结构化参数读取", "候选说明", "localhost-decision", "not_needed", lab.http.url):
        assert secret not in text, secret


def test_observation_keeps_its_schema_when_an_experiment_record_is_attached(lab):
    _grant(lab)
    observation = dict(_recommend(lab).observation)
    record = observation.pop("experiment_record")
    assert observation["adopted"] is False and observation["mode"] == "observe" and "shortlist_tool_names" not in observation
    assert record["decision"] == {"status": "success", "reason": "", "question_count": observation["question_count"]}


@pytest.mark.parametrize("limits", [{"max_input_tokens": 1_000}, None])
def test_no_ledger_reservation_means_no_record(lab, limits):
    if limits is not None:
        _grant(lab, **limits)
    result = _recommend(lab)
    assert result.observation is None or "experiment_record" not in result.observation
    assert not model_call_ledger(lab.surface.host).records()


@pytest.mark.parametrize("mode", ["observe", "apply"])
def test_ordinary_decision_never_carries_an_experiment_record(surface, monkeypatch, mode):  # noqa: F811
    from agent_py_agent.tests.test_decision_settings import patch

    patch(surface.host, {"points.skill_tool.mode": mode})
    provider(monkeypatch)
    result = _recommend(SimpleNamespace(surface=surface))
    assert result.observation is not None and "experiment_record" not in result.observation


def test_observer_splits_record_once_and_keeps_observation_entries_unchanged(gateway_request):
    observation = {"schema": "capability_presentation_observation.v1", "point": "skill_tool", "adopted": False}
    for _ in range(2):
        records.observe_capability_presentation(gateway_request, {**observation, "experiment_record": sample()})
    stored = _stored(gateway_request)
    assert stored[CAPABILITY_OBSERVATION_KEY]["entries"] == [observation, observation]
    [entry] = stored[KEY]["entries"]
    assert entry == {**sample(), "execution_attempt_id": "exec-1"} and stored["prompt"] == "核对来源"


def test_writer_bounds_entries_and_plain_observation_writes_no_record_key(gateway_request):
    records.observe_capability_presentation(gateway_request, {"schema": "capability_presentation_observation.v1"})
    assert KEY not in _stored(gateway_request)
    for index in range(10):
        records.record_decision_experiment_sample(gateway_request, sample(f"call-{index}"))
    assert [entry["record_id"] for entry in _stored(gateway_request)[KEY]["entries"]] == [f"call-{i}" for i in range(2, 10)]


@pytest.mark.parametrize("change", [{"cancel_requested": True}, {"turn_phase": "closing"}, {"status": "done"},
                                    {"execution_attempt_id": "exec-other"}])
def test_interrupted_turn_writes_no_partial_record(gateway_request, change):
    stored = _stored(gateway_request)
    gateway_request.request_path.write_text(json.dumps({**stored, **change}), encoding="utf-8")
    with pytest.raises(InterruptedError):
        records.record_decision_experiment_sample(gateway_request, sample())
    assert KEY not in _stored(gateway_request)


def test_writer_swallows_io_failure_without_touching_the_request(gateway_request, monkeypatch):
    def full_disk(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(records, "update_json_file_atomic", full_disk)
    records.record_decision_experiment_sample(gateway_request, sample())
    assert KEY not in _stored(gateway_request)


def test_turn_end_appends_realized_tools_from_the_structured_archive(gateway_request):
    records.record_decision_experiment_sample(gateway_request, sample())
    archive = [{"tool": "read_file", "output": "正文不应进入记录"}, {"tool": "read_file"}, {"tool": "presentation_optional_a"}]
    records.finish_decision_experiment_turn(gateway_request, _finished(archive_tool_calls=archive))
    [entry] = _stored(gateway_request)[KEY]["entries"]
    assert entry["status"] == "completed" and entry["realized"] == {
        "source": "archive_tool_calls", "turn_end_reason": "completed", "known": True, "reason": "",
        "tool_names": ["presentation_optional_a", "read_file"], "tool_count": 2, "names_truncated": False, "call_count": 3}
    records.finish_decision_experiment_turn(gateway_request, _finished(archive_tool_calls=[{"tool": "write_file"}]))
    assert _stored(gateway_request)[KEY]["entries"][0]["realized"]["tool_names"] == ["presentation_optional_a", "read_file"]


@pytest.mark.parametrize("result,reason", [
    (_finished(runtime_status="failed", turn_end_reason=""), "turn_not_completed"),
    (_finished(turn_end_reason="aborted"), "turn_not_completed"),
    (_finished(archive_tool_calls=None), "archive_unavailable"),
    (_finished(archive_tool_calls=[{"tool": ""}]), "archive_unavailable"),
    (_finished(archive_tool_calls=[{"tool": ["read_file"]}]), "archive_unavailable"),
    (_finished(archive_tool_calls=["read_file"]), "archive_unavailable"),
    (_finished(archive_tool_calls=[{"tool": "read_file"}, {"tool": " "}]), "archive_unavailable"),
    (_finished(archive_tool_calls=[{"tool": "read_file"}, {"call_id": "missing-name"}]), "archive_unavailable"),
])
def test_unknown_realized_usage_is_recorded_as_unknown_without_guessing(gateway_request, result, reason):
    records.record_decision_experiment_sample(gateway_request, sample())
    records.finish_decision_experiment_turn(gateway_request, result)
    realized = _stored(gateway_request)[KEY]["entries"][0]["realized"]
    assert realized["known"] is False and realized["reason"] == reason and "tool_names" not in realized


def test_turn_end_only_completes_entries_of_the_current_execution_attempt(gateway_request):
    records.record_decision_experiment_sample(gateway_request, sample("old-call"))
    stored = _stored(gateway_request)
    gateway_request.request_path.write_text(json.dumps({**stored, "execution_attempt_id": "exec-2"}), encoding="utf-8")
    gateway_request.request["execution_attempt_id"] = "exec-2"
    records.record_decision_experiment_sample(gateway_request, sample("new-call"))
    records.finish_decision_experiment_turn(gateway_request, _finished(archive_tool_calls=[{"tool": "read_file"}]))
    old, new = _stored(gateway_request)[KEY]["entries"]
    assert (old["status"], old["realized"]) == ("observed", None) and new["status"] == "completed"


def test_ordinary_turn_end_does_no_io(gateway_request, monkeypatch):
    monkeypatch.setattr(records, "update_json_file_atomic", lambda *_a, **_k: pytest.fail("普通请求不能写实验记录"))
    monkeypatch.setattr(records, "read_json_file_report", lambda *_a, **_k: pytest.fail("普通请求不能读证据链"))
    records.finish_decision_experiment_turn(gateway_request, _finished(archive_tool_calls=[{"tool": "read_file"}]))
    assert KEY not in _stored(gateway_request)


@pytest.mark.parametrize("change", [{"cancel_requested": True}, {"turn_phase": "closing"}])
def test_stopped_turn_end_leaves_the_record_uncompleted_and_does_not_raise(gateway_request, change):
    records.record_decision_experiment_sample(gateway_request, sample())
    stored = _stored(gateway_request)
    gateway_request.request_path.write_text(json.dumps({**stored, **change}), encoding="utf-8")
    records.finish_decision_experiment_turn(gateway_request, _finished(archive_tool_calls=[{"tool": "read_file"}]))
    assert _stored(gateway_request)[KEY]["entries"][0]["status"] == "observed"
