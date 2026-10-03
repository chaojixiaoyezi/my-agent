# Gateway 本机来源信任与模型命令沙箱（GATEWAY_LOCAL_TRUST）

- **状态**：3a 2026-10-03 定稿，实施中（be 起草，分支 `claude/be-gateway-local-trust` 头 `37a5a2ba7`，基于 `claude/3a-step17h` `a602d6ad6`；并入 step17i）。分工：G4、G5 由 be 实现、ae 评审；G1+G2a、G6 由 my-agent 会话实现、9b 评审；G3 在 G1 之后；G2b 等生产计数归零。ae（第 (1) 层）、9b（第 (2) 层 + 端点清单）评审意见已并入本版。
- **评审**：ae（Seatbelt/Landlock 实测，4 条必须改）、9b（代码核对 + 26 条路由档位，3 条必须改 + 4 条建议）均已采纳并入。
- **方向**（3a 定）：两层都要。
  1. 模型的命令沙箱连不到本机 Gateway：按端口结构化拒绝；macOS 用 Seatbelt，Linux 写清办法和实测计划。
  2. Gateway 的特权入口只认宿主自己持有、模型读不到的凭据。
- **关联**：
  - `HOST_CONFIG_WRITE_GUARD.md`（H3：宿主文件对模型只读、凭据对文件工具不可读）；
  - `PLUGIN_EVENT_HOOKS.md` 8.1 节与第 17 节（`call_origin=host_command` 免收紧的可信度依赖本设计，已列为 3a 待定项）；
  - `ADMIN_CHANNEL_IDENTITY.md`“已知边界”（本机回环调用方即本机管理员）；
  - `HOST_COMMAND_EXECUTION.md`（“网络可信来源不等于操作授权”）；
  - `PLUGIN_HOST_API.md`（插件宿主 API 按激活发令牌、只读、回环）。
- **证据**：
  - be：`~/.my-agent/decision-evidence/gateway-local-trust-20261003/`（Seatbelt 端口拒绝探针；`mapped_v4_verify.py` 独立复核 IPv4 映射绕过与 `*:<port>` 修复——`localhost:<port>` 放行 `::ffff:127.0.0.1` 且服务端认成 `127.0.0.1`，`*:<port>` 全拒）。
  - ae：`~/.my-agent/decision-evidence/review-gateway-local-trust-514d2e934/`（`seatbelt_mapped_v4_probe.py`、`landlock_probe.py`、`landlock_bwrap_probe.py` 等，Linux 车道内核 6.12/ABI 6）。
  - 9b：同目录 `README.md`、`ENDPOINT_TIERS.md`（26 条路由档位）。
  - 所有探针都只连自起的随机端口假服务，没碰生产 8420，无外网流量。

## 0. 结论摘要

1. 缺口是一句话：Gateway 判断“可信”只看对端是不是回环地址（`auth/middleware.py:66`），不带身份头的回环请求直接当本机管理员（`:82`）；而模型命令的沙箱默认联网、不区分回环（`attempt/sandbox.py:67`）。两件事叠加，模型在 `run_command` 里发一个本机 HTTP 请求就落在“本机管理员”这一侧。
2. 文件通道已经被 H3 挡住了（审批决定文件、请求队列都在 `workspace/runtime/` 下，对模型只读）。剩下的缺口全在 HTTP。
3. 权威修复是第 (2) 层：Gateway 每次启动生成一份“本机客户端凭据”，放在模型读不到的位置；回环来源不再自带信任，可信改成“持有这份凭据，或持有原有的 `gateway_auth_token`”。本机的真实客户端（TUI、IM 适配器、CLI 薄客户端）启动时从宿主拿这份凭据；模型命令既读不到凭据、也（第 (1) 层）连不到端口。
4. 第 (1) 层是纵深防护：在模型命令沙箱里按端口拒绝连 Gateway。macOS 已实测可行（见第 2 节）；Linux 用 Landlock 的网络规则，先在 Linux 车道实测再定，内核不支持时如实报结构化事实、绝不悄悄退回放行。
5. 两层的关系：第 (2) 层保证“连上了也没用”，第 (1) 层保证“根本连不上”。任一层单独都不够——只做 (1)，别的本机进程仍能无凭据驱动 Gateway；只做 (2)，凭据分发多一环且 IPv6/未来传输仍可能绕过端口直觉。两层一起上才闭合。

