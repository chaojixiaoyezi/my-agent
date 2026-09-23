# 后台 Compact 准备与作用域交接

## 基本信息

- workstream：决策模型第 12.4 项，后台完整恢复前置片。
- branch：`codex/decision-model-integration`；独立决策工作区，基线 `468fec0a9`。
- owner：主代理负责生产实现，sol high 负责独立测试，Astra max 负责作用域复现与审查。
- date：2026-09-23。

## 本线目标

后台容量候选需要真实完整请求，但不能为每个候选重复读取任务、写进度或扩大独立任务可见范围。

## 实际完成

`background_context.py` 的一次准备保留原副作用顺序，冻结预算、策略、wake 和上下文；纯渲染只调用原预算器与格式化。原 `context_markdown` 继续执行一次准备和渲染。

`background_history_seed.py` 在原成功加载后冻结 `TaskScopeDecision` 与 scoped thread，正常种子也使用同一纯行投影。读取失败与主动禁用不产生可用 projection。未新增持久 schema、配置、调度或后台恢复分支。

## 改动文件

- `conversation/background_context.py`、`background_history_seed.py`：一次准备与纯投影。
- `tests/test_background_prepared_context.py`：6 项冻结、重复渲染、历史隔离与错误测试。
- 入口、设计、TODO、容量审计、测试、Gateway 模块和文件树文档：同步已完成前置片与剩余边界。

## 测试命令和结果

使用仓库虚拟环境：

```bash
python -m pytest -o addopts='' agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_background_context_runtime_errors.py agent_py_agent/tests/test_background_owner_delivery_commit.py agent_py_agent/tests/test_background_capability_compact.py -q --tb=short
python -m pytest -o addopts='' agent_py_agent/tests/test_background_prepared_context.py -q --tb=short
```

结果分别为 201 passed、6 passed。Ruff、doc sync、导入边界、strict code-size（hard=0，基线不变）、diff 与 clean-package 检查通过。打包检查首次只因新增测试未跟踪失败，纳入 Git 索引后通过。无真实供应商调用、部署或重启；旧全仓 8 项失败未在本片收口。

独立诊断脚本在本机临时文件 `/tmp/background_compact_scope_repro_20260923.py`，结果在同名前缀 `.json`；用虚拟环境 Python 执行并传 `--repo <本工作区>`。三组真实 SimpleAgent/Store/checkpoint/CAS 复现均退出 0，仅摘要为替身，没有业务 HTTP。脚本断言用于确认原缺陷，修复时必须改成正确行为回归，不能将维持缺陷算验收通过。

## 影响范围与需要主线重点复查

detached transcript 压缩推进共享游标，但创建后的全局摘要被原隔离规则清除，导致 seed 为 ready 却无旧行及替代摘要。活动工具压缩也会沿全局已提交链隐藏工具 ID；detached 或窄审计不消费该摘要，仍会失去恢复材料。原始记录未删除。

下一片必须在唯一 checkpoint/CAS 权威内明确摘要消费范围和精确替代来源，不能用全局游标裁掉不消费摘要的任务材料，也不能为窄审计补入整段 owner 历史。单靠恢复创建前的旧摘要不足以保留任务创建后的工具和交错消息。

## 需要其他线协调

已与模块重构负责人对齐本片文件，不修改调度、投递、owner 或 TUI。媒体线的原生 IR 与 provider 编码继续由其负责；不抢占文件。测试机 192.168.1.9 已获用户授权，实际部署/重启须先协调共享 Gateway 占用。

## 剩余风险与后续建议

第 12.4 项仍未完成：后台完整恢复接线、初次加载及手动 Compact 尚待实施；跨窗口、模态、输出预留组合与真实缓存验证另按 TODO 推进。

建议下一步：先以以上复现修正原 Compact 作用域与替代关系，再接公共完整恢复器并核对实际出站 payload。共享 checkpoint 实现串行，独立回归与只读审查可并行；不能提前关闭总 Goal 或扩大本地证据为已安装 TUI 验收。


## 后续v3来源底座切片（基线3cbbba540）

原checkpoint writer已统一写v3，scope/摘要base/精确来源与版本共同封印；局部CAS可以保留全线程投影。原全链工具ID union改为适用摘要base链的四元覆盖；旧身份不全不隐藏。source/retained均按完整引用切分，不能以call_id重复为由拒绝合法不同轮调用。

工具归档从第一次外置写入保留原run/attempt/turn/call；索引与恢复引用不补当前runner。新artifact按执行身份区分相同正文的同名调用，旧引用保持。原合并保留未知材料，但未知材料重复不会算本轮新增执行。检查点版本降级、交错task scope、孤立候选、CAS竞争及同名调用测试见TESTS。

模型轮原生成函数已补唯一nonce，修复同run/attempt跨Goal工作片重置局部序号造成的四元身份碰撞；只改`tool_model_generation.py::_begin_model_turn_identity`，未动媒体线的IR/provider编码。21文件562项通过；最后旧摘要完整性补强组三文件74项通过。具体本地guard与剩余边界以TESTS为准，未推送部署。

重要边界：生产宿主仍使用默认thread作用域，未把后台注入/过滤绑定同一个适用view，也未把初次/手动Compact接入完整请求。新检查点底座通过不代表原后台三类缺陷已关闭。下一片继续实施宿主scope与view应用，归档与恢复测试可并行，共享CAS/source串行；大历史流式读取另与TUI线对齐，当前千万行无cursor样本未通过。


## 后续后台scope/view接线（基线7f473c44f）

