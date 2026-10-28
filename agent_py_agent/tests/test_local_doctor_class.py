# LLM: 使用真实领域服务与临时事实验证CLI边界；测试替身只替代底层写入，不改变生产接线、TTL或安全判据。
# 模块用途: 回归本地诊断、正式领域接口和索引重建的计数、写入目标及异常合同。
"""local_doctor 模块测试。

测试本地诊断、领域索引重建及本轮允许范围内的正式领域接口；不以Mock兼容分支改变生产接线。
"""
from __future__ import annotations

import contextlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.cli.local_doctor import (
    DoctorCheckRequest,
    _add_doctor_check,
    build_local_doctor_report,
    build_status_suggestions,
    rebuild_local_store,
    rebuild_subagent_index,
)

# ── _add_doctor_check 测试 ─────────────────────────────────────────────────

def _doctor_request(overrides: dict | None = None) -> DoctorCheckRequest:
    data = {
        "name": "test_check",
        "ok": True,
        "severity": "P1",
        "message": "检查通过",
        "details": None,
    }
    data.update(overrides or {})
    return DoctorCheckRequest(**data)


def test_add_doctor_check_ok():
    """测试添加正常的检查项。"""
    checks = []
    _add_doctor_check(checks, _doctor_request())
    assert len(checks) == 1
    assert checks[0]["name"] == "test_check"
    assert checks[0]["ok"] is True
    assert checks[0]["severity"] == "ok"
    assert checks[0]["details"] == {}


def test_add_doctor_check_with_details():
    """测试带详情信息的检查项。"""
    checks = []
    details = {"path": "/tmp/test", "count": 5}
    _add_doctor_check(
        checks,
        _doctor_request({
            "name": "test_detail",
            "ok": False,
            "severity": "P0",
            "message": "检查失败",
            "details": details,
        }),
    )
    assert checks[0]["details"] == details


def test_add_doctor_check_not_ok_sets_severity():
    """测试检查失败时 severity 被设置为传入值。"""
    checks = []
    _add_doctor_check(
        checks,
        _doctor_request({"name": "fail_check", "ok": False, "severity": "P2", "message": "有问题"}),
    )
    assert checks[0]["severity"] == "P2"


@dataclass
class DoctorMockConfig:
    """Bundle of mock configuration for _mock_agent_for_doctor."""
    missing_files: list = field(default_factory=list)
    source_counts: dict = field(default_factory=dict)
    request_counts: dict = field(default_factory=dict)
    stale_processing: list = field(default_factory=list)
    memory_count: int = 5
    subagent_runs: list = field(default_factory=list)


def _mock_agent_for_doctor(tmp_path, cfg: DoctorMockConfig | None = None):
    """Build a fully mocked agent for local_doctor tests."""
    if cfg is None:
        cfg = DoctorMockConfig()
    db_file = tmp_path / "test.db"
    files_dir = tmp_path / "files"
    events_file = tmp_path / "events.jsonl"
    files_dir.mkdir()
    db_file.touch()
    events_file.touch()

    mock_agent = MagicMock()
    mock_agent.configure_mock(**{
        "local_store.stats.return_value": {
            "db_path": str(db_file),
            "files_dir": str(files_dir),
            "events_path": str(events_file),
            "record_count": 10,
        },
        "local_store.source_counts.return_value": cfg.source_counts or {"memory": 5, "gateway": 3},
        "memory.path": tmp_path / "memory",
        "subagents.workspace": tmp_path / "subagents",
        "config.gateway_processing_timeout_seconds": 300,
        "config.workspace_root": "",
    })
    mock_agent.root = tmp_path
    mock_agent.local_store.missing_content_files.return_value = cfg.missing_files or []
    mock_agent.subagents.list_runs.return_value = cfg.subagent_runs or []
    return mock_agent


