/**
 * 主题（2026-08-17 完整版：/theme dark|light 真换肤，不再硬编码）。
 * dark = 默认终端深色；light = 浅色背景适配。
 */

export interface ThemeColors {
  user: string;
  assistant: string;
  toolStart: string;
  toolDone: string;
  toolFail: string;
  toolDetail: string;
  status: string;
  dim: string;
  prompt: string;
  heading: string;
  code: string;
  link: string;
  border: string;
}

export interface Theme {
  name: "dark" | "light";
  colors: ThemeColors;
}

export const DARK_THEME: Theme = {
  name: "dark",
  colors: {
    user: "#3b82f6",
    assistant: "#e5e7eb",
    toolStart: "#06b6d4",
    toolDone: "#22c55e",
    toolFail: "#ef4444",
    toolDetail: "#6b7280",
    status: "#9ca3af",
    dim: "#6b7280",
    prompt: "#22c55e",
    heading: "#06b6d4",
    code: "#f59e0b",
    link: "#60a5fa",
    border: "#374151",
  },
};

export const LIGHT_THEME: Theme = {
  name: "light",
  colors: {
    user: "#1d4ed8",
    assistant: "#1f2937",
    toolStart: "#0e7490",
    toolDone: "#15803d",
    toolFail: "#b91c1c",
    toolDetail: "#6b7280",
    status: "#6b7280",
    dim: "#9ca3af",
    prompt: "#15803d",
    heading: "#0e7490",
    code: "#b45309",
    link: "#2563eb",
    border: "#d1d5db",
  },
};

export function themeByName(name: "dark" | "light"): Theme {
  return name === "light" ? LIGHT_THEME : DARK_THEME;
}
