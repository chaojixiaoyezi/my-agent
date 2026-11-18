"""W6 property test: verify TextDeltaBuffer + StreamWriter projector prefix safety."""
from __future__ import annotations

import http.client
import json
import random
from pathlib import Path

import pytest

from agent_py_agent.agent.common.log_redaction import (
    configured_secret_redaction,
    redact_sensitive_value,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.tests.test_gateway_identity_trust import _TOKEN, _server

_BEFORE_PROSE = "Introductory diagnostic summary before details. " * 5
_AFTER_PROSE = " Trailing verification prose after section ends." * 8
_CHUNK_MODES: tuple[int | str, ...] = (1, 2, 7, 33, 128, "random_1_64")
_STREAM_INCLUDE = frozenset({"assistant_text_delta", "assistant_text_reset", "assistant_text_done"})


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


def _commit_terminal(writer: BufferedChunkStreamWriter, *, call_id: str, full_text: str) -> None:
    writer.commit_model_response(
        {
            "schema": "assistant_messages.v1",
            "source": {"call_id": call_id, "request_id": "req-w6", "run_id": "run-w6"},
            "messages": [{"text": full_text}],
            "boundary": "terminal",
            "final_text": full_text,
        }
    )


def _assert_delta_events_satisfy_invariants(
    events: list[dict[str, object]],
    *,
    expected: str,
    forbidden_tokens: tuple[str, ...],
    strict_prefix: bool,
    forbid_reset: bool = False,
    surface: str,
    mode: int | str,
    require_done: bool = True,
) -> None:
    # Inspect every emitted delta event, including deltas followed by a reset,
    # as well as the cumulative text within each active segment before reset.
    text_events = [
        event
        for event in events
        if event.get("kind") in {"assistant_text_delta", "assistant_text_reset", "assistant_text_done"}
    ]
    if forbid_reset:
        assert not any(event.get("kind") == "assistant_text_reset" for event in text_events), (
            f"{surface} mode={mode} emitted unexpected assistant_text_reset"
        )
    active_segment = ""
    rendered = ""
    for event in text_events:
        kind = event["kind"]
        if kind == "assistant_text_reset":
            active_segment = ""
            rendered = ""
        elif kind == "assistant_text_delta":
            delta_text = str(event["text"])
            active_segment += delta_text
            rendered += delta_text
            for token in forbidden_tokens:
                assert token not in delta_text, f"{surface} mode={mode} leaked full token in delta"
                assert token not in active_segment, f"{surface} mode={mode} leaked full token in segment"
                if len(token) >= 6:
                    prefix = token[:6]
                    assert prefix not in delta_text, (
                        f"{surface} mode={mode} leaked >=6-char prefix {prefix!r} in delta"
                    )
                    assert prefix not in active_segment, (
                        f"{surface} mode={mode} leaked >=6-char prefix {prefix!r} in segment"
                    )
            if strict_prefix:
                assert expected.startswith(active_segment), (
                    f"{surface} mode={mode} emitted non-prefix delta segment len={len(active_segment)}"
                )

    if require_done:
        assert text_events and text_events[-1]["kind"] == "assistant_text_done"
        assert expected.startswith(active_segment)
        assert rendered == expected, f"{surface} mode={mode} rendered != project(full)"


def _assert_stream_prefix_safety(
    tmp_path: Path,
    *,
    snippet: str,
    forbidden_tokens: tuple[str, ...],
    holdback_chars: int,
    configured_secrets: tuple[str, ...] = (),
    identifiers: tuple[tuple[object, str], ...] = (),
    strict_prefix: bool = True,
    forbid_reset: bool = False,
) -> None:
    probe_writer = BufferedChunkStreamWriter(
        tmp_path / "probe.chunks.jsonl",
        delivery_channel="feishu",
        text_delta_holdback_chars=holdback_chars,
    )
    if identifiers:
        probe_writer.set_identifier_redactions(identifiers)
    project = probe_writer._project_stream_text
    full = _BEFORE_PROSE + snippet + _AFTER_PROSE

    with configured_secret_redaction(configured_secrets):
        expected = project(full)
        wire_expected = str(redact_sensitive_value(expected))
        for token in forbidden_tokens:
            assert token not in expected
            assert token not in wire_expected
            if len(token) >= 6:
                assert token[:6] not in expected
                assert token[:6] not in wire_expected

        for index, mode in enumerate(_CHUNK_MODES):
            call_id = f"call-w6-{holdback_chars}-{index}"
            buffer = TextDeltaBuffer(
                holdback_chars=holdback_chars,
                project=project,
                identifiers=probe_writer._identifier_redactions,
            )
            buffer_events = list(buffer.begin(call_id))
            for chunk in _chunks_for(full, mode):
                buffer_events.extend(buffer.append(chunk, model_call_id=call_id))
            buffer_events.extend(buffer.finish(call_id))
            _assert_delta_events_satisfy_invariants(
                buffer_events,
                expected=expected,
                forbidden_tokens=forbidden_tokens,
                strict_prefix=strict_prefix,
                forbid_reset=forbid_reset,
                surface="buffer",
                mode=mode,
            )

            chunk_path = tmp_path / f"writer_{holdback_chars}_{index}.chunks.jsonl"
            writer = BufferedChunkStreamWriter(
                chunk_path,
                delivery_channel="feishu",
                text_delta_holdback_chars=holdback_chars,
            )
            if identifiers:
                writer.set_identifier_redactions(identifiers)
            writer.set_verbose_level("stream")
            bound_write = writer.bind_model_call(call_id)
            for chunk in _chunks_for(full, mode):
                bound_write(chunk)
            _commit_terminal(writer, call_id=call_id, full_text=full)

            raw_chunk_text = chunk_path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                assert token not in raw_chunk_text
                if len(token) >= 6:
                    assert token[:6] not in raw_chunk_text
            chunk_rows = [json.loads(line) for line in raw_chunk_text.splitlines() if line.strip()]
            _assert_delta_events_satisfy_invariants(
                chunk_rows,
                expected=wire_expected,
                forbidden_tokens=forbidden_tokens,
                strict_prefix=strict_prefix,
                forbid_reset=forbid_reset,
                surface="chunk_jsonl",
                mode=mode,
            )

            public_events, _ = _read_public_progress_events(
                chunk_path,
                0,
                channel="feishu",
                include=_STREAM_INCLUDE,
            )
            _assert_delta_events_satisfy_invariants(
                public_events,
                expected=wire_expected,
                forbidden_tokens=forbidden_tokens,
                strict_prefix=strict_prefix,
                forbid_reset=forbid_reset,
                surface="http_public_extension",
                mode=mode,
            )


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
        strict_prefix=case_id not in {"cli_api_key", "cli_api_key_quoted_spaces"},
    )


