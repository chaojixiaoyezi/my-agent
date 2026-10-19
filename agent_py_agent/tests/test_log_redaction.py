from __future__ import annotations

import io
import json
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


# LLM: rejectdiag3：authorization 补进文本清洗词表（rejectdiag2 初审实测 JSON 形状漏遮）；覆盖
#   JSON 双/单引号键、等号赋值与无引号 Bearer 头三种形状；authorization_mode、unauthorized 这类
#   不以凭据词结尾的键名不受影响（后缀判定口径不变）。
# 函数用途: 钉住 authorization 词表的覆盖范围与误伤边界。
def test_authorization_key_shapes_masked_without_false_positives() -> None:
    for raw, secret in (
        ('{"authorization": "Bearer JSONSECRETTOKEN000"}', "JSONSECRETTOKEN000"),
        ("{'authorization': 'Bearer SINGLESECRETTOKEN01'}", "SINGLESECRETTOKEN01"),
        ("authorization=BASICSECRETVALUE0002", "BASICSECRETVALUE0002"),
        ("Authorization: Bearer PLAINBEARERSECRET0003", "PLAINBEARERSECRET0003"),
    ):
        assert secret not in redact_sensitive_text(raw), raw
    kept = redact_sensitive_text("authorization_mode=verbose unauthorized=true my_authorized=yes")
    assert "authorization_mode=verbose" in kept
    assert "unauthorized=true" in kept
    assert "my_authorized=yes" in kept


# LLM: rejectdiag4 必须改 1：授权头任意方案（Basic/Token/Digest/Bearer）及 Proxy-Authorization 的整个凭据都要遮；
#   方案名保留，值里可能有空格、逗号和（转义）引号。普通模式与诊断摘要的 labels 模式都要遮住，且 JSON 仍可解析。
# 函数用途: 钉住授权头各方案、各引号形状的凭据清洗。
@pytest.mark.parametrize("labels", [False, True])
@pytest.mark.parametrize("text,secret", [
    ("Authorization: Basic dXNlcjpQQVNTV09SRDEyMzQ=", "dXNlcjpQQVNTV09SRDEyMzQ"),
    ("Authorization: Token TOKENSECRETVALUE77", "TOKENSECRETVALUE77"),
    ('Authorization: Digest username="alice", realm="api", response="DIGESTRESPONSE9f8e7d"', "DIGESTRESPONSE9f8e7d"),
    ("proxy-authorization: Basic PROXYSECRET0123456789", "PROXYSECRET0123456789"),
    ("{'Proxy-Authorization': 'Basic PYREPRSECRET77'}", "PYREPRSECRET77"),
    (json.dumps({"authorization": 'Digest username="alice", response="DIGESTJSONSECRET42"'}), "DIGESTJSONSECRET42"),
    ('curl -H "Authorization: Basic CURLBASICSECRET99" https://example.test/v1', "CURLBASICSECRET99"),
    # base64 填充 `=` 紧挨字符串闭引号、后面还有授权键：不能把它当 Digest 参数越界，漏掉后一个键的凭据
    (json.dumps({"echo": "Authorization: Basic QkFTRTY0UEFE=", "authorization": "Token AFTERPADDINGSECRET"}),
     "AFTERPADDINGSECRET"),
])
def test_authorization_any_scheme_masks_whole_credential(text, secret, labels) -> None:
    safe = redact_sensitive_text(text, redact_assignment_labels=labels)
    assert secret not in safe
    if not labels and text.lower().startswith(("authorization:", "proxy-authorization:")):
        assert safe.split(":", 1)[1].split()[0] in {"Basic", "Token", "Digest"}, "方案名应保留，便于排查"
    if text.startswith("{\"") and not labels:
        json.loads(safe)  # labels 模式按 rejectdiag3 口径连键名整段替换，只承诺不泄漏，不承诺 JSON 可解析
    if text.startswith("curl"):
        assert safe.endswith("https://example.test/v1"), "嵌在字符串里的授权头不能吞掉后面的 URL"


# LLM: rejectdiag4 小问题 2：值以 `{`/`[` 开头是结构（如 JSON schema 片段）不是凭据，原样保留；结构里嵌套的真凭据照样遮。
# 函数用途: 钉住 schema 片段不被误遮、不被破坏结构。
def test_authorization_schema_fragment_kept_and_nested_secret_masked() -> None:
    schema = '"authorization": {"type": "string"}'
    assert redact_sensitive_text(schema) == schema
    document = json.dumps({
        "properties": {"authorization": {"type": "string", "description": "请求头"}},
        "authorization": "Bearer NESTEDSECRET123456",
    })
    safe = redact_sensitive_text(document)
    assert "NESTEDSECRET123456" not in safe
    parsed = json.loads(safe)
    assert parsed["properties"]["authorization"] == {"type": "string", "description": "请求头"}


