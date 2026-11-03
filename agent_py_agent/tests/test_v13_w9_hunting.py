"""#93：真实 loopback HTTP 与原生 SQLite/存储；模型仅替换 echo 输出。"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w9_hunting as hunting
from agent_py_agent.agent.plugin_host.errors import HostAPIError


# LLM: 每例使用隔离 HOME 与真实 HTTP 服务，不用假请求替代业务路由。
# 函数用途: 启动可执行模型作业的原生网关。
@pytest.fixture
def hunting_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "host-home"))
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True,
        business_spaces_enabled=True, gateway_per_user_owner_scoping=True,
        memory_path="memory.jsonl"), tmp_path / "workspace")
    _select_echo_model(agent)
    agent.business_v13_providers = {}
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


# LLM: 运输层使用现有真实客户端，匿名之外所有身份由网关原生鉴权裁决。
# 函数用途: 发送包含精确幂等/CAS 参数的业务 HTTP 请求。
def _call(native, action, params=None, *, key=None, revision=None, other_owner=False, headers=None):
    from agent_py_agent.tests.test_mega_business_api_actions import native_request

    payload = {"action": action, "request_id": "hunt-test-" + uuid4().hex, "params": params or {}}
    if key is not None:
        payload["idempotency_key"] = key
    if revision is not None:
        payload["expected_revision"] = revision
    status, _, raw = native_request(native, "POST", "/client/business", payload,
        user="hunt-other" if other_owner else "local-agent", headers={
            "X-Channel": "feishu" if other_owner else "chat", **(headers or {})})
    return status, json.loads(raw)


# LLM: 校验实际信封，不把 200 下的失败数据或 202 误认为完成。
# 函数用途: 核对完成响应并取出数据。
def _completed(response):
    status, body = response
    assert status == 200, json.dumps(body, ensure_ascii=False)
    assert body["state"] == "completed" and body["error"] is None, body
    return body["data"]


# LLM: 错误码与状态均来自真实响应，失败结果不能含业务数据。
# 函数用途: 核对领域拒绝的完整运输形状。
def _error(response, status, code):
    actual_status, body = response
    assert actual_status == status, body
    assert body["state"] == "failed" and body["data"] == {}, body
    assert body["error"]["code"] == code, body


# LLM: 用冻结动作创建空间，不直接伪造 v13 space 记录。
# 函数用途: 创建后续 HTTP 动作实际访问的空间。
def _space(native):
    return _completed(_call(native, "spaces.create", {"name": "狩猎空间-" + uuid4().hex[:8]}, key=uuid4().hex))["space_id"]


# LLM: SQLite 和规格使用原生存储，HTTP 返回内容不由测试补造。
# 函数用途: 准备允许表、隐藏表和缺少驱动的数据源。
def _seed_sources(native):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore

    owner_home = native[1].home_paths.owner_home_dir
    path = owner_home / "data" / "hunting-fixture.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE events(id INTEGER PRIMARY KEY, process TEXT NOT NULL);"
            "INSERT INTO events VALUES(1,'pwsh'),(2,'shell'),(3,'pwsh');"
            "CREATE TABLE hidden(secret TEXT); INSERT INTO hidden VALUES('not-public');")
    store = DatasourceStore(owner_home)
    store.save_datasource({"datasource_id": "lake-one", "title": "真实 SQLite", "kind": "sqlite",
        "database_path": str(path), "read_only": True, "allowed_tables": ["events"],
        "options": {"ocsf_mapping": {"events": "process_activity"}}})
    store.save_datasource({"datasource_id": "lake-other", "title": "未支持驱动", "kind": "postgres",
        "dsn_template": "postgres://example.invalid/events", "read_only": True})
    return path


# LLM: 发现必须使用真实表与 allowed_tables，不根据名称猜 OCSF。
# 函数用途: 覆盖 #93-1 schema、明确映射、未支持驱动与空发现。
def test_lake_schema_lists_sqlite_tables_and_unsupported(hunting_http):
    space = _space(hunting_http)
    empty = _completed(_call(hunting_http, "lake.schema", {"space_id": space}))
    assert empty == {"tables": [], "unsupported": [], "coverage": "complete"}
    _seed_sources(hunting_http)
    data = _completed(_call(hunting_http, "lake.schema", {"space_id": space}))
    assert [(row["datasource_id"], row["table"]) for row in data["tables"]] == [("lake-one", "events")]
    table = data["tables"][0]
    assert table["ocsf_class"] == "process_activity" and table["row_count_estimate"] == 3
    assert table["columns"] == [
        {"name": "id", "type": "INTEGER", "notnull": False, "pk": True},
        {"name": "process", "type": "TEXT", "notnull": True, "pk": False}]
    assert data["unsupported"] == [{"datasource_id": "lake-other", "kind": "postgres",
        "error_code": "DATASOURCE_DRIVER_UNSUPPORTED"}]
    assert data["coverage"] == "partial"
    single = _completed(_call(hunting_http, "lake.schema", {"space_id": space, "datasource_id": "lake-one"}))
    assert single["coverage"] == "complete" and single["unsupported"] == []
    _error(_call(hunting_http, "lake.schema", {"space_id": space, "datasource_id": "absent"}),
        404, "RESOURCE_NOT_FOUND")


# LLM: 断言真实只读文件内容不变；拒绝多语句、写入、越表和 KQL。
# 函数用途: 覆盖 #93-2 的执行限制、行数截断与源错误。
def test_lake_sql_read_only_and_kql_rejected(hunting_http):
    space, path = _space(hunting_http), _seed_sources(hunting_http)
    params = {"space_id": space, "datasource_id": "lake-one", "statement": "SELECT * FROM events ORDER BY id", "limit": 1}
    data = _completed(_call(hunting_http, "lake.sql", params))
    row = data["results"][0]
    assert row["ok"] and row["rows"] == [[1, "pwsh"]] and row["truncated"]
    assert row["columns"] == ["id", "process"] and data["coverage"] == "complete"
    for sql in ("DELETE FROM events", "SELECT * FROM hidden", "SELECT 1; SELECT 2", "PRAGMA table_info(events)"):
        _error(_call(hunting_http, "lake.sql", {**params, "statement": sql}), 400, "QUERY_REJECTED")
    _error(_call(hunting_http, "lake.sql", {**params, "language": "kql"}), 400, "QUERY_REJECTED")
    cte = _completed(_call(hunting_http, "lake.sql", {**params,
        "statement": "WITH e AS (SELECT * FROM events) SELECT * FROM e", "limit": 100}))
    assert len(cte["results"][0]["rows"]) == 3
    unsupported = _completed(_call(hunting_http, "lake.sql", {**params, "datasource_id": "lake-other"}))
    assert unsupported["coverage"] == "partial"
    assert unsupported["results"][0]["error_code"] == "DATASOURCE_DRIVER_UNSUPPORTED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (3,)


# LLM: Echo 只替代模型文本，不替代任务提交、执行状态、finalizer 或 HTTP。
# 函数用途: 注入有界模型输出并保留模型实际收到的上下文。
def _model_text(monkeypatch, output):
    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    observed = []

    # LLM: 测试后端仍走真实执行器，记录输入用于核查 schema 上下文。
    # 函数用途: 返回确定性模型结果。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
            on_thinking_delta=None, request_options=None):
        observed.append(prompt)
        text = json.dumps(output, ensure_ascii=False)
        if on_chunk is not None:
            on_chunk(text)
        return ModelResponse(text=text, backend=self.name)

    monkeypatch.setattr(EchoBackend, "generate", generate)
    return observed


# LLM: 有 Home 的真实执行器只认 owner 模型目录；不能靠 AgentConfig 的 echo 投影绕过模型选择。
# 函数用途: 沿正式模型配置入口为隔离 owner 登记并选中 Echo 模型。
def _select_echo_model(agent):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "echo", "transport": "offline", "model_name": "w9-test-echo",
        "model_context_window_tokens": 200_000}})
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})


# LLM: 与 test_session_task_real_chain 相同，调度器负责真实唤醒、认领、run_claimed 和任务收尾。
# 函数用途: 推进 Gateway 后台执行器，并通过真实 HTTP 观察作业及 SessionTask 的终态。
def _job(native, response):
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.cli.gateway_loops import _build_background_scheduler

    status, body = response
    assert status == 202 and body["state"] == "accepted" and body["error"] is None, json.dumps(body, ensure_ascii=False)
    assert set(body["data"]) == {"state", "operation_id", "job_id", "task_id"}
    job_id = body["data"]["job_id"]
    scheduler = _build_background_scheduler(native[1], FakeDeliveryService())
    deadline = time.monotonic() + 20
    job = None
    while time.monotonic() < deadline:
        job = _completed(_call(native, "async_jobs.get", {"job_id": job_id}))
        if job["state"] in ("completed", "failed", "unknown"):
            task = _completed(_call(native, "tasks.get", {"task_id": body["data"]["task_id"]}))["task"]
            assert job["task_id"] == body["data"]["task_id"], job
            assert task["status"] == "done", json.dumps({"task": task, "job": job}, ensure_ascii=False)
            return job
        scheduler.tick(now=time.time())
        time.sleep(0.05)
    pytest.fail("异步模型作业没有在观察窗口进入终态: " + json.dumps(job, ensure_ascii=False))


# LLM: 翻译原文样例省略空间；W0 必须选中实际空间并最终检查 SQL。
# 函数用途: 覆盖 #93-3 的 202、schema 上下文与无效 SQL 作业失败。
def test_translate_example_params_job_result_validated_by_read_only_checker(hunting_http, monkeypatch):
    _space(hunting_http)
    _seed_sources(hunting_http)
    output = {"query": "SELECT * FROM events", "target": "sql", "tables_used": ["events"],
        "explanation": "按真实字段查询", "warnings": []}
    observed = _model_text(monkeypatch, output)
    params = {"prompt": "查找过去24小时内所有异常的PowerShell外联行为", "target": "sql"}
    key = uuid4().hex
    accepted = _call(hunting_http, "nl_query.translate", params, key=key)
    job = _job(hunting_http, accepted)
    assert job["state"] == "completed" and job["result"] == output, job
    assert observed and "events" in "\n".join(observed)
    replay = _call(hunting_http, "nl_query.translate", params, key=key)
    assert replay[1]["data"]["job_id"] == accepted[1]["data"]["job_id"]
    _error(_call(hunting_http, "nl_query.translate", {**params, "prompt": "另一个请求"}, key=key),
        409, "IDEMPOTENCY_CONFLICT")
    _model_text(monkeypatch, {**output, "query": "DELETE FROM events"})
    invalid = _job(hunting_http, _call(hunting_http, "nl_query.translate", params, key=uuid4().hex))
    assert invalid["state"] == "failed" and invalid["error"]["code"] == "JOB_RESULT_INVALID", invalid


# LLM: 同一动作的两种模式分别核查 200 与 202，不替模型捏造查询执行证据。
# 函数用途: 覆盖 #93-4 普通查询与异步狩猎。
def test_search_query_mode_200_agent_mode_202(hunting_http, monkeypatch):
    space, _ = _space(hunting_http), _seed_sources(hunting_http)
    data = _completed(_call(hunting_http, "hunts.search", {"space_id": space,
        "query": "SELECT * FROM events", "datasource_ids": ["lake-one"]}, key=uuid4().hex))
    assert len(data["results"][0]["rows"]) == 3, json.dumps(data, ensure_ascii=False)
    output = {"summary": "已检查提供的表结构", "findings": [], "queries_run": []}
    observed = _model_text(monkeypatch, output)
    job = _job(hunting_http, _call(hunting_http, "hunts.search", {"space_id": space,
        "query": "分析进程行为", "mode": "agent", "datasource_ids": ["lake-one"]}, key=uuid4().hex))
    assert job["state"] == "completed" and job["result"] == output, job
    assert observed and "events" in "\n".join(observed) and "hidden" not in "\n".join(observed)
    _model_text(monkeypatch, {**output, "unexpected": "闭合 schema 必须拒绝额外字段"})
    invalid = _job(hunting_http, _call(hunting_http, "hunts.search", {"space_id": space,
        "query": "分析进程行为", "mode": "agent", "datasource_ids": ["lake-one"]}, key=uuid4().hex))
    assert invalid["state"] == "failed" and invalid["result"] is None, invalid
    assert invalid["error"]["code"] == "JOB_RESULT_INVALID", invalid


# LLM: CRUD 走同一原生 HTTP 序列，复跑后旧版本删除必须失败。
# 函数用途: 覆盖 #93-5..8、保存时只读、幂等与真实 last_run。
def test_saved_hunts_crud_and_run(hunting_http):
    space, _ = _space(hunting_http), _seed_sources(hunting_http)
    params = {"space_id": space, "name": "PowerShell 查询", "query": "SELECT * FROM events ORDER BY id",
        "datasource_ids": ["lake-one"]}
    key = uuid4().hex
    first = _completed(_call(hunting_http, "hunts.saved.create", params, key=key))["hunt"]
    assert first["last_run"] is None and first["language"] == "sql"
    assert _completed(_call(hunting_http, "hunts.saved.create", params, key=key))["hunt"] == first
    _error(_call(hunting_http, "hunts.saved.create", {**params, "name": "changed"}, key=key),
        409, "IDEMPOTENCY_CONFLICT")
    for sql in ("DELETE FROM events", "SELECT 1; SELECT 2", "SELECT * FROM hidden"):
        _error(_call(hunting_http, "hunts.saved.create", {**params, "query": sql}, key=uuid4().hex),
            400, "QUERY_REJECTED")
    identifier = {"hunt_id": first["hunt_id"]}
    assert _completed(_call(hunting_http, "hunts.saved.get", identifier))["hunt"] == first
    assert _completed(_call(hunting_http, "hunts.saved.list", {"space_id": space}))["items"] == [first]
    run_key = uuid4().hex
    result = _completed(_call(hunting_http, "hunts.saved.run", identifier, key=run_key))
    assert result["hunt"]["last_run"]["row_counts"] == {"lake-one": 3}
    assert result["hunt"]["last_run"]["ok"] and result["hunt"]["last_run"]["error_codes"] == []
    assert result["hunt"]["revision"] > first["revision"]
    assert _completed(_call(hunting_http, "hunts.saved.run", identifier, key=run_key)) == result
    _error(_call(hunting_http, "hunts.saved.delete", identifier, key=uuid4().hex,
        revision=first["revision"]), 409, "REVISION_CONFLICT")
    delete_key = uuid4().hex
    deleted = _completed(_call(hunting_http, "hunts.saved.delete", identifier,
        key=delete_key, revision=result["hunt"]["revision"]))
    assert deleted == {**identifier, "deleted": True}
    assert _completed(_call(hunting_http, "hunts.saved.delete", identifier,
        key=delete_key, revision=result["hunt"]["revision"])) == deleted
    _error(_call(hunting_http, "hunts.saved.get", identifier), 404, "RESOURCE_NOT_FOUND")
    _error(_call(hunting_http, "hunts.saved.run", identifier, key=uuid4().hex), 404, "RESOURCE_NOT_FOUND")


# LLM: 初始默认、CAS 更新与重放是不同断言，不以最后写入替代并发安全。
# 函数用途: 覆盖 #93-9 owner 策略默认值和版本冲突。
def test_retro_settings_get_set_cas(hunting_http):
    initial = _completed(_call(hunting_http, "retro_hunts.settings.get"))
    assert initial == {"settings": {"enabled": False, "lookback_days": 30,
        "indicator_types": ["ip", "domain", "sha256", "md5", "url"],
        "max_matches_per_sync": 100, "revision": 0, "updated_at": None}, "configured": False}
    params = {"enabled": True, "lookback_days": 7, "indicator_types": ["ip"], "max_matches_per_sync": 20}
    key = uuid4().hex
    saved = _completed(_call(hunting_http, "retro_hunts.settings.set", params, key=key, revision=0))
    assert saved["configured"] and saved["settings"]["revision"] == 1
    assert saved["settings"]["updated_at"] is not None
    assert _completed(_call(hunting_http, "retro_hunts.settings.set", params, key=key, revision=0)) == saved
    _error(_call(hunting_http, "retro_hunts.settings.set", params, key=uuid4().hex, revision=0),
        409, "REVISION_CONFLICT")
    assert _completed(_call(hunting_http, "retro_hunts.settings.get")) == saved


# LLM: 别名必须由网关静态注册真实分派，不能通过测试改写 action 绕过。
# 函数用途: 覆盖 #93-10 所有四个桥接别名。
def test_soc_aliases(hunting_http):
    space, _ = _space(hunting_http), _seed_sources(hunting_http)
    params = {"space_id": space, "name": "别名查询", "query": "SELECT * FROM events", "datasource_ids": ["lake-one"]}
    hunt = _completed(_call(hunting_http, "soc.hunt.saved.create", params, key=uuid4().hex))["hunt"]
    assert _completed(_call(hunting_http, "soc.hunt.saved.list", {"space_id": space}))["items"] == [hunt]
    run = _completed(_call(hunting_http, "soc.hunt.saved.run", {"hunt_id": hunt["hunt_id"]}, key=uuid4().hex))
    search = _completed(_call(hunting_http, "soc.hunt.search", {"space_id": space,
        "query": params["query"], "datasource_ids": ["lake-one"]}, key=uuid4().hex))
    assert run["results"][0]["rows"] == search["results"][0]["rows"]


# LLM: 身份、关闭 schema 与真实信封同时测试，避免只测业务函数。
# 函数用途: 覆盖 #93-11 信封以及 owner/空间不可越界。
def test_envelopes(hunting_http):
    space, _ = _space(hunting_http), _seed_sources(hunting_http)
    params = {"space_id": space, "name": "隔离查询", "query": "SELECT * FROM events", "datasource_ids": ["lake-one"]}
    hunt = _completed(_call(hunting_http, "hunts.saved.create", params, key=uuid4().hex))["hunt"]
    for action, body in [("hunts.saved.get", {"hunt_id": hunt["hunt_id"]}),
            ("lake.schema", {"datasource_id": "lake-one"}),
            ("lake.sql", {"statement": "SELECT * FROM events", "datasource_id": "lake-one"})]:
        _error(_call(hunting_http, action, body, other_owner=True), 404, "RESOURCE_NOT_FOUND")
    _error(_call(hunting_http, "lake.schema", {"owner_id": "forged"}), 400, "INVALID_SCHEMA")
    # HTTP 鉴权先于业务信封；沿现有入口的真实拒绝状态核验，不假定它返回业务错误形状。
    assert _call(hunting_http, "lake.schema", {}, headers={"X-Gateway-Token": ""})[0] == 403


# LLM: 仅用于领域单元测试，不充当 W0 或 HTTP 验收替身。
# 类用途: 保存本地测试调用的显式上下文。
@dataclass(frozen=True)
class HuntingUnitCall:
    action: str = "hunts.saved.create"
    params: dict = field(default_factory=dict)
    owner_id: str = "local/main"
    subject_id: str = "unit-subject"
    expected_revision: int | None = None
    context: object = None


# LLM: 单元测试只替换基础存储端口，以确定性检验锁内读取不变量。
# 函数用途: 安装一份可观察的领域存储测试夹具。
@pytest.fixture
def hunting_unit(monkeypatch):
    collections, spaces = {}, []

    # LLM: 此检查记录给定空间；不声称覆盖原生空间鉴权。
    # 函数用途: 记录领域层请求核验的实际空间。
    def require_space(call, space_id=None):
        chosen = space_id or call.params.get("space_id") or "unit-space"
        spaces.append(chosen)
        return chosen

    # LLM: 变更回调直接收到当前字典，便于模拟锁前版本已经改变。
    # 函数用途: 执行领域事务回调，不模拟幂等或持久化。
    def write(call, collection, mutate, receipt_view=None):
        return mutate(collections.setdefault(collection, {}))

    monkeypatch.setattr(hunting, "w9_require_space", require_space)
    monkeypatch.setattr(hunting, "w9_write", write)
    monkeypatch.setattr(hunting, "w9_get", lambda call, collection, identifier:
        collections.get(collection, {}).get(identifier))
    monkeypatch.setattr(hunting, "w9_now", lambda call: "2026-11-02T12:00:00+00:00")
    return collections, spaces


# LLM: 使用真实共享只读检查函数，不替模型放宽拒绝条件。
# 函数用途: 检查 finalizer 的 SQL 安全和结构化边界。
@pytest.mark.parametrize("query", ["DELETE FROM events", "PRAGMA writable_schema=ON", "SELECT 1; SELECT 2", ""])
def test_unit_translation_rejects_non_read_only(query):
    with pytest.raises(HostAPIError) as error:
        hunting.finalize_translation(json.dumps({"query": query, "target": "sql"}))
    assert error.value.code == "QUERY_REJECTED"


# LLM: 单元检查所有合法翻译目标，完整 JSON schema 仍由 W0 按 job kind 核验。
# 函数用途: 检查正常 finalizer 解析及非对象拒绝。
def test_unit_model_finalizers():
    for target, query in [("sql", "SELECT ';' AS value"), ("sigma", "title: process"), ("kql", "events | take 1")]:
        result = {"query": query, "target": target, "tables_used": [], "explanation": "", "warnings": []}
        assert hunting.finalize_translation(json.dumps(result)) == result
    hunt = {"summary": "未执行查询", "findings": [], "queries_run": []}
    assert hunting.finalize_hunt(json.dumps(hunt)) == hunt
    for text in ("[]", "null", "模型自由文本"):
        with pytest.raises(ValueError):
            hunting.finalize_hunt(text)


# LLM: 最新记录的空间与版本必须在 mutate 内使用，防止锁外快照覆盖。
# 函数用途: 对删除 CAS 和复跑摘要做确定性领域检查。
def test_unit_saved_mutations_use_latest_record(hunting_unit, monkeypatch):
    collections, spaces = hunting_unit
    call = HuntingUnitCall(params={"name": "unit", "query": "SELECT 1"})
    first = hunting.create_saved(call)["hunt"]
    identifier = first["hunt_id"]
    row = collections["saved_hunts"][identifier]
    row.update(revision=7, space_id="latest-space", description="并发保存的新字段")
    stale = replace(call, action="hunts.saved.delete", params={"hunt_id": identifier}, expected_revision=1)
    with pytest.raises(HostAPIError) as error:
        hunting.delete_saved(stale)
    assert error.value.code == "REVISION_CONFLICT" and identifier in collections["saved_hunts"]

    # LLM: 只替查询端口的行结果；复跑读取、字段合并和版本递增仍用产品函数。
    # 函数用途: 为领域摘要提供确定性查询观察。
    def query(scoped, statement, datasource_ids=None, limit=100):
        assert scoped.params["space_id"] == "latest-space" and statement == "SELECT 1"
        return {"results": [{"datasource_id": "unit-db", "ok": True, "rows": [[1]], "error_code": None}],
            "coverage": "complete"}

    monkeypatch.setattr(hunting, "w9_query", query)
    result = hunting.run_saved(replace(stale, action="hunts.saved.run"))
    latest = result["hunt"]
    assert latest["revision"] == 8 and latest["description"] == "并发保存的新字段"
    assert latest["last_run"] == {"ran_at": "2026-11-02T12:00:00+00:00", "row_counts": {"unit-db": 1},
        "ok": True, "error_codes": []}
    assert "latest-space" in spaces
    assert hunting.delete_saved(replace(stale, expected_revision=8)) == {"hunt_id": identifier, "deleted": True}


# LLM: 攻击者知道 ID 不代表可读取其它 owner，空间检查不允许放在 owner 检查之前。
# 函数用途: 检查 owner 404 与空间核验顺序。
def test_unit_saved_owner_access(hunting_unit):
    collections, spaces = hunting_unit
    call = HuntingUnitCall(params={"name": "unit", "query": "SELECT 1"})
    first = hunting.create_saved(call)["hunt"]
    row = collections["saved_hunts"][first["hunt_id"]]
    row["owner_id"] = "other-owner"
    spaces.clear()
    with pytest.raises(HostAPIError) as error:
        hunting.get_saved(replace(call, params={"hunt_id": first["hunt_id"]}))
    assert error.value.code == "RESOURCE_NOT_FOUND" and spaces == []


# LLM: 事务测试以锁内版本为权威，不只核查字段存在。
# 函数用途: 检查默认策略、更新版本、冲突与 owner 隔离。
def test_unit_retro_cas(hunting_unit):
    collections, _ = hunting_unit
    call = HuntingUnitCall(action="retro_hunts.settings.get")
    assert hunting.get_retro_settings(call)["settings"]["revision"] == 0
    params = {"enabled": True, "lookback_days": 7, "indicator_types": ["ip"], "max_matches_per_sync": 50}
    update = replace(call, action="retro_hunts.settings.set", params=params, expected_revision=0)
    assert hunting.set_retro_settings(update)["settings"]["revision"] == 1
    collections["retro_hunt_settings"][call.owner_id]["revision"] = 9
    with pytest.raises(HostAPIError) as error:
        hunting.set_retro_settings(replace(update, expected_revision=1))
    assert error.value.code == "REVISION_CONFLICT"
    assert hunting.set_retro_settings(replace(update, expected_revision=9))["settings"]["revision"] == 10
    assert not hunting.get_retro_settings(replace(call, owner_id="other-owner"))["configured"]


# LLM: 使用真实 SQLite 与原生 DatasourceStore；只替 W0 尚缺的空间/墓碑读端口。
# 函数用途: 验证领域层与共享只读引擎、原生 schema 的实际往返。
def test_unit_native_sqlite_schema_and_query(tmp_path, monkeypatch):
    from dataclasses import asdict

    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    from agent_py_agent.agent.business_spaces import v13_w9_datasources as datasources

    path = tmp_path / "native.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE events(id INTEGER); INSERT INTO events VALUES(1),(2);"
            "CREATE TABLE hidden(secret TEXT);")
    store = DatasourceStore(tmp_path)
    store.save_datasource({"datasource_id": "unit-source", "title": "领域原生源", "kind": "sqlite",
        "database_path": str(path), "read_only": True, "allowed_tables": ["events"]})
    call = SimpleNamespace(params={"datasource_id": "unit-source"}, owner_home=tmp_path, owner_id="unit-owner",
        context=SimpleNamespace(host=SimpleNamespace(), check=lambda action, facts: None), action="lake.schema")
    monkeypatch.setattr(hunting, "w9_require_space", lambda call, space_id=None: "unit-space")
    monkeypatch.setattr(hunting, "w9_datasource_store", lambda call: store)
    monkeypatch.setattr(hunting, "w9_datasource", lambda call, identifier: asdict(store.get_datasource_spec(identifier)))
    assert store.inspect_schema("unit-source")["ok"]
    schema = hunting.lake_schema(call)
    assert schema["coverage"] == "complete" and schema["unsupported"] == []
    assert [table["table"] for table in schema["tables"]] == ["events"]
    assert schema["tables"][0]["ocsf_class"] is None and schema["tables"][0]["row_count_estimate"] == 2
    monkeypatch.setattr(datasources, "w9_get", lambda call, collection, identifier: None)
    call.params = {"datasource_id": "unit-source", "statement": "SELECT id FROM events", "limit": 1}
    data = hunting.lake_sql(call)
    assert data["coverage"] == "complete" and data["results"][0]["rows"] == [[1]]
    assert data["results"][0]["truncated"]
    call.params["statement"] = "SELECT * FROM hidden"
    with pytest.raises(HostAPIError) as error:
        hunting.lake_sql(call)
    assert error.value.code == "QUERY_REJECTED"
    call.params["language"] = "kql"
    with pytest.raises(HostAPIError) as error:
        hunting.lake_sql(call)
    assert error.value.code == "QUERY_REJECTED"
