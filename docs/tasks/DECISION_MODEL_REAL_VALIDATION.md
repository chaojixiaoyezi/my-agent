# 决策模型真实接口验收

日期：2026-09-22。状态：进行中；真实 Jev、官方 MiniMax 与 Gateway TUI 已通过，子代理自动采用官方 M3 及受限候选 DeepSeek 后执行和工具后续轮已留证，其余决策点与故障组合继续验收。

## 授权与环境

用户提供Jev凭据，并明确要求测试到发现的问题修完；不以最初5次请求作为验收结束条件。
使用独立工作区代码、仓库外隔离owner与原模型目录，凭据文件0600、目录0700。记录仅保存是否配置，不含凭据。
不改日常模型/开关、不部署；另在仓库外隔离 home 使用本地8431端口的单个测试 Gateway，完成后停止，不触碰日常8420服务。所有请求是自建测试材料，不发送用户历史、记忆或个人文件。
官方端点为`https://api.typesafe.ai/v1/systemone`，请求`jev-latest`；后续业务响应实际版本为`jev-1.13.0`。
来源：[官方快速入门](https://docs.typesafe.ai/introduction/quickstart)、[API](https://docs.typesafe.ai/api)、[模型](https://docs.typesafe.ai/models)。

## 首批真实记录

下表前7次是能力设计复测，第8次是新增窗口门后的官方接口对照；前置遗漏必填窗口的一次本地配置校验失败未发请求，不计入调用。

| 次序 | 场景 | 秒 | 结论 |
| --- | --- | --- | --- |
| 1 | 原显式连接探测，4秒期限 | 1.087 | 通过，输入334；原配置/传输/worker/账本完整 |
| 2 | 中文choice/score/noul同请求 | 1.061 | 选择总结、写权限概率0.03、资料完整度1.89/2；协议通过，输入521 |
| 3 | 原8候选/8槽能力推荐 | 1.177 | 失败：全部not_needed，遗漏相关能力；协议成功不能当判断成功 |
| 4 | 改为每候选独立题 | 1.205 | 业务结果改善；一题被wire误拒，整次保留原输入 |
| 5 | 同题只读观察原始响应 | 1.229 | 定位百分位舍入：相关能力概率和0.99，被旧1e-4校验拒绝 |
| 6 | 舍入修复后原2秒期限 | 2.013 | 真实超时，保留原路径；没有答案，不能计为判断通过或用量零 |
| 7 | 同需求、隔离设置调整到4秒 | 0.728 | 通过，4项相关include、4项无关not_needed，消费版本仍有效 |
| 8 | 两道中文独立题，新增窗口门后 | 0.658 | 通过，CSV=yes、邮件=no；实际`jev-1.13.0`，输入392、输出59 |

业务材料为“读取销售CSV、分地区汇总并画柱状图，不发邮件、不删除文件”，8个合成能力涵盖数据/图表和无关能力。
第7次选中csv_reader、plot_chart、table_analysis、chart_design；没有为这些名称或任务在产品逻辑增加分支。
第3次输入4395，第4/5/7次各3731；第6次期限内没有供应商用量，保留未知。不能由这些小样本推断p95或整体输入 token 收益。
第2次测试报告误读响应属性导致实际版本字段为空，未补造该字段；实际版本证据取后续响应的正式`model`字段。

## 通用修复与离线证据

1. 各题独立并行，跨题选择槽无法假设先前答案已被其它题看到。改为每个候选独立适用性choice，完整候选仍由原搜索可达。
   说明只在对应题instructions发一次，宿主保留原索引/版本；不依赖自然语言解析控制状态。
   参考[官方Choice说明](https://docs.typesafe.ai/primitives/choice)，不是增加专项提示词分支。
2. 删除没有官方依据的adapter 64题硬限；原JSON节点/256KiB限仍约束整包，单题255选项遵守官方协议。
   96个独立候选题本地通过；真实大批次尚未验证，不能把字节限当64k token/32k局部窗口证明。
3. 对真实两位小数分布，按单项半量化单位累积误差校验总量和评分。高精度仍严格；零总量/缺键/非有限数/越界/明显不一致继续拒绝。
   供应商概率不归一化，原值/usage保留；低置信度不是权限或写入证据。

脱敏真实响应：`agent_py_agent/tests/fixtures/decision/jev_capability_rounding.json`。
协议、消费、本地HTTP、适配器4文件联合92项通过；replay断言实际0.99值原样保留且选回4项相关能力。
新增容量边界后的协议/传输2文件69项通过，覆盖总包、最长题、声明更小窗口、不超窗时仍发送。

## 实际 Gateway、主模型与 TUI 组合

隔离 owner 的决策设置只在测试目录启用 `skill_tool=apply`、4秒期限；默认普通生成模型仍是 `MiniMax-M2.7`，供应商实际地址为 `https://api.minimaxi.com/anthropic`。先用独立普通终端经 Gateway 输入一次中文解释要求，线程账本显示 Jev 1次、决策输入17,380、主模型输入35,553/输出2,476，主模型给出回答。

图形 TUI 的第一次附着失败来自隔离测试部署方式：薄客户端按原单 Gateway 合同连接 local-main 服务，而测试 Gateway 错以 user owner 启动。停止这一隔离进程，改以同一隔离 home 的 local-main 配置启动一台8431 Gateway 后，TUI 连接正常。真实 TUI 再输入一次普通中文解释要求，回复完整，状态行实际显示 `决策入 17.4k`；其 canonical thread 账本为 Jev 1次、输入17,379，主模型输入35,373/输出2,523。没有用按键模拟结果冒充模型调用，也没有修改日常 Gateway。

另一条 direct 调试 TUI 曾由官方 MiniMax 回答，但没有 Jev 用量；该入口仍需单独核对工作片身份与原指标投影，不能计为决策 TUI 通过。前述真实 Jev 共10次（8次接口样本，加普通终端 Gateway 与图形 TUI 各1次）。这些是小样本成功，不推出p95或所有任务质量。

用户指定的子代理候选仅为官方 `MiniMax-M2.7`、官方 `MiniMax-M3` 和 OpenCode 的 `deepseek-v4-flash`。隔离模型目录新增后两项，未改默认；端点分别核对为 `api.minimaxi.com`、`api.minimaxi.com`、`opencode.ai/zen/go/v1`。M3 在官方 Anthropic 兼容端点真实短请求成功，输入48/输出7；DeepSeek 首次缺少原配置要求的 `x-opencode-session` 被供应商400拒绝，改用宿主既有 `provider_session_scope` 后真实短请求成功，prompt97/completion11。三个候选的可连接性已确认，但短探针不能算作 Jev 派工与模型切换通过。

进一步在相同隔离 Gateway 图形 TUI 中，只输入一次普通中文需求：创建三个子代理分别算 `17+25`、`91-38`、`6×7`，不显式指定模型，等结果后汇总。三个 canonical child 均为 `DONE`，返回 `42`、`53`、`42`。父线程 `thread-83fdcced8ece4b7a` 的原 `model_metrics` 记录本次回合决策调用共3次、决策输入37,403、主模型输入109,993/输出5,696、模型轮次3；原 model_usage 分为 Gateway 两次和后台接续一次，不能把三次总调用误记成每个子代理各一次模型选择。三个 child 的同一批 `host_model_decision.v1` 均为 `mode=apply,status=retained,reason=retain_original`，实际 `host_model_profile.v1` 都是官方 M2.7。可确认真实 Jev 参与了子代理选择、保留原模型的创建/执行链路成功；由于任务简单，不能据此宣称已验证切换到 M3 或 DeepSeek。累计真实 Jev 调用13次，尚未对三个候选的实际选中率或判断质量作总体结论。

官方 [MiniMax M3 模型页](https://minimax.cn/models/text/m3)声明 API 最高1M上下文并给出 `MiniMax-M3` 请求示例；这里只按该来源建立隔离候选，不推断其它模型窗口。
本地源码现已通过原决策设置增加可修改的候选 profile ID 列表：空列表仍使用全部授权 `agentic` 模型，非空列表只取交集；更改设置会使在途建议失效。上述早期测试是在此改动前靠隔离模型目录限定三个候选，当时尚未验新的设置字段与真实 Jev 联动；后文续验另覆盖了该设置，不把测试目录条件误作该新功能的实测结果。

## 未完成与下一步

### 子代理自动异模验收门（M3 与受限候选 DeepSeek 单样本已验）

下一轮仍只向被测 my-agent 提一次普通中文派工需求，之后观察其自己创建和执行的原始记录；不替它填模型、补资料或完成子任务。每个样本至少保存原创建 operation、Jev 的合法建议/非选择状态、child thread 选择版本与 pending 终态、真实 first provider payload/模型/端点、工具轮和最终 run 结果；供应商名称以原配置来源和实际请求端点共同核对。

| 场景 | 验收事实 |
| --- | --- |
| 正常自动选择 | 在三个用户指定候选中，合法 Jev 建议被宿主完成容量/工具/版本核验；若选中官方 M3 或 OpenCode DeepSeek，实际首轮及后续工具轮必须使用该配置并完成子任务，不能只把建议写成 pending。 |
| 保留原模型 | Jev 返回无需、缺资料、无匹配、弃权、超时、认证/额度错误或坏响应时自动沿用继承的官方 M2.7 并继续，不要求用户逐子代理选择或补判断资料。 |
| 竞态与恢复 | 显式同值/异值选择、连接/凭据换代、候选撤销、停止、旧 attempt 重送及首发送后崩溃均不复活旧建议、不重掷模型；无法证明时保留当前合法原模型。 |
| 窗口与历史 | fake 原请求证明 250K/1M 输入和真实输出 cap、tool schema/native IR/Compact 口径；真实跨供应商调用证明原请求没有丢工具结果或错误回放思考块。 |
| 用量与隔离 | 决策输入 token 和未知值沿原账显示，决策没有 USD 价格/owner-run 成本写入；测试 Gateway 停止并恢复私有设置，日常 8420 与用户日常模型不变。 |

连接短探针和三子代理都沿 M2.7 成功的既有记录不能代替这张表的异模执行与失败矩阵。需先完成可重复的 fake payload/竞态回归，再用有限真实调用确认供应商及模型质量；真实测试失败时留原证据并修生产原因，不把人工补产物算通过。

P5-B 的隔离真实关系判断另新增 2 次官方 Jev HTTP 尝试：一次成功返回12对短样本（预设重复/更新/冲突各一对，其余 no_match），一次50ms调用者期限取消、原提取输入不变且没有可报告 usage；关闭对照为零请求。成功响应实际 `jev-1.13.0`，供应商输入6771、输出810，原账本为同一后台 run 的 `decision` 用途。随后原 Curator 提取仅使用本地固定返回替身，没有真实生成模型或晋升质量结论；脱敏证据和完整限制见 [P5-B 交接](DECISION_MODEL_P5B_HANDOFF.md)。决策请求不进入宿主的 USD 价格估算和 owner/run 成本累计，远端是否收费由供应商决定，期限取消也不能证明服务端未处理。

P5-C 已归档网页阅读顺序另新增 **5 次**官方 Jev HTTP 尝试：observe/apply 成功各两次，共报告输入8530；一次50ms期限取消，输入用量未知。首个普通中文样本完整回答含 `not_needed`，按原非选择合同保留原展示；第二个普通中文样本返回 `later/first/first`，宿主自动向 text/native 同一结果追加页序 `2 → 3 → 1`。关闭与错误配置对照零请求，原工具结果、页面 hash、归档、refs、工具账均未改变；测试设置已恢复。页面来自本地受控响应，不是实际互联网检索或安装版 TUI 验收；记录见 [P5-C 交接](DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。至此隔离环境累计实际 Jev HTTP 尝试 **20 次**，不能由这批小样本外推整体质量或延迟分布。

P5-A 另完成两轮 off/observe/apply 的原召回入口与真实 Jev 对照，新增 **4 次**官方 HTTP，实际 `jev-1.13.0`，
每次输入 678，约 0.84–0.86 秒。两轮均建议 `query_2`，但完整查询原已召回两条正式事实，apply 为 `no_addition`，
不能计作漏召回质量提升。目录未变、thread 设置已恢复；这是直连入口验收，不是 Gateway TUI。

P5-C planning 另新增 **3 次**真实 Jev HTTP：首次 4 秒 observe 为 ProviderTimeoutError（3.9987 秒），
原 Todo read 回执不变，随后的 apply 因 cooldown 零请求。隔离期限改为 8 秒后，off=0、observe=1、apply=1，
实际 `jev-1.13.0`，每次输入 783、约 0.75/0.77 秒，均选 `todo_2`；apply 只追加 `todo-rollback` 软提示，
原 Todo 账和顺序不变，目录与设置均恢复。它证明原工具入口与真实判断合同，不代表规划的实际业务收益。
上述 20 次历史尝试加这两点为 27 次；以下子代理样本另计，最终收尾统一核对。

子代理自动采用续验中，第二个普通中文 TUI 派工样本在三候选范围内由 Jev 合法选择官方 M3，
canonical child `subagent-1790141720-7b854fab` 自动变为 revision 2 / source automatic / advice adopted，
原真实 native probe、首业务请求和含工具结果的第二轮均到 `api.minimaxi.com/anthropic/v1/messages`。
该 child 1 个 attempt、2 次业务调用、0 retry，最终 DONE；并行合法 retain 的 M2.7 child 也 DONE，主任务 completed。
全程没有用户逐 child 选择、确认或补资料。窗口为 1M，输入沿原估算 28,281、实际出站输出 cap 16,314；
供应商原 usage 记录两次业务输入 41,866、输出 4,408，不能把总用量当首请求精确计数。
此前一个三 child 样本也全部 DONE，但都继承 M2.7；两轮合计 8 次 Jev、输入 114,946，
包含能力推荐及父后台接续，不能误记为每个 child 都调用一次选模。第三个算法样本 Jev 两题合法 retain，
两个 child 沿 M2.7 均 DONE，主任务 completed，新增 4 次 Jev、输入 57,005。
第四轮只在隔离设置把候选限为 DeepSeek，Jev 合法选中它，原 OpenCode 工具探针成功；
采用终态却为 retained/selection_changed，实际用 M2.7 完成，新增 3 次 Jev、输入 36,829。
事后目录代次/设置未变，但该轮没有当时提交分支证据，不能归因为超时。首四轮合计 15 次 Jev、输入 208,780。
候选范围限制只证明该条件下的链路，不代表三候选自然偏好；原 M2.7 回退始终保留。
第五轮在新的共享 runtime 基线上遇到前置 Jev 4 秒超时，child 选模因 cooldown/connection_backoff 无请求，
仍以 M2.7 完成；该轮 2 次 Jev HTTP，仅一次报告输入 17,611，另一请求输入未知。
前五轮累计 17 次真实 Jev HTTP、已报告输入 226,391，不能把未知输入补成零；第五轮不是第四轮的原因证明。
第六轮仍是相同源码基线、原 4 秒期限与 DeepSeek 受限候选，Jev 合法建议被宿主自动采用。
child `subagent-1790143563-f9b62971` 同一 attempt 内为 revision 2 / automatic / adopted，
工具能力为真实 native probe，窗口 1M、输入估算 29,766、实际输出 cap 16,314，原提交成功后仍剩约 1.10 秒。
首请求及后续均到 `opencode.ai/zen/go/v1/chat/completions`，保留原会话头、工具结果及 assistant reasoning_content；
1 次探针和 10 次业务 HTTP 均成功。原 usage 只有 DeepSeek，10 次业务调用、0 retry、输入 385,744、输出 25,061；
9 个工具轮后自然 DONE，约 10 分 38 秒，父任务 completed。整个样本没有用户逐 child 操作、增加期限或手工指定模型。

六轮最终为 6 个父任务 completed、10 个 child DONE，新增 **20 次 Jev HTTP**，19 次报告输入共 **263,256**，
一次超时输入未知；与前序 27 次合并，已登记累计 **47 次**。M3 是三候选范围内的自然建议样本，
DeepSeek 是受限候选样本；不能由它们推断默认选中率、延迟或业务质量。
隔离临时候选字段已用原 CAS 恢复，与开跑前 overrides 逐值/hash 一致，owner revision 保持前进；
默认仍为 M2.7，8431 已正常停止、无监听，日常 8420 仍为原 PID 2543，未操作日常配置。
完整结构证据、未覆盖部分和设置恢复记录见[子代理真实验收交接](DECISION_MODEL_CHILD_LIVE_HANDOFF.md)。

尚未完成原 Skill/插件全目录采用率、安装版/多owner故障矩阵及慢响应/额度/取消的组合质量；M3/DeepSeek 单样本成功只证明各自条件下的链路。第四轮 selection_changed 的精确提交分支仍未知，不由后续成功倒推原因。
Jev发送前本地筛查参照官方总64k、state+最长题32k两层限及用户声明的更小窗口；原 JSON 资源帽仍按字节硬控。全仓回归发现直接用 UTF-8 字节数比较 token 上限误拦批量能力请求，现改用原 `estimate_tokens` 留余量；这是提前筛查而非严格 token 上界，供应商拒绝仍须沿基础方案继续。此前真实 HTTP 样本不因本地估算算法改变而追加次数或推导质量收益。
窗口检查仍需覆盖主/子完整prompt、输出/推理预留和原Compact；真实缓存收益只能取供应商usage。
本轮是文本/native 请求的自动采用证明，未验证候选视觉等模态能力和图片 token；不据此整项勾选 P2-B、12 或 13。

P5-D 主会话 Stage C 另用唯一隔离 8431 跑三条普通中文新会话：原 `apply` 设置下，默认 2 秒一次 Jev 期限取消后沿 M2.7 完成；原 CAS 临时延至 8 秒单次/10 秒阶段后，Jev 一次返回 `need_data`、一次合法建议当前 M2.7，均保留原选择并完成。新增 **5 次 Jev HTTP**（4 成功、1 次输入未知的取消）及官方 M2.7 业务/探针共 4 次 HTTP 200。没有 M3/DeepSeek 主会话业务请求，真实自动异模仍未验。隔离设置已原 CAS 恢复、8431 已停，日常 8420 保持 PID 2543。详细事实见[主会话真实交接](DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。

P4-B 普通 user owner 的两轮隔离真实中文配置各有 **1 次 Jev HTTP**：首轮工具可见但因可信线程和 CAS 回执预览问题设置未成；底层修复后第二轮模型自主 read→thread patch 成功，原事务回执、线程文件及最终回复一致。两轮与前序47次及 P5-D 新5次合并，当前登记 **54 次 Jev HTTP**。第二轮没有独立第三次 read；测试后的 thread 临时覆盖已原 CAS 清空、8431 已停，日常 8420 未变。详见[普通配置修复交接](DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md)。

## 集成安装版原生 TUI 对照（2026-09-23）

用户授权的独立测试机由本线接管验收窗口，先取得前测试 owner 的退出证据并正常停止旧 Gateway。由 `319004926d094bdddd7bfe7d52a5d956b85a00c8` 的 git archive 构建 wheel，SHA256 `411c464694fb3c30bf141e6eac2fae6b82e11c37188a309c0e68942f9ad409fd`；独立 venv/HOME/测试 owner，只复制四个私有模型配置、不复制日常历史，真实普通模型端点为官方 `https://api.minimaxi.com/anthropic`。本地与另一测试机的共享 Gateway 未动；私有凭据和完整证据保留在仓库外。

三轮分别建立新原生 TUI 会话，只提交一次相同普通中文需求：阅读测试目录的 `review_queue.py`，分析并发领取任务的正确性问题，不运行或修改代码。测试文件为自行编写的队列示例；三轮均通过一次真实 read_file 工具轮、两次 M2.7 主业务调用完成，canonical 请求均 done / ok / completed。它们没有创建子代理、发生 Compact 或切换业务模型。

| 模式 | 原请求 ID | Jev HTTP | 决策结果 | 主 LLM 输入 | 决策输入 | 主任务耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| 关闭 | `gwreq-1790191117-c76914dccdb74a25b8e644b8ca88d30b` | 0 | 不调用 | 44,564 | 无调用 | 23.045 秒 |
| apply，选模 2 秒 | `gwreq-1790191304-2b970d29f0ae44c892813b1b42eb27d3` | 1 | deadline / provider_failed，保留 M2.7 | 44,563 | 未返回 usage，未知 | 21.949 秒 |
| apply，选模 4 秒 | `gwreq-1790191475-a451581090334874950c313c563a3e07` | 2 | 选模 need_data，保留 M2.7；能力建议正常返回 | 41,155 | 18,482 | 18.533 秒 |

2 秒调用约 2.076 秒因期限中断；其后主链无需用户处理便完成。4 秒轮两个 Jev 响应均 HTTP 200 且原账本 finished；传输观察器的 1.008 / 1.365 秒只测到响应头，不能冒充完整推理延迟。新增 **3 次 Jev HTTP**，全线累计 **57 次**；一次超时的输入未知不补零。4 秒轮 TUI 原用量行显示“决策入 18.5k”，无决策价格或独立决策输出展示。

原供应商缓存读回：关闭轮 21,552，2/2 主调用报告；2 秒轮 20,929、4 秒轮 19,267，各只有 1/2 主调用报告。后两轮没有报告 cache_write，不能把账本聚合占位 0 当成实际零写入。关闭与 2 秒轮 system hash 相同，4 秒轮不同；三轮工具 schema hash 相同，说明本样本的能力减量主要改变了文本提示面。这证明存在实际缓存回执，不证明开关间缓存完全复用，也不证明跨模型或 Compact 后的命中。

4 秒轮主输入减少 3,409，但含决策后已知总输入 59,637，高于关闭轮 44,564，**本样本未节省总输入 token**。三次短任务的耗时不能据此推断提速；回答指出了 claim 的重复领取和索引竞争，但关闭轮对无锁 append 的描述过于宽泛，未形成完整质量评估。没有真实跨模型采用、多模态业务或超大历史验收，本轮不关闭 12.4 / 12.7 / 13 / 16。

初次采集脚本只抽取终态回复和 usage，漏采顶层 `model_selection_observation`，曾造成“观察记录缺失”的误判。直接读回 canonical terminal 与 done 后确认三轮文件逐值一致，2 秒记录 status=deadline，4 秒记录 status=observed / choice=need_data / adopted=false，**没有记录丢失的产品缺陷证据**，不修改生产终态逻辑。另一次 TUI 启动把未存在的 ID 当成 resume 参数，被 CLI 正确拒绝；移除该验收参数后重启新会话，拒绝期间无模型请求。

验收后设置经原 CAS 恢复为关闭（owner revision 18），三条 TUI 正常退出。队列 pending/processing 均 0、全部观察到的 HTTP 有终态记录后正常停止本候选 Gateway；确认原 PID 已退出、8420 无监听。旧安装和回退启动信息完整保留，未重写旧历史的三条 created TaskRun。原测试目录内后台辅助模型调用单列，不能混算成上述六次主业务请求。

建议下一步：继续 12.4 超大 canonical 历史的有界读取及 Compact 后真实 payload/缓存对照，再做主会话自然异模和集成版媒体验收。其他 agent 可并行只读审查和独立测试，恢复 writer 与同机 Gateway 保持单一 owner；真实任务仍只给一次普通中文需求，不代替被测 agent 补产物或改状态。


## 后续缓存验收的环境复核（2026-09-23，只读）

使用既有SSH配置成功连接用户授权独立测试机；前次319004926隔离安装目录、venv、回退/清理记录仍存在，8420/8431当前无监听。未启动、停止、安装或改模型设置，也未发供应商请求；全线Jev HTTP累计仍57次。此证据不表示最新分支已部署。下一轮需冻结源码版本及wheel hash、再次复核占用，沿原隔离owner验证真实缓存回执，未报告字段继续保留未知。

## 12.7同会话Compact与跨模型缓存验收（2026-09-23，固定安装版）

固定源码`5e5122b04ea52a06d834c1b50a4e12a69afda900`、tree `fbead33fc3627f395cba580a00a4bf071b335303`经git archive构建wheel，SHA256 `1c50c588e8687a9cbb6847f12a4a38bdd9d6d41841dba816a35fe10dc1b5f1b7`。相对前安装版变化的19个测试文件 **613 passed、1既有xpassed**，全目录Ruff、doc sync、strict code-size（hard=0，基线不变）、diff、源码与wheel清洁检查全部通过；日志`/tmp/decision_candidate_gate_5e5122b04_pytest.log`及同前缀0—4守卫日志。此focused严格gate不重新裁定旧全仓历史失败，线上CI没有作为验收来源。

与另一开发任务确认测试机空闲后，在用户授权独立机器的新目录建立venv/HOME，只复制原测试四模型配置及自编队列材料；旧安装和旧证据不变。安装后用`python -I`确认实际导入新venv。唯一候选Gateway监听8431；首次start就绪观察超时，但同PID随后running，未因此重复启动。补齐旁观器后通过原CLI正常stop/start，最终业务进程PID3163135。私有凭据不打印、不入仓，另一个任务的运行环境不动。

原生TUI会话`sess_1790209198_9e446294`、thread `thread-8e1ce61b514842bf`，每轮仅给一次普通中文需求，不代替被测对象写文件/补产物。业务先保持官方M2.7和决策关闭，完成队列审查与普通跟进；空闲后原`/compact`提交`compact-v3-1-3dad852327c8cff3d5b6`、generation 0→1、source_messages=4，原会话模型用量新增独立`conversation_compact`事件。随后同会话续聊，再用原设置CAS开启on4（owner revision19→20）。Jev选模实际返回当前M2.7，未发生自动异模采用。之后通过原`/model`菜单的“当前会话模型”先选官方M3、再选OpenCode deepseek-v4-flash；不改变新会话默认模型。

下表输入、缓存均取原用量事件及原ModelCallLedger逐字段来源；“已报”指该字段有供应商回执的调用数。Compact单列，不把摘要缓存当作恢复后主调用缓存。

| 场景 / 原request ID | 主调用数 | 主输入 | 缓存读（已报） | 缓存写（已报） | 决策输入 |
| --- | ---: | ---: | --- | --- | ---: |
| M2.7初次审查 `gwreq-1790209415-100565b079be47539a54356263dd1403` | 2 | 44,534 | 21,558（2/2） | 22,971（2/2） | 无调用 |
| M2.7同会话跟进 `gwreq-1790209489-431d6c4338d244cf92dcfe590b810b2a` | 1 | 24,827 | 22,194（1/1） | 2,633（1/1） | 无调用 |
| Compact摘要 `conversation-compact:transcript:953c6b63fb7a40d2a912bb906515bbc6` | 辅助1 | 26,371 | 25,198（1/1） | 1,173（1/1） | 无调用 |
| Compact后M2.7续聊 `gwreq-1790209646-1c05dd771b4040199b99efc249bd9258` | 1 | 23,298 | 20,222（1/1） | 3,071（1/1） | 无调用 |
| on4仍选M2.7 `gwreq-1790209783-6882ee1d3ca947898019575447b367a5` | 2 | 48,763 | 22,978（1/2，其余未知） | 未报告 | 20,466 |
| 显式M3首轮 `gwreq-1790209944-65847621a235492e8ebd179fd9e90e3a` | 1 | 28,412 | 128（1/1） | 未报告 | 20,398 |
| M3实际读取工具轮 `gwreq-1790210045-70bb11673f8b4989a2cb98657391267e` | 2 | 62,932 | 59,276（2/2） | 未报告 | 20,395 |
| 显式DeepSeek实际读取工具轮 `gwreq-1790210151-ed5ee5fa9d46497caf4b1553f8478b79` | 2 | 74,088 | 36,352（1/2，其余未知） | 未报告 | 20,430 |

七条业务均原`done/ok/completed`；M3首轮没有工具调用，不作为工具轮证据，后续独立读取需求实际完成read_file；DeepSeek同样完成原工具往返。业务文件hash保持，未运行/修改文件。真实端点分别为官方`https://api.minimaxi.com/anthropic/v1/messages`（M2.7和M3）及`https://opencode.ai/zen/go/v1/chat/completions`（deepseek-v4-flash）；Jev为原`https://api.typesafe.ai/v1/systemone`。本轮Jev **8次HTTP200，累计65次**。保留原usage，不展示决策价格；界面只追加决策输入。

窄旁观器在原HTTP组包之后仅记system/tools/messages的cache_control数量及已有hash，不改请求/响应；在原ledger.finished返回后读取原record的provider_usage_fields，缺失写null。M2.7稳定材料阶段实际有三个ephemeral断点，on4的system hash变化单列；工具schema hash保持。M3首请求缓存读128，后续两次分别28,412和30,864；这证明该模型自己的真实缓存回执，不证明沿用了M2.7的缓存。DeepSeek首主调用未报告缓存、第二次明确读36,352，不能把首调用补零。所有wire请求都有HTTP终态，另有探针等不在业务ledger范围；wire到Gateway request没有精确关联键，因此不凭数组位置将每条wire分配给某一业务请求。

不同业务输入、工具轮数和Compact成本不同，本轮**不作净token节省或速度提升结论**。回答质量也未因done自动通过：例如M2.7并发时序说明存在把空队列pop描述成仍可返回任务的不严谨处，属于后续质量评估；本片不加针对该样例的产品分支。显式选模验证历史/工具兼容，不能计作第13/16项Jev跨模型自动采用完成。真实近窗口、媒体与超大来源仍按12.4验收。

采集修正：旧脚本强取done.conversation_runtime，第二轮缺此投影而报KeyError；原conversation_claim.thread_id存在且业务完成，改为读原claim即可，不改产品。旧脚本还把采集时thread的模型和代次套到所有旧轮，窄报告现明确命名`thread_*_at_collection`；前后代次依据gen0快照、原控制展示与canonical checkpoint，实际模型依据每调用ledger/wire。最终仓外报告`/tmp/decision-cache-acceptance-5e5122b04/completed-seven-rounds-cache-usage-report.json`，SHA256 `6093ab669ec5b405fbdf5e8391adc78ae4c05f59ffbaa30f2d6e846ecb038415`。

清理：原CAS将隔离owner overrides逐值恢复baseline，revision20→21且enabled=false；TUI正常退出code0，候选Gateway通过原CLI停止，PID不存活、8420/8431无监听、inbox/processing均0、26条HTTP观察均有终态。历史测试thread保留显式DeepSeek选择供证据复查；它是新建隔离会话，不影响默认模型。清理机器记录在新隔离目录`artifacts/cleanup-current.json`。

12.7按上述明确组合收口；12的7个子项已完成6个，剩12.4。总清单仍11/18、P1—P5 Goal active。建议下一步：完成并复核12.4选中正文生命周期及其宿主伴随片；另一agent可并行查协议/取消，生产修改和Gateway控制各守单owner，源代码新实现不得借本固定旧包冒充已部署验收。
