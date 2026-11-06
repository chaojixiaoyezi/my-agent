"""Lossless causal normalization and Gateway replay invariants (C-S1W6-GOLDEN-NORMALIZE)."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlsplit

ID_NAMESPACES = {name: name for name in (
    "request_id", "thread_id", "execution_attempt_id", "tool_call_id", "receipt_id",
    "session_id", "operation_id", "event_id", "connection_id", "child_id", "run_id", "fixture_root",
    "attempt_id", "parent_id", "root_id", "pid", "pgid", "pid_birth_token", "launch_id", "source_root",
)}
ID_NAMESPACES.update({"previous_request_id": "request_id", "previous_execution_attempt_id": "execution_attempt_id",
                      "predecessor_thread_id": "thread_id", "input_request_id": "request_id", "child_id": "run_id"})
TERMINAL = frozenset({"done", "failed", "cancelled", "completed", "succeeded", "interrupted"})
ACTIVE = frozenset({"running", "processing", "started"})
TIMESTAMP_KEYS = frozenset({"at", "timestamp", "created_at", "updated_at", "started_at", "finished_at", "heartbeat_at"})
GENERATED_LABEL = re.compile(r"gwreq-msg-[0-9a-f]{32}|gateway-attempt-[0-9a-f]{32}|tool_operation:[0-9a-f]{32}|thread-[0-9a-f]{16}|(?:agentrun|taskrun|attempt|subagent)-[0-9]+-[0-9a-f]+")


# LLM: Timestamp ordering uses numerical or ISO instants, not dictionary visitation order.
# 函数用途: Parse a recorded timestamp while refusing invalid or non-finite observations.
def _instant(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (float, int)):
        result = float(value)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    if not math.isfinite(result):
        raise ValueError("non-finite timestamp")
    return result


# LLM: No event or field is removed; content digests are recomputed from normalized bodies, never renamed as IDs.
# 函数用途: Normalize generated labels and chronological ordinals while keeping actual request content comparable.
def normalize(trace):
    maps, clocks = defaultdict(dict), defaultdict(set)
    child_labels = {}

    # LLM: Each namespace retains a bijection including references embedded in provider messages.
    # 函数用途: Assign one stable label to each generated identity.
    def identity(namespace, value):
        if value in (None, "", 0):
            return value
        key = (type(value).__name__, str(value))
        if key not in maps[namespace]:
            maps[namespace][key] = f"{namespace}#{len(maps[namespace]) + 1}"
        return maps[namespace][key]

    # LLM: Collect timestamps before conversion so reversing finish/start cannot keep the same normal form.
    # 函数用途: Gather all instants in one source/session without reordering events.
    def collect(value, stream, key=""):
        if isinstance(value, dict):
            for name, child in value.items():
                collect(child, stream, name)
        elif isinstance(value, list):
            for child in value:
                collect(child, stream, key)
        elif key in TIMESTAMP_KEYS and value is not None:
            clocks[stream].add(_instant(value))
        elif isinstance(value, str):
            for match in re.finditer(r"subagent-[0-9]+-([0-9a-f]{8})", value):
                assert child_labels.setdefault(match.group(1), match.group()) == match.group(), "ambiguous child short label"
    streams = [(str(e.get("session_id") or e.get("thread_id") or ""), str(e.get("source") or "")) for e in trace]
    for event, stream in zip(trace, streams):
        collect(event, stream)
        for output in event.get("referenced_outputs", []):
            collect(json.loads(output["content"]), stream)
    ordinals = {stream: {stamp: index + 1 for index, stamp in enumerate(sorted(stamps))} for stream, stamps in clocks.items()}

    # LLM: Only fixture-generated path roots, loopback ports, causal labels and protocol probe nonces are substituted in text.
    # 函数用途: Preserve provider prompt text while making its generated labels repeatable.
    def text(value, event, *, tool_header=False):
        for namespace in ("fixture_root", "source_root"):
            root = event.get(namespace)
            if root:
                value = value.replace(root, identity(namespace, root))
        for key in ("provider_origin", "gateway_origin"):
            origin = event.get(key)
            if origin:
                parsed = urlsplit(origin)
                stable = f"{parsed.scheme}://{parsed.hostname}:<{key}-port>{parsed.path}"
                value = value.replace(origin, stable)
        # Header identities have already been visited before nested provider bodies.
        for namespace, mapping in list(maps.items()):
            for (_type, original), stable in list(mapping.items()):
                if namespace != "fixture_root" and original and len(original) > 8:
                    value = value.replace(original, stable)
        value = GENERATED_LABEL.sub(lambda match: identity("embedded_id", match.group()), value)
        value = re.sub(r"(?<=子代理\[)[0-9a-f]{8}(?=\])",
                       lambda match: identity("embedded_id", child_labels[match.group()]) if match.group() in child_labels else match.group(), value)
        value = re.sub(r"(:after-tools:\d+:)([0-9a-f]{32})",
                       lambda match: match.group(1) + identity("model_turn_nonce", match.group(2)), value)
        value = re.sub(r"(/tool_outputs/[^/\s\"]+)-([0-9a-f]{24})-([0-9a-f]{12})(?=\.json)",
                       lambda match: match.group(1) + "-" + identity("artifact_nonce", match.group(2)) + "-" + match.group(3), value)
        for digest, (canonical_digest, canonical_size, original_size) in event.get("_output_aliases", {}).items():
            pattern = r"(output_hash: )" + digest + r"(\s*[-] output_size_bytes: )(\d+)"
            for match in re.finditer(pattern, value):
                assert int(match.group(3)) == original_size, "referenced output byte count mismatch"
            value = re.sub(pattern,
                           lambda match: match.group(1) + canonical_digest + match.group(2) + str(canonical_size), value)
            value = value.replace(digest, canonical_digest).replace(digest[:12], canonical_digest[:12])
        # Only an actual tool-message header authorizes elapsed-time normalization, never arbitrary business text.
        if tool_header:
            header, separator, payload = value.partition("\n")
            if re.fullmatch(r"\[tool-result; tool=[^;\]\n]+; status=[^;\]\n]+; handler_executed=(?:true|false); [^\]\n]*\]", header):
                header = re.sub(r"(?<=duration_ms=)(-?\d+(?:\.\d+)?)(?=[;\]])",
                    lambda match: "[timestamp#1..timestamp#2]" if float(match.group()) > 0 else
                    "[timestamp#2..timestamp#1]" if float(match.group()) < 0 else "[timestamp#1..timestamp#1]", header)
            value = header + separator + payload
        value = re.sub(r"(?<=nonce )[0-9a-f]{16}", lambda match: identity("probe_nonce", match.group()), value)
        # Rendered workspace clock snapshots are independent metadata substreams, not lifecycle clocks.
        value = re.sub(r"(current_local_(?:date|year|time): )\d{4}(?:-\d{2}-\d{2})?(?: \d{2}:\d{2}:\d{2})?",
                       r"\1timestamp#1", value)
        # Range endpoints share a substream so an inverted or collapsed interval stays observable.
        # LLM: Both endpoints retain their shared temporal ordering and equality relation.
        # 函数用途: Convert one rendered calendar interval without concealing a reversed range.
        def interval(match):
            first, last = match.group(2), match.group(3)
            positions = {stamp: index + 1 for index, stamp in enumerate(sorted({first, last}))}
            return f"{match.group(1)}timestamp#{positions[first]}..timestamp#{positions[last]}"
        value = re.sub(r"((?:current_week_range|last_7_days_range): )(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", interval, value)
        return value

    # LLM: Nested values keep all epoch/state/count fields; only declared volatile identity/time fields change.
    # 函数用途: Recursively normalize a complete event without deleting payload fields.
    def visit(value, stream, event, key="", *, tool_header=False):
        if isinstance(value, dict):
            actual_tool = value.get("role") == "tool" and any(value is message for message in event.get("body", {}).get("messages", []))
            return {name: visit(child, stream, event, name, tool_header=actual_tool and name == "content") for name, child in value.items()}
        if isinstance(value, list):
            return [visit(child, stream, event, key) for child in value]
        if key in ID_NAMESPACES:
            return identity(ID_NAMESPACES[key], value)
        if key in TIMESTAMP_KEYS and value is not None:
            return ordinals[stream][_instant(value)]
        if isinstance(value, str) and key != "body_hash":
            return text(value, event, tool_header=tool_header)
        return value

    result = []
    for event, stream in zip(trace, streams):
        if "body" in event and "body_hash" in event:
            wire = json.dumps(event["body"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            assert hashlib.sha256(wire).hexdigest() == event["body_hash"], "provider content/hash mismatch"
        context = dict(event)
        outputs, aliases = [], {}
        for output in event.get("referenced_outputs", []):
            assert hashlib.sha256(output["content"].encode()).hexdigest() == output["sha256"], "referenced output content/hash mismatch"
            payload = json.loads(output["content"])
            canonical = json.dumps(visit(payload, stream, context), ensure_ascii=False, indent=2, sort_keys=True)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            aliases[output["sha256"]] = digest, len(canonical.encode()), len(output["content"].encode())
            outputs.append({"sha256": digest, "content": canonical})
        context["_output_aliases"] = aliases
        row = visit(event, stream, context)
        if "referenced_outputs" in row:
            row["referenced_outputs"] = outputs
        if "body" in row and "body_hash" in row:
            wire = json.dumps(row["body"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            row["body_hash"] = hashlib.sha256(wire).hexdigest()
        result.append(row)
    return result


# LLM: Stable-but-wrong traces fail independently of baseline equality or repetition count.
# 函数用途: Verify effects, execution fences, all pending stops, monotonic terminals and causal exit ordering.
def assert_invariants(trace, expected_effects, forbidden_effects=()):
    effects = Counter()
    states, epochs, active, stopped, proofs, last_sequences, positions, pair_scopes = {}, {}, {}, defaultdict(set), set(), {}, {}, {}
    for position, event in enumerate(trace):
        source, sequence = event["source"], event["seq"]
        assert sequence > last_sequences.get(source, 0), f"non-monotonic source sequence: {source}"
        last_sequences[source] = sequence
        positions[source, sequence] = position
        kind = event.get("kind")
        scope = event.get("scope") or event.get("thread_id") or event.get("session_id")
        pair = event.get("request_id"), event.get("execution_attempt_id")
        if kind in {"stop_ack", "exit_proof", "attempt_start", "attempt_terminal"}:
            assert scope and all(pair), "lifecycle event lacks a durable scope or execution identity"
        if all(pair) and scope is not None:
            assert pair_scopes.setdefault(pair, scope) == scope, "attempt or receipt moved to another session"
        if kind == "effect":
            count = event.get("count", 1)
            assert type(count) is int and count > 0, "invalid effect count"
            effects[event["effect_kind"]] += count
        if kind == "stop_ack":
            stopped[scope].add(pair)
        if kind == "exit_proof":
            assert event.get("confirmed") is True, "unconfirmed process exit"
            proofs.add(pair)
            if active.get(scope) == pair:
                active.pop(scope)
        if kind in {"next_turn", "attempt_start"}:
            previous = event.get("previous_request_id"), event.get("previous_execution_attempt_id")
            if previous[0] is not None:
                assert all(previous) and previous in proofs, "next turn before previous exit proof"
            assert stopped[scope] <= proofs, "re-execution before stop exit proof"
        state = str(event.get("state") or "").lower()
        if state:
            assert not (states.get(pair) in TERMINAL and state not in TERMINAL), "terminal state regressed"
            states[pair] = state
            epoch = event.get("lease_epoch", 0)
            assert type(epoch) is int, "lease epoch must not be normalized away"
            if pair[0] and event.get("disposition") != "stale_claim":
                assert epoch >= epochs.get(pair[0], epoch), "old attempt closed after a newer epoch"
                epochs[pair[0]] = max(epoch, epochs.get(pair[0], epoch))
            if state in ACTIVE:
                assert scope is not None and all(pair), "active attempt lacks identity"
                assert scope not in active or active[scope] == pair, "two active attempts in one scope"
                active[scope] = pair
            if state in TERMINAL and active.get(scope) == pair:
                active.pop(scope)
        for edge in event.get("happens_after", []):
            reference = edge["source"], edge["seq"]
            assert reference in positions and positions[reference] < position, "happens-before edge violated"
    assert dict(effects) == {key: count for key, count in expected_effects.items() if count}, (effects, expected_effects)
    assert not (set(effects) & set(forbidden_effects)), "forbidden external effect"
    for scope, pending in stopped.items():
        assert pending <= proofs, f"missing exit proof for stopped scope: {scope}"
    assert not active, f"unfinished active attempts: {active}"
