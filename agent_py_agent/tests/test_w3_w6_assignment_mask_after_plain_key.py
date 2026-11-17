"""A plain key right before a credential assignment must not hide that assignment from the mask.

Live clone (2026-11-17, W6 host read under Full Access): the value of ``api_key:`` came back unmasked.  The
generic assignment pattern took the preceding plain key (a line number ``2``, ``msg``, a YAML parent key) as
the key and the credential key itself as that key's value, so the scan resumed after ``api_key:`` and never
reached the value.
"""
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
from agent_py_agent.agent.tooling.host_output_redaction import redact_host_text
from agent_py_agent.agent.tooling.mcp_client import sanitize_credentials

VALUE = "W6probe" + "Value8c1d2e3f4a5b6"


@pytest.mark.parametrize("text", [
    f"2: api_key: {VALUE}",
    f"     2: api_key: {VALUE}",
    f"msg: token={VALUE}",
    f"cfg.x: secret: {VALUE}",
    f"2: password = {VALUE}",
    f"service:\n  api_key: {VALUE}",
    f"note=ok DB_PASSWORD={VALUE}",
])
def test_credential_assignment_after_a_plain_key_is_masked(text: str) -> None:
    assert VALUE not in redact_sensitive_text(text)
    assert VALUE not in sanitize_credentials(text)
    assert VALUE not in redact_host_text(text)


@pytest.mark.parametrize("text", [
    "2: max_tokens: 4096",
    "token_count: 3",
    "password_hint: blue",
    "secretary: bob",
    "export TOKEN=$TOKEN",
    "2: api_key: $API_KEY",
])
def test_plain_keys_and_pure_variable_references_stay_unchanged(text: str) -> None:
    assert redact_sensitive_text(text) == text


# Same loose absolute bound as the rejectdiag4 guard in test_log_redaction.py: it only catches
# catastrophic backtracking, not small speed changes.
@pytest.mark.parametrize("shape", [
    lambda n: " " + "x_" * (n // 2) + ": v",
    lambda n: "k: " * (n // 3),
    lambda n: "a:" * (n // 2),
    lambda n: " " + "_" * n + "password",
    lambda n: "2: " * (n // 3) + "api_key",
])
def test_credential_key_pattern_stays_linear_on_pathological_input(shape) -> None:
    started = time.perf_counter()
    redact_sensitive_text(shape(100_000))
    assert time.perf_counter() - started < 5.0
