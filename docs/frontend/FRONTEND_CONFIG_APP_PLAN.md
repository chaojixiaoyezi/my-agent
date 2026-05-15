# my-agent 前端管理界面规划方案

**文档版本**: v0.1.0
**创建日期**: 2026-05-12
**目标读者**: 后续负责实现前端的 AI 开发者 / 人类开发者
**约束前提**: 本方案只定义设计，不实现代码；不修改现有 Python CLI 代码；不修改 `agent_config.yaml`；不安装依赖；不改动 CI。

---

## 1. 前端整体定位

本前端是 **my-agent 的本地管理界面（Admin Dashboard）**，不是面向公众的官网或营销页。

它服务于以下核心场景：

| 场景 | 说明 |
|------|------|
| **配置项查看和修改** | 可视化编辑 `agent_config.yaml`、`capability_config.yaml`、`log_analysis_config.yaml`，避免手工改 YAML 出错 |
| **子代理运行状态查看** | 层级树展示 root / child / grandchild / deeper 的实时状态、阻塞项、产出引用 |
| **Memory / Compact / Resume 状态查看** | 按日期、任务、run 分片查询，不一次性加载全量 |
| **工具权限与预算** | 查看工具白名单、调用预算、controlled exec 状态、写边界策略 |
| **日志与调试** | 查看最近日志、debug trace 等级、E2E 测试记录、audit 事件 |
| **测试与诊断** | 触发 local-doctor 检查、查看 gateway 健康状态、运行 smoke test |
| **模板管理（预留）** | 查看 role templates、workflow templates，后续扩展 skill templates |

**使用方式**：
- 本地运行：用户在终端 `my-agent frontend start` 后，浏览器打开 `http://localhost:8420/admin`
- 前端是 **只读/配置管理** 界面，不是聊天界面（聊天仍走 CLI / gateway / 外部适配器）
- 所有写入操作必须通过后端 API，前端不直接读写本地文件

---

## 2. 技术建议

### 2.1 推荐技术栈

| 层级 | 推荐方案 | 理由 |
|------|----------|------|
| 语言 | **TypeScript** | 配置 schema 驱动 UI，强类型是必须的；减少运行时配置类型错误 |
| 框架 | **React 18+** | 组件化程度高，生态成熟，表单库（React Hook Form）和状态管理（Zustand）选择丰富 |
| 构建工具 | **Vite** | 启动快、HMR 好、配置简单、不绑定特定框架版本 |
| UI 组件库 | **shadcn/ui** 或 **Radix UI + Tailwind CSS** | 无运行时依赖负担，样式可完全定制，适合 admin 后台 |
| 状态管理 | **Zustand** | 轻量、无样板代码、支持 TypeScript |
| 表单处理 | **React Hook Form + Zod** | 声明式校验、与 schema 驱动天然契合 |
| 数据获取 | **TanStack Query (React Query)** | 缓存、重试、乐观更新、后台刷新 |
| 路由 | **React Router v6** | 声明式路由，支持嵌套路由和懒加载 |
| 图标 | **Lucide React** | 轻量、风格统一、Tree-shaking 友好 |

### 2.2 为什么不选 Vue

Vue 3 + Vite 也是很好的选择，但推荐 React 的原因是：
1. **表单复杂度**：配置项管理涉及大量动态表单、条件字段、嵌套对象，React Hook Form + Zod 的组合在此场景下比 Vue 的表单方案更成熟
2. **TypeScript 深度集成**：React 的 JSX 与 TypeScript 类型推断配合更紧密，对于 schema 驱动的 UI 开发体验更好
3. **生态成熟度**：admin dashboard 相关的 React 生态（TanStack Table、Recharts、React Query）比 Vue 更丰富

如果后续团队对 Vue 更熟悉，Vue 3 + Vite + Pinia + VeeValidate 也是可接受的替代方案。

### 2.3 目录结构（规划，本次不创建）

```text
frontend/                          # 前端源码目录（本次不创建）
|-- src/
|   |-- api/                       # API client 和类型定义
|   |-- components/                # 通用组件
|   |   |-- ui/                    # shadcn/ui 基础组件
|   |   |-- config/                # 配置项专用组件
|   |   |-- layout/                # 布局组件（Sidebar、Header、Page）
|   |-- hooks/                     # 自定义 React Hooks
|   |-- lib/                       # 工具函数、常量、schema 定义
|   |-- pages/                     # 页面级组件
|   |   |-- Dashboard/
|   |   |-- Config/
|   |   |-- Subagents/
|   |   |-- Memory/
|   |   |-- Tools/
|   |   |-- Logs/
|   |   |-- Templates/
|   |-- stores/                    # Zustand stores
|   |-- types/                     # 全局 TypeScript 类型
|   |-- App.tsx
|   |-- main.tsx
|-- index.html
|-- vite.config.ts
|-- tsconfig.json
|-- package.json
|-- tailwind.config.js

docs/frontend/                     # 前端设计文档（本次创建）
|-- FRONTEND_CONFIG_APP_PLAN.md    # 本文档
|-- API_SPEC.md                    # 后续补充：完整 API 规范
|-- UI_DESIGN.md                   # 后续补充：UI 设计稿/交互说明
```

---

## 3. 目录隔离要求

**绝对禁止修改的目录和文件**：

```text
agent_py_agent/                    # 现有 Python 源码目录
agent_py_agent/config/
  |-- agent_config.yaml            # 主配置文件
  |-- capability_config.yaml       # 能力路由配置
  |-- log_analysis_config.yaml     # 日志分析配置
scripts/                           # 现有 Python 脚本
.github/                           # CI/CD 工作流
pyproject.toml                     # Python 打包配置（前端依赖不加入）
```

**允许新增/修改的目录**：

```text
docs/frontend/                     # 前端设计文档
frontend/                          # 未来前端源码（需主线程批准后才创建）
```

**隔离原则**：
- 前端代码与 Python 代码完全分离，不共享任何源码目录
- 前端不直接读取或写入 `agent_config.yaml`，所有配置操作必须通过后端 API
- 前端构建产物（`dist/`、`node_modules/`）必须加入 `.gitignore`
- 不增加 Python 项目的依赖（不在 `pyproject.toml` 中加入前端依赖）
- 未来如果需要把前端打包进 Python 包，由主线程决定，使用 `MANIFEST.in` 或独立 npm workspace 方案

---

## 4. 配置页面设计（Schema 驱动）

### 4.1 核心设计理念

配置页必须是 **schema 驱动**，不是一个字段一个组件硬编码。原因：
- `agent_config.yaml` 已有 100+ 字段，且持续增加
- 硬编码组件维护成本极高，新增配置需要改前端代码
- schema 驱动可以实现自动生成表单、自动校验、自动 diff、自动导出

### 4.2 配置字段 Schema 定义

```typescript
// types/config.ts

type ConfigFieldType =
  | "string"
  | "number"
  | "boolean"
  | "choice"
  | "string_list"
  | "path"
  | "secret"
  | "json"
  | "duration_seconds"
  | "text";          // 长文本，如 system_prompt

type RiskLevel = "low" | "medium" | "high" | "critical";

type ConfigField = {
  // 标识
  key: string;                    // YAML 中的键名，如 "tool_write_inline_max_chars"
  label: string;                  // 中文展示名，如 "写文件单次内联字符上限"
  description: string;            // 中文详细说明，可包含风险提示
  shortDescription?: string;      // 列表/卡片视图中的简短说明

  // 类型与约束
  type: ConfigFieldType;
  defaultValue: unknown;
  currentValue?: unknown;         // 当前生效值（由后端提供）
  min?: number;                   // 数值最小值
  max?: number;                   // 数值最大值
  minLength?: number;             // 字符串最小长度
  maxLength?: number;             // 字符串最大长度
  choices?: string[];             // choice 类型的候选值
  choiceLabels?: Record<string, string>; // choice 选项的中文标签
  pattern?: string;               // 正则校验（字符串类型）
  patternMessage?: string;        // 正则不匹配时的提示
  placeholder?: string;           // 输入框占位提示

  // 分类与展示
  category: string;               // 分类 key，如 "tools", "memory", "gateway"
  categoryLabel?: string;         // 分类中文名
  subCategory?: string;           // 子分类，如 "write_boundary"
  advanced?: boolean;             // 是否为高级配置，默认折叠
  order?: number;                 // 同一分类内的排序权重

  // 行为与风险
  restartRequired?: boolean;      // 修改后是否需要重启 gateway/daemon
  riskLevel?: RiskLevel;          // 风险等级
  riskWarning?: string;           // 高风险时的具体警告文案
  confirmationRequired?: boolean; // 保存前是否需要二次确认

  // 依赖与条件展示
  dependsOn?: {                   // 条件展示：当某字段为某值时才展示
    field: string;
    value: unknown;
  };
  mutuallyExclusiveWith?: string[]; // 互斥字段

  // 环境变量相关
  envVar?: string;                // 对应的环境变量名，如 "AGENT_API_KEY"
  envOverride?: boolean;          // 环境变量是否优先于配置文件

  // 单位与格式
  unit?: string;                  // 单位，如 "秒", "字符", "条"
  step?: number;                  // 数字输入的步进值

  // 文档
  docLink?: string;               // 指向 docs/ 中相关文档的链接
  versionAdded?: string;          // 从哪个版本开始支持
};

type ConfigCategory = {
  key: string;
  label: string;
  description: string;
  icon?: string;                  // Lucide icon name
  order: number;
  fields: ConfigField[];
};

type ConfigSchema = {
  version: string;                // schema 版本，如 "0.3.0"
  categories: ConfigCategory[];
  meta: {
    lastUpdated: string;
    source: string;               // 配置来源，如 "agent_config.yaml"
    editable: boolean;            // 是否允许前端编辑
    exportFormats: ("yaml" | "json")[];
  };
};
```

### 4.3 配置分类设计

基于 `agent_config.yaml` + `capability_config.yaml` + `log_analysis_config.yaml` 的实际字段，配置页分为以下大类：

