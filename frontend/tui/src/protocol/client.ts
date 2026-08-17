/**
 * 薄 HTTP client adapter（P0）：ask/progress/result/control 四方法 +
 * 轮询状态机（cursor + 去重 + 轮询重试 + result 收口）。
 *
 * 设计约束（群复核 2525/2528）：
 *   - 只做渲染与输入适配；权限/审批/记忆/工具执行全在后端
 *   - cursor/去重：同一 request 不重复 POST /ask（幂等键）
 *   - /progress 是观察流，完成判定一律以 /result 终态为准
 *   - 错误按结构化 status/error_code 透出，UI 不匹配文本
 */

import type {
  AskAccepted,
  AskRequest,
  ClientError,
  ControlCommand,
  ControlRequest,
  ProgressEvent,
  ProgressResponse,
  ResultResponse,
} from "./types.js";

/** 客户端选项 */
export interface TuiClientOptions {
  baseUrl: string; // e.g. http://127.0.0.1:8420
  /** 进度轮询间隔（ms） */
  pollIntervalMs?: number;
  /** 单次轮询超时（ms） */
  pollTimeoutMs?: number;
  /** 轮询重试次数（网络抖动退避上限） */
  maxPollRetries?: number;
  fetchImpl?: typeof fetch;
}

const DEFAULT_POLL_INTERVAL_MS = 300;
const DEFAULT_POLL_TIMEOUT_MS = 30000;
const DEFAULT_MAX_POLL_RETRIES = 5;

function clientError(err: unknown, kind: ClientError["kind"], status?: number): ClientError {
  if (err && typeof err === "object" && "kind" in err) {
    return err as ClientError;
  }
  const message = err instanceof Error ? err.message : String(err);
  return { kind, status, message };
}

export class TuiHttpClient {
  readonly options: Required<TuiClientOptions>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: TuiClientOptions) {
    this.options = {
      pollIntervalMs: DEFAULT_POLL_INTERVAL_MS,
      pollTimeoutMs: DEFAULT_POLL_TIMEOUT_MS,
      maxPollRetries: DEFAULT_MAX_POLL_RETRIES,
      fetchImpl: globalThis.fetch,
      ...options,
    };
    this.fetchImpl = this.options.fetchImpl;
  }

  /** POST /ask → 202 {request_id} */
  async ask(body: AskRequest): Promise<AskAccepted> {
    const res = await this.request("/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status !== 202) {
      throw await this.errorFrom(res);
    }
    const data = (await res.json()) as Partial<AskAccepted>;
    if (!data.request_id) {
      throw { kind: "protocol", message: "ask 202 响应缺少 request_id" } satisfies ClientError;
    }
    return { request_id: data.request_id, status: "accepted" };
  }

  /** GET /progress/<id>?since=N → 观察事件 + next 游标 */
  async progress(requestId: string, since: number): Promise<ProgressResponse> {
    const res = await this.request(
      `/progress/${encodeURIComponent(requestId)}?since=${since}`,
      { method: "GET" },
    );
    if (res.status !== 200) {
      throw await this.errorFrom(res);
    }
    const data = (await res.json()) as ProgressResponse;
    return {
      request_id: data.request_id ?? requestId,
      events: Array.isArray(data.events) ? data.events : [],
      next: Number.isInteger(data.next) ? data.next : since,
    };
  }

  /** GET /result/<id> → 终态（processing/queued 时返回 undefined + 状态） */
  async result(requestId: string): Promise<{ done: ResultResponse } | { pending: "processing" | "queued" }> {
    const res = await this.request(`/result/${encodeURIComponent(requestId)}`, {
      method: "GET",
    });
    if (res.status === 404) {
      return { pending: "queued" };
    }
    if (res.status !== 200) {
      throw await this.errorFrom(res);
    }
    const data = (await res.json()) as ResultResponse & { status?: string };
    // 未完成态：processing/queued 由网关返回的 status 字段标识（无 response 字段）
    if (
      data.status === "processing" ||
      data.status === "queued" ||
      (data.response === undefined && data.ok === undefined)
    ) {
      return { pending: data.status === "processing" ? "processing" : "queued" };
    }
    return { done: data };
  }

  /** POST /control（会话控制，带 conversation scope） */
  async control(command: ControlCommand, conversation: ControlRequest["conversation"], text?: string): Promise<unknown> {
    const body: ControlRequest = { command, conversation };
    if (text !== undefined) {
      body.text = text;
    }
    const res = await this.request("/control", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status !== 200) {
      throw await this.errorFrom(res);
    }
    return res.json();
  }

  /**
   * 轮询状态机：progress cursor 推进 → result 收口。
   * 返回：{ events: 本次新增事件, final: 终态结果, requestId }
   * 完成判定唯一权威 = /result 终态（progress 轮空不判完成）。
   */
  async pollUntilDone(
    requestId: string,
    onEvents?: (events: ProgressEvent[], cursor: number) => void,
    signal?: AbortSignal,
  ): Promise<{ final: ResultResponse; events: ProgressEvent[]; requestId: string }> {
    let cursor = 0;
    let retries = 0;
    const collected: ProgressEvent[] = [];
    const deadline = Date.now() + this.options.pollTimeoutMs;
    for (;;) {
      if (signal?.aborted) {
        throw { kind: "timeout", message: "poll aborted" } satisfies ClientError;
      }
      // 1) 先收 progress 观察流（非权威，失败可退避重试）
      try {
        const prog = await this.progress(requestId, cursor);
        if (prog.events.length > 0) {
          cursor = prog.next;
          collected.push(...prog.events);
          onEvents?.(prog.events, cursor);
        }
        retries = 0;
      } catch (err) {
        retries += 1;
        if (retries > this.options.maxPollRetries) {
          throw clientError(err, "network");
        }
        // 退避后重试（网络抖动），不丢游标
        await sleep(Math.min(1000, this.options.pollIntervalMs * retries));
        continue;
      }
      // 2) 以 /result 判终态（权威）
      const outcome = await this.result(requestId);
      if ("done" in outcome) {
        return { final: outcome.done, events: collected, requestId };
      }
      if (Date.now() > deadline) {
        throw { kind: "timeout", message: `poll timeout: ${requestId}` } satisfies ClientError;
      }
      await sleep(this.options.pollIntervalMs);
    }
  }

  private async request(path: string, init: RequestInit): Promise<Response> {
    try {
      return await this.fetchImpl(`${this.options.baseUrl}${path}`, init);
    } catch (err) {
      throw clientError(err, "network");
    }
  }

  private async errorFrom(res: Response): Promise<ClientError> {
    let detail = "";
    try {
      const data = (await res.json()) as { error?: string; error_code?: string };
      detail = data.error ?? data.error_code ?? "";
    } catch {
      /* 非 JSON 错误体忽略 */
    }
    return { kind: "http", status: res.status, error_code: detail, message: `HTTP ${res.status} ${detail}` };
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
