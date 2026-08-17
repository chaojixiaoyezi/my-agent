/**
 * TUI 主界面（P1）：消息列表 + 输入框 + 状态条，连 HTTP client。
 * 流程：输入 → ask（幂等键防重复）→ progress 轮询（观察流）→ result 收口。
 * 排队：polling 中再提交 → queuedPrompt，当前轮 done 后自动再提交。
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput, useStdout } from "ink";
import { COMMANDS } from "./commands.js";
import { MultiLineInput } from "./components/multilineInput.js";
import { TuiHttpClient } from "./protocol/client.js";
import type { ClientError, ResultResponse } from "./protocol/types.js";
import { MessageList, StatusBar } from "./components/ui.js";
import { themeByName } from "./theme.js";
import { commandByName, helpText, parseCommand } from "./commands.js";
import {
  buildAskRequest,
  createInitialSession,
  makeIdempotencyKey,
  sessionReducer,
  type SessionState,
} from "./state/session.js";

export interface AppProps {
  client: TuiHttpClient;
  sessionId: string;
  model: string;
  history?: { role: "user" | "assistant"; text: string }[];
}

export function App({ client, sessionId, model, history }: AppProps) {
  const { exit } = useApp();
  const [state, dispatch] = React.useReducer(sessionReducer, undefined, () => ({
    ...createInitialSession(),
    // 历史恢复（完整版③）：启动时注入 /history 拉取的会话历史（重启接续）
    messages: (history ?? []).map((h) => ({ role: h.role, text: h.text })),
  }));
  const [input, setInput] = useState("");
  const [themeName, setThemeName] = useState<"dark" | "light">("dark");
  const [ctxTokens, setCtxTokens] = useState(0);
  const theme = themeByName(themeName);
  const busyRef = useRef(false);
  const stateRef = useRef<SessionState>(state);
  stateRef.current = state;
  // /stop 主动中断当前轮询（2026-08-17 真机：stop 后 poll 等超时报错）
  const pollAbortRef = useRef<AbortController | null>(null);
  // 后端权威命令表（群复核 P1）：启动拉取，Tab 补全/帮助以后端为准 + 本地命令
  const [serverCommands, setServerCommands] = useState<{ name: string }[]>([]);
  useEffect(() => {
    void client.commands().then((cmds) => setServerCommands(cmds)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 一轮完整提交：ask（幂等）→ pollUntilDone（progress + result 收口） */
  const runTurn = useCallback(
    async (prompt: string) => {
      if (busyRef.current) {
        // 群复核 P1：busy 时不再静默丢弃——进 reducer 排队（done 后消费）
        dispatch({ type: "queue", prompt });
        return;
      }
      busyRef.current = true;
      const key = makeIdempotencyKey();
      const controller = new AbortController();
      pollAbortRef.current = controller;
      try {
        dispatch({ type: "submit", prompt, sessionId });
        const accepted = await client.ask(buildAskRequest(prompt, sessionId, key));
        const outcome = await client.pollUntilDone(
          accepted.request_id,
          (events) => {
            dispatch({ type: "progress", events });
          },
          controller.signal,
        );
        // ctx 实时：从 result 的结构化 token 估计取（有则显示真实值）
        const final = outcome.final as ResultResponse;
        const rec = final as Record<string, unknown>;
        const tokenEst =
          Number(rec.current_context_token_estimate ?? 0) ||
          Number(rec.prompt_token_estimate ?? 0) ||
          Number(rec.turn_token_estimate ?? 0);
        if (tokenEst > 0) setCtxTokens(tokenEst);
        dispatch({ type: "done", result: outcome.final });
      } catch (err) {
        const e = err as ClientError;
        // /stop 主动中断不算错误（真机 2026-08-17）
        if (controller.signal.aborted) {
          dispatch({ type: "done", result: { ok: true, response: "（已停止）" } });
        } else {
          dispatch({ type: "error", message: e.message });
        }
      } finally {
        busyRef.current = false;
        // 排队消费：polling 期间提交的消息在 done 后自动再跑（consume 防重复）
        const queued = stateRef.current.queuedPrompt;
        if (queued) {
          dispatch({ type: "consume_queue" });
          void runTurn(queued);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, sessionId],
  );

  // 输入历史（↑↓）与斜杠补全（Tab）
  const inputHistoryRef = useRef<string[]>([]);
  const historyIdxRef = useRef(-1);
  const tabCycleRef = useRef(0);

  useInput((_input, keys) => {
    if (keys.escape) {
      exit();
      return;
    }
    // ↑↓ 输入历史（仅空闲时）
    if ((keys.upArrow || keys.downArrow) && state.phase === "idle") {
      const history = inputHistoryRef.current;
      if (history.length === 0) return;
      const next = keys.upArrow
        ? Math.max(0, historyIdxRef.current === -1 ? history.length - 1 : historyIdxRef.current - 1)
        : Math.min(history.length - 1, historyIdxRef.current + 1);
      historyIdxRef.current = next;
      setInput(history[next] ?? "");
      return;
    }
    // Tab 斜杠补全：/ 开头循环候选命令（后端权威 + 本地命令合并）
    if (keys.tab && input.startsWith("/")) {
      const prefix = input.slice(1).toLowerCase();
      const serverNames = serverCommands.map((c) => c.name);
      const merged = [
        ...COMMANDS.map((c) => c.name),
        ...serverNames.filter((n) => !COMMANDS.some((c) => c.name === n)),
      ];
      const matches = merged.filter((n) => n.startsWith(prefix));
      if (matches.length === 0) return;
      const idx = tabCycleRef.current % matches.length;
      tabCycleRef.current += 1;
      setInput(`/${matches[idx]} `);
      return;
    }
    tabCycleRef.current = 0;
  });

  const onSubmit = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      if (!trimmed) return;
      // 记录输入历史（完整版②：↑↓ 复用）
      const history = inputHistoryRef.current;
      if (history[history.length - 1] !== trimmed) history.push(trimmed);
      if (history.length > 100) history.shift();
      historyIdxRef.current = -1;
      setInput("");
      // 命令处理（P3）：/ 开头走命令目录；会话控制（stop/btw/goal）走 /control
      const cmd = parseCommand(trimmed);
      if (cmd) {
        void handleCommand(cmd.name, cmd.args);
        return;
      }
      void runTurn(trimmed);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runTurn],
  );

  const handleCommand = useCallback(
    async (name: string, args: string) => {
      const command = commandByName(name);
      if (!command) {
        dispatch({ type: "error", message: `未知命令 /${name}（/help 查看）` });
        return;
      }
      if (command.control) {
        try {
          await client.control(
            command.control,
            {
              channel: "chat",
              channel_conversation_id: sessionId,
              channel_user_id: "local-agent",
              canonical_user_id: "local-agent",
            },
            args || undefined,
          );
          if (command.control === "stop") {
            // /stop 主动中断当前轮询（真机 2026-08-17：stop 后 poll 等超时）
            pollAbortRef.current?.abort();
          }
          dispatch({ type: "done", result: { ok: true, response: `/${name} 已发送` } });
        } catch (err) {
          const e = err as ClientError;
          dispatch({ type: "error", message: `/${name} 失败: ${e.message}` });
        }
        return;
      }
      switch (command.local) {
        case "help":
          dispatch({ type: "done", result: { ok: true, response: helpText() } });
          return;
        case "clear":
          process.stdout.write("\x1b[2J\x1b[H");
          return;
        case "exit":
          exit();
          return;
        case "theme": {
          const next: "dark" | "light" =
            args === "light" ? "light" : args === "dark" ? "dark" : themeName === "dark" ? "light" : "dark";
          setThemeName(next);
          dispatch({ type: "done", result: { ok: true, response: `主题已切换为 ${next}` } });
          return;
        }
        default:
          dispatch({ type: "error", message: `命令 /${name} 未实现` });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, sessionId, exit, theme],
  );

  const messages = state.messages;
  // 消息滚屏（完整版②）：消息区高度 = 终端行数 - 固定区（标题/输入/状态/提示），
  // 始终显示最近的消息（窗口滚动语义）
  const { stdout } = useStdout();
  const rows = stdout.rows ?? 24;
  const fixedRows = 6; // 标题1 + 输入1 + 状态1 + 提示1 + 边距2
  const msgHeight = Math.max(5, rows - fixedRows);
  const visibleMessages = messages.slice(-msgHeight);

  return (
    <Box flexDirection="column" paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color={theme.colors.heading}>
          myagent TUI
        </Text>
        <Text color={theme.colors.dim}> (gateway client)</Text>
      </Box>
      <Box flexDirection="column" marginBottom={1}>
        <MessageList messages={visibleMessages} theme={theme} />
      </Box>
      <Box>
        <Text color={theme.colors.prompt}>❯ </Text>
        <MultiLineInput value={input} onChange={setInput} onSubmit={onSubmit} theme={theme} />
      </Box>
      <StatusBar
        phase={state.phase}
        model={model}
        ctxPercent={0}
        ctxTokens={ctxTokens}
        error={state.error}
        queued={state.queuedPrompt}
        theme={theme}
      />
      <Text color={theme.colors.dim}>输入消息回车发送 · Esc 退出</Text>
    </Box>
  );
}