def _build_doctor_report_with_mocks(mock_agent, tmp_path, **kwargs):
    """Call build_local_doctor_report with all required patches."""
    request_counts = kwargs.pop("request_counts", None)
    stale_processing = kwargs.pop("stale_processing", None)
    memory_count = kwargs.pop("memory_count", 5)
    workspace_root_mode = kwargs.pop("workspace_root_mode", None)
    if kwargs:
        raise TypeError(f"Unexpected doctor report options: {sorted(kwargs)}")

    with contextlib.ExitStack() as stack:
        mock_paths = stack.enter_context(patch("agent_py_agent.cli.local_doctor.gateway_paths"))
        mock_paths.return_value = MagicMock(root=tmp_path)
        mock_counts = stack.enter_context(patch("agent_py_agent.cli.local_doctor.gateway_request_counts"))
        mock_counts.return_value = request_counts or {"pending": 0, "processing": 0, "done": 5}
        mock_stale = stack.enter_context(patch("agent_py_agent.cli.local_doctor.gateway_stale_processing"))
        mock_stale.return_value = stale_processing or []
        mock_mem = stack.enter_context(patch("agent_py_agent.cli.local_doctor._memory_record_count"))
        mock_mem.return_value = memory_count
        return build_local_doctor_report(mock_agent, workspace_root_mode=workspace_root_mode)


def test_build_local_doctor_report_basic(tmp_path):
    """测试基本报告构建。"""
    mock_agent = _mock_agent_for_doctor(tmp_path)
    result = _build_doctor_report_with_mocks(mock_agent, tmp_path)
    assert result["ok"] is True
    assert "checks" in result
    assert "suggestions" in result
    assert result["workspace_scope"] == {
        "workspace_root": str(mock_agent.root),
        "mode": "implicit_cwd",
        "configured_value": "",
    }
    assert any("--workspace-root" in item for item in result["suggestions"])


def test_build_local_doctor_report_marks_explicit_workspace(tmp_path):
    mock_agent = _mock_agent_for_doctor(tmp_path)
    result = _build_doctor_report_with_mocks(
        mock_agent,
        tmp_path,
        workspace_root_mode="explicit_cli",
    )
    assert result["workspace_scope"]["workspace_root"] == str(tmp_path)
    assert result["workspace_scope"]["mode"] == "explicit_cli"
    assert not any("命令调用目录" in item for item in result["suggestions"])


def test_build_local_doctor_report_with_stale_processing(tmp_path):
    """测试存在 stale processing 时的报告。"""
    mock_agent = _mock_agent_for_doctor(tmp_path)
    result = _build_doctor_report_with_mocks(
        mock_agent, tmp_path,
        request_counts={"pending": 2, "processing": 1, "done": 5},
        stale_processing=[{"id": "stale-1"}],
    )
    assert result["ok"] is False
    assert any("processing" in s.lower() for s in result["suggestions"])


def test_build_local_doctor_report_missing_content_files(tmp_path):
    """测试存在缺失正文文件时的报告。"""
    mock_agent = _mock_agent_for_doctor(tmp_path, DoctorMockConfig(missing_files=["file1.txt", "file2.txt"], source_counts={"memory": 5}))
    result = _build_doctor_report_with_mocks(mock_agent, tmp_path, request_counts={})
    assert result["ok"] is False
    content_check = next(c for c in result["checks"] if c["name"] == "local_store_content_files")
    assert content_check["ok"] is False


# ── rebuild_subagent_index 测试 ───────────────────────────────────────────

# LLM: 改用真实索引服务承接CLI调用，原index_task调用断言与任务计数仍保留。
# 函数用途: 验证基本任务重建不依赖MagicMock虚构的服务返回值。
def test_rebuild_subagent_index_basic():
    """测试基本子代理索引重建。"""
    from agent_py_agent.agent.subagents.services.indexing.service import SubAgentIndexingService

    mock_agent = MagicMock()
    mock_agent.subagents.indexing = SubAgentIndexingService(mock_agent.subagents)
    mock_agent.subagents.indexing.index_task = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.task_dir = "/tmp/tasks/task-123"
    mock_agent.subagents.list_runs.return_value = [mock_task]

    count = rebuild_subagent_index(mock_agent)

    assert count == 1
    mock_agent.subagents.indexing.index_task.assert_called_once_with(mock_task)