| 分类 Key | 中文名 | 包含字段示例 |
|----------|--------|-------------|
| `basic` | 基础信息 | `agent_name`, `system_prompt`, `workspace_root`, `auto_detect_work_on_startup` |
| `model` | 模型配置 | `model_backend`, `model_name`, `api_base`, `max_tokens`, `temperature`, `stream_enabled` |
| `auth` | 认证与 API Key | `api_key_env`, `auth_enabled`, `admin_user_id`, `user_id` |
| `tools` | 工具配置 | `enable_tools`, `max_tool_rounds`, `tool_read_max_chars`, `tool_write_inline_max_chars` |
| `memory` | 记忆配置 | `memory_path`, `memory_top_k`, `auto_save_memory`, `memory_archive_level` |
| `compact_resume` | 压缩与恢复 | `memory_compact_auto_allow_apply`, `memory_resume_auto_context_enabled`, `memory_resume_auto_context_mode` |
| `local_store` | 本地事实源 | `local_store_path`, `local_store_fts_enabled` |
| `subagent` | 子代理 | `enable_subagents`, `subagent_mode`, `max_subagents`, `subagent_workspace`, `subagent_debug_trace_level` |
| `role_template` | 角色模板 | `subagent_role_template_dirs` |
| `workflow` | 工作流（预留） | `subagent_workflow_mode`, `subagent_builtin_workflows`, `subagent_workflow_review_rounds` |
| `capability` | 能力路由 | `enable_capability_routing`, `capability_grant_expires_after_task` |
| `gateway` | Gateway | `gateway_workspace`, `gateway_port`, `gateway_heartbeat_interval`, `gateway_request_workers` |
| `daemon` | 前台 Daemon | `daemon_planner`, `daemon_apply`, `daemon_execute_runners`, `daemon_interval` |
| `scheduler` | 调度策略（高级兼容） | `scheduler_mode`, `runner_failure_policy` |
| `acceptance` | 验收策略 | `acceptance_execute_tests`, `acceptance_test_timeout_seconds` |
| `notification` | 通知系统 | `notification_enabled`, `notification_store_path` |
| `audit` | 审计日志 | `audit_enabled`, `audit_log_path` |
| `concurrency` | 并发控制 | `concurrency_lock_enabled`, `task_lock_timeout_seconds` |
| `log_analysis` | 日志分析 | `enabled`, `data_dir`, `response_mode`, `query_default_limit` |
| `extensions` | 扩展（预留） | `extensions_dir` |

### 4.4 重点配置项：`tool_write_inline_max_chars`

```typescript
const toolWriteInlineMaxChars: ConfigField = {
  key: "tool_write_inline_max_chars",
  label: "写文件内联推荐字符数",
  description:
    "控制 write_file / append_file 单次 inline content 的推荐字符数。" +
    "超过该值的合法工具调用仍会写入文件，但会提示模型后续改用分块，避免反复输出超长 JSON。" +
    "建议根据模型上下文窗口和工具调用稳定性调整。",
  shortDescription: "write_file / append_file 单次 inline content 推荐值",
  type: "number",
  defaultValue: 12000,
  min: 100,
  max: 100000,
  step: 1000,
  unit: "字符",
  category: "tools",
  subCategory: "write_boundary",
  advanced: false,
  restartRequired: false,
  riskLevel: "medium",
  riskWarning:
    "值过大（> 50000）时，模型可能在工具 JSON 输出中被截断，导致 write_file 失败或内容不完整。" +
    "值过小（< 1000）时，大文件写入会产生大量分块，降低效率。",
  confirmationRequired: false,
  docLink: "/docs/frontend/#tool-write-inline-max-chars",
  order: 10,
};
```

**UI 表现建议**：
- 输入框旁显示单位"字符"
- 滑块范围 100 ~ 100000，步进 1000
- 输入值时实时显示与默认值的偏差（如 "+3000" 或 "-2000"）
- 值 > 50000 时显示黄色警告 icon 和风险提示 tooltip
- 值 < 1000 时显示黄色警告 icon 和"分块过多"提示

---

## 5. UI 交互设计

### 5.1 整体布局

```
+----------------------------------------------------------+
|  Logo    my-agent 管理后台          [搜索...]   [用户]   |
+----------+-----------------------------------------------+
|          |                                               |
|  Config  |  配置页内容区                                  |
|  Subagent|                                               |
|  Memory  |  [面包屑] 配置 / 工具配置 / 写边界              |
|  Tools   |                                               |
|  Logs    |  [基础] [高级] 切换                            |
|  Templates|                                              |
|  Dashboard|                                              |
|          |  [左侧分类导航]      [右侧配置表单]              |
|          |                                               |
|          |  +------------------+  +--------------------+ |
|          |  | 基础信息          |  | 模型后端           | |
|          |  | 模型配置          |  | API 地址           | |
|          |  | 工具配置    <--   |  | 模型名称           | |
|          |  | 子代理配置        |  | ...                | |
|          |  | ...               |  +--------------------+ |
|          |  +------------------+                       |
|          |                                               |
+----------+-----------------------------------------------+
|  [保存] [导出 YAML] [重置修改] [恢复默认]    未保存修改 * |
+----------------------------------------------------------+
```

### 5.2 左侧分类导航

- 树形结构，一级分类 + 可选的二级分类
- 点击分类后，右侧表单自动滚动到对应区域（或仅显示该分类字段）
- 分类旁显示该分类下有多少字段被修改（红色 badge）
- 支持折叠/展开分类

### 5.3 顶部搜索

- 全局搜索框，支持按 `key`、`label`、`description` 搜索
- 搜索结果高亮匹配字段，点击后跳转到对应分类
- 支持快捷键 `Cmd/Ctrl + K` 聚焦搜索

### 5.4 基础/高级切换

- 全局切换开关（顶部工具栏）
- `基础` 模式：只展示 `advanced !== true` 的字段
- `高级` 模式：展示所有字段，高级字段以淡灰色背景或特殊标识区分
- 用户偏好保存在 `localStorage`

### 5.5 修改后 Diff 预览

- 每个被修改的字段旁显示"已修改"标签
- 底部悬浮栏显示修改统计（"已修改 3 项"）
- 点击"查看 Diff"弹出 side panel，显示：
  ```
  tool_write_inline_max_chars
    默认值: 12000
    当前值: 12000
    新值:   25000  ←

  memory_top_k
    默认值: 5
    当前值: 5
    新值:   10  ←
  ```

### 5.6 保存前校验

- 前端实时校验（Zod schema）：类型、范围、正则、互斥
- 点击"保存"时，先调用 `POST /api/config/validate`
- 后端二次校验（因为前端 schema 可能滞后）
- 校验失败时，错误信息定位到具体字段，滚动到错误位置

### 5.7 导出 YAML

- "导出 YAML"按钮生成当前配置（含修改）的 YAML 预览
- 弹窗展示 YAML 内容，支持复制到剪贴板
- 不直接下载文件（安全考虑），由用户决定是否保存

### 5.8 重置单项

- 每个字段旁有"重置"按钮（hover 时显示）
- 点击后恢复该字段的当前生效值（不是默认值，是当前从后端读取的值）
- 如果当前值就是默认值，按钮 disabled

### 5.9 恢复默认

- 底部工具栏有"恢复默认"按钮
- 点击后弹出确认对话框："确定将所有配置恢复为默认值吗？此操作不可撤销。"
- 仅重置前端表单状态，不直接写入后端，仍需点"保存"才生效

### 5.10 风险配置二次确认

- `riskLevel === "high" || riskLevel === "critical"` 的字段：
  - 输入框边框变为橙色/红色
  - 修改后保存时，弹出确认对话框，显示 `riskWarning` 文案
  - 用户必须勾选"我已了解风险"才能继续保存

### 5.11 未保存修改提示

- 页面离开前（`beforeunload`）提示："您有未保存的修改，确定要离开吗？"
- 切换左侧导航时，如果当前页有未保存修改，提示确认
- 未保存状态以红色圆点标记在浏览器标签标题前

### 5.12 配置说明 Tooltip

- 每个字段标签旁有 `?` icon
- hover 时显示 `description` 全文
- 支持点击固定 tooltip，方便阅读长说明
- 高级字段额外显示 `docLink`，可点击跳转到文档

### 5.13 中文友好

- 所有 label、description、placeholder、错误提示均为中文
- 技术名词保留英文并在首次出现时括号标注，如：
  - "模型后端（model backend）"
  - "工具预算（tool budget）"
  - "心跳间隔（heartbeat interval）"
- choice 选项使用中文标签（`choiceLabels`）

### 5.14 页面风格与视觉设计

#### 整体视觉风格：柔和

- **配色方案**：采用低饱和度、暖灰色调，灵感接近莫兰迪色系，整体观感安静、不刺眼
- **背景色**：不使用 `#FFFFFF` 纯白，主背景使用极浅暖灰，如 `#FAFAF8` 或 `#F5F4F0`；卡片/浮层面板使用 `#FFFFFF` 但带极淡的暖色底
- **文字色**：正文不使用纯黑，使用深暖灰 `#2D2D2D` 或 `#333333`；次要文字用中灰 `#6B6B6B`；占位符用浅灰 `#9CA3AF`
- **重点色**：柔和蓝 `#60A5FA`、柔和绿 `#34D399`、柔和橙 `#FB923C`、柔和红 `#F87171`；避免荧光色和高饱和色
- **边框**：使用半透明浅灰 `rgba(0,0,0,0.06)` 或 `rgba(0,0,0,0.08)`，不用硬边实线；hover 时边框透明度略微升高
- **圆角**：大圆角设计，营造柔和感
  - 卡片/面板：`border-radius: 12px` ~ `16px`
  - 按钮：`border-radius: 8px` ~ `12px`
  - 输入框/选择器：`border-radius: 8px`
  - 小标签/tag：`border-radius: 9999px`（全圆角）
- **阴影**：柔和的弥散阴影，模拟自然光感
  - 默认：`0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.02)`
  - hover 时轻微加深：`0 2px 6px rgba(0,0,0,0.05), 0 8px 20px rgba(0,0,0,0.04)`
  - 不使用锐利、浓重的黑色阴影
