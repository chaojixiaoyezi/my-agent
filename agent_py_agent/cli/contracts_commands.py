# LLM: Contract CLI commands expose read-only status and explicit schema migration.
# 模块用途: 提供 contracts status/migrate 命令，方便查看合同失败和升级旧合同文件。

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..agent.contracts.contract_doctor import lint_contract, migrate_contract
from ..agent.contracts.contract_status import summarize_contract_status


# LLM: cmd_contracts dispatches nested contract commands without side effects by default.
# 函数用途: 处理 contracts status/migrate 子命令；未指定时默认输出 status。
def cmd_contracts(args: argparse.Namespace) -> int:
    action = str(getattr(args, "contracts_action", "") or "status")
    if action == "migrate":
        return _cmd_contracts_migrate(args)
    return _cmd_contracts_status(args)


# LLM: add_contracts_subcommand keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def add_contracts_subcommand(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("contracts", help="查看或迁移结构化合同")
    nested = parser.add_subparsers(dest="contracts_action")

    status = nested.add_parser("status", help="汇总最近合同 finding 状态")
    status.add_argument("--root", default=".", help="要扫描的报告目录或 JSON 文件")
    status.add_argument("--limit", type=int, default=20, help="recent_findings 最大条数")
    status.add_argument("--max-files", type=int, default=1000, help="最多扫描多少个 JSON 文件")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_contracts)

    migrate = nested.add_parser("migrate", help="迁移旧版本合同 JSON")
    migrate.add_argument("--input", required=True, help="输入合同 JSON 文件或目录")
    migrate.add_argument("--output", default="", help="输出文件或目录；省略时只 dry-run")
    migrate.add_argument("--in-place", action="store_true", help="原地覆盖迁移后的合同文件")
    migrate.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    migrate.set_defaults(func=cmd_contracts)

    parser.set_defaults(func=cmd_contracts, contracts_action="status", root=".", limit=20, max_files=1000, json=False)


# LLM: _cmd_contracts_status keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _cmd_contracts_status(args: argparse.Namespace) -> int:
    report = summarize_contract_status(
        Path(str(getattr(args, "root", ".") or ".")),
        limit=int(getattr(args, "limit", 20) or 20),
        max_files=int(getattr(args, "max_files", 1000) or 1000),
    )
    payload = report.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"contracts status: findings={report.finding_count} files={report.files_with_findings}")
        for code, count in report.by_code.items():
            print(f"- {code}: {count}")
    return 0


# LLM: _cmd_contracts_migrate keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _cmd_contracts_migrate(args: argparse.Namespace) -> int:
    input_path = Path(str(getattr(args, "input", "") or "")).expanduser()
    output_arg = str(getattr(args, "output", "") or "")
    in_place = bool(getattr(args, "in_place", False))
    results = _migrate_path(input_path, Path(output_arg).expanduser() if output_arg else None, in_place=in_place)
    payload = {"migrated": results, "count": len(results)}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"contracts migrate: {len(results)} file(s)")
        for item in results:
            print(f"- {item['input']} -> {item.get('output') or '<dry-run>'} changed={item['changed']}")
    return 0


# LLM: _migrate_path keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _migrate_path(input_path: Path, output_path: Path | None, *, in_place: bool) -> list[dict[str, object]]:
    if input_path.is_dir():
        return [
            _migrate_file(path, _directory_output(path, input_path, output_path), in_place=in_place)
            for path in sorted(input_path.rglob("*.json"))
            if path.is_file()
        ]
    return [_migrate_file(input_path, output_path, in_place=in_place)]


# LLM: _directory_output keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _directory_output(path: Path, root: Path, output_path: Path | None) -> Path | None:
    if output_path is None:
        return None
    return output_path / path.relative_to(root)


# LLM: _migrate_file keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _migrate_file(input_path: Path, output_path: Path | None, *, in_place: bool) -> dict[str, object]:
    payload = _read_contract(input_path)
    migrated = migrate_contract(payload)
    lint = lint_contract(migrated)
    target = input_path if in_place else output_path
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "input": str(input_path),
        "output": str(target) if target is not None else "",
        "changed": migrated != payload,
        "lint_ok": lint.ok,
        "error_codes": list(lint.error_codes),
    }


# LLM: _read_contract keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _read_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["add_contracts_subcommand", "cmd_contracts"]
