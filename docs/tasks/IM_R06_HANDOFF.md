# R06 模型控制卡交接

## 基本信息

- workstream：IM R06；共享 checkout，逐文件独占。
- branch：`codex/im-entry-update-assessment`。
- 日期：2026-10-14。
- 状态：本地实现与定向验证；未提交、未推送、未部署或外发真实飞书。

## 目标与完成

裸 `/model` 在飞书返回原生模型卡，区分当前会话和未来默认；新增、编辑仅向原操作者私聊发后端/地址/模型名/上下文/遮罩密钥表单。群回调继续写原群 owner/thread；签名/密码/真实卡身份/目录及线程版本共同核验，去重结果只保存无秘密回执。复用原模型管理、线程 CAS、原目录写前检查、原向量端点保护和唯一 Delivery；不创建任务、持久输入依赖或无限审批等待。

## 改动文件

- `agent_py_agent/agent/gateway_parts/model_control_cards.py`：卡、签名控制元数据、冷 owner 宿主、精确提交。
- `agent_py_agent/agent/gateway_parts/model_profile_service.py`：裸 IM `/model` 接卡与使用说明。
- `agent_py_agent/agent/gateway_parts/private_interaction_service.py`：严格新增 `model_configuration` 类型，在 Agent 初始化之前沿原签名 owner 的轻量 store 执行。
- `agent_py_agent/agent/adapter/private_controls.py`：模型字段仅转交宿主认证回环，不写聊天。
- `agent_py_agent/agent/adapter/feishu.py`：控制卡发消息复用原 `_message_send_path`，允许准确群 `oc_` 和个人 `ou_`。
- `agent_py_agent/agent/settings/model_profiles.py`：select 转交 `expected_revision`；原操作合同说明。
- `agent_py_agent/agent/settings/thread_model_selection.py`：原线程原子事务内部 optional `expected_revision` CAS。
- `agent_py_agent/agent/settings/model_provider_operations.py`：`save_profile` 显式 editing，独立连接原子编辑，模型/服务商 ID 保持，共用连接拒绝。
- `agent_py_agent/tests/test_im_model_cards.py`：16 项原路由与安全验证。
- `docs/design/IM_MODEL_CONTROL_CARDS.md`：合同、路径、组件依据和验收边界。

## 配置与根协调

根负责 capability 默认配置/dataclass 的 `im_model_cards_enabled=true` 和部署写入保护。读取点在 `model_profile_service.execute_model_text_control` 与 `private_interaction_service.resolve_private_interaction`，均调用原 `capability_config_for_agent(base_agent)`；没有新主配置项。根负责 DESIGN_LEDGER/ROADMAP/TESTS/CODEBASE_TREE 文档入口同步；本线未修改这些共享入口。

准确秘密位置继续是 `HomePaths.config_dir/model-profiles/<owner身份摘要>.json`，不是 `owner_home` 的业务工作区。原 H3 与 owner 隔离及 shell sandbox 必须继续保护这一中央目录；新测试已拿真实生成的个人/群 catalog，分别由原 `read_file` 和原真实 shell 路由拒读。无新秘密仓库。管理员 full shell 的既有边界仍属于根 R09 审计。

## 验证

使用项目既有 `.venv/bin/python` 与 `.venv/bin/ruff`；16 项新卡测试通过。原 model_text_control、model_provider_management、thread_model_selection_revision、task_credentials、im_tool_approval、feishu_transport_security、gateway_model_profiles 与新卡测试的最终联合验证为 120 passed；同时有 1 个 teardown error：运输安全测试的 tracked-file fixture 捕获共享 CODE_SIZE_REPORT.md 被并行改写。本线 code-size 命令显式使用 /tmp report，没有覆写该文件；已报告根，不把本次命令称全绿。此前同组 118 项在增加两个真实 owner catalog 隔离用例前通过；污染用例单独重跑为 1 passed in 1.89s，证明它的原断言在无并行报告写入时通过。

R06 改动文件 Ruff 通过，`git diff --check` 通过。全仓 strict code-size 使用独立 `/tmp` 报告避免覆盖根文件，发现 scheduler/tool 的 `build_schedule_tool_model_spec` 长函数及 scheduler/repository 的 `record_cancel_cleanup` 深嵌套两个阻断；已通知根/监控线。R06 没有新增 hard 阻断。根仍需全组合严格 gate，不能以本线绿代替总验收。

## 边界与审阅重点

- 官方输入/表单/示例已核对，使用既有 native password/form_submit；官方组件页面正文抓取受限，真实飞书 UI 遮罩、渲染、送达、SDK 回调期限仍需最终租户验收。
- 打开/保存不网络探测，不声称连接成功。按根决定不提供“测试连接”按钮：原 TUI probe 的 60 秒调用和网络门不应接入 2 秒卡回调，未引入第二模型传输。
- OAuth 或多模型共用 provider 的编辑保留原终端入口，避免单模型表单改其它认证。共享档案只提供选择，不在卡片暴露/编辑管理员认证；共享发布、全局初始/向量操作仍走原管理员入口。
- 选择同时持原 catalog 锁再线程 CAS；默认设置/表单提交的写前检查在原 catalog 锁内重核。已完成同动作回调返回原回执，不重复保存或发卡。

## 建议下一步

根先审阅类型、原群归属、目录保护和版本锁序，再完成总 focused/严格 gate。其它 agent 可并行只读核验飞书组件与卡片回调运输，不能同时修改本线文件。真实验收只给被测对象一次需求并观察原卡/记录，不能代写目录或补卡；通过后由根独占发布生命周期。


## 非作者审查修复追加

2026-10-14 已修复 completed 仅按 decision 返回旧成功的问题：完整 decision/fields 规范 JSON 经原 owner 交互保护 key、独立用途和 control UUID 做 HMAC 摘要。完全相同载荷重放原回执，异值 select/default/save/额外字段拒绝；不保存明文 API key、表单或裸秘密摘要。旧 completed 缺摘要拒绝重放，不隐式猜旧请求。卡片与私人表单新增结构 OwnerIdentity 来源的群/个人/管理员作用范围，群新会话默认明确属于原群，私人投递不改变配置归属。

新增 11 项回归（两种选择异值、五个保存字段异值/密钥不落控制记录、三种 owner 卡片与表单 scope、群未来默认不写个人目录），末次卡测试为 27 passed in 1.52s。相邻组合为 129 passed/1 failed：既有飞书注册鉴权重定向测试在拒绝 302 后读取响应体时 ConnectionResetError，独立复跑仍为 1 failed in 2.11s。已通知根检查原注册错误响应读取，不将该组合称全绿。改动仍仅本线 model_control_cards、test_im_model_cards、模块设计和本交接；R05及根 R04/R09 文件保持冻结。

建议下一步：根复审 HMAC 载荷与原 owner scope 后接总 gate；可并行只读复核，不能同时写本线文件。
