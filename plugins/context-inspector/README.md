# context-inspector

自有纯展示插件：在 TUI 面板查看当前会话最近一次模型调用前的上下文组成——当前用量 / 窗口（百分比）、自动压缩触发线、
消息历史 / 运行引导 / 工具目录三部分 token 数，以及已压缩次数。宿主标记为估算时面板注明"估算"；还没有快照时只提示
"还没有本会话的上下文快照（发送一条消息后出现）"，不编数字。
没有工具、没有设置，不读文件、不联网；只实现只读的 `my-agent/display.render`，输入只有宿主裁剪后的公开主题 `context`。
协议见 [插件展示](../../docs/design/PLUGIN_DISPLAY.md)。

## 开发构建

```bash
python scripts/build_plugin_package.py \
  --project plugins/context-inspector \
  --declaration context_inspector/declaration.json \
  --output /tmp/plugin-build/context-inspector.zip
```

## 使用

安装并启用后，在 TUI 输入 `/plugins@context-inspector show` 打开或关闭面板。停用或卸载后面板立即消失。