@pytest.mark.parametrize("holdback_chars", [256, 64])
@pytest.mark.parametrize("item_repeats", [28, 70], ids=["body_166", "body_gt_256"])
def test_inline_function_xml_block_prefix_safety(
    tmp_path: Path, holdback_chars: int, item_repeats: int
) -> None:
    secret_arg = "InlineFuncArgSecret123456"
    body = secret_arg + " " + ("item " * item_repeats)
    if item_repeats == 70:
        assert len(body) > 256
    snippet = '<function name="read_file">' + body + "</function>"
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=snippet,
        forbidden_tokens=("<function", secret_arg),
        holdback_chars=holdback_chars,
        strict_prefix=True,
        forbid_reset=True,
    )


@pytest.mark.parametrize("holdback_chars", [256, 64])
def test_consecutive_inline_function_blocks_preserve_middle_prose_without_reset(
    tmp_path: Path, holdback_chars: int
) -> None:
    first_arg = "FirstInlineFuncSecret123456"
    second_arg = "SecondInlineFuncSecret789012"
    middle_prose = " Verified middle prose segment between tool blocks. " * 7
    snippet = (
        '<function name="read_file">' + first_arg + "</function>"
        + middle_prose
        + '<function name="read_file">' + second_arg + " " + ("item " * 70) + "</function>"
    )
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=snippet,
        forbidden_tokens=("<function", first_arg, second_arg),
        holdback_chars=holdback_chars,
        strict_prefix=True,
        forbid_reset=True,
    )