## 1. 第 (2) 层：Gateway 特权入口认宿主持有的凭据

### 1.1 本机客户端凭据

- **是什么**：一个随机串（`secrets.token_urlsafe(32)`），存在模型读不到的位置，本机真实客户端读它、放进请求头。
- **持久化，而不是每次启动换新串**（3a 定，be 核过利弊）：凭据写进数据根 `secrets/` 下的文件，**只在文件缺失时生成**，存在就复用。
  - 为什么不每次启动换：每次部署都要重启 Gateway，客户端若只在启动时读一次凭据，换新串就会让所有在跑的 TUI、适配器在部署后全部失效。持久化后，重启不改串，老客户端继续有效。
  - 换串不白挡什么：Full Access 下文件本来就不是秘密（模型能读数据根），真正的防线是第 (1) 层端口拒绝；其它档下文件模型读不到，每次换并不多挡。所以持久化不降低安全，只去掉部署即失效的坑。
  - 对比另一条路（每次请求或遇到鉴权失败就重读文件再重试）：也能解决部署失效，但要给每个客户端加“401 后重读重试”的分支，复杂且多一次往返。持久化更简单，选它。
  - **轮换**：不做自动轮换。需要换时（例如怀疑泄露）由宿主显式删文件再重启，下次启动重新生成；这是运维动作，不进自动路径。
- **放哪**：数据根 `secrets/` 下一个文件，`0600`、父目录 `0700`，和 `admin-password.json` 同一套权限写法（`user_space/admin_password.py:172-175`）。选 `secrets/` 是因为 H3 已经把“数据根里 owner home 之外任何一层叫 `secrets` 的目录”整个当凭据目录：文件工具连读都拒（`path_access_policy.py` 的 `HOST_SECRET_DIR_NAME`），模型命令在隔离 owner 下也看不到数据根（H2 的 `private_read_roots`）。
  - 已知边界：Full Access（本机管理员、无 owner 墙）下模型命令能读数据根，凭据文件在这一档不是秘密；该档靠第 (1) 层端口拒绝兜底。文档和用例都写明，不夸大凭据在 Full Access 下的作用。
- **谁生成、谁读**：Gateway 宿主进程启动时确保文件存在（缺则生成）；本机客户端用一个稳定的宿主侧读取函数 `load_local_client_credential(data_root)` 读它（见 1.3）。不随停机删除（持久化）。
- **凭据等同宿主身份，按 admin-password 同级对待**（9b）：持有方可声明任意 `X-User-Id`/`X-Channel`，无头即管理员。所以：
  - 不进日志；不放进环境变量或命令行参数（环境会被模型命令和插件子进程继承，命令行参数 `ps` 就能看到）；不出现在 `/status`。
  - 用例要有一条：**模型命令的环境里没有这份凭据**。
- **进插件沙箱的 H2 隐藏路径**（9b，见第 4 节）：G1 把凭据文件路径加进插件进程沙箱的隐藏路径，任何档都登记（打开插件沙箱时生效），让打开沙箱的插件也读不到。

### 1.2 Gateway 怎么认

