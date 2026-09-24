# savepoint-lite

自有工具型插件：对明确指定的工作区文件创建、列出和恢复快照。快照只保存在宿主提供的插件自有数据目录
（`MY_AGENT_PLUGIN_DATA_DIR`，即 `<owner>/data/plugins/data/savepoint-lite`），从不写进用户工作区；
只有 `restore` 会改写工作区文件，并且必须经过宿主写入上下文。当前已完成本地标准包和独立 MCP 组件验证，
宿主审批链与真实 TUI 验收尚未完成。

## 开发构建

先安装主项目开发依赖（`setuptools>=77` 是标准 wheel 构建后端）。构建离线、不下载依赖、不改用户插件安装表；
以下命令从仓库根执行，输出目录由开发者选择，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/savepoint-lite \
  --declaration savepoint_lite/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/savepoint-lite.zip
```

插件只依赖精确版本 SDK `my-agent-plugin-api==0.2.0`，不需要在插件环境安装完整 my-agent。

## 使用

安装并启用后提供三个动作，与三个同名工具一一对应：

```text
/plugins@savepoint-lite save --path "说明 文件.md"
/plugins@savepoint-lite list --path "说明 文件.md"
/plugins@savepoint-lite restore --path "说明 文件.md" --id 20260924-101530-a1b2 --expect 3f2a9c1d
```

中文示例：改稿前先 `save` 一份；想回退时先 `list`，结果里 `current: true` 标出与当前内容相同的快照，
`current_sha256` 是当前文件的完整 sha256；再用它的前 8 位（或更长）作为 `--expect` 执行 `restore`。
如果这期间别人改过文件，`--expect` 对不上，插件拒绝恢复并返回新的 `current_sha256`，请先确认改动再决定。

| 工具 | 效果声明 | 说明 |
| --- | --- | --- |
| `save` | `read_only` | 读工作区文件，只写插件自有数据目录；不修改工作区 |
| `list` | `read_only` | 列出快照编号、时间、字节数、sha256 前 12 位，标出与当前内容相同者 |
| `restore` | `mutating` | 先核对 `--expect`，再经写入上下文裁决、no-follow 原子替换写回，保持原权限位 |

`save` 声明 `read_only` 的理由：它不写工作区、不需要写入上下文，按最小权限不应拿到工作区写权；
它写的是宿主为本插件单独创建的数据目录。

## 设置

- `max_file_bytes`：单个文件快照上限，默认 1048576（1 MiB），超过时 `save` 拒绝。
- `max_snapshots_per_file`：每个文件最多快照数，默认 20；达到上限拒绝新保存并提示先清理，不自动删除旧快照。

取值范围见唯一声明 `src/savepoint_lite/declaration.json`；设置不增加任何读写权限。

## 边界

- 快照按“规范化绝对路径的 sha256”分目录：`snapshots/<sha256>/<id>.bin` 为内容，`<id>.json` 为元数据
  （编号、原路径、sha256、字节数、创建时间），元数据写成才算快照提交。
- 路径含符号链接（文件本身或任一父目录）、多链接文件、非普通文件、`..` 上溯或越出本次读写范围时一律拒绝。
- 恢复前校验快照内容 sha256，损坏的快照不会写回。
- 当前没有删除快照的动作；达到数量上限时需在数据目录手动清理。