后台现选择thread/task/turn范围，种子、操作上下文、工具过滤及摘要器共同消费AppliedCompactContext。detached先筛canonical原文后排除view覆盖，narrow保持seed=None且只继承本conversation_turn_id摘要。原transcript source与live/carried提交显式传scope/base，局部CAS保留全局摘要/游标。

后台运行仍沿既有溢出重试，完整请求计量未在本片收口；Gateway/child准备边界的同view绑定亦待下一片统一。新增测试使用原Store/checkpoint/CAS与真实模型消息投影，不把分层回归称为完整HTTP恢复。无部署、重启或真实供应商调用；共享wake存储由模块重构线负责，本线未碰。

建议下一步：接公共PreparedCompactRecovery，候选只替换历史与宿主已知注入位置；成功CAS后从获胜结果发布实际view。narrow空历史与活动IR不能伪造空seed。独立测试可并行，公共恢复和源码整合串行；前轮全仓8失败、真实缓存和超大原文读取失败继续单列。

本片联合18文件420项通过，最终补充验证和严格guard见TESTS；没有将重复运行相加。作用域测试由独立审查代理实现，控制流旧夹具由另一代理适配，生产共享文件由各自认领者修改后主代理整合。

末次补验102项及空工具线程绑定29项通过；严格本地guard通过，尺寸基线未变。空archive不跳过显式context的线程校验，防止首轮摘要错绑。独立源码复核未发现本片阻塞问题。


## 后续完整恢复片交接（2026-09-23，本地）

本片基于上一提交`7c73c5d96`，仍在决策模型独立分支内。解决后台粗估摘要先提交、真正恢复请求才发现仍过大的问题；Gateway/child先统一同次scope/view，后台再复用原完整请求恢复器。精确实现与开放边界见[容量审计末节](DECISION_MODEL_CONTEXT_AUDIT.md)，验证结果统一写TESTS。

改动归属：root拥有公共恢复器、capture、原IR标记及后台投影/执行；Sol负责Gateway/child准备及独立HTTP/helper测试；Astra max负责active-turn完整projector和独立审查。没有修改另一任务的wake/WAL或TUI媒体实现，不推送、部署或重启。

重点复查：CAS后只安装获胜snapshot的view；后台同片范围只冻结一次；活动归档保留未知四元身份和待发送指导；候选完整计量包含原输出预留。IR source是内部结构字段，provider不发送；整份原archive、审批和预算保持。恢复不再调用旧后台粗估wrapper，删除旁路后旧测试按真实render/select或明确defer合同适配。

剩余风险：混合transcript+active两侧都很大时尚无联合候选；其它宿主无transcript活动归档仍待同入口迁移；初次/手动与超大canonical有界读取、真实缓存/安装版TUI均待验。旧全仓八项失败未借本片关闭。测试机1.9已获用户授权并只读核对可达，尚未部署。

建议下一步：先将本片定向测试和严格检查收口，再迁移其它活动入口及联合候选；公共状态/CAS由主线串行整合，可并行独立协议测试或只读审查。部署前与插件主线同步目标、现有Gateway和回滚版本。

本片最终验收：25文件657 passed（67.69秒），日志 `/tmp/compact_complete_recovery_final_20260923.log`；Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff与clean-package通过。候选/发送一致性在实际provider builder和HTTP入口替身核对，未调用真实供应商；前轮全仓失败不抵扣。


## 后续混合来源及活动归档片（基线dbb2d6983，本地）

解决问题：此前Gateway/child无transcript时先按工具局部大小提交、再重新准备业务请求；transcript与carried都大时，单压一侧仍溢出。现三宿主共用完整准备，混合来源在一个候选里同时摘要和替换，原writer封印消息/工具双覆盖，经一次generation CAS后直接发获选材料。

文件归属：root拥有公共恢复器与conversation Compact/source/projection/checkpoint/取消；Sol分别负责Gateway/child宿主及HTTP测试、纯分区与混合HTTP测试；Astra max独立审查并实现混合机械/回退/取消合同测试。新纯模块compact_tool_summary只组织全部所选记录的模型投影，复用现有分段器；不读取外置全文，不新增依赖或持久状态。完整archive保留，未知身份不授覆盖权。取消中性丢弃并在92%回调后、CAS之前检查；普通界面错误仍不影响提交。

复核重点：每个候选保存自己的完整材料与来源分区；非空机械回退仍包含全部工具投影，过大不提交；不可拿被截断的展示handoff声明所有工具都已摘要。child空第0注入槽字节等价，下一prepare使用已清除的展示，旧推荐不复活。完整payload对照走真实provider builder及HTTP入口替身，不把模型正文当调用身份。

边界：初次/手动入口、真实ToolCall/ToolResult混入的native IR、媒体集成、超大canonical读取及真实供应商缓存未完成。12.4继续进行中；早期全仓八项失败保持。测试机1.9已授权且只读确认可达，本片未部署、重启或调用供应商；模块重构线当前共享Gateway发布与本线独立，无测试占用冲突。

建议下一步：联合定向和严格gate收口后继续初次/手动及真实IR恢复接缝；共享状态由主线串行实施，独立验收可并行。部署前先与共享Gateway负责人对齐，保留原版本及真实任务证据。

本片最终32文件738 passed（75.48秒），日志 `/tmp/compact_mixed_final_20260923.log`；Ruff、doc sync、导入边界0发现、strict code-size hard=0且基线未改、diff与clean-package通过。首次四项旧夹具失败及修订在TESTS保留；真实子代理9次恢复、原读文件工具和每代CAS已验。无真实供应商、部署、重启和线上CI证据。建议下一步按上述范围继续12.4其余接缝，公共状态串行、独立验收可并行。
