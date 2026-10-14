# IM owner 权限与宿主环境安全交接

状态：S-env 已修，源回归与隔离 Linux 实验见下；未由本线提交、推送、打包或部署。

## 已确认问题

宿主通道凭据可以引用任意环境名，原过滤仅按固定名和常见密钥命名，导致不含这些命名的准确 SecretRef 来源进入普通 owner Shell。原 Linux `.20261013.8` bundle 的真实 CLI 解析和原 bwrap Shell 确认合成标记可见；不涉及真实凭据。

修复把原 Root config 中准确来源冻结为内部安全上界，沿主/子孙/后台配置和原进程 DTO 承运，统一在 spawn 前擦洗。没有新增凭据账、模型参数权威或文本黑名单。细节见 [宿主环境合同](../design/HOST_CREDENTIAL_ENVIRONMENT_BOUNDARY.md)。

## 文件归属

- 来源与内部字段：`settings/secret_ref.py`、`config.py`、`config_sources.py`、`parameter_registry.py`；`core.py` 初始化/工具装配、`owner_scoped_pool.py` owner 克隆、两个原 runtime config 重建点。
- Shell：`tooling/_subprocess_env_scrub.py`、`shell.py`、`pty_sessions.py`、registry/原注册与调用点；受控命令仍走原 grant、审批和资源账。
- 同步核验与插件准备：原 Attempt/SandboxProcessOptions、pack verification 作用域/请求、PluginManagementContext/Policy/Operation；仅传宿主来源上界。
- MCP：原 MCPServerConfig 与创建/重连；插件注册/候选/调用、Gateway 面板和事件共用池 factory。默认继承过滤，显式 native env 授权保持。
- 新测试：`test_host_secret_environment_boundary.py`；原 `test_subprocess_env_scrub.py` 增两条 red，`test_shell_stdin.py` 的 fake DTO 补内部空字段，原 stdin 断言不变。参数目录全字段断言由主线适配 host_only，旧诊断字段保留。

## 实测证据

1. 修前钉子：`/tmp/host-secret-env-TDD-red.xml`，2 failed / 6 passed；不是把原过滤规则改成合成名称命中。
2. 新基线入口组合：15 passed；扩大组合曾 138 passed / 2 failed，失败为参数目录新内部字段与主线退休旧字段的夹具，保留原 XML，不把该轮称绿。
3. MCP 默认源 red：`/tmp/host-env-mcp-TDD-red.xml`；修复后 Root 实际握手、显式 native 环境及原 MCP 组合 90 passed，`/tmp/host-env-mcp-source-focused.xml`。
4. Linux 独立源码候选：原 `.8` 自带 Python、原 bwrap，六类入口共九次启动全部来源不可见、普通变量保留，宿主环境不改。证据位于独立测试根的 `post-fix-env/report.json`，其源码归档摘要单独保存，不表示原发行包已经修好。
5. 修改前 broader owner/审批/子孙/云文档回归 557 passed / 13 skipped，`/tmp/owner-permission-security-focused.xml`；这是 pre-fix 审计，不把它累加成最终修复测试成绩。跳过包含平台项，最终整合回归另记。

首轮 35 文件 705 passed / 9 skipped / 24 failed 的原报告在 `/tmp/host-env-final-focused.xml`。24 例均因共享核验替身 Agent 缺正式主配置；只给 `test_pack_verification_service.build_env` 补原 AgentConfig，并断言原 PackVerifierRequest 继承其来源。原核验/取消/输入/保护邻域随后 116 passed，`/tmp/host-env-pack-fixture-after.xml`，未给生产添加缺来源兼容。

主线复核发现来源原型遍历全部配置字符串会把 `system_prompt='env:HOME'` 当机控。正式反例 `/tmp/host-env-source-shape-TDD-red-real.xml` 先红，随后改成六个实际 SecretRef 字段的 schema metadata 和准确凭据来源账，删除自然语言递归遍历。首个反例构造误写不存在字段的准备失败另存，不能当产品 red 证据。

Linux 第二次实际矩阵在独立根 `post-fix-env-final/report.json`：九条原启动入口全部来源隐藏；另两条 MCP 真实握手分别证明默认来源隐藏、明确 native env 保留。该轮源码归档摘要为 `d7e6854e454ddfb9f1b1eacffdd40cdd48f3bbc32a7b1f603b6bbf990ccd00dd`，属于 schema 收窄前候选，最终同源复验另记。

