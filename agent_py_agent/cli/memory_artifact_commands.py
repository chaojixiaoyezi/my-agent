from __future__ import annotations

"""CLI entrypoint for explicit externalized artifact reads."""

import json

from ..agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from ..agent.memory_archive.query import strip_sort_keys
from .common import make_agent


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


def _memory_artifact_max_chars(agent, args) -> int:
    value = getattr(args, "max_chars", None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, "memory_artifact_default_read_chars", 4000) or 0)


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
