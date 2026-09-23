# P5-B 第一片交接：来源与正式条目的临时关系建议

## 基本信息

- workstream：decision-model-plan / memory P5-B 第一片。
- 基线：本片开始时 HEAD `ef497a904`，同一已协调 worktree 内叠加此前稳定片；本片不提交、不推送、不部署。
- owner：decision_http_max；日期：2026-09-22。
- 先读：AGENTS、LLM_GUIDE、WORKSTREAMS、决策整合设计、memory 模块合同及原 Curator/Candidate/Promotion 测试。
- 参考边界：只读核对本地 Hermes `tools/memory_tool.py` 的 add/replace 锁内复读；只借鉴修改前复核权威事实，
  本仓库继续使用精确 ID/hash/version，不引入 substring 匹配或另一份正式记忆状态。

## 目标与实际完成

原 Curator 同时持有新经历和有限正式材料，可能遗漏重复、更新或冲突关系。本片为完整材料增加独立的可选提示，
保持原提取、证据验证、候选与晋升为唯一决定链。设计先落在 `DECISION_MODEL_INTEGRATION.md` 的 P5-B 小节，再实施。

- 新点 `curator_relation` 只允许 `owner_background`，默认 off；observe 只计调用，不改输入，apply 才附合法提示。
  `memory_decision_curator_relation_{mode,timeout_seconds,profile_id}` 复用原 MemorySettings、AgentConfig、YAML、owner CAS 与 TUI。
  线程通用开关/模型不能污染此后台点，线程新增本点 patch 被拒绝，运行 scope 仍由同一元数据检查。
- 原 `annotate_curator_batch` 只创建一个阶段；标签/优先级与关系共享绝对 caller deadline 和原 lease 头寸。
  后续关系等待后复核前一标签是否仍有效；普通关系失败不会抹掉仍有效的标签，用户停止继续抛出。
- 完整消息用原 UTF-8 hash 校验；long-term 输入增加仅宿主可见的 `authority_version`、`content_chars`。
  正式版本取原 MemoryRecord，原规范化正文 hash/长度证明短预览完整；旧 `to_model` 的字段与字节保持不变。
- 每次最多 32 个独立来源—条目对，本地帽只保护输入/延迟。只提供 possible_duplicate / possible_update /
  possible_conflict / no_match / need_data / abstain，覆盖声明固定为 presented_pair_only。
  state 中正文只出现一次，不靠题目互相去重；未比较材料仍完整留在原提取批次。
- 发送前和采用前复读当前 owner 的原 `JsonlMemory.all()`，按精确正式对象的引用、版本、hash 和范围复核。
  替换、同正文换代、删除、另一 owner、来源变化和配置撤销均不采用旧结果；无版本/截断/audit 覆盖未知返回 need_data 或原批次。
- `CuratorRelationAnnotation` 只在本批内存输入存在。缩批或正式绑定不匹配会过滤；超出原提取字符预算就丢本点提示。
  没有新候选 store、后台 Agent、晋升 API、强制补资料或人格覆盖入口；输出 schema 及 commit/promotion 未修改。

## 文件归属

生产：

- `memory_store/decision_curator.py`：共享阶段编排、原标签失效复核。
- `memory_store/decision_curator_relation.py`：唯一新增关系适配，准备/原库复查/合法回答投影。
- `memory_store/curator_inputs.py`：内存关系注释及双边引用过滤。
- `memory_store/curator_formal.py`：仅宿主版本/完整长度，原普通投影不变。
- `memory_store/curator_backend.py`：有提示才加入非权威说明，提取与提交行为不变。
- `settings/decision_settings_schema.py`、`decision_settings_defaults.py`、`config.py`、`_memory_types.py`、
  `config/agent_config.yaml`、`cli/chat_parts/tui_decision_menu.py`：本点原配置与展示登记。

测试/文档：

- 新 `test_decision_curator_relation.py`：真实临时仓库、fake decision、原 worker/账本/提取提交组合。
- `test_decision_settings_scope.py`：scope 预期改为按原元数据派生，覆盖多个后台点。
- 本交接、整合设计 P5-B 小节、memory 02/04 和 CODEBASE_TREE。
- memory 02/04 同步主线已验事实：task_local/control_plane 或 memory_enabled=false 不扫描正式项目记忆；
  同时纠正原“Jev 64 题上限”文案，64 仅 Curator 本地延迟/输入帽。

## 验证

```bash
python3 -m pytest -o addopts='' agent_py_agent/tests/test_decision_curator_relation.py agent_py_agent/tests/test_decision_curator.py agent_py_agent/tests/test_decision_settings_scope.py -q --tb=short
```

105 项通过：新增关系/原 Curator/设置作用范围，包括完整阳性、缺版本/截断/错误 hash、实仓库同正文版本变化、
删除/跨 owner、独立 Choice 部分失败、缩批、默认关闭不准备或读库、原普通模型投影、共同 deadline、读库迟到、
原模型账本及原提取失败不提交；成功仍由原输出决定 candidate_type/action 和宿主 promotion_mode。

```bash
python3 -m pytest -o addopts='' agent_py_agent/tests/test_memory_curator_v2.py agent_py_agent/tests/test_memory_candidate_daily_v2.py agent_py_agent/tests/test_memory_promotion_v2.py agent_py_agent/tests/test_curator_timeout_adaptive.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_tui_decision_menu.py -q --tb=short
```

