

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SlashCommandContext:
    agent: Any
    memory_limit: int
    runtime_inject: list[str]
    prompt_files: list[str]
    print_line: Callable[[str], None]
    control_executor: Callable[[Any], Any] | None = None
