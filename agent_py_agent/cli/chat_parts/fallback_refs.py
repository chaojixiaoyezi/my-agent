
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class WorkerStateRefs:
    state_lock: threading.Lock
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list


@dataclass
class FallbackInputRefs:
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    assistant_outputs: list[str]


@dataclass
class FallbackEnqueueParams:
    user: str
    jobs: object
    state_lock: threading.Lock
    pending_jobs_ref: list
    runtime_inject_list: list[str]
    prompt_files: list[str]
