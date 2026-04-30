# Log Analysis Backlog

这份文档记录“长期后台日志分析助手”的功能规划。

目标不是让大模型直接吞几十 GB / 小时的原始日志，而是先做一个能持续接入、去重、解析、索引、检测和派遣 agent 研判的底座。大模型和 subagent 只处理证据包、异常 case 和补查任务。

## 一句话结论

日志分析底座应该分成三层：

```text
数据底座处理全量日志
检测/ML 底座产出异常候选
agent 底座负责派遣、研判、验收和报告
```

第一版先做本地可运行的 `file/api/syslog/kafka -> normalized events -> parquet/duckdb -> detectors -> cases -> analyst subagents`。后续通过统一接口接 ClickHouse、Kafka、Flink、Ray、Spark、远程 ML 服务和集群 worker。

## 本轮共识总结

这个新模块不是“帮用户写 SPL 的 SIEM 查询助手”，而是“会自己找证据、派子代理、串攻击链、说明缺口的安全分析助手”。

已经确认的方向：

1. 第一版字段先按 `SecurityAlertV1` 做基础告警模型，使用用户给定字段，不一开始强行套 ECS/OCSF。
2. 字段统一可以后续逐步增强，采用已确认字段 + LLM 辅助映射 + 人工确认高价值字段。
3. 检测不走强规则为主，而是软检测器优先。软检测器只产出候选、证据、缺口和下一步查询。
4. LLM 是分析核心，不只是报告润色。LLM/agent 负责补查计划、多源解释、攻击路线、缺失证据、误报分析和派遣子代理。
5. 反查工具本质是受控日志查询，必须限时间、限结果、保存 query 和 evidence。
6. 主代理不能持续干活，主代理只做调度、验收和会话健康维护；重活交给 worker 和 subagent。
7. 安全分析功能必须独立配置、默认关闭，不影响普通用户使用。
8. 安全 prompt 必须有开关，默认不注入，只有 security/logs 命令或安全子代理场景才启用。
9. 基础款先跑本地轻量链路，后续所有存储、查询、ML、集群、SOAR 都通过接口替换，不写死。
10. 真实样本后续再喂，但基础款必须先有最小 fixture，确保链路能 replay。

基础款的价值目标：

```text
导入一批 SecurityAlertV1 日志
-> 通过软检测器找出可疑 case
-> LLM/agent 调受控查询补证据
-> 生成 route draft、gaps、next_queries
-> reviewer 验收证据
-> 输出第一响应报告
```

## 硬目标：3 分钟内发现和反推入侵路线

最终目标非常明确：当发生入侵成功、横向移动、被攻击、数据外传或正在被持续攻击时，系统必须能在 3 分钟内完成第一响应闭环。

3 分钟内必须产出：

- 告警和安全 case。
- 受影响资产、账号、IP、进程、文件、域名的第一批实体列表。
- 初步入侵点判断，例如 VPN 凭证滥用、WAF exploit、钓鱼落地、弱口令、暴露服务、恶意文件执行。
- 攻击路线草图：时间、动作、源实体、目标实体、证据引用。
- 横向移动或扩散迹象。
- 还缺哪些日志源、字段、时间窗口或证据。
- 下一步自动反查 query plan。
- 处置建议，默认只 recommend/dry-run，不自动执行高风险封禁。

3 分钟内不要求把全部取证做完，但必须给出“够用的第一判断”和“可继续追的证据链”。完整 forensic package、深度溯源、复盘报告可以在后续 10-30 分钟持续补齐。

关键约束：

- P0/P1 不等待 LLM 完整研判，快路径 detector 先建 case。
- 攻击链第一版可以是不完整的，但必须显式列出 `facts`、`inferences`、`gaps` 和 `next_queries`。
- 如果无法判断入口点，必须输出候选入口点排名和缺失证据，不能静默失败。
- agent 的职责是补证据和解释，不允许阻塞实时检测主链路。

## 0day 入侵发现能力

0day 的难点是没有稳定 IOC、没有现成 CVE 规则、没有固定利用特征。系统不能承诺“识别所有 0day”，但必须具备发现未知入侵成功和异常攻击链的能力。

0day 发现不靠单点签名，靠这些能力叠加：

```text
暴露面和资产上下文
+ 应用/运行时异常
+ 行为基线偏离
+ 多源弱信号关联
+ 成功利用后痕迹
+ 诱饵和 canary
+ 沙盒/样本分析
+ 回溯狩猎
+ agent 快速补查
```

### 0day 快路径信号

第一批要重点监测这些“未知漏洞成功利用后”信号：

- WAF 只命中通用异常，不命中已知 CVE，但同窗口 Web 服务器出现异常进程、文件写入或外联。
- Web/API 请求命中罕见路径、罕见参数、异常长度、异常编码、异常方法，随后服务端报错、崩溃、重启或延迟突增。
- Web server、Java、Python、PHP、数据库、中间件进程启动了不符合基线的子进程。
- 服务器从“从不外联”变成访问罕见 IP、域名、ASN、国家或固定周期外联。
- HIDS/EDR 发现新文件落地、权限变化、计划任务、服务创建、webshell-like 文件变化。
- 同一攻击源在多个资产上触发相似异常，但具体规则名不同或没有规则名。
- 沙盒判恶的 hash 曾经在某台服务器落地或执行。
- 账号、主机、进程、网络四条线同时出现低置信异常，单看都不严重，但组合后像入侵成功。
- 日志突然缺失、agent 被停止、审计策略变化，这本身也作为防御绕过信号。

这些信号只产出 `unknown_exploit_candidate` 或 `suspected_zero_day_intrusion`，不直接断言漏洞编号。

### 0day 数据增强

要发现 0day，必须比普通日志分析多收几类信号：

- Web access + error log + WAF raw event，不能只要 WAF 最终告警。
- 应用运行时异常：异常栈、500、崩溃、重启、OOM、超时、异常 latency。
- EDR/HIDS 的 process/file/network/registry/service/task 事件。
- DNS/proxy/NetFlow 出站行为。
- 资产指纹：应用类型、框架、端口、版本、暴露面、owner、重要性。
- 部署/变更窗口：发布、扩容、配置变更，用于降低误报。
- Canary token / honey path / honey account 命中。
- 沙盒 verdict、文件 hash、下载来源、执行路径。

缺少关键遥测时，系统要能把 `missing_telemetry` 写入 case gaps。例如“疑似 Web 利用成功，但目标服务器缺少 EDR 进程事件，无法确认命令执行”。

### 0day 检测器类型

第一批 0day detector 不写具体漏洞 payload，而写行为模式：

- `web_to_process_anomaly`: Web 请求后服务端异常子进程。
- `web_to_file_write_anomaly`: Web 请求后敏感目录新文件或脚本文件变化。
- `web_to_egress_anomaly`: Web 请求后服务器罕见外联。
- `app_error_to_host_behavior`: 应用异常后出现主机行为异常。
- `rare_endpoint_cluster`: 多资产出现相似罕见路径/参数/错误。
- `silent_edr_gap`: 高危窗口内 EDR/HIDS 心跳或日志突然缺失。
- `canary_hit`: honey URL、honey token、honey account 命中。
- `unknown_binary_execution`: 低信誉/首次出现二进制执行。
- `post_exploit_sequence`: exploit-like request -> process/file/network -> persistence。

每个 detector 都必须输出：

- 触发窗口。
- 相关资产和实体。
- 支持证据。
- 反证或误报解释。
- 缺失证据。
- 下一步反查 query。

### 0day 研判输出

当 case 被标为 `suspected_zero_day_intrusion` 时，第一响应报告必须包含：

- 为什么不像普通已知规则命中。
- 哪些行为支持“未知利用或未知攻击链”。
- 是否出现入侵成功迹象。
- 初步入口候选。
- 影响资产和横向风险。
- 需要立即补采的证据。
- 需要回溯搜索的时间范围和实体。
- 是否建议临时隔离、加 WAF 临时规则、关闭入口或人工确认。

输出必须避免虚假的确定性。没有 CVE 编号时就写“未知 exploit 候选”，不要编造漏洞名。

### 0day 回溯狩猎

新情报、新样本、新规则、新 IOC 出现后，要能反查历史：

```text
new intelligence -> retrohunt query plan -> scan historical partitions -> update old cases or create new cases
```

回溯狩猎必须支持：

- 按 IP/domain/hash/URI/path/JA3/UA/process/cmdline 回查。
- 按行为序列回查，例如 Web 请求后 5 分钟内出现异常进程或外联。
- 按资产暴露面回查，例如所有某框架资产过去 30 天异常。
- 把新发现补回旧 case，保留版本和时间。

### 0day 验收标准

0day 能力不能只靠“有异常分数”。验收要用端到端 fixture：

- 未知 Web exploit 成功，WAF 只有通用异常，EDR 看到 Web 进程启动异常子进程。
- 未知应用漏洞导致服务器罕见外联，没有 IDS signature。
- 沙盒判恶 hash 先出现于服务器临时目录，再出现外联。
- 横向移动由弱信号组成，没有单条 critical alert。
- 关键 EDR 缺失，系统仍输出 gaps 和补采建议。

验收目标：

- 180 秒内创建 `suspected_zero_day_intrusion` case。
- 180 秒内输出 route draft 和缺失证据。
- 不依赖已知 CVE 名称、固定 IOC 或单一 vendor 告警。
- 第一响应报告至少关联两个日志源。

## 核心原则

- 原始大日志不进 prompt、不进 memory、不进 git。
- 所有原始数据只保存路径、hash、时间范围、schema 摘要、样本和证据引用。
- ingestion 默认至少一次投递，靠 checkpoint、dedup 和 idempotent write 避免重复影响结果。
- parser 和 detector 必须插件化，不能把某一种日志格式写死进核心流程。
- LLM/agent 只看小证据包，并且每个结论都要能追到 query、sample、partition 或 case 文件。
- 本地第一版用 DuckDB/Parquet 起步，但接口必须能换成 ClickHouse / object storage / stream processing。
- ML 是可插拔评分层，不是第一版必须绑定的核心依赖。
- 主代理不直接分析日志、不直接跑重查询、不直接长期占用会话；主代理只做调度、验收、升级和会话健康维护。
- 所有持续任务由后台 worker、detector 和子代理执行；主代理通过队列、case、evidence 和 acceptance 收口。
- 日志分析和安全监测必须是可选功能，默认不启用、不自启动、不改变普通用户的现有 `my-agent` 使用体验。
- 能力必须分级和接口化：本地轻量版能跑，后续能替换为 Kafka、ClickHouse、Flink、Ray、Spark、远程 ML、SOAR，不把 vendor 或引擎写死进核心。

## 目标场景

必须覆盖这些日志来源：

- 不断落地的日志文件：目录轮询、tail、按 glob 扫描、压缩文件、日志轮转。
- API 拉取：分页、时间窗口、游标、rate limit、失败重试。
- Syslog：UDP/TCP/TLS，本地接收后先落 durable spool。
- Kafka / Redpanda：topic、partition、offset、consumer group、手动提交。
- 后续预留：S3/MinIO、ClickHouse、Elasticsearch/OpenSearch、云厂商日志服务、Zeek/Suricata 输出、NetFlow。

必须覆盖这些运行形态：

- 一次性离线分析。
- 后台 watch 持续处理新增日志。
- 微批处理，比如每 1 分钟或每 5 分钟处理一批。
- 源源不断的流式输入，支持 backpressure、checkpoint、replay 和 late arrival。
- 单机本地运行，后续可拆成集群 worker。

## 全面实时安全监测目标

如果日志来源覆盖态势感知、HIDS、WAF、EDR、VPN、沙盒、服务器、网络设备、身份系统和业务系统，这个模块应该升级成“实时安全运营底座”。

核心目标：

```text
多源安全日志实时接入
-> 统一安全事件和实体
-> 实时检测和关联
-> 自动创建安全 case
-> 派遣 analyst agent 补查
-> 生成攻击链、溯源路径和取证包
-> 验收、报告、处置和复盘
```

