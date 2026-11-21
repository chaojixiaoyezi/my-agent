"""Protected-read attestation binds the intended target and host-observed access protection."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_py_agent.agent.agent_core.runtime.minimal_tool_audit import archive_minimal_tool_audit
from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation import input_dependencies
from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
from agent_py_agent.agent.conversation.store_wake_publication import published_signal
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
from agent_py_agent.agent.user_space.task_credential_http import (
    ProtectedReadResponse,
    credential_authenticated_request,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_fc1_credential_continuation import _dm_case
from agent_py_agent.tests.test_web_fetch_ssrf import _tool as _web_tool

SITE = "https://m68.example.com"
TARGET = SITE + "/account"
SECRET = "SyntheticProtectedReadSecret"


class _Site:
    """Simulated origin. Protected paths refuse a request without Authorization; other paths are public."""

    def __init__(self, protected=("/account",), redirects=None, denial_redirect=None):
        self.protected, self.redirects = set(protected), dict(redirects or {})
        self.denial_redirect = denial_redirect
        self.requests = []

    def __call__(self, request, **_kwargs):
        authorized = "Authorization" in request.headers
        self.requests.append((request.method, request.url, authorized))
        final = self.redirects.get(urlsplit(request.url).path, request.url)
        if urlsplit(final).path in self.protected:
            denial = RawResponseParts(401, {}, b"", self.denial_redirect or request.url)
            return ProtectedReadResponse(
                200, {"Content-Type": "application/json"}, b'{"status":"sample"}', final,
                denial_request_url=request.url, denial_method=request.method, denial=denial,
            )
        return RawResponseParts(200, {"Content-Type": "application/json"}, b'{"status":"sample"}', final)


def _credential_wake(tmp_path, monkeypatch, target=TARGET):
    """Real card, submitted secret and published credential wake; the current turn is that wake."""
    ctx = _dm_case(tmp_path)
    inputs = input_dependencies.input_dependency_access(ctx.agent)
    ref, _ = request_credential_card(ctx.agent, url=target, authentication="basic",
                                     options=CredentialCardOptions(access_scope="task"))
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context="test").payload
    input_dependencies.arm_input_dependency(ctx.agent, row["input_id"])
    with monkeypatch.context() as patch:
        patch.setattr(input_dependencies.InputDependencyAccess, "publish", lambda self, row, **_: row)
        assert resolve_private_interaction(ctx.agent, {
            "reference": row["reference"], "decision": "submit",
            "message_id": row["message_id"], "chat_id": row["chat_id"],
            "fields": {"username": "synthetic-user", "secret": SECRET},
        }, channel="feishu", actor_id=ctx.actor.actor_id)["ok"]
    inputs.publish(inputs.read(row["input_id"]))
    wake = published_signal(inputs.store.storage, ctx.thread.thread_id, "task-input:" + row["input_id"])
    ctx.agent._current_run_params = replace(
        ctx.agent._current_run_params, turn_trigger=lifecycle_wake_turn_trigger(wake.reason, wake.to_dict(), ()))
    return ctx, inputs, row["input_id"], ref


def _read(ctx, ref, site, url, **params):
    web = _web_tool()
    web.raw_fetcher = site
    web.credential_authenticator = lambda reference, request, send: credential_authenticated_request(
        ctx.agent, reference, request, send=send,
    )
    outcome = web.execute({"url": url, "credential_ref": ref, **params})
    assert outcome.ok, outcome.output
    return outcome


def _chain(ctx, inputs, input_id, outcome, call_id):
    result = ToolResult(call_id, "web_fetch", "succeeded", handler_executed=True,
                        metadata={"handler_details": outcome.result_envelope})
    record = {"tool": result.tool_name, "id": result.call_id, "ok": result.ok,
              "handler_executed": result.handler_executed, "tool_result_envelope": _compact_result_envelope(result)}
    archive_minimal_tool_audit(ctx.agent, ctx.agent._current_run_params, record)
    with Path(record["raw_archive_path"]).open(encoding="utf-8") as handle:
        event = next(row for row in map(json.loads, handle) if row["event_id"] == record["raw_archive_event_id"])
    return input_dependencies.credential_evidence_chain(inputs, input_id, tool_event=event), event


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_protected_target_with_an_authentication_challenge_reaches_verified(tmp_path, monkeypatch, method):
    target = TARGET + "?view=private-canary"
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch, target)
    site = _Site()
    outcome = _read(ctx, ref, site, target, method=method)

    report, event = _chain(ctx, inputs, input_id, outcome, "m68-protected")
    assert report["state"] == "verified"
    proof = report["protected_resource_check"]
    assert (proof["method"], proof["http_status"], proof["credential_ref"]) == (method, 200, ref)
    assert site.requests == [(method, target, True)]
    persisted = json.dumps(event)
    assert SECRET not in persisted and "private-canary" not in persisted and "/account" not in persisted

    # Evidence belongs to that read only: a later public read in the same turn is not attested.
    public = _read(ctx, ref, site, SITE + "/status")
    assert _chain(ctx, inputs, input_id, public, "m68-public")[0]["state"] == "woken"


def test_public_resource_on_the_same_origin_stays_woken_without_an_extra_request(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site()
    outcome = _read(ctx, ref, site, SITE + "/status")

    assert "protected_resource_check" not in outcome.result_envelope
    assert site.requests == [("GET", SITE + "/status", True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-status")[0]["state"] == "woken"


def test_other_protected_resource_is_not_the_intended_target(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site(protected=("/account", "/admin"))
    outcome = _read(ctx, ref, site, SITE + "/admin")

    assert site.requests == [("GET", SITE + "/admin", True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-admin")[0]["state"] == "woken"


def test_status_only_target_uses_one_business_request_and_stays_woken(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site(protected=())
    outcome = _read(ctx, ref, site, TARGET)

    assert site.requests == [("GET", TARGET, True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-open-target")[0]["state"] == "woken"


def test_effective_response_from_another_resource_is_not_attested(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site(redirects={"/account": SITE + "/welcome"})
    outcome = _read(ctx, ref, site, TARGET)

    assert site.requests == [("GET", TARGET, True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-redirected")[0]["state"] == "woken"


def test_model_supplied_headers_cannot_supply_the_challenge(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site(protected=())
    outcome = _read(ctx, ref, site, TARGET, headers={"X-Api-Key": "model-supplied"})

    assert site.requests == [("GET", TARGET, True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-forged")[0]["state"] == "woken"


def test_effective_denial_of_a_different_resource_leaves_the_read_unattested(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch)
    site = _Site(denial_redirect=TARGET + "?record=other")
    outcome = _read(ctx, ref, site, TARGET)

    assert "sample" in outcome.output
    assert site.requests == [("GET", TARGET, True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-probe-error")[0]["state"] == "woken"


def test_ordinary_credentialed_read_sends_no_challenge(tmp_path, monkeypatch):
    ctx, _inputs, _input_id, ref = _credential_wake(tmp_path, monkeypatch)
    ctx.agent._current_run_params = replace(ctx.agent._current_run_params, turn_trigger=None)
    site = _Site()
    outcome = _read(ctx, ref, site, TARGET)

    assert "protected_resource_check" not in outcome.result_envelope
    assert site.requests == [("GET", TARGET, True)]


def test_protection_for_a_different_query_selected_record_is_excluded(tmp_path, monkeypatch):
    ctx, inputs, input_id, ref = _credential_wake(tmp_path, monkeypatch, TARGET + "?record=one")
    site = _Site()
    other = TARGET + "?record=two"
    outcome = _read(ctx, ref, site, other)

    assert site.requests == [("GET", other, True)]
    assert _chain(ctx, inputs, input_id, outcome, "m68-other-query")[0]["state"] == "woken"