@pytest.mark.parametrize("holdback_chars", [256, 64])
@pytest.mark.parametrize(
    ("secret", "configured_secrets"),
    [
        (
            "opaque configured value without recognized shape",
            ("opaque configured value without recognized shape",),
        ),
        (
            "opaque configured multiword value " + ("segment " * 36) + "tail",
            ("opaque configured multiword value " + ("segment " * 36) + "tail",),
        ),
        (
            "shared configured phrase alpha beta extra",
            ("shared configured phrase alpha", "alpha beta extra"),
        ),
    ],
    ids=["multiword_standard", "multiword_gt_256", "overlapping_multiword"],
)
def test_configured_multiword_secret_prefix_safety(
    tmp_path: Path,
    holdback_chars: int,
    secret: str,
    configured_secrets: tuple[str, ...],
) -> None:
    _assert_stream_prefix_safety(
        tmp_path,
        snippet=secret,
        forbidden_tokens=configured_secrets,
        holdback_chars=holdback_chars,
        configured_secrets=configured_secrets,
        strict_prefix=True,
        forbid_reset=True,
    )


def _http_json(
    port: int,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {
        "Content-Type": "application/json",
        "X-Gateway-Token": _TOKEN,
        "X-Channel": "feishu",
        "X-User-Id": "w6-stream-viewer",
    }
    try:
        conn.request(
            "GET" if body is None else "POST",
            path,
            None if body is None else json.dumps(body),
            headers,
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_stream_prefix_safety_over_real_http_progress_wire(tmp_path: Path) -> None:
    secret_phrase = "opaque configured value without recognized shape"
    func_secret = "InlineFuncWireSecret123456"
    long_func = '<function name="read_file">' + func_secret + " " + ("item " * 70) + "</function>"
    assert len(long_func) > 256

    cases = (
        ("configured_multiword", secret_phrase, (secret_phrase,), (secret_phrase,)),
        ("inline_function_gt_256", long_func, ("<function", func_secret), ()),
    )

    server = _server(tmp_path)
    server.start()
    try:
        for case_name, snippet, forbidden_tokens, configured_secrets in cases:
            full = _BEFORE_PROSE + snippet + _AFTER_PROSE
            for mode_index, mode in enumerate(_CHUNK_MODES):
                status, receipt = _http_json(
                    server.port,
                    "/ask",
                    {"goal": f"verify-{case_name}-{mode_index}", "verbose_level": "stream"},
                )
                assert status == 202
                request_id = str(receipt["request_id"])
                chunk_path = gateway_chunk_path(server.paths, request_id)
                writer = BufferedChunkStreamWriter(chunk_path, delivery_channel="feishu")
                assert writer.text_delta_holdback_chars == 256
                writer.set_verbose_level("stream")
                call_id = f"call-wire-{case_name}-{mode_index}"

                chunks = _chunks_for(full, mode)
                midpoint = max(1, len(chunks) // 2)
                with configured_secret_redaction(configured_secrets):
                    expected = writer._project_stream_text(full)
                    bound_write = writer.bind_model_call(call_id)
                    for chunk in chunks[:midpoint]:
                        bound_write(chunk)

                mid_code, mid_payload = _http_json(
                    server.port,
                    f"/progress/{request_id}?include=assistant_text_delta,assistant_text_reset,assistant_text_done",
                )
                assert mid_code == 200
                mid_events = list(mid_payload["events"])
                _assert_delta_events_satisfy_invariants(
                    mid_events,
                    expected=expected,
                    forbidden_tokens=forbidden_tokens,
                    strict_prefix=True,
                    surface=f"http_wire_mid:{case_name}",
                    mode=mode,
                    require_done=False,
                )

                with configured_secret_redaction(configured_secrets):
                    for chunk in chunks[midpoint:]:
                        bound_write(chunk)
                    _commit_terminal(writer, call_id=call_id, full_text=full)
                writer.close()

                end_code, end_payload = _http_json(
                    server.port,
                    f"/progress/{request_id}?include=assistant_text_delta,assistant_text_reset,assistant_text_done",
                )
                assert end_code == 200
                end_events = list(end_payload["events"])
                assert not any(ev.get("kind") == "assistant_text_reset" for ev in end_events)
                _assert_delta_events_satisfy_invariants(
                    end_events,
                    expected=expected,
                    forbidden_tokens=forbidden_tokens,
                    strict_prefix=True,
                    surface=f"http_wire_final:{case_name}",
                    mode=mode,
                )
    finally:
        server.stop()


def test_stream_writer_keeps_256_holdback_default(tmp_path: Path) -> None:
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl")
    assert writer.text_delta_holdback_chars == 256
    assert writer._text_delta.holdback_chars == 256
    assert writer._text_delta.project == writer._project_stream_text
