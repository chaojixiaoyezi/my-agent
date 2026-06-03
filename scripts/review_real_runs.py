#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


_REPO_ROOT = _ensure_repo_root_on_path()

from agent_py_agent.agent.contracts.real_run_review import (
    render_real_run_review_markdown,
    review_real_run_tree,
)


def main() -> int:
    args = _build_parser().parse_args()
    review = review_real_run_tree(args.runs_root, run_glob=args.glob)
    outputs = _write_outputs(review.to_dict(), render_real_run_review_markdown(review), args.out_dir)
    payload = dict(review.to_dict())
    payload["outputs"] = {key: str(path) for key, path in outputs.items()}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(payload)
    return 1 if review.summary["failed"] else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review structured real-run reports.")
    parser.add_argument("--runs-root", type=Path, required=True, help="真实运行根目录")
    parser.add_argument("--glob", default="*", help="run 目录匹配表达式")
    parser.add_argument("--out-dir", type=Path, required=True, help="复盘报告输出目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    return parser


def _write_outputs(payload: dict[str, object], markdown: str, out_dir: Path) -> dict[str, Path]:
    root = out_dir.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "real-run-review.json"
    jsonl_path = root / "real-run-review.jsonl"
    markdown_path = root / "real-run-review.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    _write_records_jsonl(payload, jsonl_path)
    markdown_path.write_text(markdown, encoding="utf-8")
    return {"json": json_path, "jsonl": jsonl_path, "markdown": markdown_path}


def _write_records_jsonl(payload: dict[str, object], path: Path) -> None:
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _print_text(payload: dict[str, object]) -> None:
    summary = payload.get("summary", {})
    outputs = payload.get("outputs", {})
    print(f"REAL_RUN_REVIEW summary={summary}")
    print(f"REAL_RUN_REVIEW outputs={outputs}")


if __name__ == "__main__":
    raise SystemExit(main())