- **留白**：充足的内边距和外边距，行高 `1.6` ~ `1.8`，段落间距 `16px` ~ `24px`，元素之间不拥挤
- **字体**：使用系统默认无衬线字体栈（`-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif`），不引入外部字体文件；字号层次清晰（标题 20-24px、正文 14-16px、辅助 12-13px）
- **分隔线**：用浅色背景区块或留白代替实线分隔线；必要时使用 `1px solid rgba(0,0,0,0.06)` 的极淡线

#### 按钮灵动：拒绝生硬

- **Hover 状态**：所有可点击按钮必须有 hover 反馈
  - 主按钮：颜色轻微加深（如蓝色从 `#60A5FA` 到 `#3B82F6`），同时微微上浮 `transform: translateY(-1px)`，阴影轻微扩大
  - 次按钮/幽灵按钮：背景色从透明渐变填充为极淡的主题色（如 `rgba(96,165,250,0.08)`）
  - 危险按钮：hover 时颜色从柔和红渐变为更明显的红，不直接跳变
- **点击状态（Active）**：按下时瞬间轻微下沉 `transform: translateY(0) scale(0.97)`，有弹性的按压感；松开后 150ms 内回弹到 hover 状态
- **过渡动画**：所有状态切换使用平滑的 cubic-bezier 缓动
  - 推荐：`cubic-bezier(0.34, 1.56, 0.64, 1)`（带轻微回弹的弹性效果）
  - 保守场景：`cubic-bezier(0.4, 0, 0.2, 1)`（Material Design 标准缓动）
  - 时长：`150ms` ~ `200ms`，不长不短
- **Loading 状态**：按钮内显示旋转动画（spinner）或脉冲效果，不是简单的文字变灰或按钮 disabled；按钮保持原有尺寸，不闪动
- **危险操作按钮**：hover 时除颜色变化外，可增加极轻微的左右晃动（`translateX(-1px)` → `translateX(1px)` → `0`，持续 200ms），提示用户"这是危险操作"
- **主次区分**：主按钮（保存、确认）使用实心填充；次按钮（取消、导出）使用幽灵样式（边框+文字）；文字按钮（重置单项）使用纯文字+hover 下划线；三者视觉上区分明显但不突兀
- **禁用状态**：不是简单的灰色，而是透明度降低到 `0.5` 并移除阴影，cursor 变为 `not-allowed`，hover 时不产生任何动画

#### 不要声音

- **明确禁止任何音频反馈**：包括操作成功提示音、错误蜂鸣、按钮点击音、通知提示音等
- **所有反馈必须纯视觉化**：用动画、颜色变化、toast、图标变化替代声音
- **通知系统默认静音**：即使未来接入浏览器通知 API，也默认关闭声音选项，仅允许视觉通知
- **无声≠无反馈**：恰恰相反，由于没有声音的辅助，视觉反馈必须做得更充分

#### 回馈好：每一次操作都有回应

- **即时响应（Immediate Response）**：
  - 按钮点击后 `100ms` 内必须有视觉变化（颜色、阴影、缩放），绝不让用户产生"我是不是没点中？"的疑虑
  - 输入框聚焦时边框颜色变化 + 柔和的阴影扩散（`box-shadow: 0 0 0 3px rgba(96,165,250,0.15)`）
- **Toast 轻量通知**：
  - 保存成功：顶部滑入绿色 toast，带柔和的对勾 icon，3 秒后自动淡出
  - 保存失败：顶部滑入红色 toast，带感叹号 icon，手动关闭或 5 秒后自动消失
  - 导出完成：底部居中 toast，带下载 icon，提供"复制"快捷操作
  - toast 入场：`transform: translateY(-20px) + opacity: 0 → 正常`，时长 250ms，缓动带轻微回弹
- **微动画反馈**：
  - 保存成功：对勾 icon 从小放大到正常（`scale(0.5) → scale(1.1) → scale(1)`，300ms）
  - 配置项被修改：字段标签旁出现小圆点指示器（`width: 0 → 8px`，带弹性缓动）
  - 列表加载新内容：新条目淡入（`opacity: 0 → 1`，逐个延迟 30ms 级联出现）
  - 删除/移除：条目向左滑出 + 高度收缩（`translateX(0) → translateX(-100%) + height → 0`，200ms）
- **进度指示**：
  - 长时间操作（如导出大型 YAML、加载大量子代理数据）显示进度条或不确定进度动画
  - 进度条使用主题色渐变填充，带有微弱的光晕移动效果（shimmer），让用户感知"系统在干活"
  - 不确定进度使用柔和的脉冲动画，不是生硬的旋转器
- **错误反馈**：
  - 表单校验错误：错误字段边框变红 + 轻微水平抖动（`translateX(-3px → 3px → -2px → 2px → 0)`，200ms），像摇头一样提示"不对"
  - 全局错误：红色 toast + 错误代码，可点击展开详情
- **空状态（Empty State）**：
  - 没有子代理时：展示一个柔和的插画占位（简单的几何图形组合即可）+ "还没有运行中的子代理" + "去创建第一个子代理" 按钮
  - 没有日志时："这里空空如也" + 一个淡淡的时钟 icon
  - 搜索无结果："没有找到匹配的配置项" + "清除搜索条件" 按钮
  - 绝不展示空白页面或冷冰冰的"No Data"
- **加载占位（Skeleton）**：
  - 数据加载中使用骨架屏（skeleton）而不是全屏 spinner
  - skeleton 使用柔和的 shimmer 动画（背景色从左到右的光晕移动），不是闪烁的灰色块
  - 加载完成后骨架屏淡出、真实内容淡入，无缝切换
- **Hover 反馈**：
  - 表格行 hover：背景色变为极淡的暖灰（`rgba(0,0,0,0.02)`），不是生硬的蓝色或灰色
  - 可点击卡片 hover：阴影加深 + 轻微上浮 `translateY(-2px)`
  - 链接 hover：下划线从左侧滑入（`width: 0 → 100%`，200ms）
- **数值变化反馈**：
  - 数字变化时（如 pending requests 从 2 变到 3）：数字快速放大再回缩（`scale(1.2) → scale(1)`，200ms），像心跳一样
  - status 状态变化：颜色渐变过渡（如从灰色变成绿色，过渡 300ms），不是瞬间跳变

#### 动画性能与可访问性

- **GPU 加速**：所有动画只使用 `transform` 和 `opacity`，不动 `width`、`height`、`top`、`left`，确保 60fps 流畅
- **动画时长规范**：
  - 微交互（按钮点击、hover）：`150ms` ~ `200ms`
  - 页面过渡：`250ms` ~ `300ms`
  - Toast 出现：`250ms`，消失：`200ms`
  - 骨架 shimmer：`1200ms` 循环
- **减少动画偏好**：支持 `prefers-reduced-motion` 媒体查询，用户开启系统级"减少动画"时：
  - 禁用所有弹性/回弹动画
  - 过渡时长缩短到 `50ms`
  - 禁用 shimmer、脉冲等装饰性动画
  - 保留必要的透明度变化（让用户知道状态变了）

#### 暗色模式（预留）

- 配色方案预留暗色模式切换能力，后续通过 toggle 切换
- 暗色模式同样遵循"柔和"原则：
  - 背景不用纯黑 `#000000`，使用深灰蓝 `#1A1A2E` 或 `#1E1E2E`
  - 卡片用 `#252538`，边框用 `rgba(255,255,255,0.06)`
  - 文字不用纯白，主文字 `#E4E4E7`，次要文字 `#A1A1AA`
  - 重点色保持柔和但提高饱和度，确保暗色下可读
- 切换时整体颜色渐变过渡 `300ms`，不是瞬间跳变

---

## 6. API 边界设计

**重要前提**：目前只设计 API，不实现后端。前端在 Phase 1~3 使用 mock 数据。

### 6.1 配置相关 API

#### `GET /api/config/schema`

返回配置 schema，用于驱动前端表单自动生成。

**响应**：
```json
{
  "version": "0.3.0",
  "categories": [
    {
      "key": "tools",
      "label": "工具配置",
      "description": "控制工具调用、预算和边界",
      "icon": "Wrench",
      "order": 3,
      "fields": [
        {
          "key": "tool_write_inline_max_chars",
          "label": "写文件单次内联字符上限",
          "description": "控制 write_file / append_file 单次 inline content...",
          "type": "number",
          "defaultValue": 12000,
          "min": 100,
          "max": 100000,
          "unit": "字符",
          "category": "tools",
          "advanced": false,
          "restartRequired": false,
          "riskLevel": "medium",
          "riskWarning": "值过大可能导致模型工具 JSON 被截断...",
          "order": 10
        }
      ]
    }
  ],
  "meta": {
    "lastUpdated": "2026-05-12T00:00:00Z",
    "source": "agent_config.yaml",
    "editable": true,
    "exportFormats": ["yaml", "json"]
  }
}
```

#### `GET /api/config/current`

返回当前生效配置值（含环境变量覆盖后的实际值）。

**响应**：
```json
{
  "values": {
    "tool_write_inline_max_chars": 12000,
    "memory_top_k": 5,
    "model_backend": "anthropic_compatible"
  },
  "overrides": {
    "api_key": "env:AGENT_API_KEY",
    "model_name": "env:OVERRIDE_MODEL_NAME"
  },
  "readOnlyKeys": ["api_key"],
  "lastModified": "2026-05-10T08:30:00Z"
}
```

- `overrides`：被环境变量覆盖的字段
- `readOnlyKeys`：当前只读字段（如环境变量优先级更高时）
- `api_key` 等 secret 字段不回显真实值，只显示 `"***"` 或 `"env:AGENT_API_KEY"`

#### `POST /api/config/validate`

校验配置值是否合法。

**请求**：
```json
{
  "values": {
    "tool_write_inline_max_chars": 250000,
    "memory_top_k": 10
  }
}
```

**响应**：
```json
{
  "valid": false,
  "errors": [
    {
      "key": "tool_write_inline_max_chars",
      "message": "值 250000 超过最大值 100000",
      "severity": "error"
    }
  ],
  "warnings": [
    {
      "key": "tool_write_inline_max_chars",
      "message": "值 25000 超过推荐上限 50000，可能导致工具 JSON 截断",
      "severity": "warning"
    }
  ]
}
```