必须支持的能力：

- 实时监测：P0/P1 高危事件不等待 agent 慢研判，先出告警和 case，再由 agent 补证据。
- 反查：拿到 IP、账号、主机、域名、hash、进程、URL、文件路径、漏洞编号后，能跨源回查历史。
- 溯源：从告警点反向追到入口、账号、资产、进程树、网络连接、文件落地、执行链和横向移动路径。
- 攻击链获取：把多个低层事件串成 kill chain / ATT&CK technique / timeline。
- 取证：固化原始证据引用、查询、样本、hash、时间线、截图/报告和 chain of custody。
- 处置闭环：生成 containment / eradication / recovery 建议，记录执行、验证和复盘。

## 安全传感器覆盖

第一类：网络与边界。

- 态势感知平台告警。
- NIDS / Suricata / Zeek。
- 防火墙 / NGFW。
- WAF。
- IPS。
- DNS。
- Proxy / SWG。
- VPN。
- NetFlow / VPC Flow Logs。
- API Gateway / LB / CDN。

第二类：主机与终端。

- HIDS。
- EDR。
- Windows Event。
- Sysmon。
- Linux auditd。
- osquery。
- 服务器系统日志。
- 进程、文件、注册表、服务、计划任务、登录会话。

第三类：身份与权限。

- AD / LDAP。
- IAM。
- SSO。
- MFA。
- VPN 账号登录。
- PAM / 堡垒机。
- 数据库审计账号。
- Kubernetes / 云控制台审计。

第四类：应用与业务。

- Web / Nginx / Apache / Tomcat / Java 应用日志。
- API 审计。
- 数据库审计。
- 消息队列。
- 管理后台操作日志。
- 业务风控日志。

第五类：情报与样本。

- 沙盒动态分析结果。
- 恶意文件 hash。
- 恶意 IP / 域名 / URL。
- YARA / Sigma / IOC。
- 漏洞扫描器。
- CMDB / 资产重要性。
- 外部威胁情报。

每个 source 都必须映射到统一字段：

```text
who: user/account/session/token
where: asset/host/ip/container/cloud resource
what: process/file/url/domain/action/signature
when: event_time/ingest_time
how: source product, parser, rule, detector
result: success/failure/blocked/allowed/quarantined
evidence: raw_ref/query_ref/sample_ref
```

## 统一安全事件模型

`NormalizedEvent` 是底层通用事件。安全运营要在它上面增加 `SecurityEvent` 视图。

建议字段：

```json
{
  "security_event_id": "sevt-xxx",
  "event_id": "evt-xxx",
  "source_id": "edr-prod",
  "source_product": "edr|hids|waf|vpn|sandbox|syslog|cloud",
  "event_class": "auth|network|process|file|dns|http|alert|vuln|sandbox|cloud|app",
  "event_action": "login|connect|execute|write_file|dns_query|http_request|blocked|detected",
  "event_outcome": "success|failure|blocked|allowed|unknown",
  "severity": "info|low|medium|high|critical",
  "confidence": 0.82,
  "asset_id": "asset-xxx",
  "host": "server-01",
  "user": "alice",
  "src_ip": "10.1.2.3",
  "dst_ip": "203.0.113.10",
  "process": {
    "pid": 1234,
    "name": "powershell.exe",
    "cmdline": "...",
    "parent_pid": 888,
    "parent_name": "winword.exe"
  },
  "file": {
    "path": "C:/Temp/a.exe",
    "sha256": "..."
  },
  "network": {
    "domain": "example.com",
    "url": "https://example.com/a",
    "dst_port": 443,
    "protocol": "tcp"
  },
  "rule": {
    "rule_id": "sigma-xxx",
    "rule_name": "Suspicious PowerShell",
    "technique_ids": ["T1059.001"]
  },
  "evidence_refs": ["raw-batch-xxx:line-123"]
}
```

统一模型必须能容忍字段稀疏。WAF 没有进程树，EDR 未必有 HTTP path，VPN 未必有 file hash。缺字段不能影响事件入库。

### SecurityAlertV1 基础告警字段

第一版基础告警先按用户给定字段落地，不急着强行映射 ECS/OCSF。导入时保留原始中文列名，同时生成稳定英文 key，方便查询、关联和后续代码使用。

| 中文字段 | 稳定 key | 说明 |
| --- | --- | --- |
| 告警类型 | `alert_type` | 设备或平台给出的告警类别 |
| 威胁名称 | `threat_name` | 威胁、攻击、漏洞或检测名称 |
| IOC/规则ID | `ioc_or_rule_id` | IOC ID、规则 ID、签名 ID 或策略 ID |
| URI | `uri` | 请求 URI、资源路径或接口路径 |
| XFF代理 | `xff_proxy` | X-Forwarded-For / 代理链 |
| Payload | `payload` | 命中的 payload 摘要或截断内容 |
| 域名 | `domain` | 域名、Host、SNI 或 DNS 查询名 |
| referer | `referer` | HTTP Referer |
| 目的端口 | `dst_port` | 目的端口 |
| 协议 | `protocol` | TCP/UDP/HTTP/HTTPS/DNS 等 |
| 受害资产组 | `victim_asset_group` | 受害资产所属组 |
| 攻击资产组 | `attacker_asset_group` | 攻击方资产所属组 |
| 受害IP | `victim_ip` | 被攻击或受影响 IP |
| 攻击IP | `attacker_ip` | 攻击来源 IP |
| 源IP | `src_ip` | 原始源 IP |
| 目的IP | `dst_ip` | 原始目的 IP |
| 检测位置 | `detection_location` | 网络边界、主机、云、应用、设备位置等 |
| 检测字段 | `detection_field` | 规则实际检查的字段 |
| 匹配 | `match_operator` | 命中方式，如 contains、regex、equals、ioc_match |
| 值 | `matched_value` | 实际命中的值 |
| 设备序列号 | `device_serial_number` | 安全设备或采集器序列号 |
| 告警规则 | `alert_rule` | 告警规则名称或策略名称 |
| API | `api` | API 名称、接口名或 API 资产 |
| API威胁类型 | `api_threat_type` | API 风险/威胁分类 |
| OWASP类型 | `owasp_type` | OWASP Top 10 或 API Top 10 类型 |

第一版最小 JSON：

```json
{
  "alert_id": "alert-xxx",
  "event_time": "2026-04-30T10:01:00Z",
  "source_id": "waf-prod",
  "source_product": "waf",
  "alert_type": "Web攻击",
  "threat_name": "疑似命令执行",
  "ioc_or_rule_id": "rule-1001",
  "uri": "/upload.php",
  "xff_proxy": "198.51.100.1, 10.0.0.1",
  "payload": "cmd=...",
  "domain": "www.example.com",
  "referer": "https://www.example.com/",
  "dst_port": 443,
  "protocol": "https",
  "victim_asset_group": "生产Web",
  "attacker_asset_group": "外部互联网",
  "victim_ip": "10.1.2.3",
  "attacker_ip": "198.51.100.1",
  "src_ip": "198.51.100.1",
  "dst_ip": "10.1.2.3",
  "detection_location": "边界WAF",
  "detection_field": "payload",
  "match_operator": "regex",
  "matched_value": "cmd=",
  "device_serial_number": "SN-xxx",
  "alert_rule": "命令执行检测",
  "api": "/upload.php",
  "api_threat_type": "injection",
  "owasp_type": "A03: Injection",
  "raw_ref": "raw-batch-xxx:line-123"
}
```

字段使用规则：

- 所有字段都允许为空，但 `event_time`、`source_id`、`alert_type`、`src_ip/attacker_ip`、`dst_ip/victim_ip`、`raw_ref` 能有就尽量有。
- `attacker_ip` 和 `victim_ip` 优先用于安全语义；`src_ip` 和 `dst_ip` 保留原始网络方向。
- `xff_proxy` 不能直接覆盖 `attacker_ip`，只能作为候选来源链，后续由 parser/enrichment 判定真实客户端 IP。
- `payload` 默认只保留截断预览和 hash，避免把敏感请求体或超大内容塞进 prompt。
- 原始中文列名要保存在 `raw_fields`，避免字段映射出错后丢证据。

和 `SecurityEvent` 的最小映射：

```text
alert_type/threat_name       -> event_class=alert, event_action=detected
src_ip/attacker_ip           -> src_ip / attacker entity
dst_ip/victim_ip             -> dst_ip / victim asset
domain/uri/api               -> network.domain / network.url / api entity
ioc_or_rule_id/alert_rule    -> rule.rule_id / rule.rule_name
detection_field/matched_value-> evidence match detail
owasp_type/api_threat_type   -> web/api threat taxonomy
```

第一版先用这个基础告警模型做导入、查询、关联和 case；后续再补 ECS/OCSF 映射。

### LLM 辅助字段映射

字段统一不需要第一版一次性定死。第一版采用“已确认字段 + LLM 辅助映射 + 人工确认高价值字段”的方式。

流程：

```text
raw fields
-> deterministic aliases
-> LLM mapping proposal
-> confidence score
-> confirmed mapping dictionary
-> SecurityAlertV1 / SecurityEvent
```

规则：

- 已确认字段优先，比如用户给定的 `SecurityAlertV1` 字段直接走稳定 key。
- 未知字段可以让 LLM 根据字段名、样本值、上下文和 source_product 提出候选映射。
- LLM 只能提出 `mapping_proposal`，不能静默覆盖原始字段。
- 高价值字段必须人工或规则确认后才进入强语义字段，例如 attacker_ip、victim_ip、user、host、event_time。
- 所有映射都要保存 `mapping_source`：`confirmed|alias|llm_suggested|manual_override`。
- 低置信映射只放进 `attributes` 和 `raw_fields`，不能参与 P0/P1 快路径判断。

大白话：LLM 可以帮忙“看懂字段”，但不能悄悄把字段改成事实。第一版先把你给的字段跑稳，其他字段慢慢通过 LLM 和人工确认补进字典。

## 实体图谱

为了做溯源和攻击链，必须有实体图谱，不只是事件表。

实体类型：

- `asset`: 主机、服务器、容器、云资源、数据库、业务系统。
- `identity`: 用户、服务账号、API key、token、session。
- `network_entity`: IP、domain、url、asn、country、port。
- `process`: pid、process_guid、cmdline、parent-child。
- `file`: path、hash、签名、沙盒 verdict。
- `vulnerability`: CVE、弱口令、暴露服务。
- `alert`: 原始告警、规则命中、ML finding。
- `case`: 安全事件调查单。

关系类型：

- user logged_in asset。
- process spawned process。
- process opened network connection。
- process wrote file。
- asset queried domain。
- IP resolved domain。
- VPN user mapped to src_ip。
- WAF alert mapped to web server。
- sandbox verdict mapped to file hash。
- case contains alert/finding/evidence。

第一版可以不用真正图数据库，先用 DuckDB/SQLite/Parquet 保存 edge table：

```text
entity_id, entity_type, relation, related_entity_id, time_range, source_refs, confidence
```

后续预留 Neo4j、ClickHouse graph-style query、OpenSearch graph、专门图分析服务。

## 实时处理链路

高危安全监测不能等完整批处理结束。

建议链路：

```text
collector/spool
-> parser/normalizer
-> enrichment
-> fast detectors
-> immediate alert/case
-> correlation
-> analyst agent
-> reviewer
-> report/response
```

快路径：

- WAF 拦截高危 exploit。
- EDR critical alert。
- HIDS rootkit / webshell。
- VPN 异常登录 + 新地理位置。
- sandbox malicious verdict。
- IDS exploit signature。
- 大规模扫描或爆破。

快路径要在秒级到分钟级创建 case。agent 研判可以随后补证据，不能阻塞告警。

慢路径：

- 周期性 beacon。
- 低频横向移动。
- 弱信号组合。
- 基线漂移。
- 数据外传候选。

慢路径可以按 5 分钟、15 分钟、1 小时微批。

建议 SLA：

