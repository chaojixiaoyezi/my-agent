from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from agent.ingestion.source_http import (
    normalize_source_http_request,
    normalize_top_level_response_field,
    public_source_envelope,
    render_source_http_request,
)


def test_top_level_response_field_accepts_only_equivalent_single_level_jsonpath():
    assert normalize_top_level_response_field("$.items[]") == "items"
    assert normalize_top_level_response_field("$.items[*]") == "items"
    assert normalize_top_level_response_field("$.next_cursor") == "next_cursor"
    assert normalize_top_level_response_field("literal.field") == "literal.field"
    with pytest.raises(ValueError, match="顶层字段"):
        normalize_top_level_response_field("$.payload.items[]")


def test_arbitrary_query_names_are_learned_from_placeholders():
    url, request = normalize_source_http_request(
        "https://example.test/feed?tenant=alpha&after=<next>&batch=<limit>",
        None,
        poll=False,
    )

    assert url == "https://example.test/feed?tenant=alpha"
    assert request["cursor_binding"]["name"] == "after"
    assert request["page_size_binding"]["name"] == "batch"

    rendered = render_source_http_request(
        url,
        request,
        cursor=27,
        page_size=80,
    )
    query = parse_qs(urlsplit(rendered.url).query)
    assert query == {"tenant": ["alpha"], "after": ["27"], "batch": ["80"]}


def test_parameter_names_are_never_inferred_from_common_words():
    with pytest.raises(ValueError, match="cursor_binding"):
        normalize_source_http_request(
            "https://example.test/feed?since=0&limit=50",
            None,
            poll=False,
        )


def test_post_json_cursor_and_page_size_use_declared_nested_paths():
    url, request = normalize_source_http_request(
        "https://example.test/search?tenant=alpha",
        {
            "method": "POST",
            "headers": {"Accept": "application/json"},
            "json_body": {"page": {}, "filter": {"enabled": True}},
            "cursor_binding": {
                "location": "json_body",
                "path": ["page", "token"],
                "initial": 4,
            },
            "page_size_binding": {
                "location": "json_body",
                "path": ["page", "size"],
            },
        },
        poll=False,
    )

    rendered = render_source_http_request(url, request, cursor=19, page_size=30)
    assert rendered.method == "POST"
    assert json.loads(rendered.data or b"{}") == {
        "page": {"token": 19, "size": 30},
        "filter": {"enabled": True},
    }
    assert rendered.headers["Content-Type"] == "application/json"


def test_secret_refs_resolve_only_for_transient_request_and_are_redacted(monkeypatch):
    monkeypatch.setenv("AUDIT_TEST_BEARER", "Bearer real-secret-value")
    url, request = normalize_source_http_request(
        "https://example.test/feed?cursor=<next>",
        {
            "headers": {"X-Tenant": "alpha"},
            "secret_bindings": [
                {
                    "location": "header",
                    "name": "Authorization",
                    "secret_ref": "env:AUDIT_TEST_BEARER",
                }
            ],
        },
        poll=False,
    )

    assert "real-secret-value" not in json.dumps(request)
    shown = public_source_envelope({"request": request})
    shown_text = json.dumps(shown)
    assert "AUDIT_TEST_BEARER" not in shown_text
    assert "real-secret-value" not in shown_text
    assert shown["request"]["secret_bindings"] == [
        {"location": "header", "name": "Authorization", "configured": True}
    ]

    rendered = render_source_http_request(url, request, cursor=2, page_size=None)
    assert rendered.headers["Authorization"] == "Bearer real-secret-value"


def test_plain_sensitive_headers_are_rejected():
    with pytest.raises(ValueError, match="不能保存明文"):
        normalize_source_http_request(
            "https://example.test/feed?cursor=<next>",
            {"headers": {"Authorization": "Bearer leaked"}},
            poll=False,
        )


def test_poll_is_explicit_and_cannot_carry_cursor_bindings():
    with pytest.raises(ValueError, match="poll"):
        normalize_source_http_request(
            "https://example.test/snapshot?cursor=<next>",
            None,
            poll=True,
        )

    url, request = normalize_source_http_request(
        "https://example.test/snapshot?tenant=alpha",
        {"method": "POST", "json_body": {"scope": "current"}},
        poll=True,
    )
    rendered = render_source_http_request(url, request, cursor=None, page_size=None)
    assert rendered.method == "POST"
    assert json.loads(rendered.data or b"{}") == {"scope": "current"}


def test_explicit_cursor_offset_is_applied_but_never_below_initial():
    url, request = normalize_source_http_request(
        "https://example.test/feed",
        {
            "cursor_binding": {
                "location": "query",
                "name": "after",
                "initial": 0,
                "offset": -1,
            }
        },
        poll=False,
    )

    first = render_source_http_request(url, request, cursor=0, page_size=None)
    later = render_source_http_request(url, request, cursor=9, page_size=None)
    assert parse_qs(urlsplit(first.url).query)["after"] == ["0"]
    assert parse_qs(urlsplit(later.url).query)["after"] == ["8"]


def test_unknown_request_fields_fail_closed():
    with pytest.raises(ValueError, match="未知字段"):
        normalize_source_http_request(
            "https://example.test/feed?cursor=<next>",
            {"vendor_type": "waf"},
            poll=False,
        )