# LLM: rejectdiag4 ReDoS 守卫：授权头规则只有单层量词、值边界线性扫描。这里只用宽松的绝对上限挡住灾难性回溯
#   （回溯会是分钟级），精确计时与线性比例记在 TESTS.md，不在测试里卡紧时限（CI 机器快慢不一）。
# 函数用途: 钉住病态输入下授权头清洗不出现超线性耗时。
@pytest.mark.parametrize("shape", [
    lambda n: "a" * n + "!",
    lambda n: " " * n,
    lambda n: "Authorization: " + "x " * n,
    lambda n: "authorization" + " " * n + "!",
    lambda n: "Authorization:" * (n // 14),
    lambda n: 'Authorization: Digest a="' + "x" * n,
    lambda n: '{"authorization": "' + "\\" * n,
])
def test_authorization_redaction_stays_linear_on_pathological_input(shape) -> None:
    import time

    started = time.perf_counter()
    redact_sensitive_text(shape(100_000))
    assert time.perf_counter() - started < 5.0


# LLM: 同一行两个授权头：前一个值截在下一个授权键之前，后一个键名与方案名照常保留、凭据照常遮。
# 函数用途: 钉住授权值不跨过下一个授权键。
def test_authorization_value_stops_before_next_authorization_key() -> None:
    safe = redact_sensitive_text("Authorization: Basic FIRSTSECRET01 Proxy-Authorization: Token SECONDSECRET02")
    assert "FIRSTSECRET01" not in safe and "SECONDSECRET02" not in safe
    assert "Proxy-Authorization: Token" in safe


# LLM: 完整批次也可能止于未闭合JSON或PEM；仅用合成值同时守共用脱敏和原公开正文入口，不依赖sealed来源代替脱敏。
# 函数用途: 钉住两个原漏遮尾部和闭合对照；失败仅报告合成标记是否隐藏，不打印原正文。
@pytest.mark.parametrize("kind", ["json", "pem"])
@pytest.mark.parametrize("closed", [False, True])
def test_explicit_secret_batch_truncation_masked(kind: str, closed: bool) -> None:
    from agent_py_agent.agent.gateway_parts.stream_events import public_model_text

    marker = "synthetic-only-secret-batch572"
    if kind == "json":
        text = '{"api_key":"' + marker + ('"}' if closed else '')
    else:
        text = '-----BEGIN PRIVATE KEY-----\n' + marker + ('\n-----END PRIVATE KEY-----' if closed else '')
    common_hidden = marker not in redact_sensitive_text(text)
    public_hidden = marker not in public_model_text(text, max_chars=0, delivery_channel="tui", identifiers=())
    assert common_hidden, "synthetic_common_marker_hidden=False"
    assert public_hidden, "synthetic_public_marker_hidden=False"


# LLM: 明确凭据键沿原词表和引号边界遮蔽，EOF不等于合法闭引号；转义、异种引号与换行不能漏掉尾段。
# 函数用途: 用不同终止形状和labels模式验证整段秘密不会残留，普通前后字段保持。
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("labels", [False, True])
@pytest.mark.parametrize("ending", ["closed", "eof", "backslash_eof"])
@pytest.mark.parametrize("value_kind", ["plain", "opposite_quote", "escaped_quote", "multiline"])
def test_batch_json_secret_value_boundaries(quote, labels, ending, value_kind) -> None:
    marker = "synthetic-only-secret-boundary572"
    other = "'" if quote == '"' else '"'
    body = {"plain": marker, "opposite_quote": "prefix" + other + marker,
            "escaped_quote": "prefix\\" + quote + marker,
            "multiline": "prefix\n" + marker + "\nlast"}[value_kind]
    end = quote + ', "ordinary_after": "after"}' if ending == "closed" else ("\\" if ending == "backslash_eof" else "")
    text = '{"ordinary_before":"before", ' + quote + 'db_password' + quote + ': ' + quote + body + end
    safe = redact_sensitive_text(text, redact_assignment_labels=labels)
    hidden = marker not in safe
    assert hidden, "synthetic_boundary_marker_hidden=False"
    assert '"ordinary_before":"before"' in safe
    if ending == "closed":
        assert '"ordinary_after": "after"' in safe
    elif not labels:
        assert safe.endswith(quote + "<redacted>"), "EOF不得被伪造为完整JSON"


# LLM: 普通key不消费扩展值体；内部明确凭据标签或后续真实字段仍须由同一个凭据规则处理。
# 函数用途: 防止为了EOF扩展普通外层匹配，反而吞掉需要遮蔽的内嵌或重复秘密。
@pytest.mark.parametrize("shape", ["after", "inside_single", "inside_double", "repeated", "nested"])
def test_batch_json_ordinary_fields_do_not_swallow_credentials(shape) -> None:
    marker = "synthetic-only-secret-nested572"
    texts = {
        "after": '{"note":"before", "api_key":"' + marker,
        "inside_single": '{"note":"before \'api_key\': \'' + marker + '\' after", "other":"kept"}',
        "inside_double": "{'note':'before \"api_key\": \"" + marker + "\" after', 'other':'kept'}",
        "repeated": '{"api_key":"' + marker + '","note":"kept","api_key":"' + marker,
        "nested": '{"note":{"password_hint":"kept", "nested":{"client_secret":"' + marker,
    }
    safe = redact_sensitive_text(texts[shape])
    hidden = marker not in safe
    assert hidden, "synthetic_nested_marker_hidden=False"
    assert "note" in safe
    if shape != "after":
        assert "kept" in safe


# LLM: 本片不改变完整键后缀、纯全大写变量引用和source-only调用方选择；普通值即使截断也不能被当秘密。
# 函数用途: 保持误伤边界、原变量引用及源码显式模式。
@pytest.mark.parametrize("key", ["max_tokens", "token_count", "password_hint", "secretary", "note"])
@pytest.mark.parametrize("ending", ['"}', "", "\\"])
def test_batch_json_non_secret_keys_remain_unchanged(key, ending) -> None:
    text = '{"' + key + '": "ordinary-value' + ending
    assert redact_sensitive_text(text) == text


@pytest.mark.parametrize("value", ["$TOKEN", "${TOKEN}"])
@pytest.mark.parametrize("ending", ['"}', ""])
def test_batch_json_variable_references_keep_original_contract(value, ending) -> None:
    text = '{"api_key":"' + value + ending
    assert redact_sensitive_text(text) == text


def test_batch_source_mode_and_url_authority_contract_unchanged() -> None:
    source = '{"api_key":"synthetic-source-reference572'
    assert redact_sensitive_text(source, code_file=True) == source
    for text in ('{"api_key":""}', '{"api_key":"', 'https://host:8420/path',
                 'url = f"postgres://{user}:{password}@{host}/db"'):
        assert redact_sensitive_text(text, code_file=True) == text
    assert redact_sensitive_text('{"api_key":""}') == '{"api_key":""}'


# LLM: PEM沿原大写BEGIN/END形状与原固定marker；缺END保守遮到EOF，普通前缀/完整块后的正文不吞。
# 函数用途: 验证不同私钥标签在普通和源码模式中的完整与截断边界。
@pytest.mark.parametrize("label", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "ENCRYPTED PRIVATE KEY"])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("code_file", [False, True])
def test_batch_pem_begin_masks_until_end_or_eof(label, closed, code_file) -> None:
    marker = "synthetic-only-secret-pem572"
    text = "before\n-----BEGIN " + label + "-----\n" + marker
    if closed:
        text += "\n-----END " + label + "-----\nafter"
    safe = redact_sensitive_text(text, code_file=code_file, redacted_marker="CUSTOM")
    hidden = marker not in safe
    assert hidden, "synthetic_pem_marker_hidden=False"
    assert safe == "before\n<redacted-private-key>" + ("\nafter" if closed else "")


