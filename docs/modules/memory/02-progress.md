# Memory：开发推进记录

## 已完成

- `memory_store/` 已承接长期记忆 JSONL 存储，根层 `memory.py` 保留兼容入口。
- `memory_routing/` 已有 route index 加载、匹配、校验、上下文读取和 receipt 结构。
- `memory_archive/` 已有压缩前 snapshot、raw event、每日 hook/raw JSONL、留存和 token 估算骨架。
- `memory-route`、`memory-doctor`、`memory archive` 相关 CLI 和测试已存在。
- `SimpleAgent.run()` 已有 routed memory 和 raw archive 的回归测试覆盖。
- `settings/memory.py`、`memory_store/jsonl.py`、`memory_routing/models.py`、`memory_routing/matcher.py` 已补齐更详细的 `LLM:` / `新手说明:` / 参数和返回说明。
- `memory_archive/` 整圈、`memory_routing/loader.py`、`memory_routing/context.py`、`cli/memory_commands.py`、`cli/memory_archive_commands.py` 已补齐同等级中文教学注释、字段说明、参数说明和返回说明。
- raw archive 写入现在和 hook snapshot 一样执行 readback 校验，避免“写了但没真的落盘/字段不完整”仍被当成成功。
- route index 的人工列表字段已明确支持英文逗号、中文逗号、英文分号、中文分号和竖线分隔。
- 已补真实跨天恢复 fixture：前一天 raw event、第二天 hook snapshot、任务目录 STATUS/HANDOFF，验证 `memory-resume` 和 `run()` 自动恢复都能回到任务事实源。
- `memory-resume --until YYYY-MM-DD` 现在按“包含当天全天”处理，避免用户写日期边界时漏掉当天白天的归档记录。
- gateway 请求跨天恢复已接入：LocalStore 命中 gateway_request 后，`memory-resume` / auto resume 会把 gateway request/response JSON 作为事实源推荐阅读。
- gateway 真实后台进程跨天恢复演练已接入 `scenario-test --case gateway-cross-day-resume`，不再只依赖手写 fixture 证明恢复逻辑。
- `memory-resume` 会把已移动的 gateway processing 请求路径纠偏到现存的 done/failed 终态路径，避免恢复提示指向过期临时文件。
- parent/subagent runner 跨天恢复演练已接入 `scenario-test --case parent-subagent-cross-day-resume`：真实 runner 工具回合写回后，`memory-resume` 能回到任务事实源路径。

## 解决的问题

- 把“长期记忆”从纯聊天上下文，拆成可存储、可路由、可诊断、可归档的几个层次。
- `memory_rule_auto_read_limit=0` 表示不自动读取，避免配置为 0 时反而扩大读取范围。
- `--no-save` 不写 raw archive，保留用户显式隐私边界。
- 旧 memory 可以通过 LocalStore 补建索引，减少事实源和搜索索引断裂。
- 小白读者现在能从注释里区分：配置归一化、JSONL 事实流水、LocalStore 索引、route match、required/candidate path 和 read receipt 分别是什么。
- LLM 后续维护时可以更快识别哪些函数会写文件、哪些函数只是纯匹配、哪些函数会原地修改配置对象。
- 新手读者现在能沿着 archive 的 models/storage/runtime/snapshots/query/resume/context/CLI 一路看懂：事件怎么生成、怎么落盘、怎么读回、怎么搜索、怎么恢复。
- raw event readback 解决了“冷归档流水只 append、不验收”的问题；以后恢复线索不会因为半截写入被悄悄放大。
- Markdown route 分隔符兼容解决了“用户手写索引时用了中文标点/竖线，触发词没有被拆开”的问题。
- 跨天恢复测试解决了“只在同一天 happy path 里证明 resume 可用”的问题；现在能证明 archive 线索、LocalStore 索引、subagent 事实源在跨天 handoff 里能合流。
- date-only `until` 修复了解析边界偏机械的问题：用户说“到 2026-04-30”时，系统按 2026-04-30 全天理解。
- gateway 恢复解决了“只能找回 subagent 任务目录，普通 gateway 请求只能看到摘要”的问题；现在会把响应 JSON 带回 Recovery Brief。
- 真实 gateway 演练解决了“恢复测试只覆盖伪造请求，没有覆盖后台进程、request worker、response 落盘”的问题。
- processing 路径纠偏解决了“LocalStore 记录的是处理中文件，但第二天文件已经归档到 done/failed”的恢复断链问题。
- parent/subagent runner 演练解决了“只有手写跨天 fixture，还没证明真实 runner 写回后能被恢复入口找回”的缺口。

## 下一步

- 把更多真实恢复场景写成 fixture：route 冲突、权威文件缺失、snapshot/raw 读回失败、任务目录缺失但 archive 有线索。
- 把 memory 模块接入更多场景测试，验证 gateway、subagent、local-doctor 共同恢复时的数据一致性。
- 扩展 `scripts/check_doc_sync.py` 后续规则时，继续保持 memory 的 `02-progress.md` 和 `04-structure.md` 同步更新。

## 已跑测试

- 历史记录显示 memory 专项测试覆盖 config、routing、routing context、runtime、archive、archive CLI、archive runtime。
- 相关测试入口包括 `agent_py_agent/tests/test_memory_*.py` 和 `agent_py_agent/tests/test_memory_archive*.py`。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。
- 本轮 memory 注释同步 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `50 passed`。
- 本轮 memory 注释同步全量回归：`python -m pytest` -> `241 passed`。
- 本轮 archive/routing 第二刀快速验收：`python -m pytest agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_routing.py` -> `16 passed`。
- 本轮语法验收：`python -m py_compile ...memory_archive... memory_routing\loader.py memory_routing\context.py cli\memory_commands.py cli\memory_archive_commands.py` -> passed。
- 本轮 memory 第二刀 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `67 passed`。
- 本轮同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python -m pytest` -> `243 passed`。
- 本轮跨天恢复 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `14 passed`。
- 本轮跨天恢复 memory focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `69 passed`。
- 本轮跨天恢复同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮跨天恢复全量回归：`python -m pytest` -> `245 passed`。
- gateway fact source focused 验收：`python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `16 passed`。
- gateway fact source 宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_doc_sync.py` -> `72 passed`。
- gateway fact source 同步门验收：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- gateway fact source 全量回归：`python -m pytest` -> `247 passed`。
- 真实 gateway 跨天恢复场景 focused 验收：`python -m pytest agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `16 passed`。
- 真实 gateway 跨天恢复宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `76 passed`。
- 真实 gateway 跨天恢复全量回归：`python -m pytest` -> `250 passed`。
- parent/subagent runner 跨天恢复 focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `2 passed`。
- parent/subagent runner 跨天恢复 CLI 文档验收：`python3 -m pytest agent_py_agent/tests/test_cli_reference.py -q` -> `1 passed`。
- 本轮 focused 组合验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `8 passed`。
- 本轮同步门验收：`python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python3 -m pytest -q` -> `251 passed`。

## 未跑测试

- 暂未做真实跨午夜等待；当前跨天通过固定 archive 时间模拟。
- 暂未用真实外部模型跑 parent/subagent 跨天恢复场景；当前 scenario 使用确定性 backend，但会经过真实 runner、工具和 task fact source 写回路径。

## 风险

- memory 相关设计散在旧 ledger/backlog/test 文档中，第一版四件套还没有搬完全文。
- route index、raw archive、LocalStore、daily memory 同时存在，新手可能混淆“事实源”和“索引/摘要”的区别。
- 后续如果改恢复链路但不更新结构图，会很快重新变成散乱文档。
