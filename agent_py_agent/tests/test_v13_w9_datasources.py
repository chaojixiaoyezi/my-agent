"""#72 的真实 HTTP、原生 Vault/SQLite、CAS 与来源健康回归。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.tests.test_v13_w9_hunting import (
    _call,
    _completed,
    _error,
    _space,
    hunting_http,
)


# LLM: 复用真实Gateway fixture，不替换认证、输入账本或业务执行路径。
# 函数用途: 为数据源测试显式绑定同一套原生HTTP环境。
@pytest.fixture(name="datasource_http")
def datasource_gateway(request):
    return request.getfixturevalue("hunting_http")


# LLM: 规格和数据库均来自当前测试原生数据根，不访问开发机配置。
# 函数用途: 创建可被真实业务查询的数据源和两张测试表。
def _source(native, identifier="source-one", *, credentials=None):
    params = {"datasource_id": identifier, "name": "测试数据源", "kind": "sqlite",
        "allowed_tables": ["events"], "poll_interval_sec": 60}
    if credentials is not None:
        params["credentials"] = credentials
    data = _completed(_call(native, "datasources.create", params, key=uuid4().hex))["datasource"]
    store = DatasourceStore(native[1].home_paths.owner_home_dir)
    spec = store.get_datasource_spec(identifier)
    with sqlite3.connect(spec.database_path) as conn:
        conn.executescript("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT);"
            "INSERT INTO events VALUES(1,'one'),(2,'two'); CREATE TABLE hidden(secret TEXT);")
    return data, store, spec


# LLM: 列表必须反映原生规格，不额外生成影子数据源；过滤及签名游标走HTTP。
# 函数用途: 覆盖 #72-1 原生发现与分页。
def test_list_projects_native_store_without_secrets(datasource_http):
    first, store, spec = _source(datasource_http, credentials={"access_token": "synthetic-datasource-secret"})
    _source(datasource_http, "source-two")
    assert first["has_secret"] is True and first["credential_keys"] == ["access_token"]
    listed = _completed(_call(datasource_http, "datasources.list", {"limit": 1}))
    assert listed["next_cursor"] and len(listed["items"]) == 1
    second = _completed(_call(datasource_http, "datasources.list", {"limit": 1, "cursor": listed["next_cursor"]}))
    assert listed["items"][0]["datasource_id"] != second["items"][0]["datasource_id"]
    _error(_call(datasource_http, "datasources.list", {"cursor": listed["next_cursor"], "kind": "postgres"}), 410, "CURSOR_EXPIRED")
    assert "synthetic-datasource-secret" not in json.dumps(listed)
    assert store.resolve_secret(spec.credential_ref)["access_token"] == "synthetic-datasource-secret"
    empty = _completed(_call(datasource_http, "datasources.list", {"kind": "postgres"}))
    assert empty["items"] == []
    _error(_call(datasource_http, "datasources.test", {"datasource_id": "source-one"}, key=uuid4().hex, other_owner=True), 404, "RESOURCE_NOT_FOUND")


# LLM: 同键重放必须保持规格与凭据引用，不能再次改 native revision。
# 函数用途: 覆盖 #72-2 创建幂等、冲突及原 Vault 保密。
def test_create_uses_native_vault_and_idempotency(datasource_http):
    p = {"datasource_id": "created-once", "name": "一次创建", "kind": "https",
        "credentials": {"access_token": "synthetic-access-token"}}
    key = uuid4().hex
    first = _completed(_call(datasource_http, "datasources.create", p, key=key))
    assert _completed(_call(datasource_http, "datasources.create", p, key=key)) == first
    _error(_call(datasource_http, "datasources.create", {**p, "name": "改名"}, key=key), 409, "IDEMPOTENCY_CONFLICT")
    _error(_call(datasource_http, "datasources.create", p, key=uuid4().hex), 409, "STATE_CONFLICT")
    assert "synthetic-access-token" not in json.dumps(first)
    home = datasource_http[1].home_paths.owner_home_dir
    spec = DatasourceStore(home).get_datasource_spec("created-once")
    assert spec.revision == 1
    assert DatasourceStore(home).resolve_secret(spec.credential_ref)["access_token"] == "synthetic-access-token"
    for path in (home / "data" / "business_v13").glob("*.json"):
        assert "synthetic-access-token" not in path.read_text()


# LLM: 结果投影无法表示的凭据键必须在 native 副作用之前拒绝。
# 函数用途: 回归独审发现的凭据键数量和长度边界。
@pytest.mark.parametrize("credentials", [{"k" + str(i): "synthetic-value" for i in range(51)}, {"k" * 129: "synthetic-value"}])
def test_credential_keys_admitted_before_native_commit(datasource_http, credentials):
    p = {"datasource_id": "invalid-credential-keys", "name": "边界", "kind": "https", "credentials": credentials}
    _error(_call(datasource_http, "datasources.create", p, key=uuid4().hex), 400, "INVALID_SCHEMA")
    with pytest.raises(KeyError):
        DatasourceStore(datasource_http[1].home_paths.owner_home_dir).get_datasource_spec(p["datasource_id"])


# LLM: 原生版本为 CAS 权威，更新不能因健康探测次数误判规格版本。
# 函数用途: 覆盖 #72-3 更新 CAS、幂等和真实字段合并。
def test_update_cas_and_idempotency(datasource_http):
    first, store, spec = _source(datasource_http)
    p, key = {"datasource_id": spec.datasource_id, "name": "更新名称", "poll_interval_sec": 120}, uuid4().hex
    updated = _completed(_call(datasource_http, "datasources.update", p, key=key, revision=first["revision"]))
    assert updated["datasource"]["revision"] == 2 and updated["datasource"]["poll_interval_sec"] == 120
    assert store.get_datasource_spec(spec.datasource_id).title == "更新名称"
    assert _completed(_call(datasource_http, "datasources.update", p, key=key, revision=1)) == updated
    _error(_call(datasource_http, "datasources.update", p, key=uuid4().hex, revision=1), 409, "REVISION_CONFLICT")


# LLM: 原文件与 Vault 保留，默认列表及所有取数路径必须尊重墓碑。
# 函数用途: 覆盖 #72-4 软删除、CAS 和重放。
def test_delete_creates_tombstone_and_hides_from_list(datasource_http):
    _, store, spec = _source(datasource_http)
    p, key = {"datasource_id": spec.datasource_id}, uuid4().hex
    _error(_call(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=99), 409, "REVISION_CONFLICT")
    result = _completed(_call(datasource_http, "datasources.delete", p, key=key, revision=1))
    assert result == {**p, "deleted": True, "mode": "tombstone"}
    assert _completed(_call(datasource_http, "datasources.delete", p, key=key, revision=1)) == result
    assert _completed(_call(datasource_http, "datasources.list"))["items"] == []
    assert _completed(_call(datasource_http, "datasources.list", {"include_deleted": True}))["items"][0]["deleted"]
    assert store.get_datasource_spec(spec.datasource_id).datasource_id == spec.datasource_id
    _error(_call(datasource_http, "datasources.test", p, key=uuid4().hex), 404, "RESOURCE_NOT_FOUND")


# LLM: 原认证有效绑定必须阻止删除，禁用后可删除，不删除原 token 账。
# 函数用途: 验证数据源引用约束。
def test_delete_rejects_bound_token(datasource_http):
    space = _space(datasource_http)
    _, _, spec = _source(datasource_http)
    token = _completed(_call(datasource_http, "inbox.tokens.mint", {"space_id": space,
        "datasource_id": spec.datasource_id, "label": "引用"}, key=uuid4().hex))["token"]
    p = {"datasource_id": spec.datasource_id}
    _error(_call(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=1), 409, "STATE_CONFLICT")
    _completed(_call(datasource_http, "inbox.tokens.revoke", {"fingerprint": token["fingerprint"]}, key=uuid4().hex))
    assert _completed(_call(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=1))["deleted"]


# LLM: 连接失败是观察结果，不伪装成接口失败或连通成功。
# 函数用途: 覆盖 #72-5 原生探测和可观察失败。
def test_test_failure_is_200_ok_false_and_persisted(datasource_http):
    _, store, spec = _source(datasource_http)
    spec_path = Path(spec.database_path)
    spec_path.rename(spec_path.with_suffix(".saved"))
    data = _completed(_call(datasource_http, "datasources.test", {"datasource_id": spec.datasource_id}, key=uuid4().hex))
    assert data["ok"] is False and data["error_code"] == "DATASOURCE_CONNECT_FAILED"
    health = _completed(_call(datasource_http, "datasources.fleet_health", {}))
    assert health["items"][0]["status"] == "down" and health["items"][0]["last_test"]["ok"] is False
    assert store.get_datasource_spec(spec.datasource_id).revision == 1


# LLM: SQL 必须由真实只读连接执行，文件和隐藏表都不能受影响。
# 函数用途: 覆盖 #72-7 SELECT/CTE、行数、写入及越表拒绝。
def test_query_read_only_rejects_writes_and_disallowed_tables(datasource_http):
    _, _, spec = _source(datasource_http)
    p = {"statement": "SELECT * FROM events ORDER BY id", "datasource_ids": [spec.datasource_id], "limit": 1}
    data = _completed(_call(datasource_http, "datasources.query", p))
    assert data["results"][0]["rows"] == [[1, "one"]] and data["results"][0]["truncated"]
    for sql in ("DELETE FROM events", "SELECT * FROM hidden", "SELECT 1; DROP TABLE events", "ATTACH ':memory:' AS second", "SELECT load_extension('x')"):
        _error(_call(datasource_http, "datasources.query", {**p, "statement": sql}), 400, "QUERY_REJECTED")
    cte = _completed(_call(datasource_http, "datasources.query", {**p,
        "statement": "WITH e AS (SELECT * FROM events) SELECT count(*) FROM e"}))
    assert cte["results"][0]["rows"] == [[2]]
    with sqlite3.connect(spec.database_path) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


# LLM: SQLite DSN 映射必须实际改变路径，并继续走原 nofollow/作用域限制。
# 函数用途: 验证 DSN 与真实 SQLite 查询目标一致。
def test_sqlite_dsn_maps_to_native_path(datasource_http):
    home = datasource_http[1].home_paths.owner_home_dir
    path = home / "data" / "selected.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE selected(value INTEGER)")
        conn.execute("INSERT INTO selected VALUES(7)")
    p = {"datasource_id": "dsn-selected", "name": "指定库", "kind": "sqlite",
        "dsn_template": "sqlite:///" + str(path), "allowed_tables": ["selected"]}
    _completed(_call(datasource_http, "datasources.create", p, key=uuid4().hex))
    spec = DatasourceStore(home).get_datasource_spec(p["datasource_id"])
    assert Path(spec.database_path) == path
    result = _completed(_call(datasource_http, "datasources.query", {
        "datasource_ids": [p["datasource_id"]], "statement": "SELECT value FROM selected"}))
    assert result["results"][0]["rows"] == [[7]]


# LLM: 标准信封的字段、错误和发现状态都来自真实网关。
# 函数用途: 覆盖 #72-8..9 HTTP 信封及未来驱动的明确结果。
def test_envelopes(datasource_http):
    params = {"datasource_id": "future-driver", "name": "未来驱动", "kind": "future"}
    assert _completed(_call(datasource_http, "datasources.create", params, key=uuid4().hex))["datasource"]["queryable"] is False
    data = _completed(_call(datasource_http, "datasources.query", {"statement": "SELECT 1", "datasource_ids": [params["datasource_id"]]}))
    assert data["coverage"] == "partial" and data["results"][0]["error_code"] == "DATASOURCE_DRIVER_UNSUPPORTED"
    _error(_call(datasource_http, "datasources.list", {"owner_id": "other"}), 400, "INVALID_SCHEMA")


# LLM: 健康时间只能来自原生受理回执，业务时钟只用于计算经过时间，不伪造last_event。
# 函数用途: 覆盖 #72-6 真实push从unknown到healthy再到lagging。
def test_fleet_health_uses_real_bound_push_and_clock(datasource_http):
    from agent_py_agent.tests.test_v13_w9_canary_markers import _push
    clock = [__import__("time").time()]
    datasource_http[1].business_v13_clock = lambda: clock[0]
    space = _space(datasource_http)
    _, _, spec = _source(datasource_http)
    params = {"datasource_id": spec.datasource_id}
    initial = _completed(_call(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert initial["status"] == "unknown" and initial["last_event_at"] is None
    token = _completed(_call(datasource_http, "inbox.tokens.mint", {"space_id": space,
        "datasource_id": spec.datasource_id, "label": "健康来源"}, key=uuid4().hex))
    receipt = _push(datasource_http, token, "原生数据源事件")
    clock[0] = receipt["queued_at"] + 1
    healthy = _completed(_call(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert healthy["status"] == "healthy" and healthy["lag_sec"] == 1
    clock[0] += 121
    lagging = _completed(_call(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert lagging["status"] == "lagging" and lagging["lag_sec"] == 122
