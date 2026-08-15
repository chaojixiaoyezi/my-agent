#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


_REPO_ROOT = _ensure_repo_root_on_path()

from agent_py_agent.tests.support.replay_case_runner import run_replay_case


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = args.repo_root.resolve() if args.repo_root else _REPO_ROOT
    specs_dir = repo_root / "agent_py_agent" / "tests" / "replay" / "specs"
    specs = sorted(specs_dir.glob("*.json"))
    results = []
    with tempfile.TemporaryDirectory(prefix="replay-contracts-") as temp_dir:
        temp_root = Path(temp_dir)
        for spec in specs:
            results.append(_result_payload(run_replay_case(spec, temp_root / spec.stem)))
    payload = {"ok": all(item["ok"] for item in results), "count": len(results), "results": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(payload)
    return 0 if payload["ok"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run replay golden-trace contract checks.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="仓库根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    return parser


def _result_payload(result) -> dict[str, object]:
    return {
        "name": result.name,
        "ok": result.ok,
        "trace": result.trace,
        "errors": list(result.errors),
        "contract_ok": result.replay_result.contract_result.ok,
        "blocked": result.replay_result.blocked,
        "replay_error_codes": list(result.replay_result.replay_error_codes),
        "contract_error_codes": list(result.replay_result.contract_result.error_codes),
    }


def _print_text(payload: dict[str, object]) -> None:
    print(f"REPLAY_CONTRACTS ok={payload['ok']} count={payload['count']}")
    for item in payload["results"]:
        print(f"- {item['name']}: ok={item['ok']} trace={item['trace']}")
        if item["errors"]:
            print(f"  errors={item['errors']}")


if __name__ == "__main__":
    raise SystemExit(main())
