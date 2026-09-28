// LLM: Mock schema and values are derived from the generated backend-config-catalog.json only; never hand-edit fields here.
//   Restored 2026-09-28 together with runtimeConfig.ts; mockApi imports it.
// 模块用途: 给 mock API 提供与后端随包 YAML 同步的配置目录和当前值。
import backendConfigCatalog from "../../config/backend-config-catalog.json";
import type { ConfigSchema } from "../types/config";

export const mockConfigSchema: ConfigSchema =
  backendConfigCatalog.schema as ConfigSchema;

export const mockCurrentValues: Record<string, unknown> = {};
for (const cat of mockConfigSchema.categories) {
  for (const field of cat.fields) {
    mockCurrentValues[field.key] = field.currentValue ?? field.defaultValue;
  }
}
