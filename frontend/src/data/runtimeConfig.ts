// LLM: This file is only a typed loader; edit frontend/config/*.json for runtime values.
// 模块用途: 从集中配置文件读取前端默认值、工具目录和角色模板，避免页面或 store 写死参数。
import runtimeConfig from "../../config/frontend-runtime-config.json";

export type RuntimeRoleTemplate = {
  name: string;
  label: string;
  description: string;
  max_rounds: number;
  allowed_tools: string[];
  builtin: boolean;
};

export type RuntimeTool = {
  name: string;
  desc: string;
  enabled: boolean;
  calls: number;
};

type FrontendRuntimeConfigFile = {
  defaults: {
    model: {
      temperature: number;
      top_p: number;
      max_tokens: number;
      timeout: number;
      anthropic_version: string;
      api_key_env: string;
      model_speed_profile_path: string;
      auto_bench_model_on_first_use: boolean;
    };
    tools: {
      budget: {
        window_seconds: number;
        max_calls: number;
        artifact_read_window_seconds: number;
        artifact_read_max_chars: number;
      };
      limits: {
        write_inline_max_chars: number;
        read_max_chars: number;
        list_max_entries: number;
        http_timeout: number;
        shell_timeout: number;
      };
      advanced: {
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
    };
    memory: {
      limit: number;
      retention_days: number;
      enable_archive: boolean;
      advanced: {
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
    };
    gateway: {
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
    daemon: {
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
    runner: {
      scheduler_mode: string;
      concurrency: number;
      start_rate: number;
      timeout_seconds: string;
      dynamic_timeout_min: number;
      dynamic_timeout_max: number;
      dynamic_timeout_safety_margin: number;
      failure_policy: string;
    };
    log: { level: string; max_lines: number };
    acceptance: { reviewer_mode: string; auto_accept: boolean };
    workflow: { mode: string; enable_self_learning: boolean };
    subagent: {
      board_limit: number;
      builtin_workflows: boolean;
      user_workflow_dirs: string[];
      workflow_review_rounds: number;
      task_max_subagents: number;
      task_max_grandchildren: number;
      max_auto_split_depth: number;
      max_auto_retry_attempts: number;
    };
    security: {
      max_input_length: number;
      max_path_length: number;
      forbid_dangerous_chars: boolean;
      path_whitelist_only: boolean;
    };
    localStore: {
      store_path: string;
      files_dir: string;
      events_path: string;
      fts_enabled: boolean;
    };
    notification: { store_path: string; channel_timeout_seconds: number };
    audit: { log_path: string; enabled: boolean };
    session: { workspace: string };
    watchdog: {
      enabled: boolean;
      interval: number;
      max_restarts: number;
      restart_delay: number;
    };
    adapters: {
      workspace: string;
      feishu_app_id: string;
      feishu_app_secret: string;
      feishu_webhook_url: string;
      qq_bot_uin: string;
      qq_http_api_url: string;
      qq_group_whitelist: string;
      qq_admin_qq: string;
    };
  };
  tools: RuntimeTool[];
  role_templates: RuntimeRoleTemplate[];
};

const typedRuntimeConfig = runtimeConfig as FrontendRuntimeConfigFile;

export const frontendRuntimeConfig = typedRuntimeConfig.defaults;
export const runtimeTools = typedRuntimeConfig.tools;
export const runtimeRoleTemplates = typedRuntimeConfig.role_templates;
export const runtimeRoleToolPermissions = runtimeRoleTemplates.map((role) => ({
  role: role.name,
  tools: role.allowed_tools,
}));