最终 schema 同源 38 文件回归：768 passed / 9 skipped，`/tmp/host-env-final-focused-after.xml`；不是与此前各轮成绩累加。包含正式 Root/MCP 握手、owner/子孙配置、前台/后台/PTY/受控入口、原核验与取消、插件准备与调用、架构 guard。跳过是平台沙箱项，不能据此外推全部平台已真验。

同源 Linux 复验的归档摘要为 `97f695e80b6b6dc412cece901e51887dad8046c004e05872958cd5117ae3c405`，独立根 `post-fix-env-schema/report.json` 的九条启动仍全部来源隐藏/普通变量保留；MCP 默认来源隐藏且明确 native env 保留，宿主环境不改。原 `.8` bundle 仅提供 Python，没有修改其内容或线上 prefix；该证据属于源码候选，不等于最终包验收。

最终全源 Ruff、doc sync、diff check 通过，strict code-size hard=0，报告 `/tmp/host-env-final-schema-code-size.md`。本线产品和文档冻结，主线仍需联合 gate 与最终发行验证。

本线只使用隔离目录、合成凭据和随机端口；不读线上 owner 正文或密钥，不发送 IM，也不调用模型供应商。Linux marker 只报告布尔值/权限码，不输出来源值。

## 已知权限与网络边界

原 Linux 实验确认已注册端口的 Landlock 可拒绝常见 IPv4/IPv6 回环别名和重定向；新 exec worker 的进程级端口注册表可为空，不能据此认定网络上界完整。synthetic relay 能由宿主转发，只证明纵深层限制，不证明现网存在该代理。Gateway 凭据强制和原 G1/G2b 收口由主线维护，本线不放宽 HTTP 门。

普通 owner 的自身文件可读，其他 owner、跨 owner 符号链接、合成宿主配置与 token 文件不可读。Full Access 对获准宿主文件的既有访问是明确权限档语义，本次统一擦洗环境不撤销其原文件权限。

策略损坏原先仅是静态疑点；追加正式入口已经确认 network fail-open，并进一步确认活实例的 Shell 缓存可越过当前禁网。详情、修复与 bootstrap/运行中进程边界见 [Owner 网络完整性](../design/OWNER_NETWORK_POLICY_INTEGRITY.md)。disabled-tool 损坏的正式 Executor 仍拒绝，未确认对应可利用漏洞。

另一条 `ingestion/source_http` 的宿主 SecretRef 解析是独立边界，不能用子进程环境擦洗宣称其安全。该路径由其他开发线用合成资料复现并收口，本线不并发修改，最终发布必须等待主线整合该修复。

## Owner 网络完整性追加

本片独占 `user_space/owner_policy.py` 与 `agent_core/tool_runtime_ledger._attach_owner_network_scope`，新增 `test_owner_network_policy_integrity.py` 和上面的模块合同。存在但失效的权限关闭网络、保留原件；每次原工具入口按当前 owner 策略与创建期/child 上界取交集。没有改其它权限域或引入新授权账。

主链七红与活实例四红分别保存，最终 11 文件 199 passed，`/tmp/owner-network-integrity-final.xml`。macOS 原 Seatbelt 已真验；Linux 最终 18 行原 bwrap/Registry/Executor 矩阵全部符合预期，源码归档摘要为 `88e16690b76df83acc2330e73fd5a920b6e689ce96697187b19126e306a22829`。有效开启的原请求成功，坏记录/禁网/原 false 上界都零请求；普通工具最终禁网错误与云工具 HTTP 前拒绝分别保留。

最终 Ruff、doc sync、strict size（hard=0）与 diff check 通过。产品、测试和本线文档冻结；真实缺失的 bootstrap、既有长进程/PTY 的网络撤权范围和未确认的工具策略损坏边界均在合同中列明。

建议下一步：主线做最终 focused/严格 gate、独立源复核及无源码包 Linux 验收，然后再推送和发布；独立 agent 可并行只读审查 DTO 来源与审批重放，必须保持 native 显式凭据授权和 Full Access 文件边界。
