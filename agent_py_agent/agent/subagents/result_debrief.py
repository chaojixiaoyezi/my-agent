# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

# LLM: 明确使用 UTF-8，避免 Windows 默认区域设置破坏复盘文件内容。

"""append structured runner output sections to the DEBRIEF file.

给人看的解释：
把结构化 runner 产出追加到 DEBRIEF，方便人接管。
从 result_processors.py 拆出来，让那个文件只保留核心处理逻辑。
"""

import time
from pathlib import Path

from .models import SubAgentParsedOutput, SubAgentTask
from .runner_rendering import _render_runner_item_line


# LLM: _append_runner_debrief_content 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 写入执行器复盘内容的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _append_runner_debrief_content(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
) -> None:

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
