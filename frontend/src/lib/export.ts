import type { ConfigSchema } from "../types/config";
import type { ConfigValues } from "../stores/configStore";

export function exportYaml(schema: ConfigSchema, draftValues: ConfigValues): string {
  const lines: string[] = ["# my-agent 配置导出", "---", ""];
  for (const cat of schema.categories) {
    lines.push(`# ${cat.label}`);
    for (const f of cat.fields) {
      const v = draftValues[f.key];
      if (v === null || v === undefined) {
        lines.push(`${f.key}: null`);
      } else if (typeof v === "boolean") {
        lines.push(`${f.key}: ${v}`);
      } else if (typeof v === "number") {
        lines.push(`${f.key}: ${v}`);
      } else if (typeof v === "string") {
        if (v.includes("\n") || v.includes(":") || v.includes("#")) {
          lines.push(`${f.key}: |`);
          for (const line of v.split("\n")) {
            lines.push(`  ${line}`);
          }
        } else {
          lines.push(`${f.key}: "${v}"`);
        }
      } else if (Array.isArray(v)) {
        if (v.length === 0) {
          lines.push(`${f.key}: []`);
        } else {
          lines.push(`${f.key}:`);
          for (const item of v) {
            if (typeof item === "string") {
              lines.push(`  - "${item}"`);
            } else {
              lines.push(`  - ${item}`);
            }
          }
        }
      }
    }
    lines.push("");
  }
  return lines.join("\n");
}
