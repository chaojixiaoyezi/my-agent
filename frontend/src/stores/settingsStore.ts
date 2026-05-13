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

  // Dispatch Loop
  dispatchParams: {
    max_consecutive_rounds: number;
    max_runners: number;
    limit: number;
    active_interval: number;
    idle_interval: number;
  };
  setDispatchParams: (params: Partial<SettingsState["dispatchParams"]>) => void;

  // Model Params
  modelParams: {
    temperature: number;
    top_p: number;
    max_tokens: number;
    timeout: number;
    anthropic_version: string;
    api_key_env: string;
    model_speed_profile_path: string;
    auto_bench_model_on_first_use: boolean;
  };
  setModelParams: (params: Partial<SettingsState["modelParams"]>) => void;

  // Tool Budget
  toolBudget: {
    window_seconds: number;
    max_calls: number;
    artifact_read_window_seconds: number;
    artifact_read_max_chars: number;
  };
  setToolBudget: (params: Partial<SettingsState["toolBudget"]>) => void;

  // Tool Limits
  toolLimits: {
    write_inline_max_chars: number;
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
    catalog_limit: number;
    catalog_mode: string;
    catalog_offset: number;
    catalog_categories: string[];
    catalog_include_examples: boolean;
    catalog_entry_max_chars: number;
    catalog_show_truncated_notice: boolean;
    tool_detail_max_chars: number;
    retrieval_limit: number;
    vector_search_enabled: boolean;
  };
  setToolAdvanced: (params: Partial<SettingsState["toolAdvanced"]>) => void;

  // Memory Params
  memoryParams: {
    limit: number;
    retention_days: number;
    enable_archive: boolean;
  };
  setMemoryParams: (params: Partial<SettingsState["memoryParams"]>) => void;

  // Advanced Memory Params
  memoryAdvanced: {
    top_k: number;
    archive_level: string;
    hook_enabled: boolean;
    hook_archive_level: string;
    hook_retention_days: number;
    rule_routing_enabled: boolean;
    rule_routing_mode: string;
    rule_auto_read_limit: number;
    rule_receipt_enabled: boolean;
    resume_auto_context_enabled: boolean;
    resume_auto_context_mode: string;
    resume_auto_context_limit: number;
    compact_auto_allow_apply: boolean;
  };
  setMemoryAdvanced: (params: Partial<SettingsState["memoryAdvanced"]>) => void;

  // Gateway Params
  gatewayParams: {
    port: number;
    heartbeat_interval: number;
    enable_watchdog: boolean;
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
    planner: boolean;
    apply: boolean;
    execute_runners: boolean;
    interval: number;
    max_runners: number;
    limit: number;
    max_cycles: number;
    max_cards: number;
    probe: boolean;
    reviewer: boolean;
    runner_instruction: string;
  };
  setDaemonParams: (params: Partial<SettingsState["daemonParams"]>) => void;

  // Runner / Scheduler
  runnerParams: {
    scheduler_mode: string;
    concurrency: number;
    start_rate: number;
    timeout_seconds: number;
    failure_policy: string;
  };
  setRunnerParams: (params: Partial<SettingsState["runnerParams"]>) => void;

  // Log Params
  logParams: {
    level: string;
    max_lines: number;
  };
  setLogParams: (params: Partial<SettingsState["logParams"]>) => void;

  // Acceptance & Workflow
  acceptanceParams: {
    reviewer_mode: string;
    auto_accept: boolean;
  };
  setAcceptanceParams: (params: Partial<SettingsState["acceptanceParams"]>) => void;

  workflowParams: {
    mode: string;
    enable_self_learning: boolean;
  };
  setWorkflowParams: (params: Partial<SettingsState["workflowParams"]>) => void;

  // Subagent Params
  subagentParams: {
    board_limit: number;
    builtin_workflows: boolean;
    user_workflow_dirs: string[];
    workflow_review_rounds: number;
    task_max_subagents: number;
    task_max_grandchildren: number;
    max_auto_split_depth: number;
    max_auto_retry_attempts: number;
  };
  setSubagentParams: (params: Partial<SettingsState["subagentParams"]>) => void;

  // Security
  securityParams: {
    max_input_length: number;
    max_path_length: number;
    forbid_dangerous_chars: boolean;
    path_whitelist_only: boolean;
  };
  setSecurityParams: (params: Partial<SettingsState["securityParams"]>) => void;

  // Local Store
  localStoreParams: {
    store_path: string;
    files_dir: string;
    events_path: string;
    fts_enabled: boolean;
  };
  setLocalStoreParams: (params: Partial<SettingsState["localStoreParams"]>) => void;

  // Notifications
  notificationParams: {
    store_path: string;
    channel_timeout_seconds: number;
  };
  setNotificationParams: (params: Partial<SettingsState["notificationParams"]>) => void;

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

  // Watchdog
  watchdogParams: {
    enabled: boolean;
    interval: number;
    max_restarts: number;
    restart_delay: number;
  };
  setWatchdogParams: (params: Partial<SettingsState["watchdogParams"]>) => void;

  // Adapters
  adapterParams: {
    workspace: string;
    feishu_app_id: string;
    feishu_app_secret: string;
    feishu_webhook_url: string;
    qq_bot_uin: string;
    qq_http_api_url: string;
    qq_group_whitelist: string;
    qq_admin_qq: string;
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

  dispatchParams: {
    max_consecutive_rounds: 20,
    max_runners: 1,
    limit: 20,
    active_interval: 1,
    idle_interval: 5,
  },
  setDispatchParams: (params) =>
    set((s) => ({ dispatchParams: { ...s.dispatchParams, ...params } })),

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

  memoryParams: {
    limit: frontendRuntimeConfig.memory.limit,
    retention_days: frontendRuntimeConfig.memory.retention_days,
    enable_archive: frontendRuntimeConfig.memory.enable_archive,
  },
  setMemoryParams: (params) =>
    set((s) => ({ memoryParams: { ...s.memoryParams, ...params } })),

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

  acceptanceParams: { ...frontendRuntimeConfig.acceptance },
  setAcceptanceParams: (params) =>
    set((s) => ({ acceptanceParams: { ...s.acceptanceParams, ...params } })),

  workflowParams: { ...frontendRuntimeConfig.workflow },
  setWorkflowParams: (params) =>
    set((s) => ({ workflowParams: { ...s.workflowParams, ...params } })),

  subagentParams: { ...frontendRuntimeConfig.subagent },
  setSubagentParams: (params) =>
    set((s) => ({ subagentParams: { ...s.subagentParams, ...params } })),

  securityParams: { ...frontendRuntimeConfig.security },
  setSecurityParams: (params) =>
    set((s) => ({ securityParams: { ...s.securityParams, ...params } })),

  localStoreParams: { ...frontendRuntimeConfig.localStore },
  setLocalStoreParams: (params) =>
    set((s) => ({ localStoreParams: { ...s.localStoreParams, ...params } })),

  notificationParams: { ...frontendRuntimeConfig.notification },
  setNotificationParams: (params) =>
    set((s) => ({ notificationParams: { ...s.notificationParams, ...params } })),

  auditParams: { ...frontendRuntimeConfig.audit },
  setAuditParams: (params) =>
    set((s) => ({ auditParams: { ...s.auditParams, ...params } })),

  sessionParams: { ...frontendRuntimeConfig.session },
  setSessionParams: (params) =>
    set((s) => ({ sessionParams: { ...s.sessionParams, ...params } })),

  watchdogParams: { ...frontendRuntimeConfig.watchdog },
  setWatchdogParams: (params) =>
    set((s) => ({ watchdogParams: { ...s.watchdogParams, ...params } })),

  adapterParams: { ...frontendRuntimeConfig.adapters },
  setAdapterParams: (params) =>
    set((s) => ({ adapterParams: { ...s.adapterParams, ...params } })),

  saving: false,
  errors: {},
  setSaving: (v) => set({ saving: v }),
  setErrors: (errors) => set({ errors }),
}));
