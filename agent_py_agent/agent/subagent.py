from __future__ import annotations

"""简单的子任务记录模块。

当前项目还不会真的启动独立子代理，而是先把拆分后的任务计划结构化保存下来。
这样做的意义是：现在先把“怎么拆任务、怎么留痕”打好底，后面再扩展成
真正的多代理执行时，就不用从零设计文件结构了。
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SubAgentTask:
    """子任务计划。

    可以把它理解成“从一个大目标里拆出来的小任务卡片”。
    """

    id: str
    goal: str
    thought: str
    plan: list[str]
    status: str = "PLANNING"
    result: str = ""
    created_at: float = 0.0


class SubAgentManager:
    """负责创建并保存子任务记录。"""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def split(self, goal: str, count: int) -> list[SubAgentTask]:
        """把一个目标拆成若干子任务记录。

        这里暂时还是模板化拆分，不做复杂规划。
        目的不是“真的很聪明地拆”，而是先把整个数据流打通。
        """

        count = max(1, count)
        tasks: list[SubAgentTask] = []
        for i in range(1, count + 1):
            task = SubAgentTask(
                id=f"subagent-{int(time.time())}-{i}",
                goal=f"{goal} / 子任务{i}",
                thought="先缩小任务边界，明确输入、输出和验证证据，再执行。",
                plan=["理解目标", "列出交付物", "执行最小验证", "汇报结果和证据"],
                created_at=time.time(),
            )
            self.save(task)
            tasks.append(task)
        return tasks

    def save(self, task: SubAgentTask) -> None:
        """保存子任务记录。

        一份存成 JSON，方便程序继续处理；
        一份存成 Markdown，方便人直接打开看。
        """

        task_dir = self.workspace / task.id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "thought.md").write_text(
            "# Thought\n\n"
            f"{task.thought}\n\n"
            "## Plan\n"
            + "\n".join(f"- {item}" for item in task.plan)
            + "\n",
            encoding="utf-8",
        )
