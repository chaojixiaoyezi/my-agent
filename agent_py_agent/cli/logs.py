
from __future__ import annotations

"""CLI commands for the optional log-analysis module."""

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..agent.log_analysis.config import load_log_analysis_config, resolve_log_analysis_data_dir
from ..agent.log_analysis.doctor import collect_doctor_status
from ..agent.log_analysis.ingest.pipeline import ingest_file
from ..agent.log_analysis.tools.query_functions import (
    SecurityQueryParams,
    hunt_ip,
    security_query,
    trace_case,
)


def cmd_logs(args) -> int:
    print("Usage: my-agent logs {status,ingest,query,hunt-ip,trace-case}")
    return 2


def cmd_logs_status(args) -> int:
    status = collect_doctor_status()
    config = load_log_analysis_config()
    payload = {
        "ok": True,
        **status,
        "data_dir": status["paths"]["base"]["path"],
        "query_default_limit": config.query_default_limit,
        "query_max_limit": config.query_max_limit,
    }
    _print_status(payload, json_output=args.json)
    return 0


def cmd_logs_ingest(args) -> int:
    config = load_log_analysis_config()
    root = _resolve_root(args.root, config.data_dir)
    result = ingest_file(
        args.file,
        root=root,
        source_id=args.source_id,
        file_format=args.format,
        payload_max_chars=config.payload_preview_max_chars,
    )
    payload = {
        "ok": True,
        "command": "logs ingest",
        "root": str(root),
        "result": _jsonable(result),
    }
    _print_ingest(payload, json_output=args.json)
    return 0


def cmd_logs_query(args) -> int:
    config = load_log_analysis_config()
    root = _resolve_root(args.root, config.data_dir)
    limit, warnings = _resolve_query_limit(args.limit, config.query_default_limit, config.query_max_limit)
    response = security_query(
        SecurityQueryParams(
            root=root,
            attacker_ip=args.attacker_ip,
            victim_ip=args.victim_ip,
            domain=args.domain,
            uri=args.uri,
            alert_type=args.alert_type,
            start_time=args.start_time,
            end_time=args.end_time,
            limit=limit,
            max_limit=config.query_max_limit,
        )
    )
    payload = {
        "ok": True,
        "command": "logs query",
        "root": str(root),
        "limit": limit,
        "limit_warnings": warnings,
        "result": response,
    }
    _print_query(payload, json_output=args.json)
    return 0


def cmd_logs_hunt_ip(args) -> int:
    config = load_log_analysis_config()
    root = _resolve_root(args.root, config.data_dir)
    limit, warnings = _resolve_query_limit(args.limit, config.query_default_limit, config.query_max_limit)
    response = hunt_ip(
        args.ip,
        root=root,
        role=args.role,
        start_time=args.start_time,
        end_time=args.end_time,
        limit=limit,
        max_limit=config.query_max_limit,
    )
    payload = {
        "ok": True,
        "command": "logs hunt-ip",
        "root": str(root),
        "limit": limit,
        "limit_warnings": warnings,
        "result": response,
    }
    _print_query(payload, json_output=args.json)
    return 0


def cmd_logs_trace_case(args) -> int:
    config = load_log_analysis_config()
    root = _resolve_root(args.root, config.data_dir)
    limit, warnings = _resolve_query_limit(args.limit, config.query_default_limit, config.query_max_limit)
    response = trace_case(
        args.case_id,
        root=root,
        start_time=args.start_time,
        end_time=args.end_time,
        limit=limit,
        max_limit=config.query_max_limit,
    )
    payload = {
        "ok": True,
        "command": "logs trace-case",
        "root": str(root),
        "limit": limit,
        "limit_warnings": warnings,
        "result": response,
    }
    _print_query(payload, json_output=args.json)
    return 0


def _resolve_root(raw_root: str | None, configured_data_dir: str) -> Path:
    if raw_root:
        return Path(raw_root).expanduser()
    return resolve_log_analysis_data_dir(configured_data_dir)


def _resolve_query_limit(raw_limit: int | None, default_limit: int, max_limit: int) -> tuple[int, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    requested = default_limit if raw_limit is None else raw_limit
    if requested <= 0:
        warnings.append(
            {
                "field_name": "limit",
                "raw_value": requested,
                "effective_value": default_limit,
                "reason": "expected a positive integer; using query_default_limit",
            }
        )
        requested = default_limit
    if requested > max_limit:
        warnings.append(
            {
                "field_name": "limit",
                "raw_value": requested,
                "effective_value": max_limit,
                "reason": "requested limit exceeded query_max_limit and was truncated",
            }
        )
        requested = max_limit
    return requested, warnings


def _print_status(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _print_json(payload)
        return
    print("MY-AGENT LOGS STATUS")
    print(f"state={payload['state']}")
    print(f"capability_level={payload['capability_level']}")
    print(f"data_dir={payload['data_dir']}")
    print(f"worker_enabled={payload['config']['effective']['worker_enabled']}")
    print(f"query_default_limit={payload['query_default_limit']}")
    print(f"query_max_limit={payload['query_max_limit']}")
    warnings = payload["config"]["warnings"]
    print(f"warnings={len(warnings)}")
    for warning in warnings:
        print(f"- {warning.get('field_name', 'warning')}: {warning.get('reason', '')}")


def _print_ingest(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _print_json(payload)
        return
    result = payload["result"]
    print("MY-AGENT LOGS INGEST")
    print(f"status={result['status']} root={payload['root']}")
    print(
        "counts="
        f"parsed:{result['parsed_count']} stored:{result['stored_count']} "
        f"duplicates:{result['duplicate_count']} dead_letter:{result['dead_letter_count']} "
        f"skipped:{result['skipped_count']}"
    )
    print(f"manifest={result['manifest_path']}")
    print(f"checkpoint={result['checkpoint_path']}")


def _print_query(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _print_json(payload)
        return
    result = payload["result"]
    print(f"MY-AGENT {payload['command'].upper()}")
    print(f"root={payload['root']} limit={payload['limit']}")
    for warning in payload["limit_warnings"]:
        print(f"- warning: {warning['reason']} effective={warning['effective_value']}")
    print(f"query_id={result.get('query_id', '-')}")
    print(f"row_count={result['row_count']} truncated={result.get('truncated', False)}")
    print(f"evidence={result.get('evidence_path', '-')}")
    rows = result.get("preview_rows") or []
    print(f"preview_rows={len(rows)}")
    for row in rows:
        print("- " + json.dumps(row, ensure_ascii=False, sort_keys=True))


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
