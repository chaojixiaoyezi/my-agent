import { create } from "zustand";
import type { ConfigField, ConfigSchema } from "../types/config";
import { getConfigSchema, getCurrentConfig } from "../api/mockApi";
import { exportYaml } from "../lib/export";

export type ConfigValues = Record<string, unknown>;

type ConfigState = {
  schema: ConfigSchema;
  draftValues: ConfigValues;
  flatFields: Record<string, ConfigField>;
  isLoading: boolean;
  loadError: string | null;
  showAdvanced: boolean;
  searchQuery: string;
  activeCategory: string;
  selectedField: string | null;
  showDiff: boolean;
  showYaml: boolean;
  confirmDialog: {
    open: boolean;
    fieldKey: string | null;
    fieldLabel: string;
    riskWarning: string;
  };
  toast: {
    message: string;
    type: "success" | "error" | "info";
    visible: boolean;
  } | null;
  init: () => Promise<void>;
  setDraftValue: (key: string, value: unknown) => void;
  resetField: (key: string) => void;
  resetAll: () => void;
  toggleAdvanced: () => void;
  setSearchQuery: (q: string) => void;
  setActiveCategory: (cat: string) => void;
  setSelectedField: (key: string | null) => void;
  toggleDiff: () => void;
  toggleYaml: () => void;
  openConfirm: (key: string, label: string, warning: string) => void;
  closeConfirm: () => void;
  confirmSave: () => void;
  showToast: (message: string, type: "success" | "error" | "info") => void;
  hideToast: () => void;
  getModifiedCount: () => number;
  getModifiedKeys: () => string[];
  getYamlExport: () => string;
};

const emptySchema: ConfigSchema = {
  version: "",
  categories: [],
  meta: {
    lastUpdated: "",
    source: "",
    editable: true,
    exportFormats: ["yaml"],
  },
};

function buildConfigFlat(schema: ConfigSchema): Record<string, ConfigField> {
  const flat: Record<string, ConfigField> = {};
  for (const cat of schema.categories) {
    for (const f of cat.fields) {
      flat[f.key] = f;
    }
  }
  return flat;
}

function isFieldModified(field: ConfigField, value: unknown): boolean {
  // Fast path: strict equality for primitives
  if (value === field.defaultValue) return false;
  // For arrays/objects, use deep comparison
  if (typeof value !== typeof field.defaultValue) return true;
  if (typeof value === "object" && value !== null) {
    return JSON.stringify(value) !== JSON.stringify(field.defaultValue);
  }
  return true;
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  schema: emptySchema,
  draftValues: {},
  flatFields: {},
  isLoading: false,
  loadError: null,
  showAdvanced: false,
  searchQuery: "",
  activeCategory: "basic",
  selectedField: null,
  showDiff: false,
  showYaml: false,
  confirmDialog: { open: false, fieldKey: null, fieldLabel: "", riskWarning: "" },
  toast: null,

  init: async () => {
    set({ isLoading: true, loadError: null });
    try {
      const schema = await getConfigSchema();
      const current = await getCurrentConfig();
      set({
        schema,
        draftValues: { ...current },
        flatFields: buildConfigFlat(schema),
        isLoading: false,
        activeCategory: schema.categories[0]?.key || "basic",
      });
    } catch (err) {
      set({
        isLoading: false,
        loadError: err instanceof Error ? err.message : "加载配置失败",
      });
    }
  },

  setDraftValue: (key, value) =>
    set((s) => ({ draftValues: { ...s.draftValues, [key]: value } })),

  resetField: (key) =>
    set((s) => {
      const next = { ...s.draftValues };
      const field = s.flatFields[key];
      if (field) {
        next[key] = field.defaultValue;
      }
      return { draftValues: next };
    }),

  resetAll: () =>
    set((s) => {
      const next: ConfigValues = {};
      for (const cat of s.schema.categories) {
        for (const f of cat.fields) {
          next[f.key] = f.defaultValue;
        }
      }
      return { draftValues: next };
    }),

  toggleAdvanced: () => set((s) => ({ showAdvanced: !s.showAdvanced })),

  setSearchQuery: (q) => set({ searchQuery: q }),

  setActiveCategory: (cat) => set({ activeCategory: cat }),

  setSelectedField: (key) => set({ selectedField: key }),

  toggleDiff: () => set((s) => ({ showDiff: !s.showDiff })),

  toggleYaml: () => set((s) => ({ showYaml: !s.showYaml })),

  openConfirm: (key, label, warning) =>
    set({ confirmDialog: { open: true, fieldKey: key, fieldLabel: label, riskWarning: warning } }),

  closeConfirm: () =>
    set({ confirmDialog: { open: false, fieldKey: null, fieldLabel: "", riskWarning: "" } }),

  confirmSave: () => {
    set(() => ({
      confirmDialog: { open: false, fieldKey: null, fieldLabel: "", riskWarning: "" },
      toast: { message: "配置已保存（mock）", type: "success", visible: true },
    }));
    setTimeout(() => get().hideToast(), 3000);
  },

  showToast: (message, type) => set({ toast: { message, type, visible: true } }),

  hideToast: () => set({ toast: null }),

  getModifiedCount: () => {
    const { draftValues, schema } = get();
    let count = 0;
    for (const cat of schema.categories) {
      for (const f of cat.fields) {
        if (isFieldModified(f, draftValues[f.key])) {
          count++;
        }
      }
    }
    return count;
  },

  getModifiedKeys: () => {
    const { draftValues, schema } = get();
    const keys: string[] = [];
    for (const cat of schema.categories) {
      for (const f of cat.fields) {
        if (isFieldModified(f, draftValues[f.key])) {
          keys.push(f.key);
        }
      }
    }
    return keys;
  },

  getYamlExport: () => {
    const { draftValues, schema } = get();
    return exportYaml(schema, draftValues);
  },
}));
