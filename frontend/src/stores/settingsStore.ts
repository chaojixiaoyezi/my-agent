import { create } from "zustand";
import {
  frontendRuntimeConfig,
  runtimeRoleTemplates,
} from "../data/runtimeConfig";

// ---------------------------------------------------------------------------
// Settings State Types
// ---------------------------------------------------------------------------
export type RoleTemplate = { name: string; label: string; max_rounds: number };

export type SettingsState = {
  // Role Templates
  roleTemplates: RoleTemplate[];
  setRoleRounds: (name: string, rounds: number) => void;

  // Model Params
  modelParams: {
    temperature: number;
    top_p: number;
    max_tokens: number;
    api_key_env: string;
    model_speed_profile_path: string;
  };
  setModelParams: (params: Partial<SettingsState["modelParams"]>) => void;

  // Tool Budget
  toolBudget: {
    max_calls: number;
    artifact_read_max_chars: number;
  };
  setToolBudget: (params: Partial<SettingsState["toolBudget"]>) => void;

  // Tool Limits
  toolLimits: {
    read_max_chars: number;
    list_max_entries: number;
    http_timeout: number;
    shell_timeout: number;
  };
  setToolLimits: (params: Partial<SettingsState["toolLimits"]>) => void;

  // Advanced Tool Params
  toolAdvanced: {
    search_max_matches: number;
    web_max_chars: number;
    catalog_mode: string;
    catalog_categories: string[];
    catalog_include_examples: boolean;
    retrieval_limit: number;
    vector_search_enabled: boolean;
  };
  setToolAdvanced: (params: Partial<SettingsState["toolAdvanced"]>) => void;

  // Advanced Memory Params
  memoryAdvanced: {
    top_k: number;
    archive_level: string;
    hook_enabled: boolean;
    rule_routing_mode: string;
    rule_auto_read_limit: number;
    resume_auto_context_mode: string;
    compact_auto_trigger_percent: number;
  };
  setMemoryAdvanced: (params: Partial<SettingsState["memoryAdvanced"]>) => void;

  // Gateway Params
  gatewayParams: {
    port: number;
    heartbeat_interval: number;
    stale_seconds: number;
    stop_timeout: number;
    processing_timeout_seconds: number;
    request_max_attempts: number;
    lease_heartbeat_interval_seconds: number;
    lease_stale_without_heartbeat_seconds: number;
  };
  setGatewayParams: (params: Partial<SettingsState["gatewayParams"]>) => void;

  // Daemon Params
  daemonParams: {
    runner_instruction: string;
  };
  setDaemonParams: (params: Partial<SettingsState["daemonParams"]>) => void;

  // Runner / Scheduler
  runnerParams: {
    concurrency: number;
    start_rate: number;
    timeout_seconds: string;
    dynamic_timeout_min: number;
    dynamic_timeout_max: number;
  };
  setRunnerParams: (params: Partial<SettingsState["runnerParams"]>) => void;

  // Log Params
  logParams: {
    level: string;
  };
  setLogParams: (params: Partial<SettingsState["logParams"]>) => void;

  workflowParams: {
    enable_self_learning: boolean;
  };
  setWorkflowParams: (params: Partial<SettingsState["workflowParams"]>) => void;

  // Subagent Params
  subagentParams: {
    task_max_subagents: number;
  };
  setSubagentParams: (params: Partial<SettingsState["subagentParams"]>) => void;

  // Local Store
  localStoreParams: {
    store_path: string;
    files_dir: string;
    events_path: string;
    fts_enabled: boolean;
  };
  setLocalStoreParams: (params: Partial<SettingsState["localStoreParams"]>) => void;

  // Audit
  auditParams: {
    log_path: string;
    enabled: boolean;
  };
  setAuditParams: (params: Partial<SettingsState["auditParams"]>) => void;

  // Session
  sessionParams: {
    workspace: string;
  };
  setSessionParams: (params: Partial<SettingsState["sessionParams"]>) => void;

  // Adapters
  adapterParams: {
    workspace: string;
    feishu_app_id: string;
    feishu_app_secret: string;
  };
  setAdapterParams: (params: Partial<SettingsState["adapterParams"]>) => void;

  // Save status
  saving: boolean;
  errors: Record<string, string>;
  setSaving: (v: boolean) => void;
  setErrors: (errors: Record<string, string>) => void;
};

