/**
 * 行模型（未完成项① 虚拟滚动）：消息 → 渲染行数组。
 * 三一致原则：行展开（本文件）= 行数估算 = 视口渲染的唯一事实来源——
 * 裁剪=slice、行数=length、渲染=逐行，杜绝估算与渲染两层实现漂移。
 * 全部按显示宽度（width.ts）断行，中文/emoji 不劈半。
 */
import type { ChatMessage, ToolLine } from "./state/session.js";
import { markdownToLines, type InlineSegment } from "./markdown.js";
import { displayWidth, truncateByWidth, wrapByWidth } from "./width.js";

/** 渲染行种类（与 markdown 块对齐 + 消息级行） */
export type RenderRowKind =
  | "user"
  | "tool"
  | "commentary"
  | "heading"
  | "code"
  | "text"
  | "list"
  | "quote"
  | "hr"
  | "table"
  | "empty";

export interface RenderRow {
  kind: RenderRowKind;
  /** 已断行的渲染文本（code 行含文本边框） */
  text: string;
  level?: number;
  segments?: InlineSegment[];
}

/** 扁平行：行内容 + 是否消息末行（消息间距） */
export interface FlattenedRow {
  row: RenderRow;
  msgEnd: boolean;
}

/** 工具行 → 渲染文本（与 ToolLineRow 渲染一致：detail 按显示宽度截 80） */
export function toolLineToText(line: ToolLine): string {
  const detail = truncateByWidth(line.detail ?? "", 80).text;
  const toolLabel = line.tool ? ` ${line.tool}` : "";
  const statusLabel = line.status ? ` ${line.status}` : "";
  const detailLabel = detail ? `: ${detail}` : "";
  return `[工具] round=${line.round ?? 0}#${line.callIndex ?? 1}${toolLabel}${statusLabel}${detailLabel}`;
}

/** 单条消息 → 渲染行数组（按显示宽度断行） */
export function messageToRows(msg: ChatMessage, contentWidth: number): RenderRow[] {
  const rows: RenderRow[] = [];
  if (msg.role === "user") {
    // 首行带 ❯ 前缀（2026-08-18 free-code 风格：❯ 表示用户输入），其余行纯文本
    const lines = wrapByWidth(msg.text, Math.max(2, contentWidth - 2));
    lines.forEach((line, i) => {
      rows.push({ kind: "user", text: i === 0 ? `❯ ${line}` : line });
    });
    return rows;
  }
  const toolLines = msg.toolLines ?? [];
  for (const line of toolLines) {
    if (typeof line === "string") {
      for (const l of wrapByWidth(line, contentWidth)) rows.push({ kind: "commentary", text: l });
    } else {
      rows.push({ kind: "tool", text: toolLineToText(line) });
    }
  }
  if (msg.text) {
    rows.push(...markdownRowsToRenderRows(msg.text, contentWidth));
  } else if (rows.length === 0) {
    rows.push({ kind: "empty", text: "" });
  }
  return rows;
}

/** 表格行 → 对齐渲染行（free-code/claude-code 风格：│ 列 │ 列 │ + 表头分隔线）
 * 列宽 = 该列最大 displayWidth + 2 padding；超宽列截断（不劈中文）。 */
function tableToRenderRows(block: { rows?: string[][] }, contentWidth: number): RenderRow[] {
  const raw = block.rows ?? [];
  if (raw.length === 0) return [];
  const colCount = Math.max(...raw.map((r) => r.length), 0);
  if (colCount === 0) return [];
  const cols = Array.from({ length: colCount }, (_, c) => raw.map((r) => r[c] ?? ""));
  // 可用宽度：边框字符（每列 | + 分隔）外留 1 边距
  const borderWidth = colCount + 1;
  const usable = Math.max(8, contentWidth - borderWidth);
  // 每列目标宽度：自然宽（含 2 padding）→ 超可用时按比例压缩
  const natural = cols.map((cells) => Math.max(2, ...cells.map((cell) => displayWidth(cell) + 2)));
  const totalNatural = natural.reduce((a, b) => a + b, 0);
  const widths = natural.map((w) =>
    totalNatural <= usable ? w : Math.max(2, Math.floor((w / totalNatural) * usable)),
  );
  const renderCells = (cells: string[]): string => {
    const padded = cells.map((cell, c) => {
      const max = widths[c] ?? 2;
      const fit = truncateByWidth(cell, Math.max(1, max - 2)).text;
      return " " + fit + " ".repeat(Math.max(0, max - 2 - displayWidth(fit))) + " ";
    });
    return `│${padded.join("│")}│`;
  };
  const out: RenderRow[] = [];
  raw.forEach((cells, i) => {
    out.push({ kind: "table", text: renderCells(cells) });
    if (i === 0) {
      // 表头下分隔线
      const seg = widths.map((w) => "─".repeat(w));
      out.push({ kind: "table", text: `├${seg.join("┼")}┤` });
    }
  });
  return out;
}

