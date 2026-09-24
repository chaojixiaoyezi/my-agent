# worktable-lite

自有纯展示插件：在 TUI 面板查看本用户最近的会话（最多 20 条，按更新时间倒序，由宿主给出），每行显示当前会话标记（`▶`
与"（当前）"）、会话编号、相对时间（刚刚 / N 分钟前 / N 小时前 / N 天前；时间缺失写"时间未知"）和渠道，末尾提示
"回到某个会话：my-agent resume <会话编号>"。没有会话时显示"还没有会话记录"。
没有工具，不读文件、不联网、不写数据；只实现只读的 `my-agent/display.render`，输入只有宿主裁剪后的公开主题 `sessions`。
协议见 [插件展示](../../docs/design/PLUGIN_DISPLAY.md)。

## 设置（只保存本插件的显示偏好）

| 设置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_rows` | 整数 1–20 | 8 | 面板最多显示的会话条数；text 面板上限 20 行，其中一行留给末尾提示，所以实际最多 19 条 |
| `hide_current` | 布尔 | false | 为 true 时隐藏当前会话那一行；只剩当前会话时显示"没有其他会话记录" |

设置由宿主按 `settings_schema` 校验后经 `MY_AGENT_PLUGIN_SETTINGS` 在进程启动时注入；设置无效时进程以"插件设置无效"退出，
不回显设置值。相对时间按插件进程当下的 `time.time()` 计算。

## 开发构建

```bash
python scripts/build_plugin_package.py \
  --project plugins/worktable-lite \
  --declaration worktable_lite/declaration.json \
  --output /tmp/plugin-build/worktable-lite.zip
```

## 使用

安装并启用后，在 TUI 输入 `/plugins@worktable-lite list` 打开或关闭"最近会话"面板；找到想回去的会话编号后，在终端执行
`my-agent resume <会话编号>`。停用或卸载后面板立即消失。
