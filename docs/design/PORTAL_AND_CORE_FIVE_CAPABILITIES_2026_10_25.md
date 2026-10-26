# 七大核心底座、TUI 交互增强与插件宿主接口扩展详细设计书（2026-10-25 定稿）

> **状态**：设计已拍板定稿（待主指挥 Session 拆单派工落地）
> **范围**：围绕日常多目录开发、主会话分支探索、对话记录一键提炼一等公民、统一数据源钥匙串（本机先行+平滑迁移）、浏览器操作录制转固定脚本、**TUI 任务结束耗时与时间记录行**、**TUI 免 Sleep 后台驻留任务状态尾标与 `↑↓` / `Ctrl+G` 观察视图**，以及**全套 Plugin Host API v2 / Plugin Broker 接口暴露**。
> **明确排除项（不做）**：
> 1. ❌ **结束拦截 / 审稿人接口（Turn-Stopping Reviewer Hook）**：不做（现有 `/goal` 与 `goals.*` 接口已暴露，完全覆盖长任务持续推进需求）。
> 2. ❌ **远端 SSH 工作区底层 Provider 抽象**：不做（现有命令与固定脚本直接执行 `ssh` 已满足需求）。
> 3. ❌ **子代理 Fork 主代理历史（Subagent Prefix Fork）**：不做！子代理坚定保持现有的**“干净隔离上下文（Fresh Context）+ 支持工具自定义（`allowed_tools` / 角色模板 / 动态申请）”**，避免背负 200K 历史包袱滚雪球消耗 Token 及自定义工具破坏前缀缓存。**仅保留主会话分叉（Main Session Fork）**。

---

## 一、七项核心能力总览与定位

