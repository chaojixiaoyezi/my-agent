# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""canonical state payload for one subagent run."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..models import SubAgentTask

CANONICAL_STATE_FILENAME = "canonical_state.json"


# LLM: AgentRunState is the single detailed state payload that projections mirror.
# 类用途: 把子代理完整状态和权威文件路径绑在一起，避免 persistence 各处重新组装不同版本的 payload。
@dataclass(frozen=True)
class AgentRunState:
    run_id: str
    canonical_path: Path | None
    payload: dict[str, Any]

    # LLM: AgentRunState.to_json is the single serializer for canonical state mirrors.
    # 函数用途: 按稳定 UTF-8 JSON 形态输出 payload，确保 canonical_state/task.json/run.json 内容一致。
    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2)


# LLM: build_agent_run_state snapshots one SubAgentTask into the canonical run-state payload.
# 函数用途: 从任务对象生成权威状态文件路径和 payload，供 persistence 一次写入后再镜像到旧 locator。
def build_agent_run_state(task: SubAgentTask) -> AgentRunState:
    path = canonical_state_path_for_task(task)
    return AgentRunState(run_id=task.id, canonical_path=path, payload=asdict(task))


# LLM: canonical_state_path_for_task derives the task-local authoritative state file.
# 函数用途: 根据 agent_run_workspace_dir 生成 canonical_state.json 路径；缺运行目录时返回 None。
def canonical_state_path_for_task(task: SubAgentTask) -> Path | None:
    run_workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not run_workspace:
        return None
    return Path(run_workspace) / CANONICAL_STATE_FILENAME


# LLM: canonical_state_path_from_payload lets legacy task.json act as a locator.
# 函数用途: 从 payload 中读取 canonical_state_ref，缺失时用 agent_run_workspace_dir 兼容推导权威状态路径。
def canonical_state_path_from_payload(payload: dict[str, Any]) -> Path | None:
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        ref = str(attrs.get("canonical_state_ref") or "").strip()
        if ref:
            return Path(ref)
    run_workspace = str(payload.get("agent_run_workspace_dir") or "").strip()
    if run_workspace:
        return Path(run_workspace) / CANONICAL_STATE_FILENAME
    return None


# LLM: write_agent_run_state writes the authoritative task-local state if a workspace exists.
# 函数用途: 创建目录并写 canonical_state.json；没有 agent 工作目录时保持兼容不写。
def write_agent_run_state(state: AgentRunState) -> None:
    if state.canonical_path is None:
        return
    state.canonical_path.parent.mkdir(parents=True, exist_ok=True)
    state.canonical_path.write_text(state.to_json(), encoding="utf-8")
