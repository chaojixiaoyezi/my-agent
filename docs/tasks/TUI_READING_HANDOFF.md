# TUI 阅读与插话修复交接

## 基本信息

- 日期：2026-09-23。
- 基线：`f04ec3a42896aadfc3b1445c7b21e8da8e34d43c`，原 checkout 的 `codex/refactor-runtime-composition`，本线单独本地提交，提交编号以交接消息为准；未推送。
- 所有权：已与「模块重构」「接入决策模型」协调；两线在独立 worktree 工作，本线负责 TUI/history 与 `goal_execution_scope`。
- 公共状态、测试总入口和重构台账由「模块重构」维护，本线提交精确证据供其合并。

## 本线目标与实际完成

1. 插话按提交边界切开前后回答；精确输入 ID 用于现场/重连去重，消费回执不重切正在生成的回答。
2. 显示检查点保留插话位置；历史投影按 canonical 行序恢复，后台 native 与正式投递按精确回合 ID 合并。
3. Ctrl+O/Ctrl+E 保存当前消息位置；归档异步就绪不推走锚点，超长换行文本保持同宽页内位置。
4. 上下键与滚轮直接移动可见画面；有界原文窗口连续接续，短页组合不再卡在缓存窗口底部。
5. Goal scope 公开自动续轮与无需新用户消息的结构化事实，沿用原持久 wake/任务状态链，不增加第二套循环。

## 改动范围

- TUI：`tui_reading.py`、`tui_view.py`、`tui_transcript.py`、`tui_complete_detail.py`、`tui_block_renderer.py`、`tui_keybindings.py`、`tui_runtime.py`、`tui_view_model.py`。
- conversation：`history_order.py`、`history_display.py`、`background_transcript.py`、`background_history.py`、`display_checkpoint.py`、`goal_prompting.py`。
- 相应回归、完整原文/Goal 设计文档、设计台账与文件树。
- 参考范围：实际检查 Codex TUI 的 pager overlay 直接移动 scroll offset 的实现，以及本仓库既有显示块 ID、历史分页和归档合同；不宣称完成全部参考仓库逐行审阅。

## 本地验收

15 个相关测试文件共 **381 passed**；覆盖 TUI reading/detail/view/transcript/history/input/threading/runtime/reducer、conversation history/checkpoint/background snapshot 与 Goal 工具。

```bash
python3 -m pytest agent_py_agent/tests/test_tui_{reading_position,complete_detail,view,transcript,history_paging,input,injected_input_states,threading,runtime,view_model}.py agent_py_agent/tests/test_{conversation_history_display,conversation_display_checkpoint,background_history_snapshot,agent_goals,conversation_goal_tools}.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

wheel 另做 clean-package artifact 与 distribution boundary，要求 forbidden/source-missing/source-mismatched/resource-missing 均为 0。
全仓 pytest 与线上 CI 未作为本轮验收来源。

## 真实 TUI 证据

- 测试机：用户授权的空闲测试机（私有证据编号 TUI-A），一个 Gateway、独立测试 home；与机上其它应用隔离。
- 模型：官方 `MiniMax-M2.7`，端点 `https://api.minimax.cn/anthropic/v1`；密钥仅在私有测试配置中，未改日用模型。
- 完整交互矩阵验收 wheel SHA-256：`218fe2bc4b52875b3cfbcd240a14ec0a9bb32d3f3f3f3b74e35b811e1ecda94d`。
- 最终交接 wheel SHA-256：`b2bd4258b66574e9d3d756b73c16939351296d6e84e4d7de1c912a2db649d330`；仅追加模块注释同步，运行实现与上述验收包相同，重启后另补新 TUI 真实模型调用。
- TUI-A：同一会话跨候选版本重连，精确会话和线程编号保留在仓库外验收账。
- 运行中插话：TUI-A-STEER，提交前精确 claim 为 running；现场顺序为工具、用户补充、后续思考、回应补充，恢复后用户输入仅一份。
- 真实后台命令退出后自动追加结果；修复后 history projection 中该后台回复仅一份，按回合 ID 验证，未做正文相似度去重。
- 原文通过原生 tmux 键盘与鼠标 SGR 事件验收；截图和终端字节记录在仓库外按版本保留，失败候选没有被计作最终通过。
- 最终版从第 1 段向下到第 34 段，共 159 次画面采样；未按 `[`、`]`，段号无反向跳动。
- 同样用 159 次采样从第 34 段向上回到第 1 段，段号无反向跳动；全程以普通滚动接续，没有使用手动翻段。
- 最终版 8 项实测断言均为 true：普通/详细同位置、完整展开同位置、Down 一行、Up 还原、鼠标 SGR 滚轮一行、84 列保留消息、恢复 120 列保留消息、收起保留消息。
- 独立短 Goal：TUI-B。单次普通中文需求后，真实等待 45 秒并读取第 600 条，得到编号 0600、数值 4200，再置 complete；模型正确解释无需用户再次发消息。
- 最终候选重启后另开新 TUI，再由官方模型完成 `37 × 19 = 703`；启动 banner 与真实模型响应同时保存，未用旧窗口证明新 Gateway 就绪。

原始环境身份、运行编号与终端日志只保存在私有测试证据目录；以 `acceptance-meta.json`、`v4-ui-checks.json`、`v4-history-checks.json` 与 `final-source-manifest.json` 对应版本和结果，实际位置由交接消息提供，不进入仓库。

## 失败样本与边界

- 第一轮插话在 claim 已结束后才提交，只计普通追问，不计运行中插话通过；第二轮重新取证。
- 前序候选发现邻页加载推走锚点、长换行回跳与短页接续卡住；均先补失败测试，再修复并更换候选包。
- 已被旧记录或执行端截掉且没有原文归档的内容无法补造；冻结阅读只包含入场时已收到的公开内容。
- 本轮验证的是 root TUI 与已有历史恢复；不把它扩展为全部子/孙代理、IM 或 100 小时稳定性结论。
- Goal 机制提示不替代运行事实，也不保证模型永远不会过早标记完成；本轮没有重写目标调度策略。
- 严格本地 gate、导入边界和 wheel 边界均通过；未执行远端提交或使用线上 CI 作为通过依据。

## 建议下一步

先由主线汇总本线与其它 agent 的已验证提交，再统一打包部署；当前用户本机的长任务仍使用旧进程，本轮没有重启它。
其它 agent 可继续在独立 worktree 并行，集成时重点核对 history/Goal 事实字段与 TUI 显示，不混入彼此暂存区或私有测试配置。
