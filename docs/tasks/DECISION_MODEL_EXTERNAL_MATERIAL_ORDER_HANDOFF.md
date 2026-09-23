# P5-C 外部材料阅读优先级：第一有界切片交接

## 基本信息

- workstream：决策模型 P5-C 已抓取网页的临时阅读顺序建议。
- branch：`codex/decision-model-integration`。
- worktree：主线协调的 `decision-model-plan` 独立工作区；基线 `ef497a90481203b7941c79b1d640142545173808`。
- owner：`decision_transcript_compact`；只交接本节列出的文件及函数。
- date：2026-09-22。
- 状态：本地候选；已做隔离真实 Jev 的首轮合同验收，未提交、未推送、未部署，没有总体质量结论。

## 本线目标

在原工具执行和归档结束后，允许独立决策点给多页材料提供可忽略的阅读优先级。
原页面、下载顺序、失败项、来源引用、工具状态和执行权限仍由原工具链负责。
本片只覆盖 `web_fetch mode=extract` 的已归档 `pages`，不宣称完成所有检索或外部知识排序。

## 实际完成

- `external_material_order` 注册为 thread 接入点，agent 域默认 `off`；原 owner/thread 设置服务、
  主代理 `user_config` 工具与 TUI 共用 `mode / timeout_seconds / profile_id`，保存不发模型请求。
- 用户可事先配置开关；运行中的判断、合法建议采用和失败回退均自动完成，不要求每次人工选页序或确认。
- `_record_tool_call` 在原工具账和归档写入后调用纯适配器，只将有界提示追加到同一
  `result_rendered`；text 与 native IR 的 model-facing projection 消费相同字符串，
  canonical `ToolResult.output`、归档、完整页面与 refs 不变。
- 只有 canonical 成功执行回执、原 `external_data/default` 安全投影、当前 run/task 与归档
  call/hash 配对均合法时才进入增强；不从任意 JSON 正文提取候选，不接受重放或不明来源。
- 关闭或未注册时不编码页面或当前问题。开启后仅发送原已读 `title/preview`、页序 ID、内容 hash
  和本轮 `user_prompt` 的原安全投影；不发送原调用参数、URL 字段、headers/body、路径或未读正文。
  摘录中带完整或协议相对 URL 查询串时放弃建议，原脱敏和外部数据边界继续生效。
- 复用 `begin_decision_stage / decide / decision_outcome_is_current`、原 bounded worker、
  配置撤销和唯一模型调用账本。准备、请求和采用沿同一绝对阶段期限，不建立缓存或第二份账。
- 页集合、来源 hash/refs、当前任务/问题、权限快照或有效配置在等待期间变化就不采用。
  只接受完整、唯一、无逐题错误的优先级选择；`need_data/not_needed/no_match/abstain`、
  不完整回答和原序未变化均保留原展示，不补读材料。
- 提示只含校验后的页序数字，最多 1,024 字符；不复制模型自由文本或外部指令。
  可选 provider/worker 超时保留原结果，真正的宿主取消仍传播，包括与增强失败同时发生的取消。

## 改动文件

- `agent_py_agent/agent/agent_core/tool_context/external_material_order.py`：独立可选适配器。
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`：仅模块注释与 `_record_tool_call` 的附加展示接缝；
  同文件其余完整请求投影改动属于主线，不在本片交接范围。
- `agent_py_agent/agent/settings/config.py`、`agent_py_agent/config/agent_config.yaml`：仅本点三个默认字段。
- `agent_py_agent/agent/settings/decision_settings_schema.py`：仅本点 thread 注册。
- `agent_py_agent/cli/chat_parts/tui_decision_menu.py`：仅本点中文菜单名称；复用原表单。
- `agent_py_agent/tests/test_decision_external_material_order.py`：输入/来源/取消/采用及 text/native 接缝。
- `agent_py_agent/tests/test_external_material_order_integration.py`：原生产链、服务账本、配置工具、真实控件输入。
- `agent_py_agent/tests/test_tui_decision_menu.py`：通用测试 helper 改为有界 UI 状态等待，产品菜单行为不变。
- `docs/modules/verification/02-progress.md`、`04-structure.md`、本交接、`CODEBASE_TREE.md`：本片职责和验证边界。

## 合同与参考核对

先核对 `docs/design/network-tools.md`、原 `WebFetchTool._execute_extract`、
`web_fetch_runtime.page_payload_from_response`、`ToolExecutor._canonical_result`、
`archive_tool_output_projection` 与 `_record_tool_call`。原 extract 的下载、大小上限、归档和失败项
均保持唯一生产链；本片不修改 producer、schema、权限、ledger 或下载顺序。

对照本地参考项目的 `hermes-agent-main/agent/web_search_provider.py` 及
`codex-main/codex-rs/ext/items/src/web_search.rs`：只采纳结构化来源与独立展示的边界。
已检索对应合同文件索引中的 web_search/web_fetch/external data 词，未命中相关索引条目；
该证据仅限上述源文件，不代表完整逐行审查，也不能证明新增建议提升质量。

`web_search` 现有 `_SearchResult` 经规范化/过滤/去重进入 `_search_payload`，但没有可供此消费者直接
复用的 typed candidate envelope；因此不解析正文另造权威。`search_text` 的 path/line/cursor、
`session_search` 的历史 refs、外部知识配置登记也各守原合同，本片均未接入。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_external_material_order.py agent_py_agent/tests/test_external_material_order_integration.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_scope.py agent_py_agent/tests/test_tui_decision_menu.py agent_py_agent/tests/test_user_config_decision_operations.py agent_py_agent/tests/test_tool_context_reducer.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_native_tool_use_ir_messages_flow.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py agent_py_agent/tests/test_decision_skill_tool_settings.py -o addopts='' -q --tb=short
ruff check agent_py_agent/agent/agent_core/_tool_loop_service.py agent_py_agent/agent/agent_core/tool_context/external_material_order.py agent_py_agent/agent/settings/config.py agent_py_agent/agent/settings/decision_settings_schema.py agent_py_agent/cli/chat_parts/tui_decision_menu.py agent_py_agent/tests/test_decision_external_material_order.py agent_py_agent/tests/test_external_material_order_integration.py agent_py_agent/tests/test_tui_decision_menu.py
python3 scripts/check_doc_sync.py
git diff --check
```