# LLM: 真实服务读取原事实文件，保持底层任务写入替身，不用生产Mock兼容分支。
# 函数用途: 核对任务与两类存在文件的准确重建计数。
def test_rebuild_subagent_index_with_existing_files(tmp_path):
    """测试存在工单文件时的索引重建。"""
    from agent_py_agent.agent.subagents.services.indexing.service import SubAgentIndexingService

    mock_agent = MagicMock()
    mock_agent.subagents.indexing = SubAgentIndexingService(mock_agent.subagents)
    mock_agent.subagents.indexing.index_task = MagicMock()
    task_dir = tmp_path / "task-123"
    task_dir.mkdir(parents=True)
    (task_dir / "WORK_LOG.md").write_text("work log content", encoding="utf-8")
    (task_dir / "execution_context.json").write_text("{}", encoding="utf-8")

    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.task_dir = str(task_dir)
    mock_agent.subagents.list_runs.return_value = [mock_task]

    count = rebuild_subagent_index(mock_agent)

    assert count == 3  # task + WORK_LOG.md + execution_context.json


# ── rebuild_local_store 测试 ───────────────────────────────────────────────

def test_rebuild_local_store_memory_only():
    """测试仅重建 memory 源。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 10
    mock_agent.local_store.stats.return_value = {"record_count": 10}
    mock_agent.local_store.source_counts.return_value = {"memory": 10}

    result = rebuild_local_store(mock_agent, sources={"memory"})

    assert result["reset"] is False
    assert "memory" in result["sources"]
    assert result["memory_indexed"] == 10


def test_rebuild_local_store_with_reset():
    """测试带 reset 的重建。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 5
    mock_agent.local_store.rebuild_fts.return_value = 100
    mock_agent.local_store.stats.return_value = {"record_count": 5}
    mock_agent.local_store.source_counts.return_value = {"memory": 5}

    result = rebuild_local_store(mock_agent, sources={"memory", "fts"}, reset=True)

    assert result["reset"] is True
    mock_agent.local_store.reset.assert_called_once()
    assert result["fts_rebuilt"] == 100


def test_rebuild_local_store_all_sources():
    """测试重建所有源。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 10
    mock_agent.local_store.rebuild_fts.return_value = 50
    mock_agent.local_store.stats.return_value = {"record_count": 30}
    mock_agent.local_store.source_counts.return_value = {"memory": 10, "subagent": 20}

    with patch("agent_py_agent.cli.local_doctor.rebuild_gateway_index") as mock_gw:
        mock_gw.return_value = 5
        mock_agent.subagents.list_runs.return_value = []

        result = rebuild_local_store(mock_agent, sources={"memory", "gateway", "subagent", "fts"})

    assert result["memory_indexed"] == 10
    assert result["gateway_indexed"] == 5
    assert "subagent_indexed" in result


# ── build_status_suggestions 测试 ─────────────────────────────────────────

def test_build_status_suggestions_gateway_stopped_with_requests():
    """测试 gateway 停止但队列有请求时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "stopped", "request_counts": {"pending": 5}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("gateway start" in s for s in suggestions)


def test_build_status_suggestions_stale_gateway():
    """测试 stale gateway 的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "stale", "request_counts": {}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("restart" in s for s in suggestions)


def test_build_status_suggestions_failed_requests():
    """测试有失败请求时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "running", "request_counts": {"failed": 3}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("gateway logs" in s or "timeline" in s for s in suggestions)