#### `POST /api/config/save`

保存配置修改。

**请求**：
```json
{
  "values": {
    "tool_write_inline_max_chars": 25000,
    "memory_top_k": 10
  },
  "confirmations": {
    "tool_write_inline_max_chars": true
  },
  "comment": "增大写文件上限以减少分块"
}
```

**响应**：
```json
{
  "success": true,
  "savedKeys": ["tool_write_inline_max_chars", "memory_top_k"],
  "restartRequired": false,
  "backupPath": "data/config/backups/agent_config_20260512_143022.yaml",
  "message": "配置已保存，2 项修改已生效"
}
```

- 后端必须先写备份，再写主配置
- 返回 `restartRequired` 提示用户是否需要重启 gateway/daemon
- `comment` 用于审计日志

#### `POST /api/config/export-yaml`

导出 YAML 预览（不写入磁盘）。

**请求**：
```json
{
  "values": {
    "tool_write_inline_max_chars": 25000
  }
}
```

**响应**：
```json
{
  "yaml": "# Simple Python Agent 配置文件\n...",
  "format": "yaml",
  "byteSize": 4200
}
```

### 6.2 子代理相关 API

#### `GET /api/subagents/runs`

获取子代理运行列表。

**查询参数**：
- `parent_id`：父级 run_id（可选，不传则返回 root）
- `status`：状态过滤（可选）
- `limit`：数量限制（默认 20，最大 100）
- `offset`：分页偏移

**响应**：
```json
{
  "runs": [
    {
      "run_id": "run_20260512_001",
      "agent_name": "worker-001",
      "role": "worker",
      "status": "RUNNING",
      "current_step": "执行工具调用: write_file",
      "blockers": [],
      "output_refs": ["data/subagents/run_001/output.json"],
      "artifact_refs": ["data/subagents/run_001/artifacts/patch.diff"],
      "debug_trace_refs": ["data/subagents/run_001/debug_trace.jsonl"],
      "qa_status": "PENDING",
      "created_at": "2026-05-12T10:00:00Z",
      "updated_at": "2026-05-12T10:15:00Z",
      "child_count": 3,
      "depth": 0
    }
  ],
  "total": 42,
  "has_more": true
}
```

#### `GET /api/subagents/hierarchy`

获取子代理层级树。

**查询参数**：
- `root_run_id`：根 run_id
- `max_depth`：最大展开深度；省略或 `0` 表示不限制，由前端分页/折叠控制展示量

**响应**：
```json
{
  "root": {
    "run_id": "run_root_001",
    "agent_name": "coordinator",
    "role": "coordinator",
    "status": "RUNNING",
    "children": [
      {
        "run_id": "run_child_001",
        "agent_name": "worker-001",
        "role": "worker",
        "status": "AWAITING_ACCEPTANCE",
        "children": [
          {
            "run_id": "run_grandchild_001",
            "agent_name": "tester-001",
            "role": "tester",
            "status": "DONE",
            "children": []
          }
        ]
      }
    ]
  }
}
```

### 6.3 Memory 相关 API

#### `GET /api/memory/status`

获取记忆系统概览。

**响应**：
```json
{
  "daily_ledgers": [
    {
      "date": "2026-05-12",
      "event_count": 156,
      "task_count": 8,
      "run_count": 12,
      "compact_status": "COMPACTED",
      "storage_size_mb": 2.3
    }
  ],
  "total_events": 12450,
  "total_tasks": 340,
  "total_runs": 520,
  "memory_file_size_mb": 45.2,
  "local_store_size_mb": 12.8,
  "fts_enabled": true,
  "last_compact_at": "2026-05-12T06:00:00Z"
}
```

#### `GET /api/memory/events`

分片查询记忆事件。

**查询参数**：
- `date`：日期（YYYY-MM-DD）
- `task_id`：任务 ID（可选）
- `run_id`：run ID（可选）
- `event_type`：事件类型（可选）
- `compact_apply_id`：compact apply ID（可选）
- `limit`：默认 50，最大 200
- `offset`：分页偏移

**响应**：
```json
{
  "events": [
    {
      "id": "evt_001",
      "timestamp": "2026-05-12T10:15:30Z",
      "type": "tool_call",
      "task_id": "task_001",
      "run_id": "run_001",
      "summary": "write_file: src/utils/helper.py",
      "artifact_ref": "data/subagents/run_001/artifacts/helper.py",
      "compact_apply_id": null,
      "size_chars": 1200
    }
  ],
  "total": 156,
  "has_more": true
}
```

### 6.4 日志相关 API

#### `GET /api/logs/recent`

获取最近日志。

**查询参数**：
- `source`：日志源（`gateway`, `subagent`, `audit`, `debug_trace`）
- `level`：级别（`debug`, `info`, `warn`, `error`）
- `limit`：默认 50，最大 500
- `since`：ISO 时间，只返回此后的日志

**响应**：
```json
{
  "logs": [
    {
      "timestamp": "2026-05-12T10:15:30Z",
      "level": "info",
      "source": "gateway",
      "message": "Request worker-1 completed run_001",
      "request_id": "req_001",
      "run_id": "run_001"
    }
  ],
  "total": 5000,
  "has_more": true
}
```

### 6.5 Dashboard 相关 API

#### `GET /api/status`

系统总览状态。

**响应**：
```json
{
  "gateway": {
    "running": true,
    "pid": 12345,
    "uptime_seconds": 3600,
    "pending_requests": 2,
    "processing_requests": 1
  },
  "daemon": {
    "running": false
  },
  "model": {
    "backend": "anthropic_compatible",
    "model_name": "MiniMax-M2.7",
    "last_latency_ms": 1200
  },
  "subagents": {
    "total_active": 5,
    "total_pending": 3,
    "total_awaiting_acceptance": 2
  },
  "memory": {
    "record_count": 12450,
    "last_write_at": "2026-05-12T10:15:00Z"
  },
  "notifications": {
    "unread_count": 2
  }
}
```

---

## 7. 安全和隔离设计

### 7.1 API Key 与 Secret 处理

| 规则 | 说明 |
|------|------|
| 默认隐藏 | `type: "secret"` 的字段，输入框默认显示为密码类型（`***`） |
| 不回显真实值 | `GET /api/config/current` 中 secret 字段只返回 `"***"` 或 `"env:AGENT_API_KEY"` |
| 环境变量优先提示 | 如果某 secret 被环境变量覆盖，UI 显示 "已由环境变量设置，此处修改不生效" |
| 修改时清空 | 点击修改 secret 字段时，输入框清空，用户必须重新输入完整值 |
| 不记录日志 | secret 字段的值不进入前端 console.log、不进入 Redux/Zustand 的持久化存储 |

### 7.2 保存前校验

- 前端 Zod 实时校验 + 后端 `POST /api/config/validate` 二次校验
- 校验不通过时，保存按钮 disabled，错误信息定位到字段
- 高风险字段（`riskLevel >= high`）修改后，保存前必须二次确认

### 7.3 高风险配置二次确认

- 对话框显示字段的 `riskWarning` 文案
- 用户必须手动勾选"我已了解风险，确认修改"
- 勾选框下方显示该字段的当前值和新值对比
- 二次确认状态不持久化，每次保存高风险字段都需要重新确认

### 7.4 前端权限边界

- 前端不直接读写 `agent_config.yaml`
- 所有配置操作通过 `POST /api/config/save` 由后端处理
- 后端负责：备份旧配置、校验、写入、返回结果
- 前端只负责：展示、收集用户输入、调用 API、展示结果

### 7.5 禁止浏览器直接读写本地文件

- 不使用 `<input type="file">` 让用户上传/下载配置文件
- "导出 YAML" 功能只在前端生成文本预览，由用户手动复制
- 未来如需文件操作，必须通过后端 API，由 Python 代码处理文件系统权限

### 7.6 配置备份

- 后端每次保存前自动备份到 `data/config/backups/`
- 前端显示最近 5 次备份的时间和修改摘要
- 支持"回滚到某次备份"（未来功能，当前仅展示）

---

## 8. 页面模块规划

### 8.1 Dashboard（首页）

**功能**：
- 系统状态卡片：gateway 运行状态、daemon 状态、模型后端
- 当前活跃任务数、待处理请求数
- 最近错误/警告（最近 5 条）
- 子代理红绿灯看板（按 root / child 层级）
- 记忆增长趋势（最近 7 天 event 数量）
- 快捷操作入口：重启 gateway、运行 smoke test、打开配置页

**数据源**：`GET /api/status`

### 8.2 Config（配置页）

**功能**：
- Schema 驱动的配置表单（核心页面，见第 4 节）
- 左侧分类导航 + 顶部搜索
- 基础/高级切换
- Diff 预览、保存前校验、导出 YAML
- 未保存修改提示
- 配置变更历史（只读，显示最近修改时间）

**数据源**：`GET /api/config/schema` + `GET /api/config/current`

### 8.3 Subagents（子代理页）

**功能**：
- 层级树视图：root / child / grandchild / deeper
- 每个节点显示：agent name、role、status、current step
- Blockers 列表（红色高亮）
- Output refs、artifact refs、debug trace refs（只显示路径和摘要，不加载正文）
- QA / tester / acceptor / bug_finder 角色标识（不同颜色标签）
- Rescue / takeover 状态（闪烁或特殊背景）
- 点击节点展开详情面板：生命周期时间线、验收状态、工具调用统计
- 支持按 status、role、depth 过滤

**数据源**：`GET /api/subagents/runs` + `GET /api/subagents/hierarchy`

**设计约束**：
- 不默认加载大正文，只显示 refs 和摘要
- 用户点击 ref 后，异步加载内容到右侧详情面板
- 层级树使用虚拟滚动，支持 1000+ 节点

### 8.4 Memory（记忆页）

**功能**：
- 日历视图：按日期选择，显示每日 event/task/run 数量
- 分片查询：按 date / task / run / event type / compact apply id 过滤
- 事件列表：只显示摘要（类型、时间、涉及文件、大小）
- 点击事件展开详情：异步加载完整内容
- Compact 状态视图：显示哪些日期已 compact、哪些 pending
- Resume 状态：显示最近的 resume 操作和结果
- Task workspace 浏览器：按 task 聚合事件

