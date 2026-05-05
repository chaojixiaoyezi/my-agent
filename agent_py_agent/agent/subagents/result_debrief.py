from __future__ import annotations

"""LLM: append structured runner output sections to the DEBRIEF file.

给人看的解释：
把结构化 runner 产出追加到 DEBRIEF，方便人接管。
从 result_processors.py 拆出来，让那个文件只保留核心处理逻辑。
"""

import time
from pathlib import Path

from .models import SubAgentParsedOutput, SubAgentTask
from .runner_rendering import _render_runner_item_line


def _append_runner_debrief_content(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
) -> None:
    """LLM: append structured runner output sections to the DEBRIEF file.

    新手说明:
    把结构化 runner 产出追加到 DEBRIEF，方便人接管。包括 artifacts、tests、patches、
    lessons 和 next_actions 五个段落。
    """

    sections: list[str] = []
    if parsed.artifacts:
        sections.append("## Runner Artifacts")
        sections.extend(_render_runner_item_line(item) for item in parsed.artifacts)
    if parsed.tests:
        sections.append("## Runner Tests")
        sections.extend(_render_runner_item_line(item) for item in parsed.tests)
    if parsed.patches:
        sections.append("## Runner Patches")
        sections.extend(_render_runner_item_line(item) for item in parsed.patches)
    if parsed.lessons:
        sections.append("## Runner Lessons")
        sections.extend(f"- {item}" for item in parsed.lessons)
    if parsed.next_actions:
        sections.append("## Runner Next Actions")
        sections.extend(f"- {item}" for item in parsed.next_actions)
    if not sections:
        return

    path = Path(task.debrief_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# DEBRIEF\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n## Runner Structured Output\n\n")
        handle.write(f"- created_at: {time.time()}\n")
        handle.write(f"- run_id: {task.id}\n\n")
        handle.write("\n\n".join(sections))
        handle.write("\n")
