import backendConfigCatalog from "../../config/backend-config-catalog.json";
import type { ConfigSchema, ConfigField } from "../types/config";

export const mockConfigSchema: ConfigSchema =
  backendConfigCatalog.schema as ConfigSchema;

export const mockConfigFlat: Record<string, ConfigField> = {};
for (const cat of mockConfigSchema.categories) {
  for (const field of cat.fields) {
    mockConfigFlat[field.key] = field;
  }
}

export const mockCurrentValues: Record<string, unknown> = {};
for (const cat of mockConfigSchema.categories) {
  for (const field of cat.fields) {
    mockCurrentValues[field.key] = field.currentValue ?? field.defaultValue;
  }
}
