#!/usr/bin/env python3
# LLM: Real run review CLI converts live run folders into structured review snapshots and regression queues.
# 模块用途: 扫描真实运行目录，输出 JSON/JSONL/Markdown 复盘报告；只读本地文件，不启动模型和工具。

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# LLM: _ensure_repo_root_on_path keeps direct script execution importable from any cwd.
# 函数用途: 将仓库根目录加入 sys.path，保证脚本可以导入 agent_py_agent 包。
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


# LLM: main is the stable script entrypoint for live-run postmortem gates.
# 函数用途: 解析参数、生成复盘报告、按是否存在失败 run 返回退出码。
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


# LLM: _build_parser defines the review CLI contract for humans and CI.
# 函数用途: 声明 runs-root、glob、out-dir 和 JSON 输出开关。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review structured real-run reports.")
    parser.add_argument("--runs-root", type=Path, required=True, help="真实运行根目录")
    parser.add_argument("--glob", default="*", help="run 目录匹配表达式")
    parser.add_argument("--out-dir", type=Path, required=True, help="复盘报告输出目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    return parser


# LLM: _write_outputs materializes the review snapshot for later audits.
# 函数用途: 写入 JSON 总报告、逐行 JSONL 和 Markdown 报告，便于追溯和人工阅读。
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


# LLM: _write_records_jsonl writes one record per line for grepable postmortem workflows.
# 函数用途: 从总 payload 里取 records 字段写 JSONL，方便后续抽样、diff 和回放队列处理。
def _write_records_jsonl(payload: dict[str, object], path: Path) -> None:
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: _print_text provides a compact terminal summary without duplicating report logic.
# 函数用途: 非 JSON 模式下输出摘要和报告路径，详细信息仍看落盘文件。
def _print_text(payload: dict[str, object]) -> None:
    summary = payload.get("summary", {})
    outputs = payload.get("outputs", {})
    print(f"REAL_RUN_REVIEW summary={summary}")
    print(f"REAL_RUN_REVIEW outputs={outputs}")


if __name__ == "__main__":
    raise SystemExit(main())