最终联合结果：**349 passed / 22.76 秒**；上述 Ruff、doc sync、diff 检查通过。
另外使用 `check_code_size._check_ast / compute_strict_blockers` 对本片五个生产文件做只读定向检查，
沿原 `CODE_SIZE_BASELINE.json` 得到 **0 个 strict blockers**，未写报告或基线。
本片是本地候选，未跑远端提交前全套 gate；上述自动验证未使用线上 CI 或真实模型服务。

此前新提示及设置组 175 项通过；扩展到原工具渲染链的首轮为 311 passed、1 failed，
原有 TUI pipe CAS 用例读取到旧超时值，单例立即重跑 1 passed。第二轮补充查询串/取消用例后为
318 passed、1 failed，同一个 CAS 用例更早停留在上一层菜单，表明固定 120ms 等待不能证明控件就绪。
主线授权将通用测试 helper 改为 3 秒截止时间内等待真实 UI 状态：排除原只读请求等待控件，
以 `after_render` 证明新浮层或编辑内容已绘制，不重发按键、不改产品。
修复后菜单及两个 helper 调用方 54 passed / 4.99 秒，再由上述 349 项联合覆盖。

组合测试保留真实 extract → Executor → archive，唯一替换网络响应；验实际 artifact hash 与增强前后
文件字节不变。服务测试保留真实设置、绑定、有界 worker、响应解码和用量账，唯一替换供应商调用。
TUI 测试走实际 prompt_toolkit 输入管道，但使用本地 Gateway stub；不能写成真实远程 TUI 或真实模型验收。

## 首轮隔离真实 Jev 验收（2026-09-22）

使用既有 `decision-model-live` 私有 owner/profile，在新的私有 thread 上经原 CAS 临时切换本点。
正式请求地址核对为 `https://api.typesafe.ai/v1/systemone`，请求模型 `jev-latest`，成功响应实际模型
均为 `jev-1.13.0`。8431 全程保持停止；未访问日常 8420、未改原 checkout 或日常模型。

每个独立样本只提供一次普通中文资料阅读需求，对 off/observe/apply 复用相同需求与相同页面内容。
页面是自建的受控本地 HTTP 响应，保留原 `web_fetch extract → ToolExecutor → archive → _record_tool_call`
及 text/native 消费链；不能称为真实互联网检索。Jev 没有替身：观察器只包裹原 `post_json` 和消费者，
保存脱敏事实并原样返回真实响应；每次均 `max_retries=0`、禁止重定向。

| 样本/模式 | 实际 HTTP 尝试 | 消费者耗时 | 输入 token | 展示结果 |
| --- | ---: | ---: | ---: | --- |
| 初始资料 / off | 0 | 0.010395 秒 | — | 原展示字节相同 |
| 初始资料 / observe | 1，成功 | 0.913326 秒 | 2,108 | 原展示字节相同 |
| 初始资料 / apply | 1，成功 | 0.922038 秒 | 2,108 | 非选择保护，原展示字节相同 |
| 初始资料 / 50ms 接入点期限 | 1，未获响应 | 0.059684 秒 | 未知 | 原展示字节相同，宿主未取消 |
| 初始资料 / 清空本 thread 模型引用 | 0 | 0.009210 秒 | — | configuration_required，原展示字节相同 |
| 故障恢复资料 / off | 0 | 0.001931 秒 | — | 原展示字节相同 |
| 故障恢复资料 / observe | 1，成功 | 0.854889 秒 | 2,157 | 原展示字节相同 |
| 故障恢复资料 / apply | 1，成功 | 0.839642 秒 | 2,157 | 自动只追加 `2 → 3 → 1` 阅读提示 |

