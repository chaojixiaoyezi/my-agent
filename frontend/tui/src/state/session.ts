/**
 * 会话状态机（P1）：idle → submitting → polling → done / error。
 * 关键约束（群复核 2525/2528）：
 *   - 幂等键防重复提交（重连/双击不重复 POST /ask）
 *   - 完成判定唯一权威 = /result 终态（progress 观察流不判完成）
 *   - 同会话第二请求：当前轮 polling 中再提交 → 排队（不打断）
 */

import type { AskRequest, ProgressEvent, ResultResponse } from "../protocol/types.js";

export type SessionPhase = "idle" | "submitting" | "polling" | "done" | "error";

/** 工具观察行（结构化，一个工具一行：开始显示、完成同位置更新状态色，
 *  2026-08-17 用户指示参考 hermes 紧凑风格——不占两行） */
export interface ToolLine {
  key: string; // round#call_index（合并键）
  round?: number;
  callIndex?: number;
  tool: string;
  status: string; // 开始/完成/失败…
  detail: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  /** 观察行：工具行（按 key 合并更新）+ assistant_commentary 文本行 */
  toolLines?: (ToolLine | string)[];
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
      const messages = [...state.messages, { role: "user" as const, text: action.prompt }];
      return {
        ...state,
        // 2026-08-17 真机修复：phase 直接进 polling（ask 后立即轮询，
        // progress/done 都按 polling 放行；旧代码停在 submitting 导致
        // 工具行事件全被丢弃）
        phase: "polling",
        messages,
        requestId: "",
        error: "",
        lastIdempotencyKey: makeIdempotencyKey(),
      };
    }
    case "progress": {
      if (state.phase !== "polling") return state;
      const events = action.events.map(progressEventToLine);
      const last = state.messages[state.messages.length - 1];
      if (!last || last.role !== "assistant") {
        // 观察流先于最终文本到达：补一条空的 assistant 消息承载工具行
        return {
          ...state,
          messages: [...state.messages, { role: "assistant", text: "", toolLines: events }],
        };
      }
      // 合并更新：同一工具（round#call）完成时更新原行状态，不新增行
      const merged = mergeToolLines(last.toolLines ?? [], events);
      const messages = [...state.messages.slice(0, -1), { ...last, toolLines: merged }];
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

/** progress 事件 → 工具行（结构化；assistant_commentary 保持文本） */
export function progressEventToLine(ev: ProgressEvent): ToolLine | string {
  if (ev.kind === "assistant_commentary") {
    return ev.text;
  }
  const round = ev.round ?? 0;
  const callIndex = ev.call_index ?? 1;
  return {
    key: `${round}#${callIndex}`,
    round,
    callIndex,
    tool: ev.tool ?? "",
    status: ev.status ?? ev.phase ?? "",
    detail: ev.detail ?? "",
  };
}

/** 合并工具行：同 key 更新状态（完成/失败覆盖开始），新 key 追加 */
export function mergeToolLines(existing: (ToolLine | string)[], incoming: (ToolLine | string)[]): (ToolLine | string)[] {
  const merged = new Map<string, ToolLine>();
  // 保留既有工具行（文本行原样透传）
  const textLines = existing.filter((l): l is string => typeof l === "string");
  for (const line of existing) {
    if (typeof line === "string") continue;
    merged.set(line.key, line);
  }
  for (const line of incoming) {
    if (typeof line === "string") continue;
    const prev = merged.get(line.key);
    // 完成/失败等终态覆盖开始态；同态不重复追加
    if (prev) {
      merged.set(line.key, { ...prev, status: line.status, detail: line.detail || prev.detail });
    } else {
      merged.set(line.key, line);
    }
  }
  return [...textLines, ...merged.values()];
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
