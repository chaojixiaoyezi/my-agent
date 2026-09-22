# 决策模型 P1-A 交接

## 基本信息

- workstream：可选决策模型，完整 P1—P5 Goal 的第一片。
- branch：`codex/decision-model-integration`；独立受管 worktree，原 checkout 未修改。
- owner：决策线主代理实施，两路子代理分别审查配置/消费者与期限/资源。
- date：2026-09-22；集成基线 `4974fe760`。

## 本线目标

让决策服务复用原模型配置与凭据，并保持生成模型的用途边界；全线目标仍包括短期限、开关、故障隔离和业务接入。

## 实际完成

- 原 owner 目录 v3 保存 decision 用途和 TypeSafe 协议；v1/v2 显式只读迁移，管理写入才升级。
- 私有/共享解析显式核对用途，主/子生成入口拒绝决策模型；同名决策配置不干扰生成模型选择。
- 公开 `available` 保持聊天语义，`available_for` 明示可用用途，密钥留在唯一 provider 文件。
- 原服务商表单保存/编辑完整用途，模型表单保留决策协议并排除生成采样参数；保存不触发网络。
- 复核修复 v1 迁移丢自定义请求头及会话头，未迁入外部 harness 或增加依赖。

## 改动文件

- `agent/settings/model_provider_schema.py`、`model_provider_operations.py`、`model_profiles.py`、`shared_model_catalog.py`。
- `cli/chat_parts/tui_provider_menu.py`、`tui_model_menu.py`，仅模型管理表单范围。
- `tests/test_decision_model_profiles.py`、`test_model_provider_management.py`；上述路径均在 `agent_py_agent/`。
- Goal、设计、开发入口、树、路线、已实现与测试文档；尺寸报告由原脚本更新。

## 测试命令和结果

`python3 -m pytest` 对 TESTS.md 首节列出的 10 个文件联合执行，附 `-o addopts='' -q --tb=short`：
**213 passed in 4.29s**。包含原菜单按键回归及新增真实表单控件/原保存服务的组合测试。
Ruff、strict code-size、文档同步及 diff 检查均通过，尺寸 baseline 未更改；独立复核的两项问题已闭环。
无真实模型、线上 CI、部署或 Gateway 重启，组件通过不代表实际 TUI 的 Jev 判断可用。

## 影响范围与重点复查

旧版本程序不支持 v3；正式部署前要保留原私有模型配置备份，不得把迁移后数据交给旧版本覆盖。
decision 配置当前可管理但尚不能发请求；普通 probe 会拒绝该用途，连接测试须随独立 decide 接线完成。
普通 agentic/embedding 的协议、权限、共享引用、冻结快照和 OAuth 流程保持原链。

## 需要其他线协调

插件线已确认本片及后续后端/取消文件无重叠；公共 TUI、slash、插件命令和 stream 文件均未修改。
共享文档集成按精确段落合并，不能覆盖对方的新进度。没有安排部署或重启。

## 剩余风险与建议下一步

P1-B—G、P2—P5 均未完成。继续实现有界调用与共用设置服务，再接业务点；不能只加线程等待超时。
期限审查发现两层 HTTP 重试、短超时下限、错误正文阻塞、取消唤醒与残留 worker 资源边界，详见总 Goal 记录。
协议/设置可以并行，公共 HTTP、interrupt 和原账本由单一实施者收口；必须保留主模型资源和普通 SSE 语义。
