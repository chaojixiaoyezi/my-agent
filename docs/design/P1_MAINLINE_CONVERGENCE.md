# P1 主链收敛

更新时间：2026-07-09。状态以 `docs/PRODUCT_FACTS.md` 为准；本文记录实现边界和验收方法，
不把确定性测试写成十万用户生产证明。

## 目标与结论

P1 把原先散落的“接口预留、开发 harness、可选路径”收成一条可发布主链：

1. 生产导入和分层规则由 CI 执行，不再只写在架构文档里。
2. 测试、offline contract、专项 real-e2e harness 保留在源码仓，但不进入生产 wheel。
3. 无子命令 `my-agent` 是唯一普通用户入口，默认 runtime 是 gateway；前台直连必须显式 `--direct`。
4. 插件只走管理员显式配置的 module/entrypoint 链，不扫描用户可写目录执行代码。
5. 工具检索接真实 EmbeddingProvider；PTY、LSP、OpenAI native tool calls 都有真实协议链。

## 边界实现

`scripts/check_import_boundaries.py` 执行生产/开发包隔离、agent→CLI 单向规则和完整分层矩阵。
接入时发现的 28 条历史债务只允许精确 source/target 例外；任何新反向依赖直接失败。

`package_boundary_policy.py` 是源码选择和 wheel 检查的共同权威。`setup.py` 的 build command
在每次构建后清除 forbidden 输出，避免 setuptools 复用旧 `build/lib` 把历史测试文件重新装进 wheel。
`scripts/check_distribution_boundary.py` 再检查最终 zip 成员，形成“构建时排除 + 制品后验”双门；同时逐项
核对 `agent_py_agent/` payload 在当前源码树中真实存在，防止 Setuptools 复用旧 `build/` 时把已经删除的
模块或资源重新装回 wheel。该规则对照 通道运行时/会话运行时 先 clean dist、再检查 pack 文件清单的发布边界。

## 入口和插件

正式路径是：

```text
my-agent → ensure gateway → chat client → gateway request → agent runtime
```

`my-agent run` 是一次性脚本接口，`my-agent chat --direct` 是开发调试接口；两者不能被描述成第二个
默认 runtime。插件发现由 `extension_plugins` 的有序列表决定，插件失败不会被静默跳过。

## 能力主链

- 语义检索：工具完整说明进入 embedding，工具目录变化时重建缓存，查询按 cosine 召回并与关键词合并；
  `list_tools.tool_retrieval` 报告 enabled/configured/ready/provider/last_error。
- PTY：单一 `terminal_session` 工具提供 start/write/read/close，输出有界且使用增量 cursor；复用
  shell 工作区、危险命令和 owner bwrap 策略。
- LSP：单一 `lsp` 工具惰性启动管理员配置的 stdio server，运行 initialize/request/didOpen/
  diagnostics/shutdown；文档路径只允许工作区，owner server 同样进入 bwrap。
- OpenAI native：内部继续使用一套 ToolSpec 与 ToolCall/ToolResult IR，在 provider 边界翻译为
  OpenAI function tools、assistant tool_calls、role=tool；SSE 分片 arguments 会累积解析。

## 支持边界

- PTY 当前只承诺 POSIX；Windows ConPTY 未实现。
- LSP 尚未完成 pyright/rust-analyzer/typescript-language-server 等真实兼容矩阵和长稳测试。
- 语义检索需要部署者配置 embedding model/endpoint；默认空配置不发外部请求。
- OpenAI native 当前有协议级确定性测试，本轮没有调用付费 provider，不能宣称模型矩阵完成。
- 这些能力没有改变 P2：PostgreSQL/ASGI/RLS 正式化、Redis、OTel、在线迁移和规模证明仍待完成。

## 验收命令

```bash
python3 scripts/check_import_boundaries.py
python3 -m pytest agent_py_agent/tests/test_packaging.py \
  agent_py_agent/tests/test_extension_plugin.py \
  agent_py_agent/tests/test_tooling_base.py \
  agent_py_agent/tests/test_pty_sessions.py \
  agent_py_agent/tests/test_lsp_client.py \
  agent_py_agent/tests/test_backends_openai_native_tool_use.py -q
python3 -m pip wheel . --no-build-isolation --no-deps --wheel-dir dist
python3 scripts/check_distribution_boundary.py dist/*.whl
python3 scripts/check_clean_package.py --mode artifact dist/*.whl
```

最终提交前仍要跑 `docs/PRODUCT_FACTS.md` 的完整发布判定，不能用上面的 focused 集替代全量 pytest。