**数据源**：`GET /api/memory/status` + `GET /api/memory/events`

**设计约束**：
- 不一次性加载所有内容
- 默认只加载最近一天的数据
- 大文本内容（> 10KB）显示前 500 字符 + "..." + "加载更多"按钮

### 8.5 Tools（工具页）

**功能**：
- 工具列表：名称、描述、启用状态、调用次数
- 工具权限矩阵：哪些子代理角色可以使用哪些工具
- 工具预算：当前周期内调用次数 / 预算上限
- Controlled exec 状态：是否启用、白名单命令
- 写边界策略：`tool_write_inline_max_chars` 等关键配置的只读展示
- 工具调用历史（最近 50 条）

**数据源**：`GET /api/tools`（未来 API）+ `GET /api/config/schema`（工具相关字段）

### 8.6 Logs（日志页）

**功能**：
- 日志源选择：gateway / subagent / audit / debug trace
- 级别过滤：debug / info / warn / error
- 时间范围选择
- 实时滚动（类似 `tail -f`）
- 关键字搜索
- 日志高亮：error 红色、warn 黄色、info 白色/灰色
- 点击日志行展开关联的 request_id / run_id

**数据源**：`GET /api/logs/recent`

### 8.7 Templates（模板页）

**功能**：
- Role templates 列表：coordinator、worker、tester、acceptor、bug_finder 等
- 显示每个 role 的 description、allowed tools、默认参数
- Workflow templates 列表（当前预留）
- Skill templates 列表（未来预留）
- 模板内容预览（只读，修改仍走 CLI 或文件编辑）

**数据源**：`GET /api/templates/roles`（未来 API）+ 读取 `subagents/role_template_catalog/builtin/*.json`

---

## 9. 子代理页面重点设计

### 9.1 层级树展示

```
[root] coordinator-001          RUNNING  [coordinator]
  ├── [child] worker-001        RUNNING  [worker]
  │     ├── [grandchild] tester-001  DONE  [tester]
  │     └── [grandchild] writer-001  AWAITING_ACCEPTANCE  [writer]
  ├── [child] worker-002        BLOCKED  [worker]  ⚠️ 工具调用超时
  └── [child] researcher-001    RUNNING  [researcher]
        └── [grandchild] bug_finder-001  PENDING  [bug_finder]
```

- 不同角色用不同颜色标签
- status 用图标：RUNNING（旋转）、DONE（绿色对勾）、BLOCKED（红色感叹号）、PENDING（灰色时钟）
- 点击节点展开右侧详情面板

### 9.2 详情面板内容

```
+----------------------------------+
|  worker-001                       |
|  Role: worker | Status: RUNNING  |
|  Run ID: run_20260512_001        |
+----------------------------------+
|  Current Step                     |
|  执行工具调用: write_file          |
|  目标: src/utils/helper.py        |
+----------------------------------+
|  Blockers                         |
|  无                               |
+----------------------------------+
|  Output Refs                      |
|  • output.json  (1.2KB)           |
|  • DEBRIEF.md  (800B)             |
+----------------------------------+
|  Artifact Refs                    |
|  • patch.diff  (450B)  [查看]    |
+----------------------------------+
|  Debug Trace Refs                 |
|  • debug_trace.jsonl  [下载]     |
+----------------------------------+
|  生命周期                          |
|  创建: 10:00 | 更新: 10:15        |
|  预计完成: 10:30                  |
+----------------------------------+
|  验收状态                          |
|  QA: PENDING | Tester: DONE       |
|  Acceptor: -- | Bug Finder: --    |
+----------------------------------+
```

### 9.3 QA / Tester / Acceptor / Bug Finder 角色展示

- 在详情面板的"验收状态"区域集中展示
- 每个角色的状态用颜色区分：DONE（绿）、PENDING（灰）、NEEDS_ACCEPTANCE（橙）、FAILED（红）
- 点击角色标签可展开该角色的详细报告（异步加载）

### 9.4 Rescue / Takeover 状态

- rescue 状态：节点边框变为橙色虚线，显示 "RESCUE" 标签
- takeover 状态：节点背景变为浅蓝色，显示 "TAKEOVER by parent" 标签
- hover 时显示 rescue/takeover 原因

### 9.5 性能约束

- 层级树使用虚拟滚动（React Virtual），支持 1000+ 节点
- 默认只展开 root 和第一层 child
- 不默认加载任何大正文，refs 只显示路径和文件大小
- 用户点击"查看"后才异步加载内容到详情面板

---

## 10. Memory 页面重点设计

### 10.1 分片查询策略

Memory 页面必须遵循**按需加载**原则，禁止一次性加载所有内容。

**查询维度**：

| 维度 | 说明 | UI 表现 |
|------|------|---------|
| `date` | 按日期过滤 | 日历选择器 + 日期列表 |
| `task` | 按任务 ID 聚合 | Task 下拉选择器 |
| `run` | 按 run ID 聚合 | Run ID 搜索框 |
| `event` | 按事件类型过滤 | 多选标签：tool_call、memory_write、subagent_spawn 等 |
| `compact_apply_id` | 按 compact 批次过滤 | Compact ID 下拉选择器 |
| `artifact_ref` | 按产物引用过滤 | 路径搜索框 |

### 10.2 默认加载策略

- 页面首次加载：只显示最近一天的 summary（event count、task count、run count）
- 用户选择日期后：加载该日的事件列表（分页，每页 50 条）
- 用户选择 task 后：加载该 task 下的事件列表
- 事件列表只显示摘要（类型、时间、涉及文件、大小），不显示完整内容

### 10.3 内容加载策略

| 内容大小 | 处理方式 |
|----------|----------|
| < 1KB | 直接显示完整内容 |
| 1KB ~ 10KB | 显示前 500 字符 + "..." + "展开"按钮 |
| > 10KB | 显示前 200 字符 + "内容较大（12.5KB），点击加载" |
| Artifact 文件 | 只显示路径和大小，提供"在新标签页查看"链接 |

### 10.4 Compact / Resume 状态展示

- 日历视图中，已 compact 的日期显示绿色圆点
- 未 compact 的日期显示灰色圆点
- 点击日期后，在右侧显示 compact 详情：compact_apply_id、compact 前后大小、耗时
- Resume 操作记录显示在独立面板：resume 时间、来源、恢复的内容摘要

---

## 11. 设计原则

| 原则 | 说明 |
|------|------|
| **前端不能污染现有后端架构** | 前端代码完全隔离在 `frontend/` 和 `docs/frontend/`，不修改 `agent_py_agent/`、不增加 Python 依赖 |
| **Schema 驱动** | 配置页由后端 schema 驱动，不是硬编码组件。新增配置字段只需更新后端 schema，前端自动适配 |
| **默认中文友好** | 所有用户可见文案为中文，技术名词首次出现标注英文 |
| **高级配置折叠** | 默认只展示基础配置，减少新用户的认知负担 |
| **大文本和日志必须分页/摘要** | 不一次性加载大文本，强制分页或摘要展示 |
| **不直接读写本地敏感文件** | 前端不直接操作文件系统，所有文件操作通过后端 API |
| **不提交 node_modules、dist、缓存** | `.gitignore` 必须排除前端构建产物和依赖 |
| **不增加 CI 工作流** | 本次不增加 `.github/workflows/` 中的前端 CI |
| **不引入后端依赖** | 不在 `pyproject.toml` 中加入前端相关依赖 |
| **后续再由主线程决定是否实现 API** | 本方案只定义 API 契约，不强制后端立即实现 |

---

## 12. 开发阶段规划

### Phase 1：纯前端静态原型

**目标**：搭建前端项目骨架，实现无数据的原型界面
**产出**：
- `frontend/` 目录创建
- Vite + React + TypeScript + Tailwind CSS 项目初始化
- 基础布局：Sidebar、Header、Page 容器
- 空页面占位：Dashboard、Config、Subagents、Memory、Tools、Logs、Templates
- 路由配置完成

**不做什么**：
- 不接入任何 API
- 不写真实表单逻辑
- 不做数据持久化

**验收标准**：
- `npm run dev` 能启动，浏览器能看到 7 个页面的空壳
- 路由切换正常
- 布局在不同屏幕尺寸下不崩

---

### Phase 2：配置 Schema Mock

**目标**：实现 Config 页面的 schema 驱动表单
**产出**：
- 手写 mock schema（基于 `agent_config.yaml` 实际字段）
- 实现 `ConfigField` 组件映射（string、number、boolean、choice、string_list、path、secret）
- 实现左侧分类导航
- 实现表单渲染（根据 schema 自动生成字段）
- 实现基础/高级切换

**不做什么**：
- 不接真实后端
- 不实现保存功能
- 不做校验

**验收标准**：
- Config 页面能展示所有分类和字段
- 切换基础/高级模式，字段显示/隐藏正确
- 左侧导航点击能跳转到对应分类

---

### Phase 3：配置校验和 YAML 导出

**目标**：实现前端校验和导出功能
**产出**：
- Zod schema 定义（与后端 schema 对应）
- 实时校验：输入时提示错误
- Diff 预览：展示修改项
- 导出 YAML：生成 YAML 文本预览
- 重置单项、恢复默认

**不做什么**：
- 不接后端 API
- 不实际保存配置

**验收标准**：
- 修改字段后，diff 面板正确显示变更
- 输入非法值时，实时显示错误提示
- 点击"导出 YAML"能生成合法 YAML
- 重置单项和恢复默认功能正常

---

### Phase 4：接后端只读 API

**目标**：前端接入后端只读 API
**产出**：
- API client 封装（fetch + error handling）
- 接入 `GET /api/config/schema`
- 接入 `GET /api/config/current`
- 接入 `GET /api/status`（Dashboard 数据）
- 接入 `GET /api/subagents/runs`（Subagents 数据）
- 接入 `GET /api/memory/status`（Memory 概览）
- 接入 `GET /api/logs/recent`（Logs 数据）
- 加载状态、错误状态、空状态处理

**不做什么**：
- 不接写入 API（不保存配置）
- 不做权限控制

