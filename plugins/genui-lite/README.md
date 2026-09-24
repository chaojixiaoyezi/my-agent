# genui-lite

自有工具型插件：把工作区里的 JSON 结构化数据渲染成 Markdown 表格和文字条形图，或导出成独立 HTML 文件
（内联样式、表格与内联 SVG 条形图，不含脚本，不引用任何外部脚本、字体或网络资源）。随包带一份 Skill
`genui-table`（包描述 v3），插件启用期间才出现在 Skill 目录里。当前已完成本地标准包和独立 MCP 组件验证，
宿主审批链与真实 TUI 验收尚未完成。

## 开发构建

先安装主项目开发依赖（`setuptools>=77` 是标准 wheel 构建后端）。构建离线、不下载依赖、不改用户插件安装表；
以下命令从仓库根执行，输出目录由开发者选择，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/genui-lite \
  --declaration genui_lite/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/genui-lite.zip
```

`skills/genui-table/SKILL.md` 经 `pyproject.toml` 的 package-data 打进 wheel；构建脚本会校验 wheel 内
Skill 目录与声明 `"skills": ["genui-table"]` 完全一致，不一致时构建失败。
插件只依赖精确版本 SDK `my-agent-plugin-api==0.2.0`，不需要在插件环境安装完整 my-agent。

## 使用

安装并启用后提供两个动作，与两个同名工具一一对应：

```text
/plugins@genui-lite table --path genui-sales.json --chart 销售额
/plugins@genui-lite export --path genui-sales.json --output 报表/销售.html --chart 销售额 --title "上半年销售"
/plugins@genui-lite export --path genui-sales.json --output 报表/销售.html --overwrite
```

中文示例：直接说"用 genui-lite 把 genui-sales.json 做成表格，按销售额画个条形图"，模型会调用 `table`；
说"把它导出成 HTML 报告放到 报表/销售.html"，模型会调用 `export`，宿主先弹审批再写文件。

数据只支持两种形状：对象数组 `[{"月份": "1月", "销售额": 12800}]`，或
`{"columns": ["月份", "销售额"], "rows": [["1月", 12800]]}`；单元格只能是字符串、数字、布尔或 null。

| 工具 | 效果声明 | 说明 |
| --- | --- | --- |
| `table` | `read_only` | 返回 Markdown 表格、可选 █ 文字条形图（按最大值等比）和行列计数摘要；不写任何文件 |
| `export` | `mutating` | 渲染独立 HTML，经写入上下文 check → anchor 字面路径比对 → no-follow 原子写出，权限 0644 |

`export` 的输出必须以 `.html` 结尾；输出已存在时默认拒绝（`OUTPUT_EXISTS`），带 `--overwrite` 才覆盖。
返回写出路径、字节数和行列数。

## 设置

- `max_input_bytes`：输入 JSON 字节上限，默认 524288（512 KiB），超过拒绝（`FILE_TOO_LARGE`）。
- `max_rows`：渲染的最多行数，默认 200；超过时表格、图表和 HTML 都只含前 `max_rows` 行，结果 `truncated: true`
  并在摘要里注明。

取值范围见唯一声明 `src/genui_lite/declaration.json`；设置不增加任何读写权限。

## 错误

全部是中文 `isError` 结果、不带栈：`INVALID_JSON`、`UNSUPPORTED_FORMAT`、`COLUMN_MISMATCH`、
`CHART_COLUMN_NOT_FOUND`、`CHART_COLUMN_NOT_NUMERIC`（含非数字或负数）、`FILE_TOO_LARGE`、`FILE_NOT_FOUND`、
`INVALID_PATH`/`INVALID_OUTPUT`、`OUTPUT_EXISTS`、`MISSING_CONTEXT`、`MISSING_WRITE_CONTEXT`，以及越界时宿主
路径裁决给出的 `PATH_READ_SCOPE_BLOCKED`/`PATH_WRITE_SCOPE_BLOCKED` 与链接路径的 `UNSAFE_PATH`。

## 边界

- 输入与输出路径含符号链接（文件本身或任一父目录）、多链接文件、非普通文件、`..` 上溯或越出本次读写范围时一律拒绝。
- 输出父目录不存在时由 SDK 以 0755 创建；文件以 0644 创建，实际权限仍受进程 umask 约束。
- "已存在则拒绝"是写前检查：检查与原子替换之间仍有并发窗口，SDK 目前没有"仅新建"的写入原语。