/** markdown 文本 → 渲染行（块级展开：代码块文本边框、其余按宽度 wrap） */
function markdownRowsToRenderRows(text: string, contentWidth: number): RenderRow[] {
  const rows: RenderRow[] = [];
  const blocks = markdownToLines(text);
  for (const block of blocks) {
    switch (block.kind) {
      case "table":
        rows.push(...tableToRenderRows(block, contentWidth));
        break;
      case "code": {
        // 文本边框（视觉等价 Box border，但可逐行裁剪）
        const inner = Math.max(2, contentWidth - 4);
        rows.push({ kind: "code", text: `┌─${"─".repeat(Math.min(inner, 24))}` });
        for (const rawLine of block.text.split("\n")) {
          for (const l of wrapByWidth(rawLine, inner)) {
            rows.push({ kind: "code", text: `│ ${l}` });
          }
        }
        rows.push({ kind: "code", text: `└─${"─".repeat(Math.min(inner, 24))}` });
        break;
      }
      case "heading": {
        const prefix = "#".repeat(block.level ?? 1) + " ";
        const indent = displayWidth(prefix);
        for (const l of wrapByWidth(block.text, Math.max(2, contentWidth - indent))) {
          rows.push({ kind: "heading", text: prefix + l, level: block.level, segments: block.segments });
        }
        break;
      }
      case "list": {
        for (const l of wrapByWidth(block.text, Math.max(2, contentWidth - 2))) {
          rows.push({ kind: "list", text: `• ${l}`, segments: block.segments });
        }
        break;
      }
      case "quote": {
        for (const l of wrapByWidth(block.text, Math.max(2, contentWidth - 2))) {
          rows.push({ kind: "quote", text: `│ ${l}`, segments: block.segments });
        }
        break;
      }
      case "hr":
        rows.push({ kind: "hr", text: "─".repeat(30) });
        break;
      case "empty":
        rows.push({ kind: "empty", text: "" });
        break;
      default:
        for (const l of wrapByWidth(block.text, contentWidth)) {
          rows.push({ kind: "text", text: l, segments: block.segments });
        }
    }
  }
  return rows;
}

/**
 * 全部消息扁平化为渲染行。
 * 三一致原则（2026-08-18 复读根修）：消息间距必须是扁平行的一部分——
 * 每条消息后追加一条空行（msgEnd 标记），行模型行数 == 实际渲染行数。
 * 旧实现用 RenderRowLine 的 marginBottom=1 做间距：margin 占 Ink 渲染行但
 * 不计入行模型 → 视口内渲染行数 > 视口高度 → 终端滚行 → 屏幕残留旧帧 →
 * 同一文本出现在两处（真机「报告交付复读」根因）。
 * 纯函数：App 用 useMemo(messages, contentWidth) 缓存——长对话只重算增量。
 */
export function flattenMessages(messages: ChatMessage[], contentWidth: number): FlattenedRow[] {
  const flat: FlattenedRow[] = [];
  for (const msg of messages) {
    const rows = messageToRows(msg, contentWidth);
    for (let i = 0; i < rows.length; i += 1) {
      flat.push({ row: rows[i], msgEnd: i === rows.length - 1 });
    }
    // 消息间距 = 扁平空行（计入行数；渲染层不再用 margin）
    flat.push({ row: { kind: "empty", text: "" }, msgEnd: true });
  }
  return flat;
}

/**
 * 贴底窗口视口切片：始终显示最后 viewportRows 行。
 * 返回扁平行的切片（可能少于 viewportRows——消息不足时全量）。
 */
export function viewportSlice(flat: FlattenedRow[], viewportRows: number): FlattenedRow[] {
  if (viewportRows <= 0) return flat;
  const start = Math.max(0, flat.length - viewportRows);
  return flat.slice(start);
}
