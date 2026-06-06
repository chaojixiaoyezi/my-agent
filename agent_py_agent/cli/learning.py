
from __future__ import annotations

"""CLI commands for self-learning draft candidates.

给人看的解释：
这里提供 `my-agent learn ...` 命令，方便查看、确认和拒绝 lesson 草稿。
它只操作候选草稿，不会自动改正式 skill。
"""

import json
import sys

from .common import make_agent


def _ensure_learning_enabled(args) -> tuple[int, object | None]:
    agent = make_agent(args)
    if agent.config.enable_self_learning:
        return 0, agent
    print("enable_self_learning=false，当前配置未启用 learning draft。", file=sys.stderr)
    return 2, None


def cmd_learn_list(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidates = agent.subagents.learning.list_learning_candidates()
    print("LEARNING DRAFTS")
    print(f"total={len(candidates)} dir={agent.subagents.learning.learning_drafts_dir()}")
    if not candidates:
        print("暂时没有 learning draft。")
        return 0
    for item in candidates:
        print(
            f"- {item.id} status={item.status} confidence={item.confidence:.2f} "
            f"occurrences={item.occurrence_count} evidence={item.evidence_count} :: {item.lesson}"
        )
    return 0


def cmd_learn_accept(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidate = agent.subagents.learning.set_learning_candidate_status(args.candidate_id, "accepted")
    print(
        f"accepted {candidate.id} confidence={candidate.confidence:.2f} "
        f"occurrences={candidate.occurrence_count}"
    )
    return 0


def cmd_learn_reject(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    candidate = agent.subagents.learning.set_learning_candidate_status(args.candidate_id, "rejected")
    print(
        f"rejected {candidate.id} confidence={candidate.confidence:.2f} "
        f"occurrences={candidate.occurrence_count}"
    )
    return 0


def cmd_learn_stats(args) -> int:
    code, agent = _ensure_learning_enabled(args)
    if code:
        return code
    payload = agent.subagents.learning.learning_stats()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("LEARNING STATS")
    print("summary=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0