改 `auth/middleware.py`（唯一入口），分两步上线（见 1.4，强制那步由开关 `gateway_require_local_credential` 控制、见 1.5）：
- **可信** = 请求带的 `X-Gateway-Token` 等于本机客户端凭据，或等于已配置的 `gateway_auth_token`（暴露部署用，保留）。强制打开后，回环地址本身不再等于可信。
- **不带凭据的回环请求**：强制打开后降为匿名 USER，和现在对待“不可信远程来源”一样（`extract_identity` 已有这条分支，`:74`），不认身份头、绝不给管理员；强制打开前只计数、不拦（第一版）。
- **去掉“取不到对端地址就当可信”这一支**（9b）：`_peer_trusted` 现在 `peer_ip is None` 也返回可信。G2b 强制时一起去掉——生产 handler 都有 `client_address`，只有测试替身没有，留着等于一条按“来源未知”放行的旁路。
- **G2b 后旧客户端的错误码**（9b 建议）：要凭据的路由对降匿名/被拒的旧客户端返回结构化码 `LOCAL_CREDENTIAL_REQUIRED`，TUI 和适配器据此提示“请重启客户端”，不让人对着 403 或匿名行为摸不着头脑。
- **公开只读端点保持公开**：`/status`、`/metrics` 维持无需凭据（它们本来就只读、无副作用，监控要用）。这是显式白名单，不是“回环豁免”。
- **插件令牌档**：`/plugin-host/query` 单列为“插件令牌”档，第一道关换成“`X-Plugin-Host-Token` 有效”，不看回环、不要客户端凭据（见 1.3、3.1 第 13 条）。
- **没有中间件时**（`auth_enabled=false`）现在默认管理员（`:129`）。保持：此时 `_guard_network_exposure` 已强制只能绑回环（`http_service.py:384`），是单机无鉴权档，属于用户显式关掉鉴权的选择；文档写明这一档不受本设计保护。

### 1.3 本机客户端清单与取凭据的办法

- **统一入口**：G3 提供一个稳定的宿主侧读取函数 `load_local_client_credential(data_root)`（新 `gateway_parts/local_client_token.py`）。所有本机客户端、以及仓库外的本机工具都用它取凭据、放进请求头，和它们现在附加 `X-User-Id`/`X-Channel` 同一处。接口稳定，仓库外工具才能依赖。
- **仓库内在宿主侧跑的客户端**（直接读文件）：
  - TUI（`cli/chat_client_context.py`、`cli/chat_parts/*`）；
  - IM 适配器（`agent/adapter/manager.py`，它经回环调 Gateway，凭据和现在的身份头一起带）；
  - CLI 薄客户端（`cli/gateway_client.py` 等走 `submit_gateway_ask`/`/control` 的入口）。
  - 它们都在宿主进程里、不在模型沙箱里，读 `secrets/` 下的文件是普通宿主文件读。
- **仓库外的本机工具也是客户端**（3a 指出）：
  - 派活工具 `claude-tools/myagent-dispatch`（走 `submit_gateway_ask` 和 `/control`）；
  - 切换脚本 `switch_gateway.py`（用 `/status` 加信号）——注意 `/status` 在公开白名单里、不需要凭据，但它若还调别的特权入口就要带。
  - 这些工具调 `load_local_client_credential(data_root)` 取凭据，所以这个函数必须是稳定的公开宿主接口。
- **网页端（`frontend/`，浏览器经回环调 Gateway）**：浏览器读不到 `secrets/` 下的文件，不能照上面的办法。
  - 现状：Web 面板（`agent/web/dashboard.py`）是独立只读服务、默认不接进生产 Gateway 控制面（我盘点时没找到生产接线），所以第一期网页端不是控制面客户端，无需凭据。
  - 将来网页端要调控制面时：由宿主在**服务端**把凭据注入到它发出的页面/会话里（页面是宿主经回环发的，宿主读得到文件），浏览器只转发宿主给它的凭据，绝不自己去读文件。这条留给网页端接入时单独设计，本稿只定“浏览器不读文件、凭据由宿主注入”这条边界。
