# 父终态通知依赖交接

## 本线目标

去掉自然完成和受控取消通知对整个 manager 的依赖，保留原身份、关联、通知和错误落账语义。

## 实际完成与文件

- runner_completion_wake：RunnerCompletionNotifier 实际持有并执行原终态逻辑，仅四项依赖；旧两个终态函数删除。
- runner_result_service／agent_control：在实际结果或控制装配边界绑定四项依赖。
- direct_parent_lifecycle／service_window_semantics 测试：迁移入口；两项 PENDING 替身补明确 wake 端口并断言 skipped，未放宽原不通知合同。
- 文档同步接口边界与后续集成点；未新增依赖或配置。

## 验证

直属父级、服务窗口、活动提醒、Gateway 控制、子代理资源停止和结果状态共 6 文件 130 passed。
Ruff／doc sync／严格尺寸／diff／clean-package 已通过；接口组合仍必须执行下一节的完整迁移和相关回归。

## 必须组合的接口迁移

不能单独发布本片。恢复扫描线仍按旧通知函数装配，组合时把 capability_auto_sweep 的通知回调
改成同一通知器的 notify_result 方法；恢复模块自身保持它新增的窄回调接口。
dispatch_liveness 两处故障 monkeypatch 及其恢复测试装配也要迁到新类，保留原中断和错误断言。

## 建议下一步

先合入恢复扫描片，再集成本片并迁移仅有的通知装配与测试替身，跑组合 focused 和严格 gate。
实际 TUI 用同版包覆盖失败／取消／恢复与并发隔离；其他 agent 可以只读审阅，勿同时改这两个接口连接处。