**验收标准**：
- Dashboard 显示真实系统状态
- Config 页面从后端加载 schema 和当前值
- Subagents 页面显示真实运行列表
- 网络错误时有友好的错误提示

---

### Phase 5：接配置保存 API

**目标**：实现配置的保存和校验
**产出**：
- 接入 `POST /api/config/validate`
- 接入 `POST /api/config/save`
- 接入 `POST /api/config/export-yaml`
- 保存前校验流程
- 高风险配置二次确认
- 保存成功/失败提示
- 未保存修改提示（`beforeunload`）

**不做什么**：
- 不接子代理写入 API
- 不接 memory 写入 API

**验收标准**：
- 修改配置后，点击"保存"能成功写入后端
- 校验失败时，错误信息定位到字段
- 高风险字段修改后，保存前弹出二次确认
- 离开页面前有未保存提示

---

### Phase 6：Subagent / Memory / Logs 页面完善

**目标**：完善非配置页面的功能和交互
**产出**：
- Subagents 层级树视图（虚拟滚动）
- Subagents 详情面板
- Memory 分片查询和日历视图
- Memory 事件详情异步加载
- Logs 实时滚动和过滤
- Templates 只读预览

**不做什么**：
- 不接写入 API（子代理操作仍走 CLI）
- 不做实时 WebSocket（先轮询）

**验收标准**：
- Subagents 层级树能展示 100+ 节点不卡顿
- Memory 页面分片查询响应 < 1s
- Logs 页面能实时滚动新日志

---

### Phase 7：权限、安全、审计

**目标**：增加权限控制和审计功能
**产出**：
- 登录页面（如果 `auth_enabled`）
- 前端路由权限控制
- Secret 字段隐藏和加密传输
- 配置变更历史查看（只读）
- 操作审计日志（前端记录用户操作）

**不做什么**：
- 不做复杂的 RBAC（先支持 admin/readonly 两种角色）
- 不做 SSO/OAuth

**验收标准**：
- 未登录用户不能访问管理界面
- Secret 字段不回显真实值
- 配置变更有审计记录

---

### Phase 8：真实 E2E 验收

**目标**：完整端到端测试
**产出**：
- E2E 测试：Playwright 或 Cypress
- 核心场景测试：修改配置 -> 保存 -> 验证生效
- 性能测试：大数据量下的页面响应
- 安全测试：secret 不回显、XSS 防护、CSRF 防护
- 文档更新：部署指南、开发指南

**验收标准**：
- E2E 测试通过率 100%
- 配置修改后，CLI 能读取到新值
- 安全测试无高危漏洞
- 部署文档清晰，新开发者能在 10 分钟内跑起来

---

## 13. 验收标准

### 本次文档任务完成标准

- [x] 只新增或修改 `/Users/example/my_agent/my-agent/docs/frontend/FRONTEND_CONFIG_APP_PLAN.md`
- [x] 不修改任何 Python 代码
- [x] 不修改 `agent_config.yaml`
- [x] 不安装任何依赖
- [x] 不创建 `node_modules`
- [x] 不改 CI（`.github/workflows/`）
- [x] 文档以中文为主，必要技术名词中英双语
- [x] 文档足够详细，让后续 agent 可以按阶段实现

### 文档质量检查

- [x] 包含前端整体定位说明
- [x] 包含技术栈建议和选型理由
- [x] 包含目录隔离要求
- [x] 包含配置页面 schema 驱动设计
- [x] 包含 `tool_write_inline_max_chars` 重点说明
- [x] 包含 UI 交互设计（12 项交互）
- [x] 包含 API 边界设计（7+ API）
- [x] 包含安全和隔离设计
- [x] 包含 7 个页面模块规划
- [x] 包含子代理页面重点设计
- [x] 包含 Memory 页面分片查询设计
- [x] 包含设计原则
- [x] 包含 8 个开发阶段规划
- [x] 包含验收标准

---

## 附录 A：配置字段 Schema 示例（完整片段）

以下是一个基于 `agent_config.yaml` 实际字段的 schema 片段，用于展示 schema 驱动的具体形态：