# LLM: 扩展边界仍是互斥字符分支，不能回退成嵌套量词；复用既有墙钟兜底和原0.1秒宽松预算。
# 函数用途: 钉住长截断值、尾反斜杠、普通key和重复伪END不会带来灾难回溯。
@pytest.mark.parametrize("shape", ["json_eof", "backslashes", "ordinary_key", "decoy_keys", "pem_eof", "pem_decoy_end"])
def test_batch_truncated_redaction_retains_linear_budget(shape) -> None:
    from agent_py_agent.tests.test_log_redaction_variable_refs import _assert_redacts_within_budget

    text = {
        "json_eof": '{"api_key":"' + "x" * 20000,
        "backslashes": '{"api_key":"' + "\\" * 20001,
        "ordinary_key": '{"' + "x" * 20000 + '":"' + "v" * 20000,
        "decoy_keys": '"password_hint":"x" ' * 2000,
        "pem_eof": "-----BEGIN PRIVATE KEY-----\n" + "x" * 50000,
        "pem_decoy_end": "-----BEGIN PRIVATE KEY-----\n" + "-----END INVALID-----\n" * 2000,
    }[shape]
    _assert_redacts_within_budget(shape, text)


def test_batch_unrelated_pem_and_port_urls_remain_unchanged() -> None:
    for text in ("-----BEGIN PUBLIC KEY-----\nordinary-public-material", "-----BEGIN CERTIFICATE-----\nordinary-certificate", "https://host:8420/path", "postgres://host:5432/db"):
        assert redact_sensitive_text(text) == text
