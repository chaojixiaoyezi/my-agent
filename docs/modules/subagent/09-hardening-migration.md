# Subagent Hardening

当前 hardening 只保留主链路需要的内容。

## 保留

- dangerous roots / owner policy / explicit grants。
- runner 启动早期健康确认；秒退标记 channel error。
- 子代理 cancel/takeover 控制面。
- tree/rollup 中展示未完成、失败、卡住和无进展原因。
- compact 续接读取当前 task-local state 和 refs。

## 不保留

- 非安全类前置硬门。
- 活跃子代理存在就禁止继续派工。
- 子代理写协作产物时默认拒绝父 task output。
- 历史路径读取旁路。
- 为旧字段新增隐藏开关。
