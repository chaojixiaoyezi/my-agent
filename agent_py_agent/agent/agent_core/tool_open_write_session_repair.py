# LLM: Open write-session repair prevents final answers while staged file chunks are uncommitted.
# 模块用途: 在工具循环收口前检查 file_write_session open 状态，并生成继续 finish/abort 的提示。

from __future__ import annotations

import json
from pathlib import Path

from ..backend import ModelResponse
from ..tooling.file_write_session_inspection import open_file_write_sessions

_MAX_REPAIRS = 2


# LLM: open_write_session_repair_context is based on manifest facts, not model prose.
# 函数用途: 如果存在未 finish 的分块写入 session，则返回下一轮模型必须处理的结构化提示。
def open_write_session_repair_context(agent: object, repairs: int) -> str:
    sessions = open_file_write_sessions(_agent_root(agent))
    if not sessions or repairs >= _MAX_REPAIRS:
        return ""
    payload = {"open_file_write_sessions": sessions}
    return "\n".join(
        [
            "[tool-system open-file-write-session]",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "你还有未提交的 file_write_session。下一轮必须先调用 file_write_session finish "
            "提交目标文件；如果决定放弃该产物，必须调用 abort。处理完之前不要给最终答复。",
        ]
    )


# LLM: open_write_session_block_response gives a deterministic stop after repeated ignored repair prompts.
# 函数用途: 模型多次忽略 open session 合同时，返回明确失败，避免无限空转。
def open_write_session_block_response(agent: object) -> ModelResponse | None:
    sessions = open_file_write_sessions(_agent_root(agent))
    if not sessions:
        return None
    return ModelResponse(
        text="[OPEN_FILE_WRITE_SESSION_BLOCKED] 分块写入会话仍未 finish/abort，已停止最终收口。",
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
    )


# LLM: _agent_root keeps repair helpers tolerant of lightweight test harnesses.
# 函数用途: 从 agent 对象读取工作区根目录。
def _agent_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).resolve()
