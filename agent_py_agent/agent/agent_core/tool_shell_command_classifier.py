# LLM: Shell command classification is shared by repair guards to avoid name-only run_command decisions.
# 模块用途: 把复合 shell 命令拆成片段，判断是否包含本地副作用，而不是只看整条命令开头。

from __future__ import annotations

_LOCAL_MUTATION_PREFIXES = ("mkdir ", "mkdir -p", "touch ", "cp ", "mv ", "tee ")


def command_has_local_mutation(command: str) -> bool:
    return any(_segment_has_local_mutation(segment) for segment in shell_segments(command))


def shell_segments(command: str) -> list[str]:
    pieces: list[str] = []
    for chunk in command.replace("&&", ";").replace("||", ";").split(";"):
        pieces.extend(part.strip() for part in chunk.splitlines())
    return [piece for piece in pieces if piece]


def _segment_has_local_mutation(segment: str) -> bool:
    return any(segment.startswith(prefix) for prefix in _LOCAL_MUTATION_PREFIXES) or ">" in segment
