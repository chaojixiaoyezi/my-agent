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
