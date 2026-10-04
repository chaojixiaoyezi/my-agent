"""B5 第一段纯合同；不启动插件、Gateway、模型或真实 owner。"""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from itertools import permutations

import pytest

from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.plugin_events.tool_gate import (
    GateCall,
    GateReply,
    GateReview,
    GateTarget,
    PluginToolGate,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


def gate_target(plugin="guard", gate_id="guard-one", arguments="none", scope=None):
    tools, effects = scope or (("run_command",), ())
    return GateTarget(plugin, "1.0.0", f"activation-{plugin}",
                      PluginToolGateDeclaration(gate_id, tools, effects, arguments))


def gate_call(arguments=None, tool="run_command", effect="dangerous", actor="main"):
    call = ToolCall("call-one", tool, arguments or {}, "native", "sha256:test", "run", "turn", "attempt")
    return GateCall(call, effect, actor, True)


def review(plugin, verdict, gate_id="guard-one", outcome="ok"):
    return GateReview(gate_target(plugin, gate_id), GateReply(verdict, f"REASON_{plugin.upper()}"), outcome)


@pytest.mark.parametrize("row", [
    ("allow", "allow_as_is", "allow"), ("allow", "ask", "ask"), ("allow", "deny", "deny"),
    ("ask", "allow_as_is", "ask"), ("ask", "ask", "ask"), ("ask", "deny", "deny"),
    ("deny", "allow_as_is", "deny"),
])
def test_seven_design_combinations_never_relax_host(row):
    host, verdict, expected = row
    merged = PluginToolGate.merge(host, (review("guard", verdict),))
    assert merged.status == expected
    assert bool(merged.requirements) is (verdict == "ask" and host != "deny")
    assert (merged.primary is not None) is (verdict != "allow_as_is" and host != "deny")


@pytest.mark.parametrize("host", ["allow", "ask", "deny"])
def test_no_review_keeps_host_decision(host):
    merged = PluginToolGate.merge(host, ())
    assert merged.status == host and merged.primary is None and not merged.requirements


@pytest.mark.parametrize("host", [None, "ALLOW", "unknown"])
def test_unknown_host_status_is_not_a_permission(host):
    with pytest.raises(ValueError):
        PluginToolGate.merge(host, ())


def test_most_restrictive_and_primary_do_not_depend_on_response_order():
    rows = (review("zeta", "deny"), review("beta", "deny"), review("alpha", "ask"), review("aardvark", "allow_as_is"))
    for sequence in permutations(rows):
        merged = PluginToolGate.merge("allow", sequence)
        assert merged.status == "deny" and merged.primary.target.plugin_id == "beta"
        assert [item.target.plugin_id for item in merged.requirements] == ["alpha"]


def test_ask_requirements_have_stable_plugin_and_gate_order():
    rows = (review("zeta", "ask"), review("alpha", "ask", "z-gate"), review("alpha", "ask", "a-gate"))
    for sequence in permutations(rows):
        merged = PluginToolGate.merge("ask", sequence)
        assert [(item.target.plugin_id, item.target.declaration.id) for item in merged.requirements] == [
            ("alpha", "a-gate"), ("alpha", "z-gate"), ("zeta", "guard-one")]
        assert merged.primary == merged.requirements[0]


def test_revoked_reply_is_not_a_requirement_or_a_denial():
    merged = PluginToolGate.merge("allow", (review("revoked", "deny", outcome="revoked"), review("guard", "ask")))
    assert merged.status == "ask" and merged.primary.target.plugin_id == "guard"
    assert [item.target.plugin_id for item in merged.requirements] == ["guard"]


@pytest.mark.parametrize("row", [
    (("run_command",), (), "run_command", "read_only", True),
    (("run_command",), (), "run_command_extra", "dangerous", False),
    (("run_command",), (), "other_run_command", "dangerous", False),
    ((), ("dangerous",), "future_tool", "dangerous", True),
    ((), ("mutating",), "future_tool", "dangerous", False),
    (("write_file",), ("dangerous",), "run_command", "dangerous", True),
    (("write_file",), ("dangerous",), "write_file", "read_only", True),
])
def test_matching_uses_exact_names_or_exact_effects(row):
    tools, effects, tool, effect, expected = row
    declaration = PluginToolGateDeclaration("guard", tools, effects, "none")
    assert PluginToolGate.matching((declaration,), gate_call(tool=tool, effect=effect)) == ((declaration,) if expected else ())


@pytest.mark.parametrize("actor", ["main", "subagent", "decision"])
def test_request_has_only_the_design_fields_and_no_arguments_for_none(actor):
    call = gate_call({"command": "echo synthetic", "call_origin": "host_command"}, actor=actor)
    payload = PluginToolGate.request_payload(gate_target(), call)
    assert payload == {"gate_id": "guard-one", "call": {
        "call_id": "call-one", "tool": "run_command", "effect": "dangerous", "actor": actor,
        "interactive": True, "args_hash": call.call.args_hash}}


def test_full_projection_removes_all_internal_keys_and_uses_shared_redaction_without_mutating_call():
    call = gate_call({"password": "synthetic-secret-marker", "__operation_id": "private-marker",
                      "nested": [{"__run_scope": "private-marker", "normal": "visible"}],
                      "api_key": "synthetic-key-marker", "command": "echo visible"})
    original, digest = copy.deepcopy(call.call.arguments), call.call.args_hash
    payload = PluginToolGate.request_payload(gate_target(arguments="full"), call)
    arguments = payload["call"]["arguments"]
    assert arguments == {"password": "<redacted>", "nested": [{"normal": "visible"}],
                         "api_key": "<redacted>", "command": "echo visible"}
    assert payload["call"]["args_hash"] == digest
    assert call.call.arguments == original and call.call.args_hash == digest
    arguments["nested"][0]["normal"] = "changed-projection"
    assert call.call.arguments == original


@pytest.mark.parametrize("text", ["字" * 5000, "\\\"" * 5000])
def test_full_projection_is_bounded_valid_json_and_keeps_the_arguments_object(text):
    call = gate_call({"text": text, "nested": {"secret": "synthetic-marker"}})
    arguments = PluginToolGate.request_payload(gate_target(arguments="full"), call)["call"]["arguments"]
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    assert isinstance(arguments, dict) and len(encoded) <= 4000
    assert arguments["text"] and text.startswith(arguments["text"])
    assert json.loads(encoded) == arguments and call.call.arguments["text"] == text


def test_redaction_precedes_truncation_even_for_a_secret_near_the_boundary():
    call = gate_call({"text": "x" * 3950 + " password=synthetic-secret-at-boundary " + "y" * 1000})
    arguments = PluginToolGate.request_payload(gate_target(arguments="full"), call)["call"]["arguments"]
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    assert "synthetic-secret" not in encoded and "<redacted>" in encoded and len(encoded) <= 4000


@pytest.mark.parametrize("verdict", ["allow_as_is", "ask", "deny"])
def test_reply_uses_only_verdict_reason_and_safe_message_ignoring_extra_control_fields(verdict):
    target = gate_target()
    raw = {"verdict": verdict, "reason_code": "REASON_123", "message": "first\nsecond\x00\u202e" + "字" * 100,
           "arguments": {"command": "changed"}, "call_origin": "host_command", "grant": True}
    decoded = PluginToolGate.decode_reply(target, raw)
    assert decoded.target == target and decoded.outcome == "ok"
    assert decoded.reply == GateReply(verdict, "REASON_123", ("firstsecond" + "字" * 100)[:80])
    assert raw["arguments"] == {"command": "changed"}
    with pytest.raises(FrozenInstanceError):
        decoded.reply.verdict = "allow_as_is"


@pytest.mark.parametrize("reason", ["A", "0", "_", "A" * 40])
def test_reason_code_accepts_only_the_documented_boundary_shapes(reason):
    decoded = PluginToolGate.decode_reply(gate_target(), {"verdict": "ask", "reason_code": reason})
    assert decoded.outcome == "ok" and decoded.reply == GateReply("ask", reason)


@pytest.mark.parametrize("raw", [
    None, [], "allow_as_is", {}, {"verdict": "ask"}, {"reason_code": "REASON"},
    {"verdict": "allow", "reason_code": "REASON"}, {"verdict": "ASK", "reason_code": "REASON"},
    {"verdict": ["ask"], "reason_code": "REASON"}, {"verdict": "ask", "reason_code": "lower"},
    {"verdict": "ask", "reason_code": "A" * 41}, {"verdict": "ask", "reason_code": "A\n"},
    {"verdict": "ask", "reason_code": ""}, {"verdict": "ask", "reason_code": True},
    {"verdict": "ask", "reason_code": "中文"}, {"verdict": "ask", "reason_code": "A-B"},
    {"verdict": "ask", "reason_code": "VALID", "message": None},
    {"verdict": "ask", "reason_code": "VALID", "message": ["text"]},
])
def test_malformed_reply_is_always_ask_and_not_a_permission(raw):
    decoded = PluginToolGate.decode_reply(gate_target(), raw)
    assert decoded.outcome == "malformed" and decoded.reply == GateReply("ask", "PLUGIN_GATE_MALFORMED")
    assert PluginToolGate.merge("allow", (decoded,)).status == "ask"


def test_plugin_extra_arguments_never_replace_real_call_arguments():
    call = gate_call({"command": "echo original"})
    target = gate_target(arguments="full")
    payload = PluginToolGate.request_payload(target, call)
    decoded = PluginToolGate.decode_reply(target, {"verdict": "ask", "reason_code": "CONFIRM",
                                                   "arguments": {"command": "echo replaced"}})
    assert PluginToolGate.merge("allow", (decoded,)).status == "ask"
    assert call.call.arguments == {"command": "echo original"}
    assert payload["call"]["arguments"] == {"command": "echo original"}
    assert replace(call, interactive=False).call == call.call


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_message_unicode_line_separators_cannot_insert_approval_lines(separator):
    decoded = PluginToolGate.decode_reply(gate_target(), {"verdict": "ask", "reason_code": "CONFIRM",
                                                         "message": "first" + separator + "second"})
    assert decoded.reply.message == "firstsecond"


def test_large_nested_lists_share_one_budget_and_preserve_json_and_original_input():
    call = gate_call({"rows": [{"a": "x" * 2500}, {"b": "字" * 2500}, {"__hidden": "private-marker"}]})
    original = copy.deepcopy(call.call.arguments)
    arguments = PluginToolGate.request_payload(gate_target(arguments="full"), call)["call"]["arguments"]
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 4000 and json.loads(encoded) == arguments
    assert isinstance(arguments["rows"], list) and arguments["rows"][0] == {"a": "x" * 2500}
    assert arguments["rows"][1]["b"] and "private-marker" not in encoded
    assert call.call.arguments == original


def test_key_over_the_budget_is_not_sent_as_invalid_json_or_a_fake_parameter():
    call = gate_call({"a" * 4000: "synthetic", "normal": "visible"})
    arguments = PluginToolGate.request_payload(gate_target(arguments="full"), call)["call"]["arguments"]
    assert arguments == {} and len(json.dumps(arguments)) <= 4000
    assert call.call.arguments["normal"] == "visible"


def test_small_primitives_and_nested_arrays_keep_their_types_and_original_hash():
    call = gate_call({"values": [True, False, None, 1, 1.5, ["a"]], "object": {"value": 2}})
    payload = PluginToolGate.request_payload(gate_target(arguments="full"), call)
    assert payload["call"]["arguments"] == call.call.arguments
    assert payload["call"]["args_hash"] == call.call.args_hash


def test_effect_only_gate_receives_hash_but_no_parameters_or_host_identity_fields():
    call = gate_call({"normal": "visible", "__activation": "private-marker"})
    target = gate_target(scope=((), ("dangerous",)))
    payload = PluginToolGate.request_payload(target, call)
    assert set(payload) == {"gate_id", "call"}
    assert set(payload["call"]) == {"call_id", "tool", "effect", "actor", "interactive", "args_hash"}
    assert "activation-guard" not in json.dumps(payload) and "private-marker" not in json.dumps(payload)


def test_budget_omits_an_inseparable_scalar_instead_of_fabricating_null():
    call = gate_call({"text": "x" * 3960, "number": 12345678901234567890})
    arguments = PluginToolGate.request_payload(gate_target(arguments="full"), call)["call"]["arguments"]
    assert "number" not in arguments
    assert call.call.arguments["number"] == 12345678901234567890


@pytest.mark.parametrize("outcome", ["timeout", "error", "malformed", "unavailable", "revoked"])
@pytest.mark.parametrize("verdict", ["allow_as_is", "ask", "deny"])
def test_non_ok_construction_cannot_produce_a_permission(outcome, verdict):
    item = GateReview(gate_target(), GateReply(verdict, "UNTRUSTED", "not an authority"), outcome, 12)
    expected_code = "PLUGIN_GATE_UNAVAILABLE" if outcome == "revoked" else "PLUGIN_GATE_" + outcome.upper()
    assert item.reply == GateReply("ask", expected_code)
    assert item.latency_ms == 12 and item.outcome == outcome
    merged = PluginToolGate.merge("allow", (item,))
    assert merged.status == ("allow" if outcome == "revoked" else "ask")
    assert bool(merged.requirements) is (outcome != "revoked")


@pytest.mark.parametrize("separator", ["\x00", "\n", "\r", "\t", "\u2028", "\u2029", "\u202e", "\u2066", "\u2069"])
def test_direct_reply_uses_the_same_message_cleaning_as_decode(separator):
    raw = "first" + separator + "second" + "字" * 100
    direct = GateReply("ask", "CONFIRM", raw)
    decoded = PluginToolGate.decode_reply(gate_target(), {"verdict": "ask", "reason_code": "CONFIRM", "message": raw})
    assert direct.message == ("firstsecond" + "字" * 100)[:80]
    assert decoded.reply.message == direct.message
