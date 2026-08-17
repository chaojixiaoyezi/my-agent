/**
 * TUI 主界面（P1）：消息列表 + 输入框 + 状态条，连 HTTP client。
 * 流程：输入 → ask（幂等键防重复）→ progress 轮询（观察流）→ result 收口。
 * 排队：polling 中再提交 → queuedPrompt，当前轮 done 后自动再提交。
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import { TuiHttpClient } from "./protocol/client.js";
import type { ClientError } from "./protocol/types.js";
import { MessageList, StatusBar } from "./components/ui.js";
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

export function App({ client, sessionId, model, history: _history }: AppProps) {
  const { exit } = useApp();
  const [state, dispatch] = React.useReducer(sessionReducer, undefined, () =>
    createInitialSession(),
  );
  const [input, setInput] = useState("");
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const busyRef = useRef(false);
  const stateRef = useRef<SessionState>(state);
  stateRef.current = state;
  // P1 不做历史恢复（群复核：HTTP 无 history 端点，P4 接 /history API 或明确降级）

  /** 一轮完整提交：ask（幂等）→ pollUntilDone（progress + result 收口） */
  const runTurn = useCallback(
    async (prompt: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      const key = makeIdempotencyKey();
      try {
        dispatch({ type: "submit", prompt, sessionId });
        const accepted = await client.ask(buildAskRequest(prompt, sessionId, key));
        const outcome = await client.pollUntilDone(accepted.request_id, (events) => {
          dispatch({ type: "progress", events });
        });
        dispatch({ type: "done", result: outcome.final });
      } catch (err) {
        const e = err as ClientError;
        dispatch({ type: "error", message: e.message });
      } finally {
        busyRef.current = false;
        // 排队消费：polling 期间提交的消息在 done 后自动再跑
        const queued = stateRef.current.queuedPrompt;
        if (queued) {
          dispatch({ type: "done", result: { ok: true, response: "" } });
          void runTurn(queued);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, sessionId],
  );

  useInput((_input, keys) => {
    if (keys.escape) exit();
  });

  const onSubmit = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      if (!trimmed) return;
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
          const next = args === "light" ? "light" : args === "dark" ? "dark" : theme === "dark" ? "light" : "dark";
          setTheme(next);
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

  return (
    <Box flexDirection="column" paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="#06b6d4">
          myagent TUI
        </Text>
        <Text color="#9ca3af"> (gateway client)</Text>
      </Box>
      <Box flexDirection="column" marginBottom={1}>
        <MessageList messages={messages} />
      </Box>
      <Box>
        <Text color="#22c55e">❯ </Text>
        <TextInput value={input} onChange={setInput} onSubmit={onSubmit} />
      </Box>
      <StatusBar
        phase={state.phase}
        model={model}
        ctxPercent={0}
        error={state.error}
        queued={state.queuedPrompt}
      />
      <Text color="#9ca3af">输入消息回车发送 · Esc 退出</Text>
    </Box>
  );
}
