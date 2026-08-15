from __future__ import annotations

import io
import logging

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
