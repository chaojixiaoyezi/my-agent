# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI commands for self-learning draft candidates.

给人看的解释：
这里提供 `my-agent learn ...` 命令，方便查看、确认和拒绝 lesson 草稿。
它只操作候选草稿，不会自动改正式 skill。
"""

import json
import sys

from .common import make_agent


# LLM: _ensure_learning_enabled 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _ensure_learning_enabled(args) -> tuple[int, object | None]:
    agent = make_agent(args)
    if agent.config.enable_self_learning:
        return 0, agent
    print("enable_self_learning=false，当前配置未启用 learning draft。", file=sys.stderr)
    return 2, None


# LLM: cmd_learn_list 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_learn_list(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidates = agent.subagents.list_learning_candidates()
    print("LEARNING DRAFTS")
    print(f"total={len(candidates)} dir={agent.subagents.learning_drafts_dir()}")
    if not candidates:
        print("暂时没有 learning draft。")
        return 0
    for item in candidates:
        print(
            f"- {item.id} status={item.status} confidence={item.confidence:.2f} "
            f"occurrences={item.occurrence_count} evidence={item.evidence_count} :: {item.lesson}"
        )
    return 0


# LLM: cmd_learn_accept 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_learn_accept(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidate = agent.subagents.set_learning_candidate_status(args.candidate_id, "accepted")
    print(
        f"accepted {candidate.id} confidence={candidate.confidence:.2f} "
        f"occurrences={candidate.occurrence_count}"
    )
    print("状态已标记为 accepted；未生成 SKILL.md，也未安装正式 skill。")
    return 0


# LLM: cmd_learn_reject 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_learn_reject(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidate = agent.subagents.set_learning_candidate_status(args.candidate_id, "rejected")
    print(
        f"rejected {candidate.id} confidence={candidate.confidence:.2f} "
        f"occurrences={candidate.occurrence_count}"
    )
    return 0


# LLM: cmd_learn_stats 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_learn_stats(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    payload = agent.subagents.learning_stats()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("LEARNING STATS")
    print("summary=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0
