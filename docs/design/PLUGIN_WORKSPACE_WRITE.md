# 插件工作区写入上下文

状态：第 10 步协议与宿主运输已在本地实施（SDK 0.2.0），插件侧写入原语与真实 TUI 未验。与 [读取上下文](PLUGIN_WORKSPACE_CONTEXT.md) 对称；上位合同见 [可装卸插件方案](PLUGIN_LIFECYCLE.md)。

## 解决问题

savepoint-lite、design-lite、genui-lite 等插件需要在用户工作区写文件。插件运行在独立进程，宿主无法逐次代替它写；
但写入权限必须与内置 `write_file` 完全一致：同一套危险路径、owner 墙、任务写入范围、禁止目录与锁定文件，
审批和操作记账沿原执行链，不能给插件另开一条更宽的写路径。

## 协议

- 扩展名 `my-agent/workspace-write-context`，版本 `1`。插件在 `initialize` 的 `capabilities.experimental` 中声明。
- 宿主只在同时满足以下条件时，把写入上下文放进本次 `tools/call` 的 `_meta`：插件已协商该扩展；工具声明的
  `requested_effect` 为 `mutating` 或 `dangerous`；本次调用已经通过原审批与执行权门。只读工具永远拿不到写入上下文。
- 上下文字段：`cwd`、`write_roots`、`forbidden_roots`、`locked_files`、`output_json`、`product_roots`、`path_policy`
  （路径与策略与读取上下文同一序列化；字段集合和版本严格匹配，畸形值直接拒绝）。
- 插件已协商扩展且工具声明写效果、但本次调用没有写入上下文时，宿主在发送前以 `not_started` 失败，不静默降级。
  这些值由宿主从本次调用的 `write_boundary` 与路径策略预先解析得到，插件不能扩大。

## 裁决与写入

- 插件端 `check(path)` 的规则与宿主 `validate_write_boundary` 相同：先过路径策略（危险路径、owner 墙），
  再要求目标在 `write_roots` 之内，不是产物目录里的内部 `output.json`，不在未被更具体允许根覆盖的禁止根之内，也不是锁定文件。
- 写入根：有任务写入范围（`allowed_write_roots` 等字段存在）时取宿主解析出的允许根；没有时只取当前 `cwd`。
  后者比内置 `write_file` 更严（内置工具在无范围时只受路径策略约束，可写 owner 家目录其他位置），插件永远不更宽。
- 一致性由宿主测试强制：对大量生成的目标路径与边界组合，断言「插件允许 ⇔ 宿主 `validate_write_boundary` 允许
  且目标在写入根之内」（`test_workspace_write_context.py`，2000 组边界 × 4 个目标）。
- 实际写入只能用 SDK 的 `write_bytes_atomic_beneath` / `unlink_file_beneath`（不跟随符号链接、原子替换），
  以通过裁决的写入根为锚点逐级打开目录。
- 写入发生在插件进程内，宿主记账沿原 operation；结果未知仍记 UNKNOWN，不改写成功或失败。

## 不做的事

不提供任意路径写入、不绕过审批、不允许插件写宿主私有目录（插件自有数据另由宿主管理的插件数据目录承担，
与本扩展分开设计）；不在宿主为某个插件 ID 写特判。
