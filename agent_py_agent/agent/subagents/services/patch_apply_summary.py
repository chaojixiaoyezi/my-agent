# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply summary builder helper."""

from __future__ import annotations


# LLM: PatchApplySummary 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 描述补丁应用summary的结构化结果，供审计、渲染或测试按字段读取；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
class PatchApplySummary:
    """Build patch apply summary statistics."""

    # LLM: build 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建build所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build(records) -> dict[str, int]:
        """Build summary from patch apply records."""
        summary = {"total": len(records)}
        for record in records:
            summary[record.decision] = summary.get(record.decision, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
            if record.rollback_performed:
                summary["rolled_back"] = summary.get("rolled_back", 0) + 1
        return summary