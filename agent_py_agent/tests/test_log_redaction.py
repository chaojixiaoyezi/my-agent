from __future__ import annotations

import io
import logging

import pytest

from agent_py_agent.agent.common.log_redaction import (
    install_log_redaction,
    redact_sensitive_text,
    redact_sensitive_value,
)


def test_redacts_sensitive_url_query_without_hiding_operational_fields() -> None:
    raw = (
        "connected to wss://msg.example/ws?fpid=493&access_key=opaque-value-123456"
        "&service_id=3355&ticket=ticket-value-654321 [conn_id=42]"
    )

    safe = redact_sensitive_text(raw)

    assert "opaque-value-123456" not in safe
    assert "ticket-value-654321" not in safe
    assert "access_key=<redacted>" in safe
    assert "ticket=<redacted>" in safe
    assert "fpid=493" in safe
    assert "service_id=3355" in safe
    assert "conn_id=42" in safe


def test_process_log_factory_redacts_formatted_args_and_nested_secret_fields() -> None:
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("test.my_agent.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "sdk=%s payload=%s count=%d",
        "wss://example/ws?access_key=secret-a&ticket=secret-b",
        {"access_token": "secret-c", "token_count": 17},
        3,
    )

    rendered = stream.getvalue()
    assert "secret-a" not in rendered
    assert "secret-b" not in rendered
    assert "secret-c" not in rendered
    assert "'access_token': '<redacted>'" in rendered
    assert "'token_count': 17" in rendered
    assert "count=3" in rendered


def test_log_redaction_install_is_idempotent() -> None:
    install_log_redaction()
    first = logging.getLogRecordFactory()
    install_log_redaction()
    assert logging.getLogRecordFactory() is first


def test_recursive_redaction_masks_nested_credential_fields() -> None:
    payload = {
        "result": {
            "api_key": "opaque-value",
            "nested": [{"authorization": "Bearer live-token"}, {"count": 2}],
        }
    }

    safe = redact_sensitive_value(payload)

    assert safe["result"]["api_key"] == "<redacted>"
    assert safe["result"]["nested"][0]["authorization"] == "<redacted>"
    assert safe["result"]["nested"][1]["count"] == 2


def test_code_file_mode_preserves_placeholders_but_redacts_real_tokens() -> None:
    source = (
        'MAX_TOKENS=8000\napi_key = os.getenv("API_KEY")\n'
        'fixture = "sk-abcdefghij123456"\n'
    )

    safe = redact_sensitive_text(source, code_file=True)

    assert "MAX_TOKENS=8000" in safe
    assert 'api_key = os.getenv("API_KEY")' in safe
    assert "sk-abcdefghij123456" not in safe
    assert "<redacted>" in safe


@pytest.mark.parametrize("source", [
    'axios.post(`${base}/incoming?token=${notification.token}`, data, config);',
    'axios.post(`${base}?token=${encodeURIComponent(notification.token)}&page=1`, data);',
    'axios.get(`${base}?apikey=${config["token"]}&message=${message}`, config);',
    'url = f"https://example.test?token={config[\'token\']}&page=1"\nnext_step()',
    'url = f"https://example.test?token={quote(token)}&page=1"',
    'url = "https://example.test?token={token}".format(token=token)',
    'url := fmt.Sprintf("https://example.test?token=%s&page=1", token)',
    'url := fmt.Sprintf(`https://example.test?token=%[1]s`, token)',
    'url = "https://example.test?token=%(token)s" % values',
    'dsn = f"postgresql://{user}:{password}@{host}/db"',
    'dsn = `postgresql://${user}:${encodeURIComponent(password)}@${host}/db`;',
    'dsn := fmt.Sprintf("postgres://%s:%s@host/db", user, password)',
    'url = f"https://{user}:{password}@host/path"',
])
def test_source_url_references_preserve_complete_syntax(source: str) -> None:
    assert redact_sensitive_text(source, code_file=True) == source


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_source_url_literal_secrets_are_masked_without_swallowing_delimiters(quote: str) -> None:
    source = f"use({quote}https://host/path?token=opaque-secret{quote}, config)\nnext_step()"
    expected = source.replace("opaque-secret", "<redacted>")
    assert redact_sensitive_text(source, code_file=True) == expected


