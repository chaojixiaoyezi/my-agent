"""W0 pure contracts: exact injection bytes, safe diagnostics, and shared flags.

Business handlers and provider cache rates are deliberately not asserted here.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import AgentConfig, load_config, normalize_agent_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry

BASE = "prompt.runtime_injection"


def _module(suffix):
    name = "agent_py_agent.agent." + suffix
    assert importlib.util.find_spec(name) is not None, f"W0 contract module missing: {name}"
    return importlib.import_module(name)


def test_runtime_fragment_is_frozen_and_protocol_sources_are_fixed():
    mod = _module("prompting_parts.runtime_injection")
    assert mod.RUNTIME_INJECTION_SOURCE == BASE
    for constant, suffix in (("DIRECT_CHILDREN", "direct_children"), ("DELIVERY_CONTRACT", "delivery_contract"),
                             ("PUBLIC_RECEIPT", "public_receipt"), ("GOAL", "goal")):
        assert getattr(mod, f"RUNTIME_{constant}_SOURCE") == BASE + "." + suffix
    fragment = mod.RuntimeInjectionFragment(" # not parsed\n ", BASE + ".goal")
    assert asdict(fragment) == {"text": " # not parsed\n ", "source": BASE + ".goal"}
    assert [f.name for f in fields(fragment)] == ["text", "source"]
    with pytest.raises(FrozenInstanceError):
        fragment.text = "changed"


@pytest.mark.parametrize("text", [0, False, b"x", [], object()])
def test_runtime_fragment_rejects_non_text(text):
    mod = _module("prompting_parts.runtime_injection")
    with pytest.raises(TypeError):
        mod.RuntimeInjectionFragment(text, BASE + ".goal")


@pytest.mark.parametrize("source", [None, 0, b"x", []])
def test_runtime_fragment_rejects_non_string_source(source):
    mod = _module("prompting_parts.runtime_injection")
    with pytest.raises(TypeError):
        mod.RuntimeInjectionFragment("x", source)


@pytest.mark.parametrize("source", ["", " ", BASE, BASE + ".", "prefix." + BASE + ".goal",
                                    BASE + "x.goal", " " + BASE + ".goal", BASE + ".goal "])
def test_typed_fragments_require_strict_child_source_without_edge_whitespace(source):
    mod = _module("prompting_parts.runtime_injection")
    with pytest.raises(ValueError):
        mod.RuntimeInjectionFragment(None, source)


@pytest.mark.parametrize("source, expected", [(None, False), ({}, False), (12, False), (BASE, True),
                                              (BASE + ".", False), (BASE + ".goal", True),
                                              (BASE + "_other.goal", False), ("x." + BASE, False)])
def test_source_family_predicate_never_coerces_or_raises(source, expected):
    mod = _module("prompting_parts.runtime_injection")
    assert mod.is_runtime_injection_source(source) is expected


def test_freeze_copies_iterable_preserves_order_and_legacy_whitespace():
    mod = _module("prompting_parts.runtime_injection")
    fragment = mod.RuntimeInjectionFragment(None, BASE + ".goal")
    original = ["", "  ", fragment, "# Runtime Injection: fake\nraw"]
    frozen = mod.freeze_runtime_injections(iter(original))
    original.clear()
    assert frozen == ("", "  ", fragment, "# Runtime Injection: fake\nraw")
    assert mod.freeze_runtime_injections(None) == ()
    assert mod.runtime_injection_texts(frozen) == ("", "  ", "# Runtime Injection: fake\nraw")


@pytest.mark.parametrize("values", ["legacy", b"legacy", bytearray(b"x"), 7, [object()], [None]])
def test_freeze_rejects_bare_containers_and_invalid_elements(values):
    mod = _module("prompting_parts.runtime_injection")
    with pytest.raises(TypeError):
        mod.freeze_runtime_injections(values)


def test_duplicate_typed_sources_fail_in_every_projection():
    mod = _module("prompting_parts.runtime_injection")
    values = [mod.RuntimeInjectionFragment("old", BASE + ".goal"),
              mod.RuntimeInjectionFragment(None, BASE + ".goal")]
    for function in (mod.freeze_runtime_injections, mod.runtime_injection_texts, mod.runtime_injection_sections):
        with pytest.raises(ValueError):
            function(values)
    assert mod.freeze_runtime_injections(["same", "same"]) == ("same", "same")


def test_replace_preserves_source_and_cannot_express_withdrawal():
    mod = _module("prompting_parts.runtime_injection")
    old = mod.RuntimeInjectionFragment(None, BASE + ".goal")
    new = mod.replace_runtime_injection_text(old, "")
    assert new is not old and new.source == old.source and new.text == "" and old.text is None
    assert mod.replace_runtime_injection_text("old", "  new\n") == "  new\n"
    with pytest.raises(TypeError):
        mod.replace_runtime_injection_text(old, None)
    with pytest.raises(TypeError):
        mod.replace_runtime_injection_text(object(), "x")


def test_sections_keep_one_legacy_base_and_typed_tombstone_in_input_order():
    mod = _module("prompting_parts.runtime_injection")
    values = [mod.RuntimeInjectionFragment("", BASE + ".goal"), "legacy\n",
              mod.RuntimeInjectionFragment(None, BASE + ".direct_children"), "  ",
              mod.RuntimeInjectionFragment("# opaque\nbody", BASE + ".delivery_contract")]
    assert mod.runtime_injection_sections(values) == (
        (BASE, "# Runtime Injection\nlegacy\n\n  "),
        (BASE + ".goal", "# Runtime Injection: " + BASE + ".goal\n"),
        (BASE + ".direct_children", ""),
        (BASE + ".delivery_contract", "# Runtime Injection: " + BASE + ".delivery_contract\n# opaque\nbody"),
    )
    assert mod.runtime_injection_sections(None) == ((BASE, "# Runtime Injection\n（无）"),)
    assert mod.runtime_injection_sections([""]) == ((BASE, "# Runtime Injection\n（无）"),)
    assert mod.runtime_injection_sections([" "]) == ((BASE, "# Runtime Injection\n "),)


def test_clear_text_is_exact_and_not_a_business_absence_claim():
    mod = _module("prompting_parts.runtime_injection")
    for source in (BASE, BASE + ".goal"):
        assert mod.runtime_injection_clear_text(source) == (
            f"[runtime-injection-cleared source={source}] 当前无有效快照；该来源更早的快照不代表当前状态。")
    with pytest.raises(TypeError):
        mod.runtime_injection_clear_text(None)
    with pytest.raises(ValueError):
        mod.runtime_injection_clear_text("other.source")


def test_injection_module_has_no_io_runtime_or_business_imports():
    mod = _module("prompting_parts.runtime_injection")
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in {"dataclasses", "collections.abc", "typing"} for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in {"__future__", "dataclasses", "collections.abc", "typing"}


@pytest.mark.parametrize("key, default", [("business_spaces_enabled", True), ("prompt_runtime_snapshots_enabled", True)])
def test_shared_flags_have_matching_python_yaml_registry_defaults(key, default):
    assert getattr(AgentConfig(), key, None) is default
    path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    config = load_config(path)
    assert getattr(config, key) is default
    spec = parameter_registry()[key]
    assert spec.default is default and spec.value_type == "bool" and spec.description
    assert not spec.writable  # A presentation/context flag cannot grant itself business access.
    assert not any(key in warning for warning in config.config_warnings)


@pytest.mark.parametrize("key", ["business_spaces_enabled", "prompt_runtime_snapshots_enabled"])
@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True), (False, False), (True, True)])
def test_shared_flags_use_existing_boolean_normalizer(key, raw, expected):
    original = {key: raw}
    normalized, warnings = normalize_agent_config(original)
    assert normalized[key] is expected and original[key] == raw and warnings == []


@pytest.mark.parametrize("key, default", [("business_spaces_enabled", True), ("prompt_runtime_snapshots_enabled", True)])
def test_invalid_shared_flags_warn_and_use_safe_defaults(key, default):
    normalized, warnings = normalize_agent_config({key: ["false"]})
    assert normalized[key] is default
    assert any(key in warning for warning in warnings)


def test_config_diagnostic_is_readonly_and_redacts_values_without_writing(tmp_path, monkeypatch):
    mod = _module("settings.config_diagnostics")
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("diagnostic must not write"))
    diagnostic = mod.ConfigParseDiagnostic(str(tmp_path / "config.yaml"), 3, 5, "YAML_SYNTAX",
                                           "api_key=example-only-secret", "error", True)
    data = asdict(diagnostic)
    assert set(data) == {"source", "line", "column", "code", "message", "severity", "value_redacted"}
    assert data["line"] == 3 and data["column"] == 5 and data["severity"] == "error"
    assert "example-only-secret" not in data["message"]
    with pytest.raises(FrozenInstanceError):
        diagnostic.code = "changed"
    with pytest.raises(ValueError):
        mod.ConfigParseDiagnostic("x", 0, None, "BAD", "bad", "error", False)
    with pytest.raises(ValueError):
        mod.ConfigParseDiagnostic("x", None, None, "BAD", "bad", "fatal", False)


def test_diagnostic_envelope_preserves_control_fields_and_filters_arbitrary_objects(monkeypatch):
    mod = _module("contracts.diagnostics")
    class Opaque:
        def __repr__(self):
            pytest.fail("must not repr an opaque plugin object")
        def __str__(self):
            pytest.fail("must not stringify an opaque plugin object")
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("diagnostic must remain in memory"))
    provider = {"provider_code": "overloaded", "request_id": "req-1", "token": "example-secret",
                "prompt": "private full prompt", "plugin": Opaque()}
    diagnostic = mod.DiagnosticEnvelope("PROVIDER_BUSY", "provider", True, provider,
        {"code": -32603, "data": {"request_id": "mcp-1", "token": "other-secret", "plugin": Opaque()}},
        28, "request", "diag:one", False)
    data = diagnostic.to_dict()
    assert data["code"] == "PROVIDER_BUSY" and data["category"] == "provider"
    assert data["retryable"] is True and data["errno"] == 28 and data["diagnostic_ref"] == "diag:one"
    assert data["provider_error"] == {"provider_code": "overloaded", "request_id": "req-1"}
    assert data["mcp_error"] == {"code": -32603, "data": {"request_id": "mcp-1"}}
    serialized = json.dumps(data, ensure_ascii=False)
    assert "secret" not in serialized and "private full prompt" not in serialized
    provider["request_id"] = "mutated"
    assert diagnostic.to_dict()["provider_error"]["request_id"] == "req-1"
    data["provider_error"]["request_id"] = "mutated again"
    assert diagnostic.to_dict()["provider_error"]["request_id"] == "req-1"
    with pytest.raises(FrozenInstanceError):
        diagnostic.errno = 0


def test_diagnostic_total_utf8_bound_retains_errno_and_marks_truncation():
    mod = _module("contracts.diagnostics")
    assert mod.DIAGNOSTIC_DETAILS_MAX_BYTES == 8192
    diagnostic = mod.DiagnosticEnvelope("IO_ERROR", "io", False,
        {"provider_code": "code", "request_id": "汉" * 10000}, None, 28, "write", "diag:disk-full", False)
    data = diagnostic.to_dict()
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= mod.DIAGNOSTIC_DETAILS_MAX_BYTES
    assert data["details_truncated"] is True and data["errno"] == 28 and data["code"] == "IO_ERROR"
    assert data["operation"] == "write" and data["diagnostic_ref"] == "diag:disk-full"


def test_lossless_mcp_data_is_not_reported_as_truncated():
    mod = _module("contracts.diagnostics")
    mcp = {"code": -32603, "data": {"request_id": "r1", "errno": 28, "operation": "write"}}
    data = mod.DiagnosticEnvelope("MCP_ERROR", "mcp", False, mcp_error=mcp).to_dict()
    assert data["mcp_error"] == mcp
    assert data["details_truncated"] is False


@pytest.mark.parametrize("field", ["code", "category", "operation", "diagnostic_ref", "provider", "mcp"])
def test_diagnostic_redacts_bare_bearer_at_every_text_boundary(field):
    mod = _module("contracts.diagnostics")
    credential = "opaque-demo-credential"
    value = "invalid value: bEaReR " + credential
    kwargs = {"code": "ERROR", "category": "provider", "retryable": False}
    if field == "provider":
        kwargs["provider_error"] = {"request_id": value}
    elif field == "mcp":
        kwargs["mcp_error"] = {"code": -32603, "data": {"operation": value}}
    else:
        kwargs[field] = value
    diagnostic = mod.DiagnosticEnvelope(**kwargs)
    data = diagnostic.to_dict()
    assert credential not in json.dumps(data)
    assert data["details_truncated"] is True
    assert diagnostic.to_dict() == data  # Redaction must be idempotent.


@pytest.mark.parametrize("field", ["source", "code", "message"])
def test_config_diagnostic_redacts_bare_bearer_and_marks_value_redacted(field):
    mod = _module("settings.config_diagnostics")
    credential = "opaque-demo-credential"
    kwargs = {"source": "config.yaml", "line": None, "column": None, "code": "BAD_VALUE",
              "message": "invalid", "severity": "error", "value_redacted": False}
    kwargs[field] = "invalid value: Bearer " + credential
    data = asdict(mod.ConfigParseDiagnostic(**kwargs))
    assert credential not in json.dumps(data)
    assert data["value_redacted"] is True


def test_unicode_classification_is_lossless_when_total_budget_is_available():
    mod = _module("contracts.diagnostics")
    code, category = "错" * 50, "分类" * 50
    data = mod.DiagnosticEnvelope(code, category, False, errno=28).to_dict()
    assert data["code"] == code and data["category"] == category
    assert data["errno"] == 28 and data["details_truncated"] is False
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= mod.DIAGNOSTIC_DETAILS_MAX_BYTES


def test_classification_cannot_be_silently_rewritten_to_fit_the_budget():
    mod = _module("contracts.diagnostics")
    with pytest.raises(ValueError, match="control fields"):
        mod.DiagnosticEnvelope("错" * mod.DIAGNOSTIC_DETAILS_MAX_BYTES, "io", False, errno=28)