```typescript
const configSchemaExample: ConfigSchema = {
  version: "0.3.0",
  categories: [
    {
      key: "basic",
      label: "基础信息",
      description: "智能体名称、人格、工作区等基础配置",
      icon: "Settings",
      order: 1,
      fields: [
        {
          key: "agent_name",
          label: "智能体名称",
          description: "主要用于 CLI 输出和日志标识",
          type: "string",
          defaultValue: "myagent",
          minLength: 1,
          maxLength: 50,
          category: "basic",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 1,
        },
        {
          key: "system_prompt",
          label: "系统人格（System Prompt）",
          description: "每次请求都会放进最终 prompt 的 System 段",
          type: "text",
          defaultValue:
            "你是一个谨慎、可扩展、会记录记忆、会在必要时调用工具的 Python CLI 智能体。先理解任务，再给出结构化回答。",
          category: "basic",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          order: 2,
        },
        {
          key: "workspace_root",
          label: "工作区根目录",
          description:
            "留空时使用项目默认根目录。memory_path、subagent_workspace、gateway_workspace 和文件工具都会以这个目录为基础。",
          type: "string_list",
          defaultValue: [""],
          category: "basic",
          advanced: true,
          restartRequired: true,
          riskLevel: "high",
          riskWarning: "修改工作区根目录后，所有数据路径都会变化。请确保新目录存在且有写入权限。",
          order: 3,
        },
      ],
    },
    {
      key: "model",
      label: "模型配置",
      description: "模型后端、API 地址、模型名称和生成参数",
      icon: "Brain",
      order: 2,
      fields: [
        {
          key: "model_backend",
          label: "模型后端",
          description: "选择模型通信协议：echo（离线调试）、openai_compatible、anthropic_compatible",
          type: "choice",
          defaultValue: "anthropic_compatible",
          choices: ["echo", "openai_compatible", "anthropic_compatible"],
          choiceLabels: {
            echo: "Echo（离线调试）",
            openai_compatible: "OpenAI 兼容",
            anthropic_compatible: "Anthropic 兼容",
          },
          category: "model",
          advanced: false,
          restartRequired: true,
          riskLevel: "low",
          order: 1,
        },
        {
          key: "api_base",
          label: "API 地址",
          description: "真实模型接口地址",
          type: "string",
          defaultValue: "https://api.minimaxi.com/anthropic",
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
        {
          key: "api_key_env",
          label: "API Key 环境变量名",
          description: "API Key 对应的环境变量名。Windows: $env:AGENT_API_KEY；macOS/Linux: export AGENT_API_KEY",
          type: "string",
          defaultValue: "AGENT_API_KEY",
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          order: 3,
        },
        {
          key: "model_name",
          label: "模型名称",
          description: "使用的具体模型名称",
          type: "string",
          defaultValue: "MiniMax-M2.7",
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 4,
        },
        {
          key: "max_tokens",
          label: "最大生成 Token 数",
          description: "允许模型最多生成多少 token",
          type: "number",
          defaultValue: 16314,
          min: 1,
          max: 200000,
          unit: "tokens",
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 5,
        },
        {
          key: "temperature",
          label: "采样温度（Temperature）",
          description: "控制模型输出的随机性。0 为确定性输出，越高越随机",
          type: "number",
          defaultValue: 0.2,
          min: 0,
          max: 2,
          step: 0.1,
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 6,
        },
        {
          key: "request_timeout",
          label: "请求超时（秒）",
          description: "HTTP 请求总超时秒数。超时会记录为 provider_timeout",
          type: "number",
          defaultValue: 60,
          min: 1,
          max: 3600,
          unit: "秒",
          category: "model",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 7,
        },
      ],
    },
    {
      key: "tools",
      label: "工具配置",
      description: "工具调用开关、预算和边界限制",
      icon: "Wrench",
      order: 3,
      fields: [
        {
          key: "enable_tools",
          label: "启用工具调用",
          description: "是否启用工具调用循环",
          type: "boolean",
          defaultValue: true,
          category: "tools",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          riskWarning: "关闭后模型将无法调用任何工具，只能进行纯文本对话",
          order: 1,
        },
        {
          key: "max_tool_rounds",
          label: "最大工具轮数",
          description: "单轮请求里，最多允许几次"模型调用工具再继续推理"；0 表示不限制",
          type: "number",
          defaultValue: 0,
          min: 0,
          max: 50,
          unit: "轮",
          category: "tools",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
        {
          key: "tool_write_inline_max_chars",
          label: "写文件内联推荐字符数",
          description:
            "控制 write_file / append_file 单次 inline content 的推荐字符数。超过该值的合法工具调用仍会写入文件，但会提示模型后续改用分块，避免反复输出超长 JSON。",
          shortDescription: "write_file / append_file 单次 inline content 推荐值",
          type: "number",
          defaultValue: 12000,
          min: 100,
          max: 100000,
          step: 1000,
          unit: "字符",
          category: "tools",
          subCategory: "write_boundary",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          riskWarning:
            "值过大（> 50000）时，模型可能在工具 JSON 输出中被截断，导致 write_file 失败或内容不完整。值过小（< 1000）时，大文件写入会产生大量分块，降低效率。",
          order: 10,
        },
        {
          key: "tool_read_max_chars",
          label: "读文件返回字符上限",
          description: "read_file 工具最多返回多少字符，避免单次结果太长",
          type: "number",
          defaultValue: 50000,
          min: 100,
          max: 50000,
          step: 500,
          unit: "字符",
          category: "tools",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 11,
        },
        {
          key: "tool_list_max_entries",
          label: "文件列表最大条目数",
          description: "list_files 工具最多列出多少条结果",
          type: "number",
          defaultValue: 200,
          min: 1,
          max: 10000,
          unit: "条",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 12,
        },
        {
          key: "tool_web_max_chars",
          label: "网页/API 返回字符上限",
          description: "fetch_url / http_request 工具最多返回多少字符",
          type: "number",
          defaultValue: 100000,
          min: 0,
          max: 200000,
          step: 1000,
          unit: "字符",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          riskWarning: "值越大越适合读取长网页或大 JSON，但也更容易把单次工具结果带入大量上下文。",
          order: 13,
        },
        {
          key: "tool_http_timeout",
          label: "HTTP 工具超时（秒）",
          description: "fetch_url / http_request 工具默认超时秒数",
          type: "number",
          defaultValue: 30,
          min: 1,
          max: 300,
          unit: "秒",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 14,
        },
        {
          key: "tool_shell_timeout",
          label: "Shell 工具超时（秒）",
          description: "run_command 工具默认超时秒数",
          type: "number",
          defaultValue: 240,
          min: 1,
          max: 300,
          unit: "秒",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          riskWarning: "超时过长可能导致危险命令长时间运行",
          order: 15,
        },
        {
          key: "tool_agent_budget_window_seconds",
          label: "工具预算窗口（秒）",
          description: "单个代理滚动工具预算的时间窗口",
          type: "number",
          defaultValue: 600,
          min: 10,
          max: 3600,
          unit: "秒",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 16,
        },
        {
          key: "tool_agent_budget_max_calls",
          label: "工具预算最大调用次数",
          description: "单个代理在预算窗口内最多调用多少次工具",
          type: "number",
          defaultValue: 50,
          min: 1,
          max: 1000,
          unit: "次",
          category: "tools",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 17,
        },
      ],
    },
    {
      key: "memory",
      label: "记忆配置",
      description: "记忆存储、归档、规则路由和恢复策略",
      icon: "Database",
      order: 4,
      fields: [
        {
          key: "memory_path",
          label: "记忆文件路径",
          description: "记忆文件路径，JSONL 格式，每行一条记录",
          type: "path",
          defaultValue: "data/memory.jsonl",
          category: "memory",
          advanced: false,
          restartRequired: true,
          riskLevel: "medium",
          riskWarning: "修改后记忆存储位置会变，旧记忆不会自动迁移",
          order: 1,
        },
        {
          key: "memory_top_k",
          label: "记忆检索条数",
          description: "每轮请求最多带多少条相关记忆进入 prompt",
          type: "number",
          defaultValue: 5,
          min: 0,
          max: 50,
          unit: "条",
          category: "memory",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
        {
          key: "auto_save_memory",
          label: "自动保存记忆",
          description: "是否自动把用户输入和模型回复写入记忆",
          type: "boolean",
          defaultValue: true,
          category: "memory",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 3,
        },
        {
          key: "memory_archive_level",
          label: "记忆归档粒度",
          description:
            "控制全量会话冷归档粒度。0: 尽量全量；1: 去掉重型工具展示数据；2: 摘要加关键事件；3: 最小恢复快照",
          type: "choice",
          defaultValue: 3,
          choices: ["0", "1", "2", "3"],
          choiceLabels: {
            "0": "0 - 尽量全量",
            "1": "1 - 去掉重型数据",
            "2": "2 - 摘要加关键事件",
            "3": "3 - 最小恢复快照",
          },
          category: "memory",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 4,
        },
        {
          key: "memory_rule_routing_enabled",
          label: "启用长期规则路由",
          description: "是否启用长期规则路由",
          type: "boolean",
          defaultValue: true,
          category: "memory",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 5,
        },
        {
          key: "memory_rule_routing_mode",
          label: "规则路由模式",
          description:
            "off: 不做长期规则路由；soft: 命中后提示/自动读少量规则；strict: 强规则场景必须读权威文件",
          type: "choice",
          defaultValue: "soft",
          choices: ["off", "soft", "strict"],
          choiceLabels: {
            off: "关闭",
            soft: "软规则（提示+自动读取）",
            strict: "严格（必须读取权威文件）",
          },
          category: "memory",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          dependsOn: { field: "memory_rule_routing_enabled", value: true },
          order: 6,
        },
        {
          key: "memory_resume_auto_context_enabled",
          label: "启用自动恢复上下文",
          description: "默认关闭，避免普通请求因为恢复检索变慢",
          type: "boolean",
          defaultValue: false,
          category: "memory",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 7,
        },
        {
          key: "memory_compact_auto_allow_apply",
          label: "允许自动 Compact Apply",
          description:
            "默认关闭；开启后也只允许非破坏性 apply + auto resume + guard，不自动跑工具",
          type: "boolean",
          defaultValue: false,
          category: "memory",
          advanced: true,
          restartRequired: false,
          riskLevel: "high",
          riskWarning:
            "开启后系统会自动执行 compact apply 操作。虽然已有限制，但仍建议在有监督的情况下使用。",
          confirmationRequired: true,
          order: 8,
        },
      ],
    },
    {
      key: "gateway",
      label: "Gateway 配置",
      description: "后台常驻 Gateway 的工作区、心跳、请求队列和 HTTP 服务",
      icon: "Server",
      order: 5,
      fields: [
        {
          key: "gateway_workspace",
          label: "Gateway 工作区",
          description: "Gateway 本地状态存储目录",
          type: "path",
          defaultValue: "data/gateway",
          category: "gateway",
          advanced: false,
          restartRequired: true,
          riskLevel: "medium",
          order: 1,
        },
        {
          key: "gateway_port",
          label: "HTTP 服务端口",
          description: "Gateway 守护进程 HTTP 服务端口。设置为 0 则禁用 HTTP 服务",
          type: "number",
          defaultValue: 8420,
          min: 0,
          max: 65535,
          category: "gateway",
          advanced: false,
          restartRequired: true,
          riskLevel: "medium",
          riskWarning: "修改端口后需要重启 Gateway 才能生效",
          order: 2,
        },
        {
          key: "gateway_heartbeat_interval",
          label: "心跳间隔（秒）",
          description: "Gateway 心跳刷新间隔",
          type: "number",
          defaultValue: 5,
          min: 1,
          max: 60,
          unit: "秒",
          category: "gateway",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 3,
        },
        {
          key: "gateway_request_workers",
          label: "请求 Worker 数",
          description:
            "本地 ask 队列并发处理数。1 是最保守默认值；调大后每个 worker 都会创建自己的 agent/backend",
          type: "number",
          defaultValue: 1,
          min: 1,
          max: 10,
          unit: "个",
          category: "gateway",
          advanced: true,
          restartRequired: true,
          riskLevel: "medium",
          riskWarning: "增加 worker 数会提高并发，但也会增加资源消耗",
          order: 4,
        },
        {
          key: "gateway_request_timeout",
          label: "请求超时（秒）",
          description: "CLI 等待 Gateway 结果的默认时长",
          type: "number",
          defaultValue: 300,
          min: 10,
          max: 3600,
          unit: "秒",
          category: "gateway",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 5,
        },
      ],
    },
    {
      key: "daemon",
      label: "Daemon 配置",
      description: "前台常驻 Daemon 的调度策略",
      icon: "Activity",
      order: 6,
      fields: [
        {
          key: "daemon_planner",
          label: "启用 Planner",
          description: "Daemon 是否启用 planner 阶段",
          type: "boolean",
          defaultValue: true,
          category: "daemon",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 1,
        },
        {
          key: "daemon_apply",
          label: "自动应用调度",
          description: "Daemon 是否自动应用调度结果",
          type: "boolean",
          defaultValue: true,
          category: "daemon",
          advanced: false,
          restartRequired: false,
          riskLevel: "high",
          riskWarning: "关闭后 daemon 只会生成计划，不会自动执行",
          order: 2,
        },
        {
          key: "daemon_execute_runners",
          label: "自动执行 Runner",
          description: "Daemon 是否自动执行子代理 runner",
          type: "boolean",
          defaultValue: true,
          category: "daemon",
          advanced: false,
          restartRequired: false,
          riskLevel: "high",
          riskWarning: "关闭后 runner 不会自动执行，需要手动触发",
          confirmationRequired: true,
          order: 3,
        },
        {
          key: "daemon_interval",
          label: "调度间隔（秒）",
          description: "Daemon 每轮调度之间的间隔",
          type: "number",
          defaultValue: 30,
          min: 0,
          max: 3600,
          unit: "秒",
          category: "daemon",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 4,
        },
        {
          key: "daemon_max_runners",
          label: "每轮最大 Runner 数",
          description: "每轮调度最多推进多少 runner。auto 映射为保守值 1",
          type: "string",
          defaultValue: "auto",
          pattern: "^(auto|[0-9]+)$",
          patternMessage: "请输入 auto 或正整数",
          category: "daemon",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 5,
        },
      ],
    },
    {
      key: "subagent",
      label: "子代理配置",
      description: "子代理开关、自动化级别、规模限制和调试追踪",
      icon: "Users",
      order: 7,
      fields: [
        {
          key: "enable_subagents",
          label: "启用子代理",
          description: "是否启用子任务记录和子代理工作流",
          type: "boolean",
          defaultValue: true,
          category: "subagent",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          riskWarning: "关闭后所有子代理功能不可用，复杂任务将由主代理直接处理",
          order: 1,
        },
        {
          key: "max_subagents",
          label: "最大子代理数",
          description: "单次最多拆出多少个子任务。0 表示不设硬上限",
          type: "number",
          defaultValue: 1000,
          min: 0,
          max: 10000,
          unit: "个",
          category: "subagent",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 2,
        },
        {
          key: "subagent_debug_trace_level",
          label: "调试追踪等级",
          description:
            "0: 关闭；1: 关键生命周期；2: 调度/验收/refs；3: 模型请求/响应/refs；4: 短预览；5: 完整 prompt/response（仅排障）",
          type: "choice",
          defaultValue: 0,
          choices: ["0", "1", "2", "3", "4", "5"],
          choiceLabels: {
            "0": "0 - 关闭",
            "1": "1 - 关键生命周期",
            "2": "2 - 调度/验收/refs",
            "3": "3 - 模型请求/响应/refs",
            "4": "4 - 短预览（JSONL）",
            "5": "5 - 完整内容（仅排障）",
          },
          category: "subagent",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 3,
        },
        {
          key: "subagent_mode",
          label: "子代理模式",
          description: "默认 trusted_local_hardening：少卡流程，优先把活干好；更保守模式后续再接。",
          type: "choice",
          defaultValue: "trusted_local_hardening",
          choices: ["trusted_local_hardening", "balanced", "strict"],
          category: "subagent",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          order: 4,
        },
        {
          key: "subagent_workspace",
          label: "子代理工作区",
          description: "保存子代理 task、runner 日志、验收报告和 refs 的目录。",
          type: "string",
          defaultValue: "data/subagents",
          category: "subagent",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 5,
        },
        {
          key: "subagent_role_template_dirs",
          label: "角色模板目录",
          description: "额外角色模板目录；[] 表示自动使用内置模板和工作区模板。",
          type: "string_list",
          defaultValue: [],
          category: "subagent",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 6,
        },
      ],
    },
    {
      key: "scheduler",
      label: "调度策略",
      description: "Runner 并发、超时和失败重试策略",
      icon: "GitBranch",
      order: 8,
      fields: [
        {
          key: "scheduler_mode",
          label: "调度模式",
          description: "auto: 自适应调整；off: 不设外层超时",
          type: "choice",
          defaultValue: "auto",
          choices: ["auto", "off"],
          choiceLabels: {
            auto: "自适应",
            off: "关闭",
          },
          category: "scheduler",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 1,
        },
        {
          key: "runner_failure_policy",
          label: "Runner 失败策略",
          description: "auto: 最多 2 次；off: 不自动重试；数字: 最多尝试 N 次",
          type: "string",
          defaultValue: "auto",
          pattern: "^(auto|off|none|disabled|[0-9]+)$",
          patternMessage: "请输入 auto、off、none、disabled 或正整数",
          category: "scheduler",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 2,
        },
        {
          key: "max_auto_retry_attempts",
          label: "最大自动重试次数",
          description: "失败后最大自动重试次数。超过后通知用户介入",
          type: "number",
          defaultValue: 3,
          min: 0,
          max: 10,
          unit: "次",
          category: "scheduler",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 5,
        },
      ],
    },
    {
      key: "acceptance",
      label: "验收策略",
      description: "父级验收的测试执行和超时配置",
      icon: "CheckCircle",
      order: 9,
      fields: [
        {
          key: "acceptance_execute_tests",
          label: "执行验收测试",
          description: "父级验收是否默认真实执行 runner 输出里的 tests",
          type: "boolean",
          defaultValue: false,
          category: "acceptance",
          advanced: false,
          restartRequired: false,
          riskLevel: "high",
          riskWarning:
            "开启后会自动执行 runner 填写的测试命令。请确保测试命令安全，不会破坏数据。",
          confirmationRequired: true,
          order: 1,
        },
        {
          key: "acceptance_test_timeout_seconds",
          label: "测试超时（秒）",
          description: "父级验收真实执行 tests 时的单条测试超时",
          type: "number",
          defaultValue: 120,
          min: 1,
          max: 300,
          unit: "秒",
          category: "acceptance",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
      ],
    },
    {
      key: "capability",
      label: "能力路由",
      description: "Skill / Tool / 子代理授权和能力上抛参数",
      icon: "Zap",
      order: 10,
      fields: [
        {
          key: "enable_capability_routing",
          label: "启用能力路由",
          description: "是否允许子代理用 capability_request 向父代理申请 skill/tool 能力",
          type: "boolean",
          defaultValue: false,
          category: "capability",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          order: 1,
        },
        {
          key: "capability_grant_expires_after_task",
          label: "授权随任务结束回收",
          description: "建议保持开启，任务级临时能力结束后自动回收，避免污染后续任务。",
          type: "boolean",
          defaultValue: true,
          category: "capability",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
      ],
    },
    {
      key: "notification",
      label: "通知系统",
      description: "通知推送和存储配置",
      icon: "Bell",
      order: 11,
      fields: [
        {
          key: "notification_enabled",
          label: "启用通知",
          description: "是否启用通知推送",
          type: "boolean",
          defaultValue: true,
          category: "notification",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 1,
        },
        {
          key: "notification_store_path",
          label: "通知存储路径",
          description: "通知存储目录，每个通知一个 JSON 文件",
          type: "path",
          defaultValue: "data/notifications",
          category: "notification",
          advanced: true,
          restartRequired: true,
          riskLevel: "medium",
          order: 2,
        },
      ],
    },
    {
      key: "audit",
      label: "审计日志",
      description: "审计日志开关和存储路径",
      icon: "FileText",
      order: 12,
      fields: [
        {
          key: "audit_enabled",
          label: "启用审计日志",
          description: "是否启用审计日志",
          type: "boolean",
          defaultValue: true,
          category: "audit",
          advanced: false,
          restartRequired: false,
          riskLevel: "low",
          order: 1,
        },
        {
          key: "audit_log_path",
          label: "审计日志路径",
          description: "审计日志存储路径",
          type: "path",
          defaultValue: "data/audit",
          category: "audit",
          advanced: true,
          restartRequired: true,
          riskLevel: "medium",
          order: 2,
        },
      ],
    },
    {
      key: "log_analysis",
      label: "日志分析",
      description: "日志分析模块独立配置",
      icon: "BarChart",
      order: 13,
      fields: [
        {
          key: "log_analysis_enabled",
          label: "启用日志分析",
          description: "是否启用日志分析模块",
          type: "boolean",
          defaultValue: false,
          category: "log_analysis",
          advanced: false,
          restartRequired: false,
          riskLevel: "medium",
          order: 1,
        },
        {
          key: "log_analysis_capability_level",
          label: "能力等级",
          description: "日志分析能力等级",
          type: "string",
          defaultValue: "L0",
          category: "log_analysis",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
        {
          key: "log_analysis_response_mode",
          label: "响应模式",
          description: "recommend: 只给建议；dry_run: 只生成试运行计划；execute: 需额外开启",
          type: "choice",
          defaultValue: "recommend",
          choices: ["recommend", "dry_run", "execute"],
          choiceLabels: {
            recommend: "建议",
            dry_run: "试运行",
            execute: "执行",
          },
          category: "log_analysis",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 3,
        },
      ],
    },
    {
      key: "auth",
      label: "安全与认证",
      description: "多租户鉴权和用户隔离",
      icon: "Shield",
      order: 14,
      fields: [
        {
          key: "auth_enabled",
          label: "启用鉴权",
          description: "是否启用鉴权。关闭后所有人都有 admin 权限（方便本地开发）",
          type: "boolean",
          defaultValue: true,
          category: "auth",
          advanced: false,
          restartRequired: true,
          riskLevel: "high",
          riskWarning: "关闭鉴权后，任何能访问管理界面的人都有 admin 权限",
          confirmationRequired: true,
          order: 1,
        },
        {
          key: "admin_user_id",
          label: "管理员用户 ID",
          description: "管理员用户 ID，外部通道携带此 user_id 时也获得 admin 权限",
          type: "string",
          defaultValue: "admin",
          category: "auth",
          advanced: true,
          restartRequired: true,
          riskLevel: "critical",
          riskWarning: "修改管理员 ID 可能导致当前会话失去权限！",
          confirmationRequired: true,
          order: 2,
        },
        {
          key: "user_id",
          label: "当前用户 ID",
          description: "当前用户标识，默认为 admin",
          type: "string",
          defaultValue: "admin",
          category: "auth",
          advanced: true,
          restartRequired: true,
          riskLevel: "medium",
          order: 3,
        },
      ],
    },
    {
      key: "watchdog",
      label: "Watchdog",
      description: "Daemon 进程监控和自动重启",
      icon: "Eye",
      order: 15,
      fields: [
        {
          key: "watchdog_enabled",
          label: "启用 Watchdog",
          description: "是否启用 watchdog 监控 daemon 进程存活",
          type: "boolean",
          defaultValue: false,
          category: "watchdog",
          advanced: true,
          restartRequired: false,
          riskLevel: "medium",
          order: 1,
        },
        {
          key: "watchdog_interval",
          label: "检查间隔（秒）",
          description: "Watchdog 检查间隔",
          type: "number",
          defaultValue: 60,
          min: 5,
          max: 3600,
          unit: "秒",
          category: "watchdog",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 2,
        },
        {
          key: "watchdog_max_restarts",
          label: "最大重启次数",
          description: "最多自动重启次数",
          type: "number",
          defaultValue: 3,
          min: 0,
          max: 100,
          unit: "次",
          category: "watchdog",
          advanced: true,
          restartRequired: false,
          riskLevel: "low",
          order: 3,
        },
      ],
    },
  ],
  meta: {
    lastUpdated: "2026-05-12T00:00:00Z",
    source: "agent_config.yaml + capability_config.yaml + log_analysis_config.yaml",
    editable: true,
    exportFormats: ["yaml", "json"],
  },
};
```

