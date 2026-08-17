/**
 * 渲染组件（完整版）：消息列表（markdown 渲染）/ 工具观察行（单行合并）/
 * 状态条。主题化：全部颜色来自 theme（/theme dark|light 真换肤）。
 */
import React from "react";
import { Box, Text } from "ink";
import type { ChatMessage, ToolLine } from "../state/session.js";
import type { Theme } from "../theme.js";
import { markdownToLines, type InlineSegment } from "../markdown.js";

/** 工具行着色：进行中青 / 完成绿 / 失败红（theme） */
export function toolStatusColor(theme: Theme, status: string): string {
  const c = theme.colors;
  if (status.includes("失败") || status.toUpperCase().includes("FAILED")) return c.toolFail;
  if (status.includes("完成") || status.toUpperCase().includes("OK")) return c.toolDone;
  return c.toolStart;
}

/** 内联分段渲染（**bold** / `code` / [link]） */
function InlineText({ segments, theme }: { segments: InlineSegment[]; theme: Theme }) {
  const c = theme.colors;
  return (
    <Text wrap="wrap">
      {segments.map((seg, i) =>
        seg.bold ? (
          <Text key={i} bold>
            {seg.text}
          </Text>
        ) : seg.code ? (
          <Text key={i} color={c.code}>
            {seg.text}
          </Text>
        ) : seg.link ? (
          <Text key={i} color={c.link} underline>
            {seg.text}
          </Text>
        ) : (
          <Text key={i}>{seg.text}</Text>
        ),
      )}
    </Text>
  );
}

/** 工具行渲染（紧凑单行） */
function ToolLineRow({ line, theme }: { line: ToolLine; theme: Theme }) {
  const c = theme.colors;
  const detail = line.detail.length > 80 ? `${line.detail.slice(0, 80)}…` : line.detail;
  const toolLabel = line.tool ? ` ${line.tool}` : "";
  const statusLabel = line.status ? ` ${line.status}` : "";
  const detailLabel = detail ? `: ${detail}` : "";
  return (
    <Text color={toolStatusColor(theme, line.status)}>
      [工具] round={line.round ?? 0}#{line.callIndex ?? 1}
      {toolLabel}
      {statusLabel}
      <Text color={c.toolDetail}>{detailLabel}</Text>
    </Text>
  );
}

/** markdown 块渲染（heading/code/text/list/quote/hr） */
function MarkdownBody({ text, theme }: { text: string; theme: Theme }) {
  const c = theme.colors;
  const blocks = markdownToLines(text);
  return (
    <Box flexDirection="column">
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "heading":
            return (
              <Text key={i} color={c.heading} bold>
                {"#".repeat(block.level ?? 1)} {block.text}
              </Text>
            );
          case "code":
            return (
              <Box key={i} flexDirection="column" borderStyle="round" borderColor={c.border} paddingX={1}>
                {block.text.split("\n").map((line, j) => (
                  <Text key={j} color={c.code}>
                    {line}
                  </Text>
                ))}
              </Box>
            );
          case "list":
            return (
              <Text key={i} wrap="wrap">
                <Text color={c.toolStart}>• </Text>
                <InlineText segments={block.segments ?? [{ text: block.text }]} theme={theme} />
              </Text>
            );
          case "quote":
            return (
              <Text key={i} wrap="wrap" color={c.status}>
                <Text color={c.border}>│ </Text>
                <InlineText segments={block.segments ?? [{ text: block.text }]} theme={theme} />
              </Text>
            );
          case "hr":
            return (
              <Text key={i} color={c.border}>
                ───────────────────────────────
              </Text>
            );
          case "empty":
            return <Text key={i}> </Text>;
          default:
            return (
              <Text key={i} wrap="wrap" color={c.assistant}>
                <InlineText segments={block.segments ?? [{ text: block.text }]} theme={theme} />
              </Text>
            );
        }
      })}
    </Box>
  );
}

export function MessageRow({ msg, theme }: { msg: ChatMessage; theme: Theme }) {
  const c = theme.colors;
  if (msg.role === "user") {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <Text color={c.user} bold>
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
              <Text key={i} color={c.status}>
                {line}
              </Text>
            ) : (
              <ToolLineRow key={line.key} line={line} theme={theme} />
            ),
          )}
        </Box>
      )}
      {msg.text ? (
        <MarkdownBody text={msg.text} theme={theme} />
      ) : (
        <Text color={c.dim}>…</Text>
      )}
    </Box>
  );
}

export function MessageList({ messages, theme }: { messages: ChatMessage[]; theme: Theme }) {
  return (
    <Box flexDirection="column">
      {messages.map((msg, i) => (
        <MessageRow key={i} msg={msg} theme={theme} />
      ))}
    </Box>
  );
}

export interface StatusBarProps {
  phase: string;
  model: string;
  ctxPercent: number;
  ctxTokens: number;
  error: string;
  queued: string | null;
  theme: Theme;
}

export function StatusBar({ phase, model, ctxPercent, ctxTokens, error, queued, theme }: StatusBarProps) {
  const c = theme.colors;
  const busy = phase === "polling" || phase === "submitting";
  const phaseText = busy ? "处理中…" : phase === "done" ? "完成" : phase === "error" ? "错误" : "就绪";
  const bar = progressBar(ctxPercent, 10);
  return (
    <Box flexDirection="column">
      <Text color={c.status}>
        {busy ? <Text color={c.toolStart}>✷ {phaseText}</Text> : <Text>{phaseText}</Text>} | model {model} | ctx{" "}
        {Math.round(ctxTokens / 1000)}K/200K | [{bar}]
      </Text>
      {queued ? <Text color={c.dim}>（排队：{queued.slice(0, 24)}…）</Text> : null}
      {error ? <Text color={c.toolFail}>⚠ {error}</Text> : null}
    </Box>
  );
}

function progressBar(ratio: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}
