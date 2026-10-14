# 查询零结果的结构事实与有界复查

状态：R05 本地实施，假模型/原工具循环与相邻模块定向验证；没有真实模型、来源网络或生产 IM 外发。

零返回只说明本次条件下工具或解析器没有返回候选。来源总数、来源端覆盖、网页解析是否完整、分页之外是否还有数据都必须单独标明；模型不能把零返回直接写成“来源不存在”。这一合同覆盖个人、群、管理员、主代理、子代理覆盖提示与后台执行，不按具体任务或中文词表增加判断分支。

## 统一观察

原 `ToolHandlerOutcome.result_envelope` 与工具回执保留 `query_evidence.v1`，继续走原 ToolResult metadata、原生历史和原工具输出归档，没有第二查询账本。字段包含来源、实际生效条件、`returned_count`、`observed_count`、计数单位、`total_count/total_known`、覆盖范围、解析版本、来源真实性未核验、错误和 `review_required`。复查描述只含最多一次基线的软预算，`automatic_request=false` 和 `authority_change=false`。

- 本地文本搜索使用真实 query/path/glob/literal/大小写/忽略目录/分页/输出模式及原路径权限过滤；全枚举完成才给已知总数，命中页有更多或后备扫描提前停止时总数未知。越过末页但此前有匹配是 empty_page，不能作为全源零结果。
- ripgrep 坏 JSON 不再静默返回空集合，沿已有 Python 后备执行真实匹配；后备扫描有界上限仍明确 incomplete。
- 网页搜索只证明 provider/parser 返回的候选窗口，互联网总数和完整覆盖恒未知。过滤后为零保留原域名条件和 provider 实际返回数；挑战页/空解析不能成为可靠全源零结果，provider 失败保留原 TOOL_UNAVAILABLE。
- Source HTTP 观察只在真实 render/fetch/已声明解析边界之后生成。实际 URL/query/JSON body 脱敏，header 只显示名称；显式秘密绑定位置、SecretRef/credential_ref 内容、认证 header 值不出现在新增观察中。
- pinned adapter 的 sha256 是解析版本；返回数归因于该 parser，`has_more=false` 也不代表源端全部无数据。poll 整份响应计为 response_snapshot，禁止从未声明的 JSON 子数组或业务过滤猜条数。

每轮来源 drain 仅瞬时携带最后真实请求的观察，pull 回执随原聚合投影；未到 poll 节拍或网络拒绝时不虚构新查询。原 probe 最多两页的结构验证不增加请求，保存原 source_envelope 中的观察明确属于探针；live pull 的当前观察在原回执顶层提供，不用旧探针条件冒充本次游标/页长。

只读参考核对 Codex 的 `codex-rs/ext/web-search/src/tool.rs`：保留结构 action/query、可选 results 和原工具事件；未把展示正文或“零”字样做成业务判定。本仓库沿已有 typed search_result、原 envelope 与归档扩展观察，未另复制搜索状态源。

## 共享软指导

`query_zero_result_review_enabled` 默认 true，由 AgentConfig/默认 YAML 唯一控制。canonical `PromptBuilder.prepare_render_input` 在原 system/override 上追加一次共享指导；子代理覆盖、后台和 native/cache 路径同源，runner 不复制一份。关闭只移除模型指导，真实工具事实仍照常输出。

首次查询或条件变化后零结果，模型先检查准确字段/接口键、原来源、时间、分页和错误；权限、网络和预算允许时，在同 owner/来源/时间范围内最多做一次有界基线，移除未经证实的额外 AND 条件。已有可靠基线可复用；不能自动全扫、扩权限、无限重试，不能仅因为中文而判查询不合法。普通零结果不触发单独 IM 告警。网络不可用或不足以核验时由模型如实表达未知，不增加最终回复词句硬门。

shell、自定义脚本或 MCP 没有显式声明查询协议时，不猜 stdout 中的查询条件/计数，不造 verified 事实。本轮没有改 shell/MCP 来尝试这种推断。

## 验证边界

新回归涵盖精确条件与合法中文、AND 初查→基线阳性、中文零结果→未知、原 native tool_search→search_text 工具循环、全 owner/覆盖提示/native 开关、分页末页/扫描不全/坏 rg、provider 失败/过滤/provider 观察、已渲染 SecretRef 槽与认证值脱敏、pinned parser 与源端真值区分、live 游标/页长/原时间字段、未到期 poll/网络拒绝不发基线、原 pull 回执投影。

软指导不是模型一定复查的保证；没有真实供应商对照实验或生产来源事实。继续以原日志、请求、ToolResult 和归档作为验收来源。
