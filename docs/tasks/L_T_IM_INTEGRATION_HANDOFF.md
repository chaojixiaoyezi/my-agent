# 长任务 L 分支与当前 TUI/IM 临时版本整合

状态：2026-10-12，整合回归与 Linux smoke 已完成；用户随后明确授权合入 main、推送远端并打包。本次发布按下方追加记录执行。其余段落的“未发布”是之前验收时点，不能作为当前 Git 或安装包状态。线上运行安装不随源码推送而自动切换。

## 来源和当前边界

- 来源分支 `codex/long-task-runtime-l`，锁定头 `51767a3c6c578e9740ab129d588c2cc75d57e3d4`，共 8 个相对共同基线的提交。
- 共同基线 `c52257ef52a05dd24ed99dbba133bf58b2b7d7dd`。来源产品代码头为 `134f43c3a`，其后两次提交为验收文档。
- 接收工作树为当前 `codex/tui-steer-control-20261009` 的 TUI/IM 预览、当天委派和公共包版本；保留原 staged/unstaged 内容。
- 接收前创建本地不可变快照 `5b52b3726dd53054d8cd0ee433ab1cfb79587fa6`，回退引用 `codex/tui-pre-l-integration-20261012`。使用真实三方 merge-tree，不直接覆盖文件。
- 整合结果保存到独立本地分支 `codex/tui-l-im-integrated-20261012`，以原工作快照和 L 源分支为双父提交；原接收分支 HEAD 不移动，保留原暂存布局。来源分支和工作树保持不变。新文件只 intent-to-add，原已暂存 diff 的 SHA256 与整合前相同。
- 检查对象是源码、开发交接和合成测试，不读取正在执行的原业务会话正文。

## 对方已经实现的改动

1. 运行中显式选模在下一合法请求前采用；已经开始的响应、工具及同次重试保持原依赖。区分选择、采用、实际请求。
2. Goal 原事务增加来源、revision、执行身份、验证引用和进度计数；旧目标 revision 的 complete 不能结束用户扩大后的新目标。
3. Goal、schedule、Gateway 提供纯元数据查询模式，错误和嵌套状态也不回显正文。
4. 定时任务增加 followup/isolated、inherit/fixed、skip/coalesce、排队期限、失败退避/暂停和只读预检。只有实际获得后台车道才开始计时。
5. 无变化的只读预检不调用模型；实际工作完成才提交 checkpoint。等待孩子及最终投递结束后再结束原 scheduler run。
6. 沿原持久任务恢复定时子代理来源，补原停止 Goal 的 user_control 来源；导入的早期 T 插话快照与当前共有部分去重。

入口：`agent_core/model/selection_refresh.py`、`selection_history.py`、`conversation/goal_transition.py`、`scheduler/execution.py`、`execution_policy.py`、`policy.py`、`precheck.py`、`run_preparation.py`、`active_run_closeout.py`。

来源六小时合成测试证明持续运行/自动续接，**没有证明完整交付**。保留检查 1197/1200、自写单测 40/48、README 示例失败；完成回执和已知失败披露、Compact 退化仍按来源报告开放。这轮没有声称修好这些质量问题，也没有新增任务专项硬门。

## 组合处的实际修正

- 合并 `scheduler/execution_policy.py` 的 add/add：原 IM 固定后台归属与 L 的显式逐次上下文/模型/预检规范共用同一模块。
- 保持当前新 IM 计划的独立执行线程；TUI 默认沿原会话，显式 isolated 再为每次运行建独立上下文。所有线程仍归原 ThreadStore。
- scheduler run 的模型继承来源指向原布置聊天，而不是中间后台线程；fixed 按原固定引用，不复制密钥或绕过撤销。
- 显式 isolated 的新 IM run 读取原计划冻结的布置者绑定；群内后来发送者不替换结果收件人。投递前仍检查原索引/来源有效性，不增加地址兜底。
- 委派续片的原 SessionTask 引用、容量让出、子代理权限裁决、停止与父模型汇总保留；新增 durable scheduler 来源解析不吞掉普通缺失输入。
- 创建 IM 执行线程前先用原纯合同验证 execution/policy/precheck；非法配置不留下空计划线程。
- schedule metadata_only 继续隐藏名称/正文/自由错误；执行位置额外投影为 background/current，不把展示字段当执行权。
- 所有原文档历史保留，两条线的新状态分别保留；此报告为此次整合证据导航。

## 验证与证据

