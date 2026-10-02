"""能力包 v2 块 1：capability.verification 声明、启用前执行确认与同意摘要的组件验证（不运行包内程序、不起 Gateway）。"""

import json

import pytest

from agent_py_agent.agent.capability_package_manifest import CapabilityDeclaration
from agent_py_agent.agent.capability_verification_manifest import VerificationDeclaration
from agent_py_agent.agent.capability_verifier_consent import (
    CONSENT_KIND,
    verifier_confirmation_code,
    verifier_confirmation_details,
    verifier_confirmation_message,
    verifier_consent_matches,
    verifier_consent_sha256,
)
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.tests.test_capability_activation import content_manager
from agent_py_agent.tests.test_capability_package import content_bundle

CHECKER = b"import json, sys\nprint(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': True}))\n"
VERIFICATION = {
    "deliverables": [{"id": "delivery", "path_patterns": ["**/*.json"], "required": True,
                      "field_match": {"format": "json", "field": "schema", "equals": ["story_delivery.v1"]}}],
    "verifiers": [{"id": "check-delivery", "member": "scripts/check.py", "runtime": "python",
                   "applies_to": "delivery", "args": ["{target}", "--host-json"], "timeout_seconds": 20,
                   "baseline": {"source": "input_same_deliverable", "flag": "--baseline-project"}}],
    "input_policy": "preserve_originals",
}


def _with_verification(verification=VERIFICATION):
    def change(manifest):
        manifest["capability"]["verification"] = verification
    return content_bundle(files={"CAPABILITY.md": b"# Content\n", "scripts/check.py": CHECKER}, change=change)


def test_verification_roundtrip_and_old_capability_bytes_unchanged():
    assert VerificationDeclaration.from_payload(VERIFICATION).to_payload() == VERIFICATION
    plain = {"description": "d", "keywords": ["k"], "entry_document": "CAPABILITY.md"}
    assert CapabilityDeclaration.from_payload(plain).to_payload() == plain
    assert "verification" not in CapabilityDeclaration.from_payload(plain).to_payload()
    manifest = inspect_plugin_package(_with_verification()).manifest
    assert manifest.capability.verification.runs_package_code
    assert manifest.to_payload()["capability"]["verification"] == VERIFICATION


@pytest.mark.parametrize("extra", [{"verificaton": VERIFICATION}, {"future_field": 1}])
def test_capability_declaration_rejects_unknown_keys(extra):
    plain = {"description": "d", "keywords": ["k"], "entry_document": "CAPABILITY.md"}
    with pytest.raises(ValueError):
        CapabilityDeclaration.from_payload({**plain, **extra})


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(extra=1),
    lambda v: v["verifiers"][0].update(args=["--host-json"]),
    lambda v: v["verifiers"][0].update(args=["{target}", "{target}"]),
    lambda v: v["verifiers"][0].update(args=["{target}", "--x={y}"]),
    lambda v: v["verifiers"][0].update(applies_to="other"),
    lambda v: v["verifiers"][0].update(timeout_seconds=121),
    lambda v: v["verifiers"][0].update(timeout_seconds=True),
    lambda v: v["deliverables"][0].update(path_patterns=["../escape.json"]),
    lambda v: v["deliverables"][0].update(path_patterns=["/abs.json"]),
    lambda v: v["deliverables"][0].update(path_patterns=["a**b/x.json"]),
    lambda v: v["deliverables"][0]["field_match"].update(field="has space"),
    lambda v: v["deliverables"].append(dict(v["deliverables"][0])),
    lambda v: v["verifiers"][0]["baseline"].update(flag="no-dash"),
])
def test_invalid_verification_shapes_are_rejected(mutate):
    payload = json.loads(json.dumps(VERIFICATION))
    mutate(payload)
    with pytest.raises(ValueError):
        VerificationDeclaration.from_payload(payload)


