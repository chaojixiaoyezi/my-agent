# DESIGN LEDGER

当前设计铁律：

- 先跑通一条主链路，再谈扩展。
- 修当前链路，不为历史目录、历史字段或历史工具形态加旁路。
- 主代理长期记忆归 owner home；子代理只保留任务周期内可审计状态。
- 子代理可以写协作产物，但最终交付由主代理汇总和验收。
- 工具面要少，优先增强现有工具和运行时语义。
- 文件大小不是硬门；是否合并或拆分看调用链是否清楚。

当前入口文档：

- 架构总览：`docs/design/ARCHITECTURE_GUIDE.md`
- 模块结构：`docs/architecture/MODULE_OWNERSHIP.md`
- Home 布局：`docs/architecture/MY_AGENT_HOME_LAYOUT.md`
- Subagent：`docs/modules/subagent/04-structure.md`
- Memory：`docs/modules/memory/04-structure.md`
- Gateway：`docs/modules/gateway/04-structure.md`
- 代码尺寸报告：`CODE_SIZE_REPORT.md`

以后新增长期设计，只写摘要和链接，不再把完整方案塞回这个文件。
