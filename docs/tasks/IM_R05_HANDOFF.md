# R05 零结果查询交接

## 基本信息

- workstream：IM R05，延续 R06 后的共享 checkout 文件独占。
- branch：`codex/im-entry-update-assessment`。
- 日期：2026-10-14。
- 状态：本地实现/定向验收；没有提交、推送、部署、真实供应商请求或生产 IM 外发；此前 R06 保持冻结。

## 实际完成

统一 `query_evidence.v1` 如实记录实际条件、返回/观察数量、已知/未知总数、覆盖、解析版本和来源未核验。零结果 `review_required` 只作结构化观察，canonical PromptBuilder 在主/子覆盖/群/后台/native 同一 system 入口加入可关闭的“最多一次同权限同来源同时间有界基线”软指导。没有硬回复检查、中文词表、自动基线、权限扩张、全量扫描或无限重试。

本地搜索保留原 search_result/page_window，补真实 query 和所有有效筛选参数；坏 rg JSON 不能误报零集合，改沿原 Python 后备实查。网页搜索只证明 provider/parser 候选窗口，含实际域名过滤和逐 provider 返回/失败，源端总数与覆盖未知。

来源 HTTP 实际 render 请求可见 URL/query/JSON body 脱敏视图；所有 header 值、显式 secret slot、SecretRef 与 credential_ref 值不进入新增事实。adapter 记录数归属于 pinned sha256 parser。原 probe 两页上界不增加请求，probe 观察与 live pull 分开；DrainResult 只瞬时携带最近真实请求，原 pull 回执投影，不创建第二账本或改变源配置 hash/游标提交。

## 改动文件

- `agent_py_agent/agent/tooling/query_evidence.py`：共享纯事实/指导。
- `agent_py_agent/agent/tooling/_filesystem_search.py`：真实查询与计数投影、坏 rg 防假零。
- `agent_py_agent/agent/tooling/web_search.py`：窗口、实际过滤、provider/parser 观察。
- `agent_py_agent/agent/prompting_parts/builder.py`：canonical system 与 override 的共享开关。
- `agent_py_agent/agent/ingestion/source_http.py`：实际请求公开脱敏与统一来源观察。
- `agent_py_agent/agent/ingestion/source_adapter.py`：声明协议 parser 页观察。
- `agent_py_agent/agent/ingestion/watch_tool.py`：probe/live 原回执、原两页 continuation 拆小。
- `agent_py_agent/agent/ingestion/puller.py`：最近实际游标/页长观察，DrainResult 瞬时字段。
- `agent_py_agent/agent/ingestion/sources.py`：真实 adapter/poll 观察、未到节拍不虚构。
- `agent_py_agent/tests/test_query_evidence.py`：24 项直接事实/真工具循环假模型回放。
- `agent_py_agent/tests/test_ingestion_watch_tool.py`：旧 AdapterPage 测试替身补新观察字段，未加产品兼容旁路。
- `docs/design/QUERY_ZERO_RESULT_REVIEW.md`：合同与边界。
- 本交接。

## 配置与根协调

根负责 `AgentConfig.query_zero_result_review_enabled=true` 与默认 `agent_config.yaml` 中文注释/中央配置测试及 DESIGN_LEDGER/ROADMAP/TESTS/CODEBASE_TREE。读取点唯一是 `query_zero_result_guidance(config)`，由 PromptBuilder `prepare_render_input` 使用。关闭只停止新增模型指导；真实结构事实继续输出。没有 runner prompt 第二份副本或新 capability 参数。

## 验证

- 组合：新 query_evidence + source_adapter、ingestion_source_http/puller_cursor/sources/watch_tool、tooling_filesystem/web、prompting_builder：264 passed in 10.76s（当时新文件 23 项）。
- 随后仅新增“查询回执不改变来源配置权威 hash”用例，末次新文件 focused 回归为 24 passed in 1.90s。
- 改动文件 Ruff 和 `git diff --check` 通过。
- strict code-size 使用 `--report /tmp/im-r05-code-size-report.md`，`hard=0 blocked=False`；没有写共享 CODE_SIZE_REPORT.md。

前稿两个 fake LLM 用例失败于测试准备错误：原工具是动态加载且实际 cwd 为 owner home，修复测试沿原 `tool_search` 和真实 owner fixture 文件；不是把产品变成旁路。组合曾 260 passed/1 failed：旧 SimpleNamespace adapter 页替身缺新 query_evidence，补替身后联合全绿；没有增加产品“缺字段就猜事实”兼容分支。

## 边界与主线重点审阅

- 软指导不能保证真实模型一定做基线；仅有 scripted fake LLM 走原 native 工具循环证明指导/事实可达及一次回放有效，不外推真实模型成功率。
- 未声明协议的 shell/custom script/MCP 不被包装成 verified 查询事实。未知/截断/分页/provider 失败不当正常全源零结果。
- poll 计数单位是整份快照，不猜内部列表业务条数。adapter 即使 has_more=false 也只证明其输出窗口，source_truth 恒未核验。
- source 阻断继续走原失败码、原权限/网络出口；没有补请求、重试、异步告警或持久配置副本。
- 来源 probe 两页拆分仅为尺寸守卫，原网络失败码与未提交游标语义保持。

## 建议下一步

根先复查真实条件脱敏、当前 pull 与旧 probe 区分、未知覆盖及 canonical prompt 开关，再跑九项总 focused/严格 gate。其它 agent 可并行只读审计归档与子代理提示投影，不能同时写本线文件或在测试期间覆盖共享报告。真实验收只观察被测对象自身查询/记录，不替它执行基线补证；根独占后续发布生命周期。
