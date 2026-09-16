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

- 状态读取：`test_agent_tree_model_view.py`、`test_agent_tree_three_layer_status.py`、`test_orchestration_tools.py`，
  覆盖规范原状态、scope 裁决、恢复路径不外泄、实际报告与缺失报告、八节点直接可读及大树省略计数；
  与 `test_tool_context_reducer.py` 联合核对输出外置后仍保留状态和精确逻辑回读入口。
  真实 TUI 验证运行中查询、完成后交接和真实文件读取，不以最终 DONE 替代工具调用证据。
  无活动任务目录时验证当前会话过滤，显式主请求根验证 parent_id 子树；已有终态报告需实际读取。
- 思考预览：`test_tui_renderer.py` 覆盖流式折叠行数、接收字符数、无换行长段落和窄终端，
  与 `test_thinking_display_boundaries.py`、`test_tui_complete_detail.py` 联测；真实 TUI 需捕获多帧计数增长。

- 派工一致性：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`，覆盖启动/运行/终态混合快照不推导父级动作、角色正文隔离、冻结自定义角色、
  主代理保留自身分工与用户限制；`test_tool_context_reducer.py` 验证精简回执保留唯一动作建议。
  真实 TUI 分开记录父级独立工作、分层是否如实创建、活跃范围是否重复写、确实依赖结果时是否正常等待。

- 子代理交接：`test_subagent_registered_artifact_handoff.py`、`test_subagent_output_alignment.py`，
  覆盖自然/结构化结果、孙级身份、最新文件、删除、日志排除、账本链接拒绝、cwd 与相对/绝对路径一致、
  不从输出声明增权、不从内部同名文件隐式搬运。真实 TUI 另核对创建谱系与完成信封中的实际路径。
- 补丁交接：`test_artifact_registry.py`、`test_tools/test_filesystem_tools.py`，真实 handler 到归档再到自然收口，
  覆盖新增、修改、移动、删除、部分失败、同路径不同历史 artifact_id 与当前删除状态，保留权限和执行事实。

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 父子并行：`test_direct_parent_lifecycle.py`、`test_runtime_guidance.py`、`test_subagent_activity_diagnostics.py`、
  `test_runner_session_pool.py`，覆盖逐个完成、同时释放去重、模型答复/登记等待竞态、忙父级交接、
  慢流不误杀、阶段/审批诊断、旧 attempt、进度快照覆盖、通知重试和心跳回调失败；真实 TUI 组合另列。
- 退出与积压：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`，包含 exact attempt、慢执行存活、
  未知副作用封存、超过分页窗口、消费去重和重启游标；实际模型/故障注入仍需独立 TUI 证据。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 目标：`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、
  `test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_conversation_control.py`、`test_run_audit_terminal.py`。
  覆盖默认工具可见、无工具/无 Todo 的安全续跑、审批/暂停/错误边界、旧绑定显式迁移、
  命名目标的精确回合上下文、前后台共享时钟；普通模式不得因此自动续跑。
  另覆盖每代理一个未结束目标、父子计费与权限隔离、编辑版本冲突、暂停后保存不恢复、
  子 Goal 在同一 attempt 中跨轮与 Compact 续接、独立历史不覆盖；实际草稿键盘操作仍须 TUI 验收。
  当前真实 TUI 已覆盖主子保存、放弃、编辑中停止，以及旧版本冲突保留草稿；详情与未测组合见持续目标设计。
  `test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary` 复现运行中改主目标被子代理误领，
  覆盖主/子消息隔离与提交模型、确认消费完整链路；共享 root task 不能授予父级邮箱。
- 模型：`test_model_provider_management.py`、`test_provider_sampling.py`、`test_model_unconfigured.py`；
  未配置可进设置但不发请求，发布默认值为空，用户显式选择仍保留。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 模型统计：`test_tui_model_metrics.py`，覆盖协议缓存分母、缺报、重放去重、明细裁剪、重试、主子隔离、重连和宽字符窄屏；独立压缩成功/失败均落账，绑定工作片的不重复结算；统计字段不得影响模型上下文。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

默认配置行为必须核对实际合并结果：旧安装若把完整默认 `system_prompt` 或工具延迟目录另存为显式
覆盖，仅升级 wheel 不会替换这些值。测试可在备份后移除测试配置中已确认是旧默认副本的字段，
不能直接覆盖用户定制提示。模型声明、界面 Goal、目标账本、任务绑定和最终工具结果分别取证。

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
