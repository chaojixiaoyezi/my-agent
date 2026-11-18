"""W6 property test: verify TextDeltaBuffer + StreamWriter projector prefix safety."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from agent_py_agent.agent.common.log_redaction import configured_secret_redaction
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

_BEFORE_PROSE = "Introductory diagnostic summary before details. " * 5
_AFTER_PROSE = " Trailing verification prose after section ends." * 8
_CHUNK_MODES: tuple[int | str, ...] = (1, 2, 7, 33, 128, "random_1_64")


def _chunks_for(text: str, mode: int | str) -> list[str]:
    if isinstance(mode, int):
        return [text[offset : offset + mode] for offset in range(0, len(text), mode)]
    rng = random.Random(1118)
    chunks: list[str] = []
    offset = 0
    while offset < len(text):
        width = rng.randint(1, 64)
        chunks.append(text[offset : offset + width])
        offset += width
    return chunks


def _assert_stream_prefix_safety(
    tmp_path: Path,
    *,
    snippet: str,
    forbidden_tokens: tuple[str, ...],
    holdback_chars: int,
    configured_secrets: tuple[str, ...] = (),
    identifiers: tuple[tuple[object, str], ...] = (),
) -> None:
    writer = BufferedChunkStreamWriter(tmp_path / "stream.chunks.jsonl", delivery_channel="feishu")
    if identifiers:
        writer.set_identifier_redactions(identifiers)
    project = writer._project_stream_text
    full = _BEFORE_PROSE + snippet + _AFTER_PROSE

    with configured_secret_redaction(configured_secrets):
        expected = project(full)
        for token in forbidden_tokens:
            assert token not in expected
            if len(token) >= 6:
                assert token[:6] not in expected

        for mode in _CHUNK_MODES:
            buffer = TextDeltaBuffer(
                holdback_chars=holdback_chars,
                project=project,
                identifiers=writer._identifier_redactions,
            )
            events = list(buffer.begin("call-w6"))
            for chunk in _chunks_for(full, mode):
                events.extend(buffer.append(chunk, model_call_id="call-w6"))
            events.extend(buffer.finish("call-w6"))

            # Inspect every emitted delta, including deltas later retracted by reset,
            # as well as the cumulative text within each active segment before reset.
            active_segment = ""
            rendered = ""
            for event in events:
                kind = event["kind"]
                if kind == "assistant_text_reset":
                    active_segment = ""
                    rendered = ""
                elif kind == "assistant_text_delta":
                    delta_text = str(event["text"])
                    active_segment += delta_text
                    rendered += delta_text
                    for token in forbidden_tokens:
                        assert token not in delta_text, f"mode={mode} leaked full token in delta"
                        assert token not in active_segment, f"mode={mode} leaked full token in segment"
                        if len(token) >= 6:
                            prefix = token[:6]
                            assert prefix not in delta_text, (
                                f"mode={mode} leaked >=6-char prefix {prefix!r} in delta: {delta_text!r}"
                            )
                            assert prefix not in active_segment, (
                                f"mode={mode} leaked >=6-char prefix {prefix!r} in segment: {active_segment!r}"
                            )

            assert events[-1]["kind"] == "assistant_text_done"
            assert rendered == expected, f"mode={mode} rendered != project(full)"


_CORPUS_CASES: list[tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[tuple[object, str], ...]]] = [
    (
        "api_key_assignment",
        "api_key=sk-" + "A1b2C3d4E5" * 4,
        ("sk-" + "A1b2C3d4E5" * 4,),
        (),
        (),
    ),
    (
        "json_password_field",
        '{"password": "JsonSecretPasswordValue20261118Alpha"}',
        ("JsonSecretPasswordValue20261118Alpha",),
        (),
        (),
    ),
    (
        "json_password_with_spaces",
        '{"password": "json credential phrase with spaces Alpha123"}',
        ("json credential phrase with spaces Alpha123",),
        (),
        (),
    ),
    (
        "token_single_quoted",
        "token = 'TokSingleQuote9876543210ZyXwVuTsRqPoNm'",
        ("TokSingleQuote9876543210ZyXwVuTsRqPoNm",),
        (),
        (),
    ),
    (
        "token_single_quoted_with_spaces",
        "token = 'single quote credential phrase with spaces Beta456'",
        ("single quote credential phrase with spaces Beta456",),
        (),
        (),
    ),
    (
        "authorization_bearer_jwt",
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        ),
        (),
        (),
    ),
    (
        "cookie_header",
        "Cookie: a=b; session=CookieSessionSecretAbCdEf1234567890",
        ("CookieSessionSecretAbCdEf1234567890",),
        (),
        (),
    ),
    (
        "pem_private_key",
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDPemSecretLine1234567890\n"
        "-----END PRIVATE KEY-----",
        (
            "-----BEGIN PRIVATE KEY-----",
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDPemSecretLine1234567890",
        ),
        (),
        (),
    ),
    (
        "url_userinfo",
        "https://alice:UrlPassSecret987654321@example.internal/x",
        ("UrlPassSecret987654321",),
        (),
        (),
    ),
    (
        "cli_api_key",
        "mycli --api-key sk-CliSecretKey1234567890AbCdEfGhIjKlMnOp",
        ("sk-CliSecretKey1234567890AbCdEfGhIjKlMnOp",),
        (),
        (),
    ),
    (
        "cli_api_key_quoted_spaces",
        'mycli --api-key "CliQuotedCredential phrase with spaces exceeding sixty four chars"',
        ("CliQuotedCredential phrase with spaces exceeding sixty four chars",),
        (),
        (),
    ),
    (
        "configured_secret_single_token",
        "opaque-private-synthetic-value-987654321",
        ("opaque-private-synthetic-value-987654321",),
        ("opaque-private-synthetic-value-987654321",),
        (),
    ),
    (
        "host_path_users",
        "/Users/alice_developer/workspace/private_repo/credentials.txt",
        ("/Users/alice_developer/workspace/private_repo/", "/Users/"),
        (),
        (),
    ),
    (
        "bracket_tool_block",
        "\n[TOOL_CALL]\nrun_command BracketToolArgSecret123456\n[/TOOL_CALL]\n",
        ("[TOOL_CALL]", "BracketToolArgSecret123456"),
        (),
        (),
    ),
    (
        "xml_tool_call_block",
        "\n<tool_call>\n<name>read_file</name><arg>XmlToolArgSecret123456</arg>\n</tool_call>\n",
        ("<tool_call>", "XmlToolArgSecret123456"),
        (),
        (),
    ),
    (
        "xml_direct_tool_block",
        "\n<read_file>\n<path>DirectXmlArgSecret123456</path>\n</read_file>\n",
        ("<read_file>", "DirectXmlArgSecret123456"),
        (),
        (),
    ),
    (
        "xml_bare_function_block",
        '\n<function=read_file>\n<parameter=path>BareFuncArgSecret123456</parameter>\n</function>\n',
        ("<function=read_file>", "BareFuncArgSecret123456"),
        (),
        (),
    ),
    (
        "structured_identifier",
        "om_synthetic_private_thread_id_987654321",
        ("om_synthetic_private_thread_id_987654321",),
        (),
        (("om_synthetic_private_thread_id_987654321", "当前会话"),),
    ),
]


@pytest.mark.parametrize("holdback_chars", [256, 64])
@pytest.mark.parametrize(
    ("case_id", "snippet", "forbidden_tokens", "configured_secrets", "identifiers"),
    _CORPUS_CASES,
    ids=[case[0] for case in _CORPUS_CASES],
)
def test_text_delta_corpus_prefix_safety(
    tmp_path: Path,
    holdback_chars: int,
    case_id: str,
    snippet: str,
    forbidden_tokens: tuple[str, ...],
    configured_secrets: tuple[str, ...],
    identifiers: tuple[tuple[object, str], ...],
) -> None:
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=snippet,
        forbidden_tokens=forbidden_tokens,
        holdback_chars=holdback_chars,
        configured_secrets=configured_secrets,
        identifiers=identifiers,
    )


@pytest.mark.parametrize(
    "holdback_chars",
    [
        256,
        pytest.param(
            64,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "user_visible_text._XML_TOOL_BLOCK_RE removes inline <function ...>...</function>, "
                    "while _UNCLOSED_XML_TOOL_BLOCK_RE omits 'function', _BARE_FUNCTION_BLOCK_RE requires '=', "
                    "and _LINE_LEADING_MARKER_RE requires line start; at holdback_chars=64 an unclosed "
                    "inline <function> body (>64 chars) leaks its prefix before </function> arrives"
                ),
            ),
        ),
    ],
)
def test_inline_function_xml_block_prefix_safety(tmp_path: Path, holdback_chars: int) -> None:
    secret_arg = "InlineFuncArgSecret123456"
    snippet = '<function name="read_file">' + secret_arg + " " + ("item " * 28) + "</function>"
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=snippet,
        forbidden_tokens=("<function", secret_arg),
        holdback_chars=holdback_chars,
    )


@pytest.mark.parametrize(
    "holdback_chars",
    [
        pytest.param(
            256,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "_safe_cut does not inspect _CONFIGURED_SECRET_VALUES and stops backing off at spaces; "
                    "multi-word configured secrets leak prefixes when a flush boundary lands inside the span"
                ),
            ),
        ),
        pytest.param(
            64,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "_safe_cut does not inspect _CONFIGURED_SECRET_VALUES and stops backing off at spaces; "
                    "multi-word configured secrets leak prefixes at holdback_chars=64"
                ),
            ),
        ),
    ],
)
def test_configured_multiword_secret_prefix_safety(tmp_path: Path, holdback_chars: int) -> None:
    secret = "opaque configured value without recognized shape"
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=secret,
        forbidden_tokens=(secret,),
        holdback_chars=holdback_chars,
        configured_secrets=(secret,),
    )


def test_stream_writer_keeps_256_holdback_default(tmp_path: Path) -> None:
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl")
    assert writer.text_delta_holdback_chars == 256
    assert writer._text_delta.holdback_chars == 256
    assert writer._text_delta.project == writer._project_stream_text
