# my-agent 前端管理界面

独立前端管理界面，用于可视化配置、监控和管理 my-agent Python CLI 智能体。

## 技术栈

- **React 18** + **TypeScript**
- **Vite** 构建工具
- **Tailwind CSS** 样式
- **React Router v6** 路由
- **Zustand** 状态管理
- **Lucide React** 图标

## 快速开始

```bash
cd frontend
npm install
npm run dev
```

打开 http://localhost:3000

## 脚本

| 命令 | 说明 |
|------|------|
| `npm run dev` | 启动开发服务器 |
| `npm run sync:config` | 从后端 YAML 生成前端配置目录 |
| `npm run check:config` | 检查前端配置目录是否和后端 YAML 同步 |
| `npm run build` | TypeScript 检查 + 生产构建 |
| `npm run preview` | 预览生产构建 |
| `npm run lint` | ESLint 检查 |

## Mock API 说明

当前所有数据均为 mock，API 层位于 `src/api/mockApi.ts`。

已提供的 mock 接口：

| 接口 | 说明 |
|------|------|
| `getConfigSchema()` | 获取配置 schema |
| `getCurrentConfig()` | 获取当前配置值 |
| `validateConfig(values)` | 验证配置合法性 |
| `exportYaml(values)` | 导出 YAML 格式配置 |
| `saveConfig(values)` | 保存配置（mock） |
| `getStatus()` | 获取系统状态 |
| `restartGateway()` | 重启 Gateway |
| `runSmokeTest()` | 运行 Smoke Test |
| `getSubagentRuns()` | 获取子代理运行树 |
| `getMemoryStats()` | 获取记忆统计 |
| `getRecentLogs()` | 获取最近日志 |

所有 mock 接口返回 `Promise`，带有 150-1200ms 的模拟延迟，方便测试 loading 状态。

## 后续接入真实 API 指南

1. **替换 mockApi.ts**：保持相同函数签名，将内部实现改为 `fetch('/api/...')` 调用
2. **环境变量**：在 `.env` 中配置 `VITE_API_BASE_URL`
3. **CORS**：确保后端允许前端域名跨域访问
4. **认证**：如需要，在请求头中注入 token

示例替换：

```typescript
// src/api/mockApi.ts → src/api/api.ts
export async function getConfigSchema(): Promise<ConfigSchema> {
  const res = await fetch(`${API_BASE}/config/schema`);
  if (!res.ok) throw new Error("Failed to fetch schema");
  return res.json();
}
```

## 项目结构

```
frontend/
  src/
    api/              API 层（mock / 真实）
    components/
      config/         配置页专用组件
      ui/             通用 UI 组件
    data/             Mock 数据
    pages/            页面组件
    stores/           Zustand 状态管理
    types/            TypeScript 类型定义
```
