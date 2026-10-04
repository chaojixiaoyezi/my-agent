# rm-guard-node：工具收紧样例插件（Node.js）

和 [`plugins/rm-guard`](../rm-guard/) 行为完全一致的 Node.js 版本，用来演示"任意语言插件"都能写 v8 收紧钩子：

- `run_command`（`arguments: "full"`）：命令里出现 **rm 加 -r 和 -f** 的组合时回 `ask`（原因码 `RM_RF`）；
- `apply_patch`（`arguments: "full"`）：补丁里出现 `*** Delete File: ` 删除段时回 `deny`（原因码 `DELETE_FILE_BLOCKED`）；只改不删的补丁回 `allow_as_is`；
- **参数被宿主截断时**（请求里 `arguments_truncated: true`，宿主发现参数副本超预算）：不再按看到的片段判，直接回 `ask`（原因码 `ARGUMENTS_TRUNCATED`，消息"参数太长被截断，看不全，先确认一次"）；`false` 或没有这个字段（旧宿主）时照旧；
- 其它工具不订阅；不联网；只用 Node 标准库，没有 npm 依赖。

判定逻辑与 Python 版逐条对齐（同一组合同用例对两个实现断言同样的输出）。需要本机能在 Gateway 的 PATH 里找到 `node`。

## 构建安装包

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/rm-guard-node/declaration.json \
  --files-root plugins/rm-guard-node \
  --output /tmp/plugin-build/rm-guard-node.zip
```

## 安装与启用

与 rm-guard 相同：先 `/plugins install`，再 `/plugins enable rm-guard-node` 看确认回执，最后用确认码启用。

## 说明

- 插件本身**不执行任何命令**，只对宿主发来的征询返回裁决；参数缺失或截断时按"要求确认"处理（宁严勿松）。
- 合同用例：`agent_py_agent/tests/test_plugin_m1_b8_samples.py`（假宿主直连进程，不经 Gateway；本机没有 node 时该用例跳过）。
