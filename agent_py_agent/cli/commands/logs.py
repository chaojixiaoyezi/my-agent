from __future__ import annotations

"""LLM: log-analysis CLI subcommand registration."""

import argparse

from ..logs import (
    cmd_logs,
    cmd_logs_hunt_ip,
    cmd_logs_ingest,
    cmd_logs_query,
    cmd_logs_status,
    cmd_logs_trace_case,
)


def add_logs_subcommands(sub: argparse._SubParsersAction) -> None:
    logs = sub.add_parser("logs", help="Log analysis status, ingest and query commands")
    logs_sub = logs.add_subparsers(dest="logs_command")
    logs.set_defaults(func=cmd_logs)

    logs_status = logs_sub.add_parser("status", help="Show log analysis module status without starting workers")
    logs_status.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_status.set_defaults(func=cmd_logs_status)

    logs_ingest = logs_sub.add_parser("ingest", help="Ingest a local security log file")
    logs_ingest.add_argument("file", help="File to ingest")
    logs_ingest.add_argument("--root", help="Override log-analysis data directory")
    logs_ingest.add_argument("--source-id", help="Source identifier for checkpoints and manifests")
    logs_ingest.add_argument("--format", choices=["jsonl", "json", "csv", "log"], help="Input file format")
    logs_ingest.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_ingest.set_defaults(func=cmd_logs_ingest)

    logs_query = logs_sub.add_parser("query", help="Query ingested security events")
    _add_log_query_filters(logs_query)
    logs_query.set_defaults(func=cmd_logs_query)

    logs_hunt_ip = logs_sub.add_parser("hunt-ip", help="Run attacker/victim IP hunt queries")
    logs_hunt_ip.add_argument("ip", help="IP address to hunt")
    logs_hunt_ip.add_argument("--root", help="Override log-analysis data directory")
    logs_hunt_ip.add_argument("--role", choices=["any", "attacker", "victim"], default="any", help="IP role to query")
    logs_hunt_ip.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_hunt_ip.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_hunt_ip.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_hunt_ip.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_hunt_ip.set_defaults(func=cmd_logs_hunt_ip)

    logs_trace_case = logs_sub.add_parser("trace-case", help="Trace a case through stored query seeds")
    logs_trace_case.add_argument("case_id", help="Case identifier")
    logs_trace_case.add_argument("--root", help="Override log-analysis data directory")
    logs_trace_case.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_trace_case.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_trace_case.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_trace_case.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_trace_case.set_defaults(func=cmd_logs_trace_case)


def _add_log_query_filters(logs_query: argparse.ArgumentParser) -> None:
    logs_query.add_argument("--root", help="Override log-analysis data directory")
    logs_query.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_query.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_query.add_argument("--attacker-ip", help="Filter by attacker/source IP")
    logs_query.add_argument("--victim-ip", help="Filter by victim/destination IP")
    logs_query.add_argument("--domain", help="Filter by domain/host/SNI/DNS query")
    logs_query.add_argument("--uri", help="Filter by URI/URL/path/API")
    logs_query.add_argument("--alert-type", help="Filter by alert type")
    logs_query.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_query.add_argument("--json", action="store_true", help="Output machine-readable JSON")
