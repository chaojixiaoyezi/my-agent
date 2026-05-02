"""日志分析模块第一轮循环测试。

测试 SecurityCase、LogWorkOrder 模型、work_order 到 subagent 转换、bounded_query 工具和完整链路。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.log_analysis import (
    bounded_query,
    LogWorkOrder,
    SecurityCase,
    work_order_to_subagent_task,
)
from agent_py_agent.agent.log_analysis.bounded_query import (
    BoundedQueryConfig,
    BoundedQueryError,
)
from agent_py_agent.agent.log_analysis.models import QueryResult
from agent_py_agent.agent.subagents.models import SubAgentTask


@pytest.fixture
def sample_cases_data():
    """加载 sample_cases.json fixture。"""
    fixture_path = Path("agent_py_agent/data/log_fixtures/sample_cases.json")
    if not fixture_path.exists():
        pytest.skip(f"fixture 不存在: {fixture_path}")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_security_case(sample_cases_data):
    """获取第一个 SecurityCase 示例。"""
    case_data = sample_cases_data[0]
    return SecurityCase.from_dict(case_data)


@pytest.fixture
def sample_work_order(sample_security_case):
    """创建一个 LogWorkOrder 示例。"""
    return LogWorkOrder(
        work_order_id="wo-001",
        case_id=sample_security_case.case_id,
        investigation_goal="确认 SQL 注入攻击是否成功，查找后续横向移动",
        start_time="2026-04-30T09:25:00Z",
        end_time="2026-04-30T10:00:00Z",
        allowed_query_templates=["file_tail"],
        max_results=50,
        evidence_budget=500,
    )


class TestSecurityCase:
    """测试 SecurityCase 模型。"""

    def test_create_security_case_from_dict(self, sample_cases_data):
        """测试从字典创建 SecurityCase。"""
        case_data = sample_cases_data[0]
        case = SecurityCase.from_dict(case_data)

        assert case.case_id == "case-web-attack-001"
        assert case.severity == "high"
        assert case.event_class == "web_attack"
        assert case.detector_id == "waf_sql_injection_detector"
        assert len(case.trigger_entities["attacker_ip"]) == 1
        assert case.trigger_entities["attacker_ip"][0] == "198.51.100.23"
        assert len(case.initial_evidence) == 1
        assert case.initial_evidence[0].evidence_id == "evidence-waf-001"

    def test_security_case_to_json(self, sample_security_case):
        """测试 SecurityCase 序列化为 JSON。"""
        case_json = sample_security_case.to_json()
        assert isinstance(case_json, str)

        # 验证可以反序列化
        parsed = json.loads(case_json)
        assert parsed["case_id"] == "case-web-attack-001"
        assert parsed["severity"] == "high"

    def test_security_case_to_dict(self, sample_security_case):
        """测试 SecurityCase 转换为字典。"""
        case_dict = sample_security_case.to_dict()
        assert case_dict["case_id"] == "case-web-attack-001"
        assert "trigger_entities" in case_dict
        assert "initial_evidence" in case_dict

    def test_security_case_empty_evidence_refs(self):
        """测试空的 evidence refs。"""
        case = SecurityCase(
            case_id="case-empty-001",
            severity="low",
            event_class="test",
            trigger_entities={},
            initial_evidence=[],
        )
        assert case.case_id == "case-empty-001"
        assert len(case.initial_evidence) == 0


class TestLogWorkOrder:
    """测试 LogWorkOrder 模型。"""

    def test_create_work_order(self, sample_work_order):
        """测试创建 LogWorkOrder。"""
        assert sample_work_order.work_order_id == "wo-001"
        assert sample_work_order.case_id == "case-web-attack-001"
        assert "SQL 注入" in sample_work_order.investigation_goal
        assert sample_work_order.start_time == "2026-04-30T09:25:00Z"
        assert sample_work_order.end_time == "2026-04-30T10:00:00Z"
        assert sample_work_order.max_results == 50
        assert sample_work_order.evidence_budget == 500
        assert sample_work_order.status == "OPEN"

    def test_work_order_to_json(self, sample_work_order):
        """测试 LogWorkOrder 序列化为 JSON。"""
        work_order_json = sample_work_order.to_json()
        assert isinstance(work_order_json, str)

        parsed = json.loads(work_order_json)
        assert parsed["work_order_id"] == "wo-001"
        assert parsed["case_id"] == "case-web-attack-001"

    def test_work_order_from_dict(self):
        """测试从字典创建 LogWorkOrder。"""
        work_order_dict = {
            "work_order_id": "wo-002",
            "case_id": "case-test-001",
            "investigation_goal": "测试目标",
            "start_time": "2026-04-30T00:00:00Z",
            "end_time": "2026-04-30T23:59:59Z",
            "max_results": 100,
            "evidence_budget": 1000,
        }
        work_order = LogWorkOrder.from_dict(work_order_dict)

        assert work_order.work_order_id == "wo-002"
        assert work_order.case_id == "case-test-001"
        assert work_order.investigation_goal == "测试目标"


class TestWorkOrderToSubAgentTask:
    """测试 work_order 到 subagent 的转换。"""

    def test_basic_conversion(self, sample_work_order):
        """测试基本转换。"""
        task = work_order_to_subagent_task(sample_work_order)

        assert isinstance(task, SubAgentTask)
        assert task.id.startswith("log-subagent-wo-001")
        assert "case-web-attack-001" in task.goal
        assert "SQL 注入" in task.goal
        assert task.agent_name == "log_analyst"
        assert task.role == "log_analyst"
        assert task.status == "PLANNING"
        assert task.verification_status == "UNVERIFIED"

    def test_allowed_tools_populated(self, sample_work_order):
        """测试允许的工具列表被正确填充。"""
        task = work_order_to_subagent_task(sample_work_order)

        assert "log_bounded_query" in task.allowed_tools
        assert len(task.allowed_tools) >= 1

    def test_execution_context_generated(self, sample_work_order):
        """测试执行上下文被正确生成。"""
        task = work_order_to_subagent_task(sample_work_order)

        assert task.execution_context_json
        context = json.loads(task.execution_context_json)

        assert context["work_order_id"] == "wo-001"
        assert context["case_id"] == "case-web-attack-001"
        assert context["time_window"]["start"] == "2026-04-30T09:25:00Z"
        assert context["time_window"]["end"] == "2026-04-30T10:00:00Z"
        assert context["query_limits"]["max_results"] == 50
        assert context["query_limits"]["evidence_budget"] == 500
        assert "allowed_tools" in context
        assert "instructions" in context
        assert len(context["instructions"]) > 0

    def test_custom_config(self, sample_work_order):
        """测试自定义配置。"""
        from agent_py_agent.agent.log_analysis.work_order import WorkOrderToTaskConfig

        config = WorkOrderToTaskConfig(
            default_agent_name="senior_analyst",
            default_role="senior_log_analyst",
        )

        task = work_order_to_subagent_task(sample_work_order, config)

        assert task.agent_name == "senior_analyst"
        assert task.role == "senior_log_analyst"

    def test_quality_contract_in_context(self, sample_work_order):
        """测试质量契约被包含在执行上下文中。"""
        task = work_order_to_subagent_task(sample_work_order)
        context = json.loads(task.execution_context_json)

        assert "quality_contract" in context
        qc = context["quality_contract"]
        assert qc["user_visible_goal"]
        assert qc["quality_bar"]
        assert "forbidden_delivery" in qc
        assert "must_check" in qc
        assert "evidence_required" in qc


class TestBoundedQuery:
    """测试受控查询工具。"""

    def test_unsupported_template(self):
        """测试不支持的查询模板。"""
        result = bounded_query(
            query_template="unsupported_template",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-04-30T23:59:59Z",
        )

        assert isinstance(result, QueryResult)
        assert result.query_template == "unsupported_template"
        assert result.result_count == 0
        assert not result.truncated
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "unsupported_template"

    def test_file_tail_missing_file_path(self):
        """测试 file_tail 缺少 file_path 参数。"""
        result = bounded_query(
            query_template="file_tail",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-04-30T23:59:59Z",
        )

        assert isinstance(result, QueryResult)
        assert result.query_template == "file_tail"
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "missing_parameter"

    def test_invalid_time_window(self):
        """测试无效的时间窗口。"""
        result = bounded_query(
            query_template="file_tail",
            file_path="dummy.log",
            start_time="2026-04-30T23:59:59Z",
            end_time="2026-04-30T00:00:00Z",  # 结束时间早于开始时间
        )

        assert isinstance(result, QueryResult)
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "invalid_time_window"

    def test_invalid_time_format(self):
        """测试无效的时间格式。"""
        result = bounded_query(
            query_template="file_tail",
            file_path="dummy.log",
            start_time="invalid-time",
        )

        assert isinstance(result, QueryResult)
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "invalid_time_format"

    def test_time_window_too_large(self):
        """测试时间窗口过大。"""
        result = bounded_query(
            query_template="file_tail",
            file_path="dummy.log",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-12-31T23:59:59Z",  # 365 天
        )

        assert isinstance(result, QueryResult)
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "time_window_too_large"

    def test_file_not_exists(self):
        """测试文件不存在。"""
        result = bounded_query(
            query_template="file_tail",
            file_path="/nonexistent/path/to/file.log",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-04-30T23:59:59Z",
        )

        assert isinstance(result, QueryResult)
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "access_denied"

    def test_file_path_not_allowed(self):
        """测试不允许的文件路径。"""
        config = BoundedQueryConfig(allow_absolute_paths=False)
        result = bounded_query(
            query_template="file_tail",
            file_path="/etc/passwd",
            start_time="2026-04-30T00:00:00Z",
            end_time="2026-04-30T23:59:59Z",
            config=config,
        )

        assert isinstance(result, QueryResult)
        assert result.result_count == 0
        assert "error" in result.metadata
        assert result.metadata["error"]["error_type"] == "access_denied"

    def test_max_results_zero_or_negative(self):
        """测试 max_results 为零或负数时使用默认值。"""
        config = BoundedQueryConfig(default_max_results=50)

        # max_results = 0
        result = bounded_query(
            query_template="file_tail",
            file_path="dummy.log",
            max_results=0,
            config=config,
        )
        assert result.max_limit == 50

        # max_results = -1
        result = bounded_query(
            query_template="file_tail",
            file_path="dummy.log",
            max_results=-1,
            config=config,
        )
        assert result.max_limit == 50

    def test_query_result_to_json(self):
        """测试 QueryResult 序列化为 JSON。"""
        result = QueryResult(
            query_template="file_tail",
            query_params={"file_path": "test.log"},
            time_window={"start": "2026-04-30T00:00:00Z", "end": "2026-04-30T23:59:59Z"},
            results=[{"timestamp": "2026-04-30T12:00:00Z", "message": "test line"}],
            result_count=1,
            truncated=False,
            max_limit=100,
        )

        result_json = result.to_json()
        assert isinstance(result_json, str)

        parsed = json.loads(result_json)
        assert parsed["query_template"] == "file_tail"
        assert parsed["result_count"] == 1
        assert not parsed["truncated"]


class TestBoundedQueryError:
    """测试 BoundedQueryError。"""

    def test_error_to_dict(self):
        """测试 BoundedQueryError 转换为字典。"""
        error = BoundedQueryError(
            error_type="test_error",
            message="Test error message",
            details={"key": "value"},
        )

        error_dict = error.to_dict()
        assert error_dict["error_type"] == "test_error"
        assert error_dict["message"] == "Test error message"
        assert error_dict["details"]["key"] == "value"


class TestBoundedQueryConfig:
    """测试 BoundedQueryConfig。"""

    def test_default_config(self):
        """测试默认配置。"""
        config = BoundedQueryConfig()

        assert config.default_max_results == 100
        assert config.default_max_file_size_mb == 10.0
        assert config.default_max_lines == 10000
        assert config.default_tail_lines == 1000
        assert config.enforce_time_window is True
        assert config.allow_absolute_paths is False
        assert len(config.allowed_base_paths) > 0

    def test_custom_config(self):
        """测试自定义配置。"""
        config = BoundedQueryConfig(
            default_max_results=200,
            default_max_file_size_mb=20.0,
            enforce_time_window=False,
            allow_absolute_paths=True,
        )

        assert config.default_max_results == 200
        assert config.default_max_file_size_mb == 20.0
        assert config.enforce_time_window is False
        assert config.allow_absolute_paths is True


class TestFullChain:
    """测试完整链路：case -> work_order -> subagent_task。"""

    def test_case_to_work_order_to_task(self, sample_security_case):
        """测试从 case 到 work_order 再到 subagent_task 的完整转换。"""
        # 1. 创建 work_order
        work_order = LogWorkOrder(
            work_order_id="wo-chain-001",
            case_id=sample_security_case.case_id,
            investigation_goal="确认攻击链",
            start_time="2026-04-30T09:00:00Z",
            end_time="2026-04-30T10:30:00Z",
            allowed_query_templates=["file_tail"],
            max_results=100,
        )

        # 2. 转换为 subagent_task
        task = work_order_to_subagent_task(work_order)

        # 3. 验证转换正确性
        assert task.id.startswith("log-subagent-wo-chain-001")
        assert "case-web-attack-001" in task.goal
        assert "确认攻击链" in task.goal
        assert "09:00:00Z" in task.goal
        assert "10:30:00Z" in task.goal
        assert "100" in task.goal

        # 4. 验证执行上下文
        context = json.loads(task.execution_context_json)
        assert context["work_order_id"] == "wo-chain-001"
        assert context["case_id"] == "case-web-attack-001"
        assert context["investigation_goal"] == "确认攻击链"
        assert context["time_window"]["start"] == "2026-04-30T09:00:00Z"
        assert context["time_window"]["end"] == "2026-04-30T10:30:00Z"
        assert "log_bounded_query" in context["allowed_tools"]
        assert "quality_contract" in context
        assert "instructions" in context

        # 5. 验证质量契约
        qc = context["quality_contract"]
        assert qc["evidence_required"]
        assert "查询记录" in str(qc["evidence_required"])
        assert "must_check" in qc

        # 6. 验证状态
        assert task.status == "PLANNING"
        assert task.verification_status == "UNVERIFIED"
        assert task.runner_attempts == 0

    def test_multiple_cases_to_tasks(self, sample_cases_data):
        """测试多个 case 转换为多个 subagent_task。"""
        tasks = []

        for case_data in sample_cases_data:
            case = SecurityCase.from_dict(case_data)
            work_order = LogWorkOrder(
                work_order_id=f"wo-{case.case_id}",
                case_id=case.case_id,
                investigation_goal="调查安全事件",
                start_time="2026-04-30T00:00:00Z",
                end_time="2026-04-30T23:59:59Z",
                max_results=100,
            )
            task = work_order_to_subagent_task(work_order)
            tasks.append(task)

        assert len(tasks) == len(sample_cases_data)
        assert all(isinstance(t, SubAgentTask) for t in tasks)

        # 验证每个任务都有独立的 run_id
        run_ids = [t.id for t in tasks]
        assert len(run_ids) == len(set(run_ids))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
