# 测试与发布验收

## 原则

开发反馈优先定向合同、工具替身、模型替身和脱敏回放；真实 TUI 是最终验收最低要求。测试任务由被测代理完成，测试者不能代写产物后计为通过。

普通需求用自然中文表达。权限、参数、隔离、状态和恢复由底座控制，不靠在提示词里写特殊限制规避缺陷。详见 [测试分层](docs/design/main-agent-contract-testing.md) 与 [测试清单](TEST_CHECKLIST.md)。

## 必测模块

| 模块 | 验证要点 |
|---|---|
| 配置与模型 | 服务商协议、密钥引用、上下文容量、会话选择、用户默认、子代理继承与显式覆盖 |
| 身份与工作区 | 多用户同 Gateway、家目录隔离、管理员显式越界、工具权限与真实路径 |
| 主子代理 | 创建、插话、停止、恢复、换代、结果落账、父级唤醒、重复及乱序事件 |
| 历史与压缩 | 未压缩历史完整性、Unicode JSONL、展示分页、长输出引用、压缩计数、模型切换 |
| 工具 | 参数校验、成功/失败状态、文件读写、搜索、补丁、命令/PTY、网络、MCP |
| 记忆与技能 | owner 隔离、自主记忆维护、人格确认、索引发现与按需读取；技能选代表场景 |
| TUI | 输入回显、换行、粘贴、滚轮、复制、完整展开、到底部、主子代理视角、Todo、活动状态 |
| 调度与交付 | 普通回合与目标模式、挂起唤醒、断线、后台交付和恢复；IM 无环境时标明未测 |

## 重点定向回归入口

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 目标：`test_conversation_goal_tools.py`。
- 模型：`test_model_provider_management.py`、`test_provider_sampling.py`。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

## 提交前严格 gate

```bash
python3 -m pytest <直接相关测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

默认 focused tests。生产代码与测试代码累计增删约 10,000 行或明确另有要求时追加全仓 pytest；文档清理不算实施代码变动。线上 CI 未运行时如实说明，不替代本地严格 gate。

## 发布资料清理验证

注释与示例清理要比较生产 Python AST、默认配置值、协议与依赖标识。允许的人类展示字符串变化需单列；构建包检查 LICENSE/NOTICE、vendor 许可和不含秘密数据。历史重写须先备份、只改授权引用、带 lease 更新，验证发布树不变。
