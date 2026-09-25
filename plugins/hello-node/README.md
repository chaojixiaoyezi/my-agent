# hello-node：Node.js 示例插件

演示“系统解释器 + 随包脚本”（包描述 v6 的 `interpreter` 类型）：宿主用本机的 `node` 运行 `src/server.js`。
提供两个工具：

- `hello`：打个招呼，并说明是哪个 Node.js 版本、哪个平台在运行它；
- `read_text`：按宿主本次下发的工作区读取权限，读取一个 UTF-8 文本文件的开头部分。

只用 Node 标准库，没有 npm 依赖。需要本机能在 Gateway 的 PATH 里找到 `node`。

## 构建安装包

在仓库根目录执行（输出目录自选，不要放进仓库）：

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/hello-node/declaration.json \
  --files-root plugins/hello-node \
  --output /tmp/plugin-build/hello-node.zip
```

`declaration.json` 里的 `files` 只写路径和是否可执行，摘要由脚本计算；`conformance.js` 和 README 不打进包。

## 安装与启用

1. `/plugins install "/tmp/plugin-build/hello-node.zip"`
2. `/plugins enable hello-node`：这一步不会启用，而是列出确认回执——会用哪个 node（绝对路径和文件摘要）运行哪个脚本、
   包里有哪些文件，最后一行是带确认码的命令。
3. 核对无误后输入回执最后一行：`/plugins enable hello-node --confirm <确认码>`。
4. 使用：`/plugins@hello-node hello 小明`、`/plugins@hello-node read notes.txt`；模型也能在对话里调用这两个工具。

之后如果 node 被升级或替换（文件内容变了），插件会拒绝启动；先 `/plugins disable hello-node`，再按第 2、3 步重新确认。

## 工作区读取检查

`src/workspace_read.js` 是宿主 Python 实现的逐行移植：先逐段解析符号链接（`..` 作用在已解析的父目录上），
再判断是否在本次读取范围内，最后做 owner 墙、凭据文件名和危险目录裁决。改动它之后必须重跑一致性用例：

```bash
python3 -m pytest agent_py_agent/tests/test_plugin_any_language_samples.py -q
```

Node 没有 openat，读文件时采用“打开真实目标（末段不跟随链接）→ 重新解析并比对 dev/ino”的打开后复核，
检查与打开之间被换成链接会拒绝；竞态强度低于 Python SDK 的逐段目录描述符打开。插件以用户本人权限运行，
这份检查是与宿主约定的协作规则，不是操作系统沙箱。
