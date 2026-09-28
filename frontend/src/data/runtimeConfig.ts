// LLM: This file is only a typed loader; edit frontend/config/frontend-runtime-config.json for runtime values.
//   Types come from the JSON itself instead of a hand-written field list, so they follow the file when
//   deleted backend keys are pruned from it. Restored 2026-09-28: lost with mockConfig.ts when the
//   independent repo was initialised (0b6252590), while settingsStore, Tools, Templates and mockApi kept importing it.
// 模块用途: 从集中配置文件读取前端默认值、工具目录和角色模板，避免页面或 store 写死参数。
import runtimeConfig from "../../config/frontend-runtime-config.json";

export const frontendRuntimeConfig = runtimeConfig.defaults;
export const runtimeTools = runtimeConfig.tools;
export const runtimeRoleTemplates = runtimeConfig.role_templates;
export const runtimeRoleToolPermissions = runtimeRoleTemplates.map((role) => ({
  role: role.name,
  tools: role.allowed_tools,
}));
