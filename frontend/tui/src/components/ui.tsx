/**
 * 渲染组件（完整版）：消息列表（markdown 渲染）/ 工具观察行（单行合并）/
 * 状态条。主题化：全部颜色来自 theme（/theme dark|light 真换肤）。
 */
import React, { memo } from "react";
import { Box, Text } from "ink";
import type { Theme } from "../theme.js";
import type { InlineSegment } from "../markdown.js";
import type { FlattenedRow, RenderRow } from "../rowModel.js";
import { truncateByWidth } from "../width.js";

/** 过滤终端控制序列（群复核 P1：工具输出/回复不得注入终端） */
const ANSI_RE = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)/g;
export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "");
}

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

/** 工具行渲染（紧凑单行，detail 按显示宽度截 80） */
const ToolLineRow = memo(function ToolLineRow({ text, theme }: { text: string; theme: Theme }) {
  return <Text color={toolStatusColor(theme, text)}>{text}</Text>;
});

/**
 * 扁平渲染行（未完成项① 虚拟滚动：行模型渲染）。
 * 行组件 memo 化——长对话增量渲染只处理视口内变化行。
 */
export const RenderRowLine = memo(function RenderRowLine({
  row,
  msgEnd,
  theme,
}: {
  row: RenderRow;
  msgEnd: boolean;
  theme: Theme;
}) {
  const c = theme.colors;
  let content: React.ReactNode;
  switch (row.kind) {
    case "user":
      content = (
        <Text bold color={c.user}>
          {row.text}
        </Text>
      );
      break;
    case "tool":
      content = <Text color={toolStatusColor(theme, row.text)}>{row.text}</Text>;
      break;
    case "commentary":
      content = <Text color={c.status}>{row.text}</Text>;
      break;
    case "heading":
      content = (
        <Text bold color={c.heading}>
          {row.text}
        </Text>
      );
      break;
    case "code":
      content = <Text color={c.code}>{row.text}</Text>;
      break;
    case "list":
      content = (
        <Text>
          <Text color={c.toolStart}>{row.text.slice(0, 2)}</Text>
          <InlineText segments={row.segments ?? [{ text: row.text.slice(2) }]} theme={theme} />
        </Text>
      );
      break;
    case "quote":
      content = (
        <Text color={c.status}>
          <Text color={c.border}>{row.text.slice(0, 2)}</Text>
          <InlineText segments={row.segments ?? [{ text: row.text.slice(2) }]} theme={theme} />
        </Text>
      );
      break;
    case "hr":
      content = <Text color={c.border}>{row.text}</Text>;
      break;
    case "empty":
      content = <Text> </Text>;
      break;
    default:
      content = (
        <Text color={c.assistant}>
          <InlineText segments={row.segments ?? [{ text: row.text }]} theme={theme} />
        </Text>
      );
  }
  return <Box marginBottom={msgEnd ? 1 : 0}>{content}</Box>;
});

export function MessageList({ rows, theme }: { rows: FlattenedRow[]; theme: Theme }) {
  return (
    <Box flexDirection="column">
      {rows.map((entry, i) => (
        <RenderRowLine key={i} row={entry.row} msgEnd={entry.msgEnd} theme={theme} />
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
  /** 未完成项③：网关断线重连中（轮询退避窗口内） */
  reconnecting?: boolean;
  theme: Theme;
}

export function StatusBar({ phase, model, ctxPercent, ctxTokens, error, queued, reconnecting, theme }: StatusBarProps) {
  const c = theme.colors;
  const busy = phase === "polling" || phase === "submitting";
  const phaseText = busy ? "处理中…" : phase === "done" ? "完成" : phase === "error" ? "错误" : "就绪";
  const bar = progressBar(ctxPercent, 10);
  // ctx 进行中显示 …（token 只在完成时由 result 更新；0 会误导「用完了」）
  const ctxText = busy && ctxTokens <= 0 ? "…" : `${Math.round(ctxTokens / 1000)}K`;
  return (
    <Box flexDirection="column">
      <Text color={c.status}>
        {busy ? <Text color={c.toolStart}>✷ {phaseText}</Text> : <Text>{phaseText}</Text>}
        {reconnecting ? <Text color={c.toolFail}> ⚠ 网关重连中…</Text> : null} | model {model} | ctx {ctxText}
        /200K | [{bar}]
      </Text>
      {queued ? <Text color={c.dim}>（排队：{truncateByWidth(queued, 24).text}）</Text> : null}
      {error ? <Text color={c.toolFail}>⚠ {error}</Text> : null}
    </Box>
  );
}

function progressBar(ratio: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}
