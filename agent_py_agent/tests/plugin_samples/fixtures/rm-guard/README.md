# rm-guard：工具收紧样例插件（Python）

演示 v8 文件入口包的"只收紧"钩子：在工具真正执行之前，插件可以要求"再确认一次"或直接拒绝。

- `run_command`（`arguments: "full"`）：命令里出现 **rm 加 -r 和 -f** 的组合时回 `ask`（原因码 `RM_RF`，消息"要删除整个目录，先确认一次"）；
  其它命令回 `allow_as_is`；
- `apply_patch`（`arguments: "full"`）：补丁里出现 `*** Delete File: ` 删除段时回 `deny`（原因码 `DELETE_FILE_BLOCKED`）；只改不删的补丁回 `allow_as_is`；
- **参数被宿主截断时**（请求里 `arguments_truncated: true`，宿主发现参数副本超预算）：截断只能更严——先按看到的片段判：片段已看到删除段仍回 `deny`（原原因码）；片段本来就回 `ask` 的（如 `rm -rf`）保留原原因码、消息补半句"参数还被截断了"；只有片段可放行时才升到 `ask` + `ARGUMENTS_TRUNCATED`；`false`、没有这个字段（旧宿主）或 `1`/`"true"` 这类真值（只认布尔 true）时照旧；
- 其它工具不订阅；不联网（`permissions.network: false`）。

判定只看结构化字段（工具名、参数里的 `command` 与 `patch` 字符串），不按自然语言猜。命中的写法包括：

- `rm -rf …`、`rm -fr …`（短选项连写，含 `-r -f` 分开写和 `-Rf` 这类大小写变体）；
- `rm --recursive --force …`（长选项）；
- `sudo rm -rf …`、`cd /tmp && rm -rf …` 等命令里带独立 `rm` 词的写法。

不命中：`rm 单个文件`、`rm -f 单个文件`、`ls -la`、`grep -rf …`（没有独立 `rm` 词或缺少 r/f 组合）。

**这是演示样例，不是完整 shell 解析器**：收紧钩子是护栏，不是安全边界——插件拒了补丁里的删除段，模型仍可能换别的方式（比如 `run_command rm`）；真正的安全边界仍是宿主的审批、沙箱和只读规则。

## 构建安装包

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/rm-guard/declaration.json \
  --files-root plugins/rm-guard \
  --output /tmp/plugin-build/rm-guard.zip
```

## 安装与启用

1. `/plugins install "/tmp/plugin-build/rm-guard.zip"`
2. `/plugins enable rm-guard`：确认回执里会列出"能看到这些工具的完整参数：run_command"和强制沙箱；
3. `/plugins enable rm-guard --confirm <确认码>`；
4. 之后模型执行 `rm -rf` 命令时，TUI 和 IM 的审批文案会带"[插件 rm-guard 要求确认：RM_RF …]"。

## 说明

- 插件本身**不执行任何命令**，只对宿主发来的征询返回裁决；参数缺失或截断时按"要求确认"处理（宁严勿松）。
- 合同用例：`agent_py_agent/tests/test_plugin_m1_b8_samples.py`（假宿主直连进程，不经 Gateway）。