- **插件宿主 API 受影响，要单独处理**（9b 发现）：`/plugin-host/query` 现在第一道关就是 `require_trusted_source`（`plugin_host_api.py:145`），通过后才验自己的 per-activation 令牌 `X-Plugin-Host-Token`。插件进程拿得到这个令牌，但拿不到本机客户端凭据。G2b 一强制，`require_trusted_source` 要凭据，插件宿主 API 第一关就断；G2a 阶段这些请求也会被当成“不带凭据的回环”，计数永远归不了零。
  - 修法：`/plugin-host/query` 这一条路，把“持有效 `X-Plugin-Host-Token`”也当成满足可信来源——它是宿主按激活发的、模型读不到的强凭据，比通用客户端凭据更强。实现上让这条路的可信判定接受“客户端凭据 **或** 有效 host-API 令牌”二者之一（顺序上先认 host-API 令牌、它有效就放行这条路）。
  - G2a 的计数要相应排除：带有效 host-API 令牌的 `/plugin-host/query` 算“已凭据”，不计进 `uncredentialed_loopback_by_endpoint`。
  - 这块并进 G2（见 3 节表）；G2b 的用例要有一条“插件进程只带 host-API 令牌、不带客户端凭据，`/plugin-host/query` 仍通”。

### 1.4 分阶段上线（最重要）

生产现在开着十几个 TUI 会话和飞书适配器，跑的都是旧代码、不会带凭据。如果 G2（回环不再自带信任）和 G3 同一版上线，切换那一刻它们全部变成匿名、全都断。所以分两步：

- **第一版：只上 G1 + G3（不强制）**。
  - Gateway 生成并持有凭据；本机客户端开始附带凭据；但中间件**不强制**——不带凭据的回环请求仍按老规则（可信、无头即管理员）。
  - Gateway 在 `/status` 里加一个结构化计数 `uncredentialed_loopback_by_endpoint`：不带凭据的回环请求有多少。**计数键用路由模板**（`/result/*`、`/sessions/*/bind`），不用原始路径——否则请求编号会进公开的 `/status`、键也没有上界。schema：路由模板 → {计数, 最近一次时间}。只计数、不拦截。
  - **计数范围**：只计“要凭据”和“要管理员”两档；公开路由（`/status`、`/metrics`）不计；插件令牌路由（`/plugin-host/query`）带有效 host-API 令牌的不计（否则永远归不了零）。
  - 观察到计数归零、并确认在跑的旧客户端都重启过（都带上凭据了），再上第二版。
- **第二版：打开 G2 强制**。不带凭据的回环请求降匿名；同时去掉 `_peer_trusted` 的 `peer_ip is None` 旁路（见 1.2）。
- 第 (1) 层（G4、G5 端口拒绝）是纵深防护、和客户端凭据无关，可以独立先上，不受这个两步约束。

### 1.5 G2 强制要不要配置开关

- **建议：做一个开关 `gateway_require_local_credential`，默认 `false`，第二版上线时由宿主显式置 `true`。**
  - 理由：G2 改变安全边界、且有“存量客户端全断”的惊险切换，AGENTS.md 规定这类要有开关；两步上线天然需要一个“现在开始强制”的结构化切点，开关就是它。
  - 不让它变成长期旁路：开关只是上线闸，不是“永久可关的宽松档”。第二版稳定、生产确认后，下一个版本把默认值改成 `true`，再往后把开关连同不带凭据的旧分支一起删（主链路优先、确定不用的旧兼容分支要删）。设计稿和配置注释都写明这个退场计划。
  - 开关要三处同步：`agent_config.yaml`（中文注释）、`AgentConfig` dataclass、`test_constants_catalog` 若涉及常量。
- **不建议**把它永久留成可开可关：那会变成“回环永远可信”的旁路，违背本设计目的。

## 2. 第 (1) 层：模型命令沙箱连不到 Gateway

### 2.1 macOS（已实测，规则用 `*:<port>`）