---

## 附录 B：与现有项目的兼容性说明

### B.1 配置来源

本方案的 schema 完全基于以下现有配置文件的实际字段：

1. **`agent_py_agent/config/agent_config.yaml`** — 主配置（100+ 字段）
2. **`agent_py_agent/config/capability_config.yaml`** — 能力路由配置（20+ 字段）
3. **`agent_py_agent/config/log_analysis_config.yaml`** — 日志分析配置（15+ 字段）

### B.2 代码参考

前端 schema 的设计参考了以下 Python 代码的结构：

- `agent_py_agent/agent/settings/config.py` — `AgentConfig` dataclass 定义
- `agent_py_agent/agent/settings/tool_config.py` — `ToolConfig` 定义
- `agent_py_agent/agent/settings/subagent_config.py` — 子代理配置
- `agent_py_agent/agent/settings/memory.py` — 记忆配置
- `agent_py_agent/agent/settings/normalize.py` — 配置归一化规则

### B.3 不引入的字段

以下字段当前不在前端 schema 中，因为它们是内部实现细节或暂不需要前端管理：

- `extensions_dir`（预留，无实际 UI）
- `anthropic_version`（协议版本，通常不需要修改）
- `stream_enabled`（已在 `ToolConfig` 中）
- `concurrency_lock_enabled` / `task_lock_timeout_seconds`（内部并发控制）
- `model_speed_profile_path` / `auto_bench_model_on_first_use`（速度测试）
- `feishu_*` / `qq_*`（外部适配器配置，未来可在独立页面管理）
- `session_workspace`（会话工作区）

---

*文档结束。后续实现请严格遵循本方案的目录隔离要求和开发阶段规划。*
