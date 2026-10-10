# 长任务运行时 L 线交接

状态：实现候选已完成组合回归，测试机真实 TUI 验收进行中；未发布生产、未推送远端。

## 基本信息

- workstream：L，长任务、模型选择、Goal 事实与持久调度。
- branch：`codex/long-task-runtime-l`。
- 基线：`c52257ef52a05dd24ed99dbba133bf58b2b7d7dd`，包含原部署源 `0b938e57ef4c28e4f262ee494353a2930b6cde0d`。
- 日期：2026-10-10。
- 唯一设计入口：[长任务报告](../design/LONG_TASK_RUNTIME_REVIEW.md)。
- T 来源：共同基线上导出的已暂存代码补丁，SHA256 `54b42fb212ba0dd1a151c650ef3cb4ec59296aaa6cf68c7acda841c8f314631a`；29 个文件。是固定源码快照导入，不声称已合并一个不存在的 T 新提交。
- T 原交接：[TUI 控制交接](TUI_STEER_CONTROL_HANDOFF.md)。

## 本线目标与实际完成

1. O1：Goal、schedule、Gateway 状态提供纯元数据读取；错误和嵌套结果也经过白名单。没有读取原业务会话正文。
2. M1/M2：显式选模在下一次合法模型请求前采用，旧响应、工具和同次请求重试正常收尾。绑定 backend、配置、prompt、上下文容量、协议历史和用量来源；状态区分选择、绑定、活动阶段与实际请求。
3. G1/G2：原 Goal 文件同事务保存变更来源、revision、实际调用身份、验证引用及完整进度计数；用户扩大目标后，旧模型 complete 不得完成新 revision。完整计数和有界 ID 分开，质量不增加硬门。
4. S1：原会话跟进或独立上下文，继承来源选模或固定档案；独立线程复用原 run/TaskLink。冻结来源绑定，交付重试只发原冻结结果，不重新调用模型。
5. S2：显式错过策略、未开始队列期限、连续失败暂停和未来周期退避；`started_at` 在真正取得后台执行车道后写入。
6. S3：原后台 claim 内执行只读预检，同一权限、工具归档和事件账。完整输出用于比较；无变化不调用模型，失败不伪装无变化，checkpoint 仅随动作及交付 done 提交。
7. T 集成：原 claim/attempt 执行身份、停止、输入回执顺序和恢复候选；预检真实 attempt 也发布到同一个原 claim，不建立额外控制记录。

## 关键代码地点

| 入口 | 职责 |
| --- | --- |
| `agent_core/model/selection_refresh.py`、`selection_history.py` | 请求边界刷新与跨协议历史处理 |
| `settings/model_scope.py`、`model_selection_status.py` | 依赖绑定、实际请求和活动状态投影 |
| `conversation/goal_transition.py`、`agent_goals.py` | 原 Goal 事务来源与进度快照 |
| `scheduler/execution_policy.py`、`execution.py` | 独立执行、模型来源和交付绑定 |
| `scheduler/policy.py`、`repository.py`、`service.py` | 错过、队列、失败策略及领取/终态事务 |
| `scheduler/precheck.py`、`run_preparation.py` | 原 run 观察、恢复及 checkpoint |
| `agent_core/tool_only_execution.py`、`tool_call_runtime.py` | 宿主只读工作片，复用原执行、归档、事件与取消 |
| `gateway_parts/execution_target.py`、`execution_control.py` | T 真实执行定位及精确控制 |
| `conversation/claim_execution_binding.py` | T 后台 claim 与实际 attempt 的绑定 |

## 开发验证

组合命令：在仓库根用项目 Python 执行 `python -m pytest <final-focused-files.json 中 53 个文件> -q -o addopts= --tb=short --junitxml=final-focused.xml`。

结果：1190 passed。清单包含 L/T 直接改动测试、原 Goal/模型/调度/后台恢复，以及共享工具权限与归档测试。首次验收时并行运行尺寸检查改写了 `CODE_SIZE_REPORT.md`，触发测试防写检查；串行同组重跑已通过。后续尺寸检查使用 `--report` 指向临时目录。

后续第三候选六文件 98 项通过；第四候选目录修复六文件 91 项通过。真实 read_file 用例在第三候选红、修复后绿，预检采用原后台 cwd 装配，未放宽权限。

严格 gate 包含 Ruff、doc sync、strict code-size、diff check 和 clean-package。最终状态与安装包清单在真实验收结束时补齐；线上 CI 不作为本轮验收来源。

## 真实环境与证据边界

测试机在原约定的隔离 home/venv/workspace 内运行单 Gateway，多原生 TUI 共用。只使用官方 MiniMax-M2.7，使用不同窗口和采样参数的测试档案验证采用链。没有改日常启动器或生产配置。一次 pytest 外调试误建默认 owner 合成计划，已精确取消并删除，详情见报告；不能声称默认目录从未被写入。

首包 SHA256 `9dab4777b1274dbac79ca35798673e8e1547938c9b976f5ced9e295a4f1bb978` 只包含早期 M1/G1/M2。10 条核对覆盖完整但两项错误，内容验收失败；100/300 条正确率均为 100%。300 条工作中原生 TUI 选模约 4.28 秒后采用。

MiniQL 一次初始需求覆盖 50 类语法、600 个公开检查；测试者不补源码或任务产物。已跨多次自然 Compact，Compact 时切档案约 27.12 秒采用，原有孩子保持选择。运行超过一小时时出现主响应重复文字，同时子代理继续运行；这项观察不等于 Goal 停止，也不当作持续有效进展证明。三小时及最终正确率待核实。

新版候选需单独验证 S1–S3 和 T 组合。升级前后版本、操作者控制操作、客户端重连、实际出站参数与 Goal 状态分别记录，混合版本时段不得宣称为同一版本三小时验收。

## 影响范围与复查重点

- 默认调度行为保持 followup/inherit、missed skip、预检关闭；新能力每 job 显式启用。独立上下文不等于文件系统隔离。
- 固定模型锁定档案引用，不复制凭据；档案修改后每片解析最新有效定义。删除/撤销明确报错，不自动回默认模型。
- 预检使用完整工具输出，不使用展示预览；只读身份、归档失败、停止和恢复须联合复查。
- 原模型调用账的时间为 monotonic，选模与采用为 Unix；投影已标注时钟，不能跨时钟直接求延迟。
- `complete` 是模型声明，宿主验证未知时明确保留未知；进度完整也不保证内容正确。
- T22/T23、真实 IM 等原 T 待验项保持待验，不因源码导入自动关闭。
- 当前基线 Skill 学习已排除后台来源；新版 AGENTS 学习来源合同尚未集成，S11 不宣称完整通过。公共 drain 接口按原负责线继续，不另造第二实现。

## 建议下一步

先完成新版测试包的真实调度、恢复、模型出站参数与长任务验收，再交给独立 agent 按版本和证据复审。文档与离线审查可并行；测试 Gateway 部署和共享 runtime 修改保持单一执行者，避免改动正在工作的生产 agent。