| 等级 | 触发 | 目标 |
| --- | --- | --- |
| P0 | EDR critical、明确 exploit、恶意样本、勒索迹象、正在横向移动 | 1 分钟内 case，3 分钟内攻击路线草图 |
| P1 | 多源关联高置信、入侵成功迹象、横向移动、数据外传候选 | 3 分钟内 case、入口候选、影响范围和缺失证据 |
| P2 | 单源异常、基线偏离、可疑账号行为 | 15 分钟内 case |
| P3 | 趋势、低置信、待观察 | 小时级汇总 |

3 分钟预算建议：

| 阶段 | 预算 | 输出 |
| --- | --- | --- |
| ingest / parse / normalize | 0-30 秒 | `SecurityEvent` 和基础实体 |
| enrichment | 30-60 秒 | 资产、账号、情报、地理、ASN、重要性 |
| fast detector | 60-90 秒 | P0/P1 Finding |
| correlation / case | 90-120 秒 | case、相关实体、初步攻击链节点 |
| analyst fast pass | 120-170 秒 | 入口候选、横向路径、缺失证据、下一步 query |
| alert / report snapshot | 170-180 秒 | 第一响应报告和处置建议 |

超过预算时，系统必须降级输出已经确认的 facts 和 gaps，而不是等待完整研判。

## 检测内容库

要从“日志分析”变成“真实攻击发现”，必须有内容库。

内容类型：

- Sigma-like 主机/身份规则。
- Suricata / IDS signature 引用。
- WAF exploit 规则映射。
- EDR vendor alert normalization。
- HIDS 文件完整性和 rootkit 告警。
- VPN 异常登录规则。
- Sandbox verdict 映射。
- IOC：IP/domain/url/hash/email。
- YARA 文件匹配结果引用。
- 自定义 detector：横向移动、爆破、webshell、数据外传、C2 beacon。
- ATT&CK tactic/technique 映射。
- suppression / allowlist / maintenance window。

规则输出统一为 Finding，不直接写最终结论。

### 软检测器优先

这里的“规则”不要理解成传统 SIEM 的强判定。第一版应该更多使用软检测器：

```text
detector 负责提出候选
LLM/agent 负责补查、解释和反推路线
reviewer 负责验收证据
```

软检测器只回答：

- 这里可能有问题。
- 为什么值得查。
- 关联哪些实体。
- 有哪些证据。
- 缺哪些证据。
- 建议下一步查什么。

软检测器不直接回答：

- 一定入侵成功。
- 一定是某个 CVE。
- 一定要封禁或隔离。

只有少数高确定性场景可以作为硬规则：

- 明确恶意 hash 被执行。
- EDR critical 且 vendor verdict 为 malicious。
- canary token / honey account 命中。
- 已确认 IOC 命中并伴随成功连接或执行。
- 同一 case 已经有 reviewer 确认证据。

Finding 必须带：

```json
{
  "detector_id": "web_to_process_anomaly_v1",
  "mode": "soft",
  "hypothesis": "疑似 Web 利用后执行命令",
  "confidence": 0.68,
  "evidence_refs": ["query-q1", "query-q2"],
  "gaps": ["缺少 Web 服务器 EDR process_guid"],
  "next_queries": ["trace host web-01 +/- 10m", "hunt attacker_ip 198.51.100.1 since 24h"]
}
```

LLM 的能力主要用在：

- 字段理解和映射建议。
- 多源弱信号解释。
- 生成补查 query plan。
- 攻击路线草图。
- 误报可能性分析。
- 报告和处置建议。

LLM 不负责：

- 扫描全量日志。
- 直接决定高风险处置。
- 在没有 evidence_refs 的情况下给最终结论。

规则元数据：

```json
{
  "rule_id": "sec-rule-xxx",
  "name": "VPN impossible travel",
  "source_classes": ["vpn", "iam"],
  "severity": "high",
  "confidence": 0.7,
  "technique_ids": ["T1078"],
  "required_fields": ["user", "src_ip", "geo", "event_time"],
  "lookback": "24h",
  "suppression_key": "user",
  "response_playbook": "identity_compromise_triage"
}
```

## 反查和威胁狩猎

反查不是简单搜索字符串，而是按实体和时间窗口跨源展开。

第一版反查工具本质上就是“受控日志查询工具”。agent 不直接碰数据库，也不写任意无限查询，而是调用封装好的查询入口。

建议工具：

```text
security_query           按安全字段执行受限查询
security_hunt_ip         反查 IP
security_hunt_user       反查账号
security_hunt_host       反查主机
security_hunt_domain     反查域名/URL
security_hunt_hash       反查文件 hash
security_trace_host      生成主机时间线
security_trace_case      根据 case 生成相关时间线
evidence_read            读取小证据包
```

工具要求：

- 必须带时间范围，默认不允许查全历史。
- 必须有结果上限，超出写 evidence 文件，只返回摘要。
- 必须保存 query、参数、耗时、row_count、truncated 和 evidence_path。
- 返回结构化结果，不返回大段原始日志。
- 所有查询都能回到 `SecurityAlertV1`、`SecurityEvent` 或原始 raw_ref。

必须支持的反查入口：

- IP：内外网通信、DNS、VPN 映射、WAF 源、EDR 连接、情报命中。
- 账号：登录、失败登录、VPN、主机登录、提权、敏感操作、访问资产。
- 主机：进程树、网络连接、文件写入、登录用户、告警、漏洞、补丁状态。
- 域名/URL：DNS、HTTP、proxy、sandbox、WAF、情报。
- hash/文件：EDR、HIDS、sandbox、YARA、落地路径、执行记录。
- 进程命令行：父子进程、网络连接、文件行为、账号上下文。
- 漏洞/CVE：暴露资产、WAF 命中、利用尝试、补丁状态。

狩猎模式：

```text
hunt seed -> expand entities -> query evidence -> generate timeline -> score -> case/update
```

示例：

```text
seed: 203.0.113.10
1. 查最近 30 天谁访问过该 IP
2. 查这些主机同窗口的 EDR 进程
3. 查是否有 DNS/domain 关联
4. 查账号登录和 VPN 映射
5. 查是否命中情报或沙盒
6. 生成 affected assets 和 evidence refs
```

## 溯源和攻击链

攻击链不是单个 detector 输出，而是 case correlation 输出。

攻击链节点：

- initial_access: WAF exploit、钓鱼落地、VPN 异常登录、弱口令。
- execution: 可疑进程、脚本、webshell 命令。
- persistence: 服务、计划任务、启动项、账号创建。
- privilege_escalation: 提权、管理员组变更、sudo、UAC 绕过迹象。
- defense_evasion: 关停 agent、删除日志、混淆命令。
- credential_access: lsass、dump、异常认证、token 访问。
- discovery: whoami、net view、端口扫描、AD 枚举。
- lateral_movement: RDP/SMB/WinRM/SSH 横向登录。
- command_and_control: beacon、罕见域名、固定周期外联。
- exfiltration: 大流量出站、压缩打包、云盘上传、异常 API 调用。
- impact: 加密、删除、服务异常、勒索告警。

攻击链输出：

```json
{
  "attack_chain_id": "chain-xxx",
  "case_id": "case-xxx",
  "hypothesis": "可能从 VPN 异常登录进入后横向移动到 server-01",
  "confidence": 0.74,
  "stages": [
    {
      "stage": "initial_access",
      "time": "2026-04-30T10:01:00Z",
      "entities": ["user:alice", "vpn_ip:198.51.100.1"],
      "evidence_refs": ["query-q1"],
      "facts": ["alice 从罕见国家 VPN 登录成功"],
      "inferences": ["可能为凭证滥用"]
    }
  ],
  "gaps": ["缺少 server-01 同窗口 EDR process event"],
  "next_queries": ["..."]
}
```

agent 生成攻击链时必须区分：

- facts: 证据直接支持。
- inferences: 合理推断。
- gaps: 还缺的证据。
- alternatives: 其他可能解释。

## 入侵路线反推要求

第一响应阶段要反推出“目前能证明的路线”，而不是只报一个孤立告警。

入侵路线对象建议：

```json
{
  "route_id": "route-xxx",
  "case_id": "case-xxx",
  "status": "partial|confirmed|disputed",
  "entry_point": {
    "type": "vpn|waf|phishing|weak_password|exposed_service|malware|unknown",
    "entity": "user:alice",
    "confidence": 0.72,
    "evidence_refs": ["query-q1"],
    "missing_evidence": ["缺少 VPN MFA result 字段"]
  },
  "timeline": [
    {
      "time": "2026-04-30T10:01:00Z",
      "action": "vpn_login_success",
      "actor": "user:alice",
      "source": "ip:198.51.100.1",
      "target": "vpn:prod",
      "evidence_refs": ["query-q1"],
      "fact_status": "confirmed"
    },
    {
      "time": "2026-04-30T10:04:00Z",
      "action": "rdp_login",
      "actor": "user:alice",
      "source": "host:laptop-unknown",
      "target": "host:server-01",
      "evidence_refs": ["query-q2"],
      "fact_status": "confirmed"
    }
  ],
  "lateral_movement": {
    "observed": true,
    "paths": [["host:server-01", "host:server-02"]],
    "evidence_refs": ["query-q3"]
  },
  "impacted_entities": ["host:server-01", "host:server-02", "user:alice"],
  "data_exposure": {
    "suspected": true,
    "evidence_refs": ["query-q4"],
    "missing_evidence": ["缺少 proxy 上传大小字段"]
  },
  "gaps": [
    {
      "kind": "missing_source",
      "source": "edr:server-02",
      "why_it_matters": "无法确认 server-02 上是否执行恶意进程"
    }
  ],
  "next_queries": ["trace user alice +/- 30m", "hunt dst_ip 203.0.113.10 since 24h"]
}
```

反推规则：

- 每个路线节点都必须有时间、动作、实体和证据引用。
- 入口点必须给出候选排名；无法确认时标 `unknown`，并说明缺什么。
- 横向移动必须显示从哪个实体到哪个实体。
- 被攻击但未入侵成功时，也要输出被攻击路径和阻断点。
- 入侵成功但路径不完整时，输出 partial route，不允许把推断包装成事实。

第一批入口点候选：

- WAF / Web exploit -> Web server。
- VPN / IAM 异常登录 -> 内网资产。
- EDR malicious file execution -> host。
- HIDS webshell / file integrity alert -> server。
- 弱口令 / brute force success -> account/host。
- 沙盒恶意样本 -> hash -> endpoint execution。
- 供应链/应用日志异常 -> service account -> backend。

## 取证和证据保全

取证不等于把所有日志复制一份。第一版要做“证据索引包”。

Forensic package 内容：

- case snapshot。
- findings。
- attack chain。
- timeline。
- involved entities。
- query history。
- sampled raw events。
- raw batch refs。
- file/hash refs。
- sandbox report refs。
- rule/model versions。
- analyst report。
- reviewer decision。
- chain of custody。

证据包必须可复现：

- 每条 evidence 带 query、参数、时间、引擎、结果 hash。
- 每个 raw_ref 带 source_id、batch_id、line_no 或 offset。
- 每个外部报告带原始 URL/ID/hash，不只保存摘要。
- 每次人工或 agent 操作写 action log。

证据包状态：

```text
OPEN -> FROZEN -> EXPORTED -> ARCHIVED
```

`FROZEN` 后不能覆盖，只能追加新的 evidence version。

## 处置闭环

这个模块不应该第一版自动执行危险处置，但要规划接口。

处置动作类型：

- isolate host。
- disable account。
- revoke session/token。
- block IP/domain/url。
- quarantine file。
- add WAF block rule。
- collect endpoint triage package。
- request patch。
- notify owner。

第一版只生成 playbook 建议和人工确认记录。后续通过 connector 接 SOAR、EDR、IAM、WAF。

处置记录：

```json
{
  "action_id": "resp-xxx",
  "case_id": "case-xxx",
  "action_type": "disable_account",
  "target": "user:alice",
  "mode": "recommend|dry_run|approved|executed",
  "approver": "",
  "connector": "iam",
  "result": "pending",
  "evidence_refs": []
}
```