def test_unknown_runtime_format_and_policy_are_open_world_not_rejected():
    payload = json.loads(json.dumps(VERIFICATION))
    payload["verifiers"][0]["runtime"] = "node"
    payload["deliverables"][0]["field_match"]["format"] = "yaml"
    payload["input_policy"] = "future_policy"
    assert VerificationDeclaration.from_payload(payload).to_payload() == payload


def test_verifier_member_must_be_declared_package_file():
    def change(manifest):
        manifest["capability"]["verification"] = VERIFICATION
    with pytest.raises(PluginPackageError):
        inspect_plugin_package(content_bundle(change=change))


def test_consent_details_code_and_mismatch_on_any_change():
    package = inspect_plugin_package(_with_verification())
    details = verifier_confirmation_details(package.manifest, package.sha256)
    assert details["kind"] == CONSENT_KIND and details["verifiers"][0]["member"] == "scripts/check.py"
    assert len(details["verifiers"][0]["member_sha256"]) == 64
    consent = verifier_consent_sha256(details)
    assert verifier_confirmation_code(details) == consent[:12]
    assert verifier_consent_matches(package.manifest, package.sha256, consent)
    assert not verifier_consent_matches(package.manifest, "0" * 64, consent)
    assert not verifier_consent_matches(package.manifest, package.sha256, "")
    changed = json.loads(json.dumps(VERIFICATION))
    changed["verifiers"][0]["timeout_seconds"] = 21
    other = inspect_plugin_package(_with_verification(changed))
    assert not verifier_consent_matches(other.manifest, package.sha256, consent)
    message = verifier_confirmation_message({**details, "confirm_code": consent[:12]})
    assert "scripts/check.py" in message and f"--confirm {consent[:12]}" in message


def test_content_activation_without_consent_keeps_old_payload_and_identity():
    old = PluginContentActivation("enable", "story-content", "a" * 64, 1, 0)
    assert "verifier_consent_sha256" not in old.to_payload()
    assert PluginContentActivation.from_payload(old.to_payload()) == old
    consented = PluginContentActivation("enable", "story-content", "a" * 64, 1, 0, verifier_consent_sha256="b" * 64)
    assert PluginContentActivation.from_payload(consented.to_payload()) == consented
    assert consented.activation_id != old.activation_id
    with pytest.raises(ValueError):
        PluginContentActivation("enable", "story-content", "a" * 64, 1, 0, verifier_consent_sha256="xyz")


def test_enable_requires_confirmation_then_records_consent(tmp_path, monkeypatch):
    service, source = content_manager(tmp_path, monkeypatch)
    removed = service.command("/plugins remove story-content", revision=service.catalog().revision, request_id="rm0")
    assert removed["state"] == "succeeded", removed
    source.write_bytes(_with_verification())
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="inst")
    assert installed["state"] == "succeeded", installed
    preview = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="en1")
    confirmation = preview["details"]["confirmation"]
    assert preview["details"]["reason"] == "confirmation_required" and confirmation["kind"] == CONSENT_KIND
    assert service.installations.snapshot()[0].activation is None
    code = confirmation["confirm_code"]
    enabled = service.command(f"/plugins enable story-content --confirm {code}",
                              revision=service.catalog().revision, request_id="en2")
    assert enabled["state"] == "succeeded", enabled
    entry = service.installations.snapshot()[0]
    assert entry.activation.verifier_consent_sha256 == verifier_consent_sha256(
        {key: value for key, value in confirmation.items() if key != "confirm_code"})
    assert verifier_consent_matches(entry.manifest, entry.package_sha256, entry.activation.verifier_consent_sha256)


def test_enable_without_verifiers_needs_no_confirmation(tmp_path, monkeypatch):
    service, _source = content_manager(tmp_path, monkeypatch)
    enabled = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="en")
    assert enabled["state"] == "succeeded", enabled
    assert "verifier_consent_sha256" not in service.installations.snapshot()[0].activation.to_payload()
