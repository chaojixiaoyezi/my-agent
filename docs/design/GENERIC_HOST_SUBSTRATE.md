# 通用 Host v2 包管理、单节点调试与认证底座

状态：Wave 9 A2 源码候选，未部署。只扩展通用能力；没有门户业务路由、专项账号库或第二安装/工作流/session权威。

## 入口与授权

十个动作登记在唯一 `plugin_host.action_specs`，仍经原授权、生产组合根及四个领域端口分派。构造不会启动Agent、模型、插件或IdP连接。

第一版要求当前 caller、安装 actor、effective owner **均为本机管理员**，不把“管理员安装了插件”当成普通来访者的管理许可。TOTP目标还必须在原grant的 `manageable_user_ids` 中。

| 动作 | 模式 | 显式能力 |
| --- | --- | --- |
| `plugins.packages.install` | write | `plugins.manage`；引用制品另需 `artifacts.read` |
| `plugins.packages.lifecycle` | execute | `plugins.manage` |
| `workflows.steps.debug` | execute | `workflows.debug` |
| `users.auth.totp.setup/verify/disable` | execute | `users.manage` |
| `users.auth.sso.config.get` | read | `users.manage` |
| `users.auth.sso.config.update/initiate/callback` | execute | `users.manage` |

`plugins.manage`、`workflows.debug` 是新词汇，必须在v9清单中声明并明确批准；旧 `write` / `execute` 不会自动扩权。`workflows.debug` 允许管理员空间内的真实脚本执行，不是旧 `execution.asset_refs` 受限调用的别名，应按代码执行权限审批。源码安装不自动启用这两个能力。

## 包输入与生命周期

`plugins.packages.install` 的 `package_base64`、`package_path`、`artifact_ref` **恰好选一**：

- 小包走base64；仍受原64KiB JSON限制，不扩大broker载荷。
- 大包先置于当前Owner的 `uploads/` 或 `workspace/staging/`，路径逐段nofollow读取。拒绝点段、越界、软/硬链接，不读取任意宿主路径。
- `artifact_ref` 是原注册账的opaque ID，不是路径；沿原任务归属、明确task范围和FD/hash核验取得完整有界字节，不拿公共64KiB分块接口冒充完整读取。
- ZIP及gzip tar入口均限制归档、成员及实际文件展开总量10MiB、成员不超过256。gzip流额外允许1MiB元数据/padding预算，防止在解析tar前无限解压。tar先检查全部可见成员（含EOF零块后的拼接成员），拒绝危险路径、重复/大小写与Unicode别名、链接、设备、FIFO及稀疏成员，再以内存固定元数据生成ZIP；不调用extract。
- 当前仓库规范为根 `plugin.json`，不是任务旧审计中的 `plugin.yaml`。沿原校验器、wheel/随包文件摘要及安装Store，不另造manifest格式。原非Host安装预算不变；旧格式更严格的成员限制不放宽。

可选 `sha256` / `expected_sha256` 校验**原上传字节**。schema只检查字符串类型，领域层检查64位hex及内容（大小写等价）；空串、非hex、长度错误或不匹配统一返回HTTP 400 / `PACKAGE_SHA256_MISMATCH`，发生在安装写入之前。两字段都提供时必须全部匹配，不能用另一字段覆盖错误值。输出 `sha256` 是**最终安装ZIP字节**的摘要，tar规范化后两者可能不同。响应含plugin_id、version、manifest、revision及真实当前status。

新安装默认停用。已有包更新要求显式 `expected_revision` CAS，不能覆盖活跃包或跳过原工具/安装开关。变更要求原幂等键；来源快照同一事务只读一次，成功后的同键重放不因暂存源被清理而再次安装，但会复核当前安装未被替换/卸载。

`lifecycle.action` 支持enable、disable、reload、uninstall、status。status冷读，其他动作沿原管理命令、运行闸与审批；reload只有前一次停用明确成功才启用。待审批、cleanup_pending及未知结果不伪装成完成。HTTP组件回归中的启停/reload只核验真实管理分派；真实插件进程启停和物理清理尚未作为本轮验收。

## 无账本单节点调试

`WorkflowEngine.debug_step(workflow_id, step_id, *, mock_inputs=None, context_overrides=None, timeout_seconds=30.0, allow_side_effects=False)`：

