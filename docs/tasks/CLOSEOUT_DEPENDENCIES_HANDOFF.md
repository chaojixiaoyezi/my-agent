# 第 7 步初次提交依赖收窄交接

## 本线目标与范围

初次提交和 WAL／运行账原语不再持有完整 manager。修改 runner_result_commit、runner_result_service、
runtime_closeout，新增提交顺序与故障测试；不修改另一条线的配对发布和通知实现。

## 已实现

- 初次提交只接 RuntimeDB、task、params、result、save 与绑定交付回调。
- WAL 写入只接 save，结算和诊断只接 RuntimeDB；恢复装配点已迁移这些调用。
- trace→父通知仍在原持久顺序之后，失败保留原 WAL，删掉多参数的内部透传辅助函数。

## 验证与集成注意

候选 5 个定向文件 64 passed、1 skipped；集成配对发布修复后，18 个组合文件 391 passed、2 skipped。
dispatch_liveness 两处 record_pending_closeout 已改传 manager.save，原半写红灯转绿。
新版尚未推送部署，本地严格 gate 已通过；真实 TUI 结果按运行包分别记录于 TESTS／STATUS。

## 建议下一步

组合修复已合入并通过 focused；完成发布 gate，随后收窄恢复扫描和
父通知内部依赖；这两个写入区域按单负责人推进，不与配对发布线同时修改同文件。