def test_build_status_suggestions_empty_local_store():
    """测试本地存储为空时的建议。"""
    mock_agent = MagicMock()
    mock_agent.subagents.list_runs.return_value = [MagicMock()]
    mock_agent.memory.path = Path("/tmp/memory")
    payload = {
        "gateway": {"status": "stopped", "request_counts": {}},
        "local_store": {"record_count": 0},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("local-rebuild" in s for s in suggestions)


def test_build_status_suggestions_hot_subagents():
    """测试存在热子代理时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "running", "request_counts": {}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 3},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("subagents-due-check" in s for s in suggestions)


def test_build_status_suggestions_no_timeline():
    """测试无时间线但有记录时的建议。"""
    mock_agent = MagicMock()
    mock_agent.subagents.list_runs.return_value = []
    payload = {
        "gateway": {"status": "running", "request_counts": {}},
        "local_store": {"record_count": 100},
        "subagents": {"hot_count": 0},
        "timeline": None,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("timeline" in s and "rebuild" in s for s in suggestions)


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_build_local_doctor_report_empty_local_store_with_sources(tmp_path):
    """测试本地存储为空但有其他来源时的建议。"""
    mock_agent = _mock_agent_for_doctor(tmp_path, DoctorMockConfig(source_counts={}, subagent_runs=[MagicMock()]))
    result = _build_doctor_report_with_mocks(mock_agent, tmp_path, request_counts={"pending": 1}, memory_count=10)
    assert any("local-rebuild" in s for s in result["suggestions"])


def test_add_doctor_check_empty_message():
    """测试空消息的检查项。"""
    checks = []
    _add_doctor_check(checks, _doctor_request({"name": "empty_msg", "message": ""}))
    assert checks[0]["message"] == ""


def test_rebuild_local_store_unknown_source():
    """测试未知来源被忽略（只处理已知来源）。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 0
    mock_agent.local_store.stats.return_value = {"record_count": 0}
    mock_agent.local_store.source_counts.return_value = {}

    result = rebuild_local_store(mock_agent, sources={"unknown_source"})

    # unknown_source 保留在 sources 列表中，但不会触发任何处理
    assert "unknown_source" in result["sources"]


# LLM: 本轮唯一授权测试文件同时守住新领域入口；缺入口先断言失败，不让导入错误代替红例。
# 函数用途: 回归纯计时对冻结时间、未知耗时和失败状态的原始展示合同。
def test_formal_turn_timing_keeps_frozen_and_unknown_facts():
    import importlib.util

    name = 'agent_py_agent.agent.conversation.turn_timing'
    assert importlib.util.find_spec(name) is not None, '计时领域入口尚不存在'
    timing = __import__(name, fromlist=['render_turn_timing'])
    assert timing.format_turn_duration(0.1) == '1s'
    assert timing.format_turn_duration(3661) == '1h 1m 1s'
    assert timing.format_turn_duration(float('nan')) == '未知'
    assert timing.render_turn_timing({'completed_at': None}) == ''
    assert timing.render_turn_timing({'completed_at': 'invalid'}) == ''
    frozen = {'completed_at': 1700000000.0, 'elapsed_seconds': None, 'status': 'blocked', 'logical_id': 'turn'}
    rendered = timing.render_turn_timing(frozen)
    assert '未知' in rendered and '受阻于' in rendered and '完成于' not in rendered
    assert rendered == timing.render_turn_timing(frozen)


# LLM: 使用临时事实文件和真实领域读口，不构造Agent/Store或授执行；自学习due必须覆盖坏预检。
# 函数用途: 固定冷预检三态、紧急恢复、关闭配置及无副作用合同。
def test_formal_cold_curator_precheck_keeps_three_states(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent import memory_api

    query = getattr(memory_api, 'cold_owner_curator_precheck', None)
    assert callable(query), '记忆领域只读预检尚不存在'
    home = tmp_path / 'cold'
    config = SimpleNamespace(memory_curator_enabled=True, enable_self_learning=True, timezone='')
    assert query(home, config=config) == ('not_due', False)
    assert not home.exists()
    home.mkdir()
    policy = home / 'memory_policy.json'
    policy.write_text('broken')
    provider = MagicMock(return_value=None)
    assert query(home, config=config, curator_config_provider=provider) == ('unknown', False)
    provider.assert_not_called()
    requests = home / 'data/skill_learning/requests'
    requests.mkdir(parents=True)
    (requests / 'work.json').write_text('{}')
    assert query(home, config=config, curator_config_provider=provider) == ('due', False)
    provider.assert_not_called()
    policy.write_text('{}')
    state = home / 'memory/curator/state.json'
    state.parent.mkdir(parents=True)
    state.write_text('{"pending_reasons":["recovery"]}')
    assert query(home, config=config) == ('unknown', False)  # 原模型拒绝不支持的reason，不放宽校验。
    state.write_text('{"pending_reasons":["admin"]}')
    assert query(home, config=config, curator_config_provider=provider) == ('due', True)
    provider.assert_called_once_with()
    config.enable_self_learning = False
    config.memory_curator_enabled = False
    assert query(home, config=config, curator_config_provider=provider) == ('not_due', False)
    provider.assert_called_once_with()  # 关闭策展不提前访问配置provider。


# LLM: 完整定义与存量JSON读取保持两种合同，不放宽类型、id/kind或必填Key。
# 函数用途: 回归共享描述符构造器的完整输入验证及旧记录读取兼容性。
def test_formal_asset_definition_preserves_full_and_legacy_contracts():
    from agent_py_agent.agent.assets import descriptor

    build = getattr(descriptor, 'asset_descriptor_from_definition', None)
    assert callable(build), '完整资产定义构造器尚不存在'
    payload = dict(name='sample', access_targets=[], data_sources=[], credential_refs=[], io_contract={})
    value = build(payload, 'script-one', 'script')
    assert isinstance(value, descriptor.AssetDescriptor) and value.id == 'script-one' and value.kind == 'script'
    assert 'id' not in payload
    with pytest.raises(ValueError, match='id/kind'):
        build(dict(payload, id='other'), 'script-one', 'script')
    with pytest.raises(ValueError, match='列表'):
        build(dict(payload, data_sources={}), 'script-one', 'script')
    with pytest.raises(ValueError, match='标准提取Key'):
        build({'name': 'old'}, 'script-one', 'script')
    assert descriptor.AssetDescriptor.from_dict({'id': 'old', 'kind': 'script', 'name': 'old'}).data_sources == ()


# LLM: 对标准ID与skill_id例外分别断言，不只比较两个同时改动的调用方；不触碰审批或资源策略。
# 函数用途: 回归唯一schema构造器的严格键、动作、延迟提示和ID规则。
def test_formal_tool_spec_preserves_schema_and_deferred_hints():
    import importlib.util

    name = 'agent_py_agent.agent.tooling.tool_spec'
    assert importlib.util.find_spec(name) is not None, '共享schema构造器尚不存在'
    build = __import__(name, fromlist=['_spec'])._spec
    properties = {key: {'type': 'string'} for key in ('script_id', 'skill_id', 'scope_key', 'entrypoint')}
    spec = build('sample', '简述；其余说明', ['get'], properties)
    schema = spec.input_schema
    assert schema['required'] == ['action'] and schema['additionalProperties'] is False
    assert schema['properties']['action']['enum'] == ['get']
    for key in ('script_id', 'scope_key', 'entrypoint'):
        assert schema['properties'][key]['pattern'] == r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
    assert 'pattern' not in schema['properties']['skill_id']
    assert all('pattern' not in value for value in properties.values())
    assert spec.hints.default_deferred and spec.hints.category == 'capability'
    assert spec.hints.deferred_summary == '简述'


# LLM: 领域入口须接收明确的实际LocalStore；测试替身只截获底层写入，不改生产所有者。
# 函数用途: 验证重建接口存在，空集合返回准确计数且不创建事实记录。
def test_formal_subagent_index_rebuild_is_domain_owned():
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services.indexing.service import SubAgentIndexingService

    manager = SimpleNamespace(list_runs=lambda: [], local_store=MagicMock())
    service = SubAgentIndexingService(manager)
    rebuild = getattr(service, 'rebuild_subagent_index', None)
    assert callable(rebuild), '完整重建操作尚未归入领域服务'
    store = MagicMock()
    assert rebuild(local_store=store) == 0
    store.log_record.assert_not_called()
    manager.local_store.log_record.assert_not_called()


# LLM: 真实SubAgentTask和索引服务配两套独立底层写入替身，避免把manager库误当CLI事实库仍然通过。
# 函数用途: 准备四个原始事实文件及分离的写入目标，供计数与失败传播回归复用。
@pytest.fixture
def domain_index_rebuild_case(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.indexing.service import SubAgentIndexingService

    task = SubAgentTask(id='fact-run', goal='test indexing', thought='', plan=[])
    task.task_dir = str(tmp_path)
    manager = SimpleNamespace(local_store=MagicMock(), list_runs=lambda: [task])
    original = {}
    for relative in ('WORK_LOG.md', 'execution_context.json', 'reports/runner_result.json', 'reports/patch_review.json'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"canonical":"unchanged"}\n')
        original[path] = path.read_bytes()
    return SubAgentIndexingService(manager), manager, MagicMock(), original


# LLM: 沿真实index_task、TTL和文件读取；只用底层写入替身观察实际目标、四类sourceID及事件，不改所有者接线。
# 函数用途: 验证CLI显式事实库与manager任务库各写原目标，计数准确且canonical字节不变。
def test_domain_index_rebuild_keeps_fact_store_and_source_ids(domain_index_rebuild_case):
    service, manager, facts, original = domain_index_rebuild_case
    assert service.rebuild_subagent_index(local_store=facts) == 5
    manager.local_store.log_record.assert_called_once()
    assert manager.local_store.log_record.call_args.kwargs['source_type'] == 'subagent_run'
    rows = [call.kwargs for call in facts.log_record.call_args_list]
    assert [(row['source_type'], row['source_id'], row['event_type']) for row in rows] == [
        ('subagent_work_log', 'fact-run:WORK_LOG.md', 'subagent_work_log_rebuilt'),
        ('subagent_execution_context', 'fact-run', 'subagent_execution_context_rebuilt'),
        ('subagent_runner_result', 'fact-run:reports/runner_result.json', 'subagent_runner_result_rebuilt'),
        ('subagent_patch_review', 'fact-run:reports/patch_review.json', 'subagent_patch_review_rebuilt'),
    ]
    assert all(row['metadata']['rebuilt'] is True and row['metadata']['run_id'] == 'fact-run' for row in rows)
    assert all(path.read_bytes() == data for path, data in original.items())


# LLM: 原任务索引失败只记warning，原事实文件写入失败必须上抛；禁止迁移时把两条异常路径统一吞掉。
# 函数用途: 验证重建仍保持任务best-effort与事实库失败可见，并不改canonical文件。
def test_domain_index_rebuild_preserves_different_write_error_contracts(domain_index_rebuild_case):
    service, manager, facts, original = domain_index_rebuild_case
    manager.local_store.log_record.side_effect = OSError('task index unavailable')
    assert service.rebuild_subagent_index(local_store=facts) == 5
    facts.log_record.reset_mock()
    facts.log_record.side_effect = OSError('fact store unavailable')
    with pytest.raises(OSError, match='fact store unavailable'):
        service.rebuild_subagent_index(local_store=facts)
    facts.log_record.assert_called_once()
    assert all(path.read_bytes() == data for path, data in original.items())