## 安全运营模块补充结构

在 `agent_py_agent/agent/log_analysis/` 里补这些子模块：

```text
  security/
    schemas.py          # SecurityEvent, Entity, AttackChain, ForensicPackage
    enrichment.py       # asset/user/geo/asn/intel enrichment
    entity_graph.py     # entity and edge store facade
    content.py          # rule metadata, ATT&CK mapping, suppression
    correlation.py      # multi-source correlation and case merge
    hunting.py          # retrohunt and backtracking plans
    attack_chain.py     # chain reconstruction
    forensics.py        # evidence package and chain of custody
    response.py         # playbook suggestions and action records
    sla.py              # alert latency and priority policy
```

CLI 后续补：

```powershell
my-agent security status
my-agent security alerts
my-agent security cases
my-agent security case <case_id>
my-agent security hunt ip <ip> --since 30d
my-agent security hunt user <user> --since 7d
my-agent security trace host <host> --around <time>
my-agent security chain <case_id>
my-agent security evidence freeze <case_id>
my-agent security report <case_id>
my-agent security response plan <case_id>
```

这些命令可以先作为 `my-agent logs security ...` 子命令，成熟后再提升成独立 `security` 命名空间。

## 主代理会话健康和派遣边界

这个功能的长期形态里，主代理不能变成“自己持续分析日志的长会话”。安全日志是高吞吐、长周期、多分支任务，如果主代理亲自工作，会很快污染上下文、拖慢响应，并让会话健康不可控。

主代理只保留这些职责：

- 接收用户目标、解释状态、展示进度。
- 创建和调整 case / work queue / subagent run。
- 分配预算：每小时派多少 analyst、每个 case 最多查多少轮、P0/P1 是否升级。
- 验收子代理结果：是否有 evidence、是否区分事实/推断/gaps、是否满足 SLA。
- 处理冲突：多个子代理结论不一致时，派 reviewer 或要求补查。
- 维护会话健康：不把大日志、大查询结果、长时间线直接塞入主会话。

主代理明确不做：

- 不直接读取原始大日志。
- 不直接运行长期 watch。
- 不直接执行重型 SQL / ML job。
- 不直接把所有 case 细节常驻在 prompt。
- 不直接执行高风险处置。

执行者分层：

```text
collector worker     取数、spool、checkpoint
parser worker        解析、归一化、dead-letter
detector worker      规则/统计/ML finding
correlation worker   finding 合并、case、route draft
analyst subagent     补查证据、解释攻击链
hunt subagent        按 seed 扩散反查
forensic subagent    固化证据包
response subagent    生成处置建议
reviewer subagent    验收和拒绝无证据结论
```

主代理和子代理之间只传小对象：

- `case_id`
- `finding_refs`
- `entity_refs`
- `evidence_refs`
- `query_plan`
- `budget`
- `acceptance_checks`

如果子代理需要更多数据，只能通过 `traffic_query` / `security_hunt` / `evidence_read` 这类工具按需查，工具返回必须限量、可追溯、带 evidence path。

调度健康规则：

- `max_parallel_analyst_agents=0` 表示默认不自动派 analyst。
- `case_auto_dispatch_enabled=false` 表示只建 case，不自动派子代理。
- P0/P1 可以单独开启快路径派遣，但也必须有每小时预算和超时。
- 子代理超时、失败或输出无证据时，不拖住主代理，状态写回 case。
- 主代理恢复会话时只读 case summary、route summary 和 open gaps，不读全量 transcript。

## 独立配置和默认关闭策略

日志分析和实时安全监测不是所有用户都会用，所以必须有独立配置文件，且默认不影响普通使用。

建议配置文件：

```text
agent_py_agent/config/log_analysis_config.yaml
```

加载规则：

- 普通 `my-agent run/chat/status` 不主动加载重型日志分析配置。
- 只有执行 `my-agent logs ...` / `my-agent security ...`，或主配置显式打开 `log_analysis_config_path` 时才加载。
- 配置文件不存在时，功能视为未启用，命令只显示空状态和启用提示，不报错。
- 配置非法值安全回落，并写 doctor warning。
- 密钥、token、Kafka password、API secret 只允许环境变量名或 secret 引用，不写明文。

默认配置原则：

- 后台能力默认关闭。
- worker 并发默认 0。
- 自动派遣默认关闭。
- ML 默认关闭。
- 集群后端默认关闭。
- 高风险处置默认 recommend-only。
- 路径默认落在 `agent_py_agent/data/log_analysis/`，不污染源码和 git。

示例最小配置：

```yaml
enabled: false
security_monitoring_enabled: false
autostart: false

workspace: "data/log_analysis"

source_workers: 0
parser_workers: 0
detector_workers: 0
correlation_workers: 0

case_auto_dispatch_enabled: false
max_parallel_analyst_agents: 0
analyst_agent_budget_per_hour: 0
analyst_agent_timeout_seconds: 0

fast_path_enabled: false
fast_path_sla_seconds: 180
p0_auto_case_enabled: false
p1_auto_case_enabled: false

ml_enabled: false
ml_backend: "off"
cluster_backend: "off"
query_engine: "duckdb"
event_store: "parquet"

response_mode: "recommend"

# prompt behavior
security_prompt_enabled: false
security_prompt_mode: "off"   # off / minimal / analyst / incident
```

`0` 的约定：

- 对 worker / 并发 / 自动派遣预算：`0` 表示关闭。
- 对 timeout：`0` 表示使用安全默认值或不启用该阶段，不能表示无限等待。
- 对结果 limit：只有明确写在字段说明里的 limit，`0` 才能表示不限制；默认不把 `0` 当成扩大权限。
- 对 retention：只有明确约定 `0` 表示不自动删除时才允许，否则回落到安全默认。

### 安全分析 prompt 开关

普通用户不应该总是背安全分析 prompt。只有显式开启安全分析功能时，才给 agent 注入安全研判相关 prompt。

开关：

```yaml
security_prompt_enabled: false
security_prompt_mode: "off"
```

模式：

| 模式 | 行为 |
| --- | --- |
| `off` | 不注入安全分析 prompt，保持普通 agent 行为 |
| `minimal` | 只给基础安全术语、证据引用要求和查询工具说明 |
| `analyst` | 注入 analyst 角色提示，适合手动查 case |
| `incident` | 注入 P0/P1 事件响应提示，强调 3 分钟、route、gaps、处置建议 |

默认：

- `security_prompt_enabled=false`
- `security_prompt_mode=off`

只有这些场景允许注入安全 prompt：

- 用户执行 `my-agent security ...`。
- 用户执行 `my-agent logs ...` 且配置开启。
- 子代理角色是 `triage-agent`、`route-agent`、`hunt-agent`、`forensic-agent`、`response-agent`、`review-agent`。
- case 明确处于安全分析流程。

普通 `my-agent chat/run` 不因为这个模块存在而变重。

## 能力等级和扩展接口

功能必须按等级演进，不要第一版把所有能力堆进核心。

| 等级 | 名称 | 能力 | 依赖 |
| --- | --- | --- | --- |
| L0 | off | 默认关闭，只显示配置提示 | 无 |
| L1 | local-lite | 文件 ingest、parser、DuckDB/Parquet、基础 detector | 本地磁盘 |
| L2 | local-security | SecurityEvent、case、route、evidence、手动 analyst | 本地数据底座 |
| L3 | realtime | syslog/API/Kafka、fast detector、3 分钟 SLA、自动 case | 后台 worker |
| L4 | distributed | ClickHouse/Kafka/Flink/Ray/Spark/远程 ML | 外部集群 |
| L5 | response | SOAR/EDR/IAM/WAF 连接和审批处置 | 外部系统 |

接口必须先写成协议，而不是直接绑定实现：

```text
LogSource          file/api/syslog/kafka/cloud
Parser             jsonl/csv/syslog/waf/edr/hids/vpn/sandbox/custom
EventStore         parquet/clickhouse/object-store
QueryEngine        duckdb/clickhouse/opensearch/custom
Detector           rule/statistical/ml/external
FeatureExtractor   sql/local/cluster
ScoringEngine      local/remote/ray/spark/flink/clickhouse
CaseStore          local/sql/remote
EvidenceStore      local/object-store/forensic-vault
DispatchEngine     local-subagent/queue/cluster-worker
ResponseConnector  recommend/webhook/soar/edr/iam/waf
```

上层 agent 只能依赖协议和数据契约。底层实现可替换，替换后不能改 analyst prompt 和 case schema。

## 非目标

第一版不做这些：

- 不直接训练复杂深度学习模型。
- 不实现完整 Kafka/Flink/ClickHouse 集群。
- 不让 LLM 逐行阅读原始日志。
- 不承诺 exactly-once 语义；第一版目标是 at-least-once + idempotent + dedup。
- 不把安全告警直接判死刑；agent 输出必须区分事实、推断和待查问题。

## 建议模块结构

```text
agent_py_agent/agent/log_analysis/
  __init__.py
  models.py              # SourceSpec, Cursor, RawBatch, Event, Finding, Case, EvidenceRef
  config.py              # log analysis config and safe defaults
  interfaces.py          # public Protocols for sources, parsers, stores, detectors, ML, dispatch
  capabilities.py        # capability levels L0-L5 and feature gates
  doctor.py              # config/path/backend health checks
  sources/
    base.py              # LogSource protocol
    files.py             # file watch / spool / rotation
    api.py               # paged cursor pull
    syslog.py            # syslog receiver facade and durable spool
    kafka.py             # kafka consumer facade
  parsers/
    base.py              # Parser protocol and ParseResult
    registry.py          # parser selection by source type, format, confidence
    common.py            # jsonl/csv/syslog key-value helpers
  ingest/
    checkpoint.py        # source checkpoint and cursor commits
    dedup.py             # batch/event dedup
    pipeline.py          # source -> parser -> storage
    dead_letter.py       # malformed records and parser failures
  storage/
    base.py              # QueryEngine / EventStore protocol
    duckdb_engine.py     # local MVP
    parquet_store.py     # partitioned files
    clickhouse_engine.py # future adapter
    object_store.py      # future S3/MinIO adapter
  analytics/
    rollups.py           # time/entity aggregates
    detectors.py         # deterministic detector protocol
    rules.py             # scan, spike, rare entity, DNS, failure-rate
    baselines.py         # z-score, MAD, EWMA, seasonality
  ml/
    interfaces.py        # ScoringEngine, FeatureExtractor, ModelRegistry
    local.py             # sklearn/river style local scorer
    remote.py            # HTTP/gRPC scorer adapter
    cluster.py           # Ray/Spark/Flink job adapter facade
  dispatch/
    budgets.py           # case and agent dispatch budgets
    queue.py             # pending investigation work queue
    engine.py            # DispatchEngine protocol and local implementation
    health.py            # main-session and worker health summaries
  cases/
    case_store.py        # case lifecycle and dedup
    evidence.py          # query/sample evidence persistence
    scheduler.py         # case priority, budgets, agent dispatch
  agents/
    prompts.py           # role prompts for triage/route/hunt/forensic/response/review
    contracts.py         # subagent input/output schemas and acceptance checks
    summaries.py         # compact case summaries for main-agent recovery
  tools.py               # traffic_query/topn/sample/scan tools for agents
```

运行态目录建议：

```text
agent_py_agent/data/log_analysis/
  sources/               # source specs and health
  checkpoints/           # per source cursor/offset/position
  manifests/             # file/API/syslog/kafka batch manifests
  spool/                 # durable incoming spool for syslog/API/file batches
  normalized/            # normalized JSONL or compact event batches
  parquet/               # partitioned columnar data
  duckdb/                # local catalog and query state
  rollups/               # hourly/minute aggregates
  detections/            # detector outputs
  evidence/              # query outputs and sampled rows
  cases/                 # case records and lifecycle
  reports/               # AI analyst reports
  dead_letter/           # malformed/unsupported records
  ml/                    # features, scores, model metadata
```

`agent_py_agent/data/log_analysis/` 必须进 `.gitignore`。

