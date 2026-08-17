/**
 * 会话状态机（P1）：idle → submitting → polling → done / error。
 * 关键约束（群复核 2525/2528）：
 *   - 幂等键防重复提交（重连/双击不重复 POST /ask）
 *   - 完成判定唯一权威 = /result 终态（progress 观察流不判完成）
 *   - 同会话第二请求：当前轮 polling 中再提交 → 排队（不打断）
 */

import type { AskRequest, ProgressEvent, ResultResponse } from "../protocol/types.js";

export type SessionPhase = "idle" | "submitting" | "polling" | "done" | "error";

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  /** 工具观察行（tool_progress 投影） */
  toolLines?: string[];
}

export interface SessionState {
  phase: SessionPhase;
  messages: ChatMessage[];
  /** 当前 request_id（polling 中非空） */
  requestId: string;
  /** 排队中的下一提交（polling 中再提交 → 排队） */
  queuedPrompt: string | null;
  /** 幂等键（防重复提交） */
  lastIdempotencyKey: string;
  error: string;
}

export function createInitialSession(): SessionState {
  return {
    phase: "idle",
    messages: [],
    requestId: "",
    queuedPrompt: null,
    lastIdempotencyKey: "",
    error: "",
  };
}

/** 生成幂等键：时间戳 + 随机（窗口内唯一即可） */
export function makeIdempotencyKey(): string {
  return `tui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export interface SubmitAction {
  type: "submit";
  prompt: string;
  sessionId: string;
}

export interface ProgressAction {
  type: "progress";
  events: ProgressEvent[];
}

export interface DoneAction {
  type: "done";
  result: ResultResponse;
}

export interface ErrorAction {
  type: "error";
  message: string;
}

export interface QueueAction {
  type: "queue";
  prompt: string;
}

export type SessionAction = SubmitAction | ProgressAction | DoneAction | ErrorAction | QueueAction;

/**
 * 状态机 reducer：
 * - idle 提交 → submitting（生成幂等键）
 * - submitting/polling 再提交 → queuedPrompt（排队，不打断当前轮）
 * - polling 收 progress 事件 → 追加到当前助手消息 toolLines
 * - done → 追加助手最终文本，回到 idle（若有排队则立即再提交）
 * - error → 记录错误，回到 idle（排队可继续）
 */
export function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "submit": {
      if (state.phase === "polling" || state.phase === "submitting") {
        return { ...state, queuedPrompt: action.prompt };
      }
      const messages =
        state.phase === "done" || state.phase === "error"
          ? [...state.messages, { role: "user" as const, text: action.prompt }]
          : [...state.messages, { role: "user" as const, text: action.prompt }];
      return {
        ...state,
        phase: "submitting",
        messages,
        requestId: "",
        error: "",
        lastIdempotencyKey: makeIdempotencyKey(),
      };
    }
    case "progress": {
      if (state.phase !== "polling") return state;
      const events = action.events.map((e) => e.text);
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        // 观察流先于最终文本到达：补一条空的 assistant 消息承载工具行
        return {
          ...state,
          messages: [...state.messages, { role: "assistant", text: "", toolLines: events }],
        };
      }
      const messages = [
        ...state.messages.slice(0, -1),
        { ...last, toolLines: [...(last.toolLines ?? []), ...events] },
      ];
      return { ...state, messages };
    }
    case "done": {
      const text =
        action.result.response ??
        action.result.error ??
        (action.result.ok ? "(完成)" : "(无响应文本)");
      // 追加一条 assistant 消息（不动 user 消息）；已有 assistant 骨架则补全文
      const last = state.messages[state.messages.length - 1];
      const messages: ChatMessage[] =
        last && last.role === "assistant"
          ? [...state.messages.slice(0, -1), { ...last, text }]
          : [...state.messages, { role: "assistant", text }];
      // queuedPrompt 保留：UI 检测到非空立即再 submit（排队语义，不丢消息）
      return {
        ...state,
        phase: "idle",
        messages,
        requestId: "",
        error: "",
      };
    }
    case "queue": {
      return { ...state, queuedPrompt: action.prompt };
    }
    case "error": {
      return { ...state, phase: "idle", error: action.message, queuedPrompt: null };
    }
    default:
      return state;
  }
}

/** 提交动作的参数（供 UI 调用 client.ask） */
export function buildAskRequest(prompt: string, sessionId: string, idempotencyKey: string): AskRequest {
  return {
    prompt,
    chat_session_id: sessionId,
    idempotency_key: idempotencyKey,
    save: true,
  };
}
