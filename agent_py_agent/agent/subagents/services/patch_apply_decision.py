# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply decision helper."""

from __future__ import annotations


# LLM: PatchApplyDecision 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 描述补丁应用decision的结构化结果，供审计、渲染或测试按字段读取；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
class PatchApplyDecision:
    """Determine patch apply decision based on conditions."""

    # LLM: decide 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理decide相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    @staticmethod
    def decide(patches, patch_specs, blocked_count, apply):
        """Determine patch apply decision based on conditions."""
        if not patches:
            return ("NO_PATCHES", False, "没有 patch 可以 apply。")
        if blocked_count:
            return ("REJECT", False, f"{blocked_count} 项 patch/test 不满足 apply 条件。")
        if not apply:
            return ("WOULD_APPLY", True, f"dry-run: 将 apply {len(patch_specs)} 个 patch。")
        return ("APPLIED", True, f"已 apply {len(patch_specs)} 个 patch。")