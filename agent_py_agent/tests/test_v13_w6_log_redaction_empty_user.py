"""空用户名连接串：两条脱敏入口、实际日志组合和保留诊断信息。"""
import io
import logging
import time

import pytest

from agent_py_agent.agent.common.log_redaction import (
    RedactingFormatter,
    install_log_redaction,
    redact_sensitive_text,
)


@pytest.mark.parametrize("scheme", ["redis", "http"])
@pytest.mark.parametrize("user", ["", "user"])
@pytest.mark.parametrize("code_file", [False, True])
def test_url_literal_is_redacted(scheme, user, code_file):
    url = f"{scheme}://{user}:SYNTHETIC_ONLY_56@example.invalid/0"
    assert redact_sensitive_text(url, code_file=code_file) == (
        f"{scheme}://{user}:<redacted>@example.invalid/0"
    )


@pytest.mark.parametrize("user", ["", "user"])
def test_source_interpolation_keeps_shell_but_masks_literal(user):
    value = f"redis://{user}:${{encodeURIComponent('SYNTHETIC_ONLY_56')}}@example.invalid/0"
    assert redact_sensitive_text(value, code_file=True) == (
        f"redis://{user}:${{encodeURIComponent('<redacted>')}}@example.invalid/0"
    )


@pytest.mark.parametrize("user", ["", "user"])
def test_source_variable_reference_unchanged(user):
    value = f"redis://{user}:${{PASSWORD}}@example.invalid/0"
    assert redact_sensitive_text(value, code_file=True) == value


@pytest.mark.parametrize("user", ["", "user"])
@pytest.mark.parametrize("traceback", [False, True])
def test_log_factory_and_formatter_cover_arguments_and_traceback(user, traceback):
    original = logging.getLogRecordFactory()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.Logger("SYNTHETIC_ONLY_56", level=logging.INFO)
    logger.addHandler(handler)
    url = f"redis://{user}:SYNTHETIC_ONLY_56@example.invalid/0"
    try:
        install_log_redaction()
        if traceback:
            try:
                raise ValueError(url)
            except ValueError:
                logger.exception("connection failed")
        else:
            logger.info("connection %s", url)
        text = output.getvalue()
        assert "SYNTHETIC_ONLY_56" not in text
        assert f"redis://{user}:<redacted>@example.invalid/0" in text
        if traceback:
            assert "Traceback" in text and "ValueError" in text
    finally:
        logging.setLogRecordFactory(original)
        logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("code_file", [False, True])
@pytest.mark.parametrize("url", ["http://example.invalid/path", "redis://:@example.invalid/0"])
def test_absent_or_empty_password_preserves_url(url, code_file):
    assert redact_sensitive_text(url, code_file=code_file) == url


@pytest.mark.parametrize("code_file", [False, True])
def test_percent_encoded_password_is_hidden(code_file):
    value = "redis://:SYNTHETIC_ONLY_56%40%3A@example.invalid:6379/path"
    assert redact_sensitive_text(value, code_file=code_file) == (
        "redis://:<redacted>@example.invalid:6379/path"
    )


@pytest.mark.parametrize("code_file", [False, True])
def test_long_unmatched_text_is_bounded(code_file):
    value = "redis://:" + "x" * 100_000
    start = time.perf_counter()
    actual = redact_sensitive_text(value, code_file=code_file)
    elapsed = time.perf_counter() - start
    assert actual == value
    assert elapsed < 0.25
