# Contracts Map

当前合同地图见根目录 `CONTRACTS_MAP.md`。本文件只保留模块设计入口，避免和根目录台账重复。

主规则：

- 工具、路径、命令、审批在工具入口检查。
- 子代理状态以 canonical state 为真源。
- 交付收口只接受当前 run 产物证明或明确恢复 lineage。
- 任何坏账本都要暴露 load error，不能伪装成“没有数据”。
