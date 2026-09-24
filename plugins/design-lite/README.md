# design-lite

自有工具型插件：按模板在工作区生成一个独立的 HTML 设计文件（卡片、海报、落地页），之后可以只修改标题、
副标题、主色这几个字段。随包带一份 `design-card` Skill（包描述 v3），插件启用期间模型可按需读取它，
了解什么时候用、选哪个模板、先 create 再 edit。两个工具都会写工作区，必须经过宿主写入上下文。
当前已完成本地标准包和独立 MCP 组件验证，宿主审批链与真实 TUI 验收尚未完成。

## 开发构建

先安装主项目开发依赖（`setuptools>=77` 是标准 wheel 构建后端）。构建离线、不下载依赖、不改用户插件安装表；
以下命令从仓库根执行，输出目录由开发者选择，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/design-lite \
  --declaration design_lite/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/design-lite.zip
```

构建脚本会核对 wheel 内 `design_lite/skills/*/SKILL.md` 与声明里的 `skills` 名单完全一致。
插件只依赖精确版本 SDK `my-agent-plugin-api==0.2.0`，不需要在插件环境安装完整 my-agent。

## 使用

安装并启用后提供两个动作，与两个同名工具一一对应：

```text
/plugins@design-lite create --template card --output designs/通知.html --title "周五停电通知" --subtitle "18:00-20:00 机房检修" --color "#3366ff"
/plugins@design-lite edit --path designs/通知.html --title "周六停电通知" --color "#e4572e"
```

中文示例：直接对模型说“帮我做一张活动海报，标题是‘秋季读书会’，副标题写‘每周六下午两点’，用橙色”，
模型会选 `poster` 模板、把橙色换算成 `#RRGGBB` 并调用 `create`；再说“把海报标题改成‘冬季读书会’”，
模型调用 `edit`，结果里会列出每个字段的旧值和新值。

| 模板 | 样子 |
| --- | --- |
| `card` | 白底圆角小卡片，顶部主色色带，标题用主色；默认色 `#3366ff` |
| `poster` | 3:4 竖版，整块主色背景，大号白字标题；默认色 `#e4572e` |
| `landing` | 顶栏 + 居中大标题 + 主色按钮 + 三个要点块；默认色 `#0f766e` |

| 工具 | 效果声明 | 说明 |
| --- | --- | --- |
| `create` | `mutating` | 按模板生成 `.html`，权限 0644；已存在默认拒绝，`--overwrite` 才覆盖 |
| `edit` | `mutating` | 经读取上下文 no-follow 读回，只替换请求的字段，其余字节原样保留，保持原权限位 |

## 边界

- 生成的文件是单文件 HTML：CSS 全部内联，不引用外部资源，不含 `<script>`；标题、副标题全部 HTML 转义。
- 颜色只接受 `#RRGGBB`（统一转小写），不接受颜色名、`rgb()` 或三位短写。
- `edit` 只认本插件生成的文件：头部必须有 `<meta name="generator" content="design-lite ...">`，并且每个
  `data-dl-field` 标记都保持生成时的形态；手工改动标记或颜色行后会被拒绝，而不是猜测替换位置。
- 路径含符号链接（文件本身或任一父目录）、`..` 上溯、越出本次读写范围或不以 `.html` 结尾时一律拒绝。
- `edit` 读回与写回之间不做比较交换，期间他人对同一文件的改动会被覆盖；`create` 的存在检查与写入之间也不加锁。
- 无设置项，不使用插件数据目录。
