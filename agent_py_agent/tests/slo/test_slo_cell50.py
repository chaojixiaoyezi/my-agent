"""Fifty authenticated owners with open-loop durable acceptance and a saturated noisy owner."""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from agent_py_agent.agent.session_lock.store import SessionLockStore

from .load import Sample, durable_receipt, json_request, open_loop, wait_until
from .report import apply_report_gate, correlate_forwarding, percentile


# LLM: Every offered owner remains in the denominator; noisy requests use their own identity and quota.
# 函数用途: Measure isolated acceptance, stop acknowledgement, terminal exit evidence and stream observations.
@pytest.mark.slo
@pytest.mark.parametrize("isolated_gateway", ["cell50"], indirect=True)
def test_cell50(isolated_gateway, request, fake_provider, slo_data, slo_report):
    base, home, _proc = isolated_gateway
    gateway = request.node._slo_gateway
    paths = gateway.paths()
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == "cell50")
    profile, bounds = scenario["load_profile"], slo_data["fixture"]
    stream_owners = {row["owner"] for row in profile["arrivals"] if row["kind"] == "stream"}
    stream_tags = {row["owner"]: row["correlation_tag"] for row in profile["arrivals"] if row["kind"] == "stream"}
    observers, observer_errors, observer_fatals, frame_receipts = [], [], [], {}
    stop_observers = threading.Event()
    token_count = scenario["provider"]["long_stream_length"]
    locks = SessionLockStore(home / "session_lock" / "private_chat_locks.db")
    owners = [row["owner"] for row in profile["arrivals"]] + [profile["baseline_owner"], "noisy-owner"]
    for owner in owners:
        locks.set_password_hash(owner, "synthetic-fixture-hash")
        locks.record_activity(owner)

    # LLM: Only real public progress frames count as forwarded tokens; provider and client share this process's clock.
    # 函数用途: Correlate each synthetic token with its first authenticated client observation.
    def observe_stream(request_id, owner):
        cursor, text = 0, ""
        deadline = time.monotonic() + bounds["condition_seconds"]
        try:
            while not stop_observers.is_set() and time.monotonic() < deadline:
                status, body = json_request(base,
                    f"/progress/{request_id}?since={cursor}&include=assistant_text_delta,assistant_text_reset,assistant_text_done",
                    headers=gateway.headers(owner, "web"), timeout=bounds["probe_seconds"])
                received_at = time.monotonic()
                if status != 200:
                    observer_errors.append({"owner": owner, "status": status})
                    return
                cursor = body["next"]
                for event in body["events"]:
                    if event["kind"] == "assistant_text_reset":
                        text = ""
                    elif event["kind"] == "assistant_text_delta":
                        text += event["text"]
                for match in re.finditer(r"<fixture-stream:" + re.escape(stream_tags[owner]) + r":(\d+):(\d+)>", text):
                    frame_receipts.setdefault((owner, int(match.group(1)), int(match.group(2))), received_at)
                calls = {turn for candidate, turn, _offset in frame_receipts if candidate == owner}
                if any(all((owner, turn, index) in frame_receipts for index in range(token_count)) for turn in calls):
                    return
                stop_observers.wait(bounds["poll_seconds"])
        except (OSError, http.client.IncompleteRead) as error:
            observer_errors.append({"owner": owner, "error": type(error).__name__})
        except BaseException as error:
            observer_fatals.append(error)

    # LLM: Arrival identity and correlation ids are fixed before the transport call, with no retry.
    # 函数用途: Submit one synthetic conversation using real host authentication and receipt checks.
    def submit(arrival):
        owner, kind = arrival["owner"], arrival.get("kind", "qa")
        tag = arrival.get("correlation_tag", arrival["message_id"])
        status, body = json_request(base, "/ask", {"prompt": f"fixture-owner={owner};fixture-kind={kind};fixture-tag={tag}; fixture-question",
            "conversation_id": arrival.get("conversation_id", owner), "metadata": {"message_id": arrival["message_id"]},
            "delivery_intent": "queue", "verbose_level": "stream" if kind == "stream" else "off"},
            headers=gateway.headers(owner, "web"), timeout=profile["timeout_ms"] / 1000)
        receipt = durable_receipt(body, paths)
        if receipt and owner in stream_owners:
            thread = threading.Thread(target=observe_stream, args=(body["request_id"], owner), daemon=True)
            observers.append(thread)
            thread.start()
        return status, body, receipt
    baseline_profile = {**profile, "arrivals": [{**arrival, "owner": profile["baseline_owner"],
        "message_id": f"baseline-{arrival['message_id']}",
        "correlation_tag": f"baseline-{arrival.get('correlation_tag', arrival['message_id'])}"} for arrival in profile["arrivals"]]}
    baseline = open_loop(baseline_profile, submit)
    # Completed baseline turns avoid measuring a supposedly idle baseline while previous work still runs.
    for sample in baseline:
        if sample.request_id:
            wait_until(lambda sample=sample: (paths.terminal / f"{sample.request_id}.json").exists(), bounds["condition_seconds"], bounds["poll_seconds"])
    before_noise = sum(not row["is_probe"] for row in fake_provider.records)
    fake_provider.script.extend({} for _ in range(before_noise - len(fake_provider.script)))
    fake_provider.script.extend({"wait_for_release": True, "deadline_seconds": profile["noisy_hold_seconds"]}
                                for _ in range(profile["quota"]))
    noisy = []
    for index in range(profile["noisy_requests"]):
        noisy.append(submit({"owner": "noisy-owner", "kind": "stream", "message_id": f"noise-{index}",
                             "conversation_id": f"noise-session-{index}"}))
    wait_until(lambda: sum(not row["is_probe"] for row in fake_provider.records) >= before_noise + profile["quota"],
               bounds["condition_seconds"], bounds["poll_seconds"])
    samples = open_loop(profile, submit)
    stop_sample = Sample(time.monotonic(), profile["timeout_ms"], kind="stop", owner="noisy-owner")
    stop_sample.sent_at = time.monotonic()
    stop_status, stop_response = 0, {}
    try:
        stop_status, stop_response = json_request(base, "/control", {"command": "/stop", "conversation_id": "noise-session-0",
            "metadata": {"message_id": "stop-noisy-owner"}}, headers=gateway.headers("noisy-owner", "web"),
            timeout=profile["timeout_ms"] / 1000)
        if stop_status in {200, 202} and stop_response.get("delivery_status") == "accepted":
            stop_sample.accepted_at, stop_sample.receipt = time.monotonic(), "durable_control_disposition"
        else:
            stop_sample.unconfirmed, stop_sample.code = True, "CONTROL_RECEIPT_UNCONFIRMED"
    except (OSError, http.client.HTTPException) as error:
        stop_sample.unconfirmed, stop_sample.code = True, type(error).__name__
    finally:
        fake_provider.release.set()
    stop_ms = stop_sample.latency_ms()
    terminals, forwarding, tools = [], [], []
    accepted = [sample for sample in samples if sample.accepted_at is not None]
    try:
        wait_until(lambda: all((paths.terminal / f"{sample.request_id}.json").exists() for sample in accepted),
                   bounds["condition_seconds"], bounds["poll_seconds"])
    except AssertionError:
        pass  # The report retains every incomplete sample and the final completion assertion fails.
    for sample in accepted:
        terminal_path = paths.terminal / f"{sample.request_id}.json"
        if not terminal_path.exists():
            continue
        sample.completed = True
        terminal = json.loads(terminal_path.read_text())
        terminals.append({"request_id": sample.request_id, "status": terminal.get("status"),
                          "capability_closeout": terminal.get("capability_closeout")})
        if sample.kind == "tool":
            for directory in (paths.done, paths.failed, paths.processing):
                chunk = directory / f"{sample.request_id}.chunks.jsonl"
                if chunk.exists():
                    tools.extend({"owner": sample.owner, **row["progress"]} for row in map(json.loads, chunk.read_text().splitlines())
                                 if row.get("kind") == "tool_progress" and row.get("progress", {}).get("phase") == "finished")
                    break
    for thread in observers:
        thread.join(bounds["condition_seconds"])
    stop_observers.set()
    for thread in observers:
        thread.join(bounds["probe_seconds"] + bounds["poll_seconds"])
    forwarding = correlate_forwarding(fake_provider.tokens, frame_receipts, stream_owners, token_count, profile["timeout_ms"])
    baseline_p99 = percentile([sample.latency_ms() for sample in baseline], 0.99)
    added = percentile([sample.latency_ms() for sample in samples], 0.99) - baseline_p99
    noisy_paths = [paths.terminal / f"{body['request_id']}.json" for _status, body, _receipt in noisy if body.get("request_id")]
    for path in noisy_paths:
        wait_until(path.exists, bounds["condition_seconds"], bounds["poll_seconds"])
    proof = json.loads(noisy_paths[0].read_text()).get("capability_closeout") if noisy_paths else None
    checks = {
        "all_durable_acceptances": all(sample.receipt for sample in samples),
        "single_user_baseline_confirmed": all(sample.receipt for sample in baseline),
        "acceptance_added_p99": added <= scenario["targets"]["acceptance_added_p99_ms"],
        "control_durable_receipt": bool(stop_sample.receipt),
        "control_p99": stop_ms <= scenario["targets"]["control_p99_ms"],
        "exit_proof": bool(proof and proof.get("acknowledged") is True),
        "all_completed": all(sample.completed for sample in samples),
        "forwarding_receipts": not observer_errors and all(not token["failure"] for token in forwarding),
        "forwarding_p99": percentile([token["latency_ms"] for token in forwarding], 0.99) <= scenario["targets"]["forwarding_p99_ms"],
        "tool_mix_executed": len(tools) == sum(sample.kind == "tool" for sample in samples) and all(tool["ok"] and tool["handler_executed"] for tool in tools),
    }
    enforce = request.config._slo_stage or scenario["baseline_policy"] != "report_only"
    report = slo_report("cell50", profile, samples, baseline={"p99_ms": baseline_p99, "samples": len(baseline),
        "observations": [sample.as_dict() for sample in baseline]},
        acceptance_added_p99_ms=added, stop_acknowledged={"status": stop_status, "latency_ms": stop_ms, "response": stop_response},
        exit_proof={"source": "canonical_terminal_capability_closeout", "receipt": proof},
        provider_calls=len(fake_provider.records), provider_calls_before_noise=before_noise,
        offered_mix=dict(Counter(sample.kind for sample in samples)), tool_executions=tools,
        completion_states=terminals, observer_errors=observer_errors, target_checks=checks,
        targets_met=all(checks.values()), performance_enforced=enforce,
        forwarding={"samples": forwarding, "denominator": len(forwarding),
            "receipt_correlation": sum(not token["failure"] for token in forwarding) / len(forwarding),
            "p99_ms": percentile([token["latency_ms"] for token in forwarding], 0.99),
            "clock": "test_process_time_monotonic", "entry": "authenticated_HTTP_progress"})
    assert report["denominator"] == profile["users"], report
    assert Counter(sample.kind for sample in samples) == {kind: profile["users"] * percent // sum(profile["mix"].values())
        for kind, percent in profile["mix"].items()}, report
    assert all(token["latency_ms"] >= 0 for token in forwarding), report
    assert Path(report["report_path"]).is_file()
    if observer_fatals:
        raise observer_fatals[0]
    apply_report_gate(report, enforce=enforce)