- 读取原WorkflowStore定义与启停闸，只选择指定节点，不先跑上游DAG。原结构化 `condition` 先作为执行门求值，False时返回 `status=skipped`，不会执行被关闭的节点。
- `mock_inputs` 是选定节点输入，并覆盖同名inputs_map；`context_overrides` 是 `{上游step_id: 输出对象}`，供剩余映射读取。引用 `$inputs` 仍读取本次mock输入。
- 构造 `debug-<12hex>`、`ephemeral_debug=True` 的内存run；不调用正式 `_drive`、RunStore创建/保存、工作流/脚本摘要、断点通知或pin推进。沿原runner传 `record_summary=False`，保留数据源预检、启停、沙箱、输出合同、脱敏、超时和清理。
- `allow_side_effects` 必须是严格bool，省略默认为False（Python与Host闭合schema一致）。默认在映射/条件求值后阻止脚本子进程、HTTP、工具调用、子流及任何外部分析器，返回 `dry_run=True, side_effects_blocked=True` 的安全预览，不把预览当业务已执行。不能用config/mock或分析器自报 `side_effect_free` 绕过。
- 只有显式True才执行原脚本链、控制节点或可信节点端口；仍保留原启停、数据源、输出、超时和沙箱门，且关闭正式账本、摘要、断点通知、pin及失败升级。`notification` 的显式业务通知是例外，沿原通知账/outbox并如实报告实际状态。脚本转换模式也必须传原definition，不能丢弃工作流数据源覆盖。
- 失败不触发修复/escalation；返回 `StepDebugResult` 同源字典：`{ok, workflow_id, step_id, step_type, status, duration_ms, output_data, output, rendered_input, condition_branch, logs, error_detail, error, dry_run, side_effects_blocked, side_effects_suppressed, persisted}`。`step_type` 保留Store的原 `mode` 字符串；`output/error` 是脱敏旧字段的独立复制，`side_effects_suppressed=bool(side_effects_blocked)`，`persisted=False` 仅表示不保存正式工作流run，不否认显式许可的业务副作用。全部键在失败、跳过及预览时也存在；无条件时branch为None。`ok=True/status=failed` 表示完成失败调试，不是业务成功。

原三模式之外，Store与debug也接受以下通用mode，统一由资产层 `WORKFLOW_STEP_MODES` 校验，参数放在严格JSON `config`：

| mode | 单节点行为 |
| --- | --- |
| `script_transform` | 无script_id时计算 `config.mapping`（结构化引用）或 `config.expression`（JSON对象表达式）；有script_id则属于外部脚本，默认阻止 |
| `condition` / `condition_branch` | 共用有界AST，计算 `config.expression` 为布尔分支，或单独使用结构化condition；前置门不能覆盖节点自身表达式 |
| `script` | `deterministic_script` 的单步别名；默认抑制，显式True沿原runner执行script_id，不接分析链、不写摘要 |
| `approval_wait` | 默认抑制；True返回 `{state: waiting_for_approval, approved: False, request: 渲染输入}`，不创建或批准正式审批，不把输入approved当授权 |
| `notification` | 默认抑制；True经原Owner `emit_asset_im_notification` 记录/投递，输出 `dispatch_result` 保留recorded及no_channel/queued/unavailable；title默认节点名，message默认输入message或空串，config可指定字符串title/message，payload为脱敏渲染输入；queued不等于已送达 |
| `http_request` / `tool_call` / `subworkflow` | 默认预览；True时只调用可信 `debug_node_handler(spec, context, *, timeout_seconds)`，缺失明确 `NODE_EXECUTOR_UNAVAILABLE`，不自行遍历DAG或构造第二运行账 |
| `llm_prompt` | 与 `llm_skill_analysis` 共用原分析链；Host无live analyzer时固定内存mock预览（`mock=True,llm_called=False`），不是模型调用成功 |

表达式只支持JSON字面量、 `inputs[...]`、有界算术/逻辑/比较/三元式，不使用eval/exec/compile，不允许函数调用、属性、幂或字符串格式化；4096字符、128个AST节点，中间JSON值65,536字符，序列复制在分配前按完整嵌套成本检查。映射和表达式不能承载Python回调。

Host仅从可信组合根读取 `server.workflow_debug_analyzer` 和 `server.workflow_debug_node_handler`，不接受插件JSON注册执行器。无模型时用固定 `workflows.debug_nodes.analysis_preview`，可由 `config.mock_output` 提供mock对象，缺省回显渲染输入；默认门只认可该固定函数对象，不认可任意analyzer的纯属性声明。所有新增通用模式的**正式DAG执行不在本轮范围**，正式入口明确拒绝，原三模式的运行/恢复链不变。