初始资料包含一页无关活动记录。真实 observe/apply 均返回 `not_needed / first / first`，
按现行非选择合同放弃整组提示；此轮未命中采用分支，原答案和未采用事实均保留，没有改写答案或放宽规则。
补充的独立故障恢复样本按一次普通中文需求区分即时处置、前置检查和恢复后监测，三页均与任务相关。
真实 observe/apply 均返回完整 `later / first / first`；apply 自动追加 `2 → 3 → 1`，没有人工挑选或确认。

合计 **5 次官方 HTTP 尝试、4 次成功、1 次短期限取消**。成功请求原账中报告的输入合计 **8,530 tokens**，
逐次与真实响应一致，状态为 finished、purpose=decision、每次 1 个 provider attempt。
超时原账为 timed_out / wall_clock / 1 个 attempt，没有已报告输入，不能把未知写为零。
四次成功 HTTP 本身耗时分别为 0.873585、0.904776、0.841765、0.827417 秒；
短期限传输由准确取消结束，HTTP 观察耗时 0.056312 秒。本点未新增价格或计费统计。

所有组都在增强前后比较原 canonical result、归档与 refs、页面内容 hash、归档文件字节、工具调用账
和工具观测账，结果一致；text/native 使用同一展示，native 的原 `output` 保留。
普通只读抓取没有 runtime_gate/approval，原 gate 表正常为空并保持为空，不补造 gate 记录。
最早一次 off 预检曾误将 gate 表非空作为脚本断言，0 次 HTTP 后终止；修正的是验收脚本的观察口径，
没有改产品，也没有把失败算作实测通过。

全部私有 thread 覆盖已按 CAS 恢复；owner 模型配置文件逐字节不变，8431 收尾仍无监听。
私有记录不保存请求密钥头，响应证据只摘取模型、答案和输入 token；原密钥扫描通过，文件权限检查无异常。
证据位于测试目录 `p5c/20260922T203307/`、`p5c/20260922T203547/`，各含 `summary.json`、
实际请求/响应和原展示对照。0 HTTP 预检保留在 `p5c/20260922T203043/`；
汇总为 `p5c/acceptance-index.json`，独立入口为 `p5c/validate_material_order.py`。

该轮只证明真实 Jev 的不采用保护、完整优先级自动追加和原来源不变。页面源是本地受控材料，
宿主是隔离验收入口，不是安装版 Gateway/TUI；没有调用普通生成模型完成后续研究报告。
清空引用只覆盖真实配置错误回退，不代表真实 HTTP 4xx/额度失败已验；没有总体质量、p95、
慢本地 I/O、部署或其它检索点结论。未发现需修改产品源码的缺陷。

## 影响范围

独立开关默认关闭。`observe` 会发可选决策请求并记录输入 token，但不追加提示；本片不新增价格或计费。
`apply` 只影响当前模型读取结果的
参考顺序，不重排/删减原数据，不成为运行路由、授权、执行状态或验收事实。原结果即使失败或被拒绝
增强也按原路径消费。无新第三方依赖、存储、后台任务或隐式抓取。

## 需要主线重点复查

- 原结果、工具账及恢复引用仍是唯一来源，native 仅复用 model-facing projection。
- 关闭不准备正文；启用后的本地版本摘要不进入 provider payload；查询串/未知来源保留原结果。
- 当前设置撤销、同一绝对期限及宿主取消没有被可选建议吞掉。
- 主线统一更新总设计/目标/测试导航；本交接不能把 P5-C 首片写成全部范围完成。

## 需要其他线协调

交接后释放本片文件/函数 ownership。共享 `_tool_loop_service.py` 只认领过上述 `_record_tool_call` 接缝，
不覆盖其它 agent 的 renderer 改动。记忆、child/background、能力展示及新检索 schema 不属于本片。

## 剩余风险

真实 Jev 小样本已覆盖自动采用，但尚无足够证据证明阅读优先级改善相关性、token 或质量。
工具输出的原生 live projection 可随原 IR 保存，但不是新候选库或权限事实，不负责恢复时重新决策。
尚未覆盖 `web_search`、本地检索、历史检索及外部知识结果排序；后续需要原生产方的明确结构化候选和消费合同。

## 建议下一步

先由主线汇总本片真实 5 次尝试与原来源/取消边界，再决定是否扩大到安装版 TUI 和更多普通中文材料。
总体效果未证明前保持默认关闭。下一片检索合同审计可并行，只读原 producer 和消费方；不要并发修改
同一 `_record_tool_call` 或把当前提示升级为删页、抓取、验收或授权依据。
