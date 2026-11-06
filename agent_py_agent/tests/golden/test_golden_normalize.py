"""Fast mutation tests independent of the Gateway replay implementation."""
from __future__ import annotations

import copy
import json

import pytest

from .normalize import ID_NAMESPACES, assert_invariants, normalize


# LLM: 日期只在测试根下的完整路径段替换，重复关系与请求因果ID不变；原始哈希仍须先验证。
# 函数用途: 验证跨日回放、日期一一映射、业务正文保留和归一幂等性。
def test_fixture_date_segments_are_bijective_before_body_hashing():
    import hashlib

    def event(first, second):
        body = {"messages": [{"role": "user", "content":
            f"/tmp/fixture/context_bundles/{first}/request.json "
            f"/tmp/fixture/archive/{second}/next.json /tmp/fixture/context_bundles/{first}/again.json"}]}
        return {"source": "provider", "seq": 1, "fixture_root": "/tmp/fixture", "request_id": "request-one",
            "body": body, "body_hash": hashlib.sha256(json.dumps(body, sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}

    raw = event("2026-11-05", "2026-11-06")
    normalized = normalize([raw])
    assert normalized == normalize([event("2027-01-01", "2027-01-02")])
    text = normalized[0]["body"]["messages"][0]["content"]
    assert text.count("date#1/") == 2 and text.count("date#2/") == 1
    assert normalize([event("2026-11-05", "2026-11-05")]) != normalized
    assert normalize(normalized) == normalized
    assert raw["body"]["messages"][0]["content"].count("2026-11-05") == 2
    business = {"source": "provider", "seq": 1, "fixture_root": "/tmp/fixture",
        "text": "appointment 2026-11-05 /business/2026-11-05/a /tmp/fixture/report-2026-11-05.txt"}
    assert normalize([business])[0]["text"] == business["text"].replace("/tmp/fixture", "fixture_root#1")


# LLM: 只有本事件声明的测试根授权日期目录归一，源码发布目录与普通文字必须可区分。
# 函数用途: 保留源码日期、伪根、根名前缀以及无扩展名日期文件的反例。
@pytest.mark.parametrize("roots,path", [
    ({"source_root": "/checkout"}, "/checkout/releases/{day}/config.json"),
    ({}, "fixture_root#1/orders/{day}/item"),
    ({}, "customer_fixture_root#1/orders/{day}/item"),
    ({"fixture_root": "/tmp/fixture"}, "customer_fixture_root#1/orders/{day}/item"),
    ({"fixture_root": "fixture_root#1"}, "customer_fixture_root#1/orders/{day}/item"),
    ({"fixture_root": "/tmp/fixture"}, "/tmp/fixture-other/orders/{day}/item"),
    ({"fixture_root": "/tmp/fixture"}, "/tmp/fixture/exports/{day}"),
])
def test_non_fixture_directory_dates_remain_visible(roots, path):
    first = {"source": "provider", "seq": 1, **roots, "text": path.format(day="2025-01-01")}
    second = {**first, "text": path.format(day="2026-06-09")}
    normalized = normalize([first])
    assert "2025-01-01" in normalized[0]["text"]
    assert normalized != normalize([second])
    assert normalize(normalized) == normalized


# LLM: Two sessions share an owner but not their stop, receipt or execution identity.
# 函数用途: Build an independently specified valid causal trace.
def trace_fixture():
    events = [
        {"kind": "attempt_start", "thread_id": "thread-a", "request_id": "request-a", "execution_attempt_id": "attempt-a", "lease_epoch": 1, "state": "running"},
        {"kind": "effect", "thread_id": "thread-a", "request_id": "request-a", "execution_attempt_id": "attempt-a", "tool_call_id": "tool-a", "receipt_id": "receipt-a", "effect_kind": "outbox", "count": 1},
        {"kind": "stop_ack", "thread_id": "thread-a", "request_id": "request-a", "execution_attempt_id": "attempt-a"},
        {"kind": "exit_proof", "thread_id": "thread-a", "request_id": "request-a", "execution_attempt_id": "attempt-a", "confirmed": True},
        {"kind": "attempt_terminal", "thread_id": "thread-a", "request_id": "request-a", "execution_attempt_id": "attempt-a", "lease_epoch": 1, "state": "cancelled"},
        {"kind": "next_turn", "thread_id": "thread-b", "request_id": "request-b", "execution_attempt_id": "attempt-b", "lease_epoch": 2, "state": "running", "previous_request_id": "request-a", "previous_execution_attempt_id": "attempt-a"},
        {"kind": "effect", "thread_id": "thread-b", "request_id": "request-b", "execution_attempt_id": "attempt-b", "receipt_id": "receipt-b", "effect_kind": "outbox", "count": 1},
        {"kind": "attempt_terminal", "thread_id": "thread-b", "request_id": "request-b", "execution_attempt_id": "attempt-b", "lease_epoch": 2, "state": "done"},
    ]
    return [{"source": "gateway", "seq": i + 1, "timestamp": 100 + i, **event} for i, event in enumerate(events)]


# LLM: An independent recursive renamer changes generated labels but preserves equality relationships.
# 函数用途: Verify namespace bijection, repeated references and input immutability.
def test_consistent_generated_id_renaming():
    original = trace_fixture()
    before = copy.deepcopy(original)
    renamed = []
    for row in original:
        renamed.append({key: "different-" + value if key in ID_NAMESPACES and value else value for key, value in row.items()})
    assert normalize(original) == normalize(renamed)
    assert original == before
    assert normalize(original)[1]["request_id"] == normalize(original)[0]["request_id"]
    assert normalize(original)[6]["receipt_id"] != normalize(original)[1]["receipt_id"]
    assert_invariants(original, {"outbox": 2})


# LLM: Stop/receipt cross-session changes, duplicates and missing proofs cannot normalize away.
# 函数用途: Reject the review's five causal mutation classes and stable incorrect traces.
@pytest.mark.parametrize("mutation", ["stop_other_session", "receipt_other_session", "duplicate_effect", "delete_exit", "old_closeout", "exit_after_next"])
def test_causal_mutations_are_visible(mutation):
    original, mutated = trace_fixture(), trace_fixture()
    if mutation == "stop_other_session":
        mutated[2]["thread_id"] = "thread-b"
    elif mutation == "receipt_other_session":
        mutated[1]["thread_id"] = "thread-b"
    elif mutation == "duplicate_effect":
        mutated.insert(2, copy.deepcopy(mutated[1]))
    elif mutation == "delete_exit":
        del mutated[3]
    elif mutation == "old_closeout":
        mutated.append({**mutated[4], "request_id": "request-b", "execution_attempt_id": "old-attempt", "lease_epoch": 1, "seq": 9})
    else:
        mutated[3], mutated[5] = mutated[5], mutated[3]
    assert normalize(original) != normalize(mutated)
    if mutation != "receipt_other_session":
        with pytest.raises(AssertionError):
            assert_invariants(mutated, {"outbox": 2})


# LLM: Repetition is not an oracle; a stable duplicate effect fails exact counts on every run.
# 函数用途: Prove that three equal wrong traces do not pass the invariant layer.
def test_stable_but_wrong_three_runs_fail():
    trace = trace_fixture()
    trace[1]["effect_kind"] = "forbidden_send"
    runs = [normalize(copy.deepcopy(trace)) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    for run in runs:
        with pytest.raises(AssertionError):
            assert_invariants(run, {"outbox": 2}, ["forbidden_send"])


# LLM: Epochs, counts, states, payload keys and per-source sequence survive normalization.
# 函数用途: Ensure the normal form preserves the complete structural evidence.
def test_normalization_preserves_fields_and_order():
    raw = trace_fixture()
    result = normalize(raw)
    assert len(result) == len(raw)
    for before, after in zip(raw, result, strict=True):
        assert before.keys() == after.keys()
        for field in ("state", "lease_epoch", "count", "seq", "kind"):
            if field in before:
                assert before[field] == after[field]
    assert [event["timestamp"] for event in result if event["thread_id"] == "thread_id#1"] == [1, 2, 3, 4, 5]
    assert [event["timestamp"] for event in result if event["thread_id"] == "thread_id#2"] == [1, 2, 3]
    changed = copy.deepcopy(raw)
    changed[0]["lease_epoch"] += 1
    assert normalize(changed) != result


# LLM: Terminal regressions and simultaneous execution attempts are separate invariant violations.
# 函数用途: Detect state rollback, active-attempt overlap and violated causal edges.
@pytest.mark.parametrize("change", ["regression", "overlap", "edge"])
def test_state_and_happens_before_guards(change):
    trace = trace_fixture()
    if change == "regression":
        trace.append({**trace[0], "seq": 9})
    elif change == "overlap":
        trace.insert(1, {**trace[0], "execution_attempt_id": "attempt-overlap", "seq": 2})
    else:
        trace[0]["happens_after"] = [{"source": "gateway", "seq": 8}]
    with pytest.raises(AssertionError):
        assert_invariants(trace, {"outbox": 2})


# LLM: Neither HTTP 200 nor a mismatched request file proves durable acceptance.
# 函数用途: Check exact receipt predicates independently of Gateway transport.
def test_http_success_alone_is_not_acceptance(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.tests.slo.load import durable_receipt
    paths = SimpleNamespace(inbox=tmp_path / "pending", processing=tmp_path / "processing")
    paths.inbox.mkdir()
    paths.processing.mkdir()
    assert durable_receipt({"request_id": "one", "ok": True}, paths) == ""
    (paths.inbox / "one.json").write_text(json.dumps({"id": "other"}))
    assert durable_receipt({"request_id": "one"}, paths) == ""
    (paths.inbox / "one.json").write_text(json.dumps({"id": "one"}))
    assert durable_receipt({"request_id": "one"}, paths).startswith("file:")
    assert durable_receipt({"request_id": "one", "delivery_status": "accepted", "input_state": "queued", "disposition": "queued"})


# LLM: Fast rejections remain at timeout in the same denominator as accepted samples.
# 函数用途: Prevent success-only percentile and receipt-correlation accounting.
def test_failed_samples_stay_in_denominator(tmp_path):
    from agent_py_agent.tests.slo.load import Sample
    from agent_py_agent.tests.slo.report import build_report, write_report
    accepted = Sample(1, 1000, sent_at=1, accepted_at=1.01, receipt="durable")
    rejected = Sample(1, 1000, sent_at=1, rejected=True, status=503, code="GATEWAY_HTTP_BUSY")
    report = build_report("accounting", {"timeout_ms": 1000}, [accepted, rejected])
    assert report["samples"] == report["denominator"] == 2
    assert report["p99_ms"] == 1000
    assert report["receipt_correlation"] == 0.5
    assert report["failures"] == {"count": 1, "by_reason": {"GATEWAY_HTTP_BUSY": 1}}
    assert json.loads(write_report(tmp_path, report).read_text()) == report


# LLM: A blocked first response cannot stop a later offered arrival from being sent.
# 函数用途: Prove arrival scheduling is open-loop using events instead of timing assumptions.
def test_arrivals_are_independent_of_responses():
    import threading

    from agent_py_agent.tests.slo.load import open_loop
    later = threading.Event()
    # LLM: This callback intentionally waits for the second arrival before returning the first.
    # 函数用途: Detect any accidental closed-loop implementation deterministically.
    def submit(arrival):
        if arrival["index"] == 0:
            assert later.wait(1)
        else:
            later.set()
        return 202, {"request_id": str(arrival["index"])}, "durable"
    samples = open_loop({"timeout_ms": 1000, "arrivals": [{"offset_ms": 0, "index": i} for i in range(2)]}, submit)
    assert len(samples) == 2 and all(sample.receipt for sample in samples)


# LLM: Content digests are not generated identities; two different valid model inputs must stay different.
# 函数用途: Detect request-body mutations even when both hashes are internally consistent.
def test_provider_body_change_cannot_normalize_away():
    import hashlib
    first = {"source": "provider", "seq": 1, "body": {"messages": [{"role": "user", "content": "correct question"}]}}
    second = copy.deepcopy(first)
    second["body"]["messages"][0]["content"] = "wrong question"
    for event in (first, second):
        event["body_hash"] = hashlib.sha256(json.dumps(event["body"], sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert normalize([first]) != normalize([second])
    corrupted = {**first, "body_hash": second["body_hash"]}
    with pytest.raises(AssertionError, match="content/hash"):
        normalize([corrupted])
    assert normalize([{**first, "body": first["body"]}])[0]["body"] == first["body"]


# LLM: Ordinals follow actual chronological values, not JSON key order.
# 函数用途: Preserve an inverted finish/start relation as a visible mutation.
def test_timestamp_relation_reversal_is_visible():
    event = {"source": "queue", "seq": 1, "thread_id": "one", "started_at": 10, "finished_at": 20}
    invalid = {**event, "finished_at": 5}
    assert normalize([event]) != normalize([invalid])
    assert normalize([invalid])[0]["finished_at"] < normalize([invalid])[0]["started_at"]


# LLM: Every stopped attempt remains a pending obligation, even after a later stop in the same scope.
# 函数用途: Ensure a second stop cannot erase the first missing exit proof.
def test_second_stop_does_not_erase_first_obligation():
    first = {"source": "q", "seq": 1, "kind": "stop_ack", "thread_id": "one", "request_id": "r1", "execution_attempt_id": "a1"}
    second = {**first, "seq": 2, "request_id": "r2", "execution_attempt_id": "a2"}
    proof = {**second, "seq": 3, "kind": "exit_proof", "confirmed": True}
    with pytest.raises(AssertionError, match="missing exit proof"):
        assert_invariants([first, second, proof], {})


# LLM: Absence of identity cannot serve as a shared stop/exit correlation key.
# 函数用途: Refuse an unscoped stop followed by an equally unscoped proof.
def test_unscoped_exit_proof_is_not_authority():
    with pytest.raises(AssertionError, match="lifecycle event lacks"):
        assert_invariants([{"source": "q", "seq": 1, "kind": "stop_ack"},
            {"source": "q", "seq": 2, "kind": "exit_proof", "confirmed": True}], {})


# LLM: Clock text metadata keeps range direction while only absolute labels change.
# 函数用途: Detect an inverted rendered date interval without freezing wall time.
def test_rendered_clock_ranges_keep_direction():
    good = {"source": "provider", "seq": 1, "text": "current_week_range: 2026-11-02..2026-11-08"}
    reversed_range = {**good, "text": "current_week_range: 2026-11-08..2026-11-02"}
    assert normalize([good]) != normalize([reversed_range])


# LLM: A content-addressed output is verified from actual bytes and rehashed only after lossless semantic normalization.
# 函数用途: Build an independently addressed tool output for digest and count mutation tests.
def referenced_output_trace(payload):
    import hashlib
    content = json.dumps(payload, indent=2)
    digest = hashlib.sha256(content.encode()).hexdigest()
    body = {"messages": [{"role": "tool", "content": f"output_hash: {digest}\n- output_size_bytes: {len(content.encode())}"}]}
    return [{"source": "provider", "seq": 1, "body": body,
        "body_hash": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "referenced_outputs": [{"sha256": digest, "content": content}]}]


# LLM: Canonical digest replacement cannot hide payload changes, epochs or effect counts.
# 函数用途: Verify referenced content equivalence and detect each semantic mutation.
def test_referenced_output_content_hash_and_counts_are_not_opaque_aliases():
    original = referenced_output_trace({"request_id": "generated-one", "created_at": 100, "count": 1, "lease_epoch": 2})
    renamed = referenced_output_trace({"lease_epoch": 2, "count": 1, "created_at": 200, "request_id": "generated-two"})
    assert normalize(original) == normalize(renamed)
    for payload in ({"request_id": "generated-one", "created_at": 100, "count": 2, "lease_epoch": 2},
                    {"request_id": "generated-one", "created_at": 100, "count": 1, "lease_epoch": 3}):
        assert normalize(referenced_output_trace(payload)) != normalize(original)
    corrupted = copy.deepcopy(original)
    corrupted[0]["referenced_outputs"][0]["content"] += " "
    with pytest.raises(AssertionError, match="referenced output content/hash"):
        normalize(corrupted)


# LLM: An incorrect transport byte count is corruption, not a volatile size to silently replace.
# 函数用途: Reject a footer that does not describe its actual referenced bytes.
def test_referenced_output_size_corruption_is_rejected():
    import hashlib
    trace = referenced_output_trace({"count": 1})
    trace[0]["body"]["messages"][0]["content"] += "0"
    trace[0]["body_hash"] = hashlib.sha256(json.dumps(trace[0]["body"], sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(AssertionError, match="byte count mismatch"):
        normalize(trace)


# LLM: A local elapsed interval keeps its direction and equality rather than freezing hardware-dependent milliseconds.
# 函数用途: Distinguish positive, zero and invalid negative elapsed tool intervals.
def test_tool_elapsed_interval_keeps_direction():
    header = "[tool-result; tool=list_files; status=succeeded; handler_executed=true; duration_ms={}; effect_outcome=confirmed]\nresult"
    positive = provider_message_trace(header.format(15), "tool")
    zero = provider_message_trace(header.format(0), "tool")
    inverted = provider_message_trace(header.format(-15), "tool")
    assert normalize(positive) == normalize(provider_message_trace(header.format(30), "tool"))
    assert normalize(positive) != normalize(zero) != normalize(inverted)


# LLM: Report-only never claims the target passed, while stage mode fails the same verdict without an xfail.
# 函数用途: Verify the Cell baseline/report policy and its non-dilutable performance gate.
def test_report_only_baseline_still_fails_performance_stage(tmp_path):
    from agent_py_agent.tests.slo.report import SLOTargetFailure, apply_report_gate, write_report
    report = {"scenario": "gate-policy", "target_checks": {"acceptance": False, "control": True}, "targets_met": False}
    report["report_path"] = str(write_report(tmp_path, report))
    apply_report_gate(report, enforce=False)
    assert json.loads((tmp_path / "gate-policy.json").read_text())["targets_met"] is False
    with pytest.raises(SLOTargetFailure):
        apply_report_gate(report, enforce=True)
    with pytest.raises(ValueError):
        apply_report_gate({**report, "targets_met": True}, enforce=False)


# LLM: Complete canonical request bodies make content-change tests exercise the actual digest path.
# 函数用途: Construct one small provider message without opaque hash aliases.
def provider_message_trace(content, role="user"):
    import hashlib
    body = {"messages": [{"role": role, "content": content}]}
    wire = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return [{"source": "provider", "seq": 1, "body": body, "body_hash": hashlib.sha256(wire).hexdigest()}]


# LLM: A duration-looking business string is not host execution metadata, even inside a tool payload.
# 函数用途: Preserve duration changes in user content and beneath a genuine tool header.
def test_business_duration_text_is_not_normalized_as_tool_elapsed():
    for role in ("user", "tool"):
        assert normalize(provider_message_trace("set duration_ms=15;", role)) != normalize(provider_message_trace("set duration_ms=30;", role))
    header = "[tool-result; tool=list_files; status=succeeded; handler_executed=true; duration_ms=15; effect_outcome=confirmed]\n"
    assert normalize(provider_message_trace(header + "set duration_ms=15;", "tool")) != normalize(provider_message_trace(header + "set duration_ms=30;", "tool"))


# LLM: Parsing and fixture exceptions return unchanged to the caller, never becoming an expected SLO target failure.
# 函数用途: Reject broken JSON, invalid HTTP protocol and callback assertions in the open-loop driver.
@pytest.mark.parametrize("error_kind", [ValueError, AssertionError])
def test_open_loop_propagates_fixture_errors(error_kind):
    from pathlib import Path

    from agent_py_agent.tests.slo.load import open_loop
    data = json.loads((Path(__file__).parents[1] / "slo" / "slo_targets.json").read_text())
    profile = next(row["load_profile"] for row in data["scenarios"] if row["id"] == "cell50")
    # LLM: The failure occurs inside the actual dispatch thread, not in the calling test.
    # 函数用途: Inject a protocol or fixture failure at the real callback boundary.
    def broken(_arrival):
        raise error_kind("fixture protocol failure")
    with pytest.raises(error_kind, match="fixture protocol failure"):
        open_loop({**profile, "arrivals": profile["arrivals"][:1]}, broken)


# LLM: Multiple physical emissions cannot overwrite the earlier timestamp or be accepted as one forwarded token.
# 函数用途: Retain duplicate evidence and match receipts to the exact model call.
def test_duplicate_emissions_cannot_reduce_forwarding_latency():
    from agent_py_agent.tests.slo.report import correlate_forwarding
    first = {"owner": "fixture-owner", "turn": 1, "offset": 0, "at": 10}
    second = {**first, "turn": 2, "at": 20}
    receipts = {("fixture-owner", 1, 0): 30, ("fixture-owner", 2, 0): 31}
    rows = correlate_forwarding([first, second], receipts, {"fixture-owner"}, 1, 5000)
    assert rows[0]["failure"] == "DUPLICATE_PROVIDER_EMISSION" and rows[0]["latency_ms"] == 5000
    assert len(rows[0]["emissions"]) == 2 and rows[0]["emitted_at"] == 10
    wrong = correlate_forwarding([first], {("fixture-owner", 2, 0): 30}, {"fixture-owner"}, 1, 5000)
    assert wrong[0]["failure"] == "NO_PUBLIC_FRAME_RECEIPT"


# LLM: A provider append during recorder I/O belongs to the next batch, not a skipped cursor interval.
# 函数用途: Reproduce the concurrent-append window without timing-dependent sleeps.
def test_provider_record_cursor_does_not_skip_concurrent_append(tmp_path, monkeypatch):
    from agent_py_agent.tests.slo.fake_provider import FakeProvider

    from .recorder import Recorder
    provider = FakeProvider(tmp_path / "provider")
    provider.base_url = "http://127.0.0.1:1/v1"
    first = {"method": "POST", "path": "/v1/chat/completions", "body": {"messages": []},
        "connection_id": "first", "is_probe": False}
    provider.records.append(first)
    recorder = Recorder(tmp_path / "evidence")
    original = recorder.event
    # LLM: Append while processing the frozen first batch, mirroring another provider thread.
    # 函数用途: Put the second request precisely in the historical lost-update window.
    def append_during_record(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(provider.records) == 1:
            with provider.condition:
                provider.records.append({**first, "connection_id": "second"})
        return result
    monkeypatch.setattr(recorder, "event", append_during_record)
    recorder.provider(provider)
    recorder.provider(provider)
    assert [row["connection_id"] for row in recorder.events] == ["first", "second"]
    assert recorder.provider_offset == len(provider.records)


# LLM: Releasing an earlier scenario phase must not unblock a later recovery phase.
# 函数用途: Prove named provider gates are independent and separately releasable.
def test_provider_phase_releases_do_not_leak(tmp_path):
    from agent_py_agent.tests.slo.fake_provider import FakeProvider
    provider = FakeProvider(tmp_path / "provider")
    provider.release_phase()
    assert provider.phase_gate().is_set() and not provider.phase_gate("recovery").is_set()
    provider.release_phase("recovery")
    assert provider.phase_gate("recovery").is_set()


# LLM: A malformed fixture response cannot hide behind a transport disconnect and a legal xfail.
# 函数用途: Verify producer-thread failures surface in the foreground during teardown.
def test_provider_protocol_errors_reach_foreground(tmp_path):
    import http.client

    from agent_py_agent.tests.slo.fake_provider import FakeProvider
    from agent_py_agent.tests.slo.load import json_request
    with pytest.raises(KeyError, match="arguments"):
        with FakeProvider(tmp_path / "provider", script=[{"tool_calls": [{"function": {}}]}]) as provider:
            with pytest.raises(http.client.RemoteDisconnected):
                json_request(provider.base_url, "/v1/chat/completions", {"messages": [], "stream": False})


# LLM: Even unit-test resource bounds come from the same versioned fixture data.
# 函数用途: Read the shared temporary-server deadlines without hardcoded scenario delays.
def local_fixture_bounds():
    from pathlib import Path
    return json.loads((Path(__file__).parents[1] / "slo" / "slo_targets.json").read_text())["fixture"]


# LLM: Teardown must wait for an in-flight handler before deciding that its error list is complete.
# 函数用途: Reproduce a producer exception that occurs only after close has begun draining.
def test_provider_close_drains_late_handler_error(tmp_path, monkeypatch):
    import http.client
    import threading

    from agent_py_agent.tests.slo.fake_provider import FakeProvider
    from agent_py_agent.tests.slo.load import json_request
    bounds = local_fixture_bounds()
    provider = FakeProvider(tmp_path / "late-provider", teardown_seconds=bounds["teardown_seconds"]).start()
    entered, release, draining, closed = (threading.Event() for _ in range(4))
    errors, client_errors = [], []

    # LLM: This deliberate fixture failure occurs after close has reached its handler-drain barrier.
    # 函数用途: Hold an actual HTTP handler until the foreground releases its late exception.
    def late_failure(_handler):
        entered.set()
        assert release.wait(bounds["condition_seconds"])
        raise ValueError("late fixture protocol error")
    original_wait = provider.condition.wait_for
    # LLM: Observe the actual drain call rather than relying on a fixed sleep or on close merely starting.
    # 函数用途: Signal that teardown is waiting for the still-live request handler.
    def observe_drain(predicate, timeout=None):
        draining.set()
        return original_wait(predicate, timeout)
    monkeypatch.setattr(provider.server.RequestHandlerClass, "serve_post", late_failure)
    monkeypatch.setattr(provider.condition, "wait_for", observe_drain)
    # LLM: Socket shutdown during close is an expected client disconnect, not successful JSON delivery.
    # 函数用途: Run the real client while retaining its exact disconnect exception.
    def client_call():
        try:
            json_request(provider.base_url, "/v1/chat/completions", {"messages": []}, timeout=bounds["condition_seconds"])
        except (OSError, http.client.HTTPException) as error:
            client_errors.append(error)
    # LLM: Close must return the producer error to this foreground-owned thread.
    # 函数用途: Capture the actual teardown result and its completion boundary.
    def close_provider():
        try:
            provider.close()
        except BaseException as error:
            errors.append(error)
        finally:
            closed.set()
    client = threading.Thread(target=client_call, daemon=True)
    closer = threading.Thread(target=close_provider, daemon=True)
    client.start()
    try:
        assert entered.wait(bounds["condition_seconds"])
        closer.start()
        assert draining.wait(bounds["condition_seconds"])
        assert not closed.is_set(), "close returned while the producer could still publish an error"
    finally:
        release.set()
        if closer.ident is None:
            close_provider()
        else:
            closer.join(bounds["teardown_seconds"])
        client.join(bounds["teardown_seconds"])
    assert not closer.is_alive() and not client.is_alive() and not provider.active_requests
    assert len(errors) == 1 and isinstance(errors[0], ValueError) and "late fixture" in str(errors[0])
    assert client_errors and closed.is_set()


# LLM: A real TCP RST before response headers is a normal cancellation in either JSON or SSE mode.
# 函数用途: Ensure cancellation never becomes a producer fixture failure at response-header boundaries.
@pytest.mark.parametrize("stream", [False, True])
def test_provider_accepts_client_reset_before_headers(tmp_path, stream):
    import socket
    import struct
    from urllib.parse import urlsplit

    from agent_py_agent.tests.slo.fake_provider import FakeProvider
    bounds = local_fixture_bounds()
    script = [{"wait_for_release": True, "deadline_seconds": bounds["condition_seconds"]}]
    with FakeProvider(tmp_path / "reset-provider", script=script, teardown_seconds=bounds["teardown_seconds"]) as provider:
        origin = urlsplit(provider.base_url)
        client = socket.create_connection((origin.hostname, origin.port), timeout=bounds["condition_seconds"])
        try:
            wire = json.dumps({"messages": [], "stream": stream}).encode()
            client.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(wire)}\r\n\r\n".encode() + wire)
            with provider.condition:
                assert provider.condition.wait_for(lambda: provider.records and provider.records[0].get("waiting"), bounds["condition_seconds"])
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            client.close()
        provider.release_phase()
        with provider.condition:
            assert provider.condition.wait_for(lambda: not provider.active_requests, bounds["condition_seconds"])
        assert not provider.errors


# LLM: The recorder declares the actual checkout root; relative source paths and artifact contents remain comparable.
# 函数用途: Keep source-template references portable without erasing a different referenced file.
def test_checkout_root_moves_keep_relative_source_identity():
    first = referenced_output_trace({"source_path": "/checkout-one/roles/worker.json"})
    first[0]["source_root"] = "/checkout-one"
    moved = referenced_output_trace({"source_path": "/checkout-two/roles/worker.json"})
    moved[0]["source_root"] = "/checkout-two"
    changed = referenced_output_trace({"source_path": "/checkout-two/roles/researcher.json"})
    changed[0]["source_root"] = "/checkout-two"
    assert normalize(first) == normalize(moved)
    assert normalize(first) != normalize(changed)