- 在模型命令的 Seatbelt 配置里加一条按端口的拒绝规则：`(deny network-outbound (remote tcp "*:<port>"))`。
- **为什么不是 `localhost:<port>`**（ae 实测、be 独立复核，证据 `seatbelt_mapped_v4_probe.py`/`seatbelt_mapped_v4_report.json`、be 的 `mapped_v4_verify.py`）：Gateway 只绑 IPv4 `127.0.0.1`。沙箱里用 IPv6 socket 连 `::ffff:127.0.0.1`（IPv4 映射地址）能绕过 `localhost:<port>` 和 `ip` 两种写法，而且 Gateway 看到的对端是 `127.0.0.1`——在 G2b 强制前就是本机管理员。改用 `*:<port>` 后，`127.0.0.1`、`::ffff:127.0.0.1`、`::ffff:7f00:1` 三种目标全被拒（be 复核：server 认出的对端始终是 `127.0.0.1`，`*:` 规则下全部 EPERM）。
- **代价**：`*:<port>` 也会拒掉外部主机的同号端口。Gateway 端口（8420 一类）影响很小，写进已知边界。
- **只对模型命令沙箱置位，不进共用的 `_network_rules`**（9b 发现）：插件进程沙箱复用同一个 `AttemptExecutionSandbox.build_argv` / `_network_rules`。若把端口拒绝直接加进 `_network_rules`，插件沙箱一打开，插件就连不到 `/plugin-host/query`。所以做成一个结构化 spec 字段（如 `deny_gateway_ports: tuple[int, ...]`），只在模型命令沙箱（`tooling/shell._sandbox_exec` 构造的那条）置位；插件沙箱不置位，它的网络由 M 线 B7 管（v8 声明 `network:false` 时连回环都断，带 host_api 的插件只能连 `/plugin-host/query`）。规则放在整份配置最后（Seatbelt 后写覆盖先写，`attempt/sandbox.py:484,514`）。
- 端口用**运行中 Gateway 实际绑定的端口**（`server_address`），不只是 `gateway_port` 配置——覆盖命令行改端口和测试随机端口。
- **G4 用例**：连 `127.0.0.1`、`::1`、`::ffff:127.0.0.1`、`::ffff:7f00:1`、`0.0.0.0`、本机局域网地址各一条；每个“本来能连上”的目标都要断言**服务端认出的对端地址**（只看客户端 errno 不够，要看 Gateway 认成了谁）；断网档行为不变；`full_access=True` 也加（见 2.3）。

### 2.2 Linux：Landlock（ae 在车道镜像实测可行）

- bwrap 现在对模型命令用 `--share-net`（和宿主共享网络命名空间，`tooling/sandbox.py`），回环直达；`--unshare-net` 会连外网一起断，不符合“只挡 Gateway、别的网络不变”。
- **用 Landlock 的 CONNECT_TCP 限制**（`LANDLOCK_ACCESS_NET_CONNECT_TCP`）。ae 车道实测：内核 6.12、ABI 6、Docker 默认 seccomp 下可用（`landlock_probe.py`、`landlock_bwrap_probe.py`）。
- **白名单语义**：Landlock 网络规则只能写“允许哪些端口”。一旦声明管 CONNECT_TCP，没列的端口全被拒。所以“只拒 Gateway 端口”= 给其余 65534 个端口各加一条允许规则（ae 实测 ctypes 加完 0.045 秒）。正因为它按端口、不看地址，IPv4 映射也挡得住；代价和 Seatbelt `*:<port>` 一样，外部同号端口也连不了。
- **挂法**：在 exec bwrap 之前用一个小启动器进程先上 Landlock（需要 `no_new_privs`）再 exec bwrap；限制会传进 bwrap 和其中所有子进程（ae 实测）。
  - **不要用 `subprocess` 的 `preexec_fn`**：Gateway 是多线程进程，Python 文档写明 `preexec_fn` 在多线程下可能死锁。
- **前提（就绪探针逐项探，各出结构化原因码）**：内核 ABI ≥ 4（6.7+）且 Landlock 在 LSM 列表里启用；bwrap 不是 setuid（setuid 的 bwrap 遇到 `no_new_privs` 拿不到特权、起不来）。
- **代价**：每条命令多一个启动器进程 + 约 0.05 秒加规则，合计 < 0.1 秒；G5 用例要量。
- 端口来源同 2.1：用实际绑定端口。

### 2.3 覆盖范围：第 (1) 层挡得住什么、挡不住什么