// ---------------------------------------------------------------------------
// Initial values
// ---------------------------------------------------------------------------
const initialRoleTemplates: RoleTemplate[] = runtimeRoleTemplates.map((role) => ({
  name: role.name,
  label: role.label,
  max_rounds: role.max_rounds,
}));

export const useSettingsStore = create<SettingsState>((set) => ({
  roleTemplates: initialRoleTemplates,
  setRoleRounds: (name, rounds) =>
    set((s) => ({
      roleTemplates: s.roleTemplates.map((r) =>
        r.name === name ? { ...r, max_rounds: rounds } : r
      ),
    })),

  modelParams: { ...frontendRuntimeConfig.model },
  setModelParams: (params) =>
    set((s) => ({ modelParams: { ...s.modelParams, ...params } })),

  toolBudget: { ...frontendRuntimeConfig.tools.budget },
  setToolBudget: (params) =>
    set((s) => ({ toolBudget: { ...s.toolBudget, ...params } })),

  toolLimits: { ...frontendRuntimeConfig.tools.limits },
  setToolLimits: (params) =>
    set((s) => ({ toolLimits: { ...s.toolLimits, ...params } })),

  toolAdvanced: { ...frontendRuntimeConfig.tools.advanced },
  setToolAdvanced: (params) =>
    set((s) => ({ toolAdvanced: { ...s.toolAdvanced, ...params } })),

  memoryAdvanced: { ...frontendRuntimeConfig.memory.advanced },
  setMemoryAdvanced: (params) =>
    set((s) => ({ memoryAdvanced: { ...s.memoryAdvanced, ...params } })),

  gatewayParams: { ...frontendRuntimeConfig.gateway },
  setGatewayParams: (params) =>
    set((s) => ({ gatewayParams: { ...s.gatewayParams, ...params } })),

  daemonParams: { ...frontendRuntimeConfig.daemon },
  setDaemonParams: (params) =>
    set((s) => ({ daemonParams: { ...s.daemonParams, ...params } })),

  runnerParams: { ...frontendRuntimeConfig.runner },
  setRunnerParams: (params) =>
    set((s) => ({ runnerParams: { ...s.runnerParams, ...params } })),

  logParams: { ...frontendRuntimeConfig.log },
  setLogParams: (params) =>
    set((s) => ({ logParams: { ...s.logParams, ...params } })),

  workflowParams: { ...frontendRuntimeConfig.workflow },
  setWorkflowParams: (params) =>
    set((s) => ({ workflowParams: { ...s.workflowParams, ...params } })),

  subagentParams: { ...frontendRuntimeConfig.subagent },
  setSubagentParams: (params) =>
    set((s) => ({ subagentParams: { ...s.subagentParams, ...params } })),

  localStoreParams: { ...frontendRuntimeConfig.localStore },
  setLocalStoreParams: (params) =>
    set((s) => ({ localStoreParams: { ...s.localStoreParams, ...params } })),

  auditParams: { ...frontendRuntimeConfig.audit },
  setAuditParams: (params) =>
    set((s) => ({ auditParams: { ...s.auditParams, ...params } })),

  sessionParams: { ...frontendRuntimeConfig.session },
  setSessionParams: (params) =>
    set((s) => ({ sessionParams: { ...s.sessionParams, ...params } })),

  adapterParams: { ...frontendRuntimeConfig.adapters },
  setAdapterParams: (params) =>
    set((s) => ({ adapterParams: { ...s.adapterParams, ...params } })),

  saving: false,
  errors: {},
  setSaving: (v) => set({ saving: v }),
  setErrors: (errors) => set({ errors }),
}));
