/**
 * markdown → 终端渲染块（2026-08-17 完整版补齐：助手回复 markdown 渲染）。
 * markdown-it 解析 → 结构化块（heading/code/text/list/quote/hr）+
 * 内联样式分段（**bold** / `code` / [link](url) 显示为链接色）。
 */

import MarkdownIt from "markdown-it";

const md = new MarkdownIt({ html: false, linkify: false, breaks: false });

export interface InlineSegment {
  text: string;
  bold?: boolean;
  code?: boolean;
  link?: boolean;
}

export interface MdBlock {
  kind: "heading" | "code" | "text" | "list" | "quote" | "hr" | "empty" | "table";
  text: string;
  level?: number;
  /** 内联分段（text/heading/list/quote 用） */
  segments?: InlineSegment[];
  /** 表格结构化：每行 = 单元格文本数组（2026-08-18 表格对齐渲染） */
  rows?: string[][];
}

/** 内联解析：**bold** / `code` / [text](url) */
export function inlineSegments(text: string): InlineSegment[] {
  const segments: InlineSegment[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) segments.push({ text: text.slice(last, idx) });
    const token = m[0];
    if (token.startsWith("**")) {
      segments.push({ text: token.slice(2, -2), bold: true });
    } else if (token.startsWith("`")) {
      segments.push({ text: token.slice(1, -1), code: true });
    } else {
      const linkText = token.match(/\[([^\]]+)\]/)?.[1] ?? token;
      segments.push({ text: linkText, link: true });
    }
    last = idx + token.length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return segments.length > 0 ? segments : [{ text }];
}

/** markdown 文本 → 渲染块（容器跟踪：heading/list/quote 的 inline 文本归位）
 * 2026-08-18 表格结构化：markdown-it 默认解析 GFM 表格（th/td 是独立 inline），
 * 这里把 table_open→table_close 间的单元格按行重组为 MdBlock.rows（对齐渲染用）。 */
export function markdownToBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  const tokens = md.parse(text, {});
  let codeBuffer: string[] = [];
  // 容器跟踪：当前打开的 heading/list_item/quote 会把下一个 inline 归入
  let pendingHeading: number | null = null;
  let pendingList = false;
  let pendingQuote = false;
  // 表格跟踪：行数组 + 当前行 + 当前单元格（字符串不可变，必须按索引写回行数组）
  let tableRows: string[][] | null = null;
  let tableRow: string[] | null = null;
  let tableCell: string | null = null;
  let tableCellIndex = -1;

  const flushCode = () => {
    if (codeBuffer.length > 0) {
      blocks.push({ kind: "code", text: codeBuffer.join("\n") });
      codeBuffer = [];
    }
  };

  const flushTable = () => {
    if (tableRows !== null && tableRows.length > 0) {
      blocks.push({ kind: "table", text: "", rows: tableRows });
    }
    tableRows = null;
    tableRow = null;
    tableCell = null;
  };

  for (const token of tokens) {
    switch (token.type) {
      case "fence":
      case "code_block":
        flushTable();
        codeBuffer.push(token.content);
        break;
      case "inline": {
        flushCode();
        const content = token.content.trim();
        if (tableCell !== null && tableRow !== null && tableCellIndex >= 0) {
          // 单元格内联：追加（字符串不可变 → 按索引写回行数组）
          tableCell += content ? content : "";
          tableRow[tableCellIndex] = tableCell;
          break;
        }
        if (!content) break;
        const segments = inlineSegments(content);
        if (pendingHeading !== null) {
          blocks.push({ kind: "heading", text: content, level: pendingHeading, segments });
          pendingHeading = null;
        } else if (pendingList) {
          blocks.push({ kind: "list", text: content, segments });
          pendingList = false;
        } else if (pendingQuote) {
          blocks.push({ kind: "quote", text: content, segments });
          pendingQuote = false;
        } else {
          blocks.push({ kind: "text", text: content, segments });
        }
        break;
      }
      case "table_open": {
        flushCode();
        tableRows = [];
        break;
      }
      case "thead_open":
      case "tbody_open":
        break;
      case "tr_open": {
        if (tableRows !== null) {
          tableRow = [];
          tableRows.push(tableRow);
        }
        break;
      }
      case "th_open":
      case "td_open": {
        if (tableRow !== null) {
          tableCell = "";
          tableRow.push(tableCell);
          tableCellIndex = tableRow.length - 1;
        }
        break;
      }
      case "th_close":
      case "td_close":
        tableCell = null;
        tableCellIndex = -1;
        break;
      case "tr_close":
        tableRow = null;
        tableCell = null;
        tableCellIndex = -1;
        break;
      case "table_close":
        flushTable();
        break;
      case "heading_open": {
        flushTable();
        flushCode();
        pendingHeading = Number(token.tag?.slice(1) ?? 1);
        break;
      }
      case "list_item_open": {
        flushTable();
        flushCode();
        pendingList = true;
        break;
      }
      case "blockquote_open":
        flushTable();
        flushCode();
        pendingQuote = true;
        break;
      case "paragraph_open":
        flushTable();
        flushCode();
        // 段落边界：已有内容时插空行块，防止 markdownToLines 把相邻段落合并
        if (blocks.length > 0 && blocks[blocks.length - 1].kind !== "empty") {
          blocks.push({ kind: "empty", text: "" });
        }
        break;
      case "hr":
        flushTable();
        flushCode();
        blocks.push({ kind: "hr", text: "" });
        break;
      case "hardbreak":
        flushTable();
        flushCode();
        blocks.push({ kind: "empty", text: "" });
        break;
      default:
        break;
    }
  }
  flushTable();
  flushCode();
  return blocks;
}

/** 面向渲染的简化：合并相邻同类块 + heading/list 文本归位 */
export function markdownToLines(text: string): MdBlock[] {
  const raw = markdownToBlocks(text);
  const out: MdBlock[] = [];
  let pendingKind: MdBlock["kind"] | null = null;
  let pending: MdBlock | null = null;
  for (const block of raw) {
    if (block.kind === "text") {
      if (pending && pending.kind === "text") {
        pending.text += block.text;
        pending.segments = [...(pending.segments ?? []), ...(block.segments ?? [])];
      } else {
        pending = { kind: "text", text: block.text, segments: block.segments };
        pendingKind = "text";
        out.push(pending);
      }
      continue;
    }
    pending = null;
    pendingKind = null;
    out.push(block);
  }
  return out;
}