- 第一组六文件：69 passed。
- 新组合文件 `tests/test_scheduler_im_l_integration.py`：5 passed。含 IM 继承来源模型、群发送者变化后的 frozen 通知，以及三类非法完整配置不新建后台线程。
- 显式独立运行的冻结地址用例曾实际失败：模型已执行，但后来发送者对应的来源索引不满足交付验证，未投递。修正原计划来源桥接后通过，未放宽原索引校验。
- 两边 focused 列表及此次变更测试去重后 79 文件，**1801 passed，1 个原有 ctypes deprecation 警告，无失败/跳过**；不重复跑全仓 pytest，此轮新增/删除生产及测试实现约 3.2K 行，低于全仓阈值。
- Ruff、doc sync、strict size（hard=0、blocked=False）、diff check、clean-package 均通过；检查顺序与 pytest 串开，避免报告写入触发测试保护。
- Linux 组合 smoke 通过：独立 home、单 Gateway、官方 MiniMax-M2.7、两个真实孩子 DONE 与原 SessionTask done、公共包任务独立汇总、短聊天先结束；真实 SSH TUI 新建计划沿原线程，IM 新建计划保持后台。原 shell 工具执行也实际成功。出站使用模拟飞书 adapter，不能称真实飞书实发。
- 测试进程及原生 TUI 已退出，8420 已释放，临时 AppArmor profile 已卸载删除，系统 userns 限制仍为 1。
- 原 L 分支的六小时证据是来源证据，不能冒充此组合版本重新跑过六小时。

## 建议下一步

按用户已授权的发布步骤完成严格 gate、main 推送和离线包验收。其他 agent 可并行只读审阅切模/Goal/调度合同；同一发布写入口和生产 Gateway 仍保持单一负责人。原长测交付质量继续单独处理，服务器安装由用户安排。


## 主线发布追加（2026-10-12）

- 用户明确授权合入 main、提交远端并打包；待发布版本为 `0.3.0-tui-preview.20261012.6`。
- 发布候选位于独立 `codex/main-release-20261012`；核对远端 `8ade40d6f` 是整合提交 `ec12ccf7` 的祖先，不需再次冲突合并。主工作区及原 T 工作树的未提交改动保留。
- 远端 main 相对本次整合涉及生产/测试 Python 增删 17,906 行，达到仓库全量阈值；除联合 focused 列表与发行测试外，追加全仓 pytest。静态门、尺寸、干净打包与 diff 检查均以发布候选为对象；任一失败不得推送。
- 发行相关 focused 首次发现两个 `LAYER_BOUNDARY_FORBIDDEN`：Gateway 直接依赖核心派活工具和子代理终态集合。移位原派发到 `conversation/session_task_dispatch.py`，模型工具与宿主共用；IM 状态投影移到 `conversation/im_task_status.py`。不加边界豁免、动态导入、第二套状态或权限旁路。原普通会话没有委派策略时不执行委派线程引用写入，显式委派仍先绑准确任务 ref 再发布正文/wake。
- main 不强推。若远端在测试期间新增提交，重新 fetch、合并并重新验证实际变更。推送后核对准确远端 SHA；线上 CI 只有真实通过才作为验收来源。
- 原 wheel 过滤开发代码；复用已验证 Ubuntu 20.04 基线运行时和依赖，重新安装本次 main 的 wheel，再用同源构建器编译字节码、建立完整发行清单。载荷不带凭据、会话、运行数据和业务 `.py`。
- 新包至少验证 Ubuntu 20/22/24 断网首装和同路径升级、HOME 改变、坏包拒绝、数据字节不变；1.19 原生 x86_64 隔离 prefix 下验证包本身的真实 TUI/Gateway/官方 MiniMax 调用和活动进程阻止升级。
- 用户安装位置 `/app/.my-agent`；先退出 TUI、停止独立 adapter 和 Gateway，再安装。无需重新运行模型配置，不删除飞书身份、记忆或工作目录。恢复 adapter 的准确命令见发行指南。
- 发布过程实测结果与包 SHA256 随交付说明保存；不将之前 smoke 或 L 来源六小时长测重复计入新包成绩。原交付质量、Windows 原现场冻结、真实飞书出站的证据边界保持明确。

建议下一步：用户按同 prefix 更新服务器并核对实际版本和飞书收发；其他 agent 可以并行只读审阅剩余交付质量问题，不应同时改发布目录或重启同一 Gateway。
