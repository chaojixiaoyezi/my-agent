/**
 * 渲染组件（P1-P2）：消息列表 / 工具观察行 / 状态条。
 * 约束：tool_progress 是观察事件（不等于成功），工具状态以结构化结果为准；
 * 工具行着色（进行中青/完成绿/失败红，同 Python 侧 plain_ui 语义）。
 */
import React from "react";
import { Box, Text } from "ink";
import type { ChatMessage, ToolLine } from "../state/session.js";

const CYAN = "#06b6d4";
const GREEN = "#22c55e";
const RED = "#ef4444";
const GRAY = "#9ca3af";
const BLUE = "#3b82f6";
const GRAY_DIM = "#6b7280";

/** 工具行着色：进行中青 / 完成绿 / 失败红 */
export function toolStatusColor(status: string): string {
  if (status.includes("失败") || status.toUpperCase().includes("FAILED")) return RED;
  if (status.includes("完成") || status.toUpperCase().includes("OK")) return GREEN;
  return CYAN;
}

/** 工具行渲染（紧凑单行，2026-08-17 用户指示参考 hermes：一个工具一行） */
function ToolLineRow({ line }: { line: ToolLine }) {
  const detail = line.detail.length > 80 ? `${line.detail.slice(0, 80)}…` : line.detail;
  const toolLabel = line.tool ? ` ${line.tool}` : "";
  const statusLabel = line.status ? ` ${line.status}` : "";
  const detailLabel = detail ? `: ${detail}` : "";
  return (
    <Text color={toolStatusColor(line.status)}>
      [工具] round={line.round ?? 0}#{line.callIndex ?? 1}
      {toolLabel}
      {statusLabel}
      <Text color={GRAY_DIM}>{detailLabel}</Text>
    </Text>
  );
}

export function MessageRow({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <Text color={BLUE} bold>
          ● {msg.text}
        </Text>
      </Box>
    );
  }
  const toolLines = msg.toolLines ?? [];
  return (
    <Box flexDirection="column" marginBottom={1}>
      {toolLines.length > 0 && (
        <Box flexDirection="column">
          {toolLines.map((line, i) =>
            typeof line === "string" ? (
              <Text key={i} color={GRAY}>
                {line}
              </Text>
            ) : (
              <ToolLineRow key={line.key} line={line} />
            ),
          )}
        </Box>
      )}
      {msg.text ? (
        <Text wrap="wrap" color="#e5e7eb">
          {msg.text}
        </Text>
      ) : (
        <Text color={GRAY}>…</Text>
      )}
    </Box>
  );
}

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  return (
    <Box flexDirection="column">
      {messages.map((msg, i) => (
        <MessageRow key={i} msg={msg} />
      ))}
    </Box>
  );
}

export interface StatusBarProps {
  phase: string;
  model: string;
  ctxPercent: number;
  error: string;
  queued: string | null;
}

export function StatusBar({ phase, model, ctxPercent, error, queued }: StatusBarProps) {
  const phaseText =
    phase === "polling" || phase === "submitting"
      ? `处理中… ${spinner(0)}`
      : phase === "done"
        ? "完成"
        : phase === "error"
          ? "错误"
          : "就绪";
  const bar = progressBar(ctxPercent, 10);
  return (
    <Box flexDirection="column">
      <Text color={GRAY}>
        {phase === "polling" || phase === "submitting" ? (
          <Text color={CYAN}>✷ {phaseText}</Text>
        ) : (
          <Text>{phaseText}</Text>
        )}{" "}
        | model {model} | ctx {Math.round(ctxPercent * 200)}K/200K | [{bar}]
      </Text>
      {queued ? <Text color={GRAY}>（排队：{queued.slice(0, 24)}…）</Text> : null}
      {error ? <Text color={RED}>⚠ {error}</Text> : null}
    </Box>
  );
}

function spinner(frame: number): string {
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  return frames[frame % frames.length];
}

function progressBar(ratio: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}
