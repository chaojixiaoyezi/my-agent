# 第 7 步初次提交依赖收窄交接

## 本线目标与范围

初次提交和 WAL／运行账原语不再持有完整 manager。修改 runner_result_commit、runner_result_service、
runtime_closeout，新增提交顺序与故障测试；不修改另一条线的配对发布和通知实现。

## 已实现

- 初次提交只接 RuntimeDB、task、params、result、save 与绑定交付回调。
- WAL 写入只接 save，结算和诊断只接 RuntimeDB；恢复装配点已迁移这些调用。
- trace→父通知仍在原持久顺序之后，失败保留原 WAL，删掉多参数的内部透传辅助函数。

## 验证与集成注意

5 个定向文件 64 passed、1 skipped；严格尺寸通过。完整相关回归和发布 gate 仍待组合源码执行。
旧 dispatch_liveness 测试两处 record_pending_closeout 调用须从 manager 改传 manager.save，待配对发布分支先合入。
当前基线含另一条线负责修复的两项半写红灯，不能推送本候选。

## 建议下一步

先合入配对发布修复，再迁移两处测试调用并跑组合 focused 与严格 gate。随后收窄恢复扫描和
父通知内部依赖；这两个写入区域按单负责人推进，不与配对发布线同时修改同文件。
