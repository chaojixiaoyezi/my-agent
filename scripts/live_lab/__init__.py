# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

from __future__ import annotations

"""package for visible, transcripted my-agent runtime test infrastructure.

给人看的解释：
这里放 Live Lab 的实现。
入口、运行器和具体测试 case 分开维护，后面加真实长任务或问题任务时可以只改对应文件。
"""