@pytest.mark.parametrize("source,expected", [
    ('`?token=${token}&api_key=live-secret`', '`?token=${token}&api_key=<redacted>`'),
    ('`?token=live-secret${token}`', '`?token=<redacted>${token}`'),
    ('`?token=${token}live-secret`', '`?token=${token}<redacted>`'),
    ('`?token=${"live-secret"}`', '`?token=${"<redacted>"}`'),
    ("f'?token={\"live-secret\"}'", "f'?token={\"<redacted>\"}'"),
    ('`?token=${encodeURIComponent("live-secret")}`', '`?token=${encodeURIComponent("<redacted>")}`'),
    ('`https://${"user:pass"}@host/path`', '`https://${"<redacted>"}@host/path`'),
    ('f"postgres://{user}:{\'live-secret\'}@host/db"', 'f"postgres://{user}:{\'<redacted>\'}@host/db"'),
    ('"https://user:live-secret@host/path"', '"https://user:<redacted>@host/path"'),
    ('"postgres://user:live-secret@host/db"', '"postgres://user:<redacted>@host/db"'),
])
def test_source_dynamic_and_literal_credentials_are_handled_separately(source: str, expected: str) -> None:
    assert redact_sensitive_text(source, code_file=True) == expected


def test_source_redaction_keeps_known_key_scan_inside_preserved_expressions() -> None:
    source = '`?token=${config["sk-abcdefghij123456"]}`\nAuthorization: Bearer sk-abcdefghij123456'
    safe = redact_sensitive_text(source, code_file=True)
    assert 'sk-abcdefghij123456' not in safe
    assert safe.startswith('`?token=${config["<redacted>"]}`\n')


@pytest.mark.parametrize("source", [
    'url = "postgres://user:password"\n@decorator\ndef f(): pass',
    'url = "https://host/path:user@example.test"',
    'url = "https://host/path?q=user:password@example.test"',
    'url = "postgres://host/db?q=user:password@example.test"',
    'url = "https://host/path?token=" + token\nnext_step()',
])
def test_url_redaction_does_not_cross_code_or_authority_boundaries(source: str) -> None:
    assert redact_sensitive_text(source, code_file=True) == source


def test_log_mode_does_not_exempt_source_looking_credential_values() -> None:
    safe = redact_sensitive_text('https://host?token=${opaque_token}&page=1')
    assert 'opaque_token' not in safe
    assert 'page=1' in safe


def test_many_source_slots_without_authority_terminator_do_not_backtrack() -> None:
    source = 'url = "https://user:' + '${token}' * 1000 + '"\nnext_step()'
    assert redact_sensitive_text(source, code_file=True) == source


def test_model_tool_output_projection_preserves_source_and_masks_literal_secrets() -> None:
    from agent_py_agent.agent.tooling.output_projection import model_tool_output_body

    source = '29: axios.post(`${base}?token=${encodeURIComponent(config.token)}&api_key=live-secret`, data);'
    projected = model_tool_output_body(
        tool="read_file", output=source,
        result_envelope={"tool_output_policy": {"trust": "runtime", "redaction": "source_code"}},
    )
    assert projected == source.replace("live-secret", "<redacted>")


def test_source_private_keys_and_nested_quoted_credentials_still_masked() -> None:
    source = (
        '`?token=${encodeURIComponent("live-secret")}`\n'
        '-----BEGIN PRIVATE KEY-----\nopaque-key-material\n-----END PRIVATE KEY-----'
    )
    safe = redact_sensitive_text(source, code_file=True)
    assert safe == '`?token=${encodeURIComponent("<redacted>")}`\n<redacted-private-key>'


@pytest.mark.parametrize("secret", ["abc,def", "abc'def", 'abc"def', 'abc`def', 'abc(def)', 'abc[def]'])
def test_log_query_credentials_do_not_leak_after_source_delimiters(secret: str) -> None:
    safe = redact_sensitive_text(f'GET https://host/path?token={secret}&page=1 status=200')
    assert safe == 'GET https://host/path?token=<redacted>&page=1 status=200'


@pytest.mark.parametrize("secret", ["abc,def", "abc'def", 'abc"def', 'abc`def', 'abc(def)', 'abc[def]'])
@pytest.mark.parametrize("scheme", ["https", "postgres"])
def test_log_userinfo_credentials_do_not_leak_after_source_delimiters(secret: str, scheme: str) -> None:
    safe = redact_sensitive_text(f'{scheme}://user:{secret}@host/path status=200')
    assert safe == f'{scheme}://user:<redacted>@host/path status=200'
