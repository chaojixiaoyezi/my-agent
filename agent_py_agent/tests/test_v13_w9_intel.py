"""#91 领域行为及真实生产客户端→Gateway→原存储的合同回归。"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w9_intel as intel
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests import test_rework_r8_business_wire_contract as wire_harness

gateway = wire_harness.gateway
thin_client = wire_harness.thin_client


# LLM: 仅提供调用 DTO；单元测试显式替换 I/O 叶，不把替身当成真实事务验收。
# 函数用途: 组装隔离 home 下的领域调用。
@pytest.fixture
def unit_call(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    home = home_paths(tmp_path / "home")
    home = home_paths_with_owner(home, resolve_owner_home(home.root))
    host = SimpleNamespace(home_paths=home, config=AgentConfig(business_spaces_enabled=True), business_v13_providers={})

    # LLM: 单元测试只替换需要真实签发context的来源解析叶，wire测试保留W0原函数。
    # 函数用途: 在DTO单测中提供显式来源绑定。
    def bound_provider(call, required=False):
        provider = call.context.host.business_v13_providers.get("threat_intel")
        if provider is None and required:
            raise HostAPIError("HOST_NOT_READY", 503)
        return provider

    monkeypatch.setattr(intel, "_provider", bound_provider)
    return SimpleNamespace(action="threat_intel.sources.sync", params={}, owner_id=home.owner_id,
        subject_id="local", context=SimpleNamespace(host=host, check=lambda action, facts: None),
        owner_home=home.owner_home_dir, clock=lambda: 1793664000.0,
        idempotency_key="unit-intel-sync-0001", expected_revision=None)


# LLM: 来源仅返回测试数据；调用次数用于确认重放不再次拉取，不伪造业务成功。
# 类用途: 提供冻结 fetch/lookup 协议的确定性来源。
class IntelProvider:
    # LLM: 不持有凭据或网络依赖。
    # 函数用途: 保存本页指标与可选游标。
    def __init__(self, rows, next_cursor=None):
        self.rows, self.next_cursor, self.fetch_count = rows, next_cursor, 0

    # LLM: 一次请求只返回一页，保留来源真实游标的原样传递。
    # 函数用途: 记录源同步调用并返回测试页。
    def fetch(self, *, source, cursor):
        self.fetch_count += 1
        return {"indicators": self.rows, "next_cursor": self.next_cursor}

    # LLM: 查询由产品层再次核对规范化键，不能信任来源返回的全部行。
    # 函数用途: 返回测试富化指标。
    def lookup(self, value, kind):
        return self.rows


# LLM: 样本来自公开测试地址段，不携带真实指标或秘密。
# 函数用途: 生成完整规范指标。
def indicator(value="198.51.100.42", kind="ip", source="feed-a", confidence=80):
    return {"value": value, "type": kind, "confidence": confidence, "family_tags": ["family-a"],
        "actor_attribution": ["actor-a"], "first_seen_at": "2026-10-01T00:00:00+00:00",
        "last_seen_at": "2026-11-02T00:00:00+00:00", "sources": [source]}


# LLM: 不只测存在性，断言五种指标的规范化内容和非法值错误。
# 函数用途: 验证所有合同类型的规范键。
@pytest.mark.parametrize("value,kind,expected", [
    ("2001:0db8::0001", "ip", "2001:db8::1"),
    ("EXAMPLE.TEST.", "domain", "example.test"),
    ("A" * 64, "sha256", "a" * 64), ("B" * 32, "md5", "b" * 32),
    ("HTTPS://EXAMPLE.TEST:443/a?q=1#part", "url", "https://example.test/a?q=1"),
])
def test_normalization(value, kind, expected):
    assert intel.normalize_indicator(value, kind) == expected


# LLM: 失败不可以伪装成 found=false。
# 函数用途: 拒绝格式无效的显式指标类型。
@pytest.mark.parametrize("value,kind", [("no-ip", "ip"), ("z" * 64, "sha256"), ("a", "md5"),
    ("bad..test", "domain"), ("https://user:secret@example.test/", "url")])
def test_normalization_rejects_invalid(value, kind):
    with pytest.raises(ValueError):
        intel.normalize_indicator(value, kind)


# LLM: 对同一规范键合并富化；告警实体来自 owner 列表，文案相同但实体不同不能命中。
# 函数用途: 验证示例 IP 的本地、来源和 sightings 行为。
def test_lookup_example_ip_local_and_provider_and_sightings(unit_call, monkeypatch):
    row = {**indicator(), "indicator_id": "existing-local-id"}
    alerts = [{"alert_id": "alert-one", "entities": [{"type": "ip", "value": row["value"]}]},
        {"alert_id": "alert-two", "description": row["value"], "entities": []}]
    monkeypatch.setattr(intel, "w9_list", lambda call, collection: alerts if collection == "soc_alerts" else [row])
    unit_call.params = {"indicator": "198.51.100.42", "type": "ip"}
    result = intel.lookup(unit_call)
    assert result["found"] and result["coverage"] == "partial"
    assert result["sources_consulted"] == ["local"]
    assert result["sightings"] == {"alert_ids": ["alert-one"], "count": 1}
    unit_call.context.host.business_v13_providers["threat_intel"] = IntelProvider([indicator(source="feed-b", confidence=95)])
    result = intel.lookup(unit_call)
    assert result["coverage"] == "complete" and result["sources_consulted"] == ["local", "threat_intel"]
    assert result["matches"][0]["sources"] == ["feed-a", "feed-b"]
    assert result["matches"][0]["confidence"] == 95
    assert result["matches"][0]["indicator_id"] == "existing-local-id"


# LLM: 只有写回调看到锁内新事实；无锁外快照整条覆盖。
# 函数用途: 验证同步合并及 lookback、类型与数量上限。
def test_sync_upserts_and_reports_retro_hunt_matches(unit_call, monkeypatch):
    provider = IntelProvider([indicator(source="feed-b"), indicator(source="feed-c")], "page-two")
    unit_call.context.host.business_v13_providers["threat_intel"] = provider
    previous = {**intel._indicator(indicator(source="feed-a")), "owner_id": unit_call.owner_id,
        "revision": 7, "created_at": "2026-10-01T00:00:00+00:00", "indicator_id": "preserved-id"}
    records = {"preserved-id": previous}
    settings = {"enabled": True, "lookback_days": 2, "indicator_types": ["ip"], "max_matches_per_sync": 1}
    monkeypatch.setattr(intel, "w9_get", lambda call, collection, key: settings)
    recent = datetime.fromtimestamp(unit_call.clock() - 60, timezone.utc).isoformat()
    old = datetime.fromtimestamp(unit_call.clock() - 3 * 86400, timezone.utc).isoformat()
    alerts = [{"alert_id": key, "created_at": at, "entities": [{"type": "ip", "value": "198.51.100.42"}]}
        for key, at in (("a", recent), ("b", recent), ("old", old), ("unknown-time", None))]
    monkeypatch.setattr(intel, "w9_list", lambda call, collection: alerts)
    monkeypatch.setattr(intel, "w9_write", lambda call, collection, mutate: mutate(records))
    result = intel.sync_sources(unit_call)
    assert result == {"fetched": 2, "upserted": 1, "next_cursor": "page-two",
        "retro_hunt": {"enabled": True, "matched_alert_ids": ["a"]}}
    assert records["preserved-id"]["sources"] == ["feed-a", "feed-b", "feed-c"]
    assert records["preserved-id"]["revision"] == 9
    settings["indicator_types"] = ["domain"]
    assert intel.sync_sources(unit_call)["retro_hunt"]["matched_alert_ids"] == []
    settings["enabled"] = False
    assert intel.sync_sources(unit_call)["retro_hunt"] == {"enabled": False, "matched_alert_ids": []}


# LLM: 来源整页先验证，错误不能提交半页。
# 函数用途: 验证绑定缺失、坏页和跨 owner 记录拒绝。
def test_provider_failures_and_no_partial_commit(unit_call, monkeypatch):
    with pytest.raises(HostAPIError) as missing:
        intel.sync_sources(unit_call)
    assert missing.value.code == "HOST_NOT_READY"
    records = {}
    monkeypatch.setattr(intel, "w9_write", lambda call, collection, mutate: mutate(records))
    provider = IntelProvider([indicator(), indicator(confidence=1000)])
    unit_call.context.host.business_v13_providers["threat_intel"] = provider
    with pytest.raises(HostAPIError) as invalid:
        intel.sync_sources(unit_call)
    assert invalid.value.code == "HOST_NOT_READY" and records == {}


# LLM: 生产客户端原样发送并解析真实信封；不替换HTTP运输，不手造返回 JSON。
# 函数用途: 通过真实 Gateway 调用一个扩展业务动作。
def wire_call(gateway, action, params=None, identity=None, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts import business_api_v13
    from agent_py_agent.agent.plugin_host.business_action_specs import (
        business_schema_bundle,
        standalone_schema,
    )
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import post_gateway_json

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    payload = {"action": action, "request_id": "w9-" + action, "params": params or {}}
    if key is not None:
        payload["idempotency_key"] = key
    if revision is not None:
        payload["expected_revision"] = revision
    status, body = post_gateway_json(thin_client(gateway, identity), "/client/business", payload, timeout=10.0)
    assert "state" in body, (status, body)
    schema = standalone_schema(business_schema_bundle()["response_envelope_schema"])
    result_schema = business_api_v13.v13_action_spec(action).result_schema
    schema["$defs"].update(result_schema.pop("$defs", {}))
    schema["properties"]["data"] = {"anyOf": [{"$ref": "#/$defs/EmptyData"}, result_schema]}
    envelope = validate_object(body, schema)
    if body["error"] is None:
        business_api_v13.v13_action_spec(action).validate_result(body["data"])
    assert (envelope["error"] is None and status == 200 and envelope["state"] == "completed") or (
        envelope["error"] is not None and status >= 400 and envelope["state"] == "failed"), (status, envelope)
    return {**envelope, "http_status": status, "ok": envelope["error"] is None}


# LLM: 只按可信 owner 解析目录，用 W0 官方种子函数写原集合。
# 函数用途: 将真实测试记录送入指定 owner 的原 v13 存储。
def seed_rows(gateway, collection, rows, identity=None):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests import test_v13_w0_harness

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    owner = resolve_owner_home(gateway.agent.home_paths.root, identity)
    test_v13_w0_harness.v13_seed(owner.home_dir, collection, [{**row, "owner_id": owner.owner_id} for row in rows])
    return owner


# LLM: 真实分页复用原生 HMAC 游标，跨 owner、篡改与过滤变化均须失败。
# 函数用途: 验证列表、所有过滤、游标错误及跨 owner 隔离。
def test_list_filters_and_pages_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    rows = [{**intel._indicator(indicator("198.51.100." + str(i))), "revision": 1,
        "created_at": None, "updated_at": None} for i in range(1, 4)]
    seed_rows(gateway, "threat_indicators", rows)
    first = wire_call(gateway, "threat_intel.indicators.list", {"limit": 1})
    assert first["ok"] and first["http_status"] == 200, first
    cursor = first["data"]["next_cursor"]
    assert cursor and len(first["data"]["items"]) == 1
    second = wire_call(gateway, "threat_intel.indicators.list", {"limit": 1, "cursor": cursor})
    assert second["ok"] and second["data"]["items"] != first["data"]["items"]
    for params, identity in (({"cursor": cursor + "x"}, None),
        ({"cursor": cursor, "source": "other"}, None),
        ({"cursor": cursor}, OwnerIdentity.provider_user("feishu", "business-bob"))):
        failure = wire_call(gateway, "threat_intel.indicators.list", params, identity)
        assert (failure["http_status"], failure["error"]["code"]) == (410, "CURSOR_EXPIRED"), failure
    filtered = wire_call(gateway, "threat_intel.indicators.list",
        {"type": "ip", "source": "feed-a", "min_confidence": 80, "query": "198.51.100.2"})
    assert [row["value"] for row in filtered["data"]["items"]] == ["198.51.100.2"]
    empty = wire_call(gateway, "threat_intel.indicators.list", identity=OwnerIdentity.provider_user("feishu", "business-bob"))
    assert empty["ok"] and empty["data"]["items"] == []


# LLM: 同步来源仅 main 绑定可见；重放、不同参数重用和游标错误都经真实入口。
# 函数用途: 验证来源同步真实存储、幂等冲突与两条别名。
def test_soc_aliases_and_sync_idempotency_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    provider = IntelProvider([indicator()])
    gateway.agent.business_v13_providers = {"threat_intel": provider}
    first = wire_call(gateway, "threat_intel.sources.sync", identity=main, key="w9-wire-intel-sync-01")
    assert first["ok"] and first["data"]["upserted"] == 1, first
    replay = wire_call(gateway, "threat_intel.sources.sync", identity=main, key="w9-wire-intel-sync-01")
    assert replay["data"] == first["data"] and provider.fetch_count == 1
    conflict = wire_call(gateway, "threat_intel.sources.sync", {"source": "changed"}, main, "w9-wire-intel-sync-01")
    assert (conflict["http_status"], conflict["error"]["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    for action, params in (("soc.threat_intel.indicators.list", {}),
        ("soc.threat_intel.lookup", {"indicator": "198.51.100.42", "type": "ip"})):
        result = wire_call(gateway, action, params, main)
        assert result["ok"] and result["state"] == "completed" and result["error"] is None, result
    absent = wire_call(gateway, "threat_intel.sources.sync", key="w9-wire-intel-unbound-01")
    assert (absent["http_status"], absent["error"]["code"]) == (503, "HOST_NOT_READY")


# LLM: 错误也必须是标准信封；不能用空成功代替无权限、坏参数或缺服务。
# 函数用途: 验证示例 lookup 和封闭参数的真实运输回执。
def test_envelopes_wire(gateway):
    example = wire_call(gateway, "threat_intel.indicators.lookup", {"indicator": "198.51.100.42", "type": "ip"})
    assert (example["http_status"], example["state"], example["error"]) == (200, "completed", None), example
    invalid = wire_call(gateway, "threat_intel.indicators.list", {"owner_id": "forged"})
    assert (invalid["http_status"], invalid["error"]["code"], invalid["data"]) == (400, "INVALID_SCHEMA", {})


# LLM: 回溯依赖的策略使用真实 CAS；陈旧写不能改变随后同步采用的策略。
# 函数用途: 验证共享回溯策略的修订冲突和幂等重放。
def test_retro_settings_cas_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    params = {"enabled": True, "lookback_days": 2, "indicator_types": ["ip"], "max_matches_per_sync": 1}
    first = wire_call(gateway, "retro_hunts.settings.set", params, main, "w9-retro-cas-first-01", 0)
    assert first["ok"] and first["data"]["settings"]["revision"] == 1, first
    replay = wire_call(gateway, "retro_hunts.settings.set", params, main, "w9-retro-cas-first-01", 0)
    assert replay["data"] == first["data"]
    stale = wire_call(gateway, "retro_hunts.settings.set", {**params, "lookback_days": 365}, main, "w9-retro-cas-stale-01", 0)
    assert (stale["http_status"], stale["error"]["code"]) == (409, "REVISION_CONFLICT"), stale
    current = wire_call(gateway, "retro_hunts.settings.get", identity=main)
    assert current["data"]["settings"] == first["data"]["settings"]


# LLM: W8实体类型file_hash不能直接当指标类型；仅合法32/64位摘要可映射，文案不算实体。
# 函数用途: 回归MD5/SHA256 sightings与相同函数的回溯匹配。
@pytest.mark.parametrize("kind,size", [("md5", 32), ("sha256", 64)])
def test_file_hash_entity_maps_to_indicator(unit_call, monkeypatch, kind, size):
    value = "a" * size
    row = intel._indicator(indicator(value, kind))
    alerts = [{"alert_id": "match", "entities": [{"type": "file_hash", "value": value.upper()}]},
        {"alert_id": "wrong", "entities": [{"type": "file_hash", "value": "z" * size}]},
        {"alert_id": "text", "description": value, "entities": []}]
    monkeypatch.setattr(intel, "w9_list", lambda call, collection: alerts if collection == "soc_alerts" else [row])
    unit_call.params = {"indicator": value, "type": kind}
    assert intel.lookup(unit_call)["sightings"] == {"count": 1, "alert_ids": ["match"]}


# LLM: 一页101条必须保持同笔事务与幂等，不能截成100个或仅靠UnitStore宣称通过。
# 函数用途: 验证超过一条审计页的真实情报同步及重放。
def test_sync_more_than_100_indicators_wire(gateway):
    from agent_py_agent.agent.business_spaces import v13_store
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    provider = IntelProvider([indicator("198.51.100." + str(i)) for i in range(1, 102)])
    gateway.agent.business_v13_providers = {"threat_intel": provider}
    first = wire_call(gateway, "threat_intel.sources.sync", identity=OwnerIdentity.local_main(), key="bulk-intel-sync-0001")
    assert first["ok"] and first["data"]["upserted"] == 101, first
    assert len(v13_store.V13Store(gateway.agent.home_paths.owner_home_dir).list("threat_indicators")) == 101
    replay = wire_call(gateway, "threat_intel.sources.sync", identity=OwnerIdentity.local_main(), key="bulk-intel-sync-0001")
    assert replay["data"] == first["data"] and provider.fetch_count == 1
