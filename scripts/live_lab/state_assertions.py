# LLM: Live Lab state assertions keep case files focused on scenario flow.
# 模块用途: 读取 gateway response 和隔离 workspace 的小型 task.json，验证子代理控制面没有假绿。

from __future__ import annotations

import json
from types import SimpleNamespace


# LLM: assert_no_subagent_state_blockers makes Live Lab trust control-plane state, not just visible files.
# 函数用途: 检查 gateway 最终回复；如果主代理明确说子代理链路仍阻塞，就让自然语言 E2E 失败。
def assert_no_subagent_state_blockers(stdout: str) -> None:
    text = _gateway_response_text(stdout)
    lower = text.lower()
    markers = [
        "subagent state notice",
        "blocking_run_ids",
        "done_verified: 0",
        "尚未完整通过",
        "status=blocked",
    ]
    if any(marker in lower for marker in markers):
        raise RuntimeError("子代理链路仍阻塞，不能把自然语言 E2E 记为通过。")


# LLM: assert_persisted_subagent_state_clean compares final reports with task.json facts.
# 函数用途: 读取隔离项目里的子代理持久化状态；只要还有未解决 run，就让真实 E2E 失败。
def assert_persisted_subagent_state_clean(fixture_root) -> None:
    from agent_py_agent.agent.agent_core.subagent_dispatch_closeout_resolution import (
        blocking_task_ids,
        done_verified_count,
    )

    tasks = _persisted_subagent_tasks(fixture_root)
    if not tasks:
        raise RuntimeError("自然语言 E2E 没有创建任何子代理。")
    blockers = blocking_task_ids(tasks)
    if blockers:
        raise RuntimeError(
            "持久化子代理状态仍未完成: "
            f"done_verified={done_verified_count(tasks)}/{len(tasks)} blockers={', '.join(blockers)}"
        )


# LLM: _persisted_subagent_tasks loads only small task.json files from the isolated Live Lab workspace.
# 函数用途: 给 Live Lab 状态门提供最小 task 对象，不读取产物正文和 runner 长日志。
def _persisted_subagent_tasks(fixture_root) -> list[SimpleNamespace]:
    root = fixture_root / ".my_agent" / "subagents"
    tasks: list[SimpleNamespace] = []
    for path in sorted(root.glob("subagent-*/task.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            tasks.append(SimpleNamespace(**payload))
    return tasks


# LLM: _gateway_response_text reads the response field when gateway emits JSON, with raw stdout fallback.
# 函数用途: 从 gateway ask 的 JSON 输出里取模型最终文本；坏 JSON 时保守按原文本检查。
def _gateway_response_text(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return str(stdout or "")
    if not isinstance(payload, dict):
        return str(stdout or "")
    if payload.get("ok") is False:
        return f"gateway_not_ok {payload.get('error') or ''} {payload.get('response') or ''}"
    return str(payload.get("response") or stdout or "")
