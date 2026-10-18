# LLM: 最早 worker 分支只解析标准库参数；确立 C 环境模式后才导入进程骨架，不走 config/extension/HomePaths。
# 模块用途: 处理内部 gateway worker 入口，Windows 或缺模式事实以结构化错误拒绝。
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# LLM: 普通bootstrap前只解析原C内部argv/环境，同载荷roots完整核对后才导入W运行，不从请求/配置推断角色或补工作根。
# 函数用途: 把 worker 命令分到内部骨架，非 worker 返回空保持原 CLI 行为。
def dispatch_worker_entry(argv):
    prefix = argparse.ArgumentParser(add_help=False)
    prefix.add_argument("--config")
    prefix.add_argument("--plain", action="store_true")
    options, rest = prefix.parse_known_args(argv)
    if rest[:2] != ["gateway", "worker"]:
        return None
    if sys.platform == "win32":
        return _worker_error({"error_code": "WORKER_PLATFORM_UNSUPPORTED"})
    from ..agent.gateway_parts.multiprocess_mode import WorkerLaunch, establish_worker_mode
    from ..agent.gateway_parts.multiprocess_route import WorkerPartition

    args = _worker_parser().parse_args(rest[2:])
    launch = WorkerLaunch(WorkerPartition(args.index, args.of, args.run), args.gateway_root, args.owner_root,
                          args.parent_pid, args.instance, tuple(args.workspace_root))
    result = establish_worker_mode(launch)
    if not result["ok"]:
        return _worker_error(result)
    from ..agent.gateway_parts.multiprocess_worker import run_worker

    return run_worker(launch, _heartbeat_notice, config_path=options.config)


# LLM: 内部roots参数只运输C原解析结果且须env同值，不开放用户配置/worker数量开关，不加载全量扩展。
# 函数用途: 注册内部 worker 进程需要的固定身份和根路径。
def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="my-agent gateway worker", description="内部 worker 基础设施，不认领请求")
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--of", type=int, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--gateway-root", type=Path, required=True)
    parser.add_argument("--owner-root", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--instance", default="")
    parser.add_argument("--workspace-root", type=Path, action="append", default=[])
    return parser


# LLM: 错误只输出机器字段，不物化任何 owner 或恢复请求。
# 函数用途: 输出 worker 的结构化启动拒绝并返回非零退出码。
def _worker_error(result: dict) -> int:
    print(json.dumps({"ok": False, **result}, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
    return 2


# LLM: 标准输出是投影通知，真实权威仍是原子心跳文件；测试可用管道等待写完，不靠 sleep。
# 函数用途: 在心跳写入完成后输出可观察的结构化通知。
def _heartbeat_notice(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
