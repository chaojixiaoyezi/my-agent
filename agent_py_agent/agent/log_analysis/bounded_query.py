# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""受控查询工具。

提供时间窗口和结果数量限制的日志查询功能，防止子代理无限制查询。
第一版实现 file_tail 模板，后续可扩展 parquet_scan、sql_query 等。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .bounded_query_execution import execute_file_tail, validate_time_window
from .bounded_query_models import BoundedQueryConfig, BoundedQueryError
from .models import QueryResult

# 全局默认配置
default_config = BoundedQueryConfig()


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _EmptyResultInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _EmptyResultInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _EmptyResultInput:
    query_template: str
    params: dict[str, Any]
    time_window: dict[str, str]
    max_results: int
    error: dict[str, Any]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 BoundedQueryParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 BoundedQueryParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class BoundedQueryParams:
    """Params bundle for bounded log queries."""

    file_path: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    max_results: int | None = None
    config: BoundedQueryConfig | None = None


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bounded_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bounded query 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bounded_query(
    query_template: str,
    *,
    params: BoundedQueryParams | None = None,
    file_path: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int | None = None,
    config: BoundedQueryConfig | None = None,
) -> QueryResult:
    """执行受控查询。

    支持显式关键字兼容；内部统一转换为 BoundedQueryParams。"""
    query_params = _query_inputs(
        params=params,
        file_path=file_path,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
        config=config,
    )
    effective_config = query_params.config or default_config
    max_results = _effective_max_results(query_params.max_results, effective_config)
    time_window = _time_window(query_params.start_time, query_params.end_time)

    if effective_config.enforce_time_window:
        time_window_valid, time_error = validate_time_window(time_window)
        if not time_window_valid:
            return _empty_result(_EmptyResultInput(query_template, _result_params(query_params), time_window, max_results, time_error.to_dict() if time_error else {}))

    if query_template == "file_tail":
        if not query_params.file_path:
            return _empty_result(_EmptyResultInput(query_template, _result_params(query_params), time_window, max_results, _missing_file_error()))
        return execute_file_tail(str(query_params.file_path), time_window, max_results, effective_config)

    return _empty_result(_EmptyResultInput(query_template, _result_params(query_params), time_window, max_results, _unsupported_template_error(query_template)))


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _query_inputs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query inputs 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_inputs(
    *,
    params: BoundedQueryParams | None,
    file_path: str | None,
    start_time: str | None,
    end_time: str | None,
    max_results: int | None,
    config: BoundedQueryConfig | None,
) -> BoundedQueryParams:
    if params is not None:
        return params
    return BoundedQueryParams(
        file_path=file_path,
        start_time=start_time,
        end_time=end_time,
        max_results=max_results,
        config=config,
    )


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _result_params 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 result params 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _result_params(params: BoundedQueryParams) -> dict[str, Any]:
    return {
        "file_path": params.file_path,
        "start_time": params.start_time,
        "end_time": params.end_time,
        "max_results": params.max_results,
    }


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _effective_max_results 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 effective max results 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _effective_max_results(max_results: int | None, config: BoundedQueryConfig) -> int:
    if max_results is None or max_results <= 0:
        return config.default_max_results
    return max_results


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _time_window 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 time window 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _time_window(start_time: str | None, end_time: str | None) -> dict[str, str]:
    window: dict[str, str] = {}
    if start_time:
        window["start"] = start_time
    if end_time:
        window["end"] = end_time
    return window


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _empty_result 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 empty result 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _empty_result(data: _EmptyResultInput) -> QueryResult:
    return QueryResult(
        query_template=data.query_template,
        query_params=data.params,
        time_window=data.time_window,
        results=[],
        result_count=0,
        truncated=False,
        max_limit=data.max_results,
        metadata={"error": data.error},
    )


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _missing_file_error 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 missing file error 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _missing_file_error() -> dict[str, Any]:
    return {"error_type": "missing_parameter", "message": "file_tail 模板需要 file_path 参数", "details": {}}


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _unsupported_template_error 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 unsupported template error 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def _unsupported_template_error(query_template: str) -> dict[str, Any]:
    return {
        "error_type": "unsupported_template",
        "message": f"不支持的查询模板: {query_template}",
        "details": {"supported_templates": ["file_tail"]},
    }


__all__ = ["BoundedQueryConfig", "BoundedQueryError", "bounded_query", "default_config"]