- **挡得住**：`run_command`、PTY 会话、后台命令都走 `tooling/shell._sandbox_exec`（生产所有 shell spawn 的唯一隔离门，Full Access 也走），端口拒绝 spec 字段在这条路上置位即可全覆盖（含 `full_access=True`——Full Access 下凭据文件模型读得到，这一档恰恰只剩第 (1) 层）。**不置位于插件沙箱**（见 2.1）。
- **挡不住、写进已知边界（见第 4 节）**：宿主侧 `web_fetch` 一类（在 Gateway 进程里发请求、不经沙箱，私网放行后能连回环）、沙箱外的本机转发器（SSH 隧道、socat、本地代理）、同机其它 Gateway 实例（端口规则只知道自己这个 Gateway）。前者建议单独拒 Gateway 端口（见第 4 节并入 G2 的那块）；后两类只能靠第 (2) 层，所以 G2b 不能拖。

### 2.4 “unavailable” 的两种情况（分开处理，各一条用例）

“内核不支持”和“这次施加失败”是两件事，不能一句“由第 (2) 层兜底”带过：

1. **环境没有这个能力**（ABI < 4、Landlock 没启用、bwrap 是 setuid）：命令**照常执行**；结构化事实 `gateway_isolation=unavailable` 加原因码，写进沙箱就绪/边界事实和 `/status`，启动时记一次日志。这个事实**要被机器读**：它为 unavailable 时，G2b 不能一直关着不上，M 线 `call_origin=host_command` 免收紧也要读它。用例：模拟 ABI < 4 → 命令照跑、事实是 unavailable。
2. **能力有、但这次施加失败**（ruleset / add_rule / restrict_self 报错）：这是故障，**fail-closed**，命令不执行，报 `SANDBOX_UNAVAILABLE` 一类的码，绝不在没限制的情况下照跑。用例：模拟 `restrict_self` 失败 → 命令被拒、没执行。

## 3. 实施拆分

| 块 | 内容 | 主要改动 | 用例 | 评审 |
| --- | --- | --- | --- | --- |
| G1 | 本机客户端凭据：缺则生成、落盘（`secrets/`，0600）、持久（不随停机删）、`load_local_client_credential(data_root)` 读取函数 | 新 `gateway_parts/local_client_token.py`、`http_service.py` 启动钩子 | 缺文件时生成、存在时复用、文件权限 0600/0700、重启后串不变；Full Access 下凭据文件可读这一边界写进用例说明 | be |
| G3 | 本机客户端附凭据：TUI、IM 适配器、CLI 薄客户端、仓库外工具经同一读取函数取凭据 | `cli/chat_client_context.py`、`agent/adapter/manager.py`、`cli/chat_parts/*`、`cli/gateway_client.py` | 客户端带上凭据后功能不变；取不到凭据时明确报错不静默降级 | 9b |
| G2a | **第一版（不强制）**：`/status` 加按端点的“不带凭据回环请求”计数，中间件先不拦；`/status`、`/metrics` 公开白名单；带有效 host-API 令牌的 `/plugin-host/query` 算已凭据、不计 | `auth/middleware.py`（只计数）、`http_service.py`（`/status` 字段、白名单路由）、`plugin_host_api.py` | 不带凭据回环仍按老规则通，但被计数（按端点）；带凭据=原身份；公开端点仍通；带 host-API 令牌的 plugin-host 请求不计 | be、9b |
| G2b | **第二版（强制）**：开关 `gateway_require_local_credential` 打开后，不带凭据回环降匿名；`/plugin-host/query` 接受有效 host-API 令牌当可信 | `auth/middleware.py`、`settings/config.py`（开关默认 false）、`agent_config.yaml`、`plugin_host_api.py` | 开关开：不带凭据回环=匿名 USER、不认身份头、非管理员；**插件进程只带 host-API 令牌、不带客户端凭据，`/plugin-host/query` 仍通**；开关关：行为同 G2a；无中间件档行为不变 | be、9b |
| G6 | 宿主侧抓取工具（`web_fetch`、`watch_stream`）对 Gateway 端口单独拒绝（不受私网放行影响；这些请求永不带凭据） | `contracts/gates/network_safety.py`；读实际绑定端口 | `MY_AGENT_ALLOW_PRIVATE_URLS=1` / `allowed_private_hosts` 放开私网后，`web_fetch`/`watch_stream` 连 Gateway 端口仍被拒；连其它私网主机不受影响 | be、ae |
| G4 | macOS 端口拒绝（`*:<port>`，用实际绑定端口；`full_access` 也加） | `attempt/sandbox.py`（`_network_rules` 旁加 `*:<port>` 拒绝）、从 `server_address` 读端口 | 真沙箱：`127.0.0.1`/`::1`/`::ffff:127.0.0.1`/`::ffff:7f00:1`/`0.0.0.0`/局域网地址都被拒、断言服务端认出的对端；其它端口通、自测监听通；断网档不变；`full_access=True` 也拒 | be、ae |
| G5 | Linux 端口拒绝（Landlock CONNECT_TCP，启动器 exec 前上规则） | 新启动器 + `tooling/sandbox.py`、沙箱就绪探针（三前提逐项探） | Linux 车道：支持时被拒（含 IPv4 映射）、其它端口通、传进 bwrap 子进程；ABI<4/未启用/setuid → 命令照跑且事实 unavailable；restrict_self 失败 → fail-closed 不执行 | ae |

