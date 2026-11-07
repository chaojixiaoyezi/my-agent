"""W0基础合同回归；预期值与旧库DDL冻结自b09f402ba及本轮接口合同，不依赖外部计划文件。"""
from __future__ import annotations

import ast
import importlib
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.io.sqlite_schema import stamp_schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config, load_simple_yaml
from agent_py_agent.agent.settings.parameter_registry import ParameterSpec, parameter_registry
from scripts.build_constants_catalog import _is_protocol_constant, _module_entries, unit_for_name

_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED = {'slices': ['agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w1', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w2', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w3', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w4', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w5', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w6', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w7', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w8', 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w9'], 'config': {'openai_compat_gateway_enabled': False, 'llm_call_records_enabled': False, 'llm_call_records_max_rows': 50000, 'side_questions_enabled': False, 'model_retry_backoff_seconds': [2.0, 5.0, 15.0], 'custom_tools_enabled': False, 'notify_delivery_enabled': False, 'ops_remote_update_enabled': False, 'acp_bridge_enabled': False, 'acp_bridge_agents': {}, 'web_terminal_enabled': False}, 'capability': {'command_approval_filter_enabled': False, 'target_scope_policy_enabled': False}, 'errors': {'ARCHIVE_ENTRY_REJECTED': False, 'POLICY_RULE_DENIED': False, 'TARGET_SCOPE_DENIED': False, 'MAINTENANCE_BUSY': True, 'EXTERNAL_PROCESS_FAILED': True, 'OUTBOUND_DOMAIN_DENIED': False}, 'limits': {'LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS': 16, 'LLM_RECORDS_METADATA_MAX_BYTES': 4096, 'LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS': 604800, 'LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS': 2678400, 'LLM_RECORDS_AGGREGATE_MAX_MODELS': 200, 'LLM_RECORDS_PRUNE_BATCH_ROWS': 500, 'OPENAI_COMPAT_MAX_MESSAGES': 512, 'OPENAI_COMPAT_STREAM_CHUNK_CHARS': 2048, 'POLICY_RULES_MAX_PER_OWNER': 500, 'POLICY_PENDING_MAX_PER_OWNER': 200, 'POLICY_HISTORY_SEGMENT_BYTES': 4194304, 'POLICY_HISTORY_MAX_SEGMENTS': 8, 'POLICY_HISTORY_SCAN_MAX_RECORDS': 5000, 'POLICY_TASK_SUMMARY_SCAN_RECORDS': 2000, 'SCOPE_RULES_MAX_PER_OWNER': 500, 'SCOPE_TASK_RULES_MAX_PER_TASK': 100, 'POLICY_JUDGE_WORKERS': 2, 'SIDE_QUESTIONS_MAX_PER_CONVERSATION': 50, 'SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER': 2, 'SIDE_QUESTION_WORKERS': 4, 'SIDE_QUESTION_EVENT_RING': 256, 'SIDE_QUESTION_CONTEXT_MAX_MESSAGES': 40, 'SIDE_QUESTION_CONTEXT_MAX_CHARS': 120000, 'AGENT_PROMPT_HISTORY_MAX_PER_ROLE': 20, 'PROMPT_TRIGGERS_MAX_PER_ROLE': 32, 'SKILL_ZIP_MAX_COMPRESSION_RATIO': 100, 'SKILL_PACKAGE_FILE_MAX_BYTES': 1048576, 'CUSTOM_TOOLS_MAX_PER_OWNER': 50, 'CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS': 5, 'NOTIFY_CHANNELS_MAX_PER_OWNER': 50, 'NOTIFY_DRAIN_BATCH': 20, 'NOTIFY_WORKERS': 2, 'OPS_UPDATE_LOG_RING': 2000, 'FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER': 8, 'FS_WATCH_MAX_ENTRIES_PER_SCAN': 5000, 'FS_WATCH_MIN_SCAN_INTERVAL_SECONDS': 1.0, 'FS_WATCH_EVENT_RING': 1024, 'FS_WATCH_IDLE_EXPIRY_SECONDS': 900, 'FS_SEARCH_MAX_FILE_BYTES': 1048576, 'FS_SEARCH_MAX_CONCURRENT': 2, 'SCM_MAX_SUBSCRIPTIONS_PER_OWNER': 8, 'SCM_MAX_STATUS_ENTRIES': 2000, 'SCM_MAX_LOG_ENTRIES': 50, 'SCM_MAX_PATHS_PER_ACTION': 200, 'SCM_GIT_TIMEOUT_SECONDS': 15, 'SCM_MIN_REFRESH_INTERVAL_SECONDS': 1.0, 'ACP_MAX_SESSIONS_PER_OWNER': 4, 'ACP_EVENT_RING': 2048, 'ACP_PROBE_TIMEOUT_SECONDS': 15, 'ACP_TERMINATE_GRACE_SECONDS': 3.0, 'WEB_TERMINAL_MAX_SESSIONS_PER_OWNER': 4, 'WEB_TERMINAL_GRANT_TTL_SECONDS': 120, 'WEB_TERMINAL_READ_MAX_BYTES': 65536, 'PRESENCE_TTL_SECONDS': 30, 'PRESENCE_MAX_MEMBERS_PER_DOC': 50, 'DOC_STATE_MAX_BYTES': 262144}, 'ddl': ["CREATE TABLE IF NOT EXISTS llm_call_records (record_id TEXT PRIMARY KEY, call_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', backend TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}', started_at REAL NOT NULL, finished_at REAL, created_at REAL NOT NULL)", 'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_created ON llm_call_records(owner_id, created_at DESC, record_id DESC)', 'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_task ON llm_call_records(owner_id, task_id, created_at DESC)', 'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_model ON llm_call_records(owner_id, model, created_at DESC)', 'CREATE INDEX IF NOT EXISTS idx_outbox_scope_owner ON outbox_entries(scope, owner_id, created_at, outbox_id)']}

