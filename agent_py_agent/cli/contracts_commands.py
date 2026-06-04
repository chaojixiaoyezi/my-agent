
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..agent.contracts.contract_status import ContractStatusScanRequest, summarize_contract_status


def cmd_contracts(args: argparse.Namespace) -> int:
    return _cmd_contracts_status(args)


def add_contracts_subcommand(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("contracts", help="查看结构化合同 finding 状态")
    nested = parser.add_subparsers(dest="contracts_action")

    status = nested.add_parser("status", help="汇总最近合同 finding 状态")
    status.add_argument("--root", default=".", help="要扫描的报告目录或 JSON 文件")
    status.add_argument("--limit", type=int, default=20, help="recent_findings 最大条数")
    status.add_argument("--max-files", type=int, default=1000, help="最多扫描多少个 JSON 文件")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_contracts)

    parser.set_defaults(func=cmd_contracts, contracts_action="status", root=".", limit=20, max_files=1000, json=False)


def _cmd_contracts_status(args: argparse.Namespace) -> int:
    report = summarize_contract_status(
        Path(str(getattr(args, "root", ".") or ".")),
        ContractStatusScanRequest(
            limit=int(getattr(args, "limit", 20) or 20),
            max_files=int(getattr(args, "max_files", 1000) or 1000),
        ),
    )
    payload = report.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"contracts status: findings={report.finding_count} files={report.files_with_findings}")
        for code, count in report.by_code.items():
            print(f"- {code}: {count}")
    return 0

__all__ = ["add_contracts_subcommand", "cmd_contracts"]
