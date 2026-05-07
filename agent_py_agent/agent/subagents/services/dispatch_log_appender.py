# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Dispatch log appending helper."""

from __future__ import annotations

from agent_py_agent.agent.file_io import append_jsonl


# LLM: DispatchLogAppender 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装调度logappender相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class DispatchLogAppender:
    """Write global dispatch audit log."""

    # LLM: append 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入append的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def append(record, workspace) -> None:
        """Write global dispatch audit log entry."""
        from dataclasses import asdict

        jsonl = workspace / "subagent_dispatch_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = workspace / "DISPATCH_LOG.md"
        if not markdown.exists():
            markdown.write_text("# DISPATCH LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            run = record.run_id or "global"
            handle.write(
                f"- [{status}] {record.id} step={record.step} action={record.action} "
                f"run={run} applied={record.applied} message={record.message}\n"
            )