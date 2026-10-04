# event-watch：事件观察样例插件

演示 v8 文件入口包的事件订阅：订阅全部 6 类宿主事件（只读结构化事实，**不要正文**），
按事件类型累计收到条数和合并丢弃数，在一个只读表格面板里显示。

- `events`：6 类事件全部 `content: "none"`；
- 面板 `watch`（`kind: "table"`）：显示每类事件的"收到"与"合并丢弃"（`dropped_before` 累计）；
- 未知事件类型归入"其它"行，多余字段忽略——向前兼容，不崩；
- 不声明 `tool_gates`，不联网（`permissions.network: false`）。

面板主题声明为 `run_state` 只是满足面板合同；展示内容来自插件自己的观察计数，不消费主题数据。

## 构建安装包

在仓库根目录执行（输出目录自选，不要放进仓库）：

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/event-watch/declaration.json \
  --files-root plugins/event-watch \
  --output /tmp/plugin-build/event-watch.zip
```

`declaration.json` 里的 `files` 只写路径和是否可执行，摘要由脚本计算；README 不打进包。

## 安装与启用

1. `/plugins install "/tmp/plugin-build/event-watch.zip"`
2. `/plugins enable event-watch`：先看确认回执——订阅了哪些事件、要不要正文、要不要网络、强制沙箱；
3. 核对无误后输入回执最后一行：`/plugins enable event-watch --confirm <确认码>`；
4. 用 `/plugins@event-watch show` 打开面板看计数。

## 说明

- 计数只存在插件进程内存里，退出即丢；观察不是审计日志，事件会合并丢弃（`dropped_before` 就是这个计数）。
- 真实投递语义（合并、单在途、代次撤销、断网、收窄读）由宿主施加，本插件不做任何网络和文件操作。
- 合同用例：`agent_py_agent/tests/test_plugin_m1_b8_samples.py`（假宿主直连进程，不经 Gateway）。