- 排序：G1 → G3 → G2a（第一版上线、观察计数归零）→ G2b（第二版强制）；G4、G5、G6 是纵深防护、与凭据无关，可独立先上（G4/G5 挡沙箱内命令，G6 挡宿主侧抓取工具）。
- 每块完成后跑相关测试 + guards9（照 3a 工作规则）。
- 新增错误码、配置项进常量目录与 `agent_config.yaml`（中文注释）+ `AgentConfig` dataclass，并跑 `test_constants_catalog.py`。预计新增：开关 `gateway_require_local_credential`（默认 false）；结构化事实键 `gateway_isolation`（`unavailable` 等，机器可读）；`/status` 字段 `uncredentialed_loopback_by_endpoint`（schema：路由模板 → {计数, 最近一次时间}）；错误码 `LOCAL_CREDENTIAL_REQUIRED`。

### 3.1 G2 的访问控制清单（9b 已产出，作 G2b 用例依据）

四档定义：
- **公开**：无需凭据、只读、无副作用。仅 `/status`、`/metrics`。
- **要凭据**：带本机客户端凭据或 `gateway_auth_token` 才按请求头身份（`X-User-Id`/`X-Channel`）处理，否则降匿名。
- **要管理员**：在“要凭据”之上，解析出的 owner 还必须是本机管理员。
- **插件令牌**（新增一档）：只认 per-activation 的 `X-Plugin-Host-Token`，不认也不要求客户端凭据。仅 `/plugin-host/query`。

9b 对着 514d2e934 的 `http_service.py` `do_GET`（8 条）/`do_POST`（18 条）逐条填了 26 条路由的档位，全文见 `~/.my-agent/decision-evidence/review-gateway-local-trust-514d2e934/ENDPOINT_TIERS.md`。要点：
- 公开 2 条（`/status`、`/metrics`）；插件令牌 1 条（`/plugin-host/query`）；要管理员 4 条（`/sessions/*/channels`、`/admin/summary`、`/stop`、`/sessions/*/bind`）；`/control` 里 `/restart`、`/settings`、`/recover` owner 级、`/endtask`、`/wakes`、`/permissions full-access`、`/plugins` 管理操作在各命令服务内按本机管理员判定；其余为要凭据。
- 最敏感的用户级写入是 `/client/agent-permission`（写审批决定、能放行等待中的工具调用），G2b 用例必须覆盖。
- 横向规则（G2b 用例）：要凭据/要管理员路由在不带凭据时一律降匿名、不认身份头、绝不给管理员；去掉 `peer_ip is None` 旁路；公开路由与插件令牌路由不进计数、不参与归零；无鉴权档（`auth_enabled=false`）只断言“不变”。
- 本稿和清单都不写利用步骤，只给档位与逐条对照结论。

## 4. 风险与边界

