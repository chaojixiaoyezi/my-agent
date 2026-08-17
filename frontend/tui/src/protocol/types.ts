/**
 * my-agent gateway HTTP client contract（P0 冻结版，2026-08-17）。
 *
 * 事实来源：agent/gateway_parts/http_handlers.py 实核 + testbox 真实请求
 * replay fixtures（test/fixtures/）。
 *
 * 端点：
 *   POST /ask                    → 202 {request_id, ...}
 *   GET  /result/<request_id>    → 终态结果 JSON / processing / queued / 404
 *   GET  /progress/<request_id>?since=N
 *                                → 200 {request_id, events[], next}（≤200 行/页，
 *                                  owner 鉴权，只投影 assistant_commentary +
 *                                  verbose on/full 的 tool_progress）
 *   POST /control                → 结构化控制（stop/btw/goal，带 conversation scope）
 *
 * 边界（群复核 2525/2528 冻结）：
 *   - /progress 不承诺 token 级文本流；最终答案以 /result 为权威
 *   - /stop 是守护进程管理命令，TUI 会话控制一律走 /control
 *   - 仅回环本机（P0）；每次调用沿认证 owner/channel/conversation 约束
 */

/** POST /ask 请求体 */
export interface AskRequest {
  prompt: string;
  inject?: string[];
  prompt_files?: string[];
  save?: boolean;
  show_prompt?: boolean;
  resume_context?: boolean;
  chat_session_id?: string;
  system_task?: Record<string, unknown>;
  /** 幂等键（重连防重复提交，P1 使用） */
  idempotency_key?: string;
}

/** POST /ask 响应（202 Accepted） */
export interface AskAccepted {
  request_id: string;
  status: "accepted";
}

/** progress 事件（/progress 投影面，2026-08-17 真机实核结构：
 *  assistant_commentary 带 text；tool_progress 是结构化字段
 *  level/round/call_index/tool/phase/status/detail——不是 {kind,text}） */
export type ProgressEvent =
  | { kind: "assistant_commentary"; text: string }
  | {
      kind: "tool_progress";
      level?: string;
      round?: number;
      call_index?: number;
      tool?: string;
      phase?: string;
      status?: string;
      detail?: string;
    };

/** GET /progress 响应 */
export interface ProgressResponse {
  request_id: string;
  events: ProgressEvent[];
  /** 下次轮询游标 */
  next: number;
}

/** GET /result 的请求状态（未完成时） */
export type PendingState = "processing" | "queued";

/** GET /result 响应：终态结果（字段以 testbox 真实 response JSON 为准） */
export interface ResultResponse {
  request_id?: string;
  ok?: boolean;
  status?: string;
  error?: string;
  error_code?: string;
  response?: string;
  /** 结构化运行终态（未完成时为 undefined） */
  runtime_status?: string;
  runtime_reason?: string;
  [key: string]: unknown;
}

/** 控制命令类型（POST /control，会话控制专用） */
export type ControlCommand = "stop" | "btw" | "goal";

/** POST /control 请求体（command 为斜杠语法文本：/stop、/btw 内容、/goal 内容） */
export interface ControlRequest {
  command: string;
  /** conversation scope（必带，防误控其他会话） */
  conversation: {
    channel: string;
    channel_conversation_id: string;
    channel_user_id: string;
    canonical_user_id: string;
  };
  text?: string; // btw/goal 的内容
  [key: string]: unknown;
}

/** HTTP 客户端错误（结构化，UI 按 status/error_code 渲染，不匹配文本） */
export interface ClientError {
  kind: "http" | "network" | "timeout" | "protocol";
  status?: number;
  error_code?: string;
  message: string;
}