# 旧版完整建表及索引字面量用于真实旧库升级反例，禁止从待测新版反推旧库。
_ANCHOR_SQL = ('\n    CREATE TABLE IF NOT EXISTS metadata (\n        key TEXT PRIMARY KEY,\n        value TEXT NOT NULL\n    )\n    ', "\n    CREATE TABLE IF NOT EXISTS tasks (\n        task_id TEXT PRIMARY KEY,\n        owner_id TEXT NOT NULL,\n        thread_id TEXT NOT NULL DEFAULT '',\n        conversation_task_id TEXT NOT NULL DEFAULT '',\n        title TEXT NOT NULL DEFAULT '',\n        goal TEXT NOT NULL DEFAULT '',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", "\n    CREATE TABLE IF NOT EXISTS task_runs (\n        task_run_id TEXT PRIMARY KEY,\n        task_id TEXT NOT NULL,\n        status TEXT NOT NULL DEFAULT '',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL,\n        metadata_json TEXT NOT NULL DEFAULT '{}',\n        closed_at REAL NOT NULL DEFAULT 0\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id)', "\n    CREATE TABLE IF NOT EXISTS agent_runs (\n        agent_run_id TEXT PRIMARY KEY,\n        task_run_id TEXT NOT NULL,\n        parent_agent_run_id TEXT NOT NULL DEFAULT '',\n        delegation_id TEXT NOT NULL DEFAULT '',\n        run_id TEXT NOT NULL DEFAULT '',\n        role TEXT NOT NULL DEFAULT '',\n        status TEXT NOT NULL DEFAULT '',\n        current_attempt_id TEXT NOT NULL DEFAULT '',\n        current_attempt_generation INTEGER NOT NULL DEFAULT 0,\n        workspace_epoch INTEGER NOT NULL DEFAULT 1,\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_agent_runs_task_run ON agent_runs(task_run_id)', 'CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_agent_run_id)', 'CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id, created_at DESC)', "\n    CREATE TABLE IF NOT EXISTS agent_attempts (\n        attempt_id TEXT PRIMARY KEY,\n        agent_run_id TEXT NOT NULL,\n        attempt_generation INTEGER NOT NULL,\n        status TEXT NOT NULL DEFAULT '',\n        started_at REAL NOT NULL,\n        ended_at REAL NOT NULL DEFAULT 0,\n        metadata_json TEXT NOT NULL DEFAULT '{}',\n        UNIQUE(agent_run_id, attempt_generation)\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_agent_attempts_run ON agent_attempts(agent_run_id)', "\n    CREATE TABLE IF NOT EXISTS delegations (\n        delegation_id TEXT PRIMARY KEY,\n        parent_agent_run_id TEXT NOT NULL,\n        child_agent_run_id TEXT NOT NULL,\n        child_attempt_id TEXT NOT NULL,\n        granted_scope_json TEXT NOT NULL DEFAULT '{}',\n        created_at REAL NOT NULL,\n        UNIQUE(parent_agent_run_id, child_agent_run_id)\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_delegations_parent ON delegations(parent_agent_run_id)', 'CREATE INDEX IF NOT EXISTS idx_delegations_child ON delegations(child_agent_run_id)', "\n    CREATE TABLE IF NOT EXISTS workspace_bindings (\n        binding_id TEXT PRIMARY KEY,\n        owner_id TEXT NOT NULL,\n        agent_run_id TEXT NOT NULL,\n        attempt_id TEXT NOT NULL,\n        workspace_epoch INTEGER NOT NULL DEFAULT 1,\n        root_path TEXT NOT NULL,\n        readable_roots_json TEXT NOT NULL DEFAULT '[]',\n        writable_roots_json TEXT NOT NULL DEFAULT '[]',\n        extra_write_roots_json TEXT NOT NULL DEFAULT '[]',\n        roots_digest TEXT NOT NULL DEFAULT '',\n        status TEXT NOT NULL DEFAULT 'ACTIVE',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_workspace_bindings_run ON workspace_bindings(agent_run_id, status)', '\n    CREATE TABLE IF NOT EXISTS root_claims (\n        claim_id TEXT PRIMARY KEY,\n        binding_id TEXT NOT NULL,\n        agent_run_id TEXT NOT NULL,\n        attempt_id TEXT NOT NULL,\n        root_path TEXT NOT NULL,\n        created_at REAL NOT NULL,\n        UNIQUE(root_path)\n    )\n    ', 'CREATE INDEX IF NOT EXISTS idx_root_claims_binding ON root_claims(binding_id)', "\n    CREATE TABLE IF NOT EXISTS runtime_events (\n        seq INTEGER PRIMARY KEY AUTOINCREMENT,\n        event_id TEXT NOT NULL UNIQUE,\n        event_type TEXT NOT NULL,\n        attempt_id TEXT NOT NULL,\n        agent_run_id TEXT NOT NULL,\n        task_run_id TEXT NOT NULL DEFAULT '',\n        payload_json TEXT NOT NULL DEFAULT '{}',\n        created_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_runtime_events_attempt ON runtime_events(attempt_id, created_at)', 'CREATE INDEX IF NOT EXISTS idx_runtime_events_type ON runtime_events(event_type, created_at)', 'CREATE INDEX IF NOT EXISTS idx_runtime_events_type_seq ON runtime_events(event_type, seq)', 'CREATE INDEX IF NOT EXISTS idx_runtime_events_type_identity ON runtime_events(event_type, agent_run_id, attempt_id, seq)', "\n    CREATE TABLE IF NOT EXISTS tool_operations (\n        operation_id TEXT PRIMARY KEY,\n        agent_run_id TEXT NOT NULL,\n        attempt_id TEXT NOT NULL,\n        attempt_generation INTEGER NOT NULL,\n        tool_operation_generation INTEGER NOT NULL,\n        operation_type TEXT NOT NULL,\n        canonical_scope TEXT NOT NULL DEFAULT '',\n        status TEXT NOT NULL DEFAULT 'CLAIMED',\n        handler_started_at REAL NOT NULL DEFAULT 0,\n        settled_at REAL NOT NULL DEFAULT 0,\n        outcome_json TEXT NOT NULL DEFAULT '{}',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_tool_operations_attempt ON tool_operations(attempt_id)', 'CREATE INDEX IF NOT EXISTS idx_tool_operations_run ON tool_operations(agent_run_id)', 'CREATE INDEX IF NOT EXISTS idx_tool_operations_status ON tool_operations(status)', "\n    CREATE TABLE IF NOT EXISTS resource_locks (\n        lock_id TEXT PRIMARY KEY,\n        canonical_scope TEXT NOT NULL UNIQUE,\n        holder_instance TEXT NOT NULL,\n        pid INTEGER NOT NULL DEFAULT 0,\n        start_token TEXT NOT NULL DEFAULT '',\n        attempt_id TEXT NOT NULL,\n        attempt_generation INTEGER NOT NULL,\n        workspace_epoch INTEGER NOT NULL,\n        tool_operation_generation INTEGER NOT NULL DEFAULT 0,\n        lease_expires_at REAL NOT NULL,\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_resource_locks_lease ON resource_locks(lease_expires_at)', "\n    CREATE TABLE IF NOT EXISTS resource_mutations (\n        mutation_id TEXT PRIMARY KEY,\n        canonical_scope TEXT NOT NULL UNIQUE,\n        version INTEGER NOT NULL DEFAULT 0,\n        state TEXT NOT NULL DEFAULT 'STABLE',\n        dirty_reason TEXT NOT NULL DEFAULT '',\n        attempt_id TEXT NOT NULL DEFAULT '',\n        updated_at REAL NOT NULL\n    )\n    ", "\n    CREATE TABLE IF NOT EXISTS publish_operations (\n        publish_id TEXT PRIMARY KEY,\n        binding_id TEXT NOT NULL,\n        agent_run_id TEXT NOT NULL,\n        attempt_id TEXT NOT NULL,\n        workspace_epoch INTEGER NOT NULL,\n        status TEXT NOT NULL DEFAULT 'STAGING',\n        manifest_json TEXT NOT NULL DEFAULT '[]',\n        preimage_digests_json TEXT NOT NULL DEFAULT '{}',\n        postimage_digests_json TEXT NOT NULL DEFAULT '{}',\n        committed_at REAL NOT NULL DEFAULT 0,\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_publish_ops_attempt ON publish_operations(attempt_id)', "\n    CREATE TABLE IF NOT EXISTS artifact_records (\n        artifact_record_id TEXT PRIMARY KEY,\n        attempt_id TEXT NOT NULL,\n        agent_run_id TEXT NOT NULL,\n        publish_id TEXT NOT NULL DEFAULT '',\n        rel_path TEXT NOT NULL,\n        content_digest TEXT NOT NULL,\n        size INTEGER NOT NULL DEFAULT 0,\n        content_path TEXT NOT NULL DEFAULT '',\n        created_at REAL NOT NULL\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_artifact_records_attempt ON artifact_records(attempt_id)', '\n    CREATE TABLE IF NOT EXISTS id_path_mapping (\n        opaque_id TEXT PRIMARY KEY,\n        id_kind TEXT NOT NULL,\n        directory_id TEXT NOT NULL,\n        created_at REAL NOT NULL\n    )\n    ', 'CREATE INDEX IF NOT EXISTS idx_id_path_mapping_kind ON id_path_mapping(id_kind)', "\n    CREATE TABLE IF NOT EXISTS wake_queue (\n        wake_id TEXT PRIMARY KEY,\n        root_task_id TEXT NOT NULL,\n        root_run_id TEXT NOT NULL DEFAULT '',\n        root_thread_id TEXT NOT NULL DEFAULT '',\n        kind TEXT NOT NULL DEFAULT 'sleep',\n        next_due_at REAL NOT NULL,\n        woke_at REAL NOT NULL DEFAULT 0,\n        status TEXT NOT NULL DEFAULT 'pending',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL,\n        -- WK-INT(2026-08-20): wake 持久化状态机升级——一次性闹钟 → 可恢复待办。\n        -- claimed 期间带 lease；失败回 pending 并按 retry_after 退避重试；\n        -- 429 限流把 retry_after 推到恢复点；进程崩溃后 lease 过期由 reclaim 回收。\n        lease_until REAL NOT NULL DEFAULT 0,\n        retry_after REAL NOT NULL DEFAULT 0,\n        attempt_count INTEGER NOT NULL DEFAULT 0,\n        last_error TEXT NOT NULL DEFAULT ''\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_wake_queue_due ON wake_queue(status, next_due_at)', 'CREATE INDEX IF NOT EXISTS idx_wake_queue_task ON wake_queue(root_task_id)', "\n    CREATE TABLE IF NOT EXISTS outbox_entries (\n        outbox_id TEXT PRIMARY KEY,\n        effect_key TEXT NOT NULL UNIQUE,\n        owner_id TEXT NOT NULL DEFAULT '',\n        task_run_id TEXT NOT NULL DEFAULT '',\n        scope TEXT NOT NULL DEFAULT '',\n        payload_json TEXT NOT NULL DEFAULT '{}',\n        status TEXT NOT NULL DEFAULT 'PENDING',\n        attempts INTEGER NOT NULL DEFAULT 0,\n        claimed_at REAL NOT NULL DEFAULT 0,\n        claimed_by TEXT NOT NULL DEFAULT '',\n        next_retry_at REAL NOT NULL DEFAULT 0,\n        provider_evidence_json TEXT NOT NULL DEFAULT '{}',\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL,\n        settled_at REAL NOT NULL DEFAULT 0\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_outbox_status_retry ON outbox_entries(status, next_retry_at)', 'CREATE INDEX IF NOT EXISTS idx_outbox_task_run ON outbox_entries(task_run_id)', "\n    CREATE TABLE IF NOT EXISTS inbox_entries (\n        inbox_id TEXT PRIMARY KEY,\n        effect_key TEXT NOT NULL UNIQUE,\n        sender TEXT NOT NULL DEFAULT '',\n        task_run_id TEXT NOT NULL DEFAULT '',\n        scope TEXT NOT NULL DEFAULT '',\n        payload_json TEXT NOT NULL DEFAULT '{}',\n        status TEXT NOT NULL DEFAULT 'RECEIVED',\n        received_at REAL NOT NULL,\n        processed_at REAL NOT NULL DEFAULT 0\n    )\n    ", 'CREATE INDEX IF NOT EXISTS idx_wake_queue_lease ON wake_queue(status, lease_until)', 'CREATE INDEX IF NOT EXISTS idx_task_runs_closed_at ON task_runs(closed_at)', 'CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id, created_at DESC)', 'CREATE INDEX IF NOT EXISTS idx_agent_runs_current_attempt ON agent_runs(current_attempt_id)', 'CREATE INDEX IF NOT EXISTS idx_agent_attempts_status_ended ON agent_attempts(status, ended_at)', "CREATE INDEX IF NOT EXISTS idx_agent_attempts_stale_time ON agent_attempts(started_at, agent_run_id, attempt_id) WHERE status IN ('running','created','unknown')", "CREATE INDEX IF NOT EXISTS idx_agent_runs_created_time ON agent_runs(created_at, agent_run_id) WHERE status='created'", 'CREATE INDEX IF NOT EXISTS idx_tool_operations_status ON tool_operations(status)', 'CREATE INDEX IF NOT EXISTS idx_resource_locks_attempt ON resource_locks(attempt_id)', 'CREATE INDEX IF NOT EXISTS idx_runtime_events_task_run ON runtime_events(task_run_id, seq)', 'CREATE INDEX IF NOT EXISTS idx_resource_mutations_state ON resource_mutations(state)')
_ANCHOR_FINGERPRINT = '94245fa2e1ae1819c0d6141e80c9ce06c33c578489498caf027f960bc77ed0a8'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """所有临时配置和数据库只落在当前用例的隔离目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))


def test_slice_modules_preregistered_and_missing_slices_skip(monkeypatch):
    expected = tuple(_EXPECTED["slices"])
    assert v13.V13_SLICE_MODULES[-len(expected):] == expected
    assert v13.V13_SLICE_MODULES[-len(expected) - 1].endswith("business_api_v13_sessions")
    actual_import = importlib.import_module
    seen = []

    def missing_slices(name):
        if name in expected:
            seen.append(name)
            raise ModuleNotFoundError(name=name)
        return actual_import(name)

    monkeypatch.setattr(v13.importlib, "import_module", missing_slices)
    loaded = v13._load_registry()
    assert seen == list(expected)
    assert loaded == v13._load_registry(v13.V13_SLICE_MODULES[:-len(expected)])


def test_present_slice_inner_module_error_is_not_swallowed(monkeypatch):
    def broken_slice(name):
        raise ModuleNotFoundError(name="gap158_missing_inner_dependency")

    monkeypatch.setattr(v13.importlib, "import_module", broken_slice)
    with pytest.raises(ModuleNotFoundError) as error:
        v13._load_registry([_EXPECTED["slices"][0]])
    assert error.value.name == "gap158_missing_inner_dependency"


@pytest.mark.parametrize("source,key,default", [
    (source, key, value) for source in ("config", "capability")
    for key, value in _EXPECTED[source].items()
])
def test_new_config_keys_defaults_and_yaml_docs(source, key, default):
    config = AgentConfig() if source == "config" else CapabilityConfig()
    assert hasattr(config, key), key
    assert getattr(config, key) == default
    filename = "agent_config.yaml" if source == "config" else "capability_config.yaml"
    path = _ROOT / "agent_py_agent/config" / filename
    assert load_simple_yaml(path)[key] == default
    loader = load_config if source == "config" else load_capability_config
    assert getattr(loader(path), key) == default
    spec = parameter_registry()[key]
    assert spec.default == default and re.search(r"[\u4e00-\u9fff]", spec.description)


@pytest.mark.parametrize("code,retryable", _EXPECTED["errors"].items())
def test_new_error_codes_registered(code, retryable):
    contract = error_contract(code)
    assert contract.code == code
    assert contract.retryable is retryable
    assert re.search(r"[\u4e00-\u9fff]", contract.recovery_hint)
    assert contract.recommended_action


def test_limits_module_values_match_contract():
    path = _ROOT / "agent_py_agent/agent/business_spaces/gap158_limits.py"
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    nodes = ast.parse("\n".join(lines)).body
    assignments = [node for node in nodes if isinstance(node, ast.Assign)]
    assert len(assignments) == len(_EXPECTED["limits"])
    assert all(isinstance(node.value, ast.Constant) for node in assignments)
    assert {node.targets[0].id: node.value.value for node in assignments} == _EXPECTED["limits"]
    assert all(lines[node.lineno - 2].startswith("#") and
               re.search(r"[\u4e00-\u9fff]", lines[node.lineno - 2]) for node in assignments)
    assert all(isinstance(node, ast.Assign) or isinstance(node, ast.Expr) and
               isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) for node in nodes)
    module = importlib.import_module("agent_py_agent.agent.business_spaces.gap158_limits")
    assert {name: getattr(module, name) for name in _EXPECTED["limits"]} == _EXPECTED["limits"]


def test_contract_limits_have_real_catalog_units_and_descriptions():
    missing_units = [name for name in _EXPECTED["limits"]
                     if not _is_protocol_constant(name) and not unit_for_name(name)]
    assert missing_units == []
    path = _ROOT / "agent_py_agent/agent/business_spaces/gap158_limits.py"
    assert path.is_file()
    entries = _module_entries(_ROOT, path)
    assert {entry["name"] for entry in entries} == {
        name for name in _EXPECTED["limits"] if not _is_protocol_constant(name)}
    assert all(entry["unit"] and entry["description"] for entry in entries)


@pytest.mark.parametrize("existing", [False, True])
def test_runtime_db_ddl_idempotent(tmp_path, existing):
    path = tmp_path / "runtime.db"
    original = {}
    if existing:
        with sqlite3.connect(path) as conn:
            for statement in _ANCHOR_SQL:
                conn.execute(statement)
            stamp_schema(conn, _ANCHOR_FINGERPRINT)
            conn.execute("INSERT INTO tasks(task_id,owner_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                         ("anchor-task", "owner", "保留旧数据", 1.0, 1.0))
            conn.execute("INSERT INTO outbox_entries(outbox_id,effect_key,owner_id,scope,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?)", ("anchor-outbox", "unique-effect", "owner", "notify", 1.0, 1.0))
            original = {row[0]: row[1:] for row in conn.execute(
                "SELECT name,rootpage,sql FROM sqlite_master WHERE type='table'")}
    RuntimeRepository(path)
    with sqlite3.connect(path) as conn:
        expected_names = {"llm_call_records", "idx_llm_records_owner_created", "idx_llm_records_owner_task",
                          "idx_llm_records_owner_model", "idx_outbox_scope_owner"}
        objects = {row[0]: row[1] for row in conn.execute("SELECT name,sql FROM sqlite_master")}
        assert expected_names <= objects.keys()
        for statement in _EXPECTED["ddl"]:
            name = statement.split(" IF NOT EXISTS ", 1)[1].split(" ", 1)[0]
            assert objects[name] == statement.replace(" IF NOT EXISTS", "")
        if existing:
            tables = {row[0]: row[1:] for row in conn.execute(
                "SELECT name,rootpage,sql FROM sqlite_master WHERE type='table'")}
            assert all(tables[name] == value for name, value in original.items())
            assert conn.execute("SELECT title FROM tasks WHERE task_id='anchor-task'").fetchone() == ("保留旧数据",)
            assert conn.execute("SELECT effect_key FROM outbox_entries").fetchall() == [("unique-effect",)]
        conn.execute("INSERT INTO llm_call_records(record_id,call_id,owner_id,status,started_at,created_at) "
                     "VALUES(?,?,?,?,?,?)", ("record", "call", "owner", "completed", 1.0, 1.0))
    before = path.read_bytes()
    repository = RuntimeRepository(path)
    assert path.read_bytes() == before
    statements = []
    with repository._runtime_connection() as conn:
        conn.set_trace_callback(statements.append)
        assert repository._install_runtime_schema(conn) is False
        conn.set_trace_callback(None)
        assert tuple(conn.execute("SELECT input_tokens,metadata_json FROM llm_call_records").fetchone()) == (0, "{}")
    assert not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DROP"))
                   for sql in statements)


def test_default_switches_and_retry_delays_preserve_old_behavior():
    from agent_py_agent.agent.backends.gateway_helpers import _RETRYABLE_HTTP_DELAYS_SECONDS

    config = AgentConfig()
    for key, default in _EXPECTED["config"].items():
        assert hasattr(config, key)
        if type(default) is bool:
            assert getattr(config, key) is False
    assert tuple(config.model_retry_backoff_seconds) == _RETRYABLE_HTTP_DELAYS_SECONDS
    assert CapabilityConfig().command_approval_filter_enabled is False
    assert CapabilityConfig().target_scope_policy_enabled is False
    assert "llm.gateway.test" not in v13._load_registry()[0]


@pytest.mark.parametrize("value", [[0, 1.5, 9], (0, 1.5, 9), "[0, 1.5, 9]", []])
def test_numeric_retry_list_set_reads_back_from_real_yaml(tmp_path, value):
    key = "model_retry_backoff_seconds"
    assert key in parameter_registry()
    path = tmp_path / "agent.yaml"
    original = "\ufeff# 保留说明\r\nagent_name: keep\r\nmodel_retry_backoff_seconds:\r\n  - 2.0\r\n  - 5.0\r\n".encode("utf-8")
    path.write_bytes(original)
    expected = [] if value == [] else [0, 1.5, 9]
    spec = parameter_registry()[key]
    assert spec.writable and spec.writer
    result = changes.set_parameter(key, value, paths=changes.WritePaths(path), origin=changes.ChangeOrigin("test"))
    assert result["ok"], result
    assert getattr(load_config(path), key) == expected
    assert path.read_bytes().startswith("\ufeff# 保留说明\r\nagent_name: keep\r\n".encode("utf-8"))
    undo = changes.revert_change(result["change_id"], paths=changes.WritePaths(path), origin=changes.ChangeOrigin("test"))
    assert undo["ok"], undo
    assert getattr(load_config(path), key) == [2.0, 5.0]
    assert changes.reset_parameter(key, paths=changes.WritePaths(path), origin=changes.ChangeOrigin("test"))["ok"]
    assert getattr(load_config(path), key) == [2.0, 5.0, 15.0]


@pytest.mark.parametrize("value", [[True], ["2"], [[2]], [-1], [float("nan")], [float("inf")],
                                    "[NaN]", "[1]\nagent_name: replaced", {"delay": 1}, 1, "bad", [10 ** 1000]])
def test_numeric_retry_list_rejects_invalid_values_without_writes(tmp_path, value):
    key = "model_retry_backoff_seconds"
    assert key in parameter_registry()
    path = tmp_path / "agent.yaml"
    path.write_text("agent_name: keep\n")
    original = path.read_bytes()
    result = changes.set_parameter(key, value, paths=changes.WritePaths(path), origin=changes.ChangeOrigin("test"))
    assert result["ok"] is False and result["code"] == "PARAMETER_INVALID", result
    assert path.read_bytes() == original
    assert changes.parameter_history(user_path=path) == []


def test_numeric_tuple_rendering_is_generic_not_key_specific():
    spec = ParameterSpec(key="webhook_retry_delays_seconds", default=(1.0, 2.0), value_type="list",
                         description="数字重试间隔", category="其它", safety="free", masked=False,
                         effect="restart_gateway")
    assert spec.writer and spec.writable
    normalized, rendered = changes._render(spec, (3.0, 4.5))
    assert list(normalized) == [3.0, 4.5]
    assert json.loads(rendered) == [3.0, 4.5]
    assert changes._same((3.0, 4.5), [3.0, 4.5])
    assert not changes._same([1], [True])
    assert not changes._same([1.0], ["1"])
    assert not changes._same([1], [1, 2])
    assert not replace(spec, key="allowed_ports", safety="boundary").writable


def test_numeric_list_cas_and_ledger_failure_preserve_original_bytes(tmp_path, monkeypatch):
    key = "model_retry_backoff_seconds"
    assert key in parameter_registry()
    path = tmp_path / "agent.yaml"
    path.write_text("agent_name: keep\n")
    original = path.read_bytes()
    result = changes.set_parameter(key, [1, 2], paths=changes.WritePaths(path, expected_revision="wrong"),
                                   origin=changes.ChangeOrigin("test"))
    assert result["code"] == "CONFIG_REVISION_CONFLICT"
    assert path.read_bytes() == original

    def ledger_failure(*args, **kwargs):
        raise OSError("controlled ledger failure")

    monkeypatch.setattr(changes, "_record", ledger_failure)
    result = changes.set_parameter(key, [1, 2], paths=changes.WritePaths(path), origin=changes.ChangeOrigin("test"))
    assert result["code"] == "USER_CONFIG_WRITE_FAILED"
    assert path.read_bytes() == original and changes.parameter_history(user_path=path) == []