188 项通过；与前组共 293 项。上述定向回归使用本地 fake 模型，未跑全仓 pytest 或部署；后续真实接口记录见下一节。
所改生产与测试文件 Ruff 通过；10 个生产文件按原 strict AST/baseline 检查无新增 blocker。
`python3 scripts/check_doc_sync.py` 返回 `DOC_SYNC_PASS`，`git diff --check` 通过。

## 首轮隔离真实 Jev 验收（2026-09-22，本地）

只使用既有 `decision-model-live` 私有测试目录的 owner/profile；正式端点核对为
`https://api.typesafe.ai/v1/systemone`，请求 `jev-latest`，响应实际模型为 `jev-1.13.0`。
Gateway 8431 保持停止，未访问日常 8420、未启动另一个 Gateway、未改日常模型或生产源码。
采用原 owner 的模型目录解析与设置 CAS，仅临时覆盖 `points.curator_relation.mode/profile_id`，结束后原覆盖完整恢复。
预检时校正了验收宿主的 canonical owner 身份；此前只有本地配置解析，没有额外模型请求。

样本通过原 ConversationStore 与 JsonlMemory 写入独立私有测试子目录，再由原 collect_curator_inputs 收集：
4 条普通中文短消息、3 条短且完整的版本 1 正式 long-term 条目，共 12 个独立关系问题。
只读 HTTP 观察器调用原 `post_json`，核对官方端点、`max_retries=0` 与 `allow_redirects=false`；
私有证据保存请求/响应和脱敏账本摘要，不保存 HTTP 密钥头，不在仓库记录原正文或私有配置。

| 对照 | 实际 HTTP 尝试 | 调用者耗时 | 结果与原输入 |
| --- | ---: | ---: | --- |
| apply | 1，成功响应 | 1.584112 秒；HTTP 1.529304 秒 | 12 条合法临时关系；原材料不变，正式文件不变 |
| off | 0 | 0.007658 秒 | 无注释，原 Curator prompt 字节完全相同 |
| 50ms caller deadline | 1，未获响应 | 0.054645 秒 | `apply:deadline`，无注释，原 prompt 字节完全相同 |

本轮新增 **2 次官方 HTTP 尝试，1 次成功、1 次局部期限取消**，没有重试。
成功样本的预期重复、更新时间、冲突三对分别返回 possible_duplicate、possible_update、possible_conflict；
其余九对均 no_match。12 个结果符合这些预设短样本，但不能外推真实长期语料的准确率或全库去重效果。

成功响应报告 input_tokens=6,771、output_tokens=810，原账本对应 finished、1 次 provider attempt、
purpose=decision、auxiliary=true，run 属于本次后台测试且 thread 为空；两项字段均记录为供应商已报告。
超时调用对应 timed_out、wall_clock、1 次 provider attempt；传输收到准确取消后抛 InterruptedError，
没有响应或 usage。它显示的零累计值不代表服务端报告零，也不能证明远端未收到或未计费。

apply/off/期限三组都继续通过原 `extract_with_retries`、原输出 schema 和严格解析做本地捕获对照，
相同固定提取响应得到相同结果，输入身份与原材料均保留。本对照的提取后端是本地测试替身，
没有调用普通生成模型或执行真实晋升；因此证明的是“增强不会改写原材料/合同”，不证明真实 Curator 的后续提取质量。
正式候选提交及晋升权限仍以此前原链定向测试为证据。

私有证据索引：测试目录内 `p5b/20260922T200705/summary.json`、`request-1.json`、`response-1.json`、
`request-2.json`；没有 response-2，因为期限内未取得响应。独立入口为同目录 `p5b/validate_relations.py`。
本轮未发现需修改源码的产品缺陷；不代表后台持久用量结算、实际 TUI、本地 I/O 阻塞或所有关系质量已验。

## 主线重点复查与剩余风险

- 这是提取前的关系提示，不是直接对已保存候选做语义分类/合并；没有用 Jev 置信度修改任何 promotion 权限。
- 首片只支持完整消息和短、完整、带真实版本 long-term 条目。lesson/HOT、audit 大正文、无版本和截断仍走原链。
  本批正式目录本来就是有界的，任何 no_match 不能解释成全库无重复/冲突。
- 原本地同步仓库读取没有强杀保证；同一绝对期限在读取前后复核，迟到不再请求或采用。未增加另一个线程池。
- 真实 Jev 仅验证上述 12 对短样本，尚无总体质量/时延结论；真实 TUI 操作本点、部署和独立后台用量持久结算未验，
  原模型账本记录并不创造会话归属。
- 原提交/晋升仍复核证据与版本；本片临时提示不是提交时版本授权，外部并发修改继续由原权威链决定。
- 设置文件已释放给主线追加 `external_material_order`；它不属于本片，不把后续 diff 误算为本片验收。
  LLM_GUIDE、DESIGN_LEDGER、总 Goal/TESTS/ROADMAP 等共享导航由主线统一收口，本片未标完整 P5-B 已完成。

## 建议下一步

主线先复核共享配置登记及本轮真实小样本证据，再决定是否扩大含模糊/冲突来源的 Curator 质量对照；
保留原输出，不把“协议合法”或本轮 12 对正确当作全库效果。可以与独立外部材料排序片并行，
但 Curator 输入/正式仓库和设置文件需要继续明确认领，不能并行增加候选状态或放宽晋升边界。
