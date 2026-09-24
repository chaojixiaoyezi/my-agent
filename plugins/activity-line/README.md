# activity-line

自有纯展示插件：在 TUI 面板显示当前运行状态（工作中 / 等待审批 / 空闲）、活动描述、耗时、活动任务、子代理和压缩次数。
没有工具，不读文件、不联网；只实现只读的 `my-agent/display.render`，输入是宿主裁剪后的公开主题（`activity`、`run_state`）。
协议见 [插件展示](../../docs/design/PLUGIN_DISPLAY.md)。

## 开发构建

```bash
python scripts/build_plugin_package.py \
  --project plugins/activity-line \
  --declaration activity_line/declaration.json \
  --output /tmp/plugin-build/activity-line.zip
```

## 使用

安装并启用后，在 TUI 输入 `/plugins@activity-line show` 打开或关闭面板。停用或卸载后面板立即消失。
