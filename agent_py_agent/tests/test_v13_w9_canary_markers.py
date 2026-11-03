"""#96：原生 HTTP 的 provider 门、秘密一次性、附件与触发证据回归。"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_store
from agent_py_agent.tests import test_v13_w0_harness
from agent_py_agent.tests.test_v13_w9_hunting import (
    _call,
    _completed,
    _error,
    _space,
    hunting_http,
)


# LLM: 复用真实Gateway fixture，不替换推送验签、来源校验或游标签名。
# 函数用途: 为标记测试显式绑定同一套原生HTTP环境。
@pytest.fixture(name="marker_http")
def marker_gateway(request):
    return request.getfixturevalue("hunting_http")


# LLM: 适配器是唯一合成外部叶；测试不替换 HTTP、分页签名、存储和权限。
# 类用途: 提供有真实分页游标的 FIM 样本。
class FimProvider:
    # LLM: 所有事实都为测试固定输入，不调用系统监控。
    # 函数用途: 按 cursor 返回一页事件。
    def events(self, *, host, path_prefix, since, cursor, limit):
        event = {"event_id": "fim-two" if cursor else "fim-one", "host": host or "test-host",
            "path": "/fixture/config", "action": "modified", "sha256_before": None,
            "sha256_after": "a" * 64, "observed_at": "2026-11-02T12:00:00+00:00"}
        assert cursor in (None, "provider-two")
        return {"items": [event], "next_cursor": None if cursor else "provider-two"}

    # LLM: 样本只验证时间单位与聚合映射，不冒充实际主机 FIM 数据。
    # 函数用途: 返回指定窗口的聚合样本。
    def summary(self, *, window_seconds):
        assert window_seconds == 86400
        return {"events_total": 2, "by_action": {"modified": 2}, "top_hosts": [{"key": "test-host", "count": 2}],
            "top_paths": [{"key": "/fixture/config", "count": 2}]}


# LLM: 未绑定时不能发现也不能调用；绑定后仍须防游标篡改和跨 scope。
# 函数用途: 覆盖 #96-1 provider 门及签名分页。
def test_fim_events_require_provider_and_page(marker_http):
    _error(_call(marker_http, "fim.events.list"), 503, "HOST_NOT_READY")
    discovery = _completed(_call(marker_http, "system.capabilities.extensions"))
    assert "fim.events.list" not in discovery["supported_actions"]
    marker_http[1].business_v13_providers.update({"osquery_fim": FimProvider()})
    first = _completed(_call(marker_http, "fim.events.list", {"host": "test-host", "limit": 1}))
    assert first["items"][0]["event_id"] == "fim-one" and first["next_cursor"]
    cursor = first["next_cursor"]
    second = _completed(_call(marker_http, "fim.events.list", {"host": "test-host", "limit": 1, "cursor": cursor}))
    assert second["items"][0]["event_id"] == "fim-two" and second["next_cursor"] is None
    for params in ({"host": "other", "limit": 1, "cursor": cursor}, {"host": "test-host", "limit": 1, "cursor": cursor + "x"}):
        _error(_call(marker_http, "fim.events.list", params), 503, "CURSOR_EXPIRED")


# LLM: issue 原文的 window 24h 必须经真实接口，不靠领域函数存在证明。
# 函数用途: 覆盖 #96-2 原请求示例。
def test_fim_summary_example_window_24h(marker_http):
    marker_http[1].business_v13_providers.update({"osquery_fim": FimProvider()})
    data = _completed(_call(marker_http, "fim.summary.get", {"window": "24h"}))
    assert data["window"] == "24h" and data["events_total"] == 2
    assert data["by_action"] == {"modified": 2} and data["coverage"] == "complete"


# LLM: 不返回源码或路径；三个种类都须使用同一原始 secret-once 合同。
# 函数用途: 创建一个真实业务标记。
def _create(native, kind="api_key", space=None):
    space = space or _space(native)
    p = {"kind": kind, "label": "canary 测试", "space_id": space}
    key = uuid4().hex
    result = _completed(_call(native, "honeytokens.create", p, key=key))
    return space, result, p, key


# LLM: 所有JSON回执和记录不得含明文；文档成品另按明确产品合同携标记。
# 函数用途: 覆盖 #96-3 的三种类型及单次公开。
@pytest.mark.parametrize("kind", ["credential", "api_key", "document"])
def test_create_returns_marker_value_once_and_stores_fingerprint_only(marker_http, kind):
    _, first, p, key = _create(marker_http, kind)
    assert first["secret"].startswith("canary_" + kind + "_")
    replay = _completed(_call(marker_http, "honeytokens.create", p, key=key))
    assert replay["secret"] == "<redacted>" and replay["marker"] == first["marker"]
    _error(_call(marker_http, "honeytokens.create", {**p, "label": "changed"}, key=key), 409, "IDEMPOTENCY_CONFLICT")
    home = marker_http[1].home_paths.owner_home_dir
    row = v13_store.V13Store(home).get("canary_markers", first["marker"]["token_id"])
    assert row["value_sha256"] == hashlib.sha256(first["secret"].encode()).hexdigest()
    assert row["fingerprint"] == row["value_sha256"][:16]
    for path in (home / "data" / "business_v13").glob("*.json"):
        assert first["secret"] not in path.read_text()
    if kind == "document":
        artifact = first["marker"]["artifact"]
        assert set(artifact) == {"artifact_id", "version_ref", "name", "mime_type", "size_bytes", "content_sha256"}
        blobs = list((home / "artifacts" / "business").glob("*.blob"))
        assert len(blobs) == 1 and first["secret"].encode() in blobs[0].read_bytes()


# LLM: 告警严格采用W8规范形状，正文仅为待比较证据，不控制owner或space。
# 函数用途: 经共享原事务写一条完整告警样本。
def _alert(native, identifier, space, text, *, at=None):
    now = at or datetime.now(timezone.utc).isoformat()
    row = {"alert_id": identifier, "space_id": space, "title": "测试告警", "description": text,
        "severity": "info", "status": "new", "verdict": None, "ai_confidence": None,
        "disposition": None, "rule_id": None, "source": "ingest", "external_ref": None,
        "entities": [], "techniques": [], "assignee": None, "case_id": None, "tags": [],
        "evidence_refs": [], "acknowledged_at": None, "resolved_at": None,
        "revision": 1, "created_at": now, "updated_at": now, "owner_id": "local/main"}
    test_v13_w0_harness.v13_seed(native[1].home_paths.owner_home_dir, "soc_alerts", {identifier: row})


# LLM: 命中与不命中、跨空间、重复GET及翻页分别断言，不能把线程历史当逐事件事实。
# 函数用途: 覆盖 #96-4 告警证据与扫描回执分页回归。
def test_triggers_detected_from_alert_evidence_and_page(marker_http):
    space, first, _, _ = _create(marker_http)
    other = _space(marker_http)
    secret = first["secret"]
    _alert(marker_http, "alert-one", space, secret)
    _alert(marker_http, "alert-two", space, secret)
    _alert(marker_http, "alert-other", other, secret)
    p = {"token_id": first["marker"]["token_id"], "limit": 1}
    page = _completed(_call(marker_http, "honeytokens.triggers.list", p))
    assert len(page["items"]) == 1 and page["next_cursor"]
    following = _completed(_call(marker_http, "honeytokens.triggers.list", {**p, "cursor": page["next_cursor"]}))
    assert {page["items"][0]["source_ref"], following["items"][0]["source_ref"]} == {"alert-one", "alert-two"}
    repeated = _completed(_call(marker_http, "honeytokens.triggers.list", {"token_id": p["token_id"]}))
    assert len(repeated["items"]) == 2
    assert secret not in json.dumps(repeated)
    stored = v13_store.V13Store(marker_http[1].home_paths.owner_home_dir).get("canary_markers", p["token_id"])
    assert stored["trigger_count"] == 2
    _error(_call(marker_http, "honeytokens.triggers.list", p, other_owner=True), 404, "RESOURCE_NOT_FOUND")


# LLM: 较旧证据不能因最新窗口不命中而永久漏查并伪报complete。
# 函数用途: 验证扫描实际覆盖范围。
def test_scan_does_not_silently_drop_older_alerts(marker_http):
    space, first, _, _ = _create(marker_http)
    for i in range(51):
        _alert(marker_http, "recent-" + str(i), space, "nonmatching")
    _alert(marker_http, "old-match", space, first["secret"], at="2026-10-01T00:00:00+00:00")
    data = _completed(_call(marker_http, "honeytokens.triggers.list", {"token_id": first["marker"]["token_id"]}))
    assert [row["source_ref"] for row in data["items"]] == ["old-match"]


# LLM: 标记吊销不抹除记录，删除按最新CAS并原子删关联触发。
# 函数用途: 覆盖 #96-5..6 生命周期与幂等。
def test_revoke_then_delete_with_cas(marker_http):
    space, first, _, _ = _create(marker_http)
    p = {"token_id": first["marker"]["token_id"]}
    revoked = _completed(_call(marker_http, "honeytokens.revoke", p, key=uuid4().hex, revision=1))["marker"]
    assert revoked["state"] == "revoked" and revoked["revision"] == 2
    _alert(marker_http, "revoked-evidence", space, first["secret"])
    data = _completed(_call(marker_http, "honeytokens.triggers.list", p))
    assert data["items"][0]["details"]["revoked"] is True
    _error(_call(marker_http, "honeytokens.delete", p, key=uuid4().hex, revision=2), 409, "REVISION_CONFLICT")
    key = uuid4().hex
    deleted = _completed(_call(marker_http, "honeytokens.delete", p, key=key, revision=3))
    assert deleted == {**p, "deleted": True}
    assert _completed(_call(marker_http, "honeytokens.delete", p, key=key, revision=3)) == deleted
    store = v13_store.V13Store(marker_http[1].home_paths.owner_home_dir)
    assert store.get("canary_markers", p["token_id"]) is None and store.list("canary_triggers") == []
    _error(_call(marker_http, "honeytokens.triggers.list", p), 404, "RESOURCE_NOT_FOUND")


# LLM: 即使已打开业务开关，未知标记和伪造身份字段也不能形成空成功。
# 函数用途: 覆盖 #96-7 真实标准信封及闭合参数。
def test_envelopes(marker_http):
    _error(_call(marker_http, "honeytokens.triggers.list", {"token_id": "missing"}), 404, "RESOURCE_NOT_FOUND")
    _error(_call(marker_http, "honeytokens.create", {"kind": "api_key", "label": "x", "owner_id": "forged"}, key=uuid4().hex), 400, "INVALID_SCHEMA")


# LLM: 用业务端真实签发的秘密签原始bytes，回执来自真实原生队列；不修改认证或writer。
# 函数用途: 发一个有独立nonce的真实push，可指定复用线程以验证逐事件关联。
def _push(native, token, text, *, reuse="evidence-thread"):
    from agent_py_agent.tests.test_v13_w9_inbox_oauth import _post
    raw = json.dumps({"target_owner": "local/main", "instruction": text,
        "idempotency_key": uuid4().hex, "thread_mode": "reuse_key", "thread_reuse_key": reuse}).encode()
    stamp, nonce = str(int(time.time())), uuid4().hex
    signed = f"{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}".encode()
    headers = {"X-Agent-Push-Token": token["secret"], "X-Agent-Timestamp": stamp,
        "X-Agent-Nonce": nonce, "X-Agent-Signature": hmac.digest(token["secret"].encode(), signed, "sha256").hex()}
    status, receipt = _post(native[0], "/remote-agent/push", raw, headers)
    assert status == 202, receipt
    return receipt


# LLM: 仅逐条InputReceipt中的正文可形成命中，同线程后续非命中与其它空间命中都不能借历史归因。
# 函数用途: 验证原生push到canary触发、最近使用时间与空间隔离的实际链路。
def test_native_push_is_event_and_space_bound(marker_http):
    space, marker, _, _ = _create(marker_http)
    token = _completed(_call(marker_http, "inbox.tokens.mint", {"space_id": space, "label": "逐事件证据"}, key=uuid4().hex))
    first = _push(marker_http, token, marker["secret"])
    second = _push(marker_http, token, "本次事件不含诱饵")
    assert first["thread_id"] == second["thread_id"]
    other_space = _space(marker_http)
    other_token = _completed(_call(marker_http, "inbox.tokens.mint", {"space_id": other_space, "label": "其它空间"}, key=uuid4().hex))
    _push(marker_http, other_token, marker["secret"])
    params = {"token_id": marker["marker"]["token_id"]}
    data = _completed(_call(marker_http, "honeytokens.triggers.list", params))
    assert data["coverage"] == "complete"
    assert [(row["source"], row["source_ref"]) for row in data["items"]] == [("inbox_push", first["push_id"])]
    assert _completed(_call(marker_http, "honeytokens.triggers.list", params))["items"] == data["items"]
    listed = _completed(_call(marker_http, "inbox.tokens.list"))["items"]
    actual = next(row for row in listed if row["token_id"] == token["token"]["token_id"])
    assert actual["last_used_at"] is not None


# LLM: 同笔多集合事务删除必须覆盖超过一条审计页的触发，不允许为了过gate拆产品原子删除。
# 函数用途: 回归独审发现的100条变更上限与大标记删除。
def test_delete_more_than_100_triggers_atomically(marker_http):
    space, created, _, _ = _create(marker_http)
    identifier, now = created["marker"]["token_id"], datetime.now(timezone.utc).isoformat()
    rows = {"trigger-" + str(i): {"trigger_id": "trigger-" + str(i), "owner_id": "local/main",
        "token_id": identifier, "observed_at": now, "source": "alert", "source_ref": "alert-" + str(i),
        "details": {"matched_by": "sha256", "revoked": False}, "revision": 1,
        "created_at": now, "updated_at": now} for i in range(101)}
    test_v13_w0_harness.v13_seed(marker_http[1].home_paths.owner_home_dir, "canary_triggers", rows)
    result = _completed(_call(marker_http, "honeytokens.delete", {"token_id": identifier}, key=uuid4().hex, revision=1))
    assert result["deleted"] is True
    store = v13_store.V13Store(marker_http[1].home_paths.owner_home_dir)
    assert store.get("canary_markers", identifier) is None and store.list("canary_triggers") == []


# LLM: 所有来源先经原生事务入库，101个新命中必须在签发分页游标前登记，不由翻页本身改变视图。
# 函数用途: 复现大批扫描自己使下一页失效的问题。
def test_scan_more_than_99_hits_has_stable_page(marker_http):
    space, marker, _, _ = _create(marker_http)
    for i in range(101):
        _alert(marker_http, "bulk-match-" + str(i), space, marker["secret"])
    params = {"token_id": marker["marker"]["token_id"], "limit": 100}
    first = _completed(_call(marker_http, "honeytokens.triggers.list", params))
    assert len(first["items"]) == 100 and first["next_cursor"]
    second = _completed(_call(marker_http, "honeytokens.triggers.list", {**params, "cursor": first["next_cursor"]}))
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert len({row["source_ref"] for row in first["items"] + second["items"]}) == 101
    assert second["coverage"] == "complete"


# LLM: 原生推送允许32768字符正文，合法后段不能被静默截去再宣称complete。
# 函数用途: 复现长推送后段的canary漏扫。
def test_native_push_scans_full_admitted_instruction(marker_http):
    space, marker, _, _ = _create(marker_http)
    token = _completed(_call(marker_http, "inbox.tokens.mint", {"space_id": space, "label": "长正文"}, key=uuid4().hex))
    receipt = _push(marker_http, token, "x" * 21000 + " " + marker["secret"])
    data = _completed(_call(marker_http, "honeytokens.triggers.list", {"token_id": marker["marker"]["token_id"]}))
    assert [row["source_ref"] for row in data["items"]] == [receipt["push_id"]]
    assert data["coverage"] == "complete"


# LLM: 只在隔离测试原生token账本构造历史多owner ACL；原生HTTP仍实际验签，不修改认证函数。
# 函数用途: 验证合法旧来源不会阻断W9读取，同时不借它恢复空间身份。
def test_legacy_multi_owner_push_does_not_block_w9_readers(marker_http):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.gateway_parts.remote_push_service import (
        persist_push_json,
        read_push_json,
    )
    space, marker, _, _ = _create(marker_http)
    legacy = _completed(_call(marker_http, "inbox.tokens.mint", {"space_id": space, "label": "历史来源"}, key=uuid4().hex))
    root = marker_http[1].home_paths.root / "remote_push"
    with locked_json_path(root / "tokens.json"):
        config = read_push_json(root, "tokens.json")
        row = next(row for row in config["tokens"] if row["token_id"] == legacy["token"]["token_id"])
        row["allowed_target_owners"].append("local/second")
        row.pop("space_id", None)
        persist_push_json(root / "tokens.json", config)
    _push(marker_http, legacy, marker["secret"])
    normal = _completed(_call(marker_http, "inbox.tokens.mint", {"space_id": space, "label": "本空间"}, key=uuid4().hex))
    expected = _push(marker_http, normal, marker["secret"])
    data = _completed(_call(marker_http, "honeytokens.triggers.list", {"token_id": marker["marker"]["token_id"]}))
    assert [row["source_ref"] for row in data["items"]] == [expected["push_id"]]
    assert data["coverage"] == "partial"
    listed = _completed(_call(marker_http, "inbox.tokens.list"))["items"]
    assert next(row for row in listed if row["token_id"] == normal["token"]["token_id"])["last_used_at"] is not None
    assert _completed(_call(marker_http, "datasources.fleet_health"))["coverage"] == "partial"
