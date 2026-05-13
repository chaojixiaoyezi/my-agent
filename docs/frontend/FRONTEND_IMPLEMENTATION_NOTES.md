# 前端实现记录

> 记录日期：2026-05-13
> 对应阶段：Phase 1-3 强化版

## 已完成页面与交互

### 页面（7 个）

| 页面 | 路径 | 状态 |
|------|------|------|
| Dashboard | `/` | 完成，含状态卡片、事件流、快捷操作 |
| Config | `/config` | 完成，schema 驱动表单 |
| Subagents | `/subagents` | 完成，层级树 + 详情面板 |
| Memory | `/memory` | 占位，预留结构 |
| Tools | `/tools` | 占位，预留结构 |
| Logs | `/logs` | 占位，预留结构 |
| Templates | `/templates` | 完成，角色模板展示 |

### Config 页核心交互

- **Schema 驱动渲染**：9 种字段类型（string, number, boolean, choice, string_list, path, secret, json, text）
- **实时搜索**：按 key / label / description 过滤
- **分类导航**：左侧 sidebar，点击切换
- **高级模式**：toggle 显示 advanced 字段
- **Diff 对比**：展示修改项 vs 默认值
- **YAML 导出**：纯前端生成，不写入磁盘
- **保存流程**：高风险字段检测 → ConfirmDialog → Toast 反馈
- **字段反馈**：修改指示器（蓝点）、重置按钮、风险标签、需重启标签

## Mock API 层设计

文件：`frontend/src/api/mockApi.ts`

设计原则：
1. **与 UI 解耦**：所有 mock 数据操作集中在 API 层，UI 组件通过 import 调用
2. **异步接口**：全部返回 `Promise`，带模拟延迟
3. **类型安全**：所有接口均有 TypeScript 返回类型
4. **可替换**：后续接入真实 API 时，只需替换函数内部实现，保持签名不变

## 前端配置集中化

- 前端默认值、工具目录和角色模板统一放在 `frontend/config/frontend-runtime-config.json`。
- `frontend/src/data/runtimeConfig.ts` 只做类型包装和导出，不再保存业务默认值。
- 页面、store、mock API 不应该再各自写死工具上限、角色模板、工具列表等参数。
- 后续如果配置文件变大，只在 `frontend/config/` 下按模块拆分，例如 `tools.json`、`subagents.json`、`memory.json`，不要拆散到页面目录。
- 当前 `tool_write_inline_max_chars` 前后端统一为 `12000`，`tool_read_max_chars` 统一为 `50000`，`tool_web_max_chars` 统一为 `100000`。
- 工具目录相关配置也从 `frontend/config/frontend-runtime-config.json` 读取，包括 `tool_catalog_mode`、`tool_catalog_offset`、`tool_catalog_categories`、`tool_catalog_include_examples`、`tool_catalog_entry_max_chars`、`tool_catalog_show_truncated_notice` 和 `tool_detail_max_chars`。
- 后端真实配置目录由 `frontend/scripts/sync-backend-config.mjs` 从 `agent_py_agent/config/*.yaml` 生成到 `frontend/config/backend-config-catalog.json`。前端配置页读取这个生成文件，避免 UI 和后端默认值各写一份。
- 以后改后端配置字段时，前端验收必须跑 `npm run check:config`，确认生成目录没有漏同步。

## 子代理参数保护

- Config schema 里的 `requiresUnlock` 字段会让前端字段默认灰色禁用，用户点击「启用修改」后才可以改。
- `SettingsSubagents` 页面也对角色模板、自动拆分、验收和 workflow 等高级项做保护编辑；普通看板展示类参数仍可直接看。
- 这个保护只负责避免误触，不替代后端权限和安全校验。

## Playwright 默认能力

- 前端依赖加入 `playwright`，用于本地页面 smoke / E2E 验证。
- 后端能力路由增加内置 Playwright 能力卡：模型搜索“浏览器 / E2E / 前端 / 截图 / Playwright”时，会映射到已有 `controlled_exec` 受控执行入口。
- 当前不是新增裸 shell 权限；子代理仍需要父级授予 command/path scope 后才能真实执行 Playwright 命令。

## 已知问题与处理

| 问题 | 处理方案 |
|------|----------|
| 中文引号与字符串引号冲突 | 替换为中文直角引号 `「」` |
| lucide-react 不存在 `GitDiff` 图标 | 改为 `GitCompare` |
| `choiceLabels` 类型不匹配 | 每个对象后加 `as Record<string, string>` |
| strict mode 下大量 implicit any | `tsconfig.app.json` 中设 `noImplicitAny: false` |
| secret 字段明文显示 | 使用 `type="password"` + 显示/隐藏切换按钮 |
| Vite 默认端口和文档不一致 | 统一使用 `http://localhost:3000` |
| 前端混入 Python strict code-size | `frontend/` 明确排除 Python 规模守卫，改用 `check:config/lint/build` |

## 下一阶段建议

1. **接入真实 API**：替换 `mockApi.ts` 中的实现为 `fetch` 调用
2. **配置持久化**：当前保存仅为 mock toast，需对接后端写入 `agent_config.yaml`
3. **Dashboard 数据实时化**：使用轮询或 SSE 获取系统状态
4. **Memory / Tools / Logs 页**：从占位页扩展为功能完整页面
5. **表单校验增强**：在 `ConfigFieldRenderer` 中接入 `validateConfig` 的实时校验
6. **响应式适配**：当前为桌面优先，后续补充移动端布局