## 核心数据契约

### SourceSpec

```json
{
  "source_id": "prod-firewall",
  "kind": "file|api|syslog|kafka",
  "format": "jsonl|csv|syslog|zeek|suricata|netflow|custom",
  "enabled": true,
  "priority": "normal",
  "parser_id": "auto",
  "dedup_policy": "source_event_fingerprint",
  "checkpoint_policy": "after_durable_write",
  "retention_days": 30,
  "options": {}
}
```

`options` 只放非敏感配置。密钥、token、cookie 只放环境变量名或 secret 引用，不写明文。

### Checkpoint

```json
{
  "source_id": "prod-firewall",
  "cursor_kind": "file_offset|api_cursor|syslog_spool|kafka_offset",
  "cursor": {},
  "last_committed_batch_id": "batch-xxx",
  "last_event_time": "2026-04-30T10:15:00Z",
  "updated_at": "2026-04-30T10:16:00Z"
}
```

checkpoint 只能在 batch 已经 durable write、dedup 记录已经提交、manifest 已经写入后更新。

### RawBatch

```json
{
  "batch_id": "batch-xxx",
  "source_id": "prod-firewall",
  "source_kind": "kafka",
  "received_at": "2026-04-30T10:16:00Z",
  "time_range": ["2026-04-30T10:15:00Z", "2026-04-30T10:16:00Z"],
  "raw_refs": ["spool/prod-firewall/batch-xxx.log"],
  "size_bytes": 123456789,
  "content_hash": "sha256:...",
  "cursor_before": {},
  "cursor_after": {},
  "status": "received|parsed|stored|failed"
}
```

### NormalizedEvent

```json
{
  "event_id": "evt-xxx",
  "source_id": "prod-firewall",
  "event_time": "2026-04-30T10:15:12Z",
  "ingest_time": "2026-04-30T10:15:15Z",
  "event_type": "network_flow|dns|http|tls|auth|ids_alert|custom",
  "src_ip": "10.1.2.3",
  "dst_ip": "203.0.113.10",
  "src_port": 52344,
  "dst_port": 443,
  "protocol": "tcp",
  "bytes_out": 1234,
  "bytes_in": 4321,
  "raw_ref": "spool/...",
  "raw_line_no": 123,
  "parser_id": "zeek_conn_v1",
  "parser_confidence": 0.98,
  "dedup_key": "sha256:..."
}
```

字段允许稀疏，但基础字段必须稳定。未知字段放 `attributes`，不要污染顶层 schema。

### Finding

```json
{
  "finding_id": "finding-xxx",
  "detector_id": "egress_spike_v1",
  "detector_kind": "rule|statistical|ml|external",
  "window": ["2026-04-30T10:00:00Z", "2026-04-30T10:15:00Z"],
  "severity_hint": "low|medium|high|critical",
  "risk_score": 0.87,
  "entities": {
    "src_ip": ["10.1.2.3"],
    "dst_ip": ["203.0.113.10"]
  },
  "features": {
    "bytes_delta_ratio": 4.3,
    "new_dst_count": 18
  },
  "evidence_refs": ["query-q1", "sample-s1"],
  "status": "OPEN"
}
```

### Case

```json
{
  "case_id": "case-20260430-001",
  "title": "出站流量突增",
  "status": "OPEN|INVESTIGATING|AWAITING_REVIEW|CLOSED|SUPPRESSED",
  "priority": "P0|P1|P2|P3",
  "risk_score": 0.87,
  "finding_refs": ["finding-xxx"],
  "evidence_refs": ["query-q1", "sample-s1"],
  "assigned_run_ids": [],
  "dedup_key": "egress_spike:10.1.2.3:2026-04-30T10",
  "created_at": "2026-04-30T10:16:00Z",
  "updated_at": "2026-04-30T10:16:00Z"
}
```

## 多来源接入计划

### 文件来源

要支持：

- `glob` 扫描，例如 `logs/**/*.log`。
- 追加写入文件 tail。
- 文件轮转：rename、copy-truncate、按日期新文件。
- 压缩文件：`.gz` 第一批支持，后续 `.zst`。
- 大文件分块读取。
- 部分写入文件保护：文件大小稳定 N 秒后才当 batch，tail 模式则按 offset 微批。

去重策略：

- 文件级：`device/inode` 可用时优先；Windows 下用 path + size + mtime + rolling hash。
- batch 级：`source_id + file_identity + offset_start + offset_end + content_hash`。
- event 级：解析后生成 `dedup_key`，同一 source/window 内重复事件只算一次。

checkpoint：

- tail 模式记录 file identity 和 offset。
- batch 文件模式记录 content hash 和 processed marker。
- rotation 后保留旧 identity 到 TTL，避免新旧文件交叉重复。

### API 来源

要支持：

- page token。
- since/until 时间窗口。
- 增量 cursor。
- rate limit 和 retry-after。
- 重试时带 overlap window，比如回退 1-5 分钟，靠 dedup 消重。
- response 原文可选落 spool，大响应必须只保留路径和 hash。

checkpoint：

- 成功 durable write 后提交 cursor。
- API 返回乱序时用 watermark，不用单纯最大时间戳。
- 每个 endpoint 单独 checkpoint。

### Syslog 来源

要支持：

- UDP：可能丢包，只做 best effort，必须写 source health。
- TCP/TLS：优先推荐，按连接或时间窗口切 batch。
- RFC3164 / RFC5424 基础解析。
- 接收后先写 durable spool，再进入 parser。

去重策略：

- syslog header + host + app + procid + msg hash + short time bucket。
- 如果设备有 sequence id，纳入 dedup key。

checkpoint：

- syslog 没有天然 offset，本地 spool 文件和 batch manifest 就是 checkpoint。
- spool batch 解析成功后标记 parsed/stored。

### Kafka / Redpanda 来源

要支持：

- topic 多分区。
- consumer group。
- 手动 commit offset。
- batch size 和 max wait。
- schema registry 预留。
- dead-letter topic 预留。

checkpoint：

- 每个 topic/partition 记录 offset。
- 只有 batch durable write + dedup + storage 成功后才 commit。
- consumer crash 后从上次 commit 重放，依靠 dedup 保证结果不重复。

## 解析层计划

Parser 必须是插件协议：

```text
can_parse(sample, source_spec) -> confidence
parse_batch(raw_batch) -> ParseResult
```

第一批 parser：

- `jsonl_v1`
- `csv_v1`
- `syslog_rfc3164_v1`
- `syslog_rfc5424_v1`
- `nginx_access_v1`
- `apache_access_v1`
- `zeek_conn_v1`
- `zeek_dns_v1`
- `suricata_eve_v1`
- `generic_kv_v1`

ParseResult 必须包含：

- normalized events count。
- parser confidence。
- malformed count。
- dead-letter refs。
- schema summary。
- sample rows。

解析失败不能卡死整条 pipeline。坏行进入 `dead_letter/`，并生成可搜索的诊断事件。

## 去重和幂等

第一版用三层防重复：

1. source checkpoint 避免重复读取。
2. batch manifest 避免重复处理同一批。
3. event dedup table 避免重复写入相同事件。

本地实现：

- SQLite table: `log_ingest_batches`
- SQLite table: `log_event_dedup`
- TTL 清理旧 dedup key。
- 可选 Bloom filter 只做加速，不作为唯一事实源。

集群实现预留：

- ClickHouse ReplacingMergeTree / unique key 方案。
- Kafka compacted topic 保存 processed batch marker。
- Redis/RocksDB/Flink state 保存短期 dedup。
- Object storage manifest 作为最终审计事实源。

## 存储和查询

MVP：

- Parquet 按 `source_id/date/hour/event_type` 分区。
- DuckDB 查询 parquet。
- SQLite/LocalStore 只保存 manifest、case、evidence、summary，不保存全量事件。

建议分区：

```text
parquet/source_id=<id>/event_type=<type>/date=YYYY-MM-DD/hour=HH/*.parquet
```

查询工具必须限制输出：

- 默认最多返回 100 行。
- 大结果写 evidence 文件，只返回摘要和路径。
- 所有查询保存 query_id、SQL、参数、耗时、row_count、truncated、evidence_path。

后续外接：

- ClickHouse 作为主查询引擎。
- S3/MinIO 保存 parquet 和 evidence。
- Elasticsearch/OpenSearch 作为全文辅助，不作为唯一事实源。

## 检测器计划

第一批 deterministic detectors：

- `heavy_hitter_detector`: Top src/dst/host/uri/bytes。
- `egress_spike_detector`: 出站流量突增。
- `new_entity_detector`: 新目的 IP、域名、ASN、国家。
- `rare_port_detector`: 罕见端口。
- `scan_detector`: 同源多目标/多端口连接。
- `dns_anomaly_detector`: NXDOMAIN 暴涨、罕见 TLD、可疑长度域名。
- `failure_rate_detector`: 失败率突增。
- `beacon_detector`: 周期性连接候选。
- `exfil_candidate_detector`: 大流量出站、长连接、罕见目的地组合。

每个 detector 输出 Finding，不直接派 agent。Case scheduler 负责去重、合并、排序和派工。

## ML 底座预留

ML 不绑定具体实现，只定义接口。

### FeatureExtractor

```text
input: time window + entity scope + query engine
output: feature frame + feature_schema_version + evidence refs
```

第一批 feature：

- bytes/count per time bucket。
- src/dst cardinality。
- new entity ratio。
- historical baseline delta。
- failure rate。
- periodicity score。
- rarity score。
- detector hit counts。

### ScoringEngine

```text
score(features, model_ref, context) -> MLScoreResult
```

MLScoreResult：

```json
{
  "scoring_engine": "local_sklearn|river|remote_http|clickhouse|ray|spark|flink",
  "model_id": "traffic_anomaly_v1",
  "model_version": "2026-04-30.1",
  "feature_schema_version": "v1",
  "risk_score": 0.87,
  "labels": ["anomaly", "egress_spike"],
  "explanations": [
    {"feature": "bytes_delta_ratio", "value": 4.3, "weight": 0.31}
  ],
  "evidence_refs": ["feature-frame-xxx"],
  "error": ""
}
```

### 本地 ML 后端

第一版可以只预留，第二阶段接：

- scikit-learn: IsolationForest、LOF、OneClassSVM。
- river: streaming anomaly / drift。
- stats-only model: MAD、EWMA、seasonal baseline。

本地 ML 只能吃聚合 feature，不能吃全量原始日志。

### 远程 ML 服务

预留 HTTP/gRPC adapter：

- request 带 feature schema version。
- response 带 model version、risk_score、explanations。
- timeout、retry、circuit breaker。
- 失败时 detector 仍可继续，ML 评分缺失不能阻断 ingestion。

### 集群 ML / 大数据后端

预留这些运行模式：

- ClickHouse SQL feature + UDF scoring。
- Spark batch scoring。
- Flink streaming scoring。
- Ray distributed Python scoring。
- Kafka stream scorer service。
- GPU model service，只接 feature batch，不接原始日志。

集群接口不要泄漏具体引擎到 agent 层。agent 只看到 `Finding`、`Case` 和 `EvidenceRef`。

### 模型治理

后续正式 ML 底座要补：

- model registry。
- feature registry。
- training dataset manifest。
- label store。
- drift metrics。
- threshold config。
- evaluation report。
- rollback。

第一版只写 metadata，不做完整平台。

## Agent 派遣和研判

调度链路：

```text
detectors/ml -> findings -> case scheduler -> analyst subagent -> reviewer/acceptance -> report -> case store
```

Case scheduler 负责：

- finding 去重。
- 多 detector 结果合并。
- 按 risk_score、asset criticality、novelty、confidence 排序。
- 控制每小时最多派多少 analyst。
- 控制同一实体的 case 合并窗口。
- 低分 case 只记录，不派 agent。

Analyst subagent 输入：

- case 摘要。
- 时间窗口。
- 相关实体。
- 已有 evidence refs。
- 可用工具：`traffic_query`、`traffic_topn`、`traffic_sample`、`traffic_timeseries`、`traffic_related`。
- 验收标准：必须引用 evidence，不得只凭模型猜测。