- **Full Access**：模型命令能读数据根，凭据文件在这一档不是秘密；该档下的防线是第 (1) 层端口拒绝。文档和用例都要写明，不夸大 G1 在 Full Access 下的作用。
- **无鉴权档**（`auth_enabled=false`）：只绑回环、默认管理员，属于用户显式选择，不在本设计保护范围，文档写明。
- **第 (2) 层真正能防谁**（9b 更正，不是“唯一防线”那么宽）：凭据文件是 0600，**同一系统用户下没进沙箱的进程都读得到它**，而且这类进程还能绕开 HTTP 直接写文件队列（`gateway_parts/request_client.submit_gateway_ask` 走的是文件队列，是 Gateway 的**第二个入口**，不经中间件）。所以第 (2) 层能防的是两类：**非 Full Access 档下的模型命令**（读不到数据根），和**其它系统用户**的本机进程（现在它们连回环就被当管理员，这是凭据要修的真缺口）。它**防不住**同一用户、没进沙箱的进程——这类靠的是操作系统的用户隔离，不是本设计。
  - 文件队列入口（第二入口）对模型命令由 H3 的 `workspace/runtime` 写保护挡住；同一用户的宿主进程能写它，属于上面“防不住”的范围，写进边界。
- **插件沙箱关着时插件与宿主同信任域**（9b）：插件进程沙箱默认关。关着时插件就是“同一用户、没进沙箱的进程”，读得到凭据。所以 G1 要把凭据路径加进插件沙箱的 H2 隐藏路径（任何档都生效，打开沙箱时才有意义），并在 `PLUGIN_EVENT_HOOKS.md` 8.1 节加一句引用，供 `call_origin=host_command` 定档用。
- **第 (1) 层挡不住、靠第 (2) 层的三类路径**（ae 实测/点明）：
  - **宿主侧抓取工具**（`web_fetch`、`watch_stream`）：在 Gateway 进程里发请求、不经沙箱，且永不带凭据。默认网络安全闸拒回环，但 `MY_AGENT_ALLOW_PRIVATE_URLS=1` 或 `allowed_private_hosts` 放开私网后，模型能让宿主自己去连 Gateway 端口，对端是回环、G2b 前即管理员。→ G6：安全闸对 Gateway 端口单独拒绝，不受私网放行影响。
  - **沙箱外的本机转发器**：SSH 隧道、socat、本地代理——只要把连接转到回环就绕过端口规则。这台机器的 7890 代理实测没转（`seatbelt_proxy_hop_report.json`），但不是保证。只能靠第 (2) 层。
  - **同机其它 Gateway 实例**：端口规则只知道“自己这个 Gateway”的端口；测试车道 Gateway 里的模型命令能连到生产 8420。可选做法：从结构化的本机 Gateway 实例登记表读出全部端口一起拒；不做就靠第 (2) 层——所以 G2b 不能拖。
- **IPv4 映射地址**：Seatbelt 规则必须用 `*:<port>`（不是 `localhost:<port>`），否则 `::ffff:127.0.0.1` 能绕过且服务端认成 `127.0.0.1`（2.1，ae 实测 + be 复核）。Landlock 按端口不看地址，天然覆盖。
- **`*:<port>` 的代价**：沙箱内也连不了外部主机的同号端口；Gateway 端口影响很小，接受。
- **未来传输**：若以后加 unix socket 传输，第 (1) 层要同步按 socket 路径拒绝（现在只有 TCP，`http_service.py`）。
- **监控**：`/status`、`/metrics` 保持公开是刻意的；若未来它们开始带敏感字段，要重新评估白名单。
- **逐接口核对**：哪些端点在新规则下各自落在“公开/要凭据/要管理员”，由评审人（9b）在实施阶段对着 `http_service.py` 的路由分发逐条核对（见 3.1），本稿不展开清单。
- **存量客户端切换**：分阶段上线（1.4）就是为这个——第二版强制前必须确认 `/status` 的不带凭据回环计数归零、旧客户端都重启过，否则切换会把在跑的会话全断。
- **部署即失效**：凭据持久化（1.1）后重启不换串，老客户端读一次即长期有效；不采用“每次启动换新串”。
