# P4-B 普通中文修改决策设置：单次真实验收交接

## 结论与范围

2026-09-23 UTC 在私有隔离 home 中启动唯一 8431 Gateway，并向真实 TUI 主模型只发一次普通中文需求：

> 把这个会话的决策模型总开关保持开启，把通用等待上限设成6秒，并把子代理选模型改为只观察。请保存后读回实际生效值。

**本次未通过。**主模型没有调用 `user_config`；它搜索了用户工作区，最后说找不到相关配置并要求用户说明位置。设置未改变。失败首先发生在工具可见性：该 request 的原 `context_bundle.tool_manifest` 中没有 `user_config`，不是一个已展示工具的 schema/调用失败，也不能归因于主模型忽略已展示的工具。

本轮未手动填写工具参数、未替被测对象修改设置或补产物；未修改生产代码、共享文档、日常服务。原始请求、响应、chunk、上下文 bundle 和用量账保留在私有隔离 home。

## 可复核的原始证据

- 私有基线：仓外私有测试目录的 `p4b-natural/20260923T062632Z/baseline.json`。只记录脱敏设置版本、来源、官方模型 profile ID/API base、Gateway 与 TUI 身份；不含密钥。
- 唯一请求 ID：`gwreq-1790144867-4726bcb1185343b8872d5976ef52dbe1`；会话 `sess_1790144740_88a1ebd9`；线程 `thread-2a4cdc132b414c43`。原 request 在 `.../owners/local/main/workspace/runtime/services/gateway/requests/done/<request-id>.json`，原 response 在同级服务目录的 `responses/<request-id>.json`，工具流在 `requests/done/<request-id>.chunks.jsonl`。
- 权威工具快照在 `.../owners/providers/local/users/decision-live-validation/memory_archive/snapshots/context_bundles/2026-09-22/<request-id>.json` 的 `tool_manifest`。`owner_type=user`，`permission_mode=owner_scoped`，`snapshot_hash=sha256:5f837d5b952a5b29096e55f5df8a3cc1052ed7d338828ee31defb716967780fa`；31 个 `visible_tools` 与 31 个 `executable_tools` 均不含 `user_config`，`tool_load_errors=[]`。工具并非被 Jev/skill_tool 推荐后临时收起：注册后的原 manifest 已无此工具。
- `chunks.jsonl` 中 18 轮工具调用的 started 记录：`list_files` 7 次、`search_text` 6 次、`read_file` 14 次、`run_command` 5 次、`find_files` 2 次；`tool_search` 0 次，`user_config` 0 次。原 response 是“未能找到决策模型配置相关的文件”，并请求用户指出位置。Gateway 请求 `status=done, ok=true` 只表示模型正常结束，不表示配置任务成功。
- 同线程原 `conversations/model_usage/thread-2a4cdc132b414c43.jsonl` 只有该 request 的一条统计：`main` 为官方 MiniMax-M2.7 / `anthropic_compatible` 19 次 provider HTTP 尝试、19 次成功；`decision` 为 `jev-latest` / `typesafe_decision` 1 次尝试、1 次成功。response 总计 20 次模型尝试、0 retry、87.298 秒。决策输入 17,386 token，主模型输入 627,459 token；这是同一原账分区，不能把总 20 次再加一次 Jev。基线所选 MiniMax-M2.7 profile 的 API base 是 `https://api.minimaxi.com/anthropic`；本次未保存逐个 HTTP 请求的 URL trace，因此 endpoint 的证明止于有效 profile 配置及原模型账，不称逐请求抓包证据。
- 被测 user owner profile 的 `decision_settings.revision` 前后均为 11，整个 profile SHA256 与基线相同；当前线程 `decision_settings.revision=0, overrides={}`。`baseline.json` 中的 `owner_profile_sha256` 指此 user owner：其原身份三元组是 `local/user/providers/local/users/decision-live-validation`，不能把路径位于全局 `config/model-profiles` 误认为本机 main owner 配置。所以没有要恢复的临时 thread 覆盖。TUI 已退出；8431 Gateway PID 1090 已由原 CLI `gateway stop --timeout 20` 停止，8431 无监听；日常 8420 仍由原 PID 2543 监听。

## 根因及版本边界

`agent_py_agent/agent/core.py::_tool_registry_owner_type` 按已解析 owner 身份将普通本地 user 归为 `user`；`_register_orchestration_tools` 只在 `owner_type == "main_agent"` 时注册 `UserConfigTool`。本次 TUI 请求的 `canonical_user_id=decision-live-validation`，其工具快照正好是 `owner_type=user`，与这一注册分支一致。`user_config` 是注册阶段缺席，后面的 `tool_search`、模型 guidance 或 Jev 不能使其出现。

请求创建于 2026-09-23 06:27:47 UTC；同一请求实际工具快照 hash 是以上可复核的权威版本。收集证据时的 `core.py` SHA256 为 `7ad45c4a9627f68bd3d3f7ec5850f470ad674ea44b384b408018adb653b38d72`（mtime 2026-09-22 11:29 UTC），`tooling/user_config_tool.py` 为 `ab66f9de1004dd0c145cade3152be9c85765a83c1c2b4645e5fe551b68983b58`（mtime 2026-09-23 06:23 UTC），`settings/decision_settings_schema.py` 为 `a6c0aadf0e09411c5a8f9df3c39cadcf6b56551ac7efa471b6a7af51e2cc44fe`（mtime 06:14 UTC）。共享 worktree 同时有其他 agent 工作；这些文件哈希是证据收集时读到的内容，不能用它们声称整个运行环境是一份静态提交。

**修复边界：不能把完整 `UserConfigTool` 简单注册给普通 user owner。**其 legacy `view/set` 路径读取进程级 `MY_AGENT_CONFIG`，可能跨 owner；普通 user 只能得到沿原 owner/thread/CAS 合同的 decision-only 操作和对应模型 schema，execute 也必须拒绝 legacy 操作。main agent 继续保留完整原工具。具体实现由主线与正在改 `user_config_tool.py` 的 E1 协调后精确认领；本片只做真实验收与定位，不抢写生产文件。

## 建议下一步

先由一个 owner 在原 `user_config` 服务上做普通 user 的 decision-only 可见性/执行权限首片，保持 main agent legacy 操作和 owner 隔离不变；E1 释放文件后再改。定向 fake 测试应覆盖 user/main 两类工具 snapshot、普通 user legacy `view/set` 拒绝、decision 读写 CAS 与跨 owner 拒绝。随后在隔离 8431 以**新会话、一次普通中文需求**重新做真实验收，检查模型实际调用 `user_config`、read→patch→read、有效值及原设置恢复。此工作可与其他不触碰 `core.py`/`user_config_tool.py` 的模块并行，真实 Gateway 仍须唯一实例且不得碰日常 8420。