Reviewer 验收：

- 是否有 query/sample 证据。
- 是否区分事实、推断、待查。
- 是否给出下一步处置。
- 是否误把 archive/summary 当最终事实源。

## CLI 规划

第一批命令：

```powershell
my-agent logs status
my-agent logs sources list
my-agent logs sources add-file <glob>
my-agent logs sources add-api <name>
my-agent logs sources add-syslog <name>
my-agent logs sources add-kafka <topic>
my-agent logs ingest --source <source_id> --once
my-agent logs watch --source <source_id>
my-agent logs checkpoints
my-agent logs replay --source <source_id> --since <time>
my-agent logs query "<query>"
my-agent logs topn --field src_ip --window 1h
my-agent logs sample --case-id <case_id>
my-agent logs scan --window 15m
my-agent logs cases
my-agent logs report --case-id <case_id>
my-agent logs ml backends
my-agent logs ml score --case-id <case_id>
```

第一版也可以先只做：

```powershell
my-agent logs ingest-file <path>
my-agent logs query "<sql or preset>"
my-agent logs scan --window 1h
my-agent logs cases
```

## 基础款完成定义

基础款不追求覆盖所有日志源和所有攻击类型，目标是把“安全 agent 助手”的核心闭环跑通。

基础款必须完成：

- 独立配置存在，默认关闭，不影响普通 `my-agent`。
- 能导入 `SecurityAlertV1` CSV/JSONL。
- 能把中文字段映射为稳定 key，并保留 `raw_fields`。
- 能把日志落到本地存储，支持按字段和时间查询。
- 能生成 evidence refs，查询结果大时只返回摘要和路径。
- 能运行第一批软检测器，产出 Finding。
- 能把 Finding 合并为 Case。
- 能为 Case 生成 route draft：入口候选、时间线、影响实体、gaps、next_queries。
- 能派 analyst/reviewer 子代理，但默认不自动派，必须由配置或命令开启。
- 能使用安全 prompt 开关，普通模式不注入安全分析 prompt。
- 能跑最小 fixture，证明 WAF/EDR/VPN 三源日志能串成一个 case。

基础款可以暂缓：

- API/syslog/Kafka 实时接入。
- ClickHouse/Kafka/Flink/Ray/Spark 集群后端。
- 远程 ML 服务。
- SOAR/EDR/IAM/WAF 自动处置。
- Web UI。
- 完整 ECS/OCSF 映射。
- 复杂权限系统。

基础款验收命令目标：

```powershell
my-agent logs status
my-agent logs ingest-file fixtures/security_alert_v1.csv
my-agent logs query "attacker_ip=198.51.100.1 since=24h"
my-agent logs scan --window 1h
my-agent logs cases
my-agent security case <case_id>
my-agent security trace case <case_id>
my-agent security report <case_id>
```

基础款验收结果：

- `local-doctor` 不受影响。
- 没有日志分析配置时普通命令行为不变。
- fixture 能稳定生成至少一个 case。
- case 至少包含 2 条 evidence refs。
- route draft 至少包含入口候选、时间线、gaps、next_queries。
- reviewer 能拒绝无证据报告。

## 基础款多 worker 开发路线

基础款可以拆成多个 worker 并行推进。每个 worker 有清楚写入边界，避免互相踩文件。

### Worker A: Contract / Config / CLI Skeleton

目标：

- 建模块骨架。
- 建独立配置和默认关闭。
- 建 CLI 子命令骨架。
- 定义核心数据模型和接口协议。

主要文件：

- `agent_py_agent/agent/log_analysis/models.py`
- `agent_py_agent/agent/log_analysis/config.py`
- `agent_py_agent/agent/log_analysis/interfaces.py`
- `agent_py_agent/agent/log_analysis/capabilities.py`
- `agent_py_agent/agent/log_analysis/doctor.py`
- `agent_py_agent/config/log_analysis_config.yaml`
- `agent_py_agent/cli/logs.py`
- `agent_py_agent/cli/parser.py`

验收：

- `my-agent logs status` 在未启用时正常显示 disabled。
- 普通 `my-agent status/run/chat` 不加载重型日志分析配置。
- 配置非法值安全回落。

### Worker B: SecurityAlertV1 Ingest / Parser

目标：

- 支持 SecurityAlertV1 CSV/JSONL 导入。
- 中文字段映射为稳定 key。
- 保存 raw_fields。
- 生成 batch manifest、checkpoint、dead-letter。

主要文件：

- `agent_py_agent/agent/log_analysis/parsers/base.py`
- `agent_py_agent/agent/log_analysis/parsers/security_alert_v1.py`
- `agent_py_agent/agent/log_analysis/parsers/registry.py`
- `agent_py_agent/agent/log_analysis/ingest/pipeline.py`
- `agent_py_agent/agent/log_analysis/ingest/checkpoint.py`
- `agent_py_agent/agent/log_analysis/ingest/dedup.py`
- `agent_py_agent/agent/log_analysis/ingest/dead_letter.py`

验收：

- 用户给定 25 个字段的 CSV 能导入。
- 重复导入不重复写事件。
- 坏行进 dead-letter，不阻塞整批。
- payload 默认截断并记录 hash。

### Worker C: Local Store / Query / Evidence

目标：

- 本地轻量存储和查询。
- 第一版可用 SQLite/JSONL 起步，后续切 DuckDB/Parquet。
- 受控查询工具返回 evidence refs。

主要文件：

- `agent_py_agent/agent/log_analysis/storage/base.py`
- `agent_py_agent/agent/log_analysis/storage/local_store.py`
- `agent_py_agent/agent/log_analysis/storage/query.py`
- `agent_py_agent/agent/log_analysis/cases/evidence.py`
- `agent_py_agent/agent/log_analysis/tools.py`

验收：

- 可按 `attacker_ip`、`victim_ip`、`domain`、`uri`、`alert_type`、时间范围查询。
- 查询必须限结果，大结果写 evidence 文件。
- 每次查询保存 query_id、参数、row_count、truncated、evidence_path。

### Worker D: Soft Detectors / Case Store

目标：

- 实现第一批软检测器。
- Finding -> Case。
- case 去重、合并、优先级。

主要文件：

- `agent_py_agent/agent/log_analysis/analytics/detectors.py`
- `agent_py_agent/agent/log_analysis/analytics/security_rules.py`
- `agent_py_agent/agent/log_analysis/analytics/baselines.py`
- `agent_py_agent/agent/log_analysis/cases/case_store.py`
- `agent_py_agent/agent/log_analysis/cases/scheduler.py`

基础款先做这些软检测器：

- `waf_attack_success_candidate`
- `web_to_process_anomaly`
- `vpn_new_geo_login`
- `bruteforce_then_success`
- `rare_egress_after_alert`
- `multi_source_weak_signal`

验收：

- detector 输出 hypothesis、confidence、evidence_refs、gaps、next_queries。
- 同一攻击 IP / 受害 IP / 时间窗口的 finding 能合并到同一 case。
- 低置信 finding 可以只记录，不自动派 agent。

### Worker E: Route / Timeline / Gaps

目标：

- 从 case evidence 生成 route draft。
- 输出入口候选、时间线、影响实体、横向迹象、gaps、next_queries。
- 不把推断包装成事实。

主要文件：

- `agent_py_agent/agent/log_analysis/security/entity_graph.py`
- `agent_py_agent/agent/log_analysis/security/correlation.py`
- `agent_py_agent/agent/log_analysis/security/attack_chain.py`
- `agent_py_agent/agent/log_analysis/security/hunting.py`

验收：

- WAF -> 受害资产 -> 外联 能生成 partial route。
- VPN -> 登录成功 -> 多资产访问 能生成横向移动候选。
- 缺少 EDR/VPN 字段时输出 gaps。

### Worker F: Agent Dispatch / Prompt / Review

目标：

- 安全 prompt 开关。
- analyst/reviewer 子代理输入输出契约。
- 主代理只调度和验收，不亲自分析大日志。

主要文件：

- `agent_py_agent/agent/log_analysis/dispatch/budgets.py`
- `agent_py_agent/agent/log_analysis/dispatch/queue.py`
- `agent_py_agent/agent/log_analysis/dispatch/engine.py`
- `agent_py_agent/agent/log_analysis/agents/prompts.py`
- `agent_py_agent/agent/log_analysis/agents/contracts.py`
- `agent_py_agent/agent/log_analysis/agents/summaries.py`

验收：

- `security_prompt_enabled=false` 时普通 prompt 不变。
- `security_prompt_mode=analyst` 时只给安全 case 所需 prompt。
- `max_parallel_analyst_agents=0` 时不自动派。
- reviewer 能拒绝无 evidence 的报告。

### Worker G: Fixtures / Tests / Live Lab

目标：

- 做最小安全日志 fixture。
- 做回归测试和基础款验收脚本。
- 验证默认关闭不影响普通用户。

主要文件：

- `agent_py_agent/tests/test_log_analysis_models.py`
- `agent_py_agent/tests/test_log_analysis_ingest.py`
- `agent_py_agent/tests/test_log_analysis_query.py`
- `agent_py_agent/tests/test_log_analysis_detectors.py`
- `agent_py_agent/tests/test_log_analysis_dispatch.py`
- `validation/security_fixtures/`
- `scripts/live_lab/cases.py`

最小 fixture：

- WAF 告警：攻击 IP 打 URI，命中 payload。
- EDR/HIDS 风格事件：受害资产出现异常进程或文件变化。
- VPN/账号事件：异常登录或横向访问。

验收：

- 本地 pytest 通过。
- fixture 重放可生成 case、route、report。
- 默认关闭状态下现有 131 个测试不受影响。

### 建议并行顺序

第一批并行：

```text
Worker A: Contract / Config / CLI
Worker B: SecurityAlertV1 Ingest
Worker C: Local Query / Evidence
Worker G: Fixtures / Tests
```

第二批并行：

```text
Worker D: Soft Detectors / Case
Worker E: Route / Timeline / Gaps
Worker F: Agent Dispatch / Prompt / Review
```

基础款收口：

```text
A+B+C+D+E+F+G
-> run pytest
-> replay fixture
-> verify case/route/evidence/report
-> verify default-off behavior
```

## 分阶段开发计划

### Phase 0: 契约和配置

目标：

- 建立 `log_analysis` 模块骨架。
- 定义 SourceSpec、Checkpoint、RawBatch、NormalizedEvent、Finding、Case、EvidenceRef。
- 定义 `SecurityAlertV1`。
- 加默认配置和安全路径。
- 把 `agent_py_agent/data/log_analysis/` 加入忽略策略。
- 增加独立 `agent_py_agent/config/log_analysis_config.yaml` 示例配置，默认 `enabled=false`。
- 定义接口协议：LogSource、Parser、EventStore、QueryEngine、Detector、ScoringEngine、DispatchEngine、ResponseConnector。
- 定义能力等级 L0-L5 和 feature gates。

验收：

- 配置非法值安全回落。
- 数据模型能 JSON round-trip。
- `my-agent logs status` 能显示空状态。
- 没有日志分析配置文件时，普通 `my-agent run/chat/status` 行为不变。
- 默认 worker / 自动派遣 / ML / 集群 / 响应执行全部关闭。

### Phase 1: 本地文件接入 MVP

目标：

- 支持 `logs ingest-file <path>`。
- 生成 file manifest。
- 支持 `.log` / `.jsonl` / `.csv` 基础读取。
- 优先支持 `SecurityAlertV1` CSV/JSONL。
- 写 checkpoint 和 batch manifest。

验收：

- 同一个文件重复 ingest 不重复写事件。
- 大文件分块不会一次读进内存。
- 中断后重跑能从 checkpoint 或 manifest 恢复。
- 用户给定基础告警字段能稳定导入。

### Phase 2: Parser registry

目标：

- 建 parser protocol。
- 支持 jsonl、csv、syslog、nginx access、generic key-value。
- 解析失败写 dead-letter。

