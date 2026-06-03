
from __future__ import annotations

"""command-line contract and process entrypoint for Live Lab.

这个文件只管“用户能传哪些参数”和“程序怎么退出”。
具体测试怎么跑，交给 runner；具体每个场景做什么，交给 cases。
"""

import argparse
import subprocess
import sys

from .constants import DEFAULT_CONFIG, DEFAULT_RUNS_DIR, SUITES
from .runner import LiveLab


def build_parser() -> argparse.ArgumentParser:
    """defines the live lab command line contract.

    这里列出测试台支持哪些参数。
    常用的是 `--suite smoke` 和 `--suite real --real-llm`。"""

    parser = argparse.ArgumentParser(
        prog="live_agent_lab.py",
        description="Visible isolated harness for my-agent runtime tests.",
    )
    parser.add_argument(
        "--suite",
        choices=sorted(SUITES),
        default="smoke",
        help="测试套件：smoke 不调用真实模型；real/all 需要配合 --real-llm。",
    )
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="允许调用当前配置里的真实模型后端；不传时会临时覆盖成 echo。",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="源配置文件，默认使用项目 agent_config.yaml。")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="Live Lab 输出根目录。")
    parser.add_argument("--run-id", help="指定本轮输出目录名；默认用时间戳。")
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="单次模型请求/场景步骤的基准秒数；多步 gateway ask 会自动扩展总等待预算。",
    )
    parser.add_argument("--count", type=int, default=1, help="真实长链路场景创建多少个子代理。")
    parser.add_argument("--max-runners", type=int, default=1, help="真实长链路每轮最多推进多少 runner。")
    parser.add_argument("--max-cycles", type=int, default=2, help="真实长链路最多执行多少轮 dispatch。")
    parser.add_argument("--keep-going", action="store_true", help="某个 case 失败后继续跑后续 case。")
    return parser


def validate_args(args: argparse.Namespace) -> int:
    """validates numeric guardrails before workspace creation.

    这些数字如果是 0 或负数，后面测试含义就不清楚。
    所以在真正开跑前先拦住，并给出明确错误。"""

    if args.count <= 0:
        print("--count 必须大于 0。", file=sys.stderr)
        return 2
    if args.max_runners <= 0:
        print("--max-runners 必须大于 0。", file=sys.stderr)
        return 2
    if args.max_cycles <= 0:
        print("--max-cycles 必须大于 0。", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """process entrypoint.

    解析参数，启动 Live Lab，最后用退出码告诉外部“通过还是失败”。"""

    args = build_parser().parse_args(argv)
    code = validate_args(args)
    if code:
        return code
    lab = LiveLab(args)
    try:
        lab.setup()
        return lab.run_suite()
    except KeyboardInterrupt:
        print("\nLive Lab 被手动中断。", file=sys.stderr)
        return 130
    except subprocess.TimeoutExpired as exc:
        if lab.created:
            lab.results.append({"case": "unknown", "status": "fail", "error": f"timeout: {exc}"})
            return lab.write_summary()
        print(f"命令超时: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if lab.created:
            lab.results.append({"case": "setup", "status": "fail", "error": str(exc)})
            return lab.write_summary()
        print(f"Live Lab 启动失败: {exc}", file=sys.stderr)
        return 2
