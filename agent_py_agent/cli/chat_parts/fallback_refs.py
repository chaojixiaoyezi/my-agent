# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

import threading
from dataclasses import dataclass


# LLM: WorkerStateRefs 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass
class WorkerStateRefs:
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list


# LLM: FallbackInputRefs 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass
class FallbackInputRefs:
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    assistant_outputs: list[str]


# LLM: FallbackEnqueueParams 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass
class FallbackEnqueueParams:
    user: str
    jobs: object
    state_lock: threading.Lock
    pending_jobs_ref: list
    runtime_inject_list: list[str]
    prompt_files: list[str]