验收：

- 混合坏行不会阻断整批。
- parser confidence 和 schema summary 可见。
- dead-letter 能被 `logs status` 统计。

### Phase 3: Parquet + DuckDB 查询

目标：

- normalized event 写 Parquet 分区。
- DuckDB 查询 parquet。
- 保存 query evidence。
- 第一版允许用本地 SQLite/JSONL 作为过渡，但接口必须保持 QueryEngine 可替换。

验收：

- `logs query` 返回有限结果。
- 大结果写 evidence path。
- query_id 可追溯 SQL、参数、耗时和 row_count。

### Phase 4: Rollup 和基础 detector

目标：

- 生成分钟/小时 rollup。
- 实现基础软检测器和少量统计 detector。
- detector 输出 Finding。

验收：

- 用 fixture 日志能稳定产出预期 finding。
- detector 重跑不重复创建 finding。
- finding 带 evidence refs。
- finding 带 hypothesis、confidence、gaps、next_queries。

### Phase 5: Case store 和去重合并

目标：

- Finding -> Case。
- case dedup key。
- case 生命周期。
- priority/risk_score 排序。

验收：

- 同一实体同一窗口重复 finding 合并到同一 case。
- case 文件和 LocalStore 可搜索。
- `logs cases` 能查看状态。

### Phase 6: Agent analyst 派遣

目标：

- 为 case 创建 analyst subagent。
- 注入 traffic query/sample 工具。
- analyst 生成报告和证据引用。
- reviewer 验收。
- 主代理只写 dispatch request、读 summary 和验收结果，不直接做 analyst 工作。
- 增加 dispatch budget：并发数、每小时派遣数、case 优先级、超时。

验收：

- case -> subagent -> AWAITING_ACCEPTANCE -> DONE/VERIFIED 能跑通。
- 没有 evidence 的报告不能验收通过。
- 报告区分事实、推断和待查问题。
- `max_parallel_analyst_agents=0` 时只建 case，不自动派子代理。
- 子代理失败不会污染主会话；case 写入 failure/gaps/next action。

### Phase 7: 持续文件 watch

目标：

- 目录 watch / poll。
- 支持日志轮转。
- 支持微批。
- backpressure：处理不过来时暂停派 agent，只继续落 manifest。

验收：

- 持续追加日志不会重复。
- 文件 rotate 后不丢不重。
- 队列堆积时 status 能提示。

### Phase 8: API / Syslog / Kafka 接入

目标：

- API pull source。
- syslog durable spool。
- Kafka consumer adapter。
- 每种 source 独立 checkpoint。

验收：

- API retry + overlap 不重复。
- Kafka crash 后重放不重复入库。
- Syslog 坏消息进入 dead-letter。

### Phase 9: ML 预留接口落地

目标：

- FeatureExtractor。
- ScoringEngine protocol。
- local stats scorer。
- remote scorer adapter skeleton。
- model metadata 写入 Finding。

验收：

- ML 后端关闭时 detector 正常跑。
- ML scorer 超时不阻塞 ingestion。
- score 结果带 model_id/version/feature_schema_version。

### Phase 10: 集群预留和外接后端

目标：

- ClickHouse QueryEngine adapter。
- object storage evidence adapter。
- Kafka source production-ready config。
- cluster scoring job ref contract。

验收：

- 同一 TrafficQuery API 可切 DuckDB/ClickHouse。
- EvidenceRef 不依赖本地路径。
- cluster job 失败能回写 case/finding 状态。

### Phase 11: 运维、安全和治理

目标：

- source health。
- backlog metrics。
- parser error rate。
- detector rate limit。
- case suppression。
- secrets 只引用环境变量。
- retention/compact。

验收：

- `logs status` 显示 source、lag、last checkpoint、error rate。
- 敏感字段可脱敏。
- raw/evidence retention 可配置。

### Phase 12: Live Lab 和回归测试

目标：

- fixture 日志集。
- 大文件合成器。
- API/Kafka/syslog mock。
- detector regression。
- agent analyst E2E。

验收：

- 本地 smoke 不调真实模型。
- real lab 可选调真实模型。
- 重复 ingest、crash recovery、rotate、late arrival 都有回归。

### Phase 13: 安全传感器归一化

目标：

- 先实现 `SecurityAlertV1` 基础告警模型，支持用户给定的中文字段导入和英文 key 归一。
- 接入 WAF、EDR、HIDS、VPN、sandbox、server log 的 fixture parser。
- 在 `NormalizedEvent` 上生成 `SecurityEvent` 视图。
- 建 source product -> event_class/event_action/event_outcome 映射。
- 保存 parser confidence 和 source reliability。

验收：

- 包含“告警类型、威胁名称、IOC/规则ID、URI、XFF代理、Payload、域名、referer、目的端口、协议、受害资产组、攻击资产组、受害IP、攻击IP、源IP、目的IP、检测位置、检测字段、匹配、值、设备序列号、告警规则、API、API威胁类型、OWASP类型”的 CSV/JSONL 能稳定导入。
- 中文列名能映射到稳定 key，原始字段保存在 `raw_fields`。
- 同一登录事件在 VPN / Windows Event / IAM 里能归一成相同 auth 语义。
- EDR process/file/network 事件能生成实体和关系。
- WAF exploit、sandbox malicious verdict、HIDS alert 能进入统一 Finding。

### Phase 14: 资产、身份和情报 enrichment

目标：

- 接 CMDB / asset inventory。
- 接 user/account inventory。
- 接 GeoIP / ASN。
- 接 IOC 和 sandbox verdict。
- 给事件补 asset criticality、owner、business_system、identity_type。

验收：

- 同一 IP 能解析到内网资产或外部情报。
- 同一账号能关联 VPN、主机登录、应用访问。
- 高价值资产上的同类 finding 优先级更高。

### Phase 15: 实时安全 detector 和内容库

目标：

- 建 rule metadata。
- 支持 severity、confidence、ATT&CK technique、required_fields、lookback、suppression_key。
- 实现第一批软检测器：
  - `waf_attack_success_candidate`: WAF/告警命中后，同窗口出现 2xx/5xx 异常、服务端文件变化、进程异常或外联。
  - `web_to_process_anomaly`: Web 服务器进程启动 shell、脚本解释器、下载器或系统命令。
  - `vpn_new_geo_login`: VPN/账号从新国家、新 ASN、新设备或异常时间登录成功。
  - `vpn_to_lateral_movement`: VPN 登录后短时间内访问多台服务器或敏感端口。
  - `bruteforce_then_success`: 多次失败后成功登录，并出现资产访问或进程异常。
  - `rare_egress_after_alert`: 告警后受害资产出现罕见外联、罕见域名或罕见端口。
  - `sandbox_hash_seen_on_asset`: 沙盒判恶 hash 在资产上落地或执行。
  - `edr_or_hids_gap_during_attack`: 攻击窗口内 EDR/HIDS 心跳或日志缺失。
  - `dns_beacon_candidate`: 固定周期 DNS/外联候选。
  - `multi_source_weak_signal`: 多个低置信信号在同一资产、账号或时间窗口叠加。

验收：

- 高危 detector 在 fixture 中 1 分钟内创建 P0/P1 case。
- suppression 能压制已知误报。
- 每个 finding 都带 rule version 和 evidence refs。
- 每个 soft finding 都带 hypothesis、confidence、gaps 和 next_queries。
- LLM/agent 能基于 soft finding 调日志查询工具补证据。

### Phase 16: 实体图谱和攻击链关联

目标：

- 建 entity/edge store。
- 建 correlation engine。
- 把多源 finding 合并成 case。
- 根据时间和实体关系生成 attack chain。
- 输出 facts/inferences/gaps/next_queries。

验收：

- VPN 异常登录 -> 主机登录 -> 可疑进程 -> C2 外联 能串成一条 attack chain。
- WAF exploit -> Web 服务器进程异常 -> 文件落地 能串成一条 attack chain。
- 证据不足时必须输出 gaps，不允许假装完成溯源。

### Phase 17: 反查和威胁狩猎

目标：

- 支持 IP/user/host/domain/hash/process/CVE 作为 seed。
- 自动生成 query plan。
- 跨 WAF、VPN、EDR、HIDS、DNS、server log、sandbox 反查。
- 输出 affected entities、timeline、case update。

验收：

- 给定恶意 IP，能找出访问过它的资产、账号、进程和时间线。
- 给定 hash，能找出文件落地、执行、沙盒 verdict 和相关主机。
- hunt 结果不直接下结论，必须生成 evidence refs。

### Phase 18: 取证包和处置建议

目标：

- 建 forensic package。
- 支持 evidence freeze。
- 保存 query history、sample rows、raw refs、hash、rule/model versions。
- 生成 response plan，默认只 recommend/dry-run。

验收：

- case 可以导出完整证据包。
- FROZEN 后不能覆盖旧证据，只能追加新版本。
- response plan 明确风险、目标、人工确认点和验证方法。

### Phase 19: 安全运营实时 SLA 和可观测性

目标：

- 记录 ingest lag、parse lag、detector lag、case lag、agent lag、alert latency。
- P0/P1 快路径绕过慢研判，先告警和建 case。
- P0/P1 在 3 分钟内产出入口候选、攻击路线草图、影响范围、缺失证据和下一步反查计划。
- status 展示 source health、detector health、case backlog、agent backlog。

验收：

- fixture 中 P0 告警能在目标 SLA 内出现。
- fixture 中“入侵成功 + 横向移动”能在 3 分钟内生成 partial route。
- fixture 中缺少 EDR 或 VPN 字段时，route 输出明确 gaps 和 next_queries。
- agent backlog 堆积时，系统继续 ingestion 和 case 创建，不阻塞数据底座。
- `security status` 能显示每个 source、detector 和 analyst queue 的健康状态。

### Phase 20: 3 分钟入侵路线演练

目标：

- 构造三类端到端攻击 fixture：
  - WAF exploit 成功 -> Web server webshell -> EDR process -> C2。
  - VPN 凭证滥用 -> 服务器登录 -> 横向移动 -> 数据外传候选。
  - 恶意文件落地 -> 沙盒 verdict -> EDR 执行 -> HIDS 文件变化。
- 记录每个阶段耗时。
- 生成 first response report。
- 生成 route、gaps、next_queries、forensic snapshot。

验收：

- 每类 fixture 从最后一条关键日志进入系统开始，180 秒内生成 case 和 route。
- route 至少包含入口候选、时间线、横向路径或阻断点、影响实体、缺失证据。
- 如果证据完整，入口点 confidence >= 0.7；如果证据不完整，必须输出候选排名和 gaps。
- 第一响应报告不能只复述告警，必须跨至少两个日志源关联证据。

### Phase 21: 0day 弱信号和回溯狩猎演练

目标：

- 实现第一批 0day 行为 detector。
- 建 canary/honey signal 接口。
- 建 `suspected_zero_day_intrusion` case 类型。
- 支持新 IOC / 新规则 / 新样本到达后的 retrohunt。
- 在 route 中标注未知入口、未知漏洞候选和缺失证据。

验收：

- 未知 Web exploit fixture 不依赖 CVE 名称也能成 case。
- 低置信 WAF 异常 + EDR 进程异常 + 服务器罕见外联 能合并成高优先级 case。
- 新 hash 情报到达后，能回查历史 parquet/ClickHouse 分区并更新旧 case。
- 缺少 EDR 的资产必须在 case gaps 中标明检测盲区。

### Phase 22: 上层安全运营应用

目标：

- 在底座稳定后建设面向用户的安全运营层。
- 把 case、攻击链、取证、反查、响应和复盘组织成可操作工作台。
- 把 agent 从“后台执行者”变成“安全分析助手团队”。

验收：

- 用户能从一个 P0/P1 case 直接看到入口候选、攻击路线、影响范围、证据、缺口和处置建议。
- 用户能一键发起反查、取证包冻结、响应计划和复盘草稿。
- 上层工作台不绕过底座证据链，所有结论都能回到 evidence refs。

