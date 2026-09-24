# status-pet

自有纯展示插件：在 TUI 面板用一只文字小宠物显示当前运行状态——工作中、等待审批、空闲三种状态各有一幅 3–4 行的
字符小图，下面一行是状态文字（例如"小咪 正在工作 · 子代理 2 · 读取文件"；等待审批以"【等待审批】"开头）。
没有工具，不读文件、不联网、没有定时器或动画线程；只实现只读的 `my-agent/display.render`，每次渲染只按宿主当次
给的公开主题（`run_state`、`activity`）作画。字符图均为本仓自绘。协议见 [插件展示](../../docs/design/PLUGIN_DISPLAY.md)。

## 设置

| 字段 | 取值 | 默认 |
| --- | --- | --- |
| `style` | `cat`（猫）、`whale`（鲸）、`robot`（机器人） | `cat` |
| `name` | 1–12 个字符的名字 | `小咪` |

非法设置（未知外观、名字为空或超过 12 个字符、未声明字段）会被宿主 `/plugins configure` 拒绝，原设置不变。

## 开发构建

```bash
python scripts/build_plugin_package.py \
  --project plugins/status-pet \
  --declaration status_pet/declaration.json \
  --output /tmp/plugin-build/status-pet.zip
```

## 使用

1. 安装：`/plugins install "/tmp/plugin-build/status-pet.zip"`，再 `/plugins enable status-pet`。
2. 在 TUI 输入 `/plugins@status-pet show` 打开或关闭宠物面板。停用或卸载后面板立即消失。

## 改外观

设置只在插件进程启动时读取，所以要按"停用 → 配置 → 启用"的顺序改：

1. 写一个设置文件，例如 `status-pet-whale.json`：`{"style": "whale", "name": "蓝蓝"}`。
2. `/plugins disable status-pet`
3. `/plugins configure status-pet --file "status-pet-whale.json"`
4. `/plugins enable status-pet`，再 `/plugins@status-pet show` 打开面板，即可看到叫"蓝蓝"的小鲸鱼。
