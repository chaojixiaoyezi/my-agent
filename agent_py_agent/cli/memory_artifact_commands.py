# LLM: CLI command for explicit artifact body reads; keep it separate from archive search/resume commands.
# 模块用途: 提供 `memory-artifact-read`，只读取已登记 tool-output artifact 的正文切片。
from __future__ import annotations

"""CLI entrypoint for explicit externalized artifact reads."""

import json

from ..agent.memory_archive.artifact_reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from ..agent.memory_archive.query import strip_sort_keys
from .common import make_agent


# LLM: cmd_memory_artifact_read is the explicit CLI gate from artifact refs to artifact body text.
# 函数用途: 只读取 tool output index 已登记 artifact；失败时返回非 0 且不打印正文。
def cmd_memory_artifact_read(args) -> int:
    agent = make_agent(args)
    payload = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=agent.root,
            artifact_ref=args.artifact_ref,
            offset=getattr(args, "offset", 0),
            max_chars=_memory_artifact_max_chars(agent, args),
            mode=getattr(args, "mode", "slice"),
            query=getattr(args, "query", ""),
        )
    )
    _print_memory_artifact_read(payload, json_output=args.json)
    return 0 if payload.get("ok") else 2


# LLM: _memory_artifact_max_chars resolves the CLI default from backend config.
# 函数用途: 用户没有显式传 --max-chars 时，使用 agent_config.yaml 的 memory_artifact_default_read_chars。
def _memory_artifact_max_chars(agent, args) -> int:
    value = getattr(args, "max_chars", None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, "memory_artifact_default_read_chars", 4000) or 0)


# LLM: _print_memory_artifact_read keeps metadata visible before printing explicit artifact content.
# 函数用途: 文本模式先展示 artifact 校验信息，再输出本次显式读取的正文切片。
def _print_memory_artifact_read(payload: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARTIFACT READ")
    print(f"artifact_ref={payload.get('artifact_ref') or '-'}")
    if not payload.get("ok"):
        print(f"error_code={payload.get('error_code') or 'unknown'}")
        print(f"message={payload.get('message') or ''}")
        return
    print(f"artifact_path={payload['artifact_path']}")
    print(f"sha256={payload['sha256']}")
    print(
        f"mode={payload.get('read_mode') or 'slice'} "
        f"offset={payload['content_offset']} max_chars={payload['content_max_chars']} truncated={payload['truncated']}"
    )
    print("Content")
    print(payload["content"])