### Phase 23: 主代理派遣健康和轻量默认演练

目标：

- 验证主代理不做长期日志分析，只做调度、摘要和验收。
- 验证所有自动后台能力默认关闭。
- 验证配置缺失时普通用户体验不受影响。
- 验证开启不同能力等级时，只加载对应依赖和 worker。

验收：

- 无 `log_analysis_config.yaml` 时，`my-agent status/run/chat` 行为和耗时不因日志分析功能变化。
- `enabled=false` 时，`my-agent logs status` 只显示 disabled 和启用指引。
- L1 只需要本地文件和 DuckDB/Parquet，不要求 Kafka/ClickHouse/ML 依赖。
- L3 才允许启动持续 worker。
- L4 才允许加载集群后端配置。
- 主代理恢复会话时只读取 case summary，不读取 raw events、长 query 结果或子代理完整 transcript。

## 推荐第一版最小闭环

最小闭环不要贪大：

```text
ingest-file -> parser -> parquet/duckdb -> heavy hitter/spike detector -> case -> analyst subagent -> report
```

先把这个闭环跑稳，再接 API/syslog/kafka 和 ML。

安全运营最小闭环：

```text
WAF/EDR/VPN fixture -> SecurityEvent -> critical detector -> case -> analyst subagent -> attack chain -> forensic package
```

这个闭环跑通后，再扩大到全量传感器和集群后端。

3 分钟硬目标最小闭环：

```text
VPN/WAF/EDR 三源 fixture -> fast detector -> correlated case -> route draft -> first response report <= 180s
```

这个闭环优先级高于完整 ML 平台和完整 SOAR 处置。

0day 最小闭环：

```text
WAF generic anomaly + EDR process anomaly + rare egress -> suspected_zero_day_intrusion -> route draft <= 180s
```

这个闭环优先级高于接入更多 vendor 日志。先证明未知攻击链能被弱信号关联出来，再扩展覆盖面。

## 底座完成后的上层建设路线

底座完成指这些能力已经稳定：多源接入、归一化、去重、查询、检测、case、route、evidence、3 分钟第一响应。完成后，上层建设按下面步骤推进。

### 1. 安全运营工作台

交付物：

- 实时告警队列。
- case 看板。
- 攻击路线视图。
- 实体详情页：资产、账号、IP、域名、hash、进程。
- 证据面板。
- SLA 和 backlog 面板。

必须做到：

- P0/P1 case 第一屏能看到入口候选、影响范围、当前结论和缺失证据。
- 图上每条边都能点回 evidence。
- 能按业务系统、资产重要性、攻击阶段、账号、来源日志过滤。

### 2. AI Analyst Team

把 agent 分成稳定角色，不让一个 agent 包打天下：

- `triage-agent`: 判断优先级和是否升级。
- `route-agent`: 重建入口点、时间线和横向路径。
- `hunt-agent`: 根据 seed 执行反查和扩散搜索。
- `forensic-agent`: 整理证据包和 chain of custody。
- `response-agent`: 生成处置建议和验证步骤。
- `review-agent`: 复核事实、推断、缺口和报告质量。

必须做到：

- 每个 agent 只拿 case evidence 包和查询工具，不拿原始大日志。
- 每个 agent 输出结构化结果。
- reviewer 必须能拒绝没有证据的结论。

### 3. 检测工程工作流

上层要有持续改进检测内容的能力。

交付物：

- rule pack 管理。
- ATT&CK 映射。
- suppression / allowlist。
- detector 测试 fixture。
- 误报反馈。
- 规则版本和回滚。

必须做到：

- 每个新 detector 都有 fixture 和验收标准。
- 每次误报关闭都能沉淀 suppression 或规则调整建议。
- 新情报进入后能触发 retrohunt。

### 4. 0day 和威胁狩猎工作台

上层要能主动找未知攻击，不只是等告警。

交付物：

- seed-based hunt：IP/user/host/domain/hash/process/CVE。
- behavior hunt：行为序列搜索。
- exposure hunt：按暴露面搜索高风险资产。
- weak-signal hunt：多低置信 finding 组合。
- retrohunt：新情报回查历史。

必须做到：

- hunt 结果可以直接升级为 case。
- hunt 过程保存 query history。
- hunt 结论必须标注 facts/inferences/gaps。

### 5. 取证和报告中心

交付物：

- forensic package。
- first response report。
- executive summary。
- technical timeline。
- impacted assets/users。
- evidence export。
- post-incident review。

必须做到：

- 证据包可冻结。
- 报告可复现。
- 报告区分已证实、推断、待查。
- 支持后续导出 JSON bundle，预留 STIX/TAXII。

### 6. 响应和 SOAR 连接

第一阶段只 recommend/dry-run，后续再接执行。

交付物：

- response playbook。
- EDR isolate connector。
- IAM disable/revoke connector。
- WAF block connector。
- DNS/proxy block connector。
- ticket/webhook/IM connector。

必须做到：

- 高风险动作必须人工批准。
- 每个动作有 evidence、approver、result、rollback。
- 响应后必须自动验证是否有效。

### 7. 评估、演练和度量

没有持续演练，这个系统会慢慢变钝。

交付物：

- 攻击 fixture 库。
- 3 分钟 SLA 演练。
- 0day 弱信号演练。
- 紫队演练记录。
- detection coverage map。
- MTTD / MTTR / false positive / false negative 指标。

必须做到：

- 每个核心攻击链都有可重复 replay。
- 每个版本都能回答：哪些攻击能发现，多久发现，缺什么日志。
- 演练失败能自动变成 backlog 和 detector 改进任务。

## 需要尽早决定的问题

- 第一版日志 query DSL 用 SQL、preset 命令，还是两者都支持。
- 本地 parquet 是否引入 pyarrow/pandas，还是优先 duckdb 直接读 CSV/JSON。
- Windows 下文件 tail 和 rotation 的最小可靠策略。
- Case 是否复用现有 subagent workspace，还是单独 `data/log_analysis/cases` 后再关联 subagent。
- 低风险 finding 是只写 case store，还是也进入 LocalStore timeline。
- ML metadata 是否直接进入 Finding，还是单独 ScoreRecord。
- 安全事件字段是否直接采用 ECS/OCSF 风格，还是先做项目内最小 schema 再提供映射。
- 第一版已先采用 `SecurityAlertV1` 用户给定基础告警字段；后续需要决定哪些字段必须人工确认、哪些可由 LLM 辅助映射。
- 软检测器和 LLM 研判的边界：detector 只产出候选和证据，LLM 负责补查和解释，reviewer 负责验收。
- Attack chain 是否单独存为一等对象，还是挂在 case report 内。
- 取证包导出格式是 JSON bundle、目录包，还是后续支持 STIX/TAXII。
- P0/P1 是否需要外部通知 connector，例如 webhook、邮件、IM、SOAR。
- 3 分钟 SLA 从“关键日志到达”开始算，还是从“攻击真实发生时间”开始算；实际工程建议两者都记录，SLA 以 ingest_time 为主，event_time lag 单独告警。
- `route` 和 `attack_chain` 是否合并为一个对象；建议 route 强调入口和路径，attack_chain 强调 ATT&CK 阶段。
- 0day detector 的命名和严重度策略：未知漏洞候选不能直接等同 confirmed compromise，但出现 post-exploit 行为时必须升级。
- Canary/honey signal 是先接外部系统，还是先做最小本地 token/path 命中记录。
- 上层工作台先做 CLI/TUI，还是直接做 Web UI；建议先 CLI/TUI 验证证据链，再做 Web。
- `log_analysis_config.yaml` 是否允许用户指定多份 profile；建议先支持单文件，后续支持 `--profile`。
- 能力等级 L0-L5 的依赖安装方式：建议核心包零重依赖，日志分析 extras 后续再加。
- 自动派遣默认关闭后，P0/P1 快路径是否允许单独开；建议必须显式配置预算。
- 哪些字段的 `0` 表示关闭，哪些字段的 `0` 表示不限制，必须在 config schema 中逐项写明。
- 安全 prompt 开关是否放在独立配置还是主配置；建议独立配置控制，命令场景显式开启。

## 当前优先级判断

优先做：

1. Contract / Config / CLI skeleton，默认关闭，不影响普通用户。
2. `SecurityAlertV1` 导入、中文字段映射、raw_fields 保留。
3. 本地受控查询和 evidence refs。
4. 第一批软检测器 -> Finding -> Case。
5. Route draft：入口候选、时间线、gaps、next_queries。
6. 安全 prompt 开关、analyst/reviewer 子代理契约。
7. Fixture / pytest / 基础款验收。
8. ML / cluster adapter contract。
9. SecurityEvent 扩展到 WAF/EDR/VPN/HIDS/sandbox。
10. 实体图谱、攻击链和取证包。
11. 3 分钟入侵路线演练。
12. 0day 弱信号关联和回溯狩猎。
13. 上层安全运营工作台和 AI Analyst Team。

暂缓做：

- 完整模型训练平台。
- GPU 推理服务。
- 真正分布式调度。
- HTTP/WebSocket 多租户服务。
- 自动执行隔离主机、封禁账号、封禁 IP 等高风险处置。

大白话：先让这个系统能在单机上可靠处理不断增长的日志，并把异常派给 agent 查清楚；再把存储、计算和 ML 后端替换成集群。
## 2026-04-30 Current Landing Note

- Storage Audit slice landed: bad JSONL lines now produce a read audit, sampled corrupt/non-object rows are persisted to `corrupt_lines.jsonl`, and query summaries include skipped/corrupt counts.
- This resolves the earlier backlog item where local JSONL reads skipped bad lines without an audit or metric.
- Next recommended log-analysis slices: align storage query limits with config, wire `logs/security` capabilities into normal run/chat runtime, then build Live Lab scenario replay.

## 2026-04-30 Current Landing Note: Runtime Wiring and Replay

- Runtime capability slice landed: obvious security-log prompts now auto-grant `logs/security` in normal `SimpleAgent.run()` calls, while ordinary prompts still hide security tools.
- Chinese prompt coverage landed: security-log wording such as "security logs / attack / suspicious intrusion" in Chinese is covered by regression tests.
- Query-limit alignment landed: CLI query/hunt/trace commands pass configured `query_max_limit` through tools and storage.
- Live Lab replay landed: `scripts/live_lab/log_analysis_replay.py` runs SecurityAlertV1 fixture through ingest, detector, case, route, evidence, report, and forensic package generation without a real LLM call.
- Accepted evidence: focused tests `23 passed`, full regression `223 passed`, replay on a fresh output root `ok=true`, `dry_run=true`, `total_events=3`, `stored_events=3`, `case_count=1`, `finding_count=1`.

Next recommended worker slices:
- Real analyst dispatch: connect detected cases to controlled analyst/reviewer subagent tasks with evidence refs and parent acceptance.
- Richer replay fixtures: add more attack paths, bad/missing fields, and negative controls.
- User quickstart: document `logs status/ingest/query/hunt-ip/trace-case` plus when runtime auto-grants `logs/security`.
- Workflow entry wiring: let log-analysis development/investigation goals call `plan_workflow_for_goal()` before creating real worker tasks.

## 2026-04-30 Current Landing Note: Replay Gate and Quickstart

- Replay negative fixture landed: `security_alert_v1_no_findings.jsonl` is readable, stores 2 events, then fails at the `detector` stage because no finding is produced.
- Replay summary fields expanded: `fixture_format`, `parsed_events`, `dead_letter_events`, `duplicate_events`, `skipped_events`, `error_type`, and `error_message`.
- User quickstart landed in README and CLI_REFERENCE for explicit LOG commands, ordinary-language runtime capability behavior, and offline replay expectations.

Next recommended worker slices:
- Real analyst dispatch bridge: turn a detected case into controlled analyst/reviewer subagent work orders, still default dry-run/manual.
- Case/evidence negative fixtures: after the dispatch bridge exists, add replay fixtures that fail later than detector.
- LOG workflow plan integration: use `subagents-workflow-plan` as the preview layer before creating investigation workers.