timeout有效范围为 `(0,3600]` 秒；真实脚本子进程沿原执行器落实。外部同步分析器/节点回调的强制抢占、真实HTTP/工具/子流适配、live LLM及门户UI **未验证**；trusted adapter须自行保持原权限、超时和无正式账本约束，不能从脚本超时或受控callback测试外推。

## TOTP 与原会话链

标准库RFC6238：20字节种子、Base32、SHA1、6位、30秒、±1计数窗。setup为10分钟pending，不覆盖已启用MFA；8个64位随机恢复码只存SHA256，不能用恢复码激活pending。种子/URI/恢复码只在setup响应交付，不写通用控制回执。

账号仍是原LocalUserStore已启用普通用户。认证记录在 `<host_root>/local_users_auth/<uid>.json`，私有600原子写、nofollow读取；同一跨进程锁下消费counter或恢复码，成功counter必须递增，10次失败进入持久限流。目录列入原宿主凭据/配置路径策略，模型文件工具不可读、命令不可写。

TOTP种子在pending和active状态均只保存 `secret_ciphertext`，不保存明文 `secret`。每Owner独立32随机字节密钥置于同一宿主凭据保护目录 `local_users_auth/.keys/<uid>.key`（文件0600、新目录0700），而非可由Owner业务文件读口取得的路径。随机16字节nonce、域分离HMAC-SHA256计数PRF异或固定32字节Base32种子，Encrypt-then-MAC绑定版本、UID、nonce和密文；常量时间验MAC先于内存解密。setup安全响应仍交付一次原种子/URI，其他认证响应不返回种子。

密钥与认证记录须成套保护和备份；该封套不是OS密钥链、磁盘或所有认证字段加密。缺失/损坏/链接密钥、坏封套或跨UID移植均失败关闭，不在verify时重新生成密钥；已有pending重新setup也先验证旧封套，不能掩盖丢钥。旧版本明文记录只在首次锁内消费时原位迁移，保留epoch、防重放计数和恢复码，不自行扫描或修改生产数据；未触达的旧记录/备份不因此声称已密文化。`confirm_totp` 与 `verify_totp` 共用原激活、防重放和锁合同。

密码登录和SSO均走原 `user_auth._issue_session`。原密码入口新增可选 `totp_code`，启用MFA后缺码不能签发session；恢复码只是第二因素，不代替密码/IdP。MFA epoch及SSO配置revision并入原token的credential fingerprint，不增加session账。变更撤销旧token，签发冻结认证stamp并重核原账号；SSO消费state时冻结的stamp还必须与签发时一致，防止旧IdP判断获得新配置下的有效token。

## SSO配置与可信协议边界

配置支持OIDC/SAML协议标记、HTTPS issuer/redirect、client_id、client_secret、role_mapping、subject_mapping、enabled；secret返回掩码，掩码回传保留、空串明确清除。只将可信外部稳定subject映射到既有普通UID；角色映射只允许USER，不自动创建账号、升级管理员或相信callback的user_id/verified字段。

配置与握手表分别用 `.sso-config.json`、`.sso-states.json`，前导点不在合法UID命名空间，避免合法用户 `sso-config` / `sso-states` 撞权威文件。

initiate生成300秒、有用途前缀的HMAC state及随机nonce，绑定浏览器关联量和配置revision；有界状态表保证未过期项不通过静默淘汰复活。callback验证签名、期限、nonce、浏览器关联、配置、可信身份映射后，锁内单次消费state，再走原session与MFA门。验证器只得到深拷贝，不能修改宿主授权快照。

**这里只实现配置/状态/映射底座，不实现IdP协议验签或浏览器门户。** 可信组合根可注入 `server.sso_assertion_verifier(config, response, expected_nonce)`，须返回 `VerifiedSSOIdentity`；它负责真实OIDC/SAML协议、签名、issuer/audience、时间、nonce、重放/代码兑换及必要PKCE等验证。入口不能由插件JSON指定，也不能把同形claims字典当作验证结果。未注入时callback明确 `HOST_NOT_READY`，能力目录显示不可用。

`browser_binding` 必须由门户后端从安全浏览器会话/cookie取得，不能把任意前端字符串当作已防登录CSRF。MFA缺失/错误时已经消费的IdP state **不恢复**；本版不提供MFA continuation，调用方须重新发起握手并在callback一并提交第二因素。真实IdP、跨浏览器、网络可达性及生产适配均未验证。

## 验收入口

见根 `TESTS.md` 的Wave 9 A2小节与 `test_wave9_host_substrate_plugins_workflow_auth.py`。组件通过不代表已部署、跨机可达、生产IdP验签或真实插件进程完整生命周期通过。
