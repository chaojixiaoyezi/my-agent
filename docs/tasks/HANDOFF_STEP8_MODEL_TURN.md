# 第8.2模型采纳首片交接

## 基本信息

- workstream／branch：step8-model-turn／codex/step8-model-turn；基线85050017d。
- owner：主代理单写；Astra max对四个生产文件只读复核。
- date：2026-09-23。

## 本线目标和实际完成

解决主循环混合模型采样、重试、用量和输入消费的问题。新增model_turn只接五项绑定操作；原执行权、Goal开始、空响应修复留在装配点。统一guidance投递歧义查询，删除两个重复实现；物理提交边未移动。没有新增配置、历史或执行器。
基线两个中断测试替身补显式wakes查询；三个旧空响应xfail迁为native有效测试。

## 改动文件

- `agent_core/tool_loop/model_turn.py`、`_tool_loop_service.py`：采样和响应采纳。
- `agent_core/runtime/guidance.py`、`tool_model_generation.py`：两层重试共用只读判据。
- `tests/test_tool_loop_model_turn.py`、`tests/test_tools/test_tool_loop.py`：采纳顺序、动态重试及空响应。
- `tests/test_thread_interrupt.py`：已单独提交fb0d4dfcd的替身修正。
- 入口、状态、设计、树、测试及唯一TODO同步；详细边界见TOOL_LOOP_DEPENDENCY_SPLIT。

## 测试和影响范围

十文件组合212 passed、24既有xfail；包含stale attempt、preflight/provider超限、临时schema、物理提交、插话和重试。Ruff、doc sync、strict code-size及diff已通过；clean-package初次只因三个新文件未登记失败，登记后通过。
Astra max四文件diff未发现阻断，三种独立导入成功。此次仅本地候选，没有推送／部署／第8步原生TUI，线上CI未作为证据。

## 需要主线重点复查与其他线协调

只集成此线和独立中断夹具提交。Compact候选A/B/C尚未集成，Jev分支不整枝导入；四个候选fake Store差异未被本片处理。原生任务的业务内容错误继续保留TESTS。

## 剩余风险和建议下一步

本地严格gate已通过，下一步集成首片；继续请求准备与Compact职责拆分，组合发布后开展多路TUI长任务与恢复验收。Compact补丁可以并行只读审阅，主调用链单人写；不得移动pre-I/O提交、交换计量与确认顺序或把组件验证算作真实验收。

## 后续候选补充

首片已集成本地主线50cd9e7a7，源码tree与候选相同。第二片将首次组装／生成、超限恢复、临时工具消费迁到model_turn；原装配点仍选择回执参数并绑定原实现。A/B最小移植和三项兼容修复一并完成本地组合456 passed／24既有xfail（18文件）。参考源467f3cac3／425bcb3a9，具体边界见设计文档；无Jev、scope链或C摘要开关。
本片严格gate已通过；doc sync最初指出memory模块说明缺失，补齐后通过。
建议下一步：集成已验证候选，继续拆原prompt与Compact宿主依赖，再发布组合包跑实际TUI；不把新增扫描API或候选测试等同于完整Compact已接通。

候选ef355f822已集成本地主线57baa13cb，两者源码tree均为57fabe4c78bd404cdc482efdd303c14e9de0d202；请求周期实际diff复核无阻断。未推送或部署，继续按唯一TODO处理Compact同源接线和原生验收。


## Compact 来源与提交边界片交接

- 基线9f94ffb9f，同一工作线；主代理负责原生候选／提交／来源接线，Astra max仅写tokens及其测试，另一审阅者只读核对IR边界。
- 新增`compact_text_source.py`、`request_content.py`；原`tool_ir_compact.py`容纳窄候选，删除主服务旧settle/reduce入口。C来源去掉未启用strict参数，无v3/schema迁移。
- 持久CAS后的projection失败不回滚；临时回合无CAS仍按原语义回滚。两个原始红灯和两个审阅新增红灯均转绿。非文本来源上层两项拒绝保持原消息和检查点。
- tokens只对可证明有界的小内置对象用公开dumps，其它仍流式；原峰值断言和5005等值／异常核验通过，未修改GC策略或放宽阈值。
- 14文件组合333 passed／20既有xfail；测试文件完整列表见TESTS，不追加全仓。本地Ruff、doc sync、strict code-size hard=0、diff、clean-package全通过，未改尺寸基线。未发布部署、未启动真实模型或TUI，旧scope过滤风险和业务失败保留。

建议下一步：主线只集成本片已验证diff，继续剩余工具轮／结束决策与scope读回缺口；独立审阅可并行，Jev链不整枝导入。真实TUI必须在组合发布后另验。


## 工具事实投影片交接

- 基线b4ffb3475；主代理写runtime_facts／reducer、archive有界投影、loop_support的重建函数及reducer/MCP测试。独立空转发清理线只改同文件执行入口，函数归属已对齐。
- 原process缺失10项红转绿；审阅发现巨整数／非有限浮点可破坏投影，三项红转绿，未改变verification原编码。
- 8文件241 passed；Ruff、doc sync、strict code-size hard=0通过。当前、外置和carried恢复读取同一有界process事实；不改变原status/error/effect_outcome/call ID，无新持久schema。来源边界与字段见设计文档。
- 测试层曾错误假定unknown effect等于unknown status、MCP默认无需批准，均按现有真实合同修正夹具，不改生产语义去迎合断言。

建议下一步：与独立空转发清理片组合，继续第8步整体开发及原生多TUI；源码未部署，不能以本片通过关闭真实验收。


## 工具事实与循环入口组合

- 工具事实提交7482a40d6；独立入口片b4d6bd308已精选为c6f45425b。唯一冲突为loop_support模块双层注释，保留双方职责；恢复函数、执行入口各自变更均保留。
- 删除ToolLoopService和旧私有循环入口；11个原测试文件迁到唯一函数入口，原断言保持，增加宿主身份检查；多次驱动的monkeypatch局部还原。
- 独立入口片218 passed／20既有xfail，相邻104 passed／4既有xfail。工具事实片241 passed；这些分组有重叠，不相加为总数，16文件组合349 passed／20既有xfail，退出码0（双-q的完整进度字符计数）。

建议下一步：组合严格验收后集成本地主线；Compact跨请求过滤风险独立只读复核，确认后再定修复。尚未部署或开展新版真实TUI，不能提前关闭第8步。
