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

/** 内联分段渲染（**bold** / `code` / [link]）
 * 2026-08-18 复读根修（Ink 5.2 实测定根因）：嵌套 Text（外层包内层子 Text）
 * 渲染 wrap 成多行的文本时，内层 ink-text 的 Yoga 坐标错误 → 同一文本被写到
 * 画布多个 y 位置 → 屏幕/输出出现整块重复（行模型数据 1 份、渲染 N 份）。
 * 修复：单 Text 渲染（无子 Text 嵌套）；内联样式降级为全段统一（bold/code/
 * link 任一存在则整行取该样式）。行模型已按 displayWidth 精确断行，
 * wrap="truncate" 保证 Ink 不二次换行、不误砍。 */
function InlineText({ segments, theme }: { segments: InlineSegment[]; theme: Theme }) {
  const c = theme.colors;
  const text = segments.map((s) => s.text).join("");
  if (segments.some((s) => s.bold)) return <Text bold color={c.assistant} wrap="truncate">{text}</Text>;
  if (segments.some((s) => s.code)) return <Text color={c.code} wrap="truncate">{text}</Text>;
  if (segments.some((s) => s.link)) return <Text color={c.link} underline wrap="truncate">{text}</Text>;
  return <Text color={c.assistant} wrap="truncate">{text}</Text>;
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
  theme,
}: {
  row: RenderRow;
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
      // 2026-08-18 复读根修：单层 Text（嵌套 Text 触发 Ink 5.2 坐标 bug）
      content = <Text color={c.assistant} wrap="truncate">{row.text}</Text>;
      break;
    case "quote":
      content = <Text color={c.status} wrap="truncate">{row.text}</Text>;
      break;
    case "hr":
      content = <Text color={c.border}>{row.text}</Text>;
      break;
    case "table":
      // 表格对齐行（2026-08-18）：边框色 + 表头/分隔线同一色
      content = <Text color={c.border}>{row.text}</Text>;
      break;
    case "empty":
      content = <Text> </Text>;
      break;
    default:
      content = <InlineText segments={row.segments ?? [{ text: row.text }]} theme={theme} />;
  }
  // 2026-08-18 复读根修：间距由扁平空行提供（flattenMessages 消息后空行），
  // 这里不再用 margin——margin 占渲染行但不进行模型，行数漂移导致滚行残留。
  return <Box>{content}</Box>;
});

export function MessageList({
  rows,
  theme,
  showTools = true,
}: {
  rows: FlattenedRow[];
  theme: Theme;
  /** 2026-08-18 照搬 free-code：工具行显示可切换（/show）——隐藏工具行时保留正文/评论 */
  showTools?: boolean;
}) {
  const visible = showTools ? rows : rows.filter((entry) => entry.row.kind !== "tool");
  // 2026-08-18 复读根修（Ink 5.2 DOM bug 实锤）：多个 Text 节点同帧渲染时
  // textContent 相互串扰 → 同一文本被写入多个 y 坐标 → 整块重复（行模型数据
  // 1 份、渲染 N 份）。消息区合并为单 Text（\n 连接，1 次 write）根治；
  // 行模型已保证每行 ≤ 容器宽度，wrap="truncate" 不二次换行。
  // 样式统一 assistant 色（牺牲分 kind 着色，前缀/边框字符保留在文本里）。
  const text = visible.map((entry) => entry.row.text).join("\n");
  return <Text color={theme.colors.assistant} wrap="truncate">{text}</Text>;
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
