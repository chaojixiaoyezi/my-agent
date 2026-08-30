
"""Internal modules for the split SimpleAgent core implementation.

SimpleAgent 的主循环、子代理、dispatch、prompt 模板、工具类和参数解析已经拆到这里。
`agent.core` 是唯一组合入口；本包初始化保持无副作用，避免导入任一叶子模块时
提前装载整套子代理运行时并形成循环依赖。
"""

# LLM: agent_core 是内部实现包，__init__ 禁止 re-export 运行时类或工具；调用方
# 必须从具体模块导入，避免 package import 触发整棵依赖树和循环导入。
# 模块用途: 声明拆分后的核心实现包，不执行运行时代码。

from __future__ import annotations
