
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


@dataclass(frozen=True)
class _EmptyResultInput:
    query_template: str
    params: dict[str, Any]
    time_window: dict[str, str]
    max_results: int
    error: dict[str, Any]


@dataclass(frozen=True)
class BoundedQueryParams:
    """Params bundle for bounded log queries."""

    file_path: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    max_results: int | None = None
    config: BoundedQueryConfig | None = None


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

    支持显式关键字参数；内部统一转换为 BoundedQueryParams。"""
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


def _result_params(params: BoundedQueryParams) -> dict[str, Any]:
    return {
        "file_path": params.file_path,
        "start_time": params.start_time,
        "end_time": params.end_time,
        "max_results": params.max_results,
    }


def _effective_max_results(max_results: int | None, config: BoundedQueryConfig) -> int:
    if max_results is None or max_results <= 0:
        return config.default_max_results
    return max_results


def _time_window(start_time: str | None, end_time: str | None) -> dict[str, str]:
    window: dict[str, str] = {}
    if start_time:
        window["start"] = start_time
    if end_time:
        window["end"] = end_time
    return window


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


def _missing_file_error() -> dict[str, Any]:
    return {"error_type": "missing_parameter", "message": "file_tail 模板需要 file_path 参数", "details": {}}


def _unsupported_template_error(query_template: str) -> dict[str, Any]:
    return {
        "error_type": "unsupported_template",
        "message": f"不支持的查询模板: {query_template}",
        "details": {"supported_templates": ["file_tail"]},
    }


__all__ = ["BoundedQueryConfig", "BoundedQueryError", "bounded_query", "default_config"]
