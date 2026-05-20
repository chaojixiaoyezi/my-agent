#!/usr/bin/env python3
# LLM: Replay contract gate keeps golden traces executable outside pytest and catches drift before real-model reruns.
# 模块用途: 扫描 replay specs，逐个回放 golden trace，输出汇总结果并在失败时返回非零退出码。

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


# LLM: _ensure_repo_root_on_path keeps standalone script execution able to import in-repo helper modules.
# 函数用途: 运行脚本时把仓库根目录加入 sys.path，避免直接执行时找不到 tests/support 里的 replay runner。
def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


_REPO_ROOT = _ensure_repo_root_on_path()

from agent_py_agent.tests.support.replay_case_runner import run_replay_case


# LLM: main runs the replay-case gate as a small standalone verification command.
# 函数用途: 解析参数、运行所有 replay specs，并打印 JSON 或文本汇总。
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


# LLM: _build_parser defines the script CLI shape as a stable contract for local and CI usage.
# 函数用途: 声明 replay gate 的命令行参数，保持 repo-root 和 JSON 输出开关稳定。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run replay golden-trace contract checks.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="仓库根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    return parser


# LLM: _result_payload keeps script output stable for tests and future CI parsing.
# 函数用途: 将 replay case 结果整理成 JSON 友好的结构，避免测试依赖 dataclass 字段布局。
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


# LLM: _print_text provides a compact human-readable fallback when JSON output is not requested.
# 函数用途: 输出简单文本汇总，方便本地手工跑脚本时快速看哪些 replay case 没过。
def _print_text(payload: dict[str, object]) -> None:
    print(f"REPLAY_CONTRACTS ok={payload['ok']} count={payload['count']}")
    for item in payload["results"]:
        print(f"- {item['name']}: ok={item['ok']} trace={item['trace']}")
        if item["errors"]:
            print(f"  errors={item['errors']}")


if __name__ == "__main__":
    raise SystemExit(main())