| 编号 | 模块代号 | 能力名称 | 涉及层级 | 核心价值 |
| :--- | :--- | :--- | :--- | :--- |
| **能力 1**<br>*(原 #2)* | `CAP-DIR` | **`my-agent --dir <目录>` 启动 + 同目录多会话管理 + 目录级/全局资产双层合并** | 底座 CLI / 会话存储 / 资产发现 / Host API v2 | 像主流 Coding Agent 一样在任意项目目录直接启动、管理该目录下的多个独立会话，自动合并 `<dir>/.agent/*` 与 `~/.my-agent/*` 的四类一等公民与 `AGENTS.md`。 |
| **能力 2**<br>*(原 #3)* | `CAP-FORK` | **主会话任意回合分叉（Main Session Fork）** | 底座会话存储 / CLI `/fork` / Host API v2 | 在主会话任意回合（或指定消息序号）一键克隆历史消息、压缩基线与会话设置，拉起独立新会话探索不同方案，零子代理精分与破缓存风险。 |
| **能力 3**<br>*(原 #7)* | `CAP-EXTRACT` | **对话记录按时间筛选 + 勾选 $N$ 个回合 ➔ AI 提炼一等公民全家桶** | 底座提炼服务 / 工具链 / Host API v2 / Web 门户 | 勾选 $N$ 次用户提问（自动连带底下 AI 的工具调用、命令与回复），由 Agent 拆解生成 **1 个工作流 + 若干固定脚本 + 1 个 Skill + 可选监控** 的预览草稿包，用户挑选确认后一键入库。 |
| **能力 4**<br>*(原 #6)* | `CAP-DATASOURCE` | **统一数据源钥匙串（本机零成本建库 + 代号绑定 + 远端零改动迁移）** | 底座数据源存储 / 脚本运行时 / Agent 工具 / Host API v2 | 所有固定脚本、工作流只绑定逻辑代号 `source_id`（不写死地址密码）；支持零配置本机 SQLite 或让 Agent 本机建库先行规范化，后续改地址或让 Agent 一键迁移至远端库。 |
| **能力 5**<br>*(原 #5)* | `CAP-RECORDER` | **浏览器操作录制 ➔ 一键转可传参固定脚本 / 工作流步骤** | 底座录制解析器 / 脚本生成 / Host API v2 / Web 门户 | 将浏览器中的页面跳转、输入框填写、点击、提取文本/表格等动作序列转化为带 `${参数}` 变量的确定性固定脚本（`Script`），可直接单独跑或挂入工作流/监控。 |
| **能力 6**<br>*(新增 TUI-1)* | `CAP-TUI-TIMING` | **TUI 每轮任务结束后的“耗时 + 完成时间”消息流尾标行（对齐图 1）** | Gateway 回合账本 / TUI 消息块渲染 (`tui_block_renderer.py`) | 每轮任务结束时，在 Assistant 回复正文下方、`◉ Context` 状态栏上方固定显示一行 `✻ 耗时 3m 46s · 完成于 08:08`（随历史持久化，不覆盖底部快捷键栏）。 |
| **能力 7**<br>*(新增 TUI-2)* | `CAP-TUI-BG-WATCH` | **TUI `F6 原生模式` 后置彩色【⚡ 后台驻留 N 项】指示器 + `↑↓` 移动查看 + `Ctrl+G` 退出（解决图 2 痛点）** | Gateway 进程状态投影 / `tui_agent_navigation.py` / `tui_block_renderer.py` | 当后台任务免 Sleep 挂起等待结束/异常唤醒时，在底部 `F6 原生模式` 后用醒目色彩显示 `· ⚡ 后台驻留 N 项`；按 `↑↓` 先移到此处再移到子代理，按 `Enter` 查看后台实时日志与存活状态，`Ctrl+G` 退回。 |

---

## 二、逐项详细架构设计与需求规格

### 能力 1（`CAP-DIR`）：`my-agent --dir <目录>` 启动 + 目录多会话 + 资产双层合并

#### 1.1 需求背景与用户体验
1. **命令行直达目录**：
   - 支持顶层启动参数：`my-agent --dir <path>`（以及 `my-agent chat --dir <path>`、`my-agent resume --dir <path>`；若不传 `<path>` 或传 `.` 则解析为当前工作目录 `os.getcwd()` 的规范绝对路径）。
   - 当用户直接运行 `my-agent --dir /path/to/proj` 时，无需再写子命令 `chat`，直接进入绑定 `/path/to/proj` 的交互式 TUI / 会话界面。
2. **同一目录下的多会话（Directory-Scoped Multi-Sessions）**：
   - 每个主会话（`ConversationThread`）在 `metadata` 中持久化结构化字段：
     - `workspace_root: str`（规范化绝对路径，如 `/path/to/project`）
     - `workspace_roots: list[str]`
   - 在 `--dir <path>` 下启动或使用 `/sessions`（及插件接口）时：
     - 默认优先列出绑定到当前 `workspace_root == <path>` 的会话列表（按最近活跃时间倒序），支持 **【➕ 在当前目录新建会话】**、**【🔄 切换同目录历史会话】**，也支持加 `--all` 查看全机所有目录的会话。
3. **目录级资产与个人全局资产双层自动合并（Workspace + Owner Overlay）**：
   - 在绑定了 `workspace_root` 的会话中，Agent 与插件看到的四类一等公民及规则是**“当前目录资产 + 用户个人全局资产”的并集**，且同名 ID 时优先级固定为 **`workspace (<dir>/.agent/*) > owner (~/.my-agent/*) > builtin`**：
     - **规则（`AGENTS.md`）**：全局 `<owner_home>/AGENTS.md` + 项目 `<dir>/AGENTS.md`（及 `<dir>/.agent/AGENTS.md`），同轮合并注入提示词且遵循人格快照缓存纪律。
     - **技能（`Skill`）**：`<dir>/.agent/skills/*/SKILL.md` + `<owner_home>/skills/*/SKILL.md` + `<owner_home>/skills/learned/*/SKILL.md` + 内置 skills。
     - **固定脚本（`Script`）**：`<dir>/.agent/scripts/<script_id>/` + `<owner_home>/scripts/<script_id>/`。
     - **工作流（`Workflow`）**：`<dir>/.agent/workflows/<workflow_id>/` + `<owner_home>/workflows/<workflow_id>/`。
     - **定时监控（`Monitor`）**：支持在 `<dir>/.agent/monitors/` 声明项目级监控模板，或关联当前 `workspace_root`。
   - 每个资产描述符 `AssetDescriptor` 新增结构化字段 `scope_layer: "workspace" | "owner" | "global"` 与 `workspace_root: str`，让 Web 门户、TUI 和插件一眼分清“这是当前项目专属的脚本/工作流，还是我个人的全局资产”，并支持将目录级资产一键提升（Promote）到个人全局资产库。

#### 1.2 核心代码落点
- [`agent_py_agent/cli/parser.py`](agent_py_agent/cli/parser.py)：在 `_build_root_parser()` 中注册顶层 `--dir`（别名兼容 `--workspace`），并在 `build_interactive_parser` / `cmd_default` 中把 `--dir` 透传给会话启动流程。
- [`agent_py_agent/cli/workspace_resolution.py`](agent_py_agent/cli/workspace_resolution.py)：统一 `resolve_cli_workspace_dir(args)`，校验目录存在性、软链接规范化及路径访问策略（`path_access_policy.py`）。
- [`agent_py_agent/agent/assets/script_store.py`](agent_py_agent/agent/assets/script_store.py) 与 [`workflow_store.py`](agent_py_agent/agent/assets/workflow_store.py)：扩展可选 `workspace_root: Path | None = None` 参数；读取时合并 `<workspace_root>/.agent/{scripts,workflows}` 与 `<owner_home>/{scripts,workflows}`，写入时支持指定 `target_scope="workspace" | "owner"`（默认 `"owner"`，在项目内显式指定时写 `"workspace"`）。

---

### 能力 2（`CAP-FORK`）：主会话任意回合分叉（Main Session Fork）

#### 2.1 需求背景与架构边界
- **只做主会话分叉，不做子代理分叉**：
  - 用户在与主代理对话过程中（例如第 20 轮已经读完代码、对齐了背景），想尝试两套不同的实现方案，或者把前 15 轮干净的上下文保留下来另开一条新任务线。
  - 支持从**当前最新回合**直接分叉，也支持**指定截断到某一条历史消息 `up_to_message_id`（或 `up_to_seq`）** 进行“时光机式分叉”（即只保留前 $K$ 轮，丢弃第 $K$ 轮之后的跑偏对话，并在分叉出的新会话首轮可选附带一段用户补充说明 `fork_note`）。
- **安全回合边界对齐（Safe Turn Boundary）**：
  - 当指定 `up_to_message_id` 时，底层的分叉截断器自动校验该位置是否处于未闭合的 `tool_use -> tool_result` 中间；若是，自动向后或向前对齐到完整闭合的回合边界，杜绝新会话首次调用大模型时报 `HTTP 400 Invalid Request`。
- **完整继承的元数据与谱系**：
  - 分叉产生的新线程 `new_thread_id` 在 `ConversationThread.metadata` 中记录结构化谱系：
    ```json
    {
      "forked_from_thread_id": "thread-xxx",
      "forked_at_message_id": "msg-yyy",
      "forked_at_seq": 24,
      "forked_at_timestamp": 1761400000.0,
      "workspace_root": "/path/to/proj"
    }
    ```
  - 同时继承原会话的 `workspace_root`、`model_profile_id`、`reasoning_effort`、人格冻结快照（`persona/thread_prefix`）以及截断点之前的已提交 Compact 摘要基线，使分叉后的新会话在 5 分钟缓存窗口内发起第一轮请求时**直接命中原主会话的 Prompt Cache**！

#### 2.2 交互入口
1. **TUI / CLI 斜杠命令**：
   - `/fork [新会话标题]`：从当前最新回合一键分叉并自动切换到新会话；
   - `/fork --upto <seq_or_msg_id> [新会话标题]`：从指定历史轮次分叉并切换到新会话。
2. **Web 插件门户与 Host API v2 接口**：
   - 在对话记录或会话列表中点击 **【🔀 从此处分叉新会话】**，立即拉起带完整前序记忆的新会话。

#### 2.3 核心代码落点
- 新增 `agent_py_agent/agent/conversation/thread_fork_service.py`：实现 `fork_conversation_thread(store, *, source_thread_id, up_to_message_id="", title="", fork_note="", actor_owner="") -> ConversationThread`。
- 在 [`agent_py_agent/agent/plugin_host/conversations.py`](agent_py_agent/agent/plugin_host/conversations.py) 与 [`action_specs.py`](agent_py_agent/agent/plugin_host/action_specs.py) 暴露标准动作 `conversations.threads.fork`。

---

### 能力 3（`CAP-EXTRACT`）：对话记录勾选回合 ➔ AI 提炼一等公民全家桶

#### 3.1 核心交互流程（按用户确认的完整闭环）
1. **第一步：按时间段/会话筛选并勾选“用户提问回合（Turn Bundle）”**
   - 用户在【🕘 近期对话记录】中先选时间范围（`1天 / 3天 / 7天 / 14天 / 30天 / 全部 / 自选起止日期`）或指定某个会话。
   - 界面按**“每一次用户提问（User Turn）”作为主勾选单元**展示（例如：今天跟某个 Agent 发了 20 条消息完成了一个复杂的查库+清洗+生成报表任务）。
   - 用户直接勾选其中的 $N$ 次提问（如勾选第 3、5、8..18 次提问）——**系统自动将每次提问底下紧跟的 AI 思考、调用的所有工具命令（`run_command` / `read_file` / `web_fetch` 等）、工具输出摘要及 AI 最终回复，自动打包成完整的 `TurnExecutionBundle`（无需用户手动去勾零散的工具记录）**。
2. **第二步：选择期望提炼的目标范围并点击【✨ 让 AI 提炼可复用资产】**
   - 用户可勾选希望 AI 评估提炼哪些类型（默认全选：`☑ 工作流 (Workflow)`、`☑ 固定脚本 (Script)`、`☑ 技能说明 (Skill)`、`☐ 定时监控 (Monitor)`），并可填写一句补充要求（例如：*“把里面写死的日期和订单号变成可传参变量”*）。
3. **第三步：AI 输出结构化“提炼建议全家桶（Extraction Proposal Bundle）”**
   - 后端提炼服务（`ConversationAssetExtractor`）分析这 $N$ 个回合的真实工具执行轨迹与输入输出，生成一份结构化的草稿包（保存到 `<owner_home>/data/asset_extractions/<extraction_id>.json`）：
     - **建议提炼的固定脚本（`proposed_scripts: list[DraftScript]`，可为 1~多个）**：把回合中跑通的 Shell/Python/SQL/API 命令行去重、剥离硬编码参数（提炼为 `io_contract.inputs` 参数表）、将数据库/API 连接替换为 `source_id` 代号，生成标准 Python/Bash 固定脚本草稿。
     - **建议提炼的技能（`proposed_skills: list[DraftSkill]`，0~1 个）**：把对话中摸索出来的领域排障经验、判断标准、分析方法论浓缩为标准 `SKILL.md` 草稿。
     - **建议提炼的工作流（`proposed_workflows: list[DraftWorkflow]`，0~1 个）**：把刚刚提炼出的几个固定脚本和 Skill 按执行先后顺序自动串联成多步 `Workflow` 草稿（每一步已自动填好 `script_id` 或 `skill_id` 及步骤间参数映射）。
     - **建议提炼的定时监控（`proposed_monitors: list[DraftMonitor]`，可选 0~1 个）**：如果该流程适合周期巡检，给出 Cron 表达式与绑定的 `workflow_id`/`script_id` 草稿。
4. **第四步：用户在界面上预览、微调、勾选想要的项 ➔ 一键入库**
   - 用户在弹窗中看到 AI 建议的这套组合（比如：`1 个工作流 + 3 个固定脚本 + 1 个 Skill`），每一项都可以展开看代码/步骤、改名字、单独勾选或取消勾选。
   - 点击 **【✅ 确认导入所选资产】**，后端按依赖顺序（先落盘被依赖的 `Script` 和 `Skill`，再落盘引用它们的 `Workflow` 和 `Monitor`）原子写入正式资产库，当场即可试跑！

#### 3.2 核心代码落点
- 新增 `agent_py_agent/agent/assets/turn_extractor.py`：
  - `collect_turn_execution_bundles(owner_home, *, thread_id="", turn_ids=(), since=0.0, until=0.0) -> list[TurnExecutionBundle]`：根据用户勾选的 `user_message_id` / `turn_id` 列表，自动聚合每条用户消息直到下一条用户消息之间的全部 `assistant` 回复与 `ToolTelemetryLedger` 工具调用明细。
  - `propose_assets_from_turns(agent_or_home, *, bundles, target_kinds=("workflow", "script", "skill", "monitor"), user_hint="", workspace_root="") -> ExtractionProposalBundle`：调用模型（带严格 JSON Schema 校验）生成全家桶草稿并持久化到 `<owner_home>/data/asset_extractions/<extraction_id>.json`。
  - `commit_extracted_assets(owner_home, *, extraction_id, selected_item_ids, target_scope="owner", workspace_root="") -> dict`：将用户勾选确认的草稿项原子写入 `ScriptStore`、`WorkflowStore`、`skills` 与 `SchedulerRepository`。

---

### 能力 4（`CAP-DATASOURCE`）：统一数据源钥匙串（本机先行 + 代号绑定 + 零改动迁移）

#### 4.1 需求背景与三阶段丝滑演进
1. **核心铁律：脚本与工作流代码里只认 `source_id` 代号，绝不写死地址、库名、账号密码**
   - 在 `<owner_home>/data/datasources/registry.json`（敏感密码/Token 走现有凭据保险箱 `credentials` 引用 `credential_ref`，不落盘明文密码）中维护统一的数据源钥匙串。
2. **支持的数据源类型与“本机零门槛起步”**：
   - **`sqlite_local`（默认零安装本机库）**：一键在 `<owner_home>/data/datasources/<db_name>.sqlite` 创建本机 SQLite 数据库，开箱即用。
   - **`mysql` / `postgresql` / `clickhouse` / `redis` / `http_api`**：支持本机安装的数据库（如 `127.0.0.1:3306`）或远端数据库。
   - **Agent 自动建库与代管工具（`datasources` 工具）**：
     - 用户可直接对 Agent 说：*“帮我在本机建一个叫 `order_analytics` 的库（SQLite 或本机 MySQL），建好订单表，并登记到数据源钥匙串里，代号叫 `biz_order_db`。”*
3. **固定脚本如何零配置读取数据源**：
   - 当 `ScriptRunner` 运行声明了 `data_sources: ["biz_order_db"]` 的固定脚本时，宿主自动注入：
     - 环境变量 `MY_AGENT_DS_BIZ_ORDER_DB_JSON`（仅在受控沙箱进程内可见，且输出日志经过 `redact_bearer_values` 自动脱敏）；
     - 标准 Python 辅助库 `from agent_py_agent.runtime_sdk import get_datasource, query_sql`：脚本里只需写一行 `rows = query_sql("biz_order_db", "SELECT * FROM orders WHERE dt = :dt", {"dt": inputs["dt"]})`！
4. **后续平滑迁移（一行脚本代码都不用改）**：
   - 以后从本机 SQLite / 本机 MySQL 迁移到远端生产库时：
     - 要么在门户【🔑 数据源钥匙串】里把 `biz_order_db` 的连接地址、库名、账号改到新服务器；
     - 要么直接调用 `datasources.migrate`（或让 Agent 执行迁移）：自动把旧源的表结构与数据同步到新源、跑连通性校验、再原子切换 `biz_order_db` 的指向！所有绑定了 `biz_order_db` 的固定脚本、工作流、定时监控**零代码改动无缝切换**。

#### 4.2 数据源描述符结构（`DatasourceSpec`）
```json
{
  "source_id": "biz_order_db",
  "name": "核心业务订单库",
  "driver": "sqlite | mysql | postgresql | http_api",
  "host": "127.0.0.1",
  "port": 3306,
  "database": "order_analytics",
  "sqlite_path": "~/.my-agent/owners/local/main/data/datasources/order_analytics.sqlite",
  "username": "readonly_user",
  "credential_ref": "cred-db-biz-order-01",
  "read_only": false,
  "bound_assets": ["script:daily_order_check", "workflow:order_anomaly_pipeline"],
  "last_test_status": "ok",
  "last_test_at": 1761400000.0
}
```

#### 4.3 核心代码落点
- 新增 `agent_py_agent/agent/assets/datasource_store.py`：管理 `DatasourceSpec` 的增删改查、本机 SQLite 自动初始化、连通性测试（`test_connection`）、表结构探测（`inspect_schema`）与绑定关系反查。
- 新增 `agent_py_agent/agent/tooling/datasources_tool.py`：给主代理提供 `datasources` 工具（支持 `list / get / create_local_sqlite / register / update_connection / test / inspect_schema / migrate`）。
- 增强 [`agent_py_agent/agent/workflows/script_runner.py`](agent_py_agent/agent/workflows/script_runner.py)：在执行脚本前按 `script.data_sources` 解析钥匙串连接并注入运行时上下文，同时对 `stdout/stderr` 实施连接串与密码脱敏。

---

### 能力 5（`CAP-RECORDER`）：浏览器操作录制 ➔ 一键转可传参固定脚本 / 工作流步骤

#### 5.1 需求背景与工作流闭环
1. **三种录制输入来源（既支持浏览器直接录制，也支持导入标准录制文件）**：
   - **来源 A（内置受控浏览器录制会）**：通过宿主启动带录制钩子的 Playwright / CDP 浏览器窗口，用户在页面上点击、输入、筛选、翻页、选中提取区域时，实时捕获标准动作流（`RecordedBrowserStep`）。
   - **来源 B（导入 Chrome DevTools Recorder JSON / Playwright Trace / HAR 关键请求）**：用户在自己常用的 Chrome 里用自带的「录制器（Recorder）」录一段操作导出 JSON，直接拖进门户或传给接口。
   - **来源 C（从 Agent 自己跑过的浏览器自动化轨迹一键固化）**：Agent 用浏览器工具成功跑通一次网页操作后，把刚才的动作轨迹直接固化为脚本。
2. **自动参数化提炼（Parameterization）**：
   - 录制编译器（`BrowserRecordingCompiler`）自动识别录制过程中所有的 `fill / input / select / query_param` 动作（例如用户在搜索框输入了 `"2026-10-25"` 和 `"手机"`），自动将它们提炼为固定脚本的输入参数 `inputs_schema`（如 `{"date": "2026-10-25", "keyword": "手机"}`），并在生成的 Python Playwright / HTTP 固定脚本中替换为 `inputs.get("date")`、`inputs.get("keyword")`。
   - 如果页面需要登录态，自动关联 `credential_ref` 或复用受控浏览器 Profile，不把 Cookie 明文写死在脚本代码里。
3. **产物形态**：
   - 直接生成标准的**一等公民固定脚本（`Script`）**， immediately 拥有 `io_contract`（输入参数表 + 输出 JSON 格式），可以单独点【▶️ 立即试跑】，也可以在【工作流】编辑器里作为步骤直接添加！

#### 5.2 核心代码落点
- 新增 `agent_py_agent/agent/assets/browser_recorder.py`：
  - `start_recording_session` / `stop_recording_session` / `get_recording_steps`
  - `compile_recording_to_script(recording_payload, *, script_id, name, parameterize_inputs=True, credential_ref="") -> DraftScript`
  - `save_compiled_recording_script(owner_home, ...)`

---

### 能力 6（`CAP-TUI-TIMING`）：TUI 每轮任务结束后的“耗时 + 完成时间”消息流尾标行（对齐图 1）

#### 6.1 现状根因分析与用户痛点
- 参考截图：`refs/fig1-claude-code-turn-completion.png`（Claude Code 在每轮回复正文下方固定显示 `✻ Cogitated for 3m 46s · done 8:08 AM`）与 `fig2-my-agent-tui-bg-watch.png`。
- **现有实现的两个缺陷**：
  1. **服务端未下发权威耗时，导致恢复会话/后台唤醒回合漏显示**：现有 [`turn_completion.py`](agent_py_agent/cli/turn_completion.py) 仅在客户端本地内存 `TurnCompletionLedger.accept()` 中记录起点；当遇到 `W1-6` 后台进程完成自动唤醒回合、观察模式回合（`observed=True`）或 `resume` 恢复的历史会话时，没有起点时钟导致不显示或显示“未知”。
  2. **错误覆盖了底部快捷键栏 Footer**：现有 [`tui_block_renderer.py:3209`](agent_py_agent/cli/chat_parts/tui_block_renderer.py#L3209-L3214) 在 `snapshot.status.completion` 存在时，直接把底部 Footer 的 `? 快捷键 · F4 权限 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式` 整行顶替掉了！这既不符合图 1“显示在每轮消息正文末尾、上下文栏上方”的视觉习惯，又会破坏底部 `F6 原生模式` 常驻状态栏。

#### 6.2 目标设计与渲染规范
1. **显示位置（紧贴每轮消息正文末尾、`◉ Context` 状态栏正上方）**：
   - 每一轮任务收口（`completed` / `failed` / `interrupted` / `blocked`）时，在当前回合最后一块 Assistant 消息（或工具块）正下方，固定渲染一行带 `✻` 前缀的次要色（`class:tui-muted`）记录行：
     ```text
     ✻ 耗时 3分46秒 · 完成于 08:08
     ```
     （若耗时小于 60 秒则显示 `✻ 耗时 18.4秒 · 完成于 08:08:12`；跨天则带日期 `YYYY-MM-DD HH:MM`；中断或失败则显示 `✻ 耗时 1分12秒 · 已中断于 08:08` / `✻ 耗时 45.2秒 · 失败于 08:08`）。
2. **Gateway 服务端权威计时 + 历史持久化**：
   - Gateway 在回合真正启动时记录 `turn_started_at`（墙钟）与 `turn_started_monotonic`（单调时钟），在回合终态收口时计算精确的 `elapsed_seconds` 与 `completed_at`，随 `turn_completed` / `turn_failed` / `turn_interrupted` 事件及会话历史块一并持久化。
   - 无论是前台直接提问、后台唤醒续跑，还是退出重进 `resume` 历史会话，每一轮底下都能 100% 稳定看到该轮跑了多久、什么时候结束。
3. **恢复底部 Footer 常驻快捷键显示**：
   - 删除 [`tui_block_renderer.py:3209`](agent_py_agent/cli/chat_parts/tui_block_renderer.py#L3209-L3214) 中用 `snapshot.status.completion` 覆盖底部 Footer 的分支，保证底部 Footer 始终显示 `? 快捷键 · F4 权限 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式`。

---

### 能力 7（`CAP-TUI-BG-WATCH`）：TUI `F6 原生模式` 后置彩色【⚡ 后台驻留 N 项】指示器 + `↑↓` 移动查看 + `Ctrl+G` 退出（解决图 2 痛点）

#### 7.1 现状痛点分析（图 2 场景）
- 参考截图：`refs/fig2-my-agent-tui-bg-watch.png`。
- 自从上线 `W1-6`（免 `sleep` 非阻塞后台等待 + `completion_wake` 自动唤醒）后，主代理启动长耗时后台命令/脚本后，会跟用户交代一句*“后台结束或异常退出，系统都会自动通知我，不用你一直催”*，然后**立即结束当前前台回合**。
- **核心痛点**：前台回合一结束，TUI 画面完全静止，没有任何动画或提示告诉用户“后台其实还有 2 个任务正在跑、系统正在盯着它们”。用户看着静止的界面，根本无法判断**“它是还在后台正常跑，还是已经死掉了？”**

#### 7.2 目标交互与视觉设计（100% 契合现有 TUI 导航习惯）
1. **在底部状态栏文字 `F6 原生模式` 后面，直接追加醒目配色的后台驻留指示器（不写 `shell`，改用直观中文标识）**：
   - 当当前会话（或当前任务树）没有任何活跃后台受管进程（`managed_process_session` / `completion_wake` 待唤醒进程）时，底部保持原样：
     `? 快捷键 · F4 权限 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式`
   - 一旦存在 $N \ge 1$ 个正在后台运行或等待结束/异常唤醒的驻留任务时，直接在 `F6 原生模式` 后面用**独立醒目颜色（如亮青色/琥珀色 `class:tui-bg-watch`）**追加显示：
     ```text
     ? 快捷键 · F4 权限 · 滚轮/PgUp/Ctrl+Home 历史 · 拖选/右键复制 · F6 原生模式 · ⚡ 后台驻留 2 项
     ```
   - 当用户按 `↑ / ↓` 键把焦点移动到该指示器上时，高亮反显并提示可回车查看：
     ```text
     ... · F6 原生模式 · › ⚡ 后台驻留 2 项 (Enter 查看)
     ```
2. **无缝接入现有的 `↑ / ↓` 方向键导航链 + `Enter` 进入 + `Ctrl+G` 退出返回**：
   - 扩展 [`TuiAgentNavigationState`](agent_py_agent/cli/chat_parts/tui_agent_navigation.py#L116-L250) 的 `_selection_ids_locked` 选择序列，把后台驻留入口 `"bg:watch"` 排在导航序列**第一位**：
     **`["bg:watch" (若有活跃后台驻留任务), *goal_ids, *subagent_run_ids]`**。
   - **上下键移动顺序**：
     - 当输入框为空时，用户按 `↑` 或 `↓` 键：**第一步先移动到 `F6 原生模式` 后面的 `⚡ 后台驻留 N 项`**；
     - 再继续按 `↓` 键：**平滑移动到下方的 Goal 行 / 子代理列表行**（与现有子代理选择体验 100% 统一）。
   - **按 `Enter` 查看后台驻留详情页（Background Watch View）**：
     - 焦点停在 `⚡ 后台驻留 N 项` 时按 `Enter`，推入视图栈 `self._path.append("bg:watch")`，进入全屏只读观察页：
       - 顶部横幅显示：`⚡ 后台驻留监控（共 2 项运行中 · 结束或异常时将自动唤醒主代理） · Ctrl+G 返回`
       - 列表展示每一项后台驻留任务的：**任务/命令名称、PID、存活状态（🟢 进程存活运行中 / 🟠 刚退出待唤醒）、已持续运行时间（如 `14分28秒`）、最新实时 `stdout/stderr` 输出尾部内容（实时刷新）**。
       - 若有多项后台任务，在详情页内也可按 `↑ / ↓` 切换查看不同后台任务的完整实时日志流。
   - **按 `Ctrl+G` 退出返回主界面**：
     - 与从子代理详情页退出完全一致，按 `Ctrl+G` 立即弹出 `"bg:watch"` 视图，回到主聊天窗口！

#### 7.3 核心代码落点
- [`agent_py_agent/cli/chat_parts/tui_agent_navigation.py`](agent_py_agent/cli/chat_parts/tui_agent_navigation.py)：新增 `update_background_watch_rows(value)`，将 `"bg:watch"` 纳入 `_selection_ids_locked` 首位，并支持 `enter_selected()` 进入后台驻留详情 Runtime、`leave_current_view()`（`Ctrl+G`）返回。
- [`agent_py_agent/cli/chat_parts/tui_block_renderer.py`](agent_py_agent/cli/chat_parts/tui_block_renderer.py)：在 `_render_footer` 的 `F6 原生模式` 后面拼接彩色 Fragment `("class:tui-bg-watch", f" · ⚡ 后台驻留 {count} 项")`（选中时切换为 `class:tui-agent-selected`），并实现后台驻留详情页渲染。
- [`agent_py_agent/agent/conversation/process_events.py`](agent_py_agent/agent/conversation/process_events.py) 与 Gateway 状态轮询接口：在会话状态快照中附带当前活跃受管后台进程摘要列表（`session_id`、`command_preview`、`pid`、`alive`、`started_at`、`elapsed_seconds`、`wake_on_completion`、`recent_output_tail`）。

---

## 三、全量开放的插件接口清单（Plugin Host API v2 + Plugin Broker API，共 20 个标准 Action）

为了确保我们后续做任何 Web 插件、桌面端插件、多用户工作台插件都能 **100% 调标准宿主接口完成上述所有操作**，在 [`agent_py_agent/agent/plugin_host/action_specs.py`](agent_py_agent/agent/plugin_host/action_specs.py)（`/plugin-host/v2/call` 与 `/plugin-host/v2/stream`）及 [`plugin_broker_api.py`](agent_py_agent/agent/gateway_parts/plugin_broker_api.py) 中统一新增并暴露以下 **20 个标准 Action 接口**：

### 3.1 目录工作区与主会话分叉接口（`conversations.*` & `workspaces.*`）

| Action 名称 | 模式 (`mode`) | 所需权限 (`capabilities`) | 请求参数 Schema（核心字段） | 返回数据结构与用途说明 |
| :--- | :--- | :--- | :--- | :--- |
| **`conversations.threads.create`** *(增强)* | `write` | `chat` | `{title?, workspace_root?: str}` | 新建主会话时支持直接绑定 `workspace_root` 目录路径。 |
| **`conversations.threads.list`** *(增强)* | `read` | `read` | `{status?, workspace_root?: str, limit?, cursor?}` | 支持按 `workspace_root` 过滤某个项目目录下的全部多会话列表。 |
| **`conversations.threads.fork`** *(新增)* | `write` | `chat threads.manage` | `{thread_id: str, expected_revision: int, up_to_message_id?: str, up_to_seq?: int, title?: str, fork_note?: str}` | **主会话一键分叉**：从当前最新轮次或指定历史消息截断点克隆出新主会话，继承目录绑定、模型设置与前缀缓存基线，返回 `{new_thread_id, forked_from_thread_id, forked_message_count, workspace_root}`。 |
| **`workspaces.context.get`** *(新增)* | `read` | `read` | `{workspace_root: str}` | 查询指定目录绑定的活跃会话数、`<dir>/.agent/*` 下的目录级四类资产统计，以及合并个人全局 `~/.my-agent/*` 后的有效资产清单。 |
| **`assets.promote_to_owner`** *(新增)* | `write` | `assets.write` | `{kind: "script"\|"workflow"\|"skill", id: str, workspace_root: str, overwrite?: bool}` | 将某个项目目录 `<dir>/.agent/*` 下的专属脚本/工作流/Skill 一键提升（复制/发布）到用户个人的 `~/.my-agent/*` 全局资产库。 |

### 3.2 对话历史按时间/关键词检索与勾选回合提炼接口（`conversations.turns.*` & `assets.extract.*`）

| Action 名称 | 模式 (`mode`) | 所需权限 (`capabilities`) | 请求参数 Schema（核心字段） | 返回数据结构与用途说明 |
| :--- | :--- | :--- | :--- | :--- |
| **`conversations.turns.search`** *(新增)* | `read` | `chat.history` | `{thread_id?: str, workspace_root?: str, since?: float, until?: float, query?: str, limit?: int, cursor?: str}` | **按时间段（1-3-7-14-30天/自选日期）和关键词检索“用户提问回合（User Turn）”列表**：每一项自带该次提问摘要、底下 AI 执行的工具调用次数、涉及的脚本/Skill、**本轮总耗时 `elapsed_seconds` 与完成时间 `completed_at`** 及回答预览，供前端直接渲染勾选框。 |
| **`conversations.turns.bundle_preview`** *(新增)* | `read` | `chat.history` | `{turn_selections: [{thread_id: str, user_message_id: str}]}` | 传入用户勾选的 $N$ 次提问 ID，返回自动打包好的完整执行轨迹包（每次提问 + 底下所有 `run_command`/`read_file` 等工具调用入参出参 + AI 回复正文）。 |
| **`assets.extract.propose`** *(新增)* | `async` | `chat assets.write` | `{turn_selections: [...], target_kinds: ["workflow","script","skill","monitor"], user_hint?: str, workspace_root?: str}` | **提交 AI 提炼任务**：基于勾选的 $N$ 个对话回合，让 Agent 分析并生成包含 `1 个工作流 + N 个固定脚本 + 1 个 Skill + 可选监控` 的结构化草稿包，返回 `extraction_id` 与全套草稿详情。 |
| **`assets.extract.get`** *(新增)* | `read` | `read` | `{extraction_id: str}` | 查询某次对话提炼草稿包的生成状态与完整代码/步骤预览。 |
| **`assets.extract.commit`** *(新增)* | `write` | `assets.write` | `{extraction_id: str, selected_items: [{draft_id: str, custom_id?: str, custom_name?: str, edits?: object}], target_scope: "owner"\|"workspace", workspace_root?: str}` | **确认导入所选提炼资产**：将用户在界面上勾选（并可能微调过名字/代码）的脚本、Skill、工作流、监控按依赖顺序一键正式写入资产库。 |

### 3.3 统一数据源钥匙串接口（`datasources.*`）

| Action 名称 | 模式 (`mode`) | 所需权限 (`capabilities`) | 请求参数 Schema（核心字段） | 返回数据结构与用途说明 |
| :--- | :--- | :--- | :--- | :--- |
| **`datasources.list`** *(新增)* | `read` | `datasources.read` | `{driver?: str, workspace_root?: str}` | 列出所有已登记的数据源钥匙串（自动隐去密码，展示 `source_id`、驱动类型、地址/本机路径、最近连通状态及绑定了该数据源的固定脚本/工作流列表）。 |
| **`datasources.save`** *(新增)* | `write` | `datasources.write` | `{source_id: str, name: str, driver: "sqlite"\|"mysql"\|"postgresql"\|"http_api", host?: str, port?: int, database?: str, sqlite_path?: str, username?: str, password?: str, credential_ref?: str, create_local_if_missing?: bool}` | **创建或更新数据源（支持本机一键建库与后续改地址迁移）**：若传 `driver="sqlite"` 且 `create_local_if_missing=True`，自动在本机初始化数据库文件；若提供 `password`，自动转存进加密凭据库并生成 `credential_ref`。 |
| **`datasources.test`** *(新增)* | `read` | `datasources.read` | `{source_id: str}` | 测试指定 `source_id` 的数据库连通性、延迟与版本信息，并更新 `last_test_status`。 |
| **`datasources.schema.inspect`** *(新增)* | `read` | `datasources.read` | `{source_id: str, table?: str}` | 读取指定 `source_id` 数据库中的表清单、字段名、类型与样例行数，供前端展示或供 Agent 写固定查库脚本时自动对齐字段。 |
| **`datasources.migrate`** *(新增)* | `async` | `datasources.write execute` | `{source_id: str, target_connection: {...}, copy_schema: bool, copy_data: bool, switch_on_success: bool}` | **一键迁移数据源**：将本机 SQLite / 旧库的表结构与数据迁移到新的目标数据库，校验通过后自动把 `source_id` 的连接指向切换到新库，底下所有固定脚本零改动生效。 |

### 3.4 浏览器操作录制转固定脚本接口（`recorder.*`）

| Action 名称 | 模式 (`mode`) | 所需权限 (`capabilities`) | 请求参数 Schema（核心字段） | 返回数据结构与用途说明 |
| :--- | :--- | :--- | :--- | :--- |
| **`recorder.sessions.start`** *(新增)* | `write` | `recorder.control` | `{start_url: str, title?: str, credential_ref?: str}` | 启动受控浏览器录制会话，返回 `recording_id` 与实时步骤流状态。 |
| **`recorder.sessions.stop`** *(新增)* | `write` | `recorder.control` | `{recording_id: str}` | 结束录制会话，返回捕获到的原始操作步骤列表（点击、填表、跳转、提取文本）及自动识别出的候选变量参数。 |
| **`recorder.compile_to_script`** *(新增)* | `write` | `assets.write` | `{recording_id?: str, imported_steps_json?: object, script_id: str, name: str, parameter_mappings?: object, target_scope?: "owner"\|"workspace"}` | 将录制会话（或外部导入的 Chrome Recorder JSON）编译为带 `io_contract.inputs` 参数表的标准**固定脚本（`Script`）**并保存入库，可直接挂入工作流步骤。 |

### 3.5 后台驻留任务实时观察接口（`processes.background.*`）

| Action 名称 | 模式 (`mode`) | 所需权限 (`capabilities`) | 请求参数 Schema（核心字段） | 返回数据结构与用途说明 |
| :--- | :--- | :--- | :--- | :--- |
| **`processes.background.list`** *(新增)* | `read` | `tasks.read` | `{thread_id?: str, include_finished?: bool}` | 查询当前会话/Owner 正在后台运行并等待结束/异常唤醒的驻留任务列表（返回 `session_id`、命令摘要、PID、存活状态、已运行秒数、唤醒绑定状态）。 |
| **`processes.background.inspect`** *(新增)* | `read` | `tasks.read` | `{session_id: str, tail_lines?: int}` | 读取指定后台驻留任务的实时 `stdout/stderr` 尾部日志流、资源状态与退出码，供 TUI 观察视图与 Web 插件实时展示。 |

---

## 四、给主指挥 Session（`07`）的并行拆单与派工蓝图

主指挥 Session 接手后，可直接按以下 **5 个互不冲突的独立工作包（Work Packages）** 派发给 `c1~c4` 并行开发，并由 `astra (c5)` 与 `Opus 5.5 (/dev/ttys003)` 双路复核：

### 📦 工作包 WP-1（派给 `c4 / sol`）：`CAP-DIR` 目录启动 + 多会话绑定 + 主会话分叉 `CAP-FORK`
- **独占修改文件范围**：
  - `agent_py_agent/cli/parser.py`、`agent_py_agent/cli/workspace_resolution.py`、`agent_py_agent/cli/chat_command_parser.py`
  - `agent_py_agent/agent/conversation/thread_fork_service.py` *(新增)*
  - `agent_py_agent/agent/conversation/store_threads.py`
  - `agent_py_agent/agent/assets/script_store.py`、`agent_py_agent/agent/assets/workflow_store.py`（增加 `workspace_root` `.agent/{scripts,workflows}` 与 Owner 双层合并及 `promote_to_owner`）
- **交付与单测要求**：
  - `tests/test_cli_dir_and_workspace_assets.py`：验证 `my-agent --dir <path>` 启动、同目录多会话过滤、`<dir>/.agent/{skills,scripts,workflows,AGENTS.md}` 与 `~/.my-agent/*` 合并及优先级 `workspace > owner > builtin`。
  - `tests/test_conversation_thread_fork.py`：验证主会话从最新轮次分叉、从指定 `up_to_message_id` 安全回合边界对齐分叉（不切断 `tool_use/tool_result` 对）、继承 `workspace_root` 与模型设置。

### 📦 工作包 WP-2（派给 `c1`）：`CAP-DATASOURCE` 统一数据源钥匙串（本机建库 + 代号注入 + 迁移）
- **独占修改文件范围**：
  - `agent_py_agent/agent/assets/datasource_store.py` *(新增)*
  - `agent_py_agent/agent/tooling/datasources_tool.py` *(新增)*
  - `agent_py_agent/agent/workflows/script_runner.py`（注入 `source_id` 运行时连接与 `runtime_sdk.py` 辅助函数 `get_datasource` / `query_sql`）
- **交付与单测要求**：
  - `tests/test_datasource_keyring_and_script_binding.py`：验证零配置创建本机 SQLite、登记 `source_id`、固定脚本通过 `source_id` 查询数据、修改钥匙串连接地址或执行 `migrate` 后脚本一行代码不改直接命中新库、日志脱敏不泄露密码。

### 📦 工作包 WP-3（派给 `c2`）：`CAP-EXTRACT` 对话勾选回合提炼全家桶 + `CAP-RECORDER` 浏览器录制转固定脚本
- **独占修改文件范围**：
  - `agent_py_agent/agent/assets/turn_extractor.py` *(新增)*
  - `agent_py_agent/agent/assets/browser_recorder.py` *(新增)*
- **交付与单测要求**：
  - `tests/test_turn_asset_extractor.py`：验证按时间段搜索用户提问回合、勾选 $N$ 条用户消息自动打包底下全部 AI 工具调用与回复、生成 `1 Workflow + N Scripts + 1 Skill + Monitor` 草稿包、选择性提交入库并自动建立拓扑引用。
  - `tests/test_browser_recorder_compiler.py`：验证将浏览器录制动作序列（或 Chrome Recorder JSON）自动参数化提炼为带 `io_contract.inputs` 的标准固定脚本。

### 📦 工作包 WP-4（派给 `c3`）：Plugin Host API v2 & Broker 20 个新接口接线 + Web 门户联动
- **独占修改文件范围**：
  - `agent_py_agent/agent/plugin_host/action_specs.py`、`agent_py_agent/agent/plugin_host/conversations.py`、`agent_py_agent/agent/plugin_host/gateway_assets.py`
  - `agent_py_agent/agent/gateway_parts/plugin_broker_api.py`、`agent_py_agent/agent/plugin_host_broker_topics.py`
  - Web 门户插件前后端（`workbench_server.py` / `workbench_ui.py`）：在【🕘 近期对话记录】挂载多选框与【✨ 提炼资产】抽屉、新增【🔑 数据源钥匙串】面板、在【⚙️ 固定脚本】挂载【🎥 录制浏览器操作】与【🔀 会话分叉】按钮。
- **交付与单测要求**：
  - `tests/test_plugin_host_v2_five_capabilities.py`：逐一验证第三节列出的 20 个新增/增强 Host API v2 动作的 Schema 校验、权限硬门、幂等键校验与跨 Owner 隔离。

### 📦 工作包 WP-5（派给首轮最快完成的 Worker 或 `c4 / sol` 第二棒）：TUI 任务结束耗时记录行（`CAP-TUI-TIMING`）+ `F6 原生模式` 后置【⚡ 后台驻留 N 项】导航与观察页（`CAP-TUI-BG-WATCH`）
- **独占修改文件范围**：
  - `agent_py_agent/cli/turn_completion.py`、`agent_py_agent/cli/chat_parts/tui_runtime.py`
  - `agent_py_agent/cli/chat_parts/tui_agent_navigation.py`、`agent_py_agent/cli/chat_parts/tui_block_renderer.py`、`agent_py_agent/cli/chat_parts/tui_ui_setup.py`
  - `agent_py_agent/agent/conversation/process_events.py`（向会话状态快照投影活跃受管后台进程摘要列表）
- **交付与单测要求**：
  - `tests/test_tui_turn_completion_and_bg_watch.py`：
    1. 验证每轮任务结束（含普通前台回合、后台唤醒续跑回合、恢复历史会话）均在消息流尾部、`◉ Context` 上方渲染 `✻ 耗时 ... · 完成于 ...`，且**绝不覆盖**底部 Footer 的 `? 快捷键 · F4 权限 · ... · F6 原生模式`；
    2. 验证存在免 Sleep 后台驻留任务时，在 `F6 原生模式` 后渲染彩色 `· ⚡ 后台驻留 N 项`；
    3. 验证空输入框下按 `↑ / ↓` 键先选中 `⚡ 后台驻留 N 项`、再移动到 Goal / 子代理行，按 `Enter` 打开后台任务实时日志观察页，按 `Ctrl+G` 退回主聊天界面。

### 📦 工作包 WP-6（安全 AI 大脑总控扩展）：多园区/多事业部/多飞书成员安全事件汇聚账本、全链路铁证审计、人员绩效考评（`CAP-SECOPS-LEDGER`）与五维细粒度权限管控（`CAP-ADMIN-RBAC`）
- **核心业务场景**：
  - `1 个 Admin (local/main)` + `N 个飞书接入成员 (providers/feishu/users/<id>)`，底座连接多个园区的安全设备（防火墙、EDR、WAF）与日志源（态势感知、SLS、NDR、SIEM）。
  - 定时监控或安全设备触发告警后推送飞书，分析员直接在飞书与专属 Agent 对话排查处置。
- **结构化归并与汇总机制**：
  1. **数据源自动继承标签**：每个安全设备/日志源在钥匙串（`datasources.json`）中绑定 `campus_id`（所属园区）与 `bu_id`（所属事业部）。
  2. **三档统计视角**：支持 `scope="global"`（Admin + 全部飞书成员联邦汇总）、`scope="admin"`（仅统计 Admin 自身）、`scope="feishu/<uid>"`（单成员个人视角，普通成员登录仅允许查看自身）。
  3. **多角度聚合统计**：
     - **按园区（Campus）与事业部（Business Unit）交叉汇总**事件总数、P0/P1 高危数、闭环率与平均 MTTR；
     - **按安全设备/日志源防线归并**：自动合并同一数据源（如态势感知）上 `Admin 配置的规则数 + 各飞书成员配置的规则数` 及各自触发的事件数；
     - **跨分析员同源威胁实体（IOC）自动碰撞**：不同成员或不同园区的事件命中同一可疑 IP/域名/哈希时，自动聚合成跨分析员关联事件簇。
  4. **人员与 Agent 作战单元绩效考评（Performance & SLA Scorecard）**：
     - 按时间窗口量化考核每位飞书成员的：① 应急闭环事件数与高危攻坚数、② 接警 MTTA 与闭环 MTTR（秒）及 SLA 达标率、③ 自建四大件（监控/Workflow/固定脚本/Skill）沉淀贡献度、④ 审计合规率（零越权记录）。
  5. **全链路铁证审计（Immutable Audit Trail）与五维 RBAC 权限矩阵**：
     - 每一笔飞书指令、日志源查询、设备封禁动作与越权拦截均记入审计账本（含 SHA-256 证据摘要）；
     - 管理员可通过 `users.local.permissions.{get,set,effective}` 控制普通用户的 5 层权限：① 插件准入、② 页面功能动作锁（如禁用会话分叉 `conversations.threads.fork`、禁用修改代码 `assets.write`、禁用提炼资产 `assets.extract.*`）、③ 管辖园区与 `source_id` 数据源白名单、④ 底层工具（`run_command` / `write_file`）与 Jev 决策模型开关、⑤ 每日 Token 与并发配额，同步落盘至对应用户的 `tool_policy.json`。
