"""受控查询工具。

提供时间窗口和结果数量限制的日志查询功能，防止子代理无限制查询。
第一版实现 file_tail 模板，后续可扩展 parquet_scan、sql_query 等。
"""
from __future__ import annotations

from typing import Any

from .bounded_query_execution import execute_file_tail, validate_time_window
from .bounded_query_models import BoundedQueryConfig, BoundedQueryError
from .models import QueryResult

# 全局默认配置
default_config = BoundedQueryConfig()


def bounded_query(query_template: str, *args: Any, **params: Any) -> QueryResult:
    """执行受控查询。

    兼容旧调用方式：支持 file_path/start_time/end_time/max_results/config 关键字，
    也保留 query_template 后少量位置参数，方便旧测试和调用方继续工作。
    """
    file_path, start_time, end_time, max_results, config = _query_inputs(args, params)
    max_results = _effective_max_results(max_results, config)
    time_window = _time_window(start_time, end_time)

    if config.enforce_time_window:
        time_window_valid, time_error = validate_time_window(time_window)
        if not time_window_valid:
            return _empty_result(query_template, params, time_window, max_results, error=time_error.to_dict() if time_error else {})

    if query_template == "file_tail":
        if not file_path:
            return _empty_result(query_template, params, time_window, max_results, error=_missing_file_error())
        return execute_file_tail(str(file_path), time_window, max_results, config)

    return _empty_result(query_template, params, time_window, max_results, error=_unsupported_template_error(query_template))


def _query_inputs(args: tuple[Any, ...], params: dict[str, Any]) -> tuple[str | None, str | None, str | None, int | None, BoundedQueryConfig]:
    names = ("file_path", "start_time", "end_time", "max_results", "config")
    for name, value in zip(names, args, strict=False):
        params.setdefault(name, value)
    config = params.pop("config", None) or default_config
    return (
        params.pop("file_path", None),
        params.pop("start_time", None),
        params.pop("end_time", None),
        params.pop("max_results", None),
        config,
    )


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


def _empty_result(query_template: str, params: dict[str, Any], time_window: dict[str, str], max_results: int, *, error: dict[str, Any]) -> QueryResult:
    return QueryResult(
        query_template=query_template,
        query_params=params,
        time_window=time_window,
        results=[],
        result_count=0,
        truncated=False,
        max_limit=max_results,
        metadata={"error": error},
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
